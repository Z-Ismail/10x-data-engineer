"""The build workflow as a graph: profile, overlap, cross-source, engineer, gate, report.

Each step is a node. The graph is saved after every step (``durability="sync"``),
so a run that stops part way, by a crash or a closed laptop, resumes at the
step it was in and never runs a finished step again.

The nodes do no platform work themselves. They are given a ``Toolkit`` of
ports (a profiler, a transform engine, a validator, an agent backend and a
sandbox) by the code that starts the run, and they reach sources, builds,
checks and agents only through it. The toolkit stays outside the state,
which holds only JSON values.

Two things end a run early, as answers rather than faults: a platform that
cannot do what a step needs, found before anything runs, and a call whose
spending reservation does not fit the run's ceiling. The run is then
``stopped``, with the reason, and goes straight to the report. Anything else
that goes wrong is raised, and the run can be resumed at that step.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from langgraph.graph import END, START, StateGraph

from tenx import artefacts
from tenx.cross_source import NOTES_FILE, NotesError, notes_from_report
from tenx.cross_source import render as render_notes
from tenx.domain import (
    AgentFinding,
    ArtefactRef,
    CostRecord,
    EngineeringRequest,
    Evidence,
    ExecutionPlan,
    ResourceBudget,
)
from tenx.graph.state import FAILED, PASSED, RUNNING, STOPPED, GraphState, plain
from tenx.ledger import role_usage
from tenx.policy import Budget, BudgetStop
from tenx.ports import (
    OVERLAP,
    PROFILE,
    RUN_AGENT,
    SCAFFOLD,
    STRUCTURED_ANSWER,
    VALIDATE,
    AgentBackend,
    Mounts,
    Role,
    Sandbox,
    SourceProfiler,
    TransformEngine,
    UnsupportedCapability,
    Validator,
    require,
)

CHECKPOINT_FILE = "checkpoints.sqlite"
AGENT_FILE = "agent.json"
GOAL_FILE = "GOAL.md"
SOURCE_PROFILES_DIR = "source_profiles"
PROFILE_DIR = "profile"
OVERLAP_DIR = "overlap"

PLAN = "plan"
PROFILE_STEP, OVERLAP_STEP, CROSS_SOURCE_STEP = "profile", "overlap", "cross-source"
ENGINEER_STEP, GATE_STEP, REPORT_STEP = "engineer", "gate", "report"
WORK_STEPS = (PROFILE_STEP, OVERLAP_STEP, CROSS_SOURCE_STEP, ENGINEER_STEP, GATE_STEP)


@dataclass(frozen=True)
class Toolkit:
    """The ports a run's steps work through, chosen by the code that starts it."""

    profiler: SourceProfiler
    engine: TransformEngine
    validator: Validator
    backend: AgentBackend
    sandbox: Sandbox
    # The agents by step: "cross-source" and "engineer".
    roles: Mapping[str, Role]
    # What an agent working in a folder may see; True also shows it the raw files.
    mounts: Callable[[Path, bool], Mounts]
    # The add-on as loaded for the run, holding each agent's prompt file.
    plugin: Path
    # What the validator's builds must not see besides the project.
    hidden: tuple[Path, ...]
    budget: Budget
    timeout: float


def config(run_id: str) -> dict:
    """The graph configuration that names one run's saved state."""
    return {"configurable": {"thread_id": run_id}}


def _request(state: GraphState) -> EngineeringRequest:
    return EngineeringRequest.load(state["request"])


def _stop(reason: str, **update) -> dict:
    return plain({**update, "status": STOPPED, "reason": reason})


def steps_for(request: EngineeringRequest) -> tuple[str, ...]:
    """The steps the build workflow takes for a request, in order."""
    middle = (OVERLAP_STEP, CROSS_SOURCE_STEP) if request.cross_source else ()
    return (PROFILE_STEP, *middle, ENGINEER_STEP, GATE_STEP, REPORT_STEP)


def needs(toolkit: Toolkit, steps: tuple[str, ...]) -> list[tuple[object, str]]:
    """Each port the steps use, with the capability each step asks of it."""
    wanted = {
        PROFILE_STEP: [(toolkit.profiler, PROFILE)],
        OVERLAP_STEP: [(toolkit.profiler, OVERLAP)],
        CROSS_SOURCE_STEP: [(toolkit.backend, RUN_AGENT), (toolkit.backend, STRUCTURED_ANSWER)],
        ENGINEER_STEP: [(toolkit.engine, SCAFFOLD), (toolkit.backend, RUN_AGENT)],
        GATE_STEP: [(toolkit.validator, VALIDATE)],
    }
    return [need for step in steps for need in wanted.get(step, [])]


