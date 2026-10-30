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

import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from langgraph.graph import END, START, StateGraph

from tenx.domain import ArtefactRef, EngineeringRequest, ExecutionPlan, ResourceBudget
from tenx.graph.state import FAILED, PASSED, RUNNING, STOPPED, GraphState, plain
from tenx.policy import Budget
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
