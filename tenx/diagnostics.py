"""Read and check a diagnostician's answer about a project the validator failed.

The diagnostician reads the diagnosis packet (``tenx.diagnosis``) and
answers, as JSON that Claude Code checks against SCHEMA, why the project
fails: confirmed facts, causes ranked from the most likely, and what it
could not find out. Nothing it says is taken on trust here either. Each
fact quotes a packet file, and the quote must be in that file; each cause
names one kind from CAUSES, a model the project defines, a project file
and a line that file has, and the checks it explains, each a check the
validator failed or could not run. Probable causes come before
hypotheses, so the first cause is the diagnosis; an answer may name no
cause if it says what is missing. The answer is advice; it changes no file
and no verdict.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from tenx.ports import ValidationReport
from tenx.reviewer import models_in

__all__ = [
    "ANSWER_FIELDS",
    "CAUSES",
    "DIAGNOSIS_FILE",
    "HYPOTHESIS",
    "KINDS",
    "LIKELIHOODS",
    "MAX_CAUSES",
    "MIN_QUOTE",
    "OTHER",
    "PROBABLE",
    "SCHEMA",
    "DiagnosisError",
    "check_answer",
]

DIAGNOSIS_FILE = "DIAGNOSIS.md"
PROJECT_PREFIX = "project/"

# Every kind of cause a diagnosis may name, with what it means. A fixed list,
# so that two wordings of one cause count as the same.
CAUSES = {
    "match rule": "records of two sources are matched on the wrong value",
    "join type": "a join drops the rows that have no match",
    "row filter": "rows are left out or kept against the goal",
    "deduplication": "rows are merged or repeated against the goal",
    "type": "a value is read as the wrong type",
    "unit or sign": "amounts are scaled or their sign is changed",
    "nondeterminism": "a value changes from one build to the next",
    "missing column": "a model names a column its source does not have",
}
# For a cause that fits none of the list, described in the claim.
OTHER = "other"
KINDS = (*CAUSES, OTHER)
PROBABLE, HYPOTHESIS = "probable", "hypothesis"
LIKELIHOODS = (PROBABLE, HYPOTHESIS)
MAX_CAUSES = 5
# A fact must quote at least this many characters, so it cannot rest on a word.
MIN_QUOTE = 8

ANSWER_FIELDS = ("summary", "facts", "causes", "missing")
FACT_FIELDS = ("claim", "file", "quote")
CAUSE_FIELDS = ("likelihood", "cause", "model", "file", "line", "claim", "evidence", "explains")
CHECK_FIELDS = ("check", "subject")

_TEXT = {"type": "string"}
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": list(ANSWER_FIELDS),
    "properties": {
        "summary": _TEXT,
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": list(FACT_FIELDS),
                "properties": {"claim": _TEXT, "file": _TEXT, "quote": _TEXT},
            },
        },
        "causes": {
            "type": "array",
            "maxItems": MAX_CAUSES,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": list(CAUSE_FIELDS),
                "properties": {
                    "likelihood": {"enum": list(LIKELIHOODS)},
                    "cause": {"enum": list(KINDS)},
                    "model": _TEXT,
                    "file": _TEXT,
                    "line": {"type": "integer", "minimum": 1},
                    "claim": _TEXT,
                    "evidence": _TEXT,
                    "explains": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": list(CHECK_FIELDS),
                            "properties": {"check": _TEXT, "subject": _TEXT},
                        },
                    },
                },
            },
        },
        "missing": {"type": "array", "items": _TEXT},
    },
}


class DiagnosisError(Exception):
    """The diagnostician gave no answer that can be used, and why, in a sentence."""


def _text(value, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DiagnosisError(f"{where} is not text")
    return value


def _list(value, where: str) -> list:
    if not isinstance(value, list):
        raise DiagnosisError(f"{where} must be a list")
    return value


def _fields(item, fields: tuple[str, ...], where: str) -> None:
    if not isinstance(item, dict) or set(item) != set(fields):
        raise DiagnosisError(f"{where} does not have the fields {', '.join(fields)}")


def _flat(text: str) -> str:
    # Runs of spaces and line breaks count as one space.
    return re.sub(r"\s+", " ", text).strip()


def _check_fact(fact, where: str, files: Mapping[str, str]) -> None:
    _fields(fact, FACT_FIELDS, where)
    for field in FACT_FIELDS:
        _text(fact[field], f"{where} {field}")
    if fact["file"] not in files:
        raise DiagnosisError(f"{where} cites {fact['file']!r}, which is not a file in the packet")
    quote = _flat(fact["quote"])
    if len(quote) < MIN_QUOTE:
        raise DiagnosisError(f"{where} quotes fewer than {MIN_QUOTE} characters")
    if quote not in _flat(files[fact["file"]]):
        raise DiagnosisError(f"{where} quotes text that {fact['file']} does not have")


def _check_cause(cause, where: str, files: Mapping[str, str], models: dict, open_: set) -> None:
    _fields(cause, CAUSE_FIELDS, where)
    if cause["likelihood"] not in LIKELIHOODS:
        raise DiagnosisError(
            f"{where} likelihood {cause['likelihood']!r} is not one of {', '.join(LIKELIHOODS)}"
        )
    if cause["cause"] not in KINDS:
        raise DiagnosisError(f"{where} cause {cause['cause']!r} is not one of the listed causes")
    for field in ("model", "file", "claim", "evidence"):
        _text(cause[field], f"{where} {field}")
    model, file, line = cause["model"], cause["file"], cause["line"]
    if model not in models:
        raise DiagnosisError(f"{where} names model {model!r}, which the project does not define")
    if not file.startswith(PROJECT_PREFIX) or file not in files:
        raise DiagnosisError(f"{where} cites {file!r}, which is not a project file in the packet")
    if isinstance(line, bool) or not isinstance(line, int):
        raise DiagnosisError(f"{where} line is not a whole number")
    count = len(files[file].splitlines())
    if not 1 <= line <= count:
        lines = f"{count} line{'' if count == 1 else 's'}"
        raise DiagnosisError(f"{where} cites line {line} of {file}, which has {lines}")
    explains = _list(cause["explains"], f"{where} explains")
    if not explains:
        raise DiagnosisError(f"{where} explains no check")
    seen = set()
    for item in explains:
        _fields(item, CHECK_FIELDS, f"{where} explains an item that")
        named = (item["check"], item["subject"])
        if named not in open_:
            raise DiagnosisError(
                f"{where} explains {named[0]} on {named[1]}, which did not fail or go unrun"
            )
        if named in seen:
            raise DiagnosisError(f"{where} explains {named[0]} on {named[1]} twice")
        seen.add(named)


def check_answer(answer, files: Mapping[str, str], report: ValidationReport) -> dict:
    """Return the answer if it has the agreed shape and everything it cites is there.

    ``files`` maps each file in the packet, by its path in the packet, to its
    text; ``report`` is the validation the packet was made from.
    """
    if not isinstance(answer, dict):
        raise DiagnosisError("the answer is not a JSON object")
    if set(answer) != set(ANSWER_FIELDS):
        raise DiagnosisError(
            f"the answer has fields {sorted(answer)}, not {', '.join(ANSWER_FIELDS)}"
        )
    _text(answer["summary"], "the summary")
    for number, fact in enumerate(_list(answer["facts"], "facts"), start=1):
        _check_fact(fact, f"fact {number}", files)
    causes = _list(answer["causes"], "causes")
    if len(causes) > MAX_CAUSES:
        raise DiagnosisError(f"the answer gives {len(causes)} causes, more than {MAX_CAUSES}")
    project = {
        name[len(PROJECT_PREFIX) :]: text
        for name, text in files.items()
        if name.startswith(PROJECT_PREFIX)
    }
    models = models_in(project)
    open_ = {(r.check, r.subject) for r in report.results if r.status != "pass"}
    for number, cause in enumerate(causes, start=1):
        _check_cause(cause, f"cause {number}", files, models, open_)
    likelihoods = [cause["likelihood"] for cause in causes]
    if HYPOTHESIS in likelihoods and PROBABLE in likelihoods[likelihoods.index(HYPOTHESIS) :]:
        raise DiagnosisError("a probable cause comes after a hypothesis")
    missing = _list(answer["missing"], "missing")
    for number, item in enumerate(missing, start=1):
        _text(item, f"missing item {number}")
    if not causes and not missing:
        raise DiagnosisError("the answer names no cause and says nothing is missing")
    return answer
