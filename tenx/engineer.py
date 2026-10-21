"""Have the engineer agent build a dbt project from raw files, then gate it.

    python -m tenx.engineer --raw <folder> --goal <file> --checks <file> --out <folder>

The agent works in a project folder prepared here. The folder holds a dbt
project with no models yet, a profile of every raw table and the goal, and
nothing else: the gate's declaration stays outside it, so the checks cannot
be read or edited by the agent.

    <out>/
        project/                the agent's working folder
            GOAL.md             what to build, in plain English
            source_profiles/    one profile per raw table, as text and JSON
            dbt_project.yml     reads the raw files from the variable raw_dir
            profiles.yml        builds into dev.duckdb inside the folder
            models/             empty; the agent writes the models here
        cross_source/           with --cross-source: the cross-source agent's folder
            GOAL.md
            source_profiles/    the profiles, with overlap.txt and overlap.json
        plugin/                 the add-on's manifest and agents, as loaded for the run
        cross_source.json       with --cross-source: what Claude Code reported for that agent
        agent.json              what Claude Code reported when the agent stopped
        usage.json              tokens, turns, time and cost by role, and the gate verdict
        gate/                   the gate's databases, dbt artifacts and logs

The agent runs headless in Claude Code, on the user's own subscription. It
may edit files in the project folder and run ``dbt``, and nothing else; any
other action is refused rather than asked about. Once it stops, the gate
builds the project from an empty database and checks it against the raw
files. The run passes only if the gate passes. Last, the run's usage is
recorded and printed by role.

With --cross-source, the cross-source agent runs first. It reads the goal,
the profiles and the overlap report in a folder of its own, changes nothing,
and answers how the tables the goal needs join. Its answer is checked, and
written as JOINS.md in the project folder for the engineer. An answer that
fails the check is not passed on; the engineer then works without it.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from tenx.cross_source import NOTES_FILE, SCHEMA, NotesError, notes_from_report
from tenx.cross_source import render as render_notes
from tenx.gate import GateError, Result, load_declaration, passed, render, run_gate
from tenx.ledger import Ledger, role_usage
from tenx.ledger import render as render_usage
from tenx.overlap import overlap_folder, write_report
from tenx.profile import ProfileError, profile_folder
from tenx.sample import sample

PLUGIN_DIR = Path(__file__).resolve().parent.parent
MAX_BUDGET_USD = 5
TIMEOUT_MINUTES = 30
STOP_GRACE_SECONDS = 30

PROJECT_DIR = "project"
PLUGIN_COPY_DIR = "plugin"
AGENT_FILE = "agent.json"
CROSS_SOURCE_DIR = "cross_source"
CROSS_SOURCE_FILE = "cross_source.json"
PROFILE_FILE = "PROFILE.txt"
SAMPLE_FILE = "SAMPLE.jsonl"
USAGE_FILE = "usage.json"
GATE_DIR = "gate"
PROJECT_NAME = "tenx_project"
GOAL_FILE = "GOAL.md"
SOURCE_PROFILES_DIR = "source_profiles"
DEV_DATABASE = "dev.duckdb"


class EngineerError(Exception):
    """The run could not start or its inputs are unusable."""


@dataclass(frozen=True)
class Role:
    """One agent of the add-on, and how the runner starts it."""

    name: str
    agent: str
    prompt: str
    allowed_tools: tuple[str, ...]
    max_turns: int
    json_schema: dict | None = None


ENGINEER = Role(
    name="engineer",
    agent="tenx:engineer",
    prompt="Build what GOAL.md asks for.",
    allowed_tools=("Edit", "Write", "Bash(dbt *)"),
    max_turns=60,
)

# Reads inside its own folder need no approval, so it is allowed nothing more.
CROSS_SOURCE = Role(
    name="cross-source",
    agent="tenx:cross-source",
    prompt="Work out the joins GOAL.md needs.",
    allowed_tools=(),
    max_turns=20,
    json_schema=SCHEMA,
)


def dbt_project_yml(raw: Path) -> str:
    # raw_dir is given a default so that the agent's own `dbt build` reads the
    # raw files; the gate passes its own value on the command line.
    return (
        f"name: {PROJECT_NAME}\n"
        'version: "1.0.0"\n'
        "config-version: 2\n"
        f"profile: {PROJECT_NAME}\n"
        "\n"
        'model-paths: ["models"]\n'
        "\n"
        "vars:\n"
        f"  raw_dir: {json.dumps(str(raw))}\n"
    )


def profiles_yml() -> str:
    return (
        f"{PROJECT_NAME}:\n"
        "  target: dev\n"
        "  outputs:\n"
        "    dev:\n"
        "      type: duckdb\n"
        f"      path: {DEV_DATABASE}\n"
        "      threads: 1\n"
    )


def prepare_project(project: Path, raw: Path, goal: Path) -> list[dict]:
    """Write the folder the agent works in. Returns the source profiles.

    The folder must not exist yet or be empty, so that nothing from an
    earlier attempt is left for the agent to find.
    """
    raw = Path(raw).resolve()
    goal = Path(goal)
    if not raw.is_dir():
        raise EngineerError(f"{raw} is not a folder of raw files")
    if not goal.is_file():
        raise EngineerError(f"{goal} does not exist")
    if not goal.read_text(encoding="utf-8").strip():
        raise EngineerError(f"{goal} is empty")
    project = Path(project)
    if project.exists() and (not project.is_dir() or any(project.iterdir())):
        raise EngineerError(f"{project} must be empty, so that the agent starts from nothing")
    project.mkdir(parents=True, exist_ok=True)
    try:
        profiles = profile_folder(raw, project / SOURCE_PROFILES_DIR)
    except ProfileError as error:
        raise EngineerError(str(error)) from error
    shutil.copyfile(goal, project / GOAL_FILE)
    (project / "dbt_project.yml").write_text(dbt_project_yml(raw), encoding="utf-8")
    (project / "profiles.yml").write_text(profiles_yml(), encoding="utf-8")
    (project / "models").mkdir()
    return profiles


def _duration(seconds: float) -> str:
    if seconds >= 60 and seconds % 60 == 0:
        minutes = int(seconds // 60)
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    return f"{seconds:g} seconds"


@dataclass(frozen=True)
class AgentRun:
    exit_code: int | None
    report: dict | None
    timed_out_after: float | None = None
    message: str = ""

    def describe(self) -> str:
        if self.timed_out_after is not None:
            return f"stopped: did not finish within {_duration(self.timed_out_after)}"
        if self.report is None:
            reason = f": {self.message}" if self.message else ""
            return f"exited {self.exit_code} without a report{reason}"
        outcome = self.report.get("subtype") or "unknown"
        turns = self.report.get("num_turns")
        detail = f"{outcome} after {turns} turns" if turns is not None else outcome
        return f"{detail} (exit {self.exit_code})"


def stage_plugin(destination: Path) -> Path:
    """Copy the add-on's manifest and agents to destination for the run.

    Claude Code protects a folder loaded with --plugin-dir from writes, so a
    project inside it could not be written. A copy beside the project keeps
    the project writable wherever the repository is.
    """
    shutil.copytree(PLUGIN_DIR / ".claude-plugin", destination / ".claude-plugin")
    shutil.copytree(PLUGIN_DIR / "agents", destination / "agents")
    return destination


def agent_command(claude: str, plugin_dir: Path, role: Role = ENGINEER) -> list[str]:
    """The Claude Code command line that runs one of the agents headless."""
    allowed = ["--allowedTools", *role.allowed_tools] if role.allowed_tools else []
    schema = ["--json-schema", json.dumps(role.json_schema)] if role.json_schema else []
    return [
        claude,
        "-p",
        role.prompt,
        "--plugin-dir",
        str(plugin_dir),
        "--agent",
        role.agent,
        "--permission-mode",
        "dontAsk",
        *allowed,
        "--max-turns",
        str(role.max_turns),
        "--max-budget-usd",
        str(MAX_BUDGET_USD),
        "--output-format",
        "json",
        "--no-session-persistence",
        *schema,
    ]


def run_agent(project: Path, command: list[str], report_file: Path, timeout: float) -> AgentRun:
    """Run the agent in the project folder and keep what it reported."""
    # dbt must find the project's own profiles.yml, not one set elsewhere.
    environment = {key: value for key, value in os.environ.items() if key != "DBT_PROFILES_DIR"}
    try:
        process = subprocess.Popen(
            command,
            cwd=project,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError as error:
        raise EngineerError(
            f"{command[0]} was not found; install Claude Code and sign in first"
        ) from error
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # SIGTERM lets Claude Code stop the commands it started before it exits.
        process.terminate()
        try:
            process.communicate(timeout=STOP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
        return AgentRun(process.returncode, None, timed_out_after=timeout)
    report_file.write_text(stdout, encoding="utf-8")
    try:
        report = json.loads(stdout)
    except json.JSONDecodeError:
        report = None
    if not isinstance(report, dict):
        report = None
    lines = stderr.strip().splitlines()
    return AgentRun(process.returncode, report, message=lines[-1] if lines else "")


@dataclass(frozen=True)
class Notes:
    """What became of the cross-source agent's answer."""

    agent: AgentRun
    joins: int | None = None
    cautions: int | None = None
    problem: str = ""

    def describe(self) -> str:
        if self.joins is None:
            return f"{self.agent.describe()}; no notes for the engineer: {self.problem}"
        return (
            f"{self.agent.describe()}; {NOTES_FILE}: {self.joins} "
            f"join{'s' if self.joins != 1 else ''}, {self.cautions} "
            f"caution{'s' if self.cautions != 1 else ''}"
        )


