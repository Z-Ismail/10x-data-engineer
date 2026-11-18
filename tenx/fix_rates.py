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

from dataclasses import dataclass

from tenx import parity

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
    "render",
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
