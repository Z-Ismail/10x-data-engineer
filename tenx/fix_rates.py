"""Whether the engineer fixes more failing projects when it is given the diagnosis.

A test bed is a folder of dbt projects that each fail the validator for one
known reason, with an ``answers.json`` beside them (see
``tenx.diagnosis_rates``). Each project is fixed on each dataset twice by
the run a user starts with ``python -m tenx.run diagnose --fixes 1``: once
as it is, where the engineer gets the Diagnostics agent's ``DIAGNOSIS.md``
beside what the checks found, and once with the diagnostics step left out,
where it gets what the checks found alone. Everything else is the same, so
the two runs of a project on a dataset are a pair.

A run is fixed when the validator passes the engineer's fix. The difference
in fix rates, with the diagnosis minus without, has Tango's 95% interval for
paired proportions (``tenx.parity``).
"""

from __future__ import annotations

import json
import random
import shutil
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from tenx import artefacts, parity
from tenx.compare import CostNotApproved
from tenx.diagnosis_rates import ANSWERS_FILE, RatesError, read_answers, score
from tenx.diagnostics import ANSWER_FILE
from tenx.domain import CostRecord, ValidationResult
from tenx.engineer import DIAGNOSTICS, ENGINEER
from tenx.policy import Policy

__all__ = [
    "ARMS",
    "HELPS",
    "HURTS",
    "NO_GAIN",
    "UNCLEAR",
    "WITH",
    "WITHOUT",
    "Pair",
    "Summary",
    "fix_project",
    "measure",
    "pairs",
    "plan",
    "render",
    "report",
    "summarise",
    "verdict",
]

WITH, WITHOUT = "with", "without"
ARMS = (WITH, WITHOUT)
HELPS, HURTS, NO_GAIN, UNCLEAR = "helps", "hurts", "no gain", "unclear"
# Set before the first live run (research 52): no gain when the diagnosis
# surely adds less than this to the fix rate, the parity margin.
GAIN_MARGIN = parity.MARGIN


@dataclass(frozen=True)
class Pair:
    """One project fixed on one dataset with and without the diagnosis."""

    project: str
    dataset: str
    variant: str
    origin: str
    fixed_with: bool
    fixed_without: bool
    # What each run cost, its diagnosis included; None when a call's is unknown.
    cost_with: float | None = None
    cost_without: float | None = None
    # How the diagnosis given in the run with it scores (tenx.diagnosis_rates).
    diagnosis: str = ""
    problem: str = ""


def verdict(low: float, high: float, margin: float = GAIN_MARGIN) -> str:
    """Helps above 0, hurts below it, no gain when it surely adds less than the margin."""
    if low > 0:
        return HELPS
    if high < 0:
        return HURTS
    if high < margin:
        return NO_GAIN
    return UNCLEAR


def count(pairs: list[Pair]) -> parity.Pairs:
    """Both fixed, only with the diagnosis, only without, neither."""
    found = [(p.fixed_with, p.fixed_without) for p in pairs]
    return parity.Pairs(
        both=found.count((True, True)),
        first_only=found.count((True, False)),
        second_only=found.count((False, True)),
        neither=found.count((False, False)),
    )


@dataclass(frozen=True)
class Summary:
    pairs: tuple[Pair, ...]
    counts: parity.Pairs
    interval: tuple[float, float] | None
    verdict: str
    # US$ per passing fix in each arm; infinite when nothing was fixed.
    per_fix: dict[str, float] | None
    # The with / without ratio of cost per passing fix, bootstrap 95%.
    ratio_interval: tuple[float, float] | None

    def variants(self) -> list[tuple[str, str, int, int, int]]:
        """Each variant with its origin, fixes with and without, and its pairs."""
        found: dict[str, list] = {}
        for item in self.pairs:
            entry = found.setdefault(item.variant, [item.origin, 0, 0, 0])
            entry[1] += item.fixed_with
            entry[2] += item.fixed_without
            entry[3] += 1
        return [(name, *found[name]) for name in sorted(found)]