@dataclass(frozen=True)
class Run:
    engineer: AgentRun
    results: list[Result]
    notes: Notes | None = None


def _role_usage(role: Role, agent: AgentRun):
    missing = "timed out" if agent.timed_out_after is not None else "no report"
    return role_usage(role.name, agent.report, missing)


def usage(run: Run) -> Ledger:
    """The run's usage by role, in the order they ran, with the gate's verdict."""
    roles = []
    if run.notes is not None:
        roles.append(_role_usage(CROSS_SOURCE, run.notes.agent))
    roles.append(_role_usage(ENGINEER, run.engineer))
    return Ledger(tuple(roles), "PASS" if passed(run.results) else "FAIL")


def prepare_cross_source(folder: Path, project: Path, raw: Path) -> None:
    """Give the cross-source agent the goal, the profiles and the overlap report."""
    folder.mkdir()
    shutil.copyfile(project / GOAL_FILE, folder / GOAL_FILE)
    shutil.copytree(project / SOURCE_PROFILES_DIR, folder / SOURCE_PROFILES_DIR)
    try:
        write_report(overlap_folder(raw), folder / SOURCE_PROFILES_DIR)
    except ProfileError as error:
        raise EngineerError(str(error)) from error


def run_cross_source(
    out: Path,
    project: Path,
    raw: Path,
    profiles: list[dict],
    plugin: Path,
    claude: str,
    timeout: float,
) -> Notes:
    """Run the cross-source agent and write its checked answer for the engineer."""
    folder = out / CROSS_SOURCE_DIR
    prepare_cross_source(folder, project, raw)
    command = agent_command(claude, plugin, CROSS_SOURCE)
    agent = run_agent(folder.resolve(), command, out / CROSS_SOURCE_FILE, timeout)
    try:
        notes = notes_from_report(agent.report, profiles)
    except NotesError as error:
        return Notes(agent, problem=str(error))
    (project / NOTES_FILE).write_text(render_notes(notes), encoding="utf-8")
    return Notes(agent, len(notes["joins"]), len(notes["cautions"]))


