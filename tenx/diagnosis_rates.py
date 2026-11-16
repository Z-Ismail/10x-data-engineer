"""How often the Diagnostics agent names the root cause of a failing project.

A test bed is a folder of dbt projects that each fail the validator for one
known reason, with an ``answers.json`` beside them giving, for each project,
the kind of cause (one of ``tenx.diagnostics.CAUSES``), the model it is in,
and whether the mistake was seen in a live run (``observed``) or planted.
Each project is diagnosed on each dataset by the same run a user starts
with ``python -m tenx.run diagnose``; each diagnosis is scored here against
the answers the agent never sees.

The first cause a diagnosis gives is the diagnosis. It is right only when
it names both the bed's kind of cause and the bed's model.
"""

from __future__ import annotations

import json
import random
import shutil
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from tenx import artefacts
from tenx.compare import CostNotApproved, Tally, interval
from tenx.diagnostics import ANSWER_FILE, CAUSES
from tenx.domain import AgentFinding, CostRecord, ValidationResult
from tenx.engineer import DIAGNOSTICS
from tenx.policy import Policy

DIAGNOSTICS_STEP = "diagnostics"

__all__ = [
    "CAUSE_ONLY",
    "MODEL_ONLY",
    "NO_CAUSE",
    "NO_DIAGNOSIS",
    "OUTCOMES",
    "RIGHT",
    "WRONG",
    "Diagnosis",
    "RatesError",
    "Summary",
    "diagnose_project",
    "listed",
    "measure",
    "render",
    "report",
    "score",
    "summarise",
    "verdict",
]

RIGHT, MODEL_ONLY, CAUSE_ONLY, WRONG = "right", "model only", "cause only", "wrong"
NO_CAUSE, NO_DIAGNOSIS = "no cause", "no diagnosis"
OUTCOMES = (RIGHT, MODEL_ONLY, CAUSE_ONLY, WRONG, NO_CAUSE, NO_DIAGNOSIS)
OBSERVED, PLANTED = "observed", "planted"
ORIGINS = (OBSERVED, PLANTED)


class RatesError(Exception):
    """The answers or a diagnosis cannot be scored, for the reason given."""


def _truth(truth: Mapping) -> tuple[str, str]:
    cause, model = truth.get("cause"), truth.get("model")
    if cause not in CAUSES:
        raise RatesError(f"a project's cause must be one of the listed causes, not {cause!r}")
    if not isinstance(model, str) or not model.strip():
        raise RatesError(f"a project's model must be a name, not {model!r}")
    if truth.get("origin") not in ORIGINS:
        raise RatesError(
            f"a project's origin must be {OBSERVED!r} or {PLANTED!r}, not {truth.get('origin')!r}"
        )
    return cause, model.strip().lower()


def _matches(item: Mapping, cause: str, model: str) -> tuple[bool, bool]:
    return item["cause"] == cause, item["model"].strip().lower() == model


def score(diagnosis: Mapping | None, truth: Mapping) -> str:
    """The outcome of one checked diagnosis of one project, against its known cause.

    ``diagnosis`` is the agent's checked answer, or None when there is none
    (a refused answer, a crash, a project the validator passed). Only the
    first cause counts. ``other`` is never one of the bed's causes.
    """
    cause, model = _truth(truth)
    if diagnosis is None:
        return NO_DIAGNOSIS
    if not diagnosis["causes"]:
        return NO_CAUSE
    kind, place = _matches(diagnosis["causes"][0], cause, model)
    if kind and place:
        return RIGHT
    if place:
        return MODEL_ONLY
    return CAUSE_ONLY if kind else WRONG


def listed(diagnosis: Mapping | None, truth: Mapping) -> bool:
    """Whether any cause the diagnosis gives, first or not, is the known one."""
    cause, model = _truth(truth)
    if diagnosis is None:
        return False
    return any(all(_matches(item, cause, model)) for item in diagnosis["causes"])


USEFUL, WEAK, UNCLEAR = "useful", "weak", "unclear"
# Set before the first live run (research 50): useful when the agent surely
# names most root causes, weak when it surely names fewer than that.
ACCURACY_FLOOR = 0.60


@dataclass(frozen=True)
class Diagnosis:
    """One project diagnosed on one dataset, and what came of it."""

    project: str
    dataset: str
    variant: str
    origin: str
    outcome: str
    listed: bool = False
    # Whether the agent was called; a run that never called it costs nothing.
    called: bool = True
    cost_usd: float | None = None
    problem: str = ""


def _tally(diagnoses) -> Tally:
    right = sum(item.outcome == RIGHT for item in diagnoses)
    return Tally(right, len(diagnoses) - right)


