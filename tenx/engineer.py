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
        scouts/                 with --scouts: one folder per raw table for its scout
            <table>/            GOAL.md, PROFILE.txt and SAMPLE.jsonl
            <table>.json        what Claude Code reported for that scout
        cross_source/           unless --no-cross-source: the cross-source agent's folder
            GOAL.md
            source_profiles/    the profiles, with overlap.txt and overlap.json
        plugin/                 the add-on's manifest and agents, as loaded for the run
        cross_source.json       unless --no-cross-source: what Claude Code reported for that agent
        agent.json              what Claude Code reported when the agent stopped
        usage.json              tokens, turns, time and cost by role, and the gate verdict
        gate/                   the gate's databases, dbt artifacts and logs

The agent runs headless in Claude Code, on the user's own subscription. It
may edit files in the project folder and run ``dbt``, and nothing else; any
other action is refused rather than asked about. Once it stops, the gate
builds the project from an empty database and checks it against the raw
files. The run passes only if the gate passes. Last, the run's usage is
recorded and printed by role.

Every agent runs in a sandbox (bubblewrap on Linux): it sees its own folder,
the add-on, the programs it runs and, for the engineer, the raw files, and
nothing else of the run, the repository or the home folder. Where no sandbox
can be set up the run is refused, unless --unisolated is given, and the
report then says the agents ran without one.

Unless --no-cross-source is given, the cross-source agent runs first. It
reads the goal, the profiles and the overlap report in a folder of its own,
changes nothing, and answers how the tables the goal needs join. Its answer
is checked, and written as JOINS.md in the project folder for the engineer.
An answer that fails the check is not passed on; the engineer then works
without it.

With --scouts, a scout runs for each raw table first, several at a time. Each
reads the goal, its table's profile and a sample of the table's rows in a
folder of its own, changes nothing, and answers what the engineer must
handle in that table. The answers that pass their check are written
together as TABLE_NOTES.md in the project folder.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path

from tenx import artefacts
from tenx.cross_source import NOTES_FILE, SCHEMA, NotesError, notes_from_report
from tenx.cross_source import render as render_notes
from tenx.gate import GateError, Result, load_declaration, passed, render, run_gate
from tenx.ledger import Ledger, role_usage
from tenx.ledger import render as render_usage
from tenx.overlap import overlap_folder, write_report
from tenx.policy import Policy
from tenx.ports import AgentRun, Role
from tenx.profile import ProfileError, profile_folder
from tenx.sample import sample
from tenx.sandbox import (
    Mounts,
    Sandbox,
    SandboxError,
    Unisolated,
    default_sandbox,
    python_folders,
)
from tenx.scouts import NOTES_FILE as TABLE_NOTES_FILE
from tenx.scouts import SCHEMA as SCOUT_SCHEMA
from tenx.scouts import ScoutError
from tenx.scouts import notes_from_report as scout_notes_from_report
from tenx.scouts import render as render_table_notes

PLUGIN_DIR = Path(__file__).resolve().parent.parent
TIMEOUT_MINUTES = 30
STOP_GRACE_SECONDS = 30

PROJECT_DIR = "project"
PLUGIN_COPY_DIR = "plugin"
AGENT_FILE = "agent.json"
CROSS_SOURCE_DIR = "cross_source"
CROSS_SOURCE_FILE = "cross_source.json"
SCOUTS_DIR = "scouts"
PROFILE_FILE = "PROFILE.txt"
SAMPLE_FILE = "SAMPLE.jsonl"
PARALLEL_SCOUTS = 4
USAGE_FILE = "usage.json"
GATE_DIR = "gate"
PROJECT_NAME = "tenx_project"
GOAL_FILE = "GOAL.md"
SOURCE_PROFILES_DIR = "source_profiles"
DEV_DATABASE = "dev.duckdb"
# Claude Code can move each shell command into a memory cgroup. Started from
# inside Claude Code's own shell, it reuses the cgroup it is already in, which
# a sandbox cannot enter because /sys is read-only there, and then no shell
# command can start. Inside the sandbox, the cgroup is turned off.
SANDBOXED_AGENT_ENVIRONMENT = {"CLAUDE_CODE_TOOL_MEMORY_LIMIT": "none"}


class EngineerError(Exception):
    """The run could not start or its inputs are unusable."""


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