def summarise(pairs: list[Pair], seed: int = 0) -> Summary:
    """The paired counts, the interval and verdict, and the cost per passing fix."""
    counts = count(pairs)
    low_high = parity.interval(counts) if pairs else None
    per_fix, ratio = None, None
    known = all(p.cost_with is not None and p.cost_without is not None for p in pairs)
    if pairs and known:
        costs = [
            parity.PairCost(p.fixed_without, p.cost_without, p.fixed_with, p.cost_with)
            for p in pairs
        ]
        per_fix = {
            WITH: parity.cost_per_pass(
                [p.cost_with for p in pairs], sum(p.fixed_with for p in pairs)
            ),
            WITHOUT: parity.cost_per_pass(
                [p.cost_without for p in pairs], sum(p.fixed_without for p in pairs)
            ),
        }
        ratio = parity.cost_interval(costs, seed=seed)
    ordered = tuple(sorted(pairs, key=lambda p: (p.variant, p.dataset)))
    shown = verdict(*low_high) if low_high else UNCLEAR
    return Summary(ordered, counts, low_high, shown, per_fix, ratio)


def _points(value: float) -> str:
    return f"{value * 100:+.1f}"


def _usd(value: float) -> str:
    return "-" if value == float("inf") else f"US${value:.4f}"


def _ratio(value: float) -> str:
    return "infinite" if value == float("inf") else f"{value:.3f}"


def render(summary: Summary) -> str:
    """The report a person reads."""
    counts, n = summary.counts, summary.counts.n
    lines = [f"Fix measurement: {summary.verdict}", ""]
    if not n:
        return "\n".join(lines + ["No pairs finished yet."]) + "\n"
    low, high = summary.interval
    with_rate = (counts.both + counts.first_only) / n
    without_rate = (counts.both + counts.second_only) / n
    lines += [
        f"Pairs: {n} (each project fixed once on each dataset with the diagnosis and once without)",
        (
            f"  both fixed {counts.both}, only with the diagnosis {counts.first_only}, "
            f"only without {counts.second_only}, neither {counts.neither}"
        ),
        (
            f"Fixed: with the diagnosis {counts.both + counts.first_only} of {n} "
            f"({with_rate:.0%}), without {counts.both + counts.second_only} of {n} "
            f"({without_rate:.0%})"
        ),
        (
            f"Difference: {_points(counts.difference)} points, 95% interval (Tango) "
            f"{_points(low)} to {_points(high)}"
        ),
        (
            "Rule: helps when the interval lies above 0, hurts when it lies below 0, no gain "
            f"when its upper limit is below {GAIN_MARGIN * 100:.0f} points. Pairs of one "
            "variant are not independent, so read the variants below as well."
        ),
    ]
    if summary.per_fix is None:
        lines.append("Cost per passing fix: unknown (a run's cost was not reported)")
    else:
        cost_low, cost_high = summary.ratio_interval
        lines += [
            (
                "US$ per passing fix (the diagnosis included): "
                f"with {_usd(summary.per_fix[WITH])}, without {_usd(summary.per_fix[WITHOUT])}"
            ),
            (
                "With / without: 95% interval (bootstrap over pairs) "
                f"{_ratio(cost_low)} to {_ratio(cost_high)}"
            ),
        ]
    lines += ["Cost is Claude Code's own estimate, not a bill.", ""]
    for name, origin, fixed_with, fixed_without, seen in summary.variants():
        lines.append(
            f"  {name:<28}  {origin:<8}  with {fixed_with} of {seen}, "
            f"without {fixed_without} of {seen}"
        )
    lines.append("")
    for item in summary.pairs:
        shown = f"with {'fixed' if item.fixed_with else 'not fixed'}, "
        shown += f"without {'fixed' if item.fixed_without else 'not fixed'}"
        if item.diagnosis:
            shown += f"; diagnosis {item.diagnosis}"
        note = f" ({item.problem})" if item.problem else ""
        lines.append(f"  {item.variant:<28}  {item.dataset:<10}  {shown}{note}")
    return "\n".join(lines) + "\n"


RECORD_FILE = "fix-record.json"
PLAN_FILE = "plan.json"
RUNS_DIR = "runs"
REPORT_TEXT, REPORT_JSON = "report.txt", "report.json"
# The engineer makes one fix: the only one the diagnosis is given to.
FIXES = 1

# Runs a fix the way ``python -m tenx.run diagnose --fixes 1`` does: (project,
# raw, goal, declaration, out, skip_diagnosis) to the run's final state.
Runner = Callable[[Path, Path, Path, Path, Path, bool], dict]


def _cost(cost: CostRecord) -> float | None:
    if cost.cost_basis == "unknown" or cost.estimated_usd is None:
        return None
    return float(Decimal(str(cost.estimated_usd)))


def _add(total: float | None, value: float | None) -> float | None:
    return None if total is None or value is None else total + value