def plan_node(toolkit: Toolkit):
    """Choose the steps for the request, and check every port can do its part first."""

    def plan(state: GraphState) -> dict:
        request = _request(state)
        budget = ResourceBudget.of(toolkit.budget.policy).dump()
        if request.scouts:
            return _stop("scouts are not part of the graph run; run without them", budget=budget)
        steps = steps_for(request)
        chosen = ExecutionPlan(workflow="build", steps=steps).dump()
        try:
            for port, capability in needs(toolkit, steps):
                require(port, capability)
        except UnsupportedCapability as missing:
            return _stop(str(missing), plan=chosen, budget=budget)
        return plain({"plan": chosen, "budget": budget, "status": RUNNING})

    return plan


def _fresh(folder: Path) -> Path:
    # A deterministic step that crashed part way runs again from nothing.
    if folder.exists():
        shutil.rmtree(folder)
    return folder


def profile_node(toolkit: Toolkit):
    def profile(state: GraphState) -> dict:
        request = _request(state)
        folder = _fresh(Path(request.out) / PROFILE_DIR)
        toolkit.profiler.profile(Path(request.raw), folder)
        return plain({"artefacts": {"profiles": ArtefactRef.of("profiles", folder).dump()}})

    return profile


def overlap_node(toolkit: Toolkit):
    def overlap(state: GraphState) -> dict:
        request = _request(state)
        folder = _fresh(Path(request.out) / OVERLAP_DIR)
        toolkit.profiler.overlap(Path(request.raw), folder)
        return plain({"artefacts": {"overlap": ArtefactRef.of("overlap", folder).dump()}})

    return overlap