# One scout per raw table; the runner names each after its table.
SCOUT = Role(
    name="scout",
    agent="tenx:scout",
    prompt="Note what the engineer must handle in this table.",
    allowed_tools=(),
    max_turns=8,
    json_schema=SCOUT_SCHEMA,
    kind="scout",
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


def stage_plugin(destination: Path) -> Path:
    """Copy the add-on's manifest and agents to destination for the run.

    Claude Code protects a folder loaded with --plugin-dir from writes, so a
    project inside it could not be written. A copy beside the project keeps
    the project writable wherever the repository is.
    """
    shutil.copytree(PLUGIN_DIR / ".claude-plugin", destination / ".claude-plugin")
    shutil.copytree(PLUGIN_DIR / "agents", destination / "agents")
    return destination


def agent_command(
    claude: str, plugin_dir: Path, role: Role = ENGINEER, policy: Policy | None = None
) -> list[str]:
    """The Claude Code command line that runs one of the agents headless.

    The agent's spending cap is the policy's cap for its kind of agent, so
    that what a run reserves for the call is what Claude Code enforces.
    """
    cap = (policy or Policy()).cap(role.budget_kind)
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
        str(cap),
        "--output-format",
        "json",
        "--no-session-persistence",
        *schema,
    ]


def _stop_group(process: subprocess.Popen, grace: float) -> None:
    """Stop the agent and every process it started.

    The agent runs in its own process group, so one signal reaches the
    commands it started as well, such as a dbt build. SIGTERM lets Claude
    Code stop its commands; whatever is still running after the grace period
    is killed.
    """
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    process.wait()


def run_agent(
    project: Path,
    command: list[str],
    report_file: Path,
    timeout: float,
    sandbox: Sandbox | None = None,
    mounts: Mounts | None = None,
) -> AgentRun:
    """Run the agent in the project folder and keep what it reported.

    With a sandbox, the agent sees only what ``mounts`` allow; without one
    it runs unisolated.

    The agent's output goes to files rather than pipes. A command the agent
    started can outlive it and keep a pipe open, which would leave the runner
    waiting for output that never ends; a file cannot hold the runner. The
    report is moved into place only once the agent has finished, so a stopped
    run never leaves a partial report.
    """
    # dbt must find the project's own profiles.yml, not one set elsewhere.
    environment = {key: value for key, value in os.environ.items() if key != "DBT_PROFILES_DIR"}
    report_file.parent.mkdir(parents=True, exist_ok=True)
    partial = report_file.with_name(report_file.name + ".partial")
    errors = report_file.with_name(report_file.name + ".stderr")
    sandbox = sandbox or Unisolated()
    if not isinstance(sandbox, Unisolated):
        environment.update(SANDBOXED_AGENT_ENVIRONMENT)
    try:
        started = sandbox.command(command, mounts or Mounts(workspace=project))
    except SandboxError as error:
        raise EngineerError(str(error)) from error
    with partial.open("w", encoding="utf-8") as out, errors.open("w", encoding="utf-8") as err:
        try:
            process = subprocess.Popen(
                started,
                cwd=project,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                text=True,
                start_new_session=True,
            )
        except FileNotFoundError as error:
            partial.unlink(missing_ok=True)
            errors.unlink(missing_ok=True)
            raise EngineerError(
                f"{command[0]} was not found; install Claude Code and sign in first"
            ) from error
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _stop_group(process, STOP_GRACE_SECONDS)
            partial.unlink(missing_ok=True)
            errors.unlink(missing_ok=True)
            return AgentRun(process.returncode, None, timed_out_after=timeout)
        finally:
            # Commands the agent left running when it ended are stopped too.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    stdout = partial.read_text(encoding="utf-8")
    stderr = errors.read_text(encoding="utf-8")
    errors.unlink(missing_ok=True)
    artefacts.replace(partial, report_file)
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
class ScoutNotes:
    """What became of one scout's answer."""

    table: str
    agent: AgentRun
    findings: int | None = None
    questions: int | None = None
    problem: str = ""

    def describe(self) -> str:
        if self.findings is None:
            return f"{self.agent.describe()}; no notes for the engineer: {self.problem}"
        return (
            f"{self.agent.describe()}; {self.findings} "
            f"finding{'s' if self.findings != 1 else ''}, {self.questions} "
            f"question{'s' if self.questions != 1 else ''}"
        )


@dataclass(frozen=True)
class Run:
    engineer: AgentRun
    results: list[Result]
    notes: Notes | None = None
    scouts: tuple[ScoutNotes, ...] = ()
    sandbox: str | None = None


def claude_config() -> tuple[Path, ...]:
    """Claude Code's own settings and sign-in, which it must be able to read and update."""
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    folder = Path(configured) if configured else Path.home() / ".claude"
    paths = [folder, Path.home() / ".claude.json"]
    return tuple(path for path in paths if path.exists())