def verdict(accuracy: Tally) -> str:
    """Useful, weak or unclear, from the count of right diagnoses among all finished."""
    if not accuracy.runs:
        return UNCLEAR
    low, high = interval(accuracy)
    if low >= ACCURACY_FLOOR:
        return USEFUL
    if high < ACCURACY_FLOOR:
        return WEAK
    return UNCLEAR


@dataclass(frozen=True)
class Summary:
    diagnoses: tuple[Diagnosis, ...]
    counts: dict[str, int]
    accuracy: Tally
    by_origin: dict[str, Tally]
    listed: int
    verdict: str
    cost_usd: float | None

    @property
    def per_diagnosis(self) -> float | None:
        if self.cost_usd is None or not self.accuracy.runs:
            return None
        return self.cost_usd / self.accuracy.runs

    @property
    def per_right(self) -> float | None:
        if self.cost_usd is None or not self.accuracy.passes:
            return None
        return self.cost_usd / self.accuracy.passes

    def variants(self) -> list[tuple[str, str, int, int]]:
        """Each variant with its origin, its right diagnoses and its runs."""
        found: dict[str, list] = {}
        for item in self.diagnoses:
            entry = found.setdefault(item.variant, [item.origin, 0, 0])
            entry[1] += item.outcome == RIGHT
            entry[2] += 1
        return [(name, *found[name]) for name in sorted(found)]


def summarise(diagnoses: list[Diagnosis]) -> Summary:
    """Counts, the accuracy overall and by origin, and the verdict.

    Every finished run is counted, with or without a diagnosis: a run that
    gives a person nothing has not named the cause. The cost is unknown if
    any call's is; runs that made no call add nothing.
    """
    counts = {outcome: 0 for outcome in OUTCOMES}
    for item in diagnoses:
        counts[item.outcome] += 1
    accuracy = _tally(diagnoses)
    by_origin = {o: _tally([d for d in diagnoses if d.origin == o]) for o in ORIGINS}
    costs = [item.cost_usd for item in diagnoses if item.called]
    cost = None if any(value is None for value in costs) else sum(costs)
    ordered = tuple(sorted(diagnoses, key=lambda d: (d.variant, d.dataset)))
    return Summary(
        ordered,
        counts,
        accuracy,
        by_origin,
        sum(item.listed for item in diagnoses),
        verdict(accuracy),
        cost,
    )


def _rate(tally: Tally) -> str:
    if not tally.runs:
        return "-"
    low, high = interval(tally)
    return f"{tally.passes} of {tally.runs} ({tally.passes / tally.runs:.0%}, 95% {low:.0%} to {high:.0%})"


def _usd(value: float | None) -> str:
    return "unknown" if value is None else f"US${value:.4f}"


def render(summary: Summary) -> str:
    """The report a person reads."""
    counts, runs = summary.counts, summary.accuracy.runs
    every = [name for name, _, right, seen in summary.variants() if right == seen]
    rule = (
        f"Rule: useful when the accuracy's lower limit is at least {ACCURACY_FLOOR:.0%}; weak "
        f"when its upper limit is below {ACCURACY_FLOOR:.0%}. Runs of one variant are not "
        "independent, so read the variants below as well."
    )
    cost = (
        f"Cost: {_usd(summary.cost_usd)} for {runs} runs, {_usd(summary.per_diagnosis)} a run, "
        f"{_usd(summary.per_right)} a right diagnosis (Claude Code's own estimates)"
    )
    others = ", ".join(f"{name} {counts[name]}" for name in OUTCOMES[1:])
    lines = [
        f"Diagnosis measurement: {summary.verdict}",
        "",
        f"Right (first cause names the cause and the model): {_rate(summary.accuracy)}",
        f"  observed in live runs: {_rate(summary.by_origin[OBSERVED])}",
        f"  planted: {_rate(summary.by_origin[PLANTED])}",
        f"Otherwise: {others}",
        f"The known cause anywhere in the diagnosis: {summary.listed} of {runs}",
        f"Variants right on every dataset: {len(every)} of {len(summary.variants())}",
        rule,
        cost,
        "",
    ]
    for name, origin, right, seen in summary.variants():
        lines.append(f"  {name:<28}  {origin:<8}  {right} of {seen}")
    lines.append("")
    for item in summary.diagnoses:
        note = f" ({item.problem})" if item.problem else ""
        lines.append(f"  {item.variant:<28}  {item.dataset:<10}  {item.outcome}{note}")
    return "\n".join(lines) + "\n"


RECORD_FILE = "diagnosis-record.json"
ANSWERS_FILE = "answers.json"
PLAN_FILE = "plan.json"
RUNS_DIR = "runs"
REPORT_TEXT, REPORT_JSON = "report.txt", "report.json"
NO_DIAGNOSIS_PREFIX = "no diagnosis: "

