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
]

DIAGNOSIS_FILE = "DIAGNOSIS.md"

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