def fix_project(
    project: Path,
    raw: Path,
    goal: Path,
    declaration: Path,
    folder: Path,
    runner: Runner,
    arm: str,
) -> dict:
    """Fix one project into ``folder``, with or without the diagnosis, and keep what came of it.

    The record says how the validator found the project as handed over and
    after the fix, whether a fix was made and passed, the checked diagnosis
    when there was one, and what the diagnosis and the fix each cost.
    """
    if arm not in ARMS:
        raise RatesError(f"the arm must be {WITH!r} or {WITHOUT!r}, not {arm!r}")
    out = Path(folder) / "run"
    state = runner(Path(project), Path(raw), Path(goal), Path(declaration), out, arm == WITHOUT)
    checked = [ValidationResult.load(item) for item in state.get("validations") or []]
    record = {"arm": arm, "first": None, "failing": [], "last": None, "fix_made": False}
    record.update({"fixed": False, "answer": None, "problem": ""})
    if checked:
        record["first"] = "PASS" if checked[0].passed else "FAIL"
        record["failing"] = sorted([f.check, f.subject, f.status] for f in checked[0].failures)
        record["last"] = "PASS" if checked[-1].passed else "FAIL"
    costs = {"diagnostics": 0.0, "engineer": 0.0}
    for value in state.get("costs") or []:
        cost = CostRecord.load(value)
        if cost.node in costs:
            costs[cost.node] = _add(costs[cost.node], _cost(cost))
            record["fix_made"] = record["fix_made"] or cost.node == "engineer"
    record["cost_diagnosis"], record["cost_fix"] = costs["diagnostics"], costs["engineer"]
    record["cost_usd"] = _add(costs["diagnostics"], costs["engineer"])
    record["fixed"] = (
        record["fix_made"]
        and len(checked) > 1
        and checked[-1].passed
        and state.get("status") == "passed"
    )
    found = (state.get("artefacts") or {}).get("diagnosis")
    if found is not None:
        data = Path(found["path"]).with_name(ANSWER_FILE)
        record["answer"] = json.loads(data.read_text(encoding="utf-8"))
    if record["first"] == "PASS":
        record["problem"] = "the project passed as handed over"
    elif not record["fixed"]:
        made = "the fix still failed" if record["fix_made"] else "no fix made"
        reason = state.get("reason") or ""
        if reason.startswith(made):
            record["problem"] = reason
        else:
            record["problem"] = f"{made}: {reason}" if reason else made
    text = json.dumps(record, indent=2, sort_keys=True) + "\n"
    artefacts.write_text(Path(folder) / RECORD_FILE, text)
    return record


def plan(datasets: list[str], projects: list[str], seed: int = 0) -> list[tuple[str, str, str]]:
    """Every project on every dataset in both arms, a pair's two runs together.

    The pairs are shuffled by the seed, and so is which arm of a pair runs first.
    """
    rng = random.Random(f"{seed}:fix-rates")
    order = [(dataset, project) for dataset in datasets for project in sorted(projects)]
    rng.shuffle(order)
    runs = []
    for dataset, project in order:
        arms = list(ARMS)
        rng.shuffle(arms)
        runs += [(dataset, project, arm) for arm in arms]
    return runs


def _settings(bed: Path, datasets: dict[str, Path], goal: Path, declaration: Path, seed: int):
    return {
        "answers": artefacts.file_hash(Path(bed) / ANSWERS_FILE),
        "datasets": {name: str(Path(folder).resolve()) for name, folder in datasets.items()},
        "goal": artefacts.file_hash(Path(goal)),
        "declaration": artefacts.file_hash(Path(declaration)),
        "fixes": FIXES,
        "seed": seed,
    }


def _record(out: Path, dataset: str, project: str, arm: str) -> dict | None:
    try:
        path = out / RUNS_DIR / dataset / project / arm / RECORD_FILE
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def pairs(out: Path, answers: dict[str, dict], order: list[tuple[str, str, str]]) -> list[Pair]:
    """Every pair whose two runs have finished, in the order planned."""
    found, seen = [], set()
    for dataset, project, _ in order:
        if (dataset, project) in seen:
            continue
        seen.add((dataset, project))
        both = {arm: _record(out, dataset, project, arm) for arm in ARMS}
        if None in both.values():
            continue
        truth = answers[project]
        notes = [f"{arm}: {both[arm]['problem']}" for arm in ARMS if both[arm]["problem"]]
        if both[WITH]["failing"] != both[WITHOUT]["failing"]:
            notes.append("the validator found the project differently in the two runs")
        found.append(
            Pair(
                project,
                dataset,
                truth["variant"],
                truth["origin"],
                both[WITH]["fixed"],
                both[WITHOUT]["fixed"],
                both[WITH]["cost_usd"],
                both[WITHOUT]["cost_usd"],
                score(both[WITH]["answer"], truth),
                "; ".join(notes),
            )
        )
    return found


