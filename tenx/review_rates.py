"""How often the reviewer catches a planted mistake, and how often it rejects good work.

A test bed is a folder of dbt projects that all pass the validator, with an
``answers.json`` beside them saying which carry one planted mistake (and in
which model and columns) and which are correct. Each project is reviewed on
each dataset; each review is scored here against the answers the reviewer
never sees.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from tenx.compare import Tally, interval
from tenx.reviewer import BLOCKING, REQUEST_CHANGES

__all__ = [
    "APPROVED",
    "CAUGHT",
    "DEFECT",
    "ELSEWHERE",
    "GOOD",
    "MISSED",
    "NO_REVIEW",
    "OUTCOMES",
    "REJECTED",
    "RatesError",
    "Review",
    "Summary",
    "render",
    "score",
    "summarise",
    "verdict",
]

DEFECT, GOOD = "defect", "good"
CAUGHT, ELSEWHERE, MISSED = "caught", "elsewhere", "missed"
APPROVED, REJECTED = "approved", "rejected"
NO_REVIEW = "no review"
OUTCOMES = {DEFECT: (CAUGHT, ELSEWHERE, MISSED), GOOD: (APPROVED, REJECTED)}


class RatesError(Exception):
    """The answers or a review cannot be scored, for the reason given."""


def _places(truth: Mapping) -> set[tuple[str, str]]:
    columns = truth.get("columns")
    if not isinstance(columns, list) or not columns:
        raise RatesError("a defect must name at least one [model, column] it makes wrong")
    places = set()
    for pair in columns:
        if (
            not isinstance(pair, list)
            or len(pair) != 2
            or not all(isinstance(part, str) and part.strip() for part in pair)
        ):
            raise RatesError(f"a defect's column must be [model, column], not {pair!r}")
        places.add((pair[0].strip().lower(), pair[1].strip().lower()))
    return places


def score(review: Mapping | None, truth: Mapping) -> str:
    """The outcome of one checked review of one project, against what the project is.

    ``review`` is the reviewer's checked answer, or None when there is none
    (a crash, a time-out, an answer that failed its check). A defect counts
    as caught only when a blocking finding names its model and one of its
    columns; a request for changes that does not is ``elsewhere``.
    """
    kind = truth.get("kind")
    if kind not in OUTCOMES:
        raise RatesError(f"a project's kind must be {DEFECT!r} or {GOOD!r}, not {kind!r}")
    places = _places(truth) if kind == DEFECT else set()
    if review is None:
        return NO_REVIEW
    asked = review["verdict"] == REQUEST_CHANGES
    if kind == GOOD:
        return REJECTED if asked else APPROVED
    for finding in review["findings"]:
        place = (finding["model"].strip().lower(), finding["column"].strip().lower())
        if finding["severity"] == BLOCKING and place in places:
            return CAUGHT
    return ELSEWHERE if asked else MISSED


USEFUL, HARMFUL, UNCLEAR = "useful", "harmful", "unclear"
# Set before the first live run (research 46): the reviewer is useful when
# it surely catches most planted mistakes and surely rejects few good
# projects; harmful when it surely rejects more good work than that.
CATCH_FLOOR = 0.60
REJECTION_LIMIT = 0.25


@dataclass(frozen=True)
class Review:
    """One project reviewed on one dataset, and what came of it."""

    project: str
    dataset: str
    variant: str
    kind: str
    outcome: str
    cost_usd: float | None = None
    problem: str = ""


def verdict(catches: Tally, rejections: Tally) -> str:
    """Useful, harmful or unclear, from the catch and false-rejection counts."""
    if rejections.runs and interval(rejections)[0] > REJECTION_LIMIT:
        return HARMFUL
    if (
        catches.runs
        and rejections.runs
        and interval(catches)[0] >= CATCH_FLOOR
        and interval(rejections)[1] <= REJECTION_LIMIT
    ):
        return USEFUL
    return UNCLEAR


@dataclass(frozen=True)
class Summary:
    reviews: tuple[Review, ...]
    counts: dict[str, int]
    catches: Tally
    rejections: Tally
    verdict: str
    cost_usd: float | None

    @property
    def scored(self) -> int:
        return self.catches.runs + self.rejections.runs

    @property
    def per_review(self) -> float | None:
        if self.cost_usd is None or not self.scored:
            return None
        return self.cost_usd / self.scored

    @property
    def per_catch(self) -> float | None:
        if self.cost_usd is None or not self.catches.passes:
            return None
        return self.cost_usd / self.catches.passes


def summarise(reviews: list[Review]) -> Summary:
    """Counts, both rates and the verdict; cost is unknown if any review's is."""
    counts = {outcome: 0 for outcome in (*OUTCOMES[DEFECT], *OUTCOMES[GOOD], NO_REVIEW)}
    for review in reviews:
        counts[review.outcome] += 1
    catches = Tally(counts[CAUGHT], counts[ELSEWHERE] + counts[MISSED])
    rejections = Tally(counts[REJECTED], counts[APPROVED])
    costs = [review.cost_usd for review in reviews if review.outcome != NO_REVIEW]
    cost = None if any(value is None for value in costs) else sum(costs)
    ordered = tuple(sorted(reviews, key=lambda r: (r.kind, r.variant, r.dataset)))
    return Summary(ordered, counts, catches, rejections, verdict(catches, rejections), cost)


def _rate(tally: Tally) -> str:
    if not tally.runs:
        return "-"
    low, high = interval(tally)
    return f"{tally.passes} of {tally.runs} ({tally.passes / tally.runs:.0%}, 95% {low:.0%} to {high:.0%})"


def _usd(value: float | None) -> str:
    return "unknown" if value is None else f"US${value:.4f}"


def render(summary: Summary) -> str:
    """The report a person reads."""
    counts = summary.counts
    asked = counts[CAUGHT] + counts[ELSEWHERE]
    rule = (
        f"Rule: useful when the catch rate's lower limit is at least {CATCH_FLOOR:.0%} and the "
        f"false-rejection rate's upper limit at most {REJECTION_LIMIT:.0%}; harmful when the "
        f"false-rejection rate's lower limit is above {REJECTION_LIMIT:.0%}."
    )
    cost = (
        f"Cost: {_usd(summary.cost_usd)} for {summary.scored} reviews, "
        f"{_usd(summary.per_review)} a review, {_usd(summary.per_catch)} a catch "
        "(Claude Code's own estimates)"
    )
    lines = [
        f"Reviewer measurement: {summary.verdict}",
        "",
        f"Caught (blocking, right model and column): {_rate(summary.catches)}",
        f"Defect projects with any request for changes: {asked} of {summary.catches.runs}",
        f"Good projects rejected: {_rate(summary.rejections)}",
        rule,
        cost,
        "",
    ]
    for review in summary.reviews:
        note = f" ({review.problem})" if review.problem else ""
        lines.append(
            f"  {review.kind:<6}  {review.variant:<30}  {review.dataset:<10}  "
            f"{review.outcome}{note}"
        )
    return "\n".join(lines) + "\n"
