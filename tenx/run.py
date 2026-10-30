"""Run the build workflow as a graph that is saved after every step and can be resumed.

    python -m tenx.run run --raw <folder> --goal <file> --checks <file> --out <folder>
    python -m tenx.run resume --out <folder>
    python -m tenx.run status --out <folder>

``run`` does what ``python -m tenx.engineer`` does (profile the raw files,
compare their columns, the cross-source agent, the engineer, then the gate)
as steps of a graph whose state is saved in ``<out>/checkpoints.sqlite``
after each one. If the run stops part way, because the process was killed
or a step failed, ``resume`` carries on from the step it was in: finished
steps are not run again, and an agent call that finished before the stop is
used rather than made again. ``status`` says where a run is.

Every agent call reserves its spending cap first, and the run stops before a
call that would take it past the policy's ceiling (US$2.00 unless ``--policy``
gives another). Agents and the gate's builds run in the sandbox, as with
``tenx.engineer``.

Exit status: 0 passed, 1 failed the gate, 2 refused or stopped by an error
(resume tries the step again), 3 stopped by the budget or the platform.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from tenx import policy as spending
from tenx.adapters.duckdb import DbtDuckDB, DuckDBGate, DuckDBProfiler
from tenx.backends.claude_code import ClaudeCode
from tenx.domain import CostRecord, EngineeringRequest, ResourceBudget
from tenx.engineer import (
    CROSS_SOURCE,
    ENGINEER,
    PLUGIN_COPY_DIR,
    PLUGIN_DIR,
    TIMEOUT_MINUTES,
    EngineerError,
    mounts,
    stage_plugin,
)
from tenx.gate import GateError, load_declaration
from tenx.graph.build import (
    CHECKPOINT_FILE,
    GATE_REPORT_FILE,
    USAGE_FILE,
    Toolkit,
    build_graph,
    config,
)
from tenx.graph.build import ledger as run_ledger
from tenx.graph.checkpoints import open_checkpoints
from tenx.ledger import render as render_usage
from tenx.policy import Budget, Policy, PolicyError
from tenx.ports import PlatformError
from tenx.sandbox import Sandbox, SandboxError, default_sandbox

EXIT = {"passed": 0, "failed": 1, "stopped": 3}
ROLES = {"cross-source": CROSS_SOURCE, "engineer": ENGINEER}


class RunError(Exception):
    """The run cannot start or resume, for the reason given."""


def toolkit(request: EngineeringRequest, claude: str, sandbox: Sandbox, budget: Budget) -> Toolkit:
    """The DuckDB, dbt and Claude Code ports a run works through."""
    out = Path(request.out)
    plugin = out / PLUGIN_COPY_DIR
    if not plugin.is_dir():
        stage_plugin(plugin)
    checks = Path(request.declaration)

    def seen(workspace: Path, raw: bool):
        return mounts(workspace, out, checks, claude, raw=Path(request.raw) if raw else None)

    return Toolkit(
        profiler=DuckDBProfiler(),
        engine=DbtDuckDB(),
        validator=DuckDBGate(),
        backend=ClaudeCode(claude, plugin, budget.policy),
        sandbox=sandbox,
        roles=ROLES,
        mounts=seen,
        plugin=plugin,
        hidden=(PLUGIN_DIR, out),
        budget=budget,
        timeout=TIMEOUT_MINUTES * 60,
    )


def _claude(claude: str) -> str:
    found = shutil.which(claude)
    if found is None:
        raise RunError(f"{claude} was not found; install Claude Code and sign in first")
    return found


def restored(state: dict, roles: dict) -> Budget:
    """The run's budget with every call it settled before it stopped put back."""
    budget = Budget(ResourceBudget.load(state["budget"]).to_policy())
    for value in state.get("costs") or []:
        cost = CostRecord.load(value)
        known = cost.estimated_usd if cost.cost_basis != "unknown" else None
        kind = roles[cost.node].budget_kind if cost.node in roles else cost.node
        budget.restore(cost.role, kind, cost.reserved_usd, known)
    return budget


def _invoke(request: EngineeringRequest, start: dict | None, kit: Toolkit) -> dict:
    with open_checkpoints(Path(request.out) / CHECKPOINT_FILE) as saver:
        app = build_graph(kit).compile(checkpointer=saver)
        return app.invoke(start, config(request.run_id), durability="sync")


def start(
    raw: Path,
    goal: Path,
    checks: Path,
    out: Path,
    cross_source: bool = True,
    rules: Policy | None = None,
    sandbox: Sandbox | None = None,
    claude: str = "claude",
) -> dict:
    """Check the inputs, then run the graph from its first step. Returns the final state."""
    if not (PLUGIN_DIR / "agents" / "engineer.md").is_file():
        raise RunError(f"the agent definitions are not in {PLUGIN_DIR}; run from a clone")
    load_declaration(checks)
    out = Path(out)
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise RunError(f"{out} must be empty; resume a stopped run with: resume --out {out}")
    claude = _claude(claude)
    sandbox = sandbox or default_sandbox()
    for path, what in ((raw, "folder of raw files"), (goal, "goal")):
        if not Path(path).exists():
            raise RunError(f"the {what} {path} does not exist")
    out.mkdir(parents=True, exist_ok=True)
    request = EngineeringRequest(
        run_id=out.resolve().name,
        raw=str(Path(raw).resolve()),
        goal=str(Path(goal).resolve()),
        declaration=str(Path(checks).resolve()),
        out=str(out.resolve()),
        cross_source=cross_source,
        isolation="unisolated" if sandbox.name == "none" else "bubblewrap",
    )
    budget = Budget(rules)
    begin = {
        "run_id": request.run_id,
        "request": request.dump(),
        "budget": ResourceBudget.of(budget.policy).dump(),
    }
    return _invoke(request, begin, toolkit(request, claude, sandbox, budget))