def estimate(runs: list[tuple[str, str, str]], rules: Policy) -> tuple[Decimal, Decimal]:
    """The typical cost and the most the runs can cost, at the agents' call figures.

    Every run pays for its fix; a run with the diagnosis also pays for that.
    """
    typical = ceiling = Decimal(0)
    for *_, arm in runs:
        kinds = [ENGINEER.budget_kind] + ([DIAGNOSTICS.budget_kind] if arm == WITH else [])
        typical += sum(rules.typical_usd[kind] for kind in kinds)
        ceiling += sum(rules.cap(kind) for kind in kinds)
    return typical, ceiling


def report(out: Path) -> str:
    """Pair and score every finished run of the measurement in out, and keep the report there."""
    out = Path(out)
    saved = json.loads((out / PLAN_FILE).read_text(encoding="utf-8"))
    answers = read_answers(Path(saved["bed"]))
    order = [tuple(item) for item in saved["order"]]
    summary = summarise(pairs(out, answers, order), seed=saved["settings"]["seed"])
    text = render(summary)
    planned = len(order) // 2
    if len(summary.pairs) < planned:
        text += f"\n{planned - len(summary.pairs)} of {planned} pairs not done yet\n"
    counts = summary.counts
    data = {
        "verdict": summary.verdict,
        "pairs": {
            "both": counts.both,
            "with_only": counts.first_only,
            "without_only": counts.second_only,
            "neither": counts.neither,
        },
        "interval_95": list(summary.interval) if summary.interval else None,
        "cost_per_fix_usd": (
            {k: None if v == float("inf") else v for k, v in summary.per_fix.items()}
            if summary.per_fix
            else None
        ),
        "pairs_found": [vars(item) for item in summary.pairs],
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
    """Fix every bed project on every dataset with and without the diagnosis, then report.

    ``runner(out)`` gives the function that runs one fix, once the cost is
    approved. A run with a record is kept; one cut off part way, or one that
    raised, is started again on the next start. The runs still to do are
    priced first: above the policy's limit nothing is written or run unless
    ``approve_cost`` is true.
    """
    bed, out = Path(bed).resolve(), Path(out).resolve()
    answers = read_answers(bed)
    settings = _settings(bed, datasets, goal, declaration, seed)
    order = plan(list(datasets), list(answers), seed)
    if (out / PLAN_FILE).is_file():
        saved = json.loads((out / PLAN_FILE).read_text(encoding="utf-8"))
        if saved["settings"] != settings:
            raise RatesError(f"{out} holds a measurement of other projects, data or settings")
    pending = [run for run in order if _record(out, *run) is None]
    rules = Policy()
    typical, ceiling = estimate(pending, rules)
    described = (
        f"{len(pending)} fixing runs to do, estimated at US${typical}, at most US${ceiling} "
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
    plan_text = {"bed": str(bed), "settings": settings, "order": [list(r) for r in order]}
    artefacts.write_text(out / PLAN_FILE, json.dumps(plan_text, indent=2) + "\n")
    lock, done = threading.Lock(), [len(order) - len(pending)]

    def one(run: tuple[str, str, str]) -> None:
        dataset, project, arm = run
        folder = out / RUNS_DIR / dataset / project / arm
        if folder.exists():
            shutil.rmtree(folder)
        folder.mkdir(parents=True)
        try:
            record = fix_project(
                bed / "projects" / project,
                Path(datasets[dataset]).resolve(),
                Path(goal).resolve(),
                Path(declaration).resolve(),
                folder,
                run_one,
                arm,
            )
            outcome = "fixed" if record["fixed"] else "not fixed"
        except Exception as error:  # noqa: BLE001 - one run must not stop the rest
            # A run that raised has no record, so a second start runs it again.
            outcome = f"error: {error}"
        with lock:
            done[0] += 1
            progress(f"[{done[0]}/{len(order)}] {dataset} {project} {arm}: {outcome}")

    with ThreadPoolExecutor(max_workers=parallel) as pool:
        for future in [pool.submit(one, run) for run in pending]:
            future.result()
    return report(out)
