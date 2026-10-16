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
        plugin/                 the add-on's manifest and agents, as loaded for the run
        agent.json              what Claude Code reported when the agent stopped
        usage.json              tokens, turns, time and cost by role, and the gate verdict
        gate/                   the gate's databases, dbt artifacts and logs

The agent runs headless in Claude Code, on the user's own subscription. It
may edit files in the project folder and run ``dbt``, and nothing else; any
other action is refused rather than asked about. Once it stops, the gate
builds the project from an empty database and checks it against the raw
files. The run passes only if the gate passes. Last, the run's usage is
recorded and printed by role.
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

from tenx.gate import GateError, Result, load_declaration, passed, render, run_gate
from tenx.ledger import Ledger, role_usage
from tenx.ledger import render as render_usage
from tenx.profile import ProfileError, profile_folder

PLUGIN_DIR = Path(__file__).resolve().parent.parent
AGENT = "tenx:engineer"
ROLE = "engineer"
PROMPT = "Build what GOAL.md asks for."
ALLOWED_TOOLS = ("Edit", "Write", "Bash(dbt *)")
MAX_TURNS = 60
MAX_BUDGET_USD = 5
TIMEOUT_MINUTES = 30
STOP_GRACE_SECONDS = 30

PROJECT_DIR = "project"
PLUGIN_COPY_DIR = "plugin"
AGENT_FILE = "agent.json"
USAGE_FILE = "usage.json"
GATE_DIR = "gate"
PROJECT_NAME = "tenx_project"
GOAL_FILE = "GOAL.md"
SOURCE_PROFILES_DIR = "source_profiles"
DEV_DATABASE = "dev.duckdb"


class EngineerError(Exception):
    """The run could not start or its inputs are unusable."""


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


def agent_command(claude: str, plugin_dir: Path) -> list[str]:
    """The Claude Code command line that runs the engineer agent headless."""
    return [
        claude,
        "-p",
        PROMPT,
        "--plugin-dir",
        str(plugin_dir),
        "--agent",
        AGENT,
        "--permission-mode",
        "dontAsk",
        "--allowedTools",
        *ALLOWED_TOOLS,
        "--max-turns",
        str(MAX_TURNS),
        "--max-budget-usd",
        str(MAX_BUDGET_USD),
        "--output-format",
        "json",
        "--no-session-persistence",
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


def usage(agent: AgentRun, results: list[Result]) -> Ledger:
    """The run's usage by role, with the gate's verdict."""
    missing = "timed out" if agent.timed_out_after is not None else "no report"
    return Ledger((role_usage(ROLE, agent.report, missing),), "PASS" if passed(results) else "FAIL")


def run(
    raw: Path,
    goal: Path,
    checks: Path,
    out: Path,
    claude: str = "claude",
    timeout: float = TIMEOUT_MINUTES * 60,
) -> tuple[AgentRun, list[Result]]:
    """Prepare the project, let the agent build it, then run the gate on it."""
    if not (PLUGIN_DIR / "agents" / "engineer.md").is_file():
        raise EngineerError(
            f"the agent definitions are not in {PLUGIN_DIR}; run from a clone of the repository"
        )
    load_declaration(checks)
    out = Path(out)
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise EngineerError(f"{out} must be empty")
    project = out / PROJECT_DIR
    prepare_project(project, raw, goal)
    plugin = stage_plugin(out.resolve() / PLUGIN_COPY_DIR)
    command = agent_command(claude, plugin)
    agent = run_agent(project.resolve(), command, out / AGENT_FILE, timeout)
    results = run_gate(project, raw, checks, out / GATE_DIR)
    usage(agent, results).write(out / USAGE_FILE)
    return agent, results


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
    args = parser.parse_args(argv)
    try:
        agent, results = run(args.raw, args.goal, args.checks, args.out)
    except (EngineerError, GateError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(f"agent: {agent.describe()}")
    print(f"project: {args.out / PROJECT_DIR}")
    print()
    print(render(results), end="")
    print()
    print(render_usage(usage(agent, results)), end="")
    return 0 if passed(results) else 1


if __name__ == "__main__":
    sys.exit(main())
