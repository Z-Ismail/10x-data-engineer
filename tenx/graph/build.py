"""The build workflow as a graph: profile, overlap, cross-source, architect, engineer, gate, report.

With a spec review, the run waits after the architect for a person to
approve, edit or reject the spec (``review``) before the engineer starts.

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
from langgraph.types import interrupt

from tenx import artefacts
from tenx.contract import ContractError, check_contract, contract_from_report
from tenx.cross_source import NOTES_FILE, NotesError, notes_from_report
from tenx.cross_source import render as render_notes
from tenx.domain import (
    AgentFinding,
    ApprovalDecision,
    ArtefactRef,
    CostRecord,
    EngineeringRequest,
    Evidence,
    ExecutionPlan,
    ResourceBudget,
    ValidationResult,
)
from tenx.graph.state import FAILED, PASSED, RUNNING, STOPPED, GraphState, plain
from tenx.ledger import Ledger, role_usage
from tenx.policy import Budget, BudgetStop
from tenx.ports import (
    COUNT_KEPT,
    OVERLAP,
    PROFILE,
    RUN_AGENT,
    SCAFFOLD,
    STRUCTURED_ANSWER,
    VALIDATE,
    AgentBackend,
    KeptRows,
    Mounts,
    PlatformError,
    Role,
    Sandbox,
    SourceProfiler,
    TransformEngine,
    UnsupportedCapability,
    Validator,
    require,
)
from tenx.repair import REPORT_FILE as REPAIR_FILE
from tenx.repair import decide
from tenx.repair import render as render_repair
from tenx.spec import render as render_spec

CHECKPOINT_FILE = "checkpoints.sqlite"
AGENT_FILE = "agent.json"
GOAL_FILE = "GOAL.md"
SOURCE_PROFILES_DIR = "source_profiles"
GATE_REPORT_FILE = "report.txt"
USAGE_FILE = "usage.json"
BUDGET_FILE = "budget.json"
PROFILE_DIR = "profile"
OVERLAP_DIR = "overlap"
SPEC_FILE = "SPEC.md"
CONTRACT_FILE = "contract.json"
COUNTS_FILE = "counts.json"
COUNT_DIR = "count"

PLAN = "plan"
PROFILE_STEP, OVERLAP_STEP, CROSS_SOURCE_STEP = "profile", "overlap", "cross-source"
ARCHITECT_STEP, REVIEW_STEP, ENGINEER_STEP = "architect", "review", "engineer"
GATE_STEP, REPORT_STEP = "gate", "report"
WORK_STEPS = (
    PROFILE_STEP,
    OVERLAP_STEP,
    CROSS_SOURCE_STEP,
    ARCHITECT_STEP,
    REVIEW_STEP,
    ENGINEER_STEP,
    GATE_STEP,
)


@dataclass(frozen=True)
class Toolkit:
    """The ports a run's steps work through, chosen by the code that starts it."""

    profiler: SourceProfiler
    engine: TransformEngine
    validator: Validator
    backend: AgentBackend
    sandbox: Sandbox
    # The agents by step: "cross-source", "architect" and "engineer".
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
    middle = (CROSS_SOURCE_STEP,) if request.cross_source else ()
    middle += (ARCHITECT_STEP,) if request.architect else ()
    middle += (REVIEW_STEP,) if request.review_spec else ()
    overlap = (OVERLAP_STEP,) if middle else ()
    return (PROFILE_STEP, *overlap, *middle, ENGINEER_STEP, GATE_STEP, REPORT_STEP)