def prepare_scout(folder: Path, project: Path, raw: Path, profile: dict) -> None:
    """Give one scout the goal, its table's profile and a sample of its rows."""
    folder.mkdir(parents=True)
    shutil.copyfile(project / GOAL_FILE, folder / GOAL_FILE)
    table = profile["table"]
    shutil.copyfile(project / SOURCE_PROFILES_DIR / f"{table}.txt", folder / PROFILE_FILE)
    (folder / SAMPLE_FILE).write_text(sample(raw / profile["file"], table), encoding="utf-8")


def run(
    raw: Path,
    goal: Path,
    checks: Path,
    out: Path,
    claude: str = "claude",
    timeout: float = TIMEOUT_MINUTES * 60,
    cross_source: bool = False,
) -> Run:
    """Prepare the project, let the agent build it, then run the gate on it.

    With cross_source, the cross-source agent runs first and its checked
    answer is put in the project as JOINS.md.
    """
    if not (PLUGIN_DIR / "agents" / "engineer.md").is_file():
        raise EngineerError(
            f"the agent definitions are not in {PLUGIN_DIR}; run from a clone of the repository"
        )
    load_declaration(checks)
    out = Path(out)
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise EngineerError(f"{out} must be empty")
    project = out / PROJECT_DIR
    profiles = prepare_project(project, raw, goal)
    plugin = stage_plugin(out.resolve() / PLUGIN_COPY_DIR)
    notes = None
    if cross_source:
        notes = run_cross_source(out, project, Path(raw), profiles, plugin, claude, timeout)
    command = agent_command(claude, plugin)
    agent = run_agent(project.resolve(), command, out / AGENT_FILE, timeout)
    results = run_gate(project, raw, checks, out / GATE_DIR)
    outcome = Run(agent, results, notes)
    usage(outcome).write(out / USAGE_FILE)
    return outcome


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--raw", type=Path, required=True, help="folder of raw source files")
    parser.add_argument("--goal", type=Path, required=True, help="what to build, in plain English")
    parser.add_argument(
        "--checks", type=Path, required=True, help="the gate's JSON declaration of what to expect"
    )
    parser.add_argument(
        "--out", type=Path, required=True, help="empty folder for the project and the run"
    )
    parser.add_argument(
        "--cross-source",
        action="store_true",
        help="run the cross-source agent first and give its notes to the engineer",
    )
    args = parser.parse_args(argv)
    try:
        outcome = run(args.raw, args.goal, args.checks, args.out, cross_source=args.cross_source)
    except (EngineerError, GateError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if outcome.notes is not None:
        print(f"cross-source: {outcome.notes.describe()}")
    print(f"agent: {outcome.engineer.describe()}")
    print(f"project: {args.out / PROJECT_DIR}")
    print()
    print(render(outcome.results), end="")
    print()
    print(render_usage(usage(outcome)), end="")
    return 0 if passed(outcome.results) else 1


if __name__ == "__main__":
    sys.exit(main())
