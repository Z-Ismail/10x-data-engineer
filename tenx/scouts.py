"""Read and check each scout's answer.

A scout looks at one raw table, from its profile and a sample of its rows,
and answers, as JSON that Claude Code checks against SCHEMA, what the
engineer must handle in that table. Nothing it says is taken on trust here
either: the answer is checked again, and every column it names must be a
column of its table.
"""

from __future__ import annotations

FINDING_FIELDS = ("columns", "issue", "evidence", "handling")

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["findings", "questions"],
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": list(FINDING_FIELDS),
                "properties": {
                    "columns": {"type": "array", "items": {"type": "string"}},
                    "issue": {"type": "string"},
                    "evidence": {"type": "string"},
                    "handling": {"type": "string"},
                },
            },
        },
        "questions": {"type": "array", "items": {"type": "string"}},
    },
}


class ScoutError(Exception):
    """A scout gave no answer the engineer can use."""


def _text(value, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ScoutError(f"{where} is not text")
    return value


def check_answer(answer, columns: set[str]) -> dict:
    """Return the answer if it has the agreed shape and names only these columns."""
    if not isinstance(answer, dict):
        raise ScoutError("the answer is not a JSON object")
    if set(answer) != {"findings", "questions"}:
        raise ScoutError(f"the answer has fields {sorted(answer)}, not findings and questions")
    findings, questions = answer["findings"], answer["questions"]
    if not isinstance(findings, list) or not isinstance(questions, list):
        raise ScoutError("findings and questions must be lists")
    for number, finding in enumerate(findings, start=1):
        where = f"finding {number}"
        if not isinstance(finding, dict) or set(finding) != set(FINDING_FIELDS):
            raise ScoutError(f"{where} does not have the fields {', '.join(FINDING_FIELDS)}")
        for field in FINDING_FIELDS[1:]:
            _text(finding[field], f"{where} {field}")
        named = finding["columns"]
        if not isinstance(named, list) or not all(isinstance(name, str) for name in named):
            raise ScoutError(f"{where} columns must be a list of column names")
        for name in named:
            if name not in columns:
                raise ScoutError(f"{where} names {name!r}, which is not a column of the table")
        if len(set(named)) != len(named):
            raise ScoutError(f"{where} names a column more than once")
    for number, question in enumerate(questions, start=1):
        _text(question, f"question {number}")
    return answer


def notes_from_report(report: dict | None, profile: dict) -> dict:
    """The checked answer from Claude Code's JSON report of one scout."""
    if report is None:
        raise ScoutError("the scout gave no report")
    if "structured_output" not in report:
        raise ScoutError(f"the scout ended {report.get('subtype') or 'unknown'} without an answer")
    columns = {column["name"] for column in profile["columns"]}
    return check_answer(report["structured_output"], columns)