def needs(toolkit: Toolkit, steps: tuple[str, ...]) -> list[tuple[object, str]]:
    """Each port the steps use, with the capability each step asks of it."""
    wanted = {
        PROFILE_STEP: [(toolkit.profiler, PROFILE)],
        OVERLAP_STEP: [(toolkit.profiler, OVERLAP)],
        CROSS_SOURCE_STEP: [(toolkit.backend, RUN_AGENT), (toolkit.backend, STRUCTURED_ANSWER)],
        ARCHITECT_STEP: [
            (toolkit.backend, RUN_AGENT),
            (toolkit.backend, STRUCTURED_ANSWER),
            (toolkit.profiler, COUNT_KEPT),
        ],
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


def _counts_json(counts: Mapping[str, KeptRows]) -> str:
    """What each model keeps, as the spec states it, so the run can be scored later."""
    document = {
        name: {"source_rows": rows.source_rows, "excluded": dict(rows.excluded), "kept": rows.kept}
        for name, rows in counts.items()
    }
    return json.dumps(document, indent=2) + "\n"


def architect_node(toolkit: Toolkit):
    """The architect agent; its contract, checked and counted, becomes SPEC.md for the engineer.

    The contract's rules for leaving rows out are counted by the profiler
    apart from the run, in the sandbox, since an agent wrote them. A contract
    that fails its check or its count leaves the engineer without a spec,
    and the run says why.
    """

    def architect(state: GraphState) -> dict:
        request = _request(state)
        found = state["artefacts"]
        profiles = Path(found["profiles"]["path"])
        overlap = Path(found["overlap"]["path"])
        files = {"goal": Path(request.goal), "profiles": profiles, "overlap": overlap}
        if "joins" in found:
            files["joins"] = Path(found["joins"]["path"])

        def prepare(work: Path) -> None:
            shutil.copyfile(request.goal, work / GOAL_FILE)
            shutil.copytree(profiles, work / SOURCE_PROFILES_DIR)
            shutil.copytree(overlap, work / SOURCE_PROFILES_DIR, dirs_exist_ok=True)
            if "joins" in files:
                shutil.copyfile(files["joins"], work / NOTES_FILE)

        call = call_agent(toolkit, state, ARCHITECT_STEP, files, prepare)
        if call.stopped:
            return call.update
        try:
            contract = contract_from_report(call.report, _profiles(state))
            counts = toolkit.profiler.count_kept(
                contract,
                Path(request.raw).resolve(),
                (call.folder / COUNT_DIR).resolve(),
                toolkit.sandbox,
                toolkit.hidden,
            )
        except (ContractError, PlatformError) as problem:
            claim = f"no spec for the engineer: {problem}"
            finding = AgentFinding(
                agent=ARCHITECT_STEP, claim=claim, kind="fact", confidence="high"
            )
            return plain({**call.update, "findings": [finding.dump()]})
        artefacts.write_text(call.folder / CONTRACT_FILE, json.dumps(contract, indent=2) + "\n")
        artefacts.write_text(call.folder / COUNTS_FILE, _counts_json(counts))
        spec = call.folder / SPEC_FILE
        artefacts.write_text(spec, render_spec(contract, counts))
        number = call.update["attempts"][ARCHITECT_STEP]
        ref = ArtefactRef.of("spec", spec, attempt=number).dump()
        return plain({**call.update, "artefacts": {"spec": ref}})

    return architect


APPROVED = "approved spec"


def waiting_on(state: GraphState) -> dict:
    """What a person is asked to decide: the spec, its hash, and why it may be missing."""
    spec = (state.get("artefacts") or {}).get("spec")
    asked = {"subject": "spec", "spec": None, "sha256": None}
    if spec is not None:
        asked.update(spec=spec["path"], sha256=spec["sha256"])
    else:
        notes = [
            item["claim"] for item in state.get("findings") or [] if item["agent"] == ARCHITECT_STEP
        ]
        asked["missing"] = notes[-1] if notes else "the architect gave no spec"
    earlier = state.get("approvals") or []
    if earlier and earlier[-1]["decision"] == "refused":
        asked["refused"] = earlier[-1]["reason"]
    return asked


def _decision(decision: str, sha256: str | None, reason: str = "") -> dict:
    made = ApprovalDecision(subject="spec", decision=decision, sha256=sha256, reason=reason)
    return {"approvals": [made.dump()]}


def _edited(toolkit: Toolkit, state: GraphState, asked: dict, contract) -> dict:
    """Use a person's own contract, checked and counted as the architect's is.

    Its rules are SQL that will run, wherever the person got them from, so
    they are counted by the profiler apart from the run, in the sandbox.
    """
    request = _request(state)
    out = Path(request.out)
    values = {"contract": json.dumps(contract, sort_keys=True), "sandbox": toolkit.sandbox.name}
    profiles = Path(state["artefacts"]["profiles"]["path"])
    hashed = artefacts.input_files({"profiles": profiles})
    attempt = artefacts.start(out, state["run_id"], REVIEW_STEP, hashed, values)
    try:
        checked = check_contract(contract, _profiles(state))
        counts = toolkit.profiler.count_kept(
            checked,
            Path(request.raw).resolve(),
            (attempt.folder / COUNT_DIR).resolve(),
            toolkit.sandbox,
            toolkit.hidden,
        )
    except (ContractError, PlatformError) as problem:
        artefacts.finish(attempt, artefacts.FAILED)
        reason = f"the edited contract cannot be used: {problem}"
        return _decision("refused", asked["sha256"], reason)
    artefacts.write_text(attempt.folder / CONTRACT_FILE, json.dumps(checked, indent=2) + "\n")
    artefacts.write_text(attempt.folder / COUNTS_FILE, _counts_json(counts))
    spec = attempt.folder / SPEC_FILE
    artefacts.write_text(spec, render_spec(checked, counts))
    artefacts.finish(attempt, artefacts.COMPLETE, [CONTRACT_FILE, COUNTS_FILE, SPEC_FILE])
    approved = ArtefactRef.of(APPROVED, spec, attempt=attempt.number)
    reason = f"edited from {asked['sha256'] or 'no spec'}"
    update = _decision("edited", approved.sha256, reason)
    return {**update, "artefacts": {APPROVED: approved.dump()}}


def _approve(toolkit: Toolkit, state: GraphState, asked: dict, answer: dict) -> dict:
    """Use the spec as it was shown, if it is still exactly that, or the person's edit of it."""
    if answer.get("sha256") != asked["sha256"]:
        reason = (
            f"the approval is for spec {answer.get('sha256')}, but the run is waiting on "
            f"{asked['sha256']}"
        )
        return _decision("refused", asked["sha256"], reason)
    if answer.get("contract") is not None:
        return _edited(toolkit, state, asked, answer["contract"])
    if asked["spec"] is None:
        return _decision("approved", None, "no spec; the engineer works from the goal alone")
    spec = ArtefactRef.load(state["artefacts"]["spec"])
    if not spec.unchanged():
        reason = (
            f"{spec.path} was changed after it was written; to use a changed spec, "
            "approve an edited contract"
        )
        return _decision("refused", spec.sha256, reason)
    approved = ArtefactRef(
        name=APPROVED, path=spec.path, kind=spec.kind, sha256=spec.sha256, attempt=spec.attempt
    )
    return {**_decision("approved", spec.sha256), "artefacts": {APPROVED: approved.dump()}}


def review_node(toolkit: Toolkit):
    """Wait for a person to approve, edit or reject the architect's spec.

    The node is run again from its start when the run resumes with an answer,
    so it only reads before it waits. An answer it cannot accept is recorded
    as refused and the run waits again; nothing reaches the engineer that the
    person did not see.
    """

    def review(state: GraphState) -> dict:
        asked = waiting_on(state)
        answer = interrupt(asked)
        choice = answer.get("decision") if isinstance(answer, dict) else None
        if choice == "reject":
            reason = str(answer.get("reason") or "").strip()
            update = _decision("rejected", asked["sha256"], reason)
            return _stop("spec rejected" + (f": {reason}" if reason else ""), **update)
        if choice == "approve":
            return plain(_approve(toolkit, state, asked, answer))
        return plain(
            _decision("refused", asked["sha256"], f"not an answer the run takes: {answer!r}")
        )

    return review


def _after_review(state: GraphState) -> str:
    approvals = state.get("approvals") or []
    if state.get("status") == RUNNING and approvals and approvals[-1]["decision"] == "refused":
        return REVIEW_STEP
    return _after(REVIEW_STEP)(state)


# What a repair does not carry over from the attempt it repairs: what the
# build left, and the inputs, which every attempt is given afresh.
NOT_CARRIED = ("target", "logs", "dbt_packages", "*.duckdb", "*.duckdb.wal", "*.duckdb.tmp")


def _carry_over(previous: Path, work: Path) -> None:
    # A link the agent made is copied as a link, never followed: this copy
    # runs outside the sandbox and could otherwise read what the agent cannot.
    skip = shutil.ignore_patterns(
        *NOT_CARRIED, GOAL_FILE, SOURCE_PROFILES_DIR, NOTES_FILE, SPEC_FILE, REPAIR_FILE
    )
    shutil.copytree(previous, work, symlinks=True, ignore=skip, dirs_exist_ok=True)


def engineer_node(toolkit: Toolkit):
    """The engineer agent, in a project made afresh for each attempt.

    After a failed validation the same step repairs: the new attempt starts
    from the project just checked, with ``GATE_REPORT.md`` saying what the
    validator let it be told of each failure.
    """

    def engineer(state: GraphState) -> dict:
        request = _request(state)
        found = state["artefacts"]
        profiles = Path(found["profiles"]["path"])
        files = {"goal": Path(request.goal), "profiles": profiles}
        if "joins" in found:
            files["joins"] = Path(found["joins"]["path"])
        spec = found.get(APPROVED if request.review_spec else "spec")
        if spec is not None:
            files["spec"] = Path(spec["path"])
        checked = [ValidationResult.load(item) for item in state.get("validations") or []]
        repairing = bool(checked) and not checked[-1].passed
        if repairing:
            files["previous project"] = Path(found["project"]["path"])
            files["gate report"] = Path(found["gate report"]["path"])

        def prepare(work: Path) -> None:
            if repairing:
                _carry_over(files["previous project"], work)
                artefacts.write_text(work / REPAIR_FILE, render_repair(checked[-1]))
            else:
                toolkit.engine.scaffold(work, Path(request.raw))
            shutil.copytree(profiles, work / SOURCE_PROFILES_DIR)
            shutil.copyfile(request.goal, work / GOAL_FILE)
            if "joins" in files:
                shutil.copyfile(files["joins"], work / NOTES_FILE)
            if "spec" in files:
                shutil.copyfile(files["spec"], work / SPEC_FILE)

        call = call_agent(toolkit, state, ENGINEER_STEP, files, prepare, raw=True)
        if call.stopped:
            if repairing:
                # The run has a verdict; not affording a repair leaves it failed.
                reason = f"no repair made: {call.update['reason']}"
                return {**call.update, "status": FAILED, "reason": reason}
            return call.update
        number = call.update["attempts"][ENGINEER_STEP]
        project = ArtefactRef.of("project", call.folder / artefacts.WORK_DIR, attempt=number)
        update = {**call.update, "artefacts": {"project": project.dump()}, "rounds": [number]}
        if "spec" in files:
            given = ArtefactRef.load(spec)
            held = Path(project.path) / SPEC_FILE
            if not (held.is_file() and artefacts.file_hash(held) == given.sha256):
                claim = f"the engineer changed or removed SPEC.md in attempt {number}"
                finding = AgentFinding(
                    agent=ENGINEER_STEP, claim=claim, kind="fact", confidence="high"
                )
                update["findings"] = [finding.dump()]
        return plain(update)

    return engineer


def gate_node(toolkit: Toolkit):
    """The validator builds the engineer's project afresh and checks it, in the sandbox."""

    def gate(state: GraphState) -> dict:
        request = _request(state)
        project = Path(state["artefacts"]["project"]["path"])
        declaration = Path(request.declaration)
        attempt = artefacts.start(
            Path(request.out),
            state["run_id"],
            GATE_STEP,
            artefacts.input_files({"project": project, "declaration": declaration}),
            {"validator": toolkit.validator.name, "sandbox": toolkit.sandbox.name},
        )
        report = toolkit.validator.validate(
            project.resolve(),
            Path(request.raw).resolve(),
            declaration,
            attempt.work.resolve(),
            toolkit.sandbox,
            toolkit.hidden,
        )
        artefacts.write_text(attempt.folder / GATE_REPORT_FILE, report.text)
        artefacts.finish(attempt, artefacts.COMPLETE, [GATE_REPORT_FILE])
        saved = ArtefactRef.of("gate report", attempt.folder / GATE_REPORT_FILE, attempt.number)
        verdict = ValidationResult.of(toolkit.validator.name, report, saved)
        return plain(
            {
                "artefacts": {"gate report": saved.dump()},
                "validations": [verdict.dump()],
                "attempts": {GATE_STEP: attempt.number},
            }
        )

    return gate


def ledger(state: GraphState, roles: Mapping[str, Role], sandbox: str) -> Ledger:
    """Each agent's usage in the attempt the run used, in the order they ran."""
    out = Path(_request(state).out)
    outcomes = {
        (cost["node"], cost["attempt"]): cost["outcome"] for cost in state.get("costs") or []
    }
    used = []
    for step in (CROSS_SOURCE_STEP, ARCHITECT_STEP, ENGINEER_STEP):
        last = (state.get("attempts") or {}).get(step)
        if last is None:
            continue
        # Each round of a repaired run is a row of its own, so the run's
        # figures add up every round; a run of one round has one row.
        rounds = state.get("rounds") if step == ENGINEER_STEP else None
        for index, number in enumerate(rounds if rounds and len(rounds) > 1 else [last]):
            folder = out / step / f"{artefacts.ATTEMPT_PREFIX}{number}"
            missing = outcomes.get((step, number), "no report")
            name = roles[step].name + (f" repair {index}" if index else "")
            used.append(role_usage(name, _report_in(folder), missing))
    validations = state.get("validations") or []
    if validations:
        verdict = "PASS" if validations[-1]["passed"] else "FAIL"
    else:
        verdict = "not run"
    return Ledger(tuple(used), verdict, sandbox)


def report_node(toolkit: Toolkit):
    """Write the run's usage and spending, and say whether it passed."""

    def report(state: GraphState) -> dict:
        out = Path(_request(state).out)
        ledger(state, toolkit.roles, toolkit.sandbox.name).write(out / USAGE_FILE)
        spending = json.dumps(toolkit.budget.to_dict(), indent=2) + "\n"
        artefacts.write_text(out / BUDGET_FILE, spending)
        if state.get("status") == STOPPED:
            return {}
        validations = state.get("validations") or []
        passed = bool(validations) and validations[-1]["passed"]
        request = _request(state)
        if passed or not request.repairs or not validations or state.get("reason"):
            return {"status": PASSED if passed else FAILED}
        checked = [ValidationResult.load(item) for item in validations]
        return {"status": FAILED, "reason": decide(checked, request.repairs).reason}

    return report


def _after(step: str):
    def route(state: GraphState) -> str:
        if state.get("status") != RUNNING:
            return REPORT_STEP
        steps = ExecutionPlan.load(state["plan"]).steps
        later = steps[steps.index(step) + 1 :] if step in steps else steps
        return later[0] if later else REPORT_STEP

    return route


def _after_gate(state: GraphState) -> str:
    """Repair a failed project while repairs remain and each one makes headway."""
    if state.get("status") != RUNNING:
        return REPORT_STEP
    checked = [ValidationResult.load(item) for item in state.get("validations") or []]
    if checked and decide(checked, _request(state).repairs).repair:
        return ENGINEER_STEP
    return REPORT_STEP


def build_graph(toolkit: Toolkit, nodes: Mapping[str, Callable] | None = None) -> StateGraph:
    """The build workflow's graph, not yet compiled. ``nodes`` replaces steps by name."""
    made = {
        PLAN: plan_node(toolkit),
        PROFILE_STEP: profile_node(toolkit),
        OVERLAP_STEP: overlap_node(toolkit),
        CROSS_SOURCE_STEP: cross_source_node(toolkit),
        ARCHITECT_STEP: architect_node(toolkit),
        REVIEW_STEP: review_node(toolkit),
        ENGINEER_STEP: engineer_node(toolkit),
        GATE_STEP: gate_node(toolkit),
        REPORT_STEP: report_node(toolkit),
        **(nodes or {}),
    }
    graph = StateGraph(GraphState)
    for name, node in made.items():
        graph.add_node(name, node)
    graph.add_edge(START, PLAN)
    targets = [step for step in (*WORK_STEPS, REPORT_STEP) if step in made]
    for name in made:
        if name == GATE_STEP:
            graph.add_conditional_edges(name, _after_gate, targets)
        elif name == REVIEW_STEP:
            graph.add_conditional_edges(name, _after_review, targets)
        elif name != REPORT_STEP:
            graph.add_conditional_edges(name, _after(name), targets)
    graph.add_edge(REPORT_STEP, END)
    return graph
