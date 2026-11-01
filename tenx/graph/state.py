"""The state a run's graph carries between steps, and how parallel writes join.

The state is saved after every step and read back on resume, so it holds
only JSON values; ``plain()`` refuses anything else before it is written.
Records from ``tenx.domain`` go in as their JSON form.

Two steps running at once may write the same key in one step of the graph.
Lists are joined. Artefacts are joined by name, and a name may be given a
new file only by a later attempt of the step that made it; anything else is
two steps disagreeing, which is a fault, not something to settle by order.
Attempt counts only ever rise. A key with no rule here (``status``, say)
may be written by one step at a time, which the graph library enforces.
"""

from __future__ import annotations

import operator
from typing import Annotated, Literal, TypedDict

from tenx.domain import ArtefactRef

RUNNING, PASSED, FAILED, STOPPED = "running", "passed", "failed", "stopped"
STATUSES = (RUNNING, PASSED, FAILED, STOPPED)


class StateConflict(Exception):
    """Two writes to the state cannot both be right."""


def plain(value, where: str = "update"):
    """Return ``value`` if it is made only of JSON values, or refuse it.

    Floats are refused too: money is kept as decimal text and times as whole
    milliseconds, so a float in the state is a figure that lost its form.
    """
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, list):
        for index, item in enumerate(value):
            plain(item, f"{where}[{index}]")
        return value
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{where} has a key that is not text: {key!r}")
            plain(item, f"{where}.{key}")
        return value
    raise TypeError(
        f"{where} is a {type(value).__name__}, not a JSON value; write records with their dump()"
    )


def merge_artefacts(left: dict | None, right: dict | None) -> dict:
    """Join artefacts by name; a later attempt may replace an earlier one's file."""
    merged = dict(left or {})
    for name, value in (right or {}).items():
        ref = ArtefactRef.load(value)
        if ref.name != name:
            raise StateConflict(f"artefact {name!r} refers to {ref.name!r}")
        old = merged.get(name)
        if old is not None and old != value and ref.attempt <= old["attempt"]:
            raise StateConflict(
                f"artefact {name!r} was given by attempt {old['attempt']} and again, "
                f"differently, by attempt {ref.attempt}; only a later attempt may replace it"
            )
        merged[name] = value
    return merged


def merge_attempts(left: dict | None, right: dict | None) -> dict:
    """Join attempt counts by step; a count may stay or rise, never fall."""
    merged = dict(left or {})
    for step, count in (right or {}).items():
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise StateConflict(f"attempt count for {step!r} must be a whole number, not {count!r}")
        if count < merged.get(step, 0):
            raise StateConflict(
                f"attempt count for {step!r} would fall from {merged[step]} to {count}"
            )
        merged[step] = count
    return merged


class GraphState(TypedDict, total=False):
    """Everything a run knows between steps; large things stay on disk."""

    run_id: str
    request: dict  # EngineeringRequest
    plan: dict  # ExecutionPlan
    budget: dict  # ResourceBudget
    artefacts: Annotated[dict[str, dict], merge_artefacts]  # name -> ArtefactRef
    findings: Annotated[list[dict], operator.add]  # AgentFinding
    validations: Annotated[list[dict], operator.add]  # ValidationResult
    costs: Annotated[list[dict], operator.add]  # CostRecord
    attempts: Annotated[dict[str, int], merge_attempts]  # step -> attempts so far
    status: Literal["running", "passed", "failed", "stopped"]
    reason: str  # why a stopped run stopped, in a sentence
