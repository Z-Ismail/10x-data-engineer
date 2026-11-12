"""How often the reviewer catches a planted mistake, and how often it rejects good work.

A test bed is a folder of dbt projects that all pass the validator, with an
``answers.json`` beside them saying which carry one planted mistake (and in
which model and columns) and which are correct. Each project is reviewed on
each dataset; each review is scored here against the answers the reviewer
never sees.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from tenx import artefacts
from tenx import review as packets
from tenx.compare import Tally, interval
from tenx.ledger import role_usage
from tenx.ports import (
    AgentBackend,
    CheckResult,
    Mounts,
    PlatformError,
    Role,
    Sandbox,
    ValidationReport,
    Validator,
)
from tenx.reviewer import BLOCKING, REQUEST_CHANGES, ReviewError, review_from_report

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
    "Kit",
    "RatesError",
    "Review",
    "Summary",
    "render",
    "review_project",
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


REVIEW_RECORD = "review.json"
GATE_DIR, PACKET_DIR, REVIEWER_DIR = "gate", "packet", "reviewer"
AGENT_FILE = "agent.json"


@dataclass(frozen=True)
class Kit:
    """The ports one review works through.

    ``mounts`` gives what the reviewer may see for its folder; ``hidden``
    is what the validator's builds must never see; ``secrets`` are files
    whose bytes may never reach a packet (the declaration, the bed's answers).
    """

    validator: Validator
    backend: AgentBackend
    role: Role
    sandbox: Sandbox | None
    mounts: Callable[[Path], Mounts]
    hidden: tuple[Path, ...] = ()
    secrets: tuple[Path, ...] = ()
    timeout: float = 30 * 60


def _agent_view(report: ValidationReport) -> ValidationReport:
    # Each check and what an agent may be told of it; nothing else reaches the packet.
    results = tuple(
        CheckResult(item.check, item.subject, item.status, "", {}, item.feedback)
        for item in report.results
    )
    return ValidationReport(results, report.passed, "")


def _keep(folder: Path, record: dict) -> None:
    text = json.dumps(record, indent=2, sort_keys=True) + "\n"
    artefacts.write_text(folder / REVIEW_RECORD, text)


def review_project(
    project: Path, raw: Path, declaration: Path, goal: Path, profiles: Path, folder: Path, kit: Kit
) -> dict:
    """Validate one project, and if it passes, have the reviewer review it.

    Everything is kept in ``folder``: the validator's work, the packet, the
    reviewer's session and ``review.json`` with the checked answer (or the
    reason there is none) and the call's cost.
    """
    folder.mkdir(parents=True)
    record = {"gate": "FAIL", "answer": None, "cost_usd": None, "problem": ""}
    try:
        report = kit.validator.validate(
            project, raw, declaration, folder / GATE_DIR, kit.sandbox, kit.hidden
        )
    except PlatformError as error:
        report, record["problem"] = None, f"the validator could not run: {error}"
    if report is not None and not report.passed:
        failed = sorted({r.check for r in report.results if r.status.lower() != "pass"})
        record["problem"] = f"the validator failed the project: {', '.join(failed)}"
    if report is None or not report.passed:
        _keep(folder, record)
        return record
    record["gate"] = "PASS"
    try:
        packet = packets.build(
            folder / PACKET_DIR,
            goal=goal,
            profiles=profiles,
            project=project,
            report=_agent_view(report),
            secrets=(declaration, folder / GATE_DIR, *kit.secrets),
        )
    except packets.PacketError as error:
        record["problem"] = f"no packet: {error}"
        _keep(folder, record)
        return record
    work = folder / REVIEWER_DIR / "work"
    shutil.copytree(packet.folder, work)
    agent = kit.backend.run(
        kit.role,
        work.resolve(),
        folder / REVIEWER_DIR / AGENT_FILE,
        kit.timeout,
        kit.sandbox,
        kit.mounts(work.resolve()),
    )
    missing = "timed out" if agent.timed_out_after is not None else "no report"
    usage = role_usage(kit.role.name, agent.report, missing)
    record["cost_usd"] = usage.cost_usd if usage.cost_known else None
    prefix = f"{packets.PROJECT_DIR}/"
    files = {
        name[len(prefix) :]: (packet.folder / name).read_text(encoding="utf-8", errors="replace")
        for name in packet.files
        if name.startswith(prefix)
    }
    try:
        record["answer"] = review_from_report(agent.report, files)
    except ReviewError as error:
        record["problem"] = str(error)
    _keep(folder, record)
    return record
