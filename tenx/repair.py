"""The repair loop: what the engineer is told when its work fails, and when it tries again.

When the validator fails a project, a run may give the engineer another
attempt on its own work, with ``GATE_REPORT.md`` saying what failed. The
report says only what the validator lets an agent be told of each check
(``CheckResult.feedback``); a check it says nothing about is named, with
its model and status, and nothing more. The checks themselves stay hidden.

A further attempt is made only while repairs remain and the attempt just
checked failed fewer checks than the one before it, so a repair that makes
no headway ends the run rather than spending more on the same fault.
"""

from __future__ import annotations

from dataclasses import dataclass

from tenx.domain import MAX_REPAIRS, ValidationResult

__all__ = ["MAX_REPAIRS", "REPORT_FILE", "Decision", "decide", "render"]

REPORT_FILE = "GATE_REPORT.md"


def _counted(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def render(validation: ValidationResult) -> str:
    """``GATE_REPORT.md`` for a failed validation, as the engineer is given it."""
    if validation.passed:
        raise ValueError("a validation that passed has nothing to repair")
    failed = [f for f in validation.failures if f.status == "fail"]
    skipped = [f for f in validation.failures if f.status == "skip"]
    passed = validation.checks - len(validation.failures)
    lines = [
        "# What the checks found",
        "",
        "Your previous attempt was built from an empty database and checked after you",
        f"finished. {_counted(len(failed), 'check')} failed, {passed} passed"
        + (f" and {len(skipped)} could not run." if skipped else "."),
        "The project in this folder is that attempt. Find the cause of each failure",
        "and fix it in the models, for every row it affects, not only for the rows or",
        "values named here, and run `dbt build` again before you stop.",
    ]
    for failure in failed:
        lines += ["", f"## {failure.check}: {failure.subject}", ""]
        lines.append(failure.feedback or "Failed. No more is said about this check.")
    if skipped:
        lines += ["", "## Could not run", ""]
        lines += [f"- {failure.check}: {failure.subject}" for failure in skipped]
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class Decision:
    """Whether the run repairs its work again, and why or why not, in a sentence."""

    repair: bool
    reason: str


def decide(validations: list[ValidationResult], repairs: int) -> Decision:
    """After a validation: try again, or stop, given the repairs the run asked for.

    ``validations`` are every validation of the run so far, oldest first; the
    first checked the engineer's first attempt, each later one a repair.
    """
    if not validations:
        raise ValueError("nothing has been validated yet")
    latest = validations[-1]
    made = len(validations) - 1
    if latest.passed:
        return Decision(False, "passed")
    if made >= repairs:
        if repairs == 0:
            return Decision(False, "no repairs were asked for")
        return Decision(False, f"the repairs allowed ({repairs}) were made")
    if made:
        before, now = len(validations[-2].failures), len(latest.failures)
        if now >= before:
            return Decision(
                False,
                f"repair {made} left {_counted(now, 'check')} failing or not run, "
                f"against {before} before it, so no further repair was made",
            )
    return Decision(True, f"repair {made + 1} of {repairs}")
