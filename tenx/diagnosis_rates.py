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

from collections.abc import Mapping
from dataclasses import dataclass

from tenx.compare import Tally, interval
from tenx.diagnostics import CAUSES

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
    "listed",
    "render",
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