def saved(out: Path) -> tuple[dict, tuple[str, ...], list[str]]:
    """A run's saved state, the steps a resume would run, and errors of the last try."""
    path = Path(out) / CHECKPOINT_FILE
    if not path.is_file():
        raise RunError(f"no run is saved in {out}")
    run_id = Path(out).resolve().name
    with open_checkpoints(path) as saver:
        # The graph's shape does not depend on the ports, so none are needed to read it.
        snapshot = build_graph(None).compile(checkpointer=saver).get_state(config(run_id))
    if not snapshot.values:
        raise RunError(f"no run is saved in {out}")
    errors = [f"{task.name}: {task.error}" for task in snapshot.tasks if task.error]
    return snapshot.values, tuple(snapshot.next), errors


def resume(out: Path, claude: str = "claude") -> dict:
    """Carry a stopped run on from the step it was in. Returns the final state."""
    state, pending, _ = saved(out)
    if not pending:
        return state
    request = EngineeringRequest.load(state["request"])
    sandbox = default_sandbox(unisolated=request.isolation == "unisolated")
    kit = toolkit(request, _claude(claude), sandbox, restored(state, ROLES))
    return _invoke(request, None, kit)


def describe(out: Path) -> str:
    """Where a saved run is, in a few lines."""
    state, pending, errors = saved(out)
    status = state.get("status", "running")
    lines = [f"run: {state['run_id']}"]
    if pending:
        lines.append(f"status: stopped part way; next step: {', '.join(pending)}")
        lines += [f"error: {error}" for error in errors]
        lines.append(f"resume with: python -m tenx.run resume --out {out}")
    else:
        lines.append(f"status: {status}")
        if state.get("reason"):
            lines.append(f"reason: {state['reason']}")
    done = ", ".join(f"{step} {number}" for step, number in (state.get("attempts") or {}).items())
    lines.append(f"attempts: {done or 'none'}")
    charged = sum(CostRecord.load(cost).charged_usd or 0 for cost in state.get("costs") or [])
    lines.append(f"charged to the budget: US${charged} (Claude Code's estimates, not a bill)")
    return "\n".join(lines) + "\n"


def summary(state: dict) -> str:
    """The end of a run: its status, the gate's report and the usage by role."""
    request = EngineeringRequest.load(state["request"])
    out = Path(request.out)
    lines = [f"status: {state['status']}"]
    if state.get("reason"):
        lines.append(f"reason: {state['reason']}")
    attempts = state.get("attempts") or {}
    if "engineer" in attempts:
        project = out / "engineer" / f"attempt-{attempts['engineer']}" / "work"
        lines.append(f"project: {project}")
    text = "\n".join(lines) + "\n"
    if "gate" in attempts:
        gate = out / "gate" / f"attempt-{attempts['gate']}" / GATE_REPORT_FILE
        text += "\n" + gate.read_text(encoding="utf-8")
    sandbox = json.loads((out / USAGE_FILE).read_text(encoding="utf-8")).get("sandbox")
    text += "\n" + render_usage(run_ledger(state, ROLES, sandbox))
    text += f"\nsandbox: {sandbox}\n"
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="start a run in an empty folder")
    run.add_argument("--raw", type=Path, required=True, help="folder of raw source files")
    run.add_argument("--goal", type=Path, required=True, help="what to build, in plain English")
    run.add_argument("--checks", type=Path, required=True, help="the gate's JSON declaration")
    run.add_argument("--out", type=Path, required=True, help="empty folder for the run")
    run.add_argument(
        "--cross-source",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="run the cross-source agent before the engineer (on unless --no-cross-source)",
    )
    run.add_argument("--policy", type=Path, help="JSON spending policy (ceiling and caps)")
    run.add_argument(
        "--unisolated",
        action="store_true",
        help="run the agents without a sandbox, so that they can read and change any file "
        "you can; only for data and machines where that is acceptable",
    )
    for name, text in (("resume", "carry a stopped run on"), ("status", "say where a run is")):
        command = commands.add_parser(name, help=text)
        command.add_argument("--out", type=Path, required=True, help="the run's folder")
    args = parser.parse_args(argv)
    try:
        if args.command == "status":
            print(describe(args.out), end="")
            return 0
        if args.command == "run":
            rules = spending.load(args.policy) if args.policy else None
            sandbox = default_sandbox(unisolated=args.unisolated)
            state = start(
                args.raw, args.goal, args.checks, args.out, args.cross_source, rules, sandbox
            )
        else:
            state = resume(args.out)
    except (RunError, PolicyError, SandboxError, GateError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except (PlatformError, EngineerError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        print(f"the run is saved; resume with: python -m tenx.run resume --out {args.out}")
        return 2
    print(summary(state), end="")
    return EXIT[state["status"]]


if __name__ == "__main__":
    sys.exit(main())
