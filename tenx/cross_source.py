"""Read and check the cross-source pass's answer, and write it for the engineer.

The cross-source agent reads the source profiles and the overlap report and
answers, as JSON that Claude Code checks against SCHEMA, how the tables the
goal needs join. Nothing it says is taken on trust here either: the answer
is checked again, and every column it names must exist in the profiles.
A checked answer is written as JOINS.md in the engineer's project folder.
"""

from __future__ import annotations

import re

NOTES_FILE = "JOINS.md"

CARDINALITIES = ("one-to-one", "one-to-many", "many-to-one", "many-to-many")
JOIN_FIELDS = ("left", "right", "prepare", "cardinality", "unmatched", "evidence")

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["joins", "cautions"],
    "properties": {
        "joins": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": list(JOIN_FIELDS),
                "properties": {
                    "left": {"type": "string"},
                    "right": {"type": "string"},
                    "prepare": {"type": "string"},
                    "cardinality": {"enum": list(CARDINALITIES)},
                    "unmatched": {"type": "string"},
                    "evidence": {"type": "string"},
                },
            },
        },
        "cautions": {"type": "array", "items": {"type": "string"}},
    },
}


class NotesError(Exception):
    """The cross-source pass gave no answer the engineer can use."""


def profile_columns(profiles: list[dict]) -> set[str]:
    """Every column of the profiled tables, as source.table.column."""
    return {
        f"{profile['table']}.{column['name']}"
        for profile in profiles
        for column in profile["columns"]
    }


def _text(value, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NotesError(f"{where} is not text")
    return value


def check_answer(answer, columns: set[str]) -> dict:
    """Return the answer if it has the agreed shape and names real columns."""
    if not isinstance(answer, dict):
        raise NotesError("the answer is not a JSON object")
    if set(answer) != {"joins", "cautions"}:
        raise NotesError(f"the answer has fields {sorted(answer)}, not joins and cautions")
    joins, cautions = answer["joins"], answer["cautions"]
    if not isinstance(joins, list) or not isinstance(cautions, list):
        raise NotesError("joins and cautions must be lists")
    for number, join in enumerate(joins, start=1):
        where = f"join {number}"
        if not isinstance(join, dict) or set(join) != set(JOIN_FIELDS):
            raise NotesError(f"{where} does not have the fields {', '.join(JOIN_FIELDS)}")
        for field in JOIN_FIELDS:
            _text(join[field], f"{where} {field}")
        if join["cardinality"] not in CARDINALITIES:
            raise NotesError(
                f"{where} cardinality {join['cardinality']!r} is not one of {', '.join(CARDINALITIES)}"
            )
        for side in ("left", "right"):
            if join[side] not in columns:
                raise NotesError(
                    f"{where} names {join[side]!r}, which is not a column in the profiles"
                )
        if join["left"] == join["right"]:
            raise NotesError(f"{where} joins {join['left']!r} to itself")
    for number, caution in enumerate(cautions, start=1):
        _text(caution, f"caution {number}")
    return answer


def notes_from_report(report: dict | None, profiles: list[dict]) -> dict:
    """The checked answer from Claude Code's JSON report of the pass."""
    if report is None:
        raise NotesError("the cross-source pass gave no report")
    if "structured_output" not in report:
        raise NotesError(
            f"the cross-source pass ended {report.get('subtype') or 'unknown'} without an answer"
        )
    return check_answer(report["structured_output"], profile_columns(profiles))


def _line(text: str) -> str:
    # One list item per field: runs of spaces and line breaks become one space.
    return re.sub(r"\s+", " ", text).strip()


def render(notes: dict) -> str:
    """The notes as the Markdown file the engineer reads."""
    lines = [
        "# How the raw tables join",
        "",
        "Notes from the cross-source pass, worked out from the source profiles and the",
        "overlap report before any model was built.",
    ]
    if not notes["joins"]:
        lines += ["", "No joins were proposed."]
    for join in notes["joins"]:
        lines += [
            "",
            f"## `{join['left']}` to `{join['right']}`",
            "",
            f"- Prepare: {_line(join['prepare'])}",
            f"- Cardinality, left to right: {join['cardinality']}",
            f"- Unmatched: {_line(join['unmatched'])}",
            f"- Evidence: {_line(join['evidence'])}",
        ]
    if notes["cautions"]:
        lines += ["", "## Cautions", ""]
        lines += [f"- {_line(caution)}" for caution in notes["cautions"]]
    return "\n".join(lines) + "\n"
