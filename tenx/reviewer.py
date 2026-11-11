"""Read and check the reviewer's answer about a project the validator passed.

The reviewer agent reads the review packet (``tenx.review``) and answers, as
JSON that Claude Code checks against SCHEMA, whether the project should
change and why. Nothing it says is taken on trust here either: every
finding must point at a project file in the packet, at a line that file
has, and at a model and column that file defines, and the verdict must
follow from the findings. The answer is advice; it never changes the
validator's verdict.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import PurePosixPath

__all__ = [
    "APPROVE",
    "BLOCKING",
    "REQUEST_CHANGES",
    "SCHEMA",
    "SEVERITIES",
    "VERDICTS",
    "ReviewError",
    "check_answer",
    "models_in",
    "review_from_report",
]

APPROVE, REQUEST_CHANGES = "approve", "request changes"
VERDICTS = (APPROVE, REQUEST_CHANGES)
BLOCKING, ADVISORY = "blocking", "advisory"
SEVERITIES = (BLOCKING, ADVISORY)
FINDING_FIELDS = ("severity", "model", "column", "file", "line", "claim", "evidence")
ANSWER_FIELDS = ("verdict", "summary", "findings")
MODEL_SUFFIXES = (".sql", ".py")

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": list(ANSWER_FIELDS),
    "properties": {
        "verdict": {"enum": list(VERDICTS)},
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": list(FINDING_FIELDS),
                "properties": {
                    "severity": {"enum": list(SEVERITIES)},
                    "model": {"type": "string"},
                    "column": {"type": "string"},
                    "file": {"type": "string"},
                    "line": {"type": "integer", "minimum": 1},
                    "claim": {"type": "string"},
                    "evidence": {"type": "string"},
                },
            },
        },
    },
}


class ReviewError(Exception):
    """The reviewer gave no answer that can be used, and why, in a sentence."""


def models_in(files: Mapping[str, str]) -> dict[str, str]:
    """Each model the project defines, by name, with the file it is defined in.

    ``files`` maps each project file's path inside the project to its text.
    """
    found = {}
    for name in sorted(files):
        path = PurePosixPath(name)
        if path.parts[:1] == ("models",) and path.suffix.lower() in MODEL_SUFFIXES:
            found.setdefault(path.stem, name)
    return found


def _text(value, where: str, empty: bool = False) -> str:
    if not isinstance(value, str) or not (empty or value.strip()):
        raise ReviewError(f"{where} is not text")
    return value


def _names(text: str, column: str) -> bool:
    # The column as a whole word, in any case, quoted or not.
    return re.search(rf"(?<![\w$]){re.escape(column)}(?![\w$])", text, re.IGNORECASE) is not None


def _check_finding(finding, where: str, files: Mapping[str, str], models: dict) -> None:
    if not isinstance(finding, dict) or set(finding) != set(FINDING_FIELDS):
        raise ReviewError(f"{where} does not have the fields {', '.join(FINDING_FIELDS)}")
    if finding["severity"] not in SEVERITIES:
        raise ReviewError(
            f"{where} severity {finding['severity']!r} is not one of {', '.join(SEVERITIES)}"
        )
    for field in ("model", "file", "claim", "evidence"):
        _text(finding[field], f"{where} {field}")
    column = _text(finding["column"], f"{where} column", empty=True).strip()
    model, file, line = finding["model"], finding["file"], finding["line"]
    if model not in models:
        raise ReviewError(f"{where} names model {model!r}, which the project does not define")
    if file not in files:
        raise ReviewError(f"{where} cites {file!r}, which is not a project file in the packet")
    if isinstance(line, bool) or not isinstance(line, int):
        raise ReviewError(f"{where} line is not a whole number")
    count = len(files[file].splitlines())
    if not 1 <= line <= count:
        raise ReviewError(f"{where} cites line {line} of {file}, which has {count} lines")
    if column and not _names(files[file], column):
        raise ReviewError(f"{where} names column {column!r}, which {file} does not mention")


def check_answer(answer, files: Mapping[str, str]) -> dict:
    """Return the answer if it has the agreed shape and every finding points at the project.

    ``files`` maps each project file in the packet, by its path inside the
    project, to its text.
    """
    if not isinstance(answer, dict):
        raise ReviewError("the answer is not a JSON object")
    if set(answer) != set(ANSWER_FIELDS):
        raise ReviewError(f"the answer has fields {sorted(answer)}, not {', '.join(ANSWER_FIELDS)}")
    if answer["verdict"] not in VERDICTS:
        raise ReviewError(f"verdict {answer['verdict']!r} is not one of {', '.join(VERDICTS)}")
    _text(answer["summary"], "the summary")
    findings = answer["findings"]
    if not isinstance(findings, list):
        raise ReviewError("findings must be a list")
    models = models_in(files)
    for number, finding in enumerate(findings, start=1):
        _check_finding(finding, f"finding {number}", files, models)
    blocking = any(finding["severity"] == BLOCKING for finding in findings)
    if blocking != (answer["verdict"] == REQUEST_CHANGES):
        said = "blocking findings" if blocking else "no blocking finding"
        raise ReviewError(f"the verdict is {answer['verdict']!r} with {said}")
    return answer


def review_from_report(report: dict | None, files: Mapping[str, str]) -> dict:
    """The checked answer from Claude Code's JSON report of the review."""
    if report is None:
        raise ReviewError("the reviewer gave no report")
    if "structured_output" not in report:
        raise ReviewError(
            f"the reviewer ended {report.get('subtype') or 'unknown'} without an answer"
        )
    return check_answer(report["structured_output"], files)