def _report_in(folder: Path) -> dict | None:
    try:
        report = json.loads((folder / AGENT_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return report if isinstance(report, dict) else None


def _record(toolkit, state, step, number, usage, settlement) -> dict:
    return CostRecord.of(
        state["run_id"], step, number, toolkit.backend.name, usage, settlement
    ).dump()


@dataclass(frozen=True)
class Call:
    """What one agent step ended with: the attempt it used, its report, the update."""

    folder: Path | None
    report: dict | None
    update: dict

    @property
    def stopped(self) -> bool:
        return self.update.get("status") == STOPPED


def agent_inputs(toolkit: Toolkit, step: str, request: EngineeringRequest, files: dict):
    """The files and values that decide what an agent step does, and so its action ID."""
    role = toolkit.roles[step]
    prompt = toolkit.plugin / "agents" / f"{role.agent.split(':')[-1]}.md"
    values = {
        "role": role.name,
        "agent": role.agent,
        "prompt": role.prompt,
        "allowed_tools": json.dumps(list(role.allowed_tools)),
        "max_turns": str(role.max_turns),
        "schema": json.dumps(role.json_schema, sort_keys=True),
        "cap_usd": str(toolkit.budget.policy.cap(role.budget_kind)),
        "backend": toolkit.backend.name,
        "sandbox": toolkit.sandbox.name,
        "raw": str(Path(request.raw).resolve()),
    }
    return artefacts.input_files({**files, "prompt": prompt}), values


def call_agent(
    toolkit: Toolkit,
    state: GraphState,
    step: str,
    files: dict,
    prepare: Callable[[Path], None],
    raw: bool = False,
) -> Call:
    """Run one agent step: charge attempts a crash left, reuse a finished one, or call.

    An attempt on disk with no cost in the state was made by an execution of
    this step that never finished. It is charged now: its measured cost if
    its report is there, its whole reservation if not. If it also completed
    from exactly these inputs, it is used as it is and no call is made.
    Otherwise the call's reservation is made first; if it does not fit, the
    run stops before the call.
    """
    request = _request(state)
    role = toolkit.roles[step]
    out = Path(request.out)
    hashed, values = agent_inputs(toolkit, step, request, files)
    recorded = {cost["attempt"] for cost in state.get("costs") or [] if cost["node"] == step}
    found = artefacts.reusable(out, state["run_id"], step, hashed, values)
    cap = toolkit.budget.policy.cap(role.budget_kind)
    costs = []
    for number in artefacts.attempt_numbers(out, step):
        if number in recorded:
            continue
        folder = out / step / f"{artefacts.ATTEMPT_PREFIX}{number}"
        usage = role_usage(role.name, _report_in(folder), "interrupted")
        known = Decimal(str(usage.cost_usd)) if usage.cost_known else None
        settlement = toolkit.budget.restore(role.name, role.budget_kind, cap, known)
        costs.append(_record(toolkit, state, step, number, usage, settlement))
    if found is not None:
        update = {"costs": costs, "attempts": {step: found.number}}
        return Call(found.folder, _report_in(found.folder), plain(update))
    try:
        reservation = toolkit.budget.reserve(role.name, role.budget_kind)
    except BudgetStop as stop:
        return Call(None, None, _stop(str(stop), costs=costs))
    attempt = artefacts.start(out, state["run_id"], step, hashed, values)
    prepare(attempt.work)
    agent = toolkit.backend.run(
        role,
        attempt.work.resolve(),
        attempt.folder / AGENT_FILE,
        toolkit.timeout,
        toolkit.sandbox,
        toolkit.mounts(attempt.work.resolve(), raw),
    )
    missing = "timed out" if agent.timed_out_after is not None else "no report"
    usage = role_usage(role.name, agent.report, missing)
    settlement = toolkit.budget.settle(reservation, usage.cost_usd, usage.cost_known)
    costs.append(_record(toolkit, state, step, attempt.number, usage, settlement))
    if agent.report is not None:
        artefacts.finish(attempt, artefacts.COMPLETE, [AGENT_FILE])
    else:
        artefacts.finish(attempt, artefacts.FAILED)
    update = {"costs": costs, "attempts": {step: attempt.number}}
    return Call(attempt.folder, agent.report, plain(update))


def _profiles(state: GraphState) -> list[dict]:
    folder = Path(state["artefacts"]["profiles"]["path"])
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(folder.glob("*.json"))]


def _findings(notes: dict, joins: str) -> list[dict]:
    found = [
        AgentFinding(
            agent=CROSS_SOURCE_STEP,
            claim=f"{join['left']} joins {join['right']} ({join['cardinality']}): "
            f"{join['prepare']}",
            kind="recommendation",
            confidence="medium",
            evidence=(Evidence(kind="file", ref=joins, detail=join["evidence"]),),
            assets=(join["left"], join["right"]),
            risks=(join["unmatched"],),
        )
        for join in notes["joins"]
    ]
    found += [
        AgentFinding(agent=CROSS_SOURCE_STEP, claim=caution, kind="hypothesis", confidence="low")
        for caution in notes["cautions"]
    ]
    return [finding.dump() for finding in found]


def cross_source_node(toolkit: Toolkit):
    """The cross-source agent; its checked answer becomes JOINS.md for the engineer."""

    def cross_source(state: GraphState) -> dict:
        request = _request(state)
        profiles = Path(state["artefacts"]["profiles"]["path"])
        overlap = Path(state["artefacts"]["overlap"]["path"])
        files = {"goal": Path(request.goal), "profiles": profiles, "overlap": overlap}

        def prepare(work: Path) -> None:
            shutil.copyfile(request.goal, work / GOAL_FILE)
            shutil.copytree(profiles, work / SOURCE_PROFILES_DIR)
            shutil.copytree(overlap, work / SOURCE_PROFILES_DIR, dirs_exist_ok=True)

        call = call_agent(toolkit, state, CROSS_SOURCE_STEP, files, prepare)
        if call.stopped:
            return call.update
        try:
            notes = notes_from_report(call.report, _profiles(state))
        except NotesError as problem:
            claim = f"no notes for the engineer: {problem}"
            finding = AgentFinding(
                agent=CROSS_SOURCE_STEP, claim=claim, kind="fact", confidence="high"
            )
            return plain({**call.update, "findings": [finding.dump()]})
        joins = call.folder / NOTES_FILE
        artefacts.write_text(joins, render_notes(notes))
        number = call.update["attempts"][CROSS_SOURCE_STEP]
        ref = ArtefactRef.of("joins", joins, attempt=number).dump()
        return plain(
            {**call.update, "artefacts": {"joins": ref}, "findings": _findings(notes, str(joins))}
        )

    return cross_source


def report_node(toolkit: Toolkit):
    def report(state: GraphState) -> dict:
        if state.get("status") == STOPPED:
            return {}
        validations = state.get("validations") or []
        passed = bool(validations) and validations[-1]["passed"]
        return {"status": PASSED if passed else FAILED}

    return report


def _after(step: str):
    def route(state: GraphState) -> str:
        if state.get("status") != RUNNING:
            return REPORT_STEP
        steps = ExecutionPlan.load(state["plan"]).steps
        later = steps[steps.index(step) + 1 :] if step in steps else steps
        return later[0] if later else REPORT_STEP

    return route


def build_graph(toolkit: Toolkit, nodes: Mapping[str, Callable] | None = None) -> StateGraph:
    """The build workflow's graph, not yet compiled. ``nodes`` replaces steps by name."""
    made = {
        PLAN: plan_node(toolkit),
        PROFILE_STEP: profile_node(toolkit),
        OVERLAP_STEP: overlap_node(toolkit),
        CROSS_SOURCE_STEP: cross_source_node(toolkit),
        REPORT_STEP: report_node(toolkit),
        **(nodes or {}),
    }
    graph = StateGraph(GraphState)
    for name, node in made.items():
        graph.add_node(name, node)
    graph.add_edge(START, PLAN)
    targets = [step for step in (*WORK_STEPS, REPORT_STEP) if step in made]
    for name in made:
        if name != REPORT_STEP:
            graph.add_conditional_edges(name, _after(name), targets)
    graph.add_edge(REPORT_STEP, END)
    return graph