# Runs a diagnosis the way ``python -m tenx.run diagnose`` does: (project,
# raw, goal, declaration, out) to the run's final state.
Runner = Callable[[Path, Path, Path, Path, Path], dict]


def _cost(value) -> float | None:
    return None if value is None else float(Decimal(str(value)))


def diagnose_project(
    project: Path, raw: Path, goal: Path, declaration: Path, folder: Path, runner: Runner
) -> dict:
    """Diagnose one project into ``folder``, and keep what came of it there.

    The record says whether the validator passed the project, which checks
    it failed, the checked diagnosis (or None, and why), whether the agent
    was called and what the call cost.
    """
    out = Path(folder) / "run"
    state = runner(Path(project), Path(raw), Path(goal), Path(declaration), out)
    record = {"gate": None, "failing": [], "answer": None, "called": False}
    record.update({"cost_usd": None, "problem": ""})
    if state.get("validations"):
        result = ValidationResult.load(state["validations"][-1])
        record["gate"] = "PASS" if result.passed else "FAIL"
        record["failing"] = sorted([f.check, f.subject, f.status] for f in result.failures)
    for value in state.get("costs") or []:
        cost = CostRecord.load(value)
        if cost.node == DIAGNOSTICS_STEP:
            record["called"] = True
            known = cost.cost_basis != "unknown" and cost.estimated_usd is not None
            record["cost_usd"] = _cost(cost.estimated_usd) if known else None
    found = (state.get("artefacts") or {}).get("diagnosis")
    if found is not None:
        data = Path(found["path"]).with_name(ANSWER_FILE)
        record["answer"] = json.loads(data.read_text(encoding="utf-8"))
    else:
        claims = [
            AgentFinding.load(value).claim
            for value in state.get("findings") or []
            if value.get("agent") == DIAGNOSTICS_STEP
        ]
        missing = [
            c[len(NO_DIAGNOSIS_PREFIX) :] for c in claims if c.startswith(NO_DIAGNOSIS_PREFIX)
        ]
        record["problem"] = missing[0] if missing else state.get("reason") or "no diagnosis"
    text = json.dumps(record, indent=2, sort_keys=True) + "\n"
    artefacts.write_text(Path(folder) / RECORD_FILE, text)
    return record


def read_answers(bed: Path) -> dict[str, dict]:
    """The bed's answers by project, each checked as ``score`` will read it."""
    path = Path(bed) / ANSWERS_FILE
    try:
        projects = json.loads(path.read_text(encoding="utf-8"))["projects"]
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise RatesError(f"cannot read {path}: {error}") from error
    if not isinstance(projects, dict) or not projects:
        raise RatesError(f"{path} lists no projects")
    for name, truth in projects.items():
        if not isinstance(truth, dict) or not isinstance(truth.get("variant"), str):
            raise RatesError(f"the answer for {name} has no variant")
        if not isinstance(truth.get("failing"), list):
            raise RatesError(f"the answer for {name} lists no failing checks")
        score(None, truth)
        if not (Path(bed) / "projects" / name / "dbt_project.yml").is_file():
            raise RatesError(f"the bed has no project {name}")
    return projects


def plan(datasets: list[str], projects: list[str], seed: int = 0) -> list[tuple[str, str]]:
    """Every project on every dataset once, in an order shuffled by the seed."""
    order = [(dataset, project) for dataset in datasets for project in sorted(projects)]
    random.Random(f"{seed}:diagnosis-rates").shuffle(order)
    return order


def _settings(bed: Path, datasets: dict[str, Path], goal: Path, declaration: Path, seed: int):
    return {
        "answers": artefacts.file_hash(Path(bed) / ANSWERS_FILE),
        "datasets": {name: str(Path(folder).resolve()) for name, folder in datasets.items()},
        "goal": artefacts.file_hash(Path(goal)),
        "declaration": artefacts.file_hash(Path(declaration)),
        "seed": seed,
    }


def _record(out: Path, dataset: str, project: str) -> dict | None:
    try:
        return json.loads((out / RUNS_DIR / dataset / project / RECORD_FILE).read_text())
    except (OSError, ValueError):
        return None


def diagnoses(out: Path, answers: dict[str, dict], order: list[tuple[str, str]]) -> list[Diagnosis]:
    """Every planned run that has finished, scored against the answers."""
    found = []
    for dataset, project in order:
        record = _record(out, dataset, project)
        if record is None:
            continue
        truth = answers[project]
        problem = record["problem"]
        listed_failing = sorted(list(item) for item in truth["failing"])
        if record["gate"] == "FAIL" and record["failing"] != listed_failing:
            note = "the validator failed other checks than the bed lists"
            problem = f"{problem}; {note}" if problem else note
        found.append(
            Diagnosis(
                project,
                dataset,
                truth["variant"],
                truth["origin"],
                score(record["answer"], truth),
                listed(record["answer"], truth),
                record["called"],
                record["cost_usd"],
                problem,
            )
        )
    return found


