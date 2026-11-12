"""How often the reviewer catches a planted mistake, and how often it rejects good work.

A test bed is a folder of dbt projects that all pass the validator, with an
``answers.json`` beside them saying which carry one planted mistake (and in
which model and columns) and which are correct. Each project is reviewed on
each dataset; each review is scored here against the answers the reviewer
never sees.
"""

from __future__ import annotations

from collections.abc import Mapping

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
    "score",
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