def tools(claude: str) -> tuple[Path, ...]:
    """The installed programs an agent runs: Claude Code, Node, Python and dbt.

    The sandbox hides the home folder, where these are often installed, so
    the folders that hold them are shown again, read-only.
    """
    folders = [Path(claude).resolve().parent, *python_folders()]
    for program in ("node", "dbt"):
        found = shutil.which(program)
        if found:
            folder = Path(found).resolve().parent
            # A program in a virtual environment needs the whole environment.
            if (folder.parent / "pyvenv.cfg").is_file():
                folder = folder.parent
            folders.append(folder)
    unique = []
    for folder in folders:
        folder = folder.resolve()
        if folder.is_dir() and folder not in unique:
            unique.append(folder)
    return tuple(unique)


def mounts(
    workspace: Path, out: Path, checks: Path, claude: str, raw: Path | None = None
) -> Mounts:
    """What one agent may see: its own folder, the add-on, its tools, and raw data if it needs it.

    The run folder (with the gate's work and the other agents' reports),
    the declaration and the repository (with its reference answers) are
    hidden.
    """
    shown = [Path(raw)] if raw is not None else []
    shown += [out / PLUGIN_COPY_DIR, *tools(claude)]
    return Mounts(
        workspace=workspace,
        read_only=tuple(shown),
        writable=claude_config(),
        hidden=(PLUGIN_DIR, Path(out), Path(checks)),
    )


def scout_role(table: str) -> Role:
    return replace(SCOUT, name=f"scout {table}")


def _role_usage(role: Role, agent: AgentRun):
    missing = "timed out" if agent.timed_out_after is not None else "no report"
    return role_usage(role.name, agent.report, missing)


def usage(run: Run) -> Ledger:
    """The run's usage by role, in the order they ran, with the gate's verdict."""
    roles = [_role_usage(scout_role(scout.table), scout.agent) for scout in run.scouts]
    if run.notes is not None:
        roles.append(_role_usage(CROSS_SOURCE, run.notes.agent))
    roles.append(_role_usage(ENGINEER, run.engineer))
    return Ledger(tuple(roles), "PASS" if passed(run.results) else "FAIL", run.sandbox)


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
    sandbox: Sandbox,
    checks: Path,
) -> Notes:
    """Run the cross-source agent and write its checked answer for the engineer."""
    folder = out / CROSS_SOURCE_DIR
    prepare_cross_source(folder, project, raw)
    command = agent_command(claude, plugin, CROSS_SOURCE)
    seen = mounts(folder, out, checks, claude)
    agent = run_agent(folder.resolve(), command, out / CROSS_SOURCE_FILE, timeout, sandbox, seen)
    try:
        notes = notes_from_report(agent.report, profiles)
    except NotesError as error:
        return Notes(agent, problem=str(error))
    artefacts.write_text(project / NOTES_FILE, render_notes(notes))
    return Notes(agent, len(notes["joins"]), len(notes["cautions"]))


def prepare_scout(folder: Path, project: Path, raw: Path, profile: dict) -> None:
    """Give one scout the goal, its table's profile and a sample of its rows."""
    folder.mkdir(parents=True)
    shutil.copyfile(project / GOAL_FILE, folder / GOAL_FILE)
    table = profile["table"]
    shutil.copyfile(project / SOURCE_PROFILES_DIR / f"{table}.txt", folder / PROFILE_FILE)
    artefacts.write_text(folder / SAMPLE_FILE, sample(raw / profile["file"], table))


def run_scouts(
    out: Path,
    project: Path,
    raw: Path,
    profiles: list[dict],
    plugin: Path,
    claude: str,
    timeout: float,
    sandbox: Sandbox,
    checks: Path,
) -> tuple[ScoutNotes, ...]:
    """Run a scout for every raw table, several at a time, and write their notes."""
    folders = out / SCOUTS_DIR
    for profile in profiles:
        prepare_scout(folders / profile["table"], project, raw, profile)
    command = agent_command(claude, plugin, SCOUT)

    def scout(profile: dict) -> AgentRun:
        table = profile["table"]
        report = folders / f"{table}.json"
        seen = mounts(folders / table, out, checks, claude)
        return run_agent((folders / table).resolve(), command, report, timeout, sandbox, seen)

    with ThreadPoolExecutor(max_workers=PARALLEL_SCOUTS) as pool:
        agents = list(pool.map(scout, profiles))
    outcomes, tables = [], []
    for profile, agent in zip(profiles, agents):
        table = profile["table"]
        try:
            notes = scout_notes_from_report(agent.report, profile)
        except ScoutError as error:
            outcomes.append(ScoutNotes(table, agent, problem=str(error)))
            tables.append((table, None))
            continue
        outcomes.append(ScoutNotes(table, agent, len(notes["findings"]), len(notes["questions"])))
        tables.append((table, notes))
    if any(notes is not None for _, notes in tables):
        artefacts.write_text(project / TABLE_NOTES_FILE, render_table_notes(tables))
    return tuple(outcomes)