def estimate(pending: int, rules: Policy) -> tuple[Decimal, Decimal]:
    """The typical cost and the most the pending runs can cost, at the agent's call figures."""
    kind = DIAGNOSTICS.budget_kind
    return rules.typical_usd[kind] * pending, rules.cap(kind) * pending


def report(out: Path) -> str:
    """Score every finished run of the measurement in out, and keep the report there."""
    out = Path(out)
    saved = json.loads((out / PLAN_FILE).read_text(encoding="utf-8"))
    answers = read_answers(Path(saved["bed"]))
    order = [tuple(pair) for pair in saved["order"]]
    summary = summarise(diagnoses(out, answers, order))
    text = render(summary)
    if len(summary.diagnoses) < len(order):
        text += f"\n{len(order) - len(summary.diagnoses)} of {len(order)} runs not done yet\n"
    data = {
        "verdict": summary.verdict,
        "counts": summary.counts,
        "right": [summary.accuracy.passes, summary.accuracy.runs],
        "by_origin": {o: [t.passes, t.runs] for o, t in summary.by_origin.items()},
        "listed": summary.listed,
        "cost_usd": summary.cost_usd,
        "diagnoses": [vars(item) for item in summary.diagnoses],
    }
    artefacts.write_text(out / REPORT_TEXT, text)
    artefacts.write_text(out / REPORT_JSON, json.dumps(data, indent=2) + "\n")
    return text


def measure(
    bed: Path,
    datasets: dict[str, Path],
    goal: Path,
    declaration: Path,
    out: Path,
    runner: Callable[[Path], Runner],
    seed: int = 0,
    parallel: int = 3,
    approve_cost: bool = False,
    progress=print,
) -> str:
    """Diagnose every bed project on every dataset, then report.

    ``runner(out)`` gives the function that runs one diagnosis, once the
    cost is approved. A run with a record is kept; one cut off part way, or
    one that raised, is started again on the next start. The runs still to
    do are priced first: above the policy's limit nothing is written or run
    unless ``approve_cost`` is true.
    """
    bed, out = Path(bed).resolve(), Path(out).resolve()
    answers = read_answers(bed)
    settings = _settings(bed, datasets, goal, declaration, seed)
    order = plan(list(datasets), list(answers), seed)
    if (out / PLAN_FILE).is_file():
        saved = json.loads((out / PLAN_FILE).read_text(encoding="utf-8"))
        if saved["settings"] != settings:
            raise RatesError(f"{out} holds a measurement of other projects, data or settings")
    pending = [pair for pair in order if _record(out, *pair) is None]
    rules = Policy()
    typical, ceiling = estimate(len(pending), rules)
    described = (
        f"{len(pending)} diagnoses to do, estimated at US${typical}, at most US${ceiling} "
        "at each call's cap (Claude Code's own estimates)"
    )
    if typical > rules.approve_above_usd and not approve_cost:
        raise CostNotApproved(
            f"{described}; above US${rules.approve_above_usd}, so it needs approval: "
            "run it again with --approve-cost to go ahead"
        )
    progress(described)
    out.mkdir(parents=True, exist_ok=True)
    run_one = runner(out)
    plan_text = {"bed": str(bed), "settings": settings, "order": [list(p) for p in order]}
    artefacts.write_text(out / PLAN_FILE, json.dumps(plan_text, indent=2) + "\n")
    lock, done = threading.Lock(), [len(order) - len(pending)]

    def one(pair: tuple[str, str]) -> None:
        dataset, project = pair
        folder = out / RUNS_DIR / dataset / project
        if folder.exists():
            shutil.rmtree(folder)
        folder.mkdir(parents=True)
        try:
            record = diagnose_project(
                bed / "projects" / project,
                Path(datasets[dataset]).resolve(),
                Path(goal).resolve(),
                Path(declaration).resolve(),
                folder,
                run_one,
            )
            outcome = score(record["answer"], answers[project])
        except Exception as error:  # noqa: BLE001 - one run must not stop the rest
            # Any failure of one run (a sandbox refusal, a crash in the graph)
            # leaves it without a record, so a second start runs it again.
            outcome = f"error: {error}"
        with lock:
            done[0] += 1
            progress(f"[{done[0]}/{len(order)}] {dataset} {project}: {outcome}")

    with ThreadPoolExecutor(max_workers=parallel) as pool:
        for future in [pool.submit(one, pair) for pair in pending]:
            future.result()
    return report(out)