def run(
    raw: Path,
    goal: Path,
    checks: Path,
    out: Path,
    claude: str = "claude",
    timeout: float = TIMEOUT_MINUTES * 60,
    cross_source: bool = False,
    scouts: bool = False,
    sandbox: Sandbox | None = None,
) -> Run:
    """Prepare the project, let the agent build it, then run the gate on it.

    Every agent runs in ``sandbox``; by default that is bubblewrap, and the
    run is refused before anything is written if it cannot be set up.

    With scouts, a scout runs for each raw table first, and the checked
    answers are put in the project as TABLE_NOTES.md. With cross_source, the
    cross-source agent runs next and its checked answer is put in the
    project as JOINS.md. The command line runs the cross-source agent unless
    told not to; here it runs only when asked for.
    """
    if not (PLUGIN_DIR / "agents" / "engineer.md").is_file():
        raise EngineerError(
            f"the agent definitions are not in {PLUGIN_DIR}; run from a clone of the repository"
        )
    load_declaration(checks)
    out = Path(out)
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise EngineerError(f"{out} must be empty")
    found = shutil.which(claude)
    if found is None:
        raise EngineerError(f"{claude} was not found; install Claude Code and sign in first")
    claude = found
    if sandbox is None:
        try:
            sandbox = default_sandbox()
        except SandboxError as error:
            raise EngineerError(str(error)) from error
    project = out / PROJECT_DIR
    profiles = prepare_project(project, raw, goal)
    plugin = stage_plugin(out.resolve() / PLUGIN_COPY_DIR)
    scout_notes: tuple[ScoutNotes, ...] = ()
    if scouts:
        scout_notes = run_scouts(
            out, project, Path(raw), profiles, plugin, claude, timeout, sandbox, checks
        )
    notes = None
    if cross_source:
        notes = run_cross_source(
            out, project, Path(raw), profiles, plugin, claude, timeout, sandbox, checks
        )
    command = agent_command(claude, plugin)
    seen = mounts(project, out, checks, claude, raw=Path(raw))
    agent = run_agent(project.resolve(), command, out / AGENT_FILE, timeout, sandbox, seen)
    # The gate builds what the agent wrote, so its builds run in the same
    # sandbox, and see nothing of the run or the repository but the project.
    results = run_gate(
        project, raw, checks, out / GATE_DIR, sandbox, hidden=(PLUGIN_DIR, out.resolve())
    )
    outcome = Run(agent, results, notes, scout_notes, sandbox.name)
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
        action=argparse.BooleanOptionalAction,
        default=True,
        help="run the cross-source agent first and give its notes to the engineer "
        "(on unless --no-cross-source is given)",
    )
    parser.add_argument(
        "--scouts",
        action="store_true",
        help="run a scout for each raw table first and give their notes to the engineer",
    )
    parser.add_argument(
        "--unisolated",
        action="store_true",
        help="run the agents without a sandbox, so that they can read and change any file "
        "you can; only for data and machines where that is acceptable",
    )
    args = parser.parse_args(argv)
    try:
        sandbox = default_sandbox(unisolated=args.unisolated)
        outcome = run(
            args.raw,
            args.goal,
            args.checks,
            args.out,
            cross_source=args.cross_source,
            scouts=args.scouts,
            sandbox=sandbox,
        )
    except (EngineerError, GateError, SandboxError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    for scout in outcome.scouts:
        print(f"scout {scout.table}: {scout.describe()}")
    if outcome.notes is not None:
        print(f"cross-source: {outcome.notes.describe()}")
    print(f"agent: {outcome.engineer.describe()}")
    print(f"project: {args.out / PROJECT_DIR}")
    print(f"sandbox: {sandbox.describe()}")
    print()
    print(render(outcome.results), end="")
    print()
    print(render_usage(usage(outcome)), end="")
    return 0 if passed(outcome.results) else 1


if __name__ == "__main__":
    sys.exit(main())
