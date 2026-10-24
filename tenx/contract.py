"""The contract of a spec: what each model must be, as JSON in the shape of SCHEMA.

A contract says, for each model a goal asks for, what one row of it means
(its grain), its key, the raw table its rows come from, the rows it leaves
out and why, where each of its columns comes from, and which money columns
must add up to the raw data.
"""

from __future__ import annotations

LAYERS = {"staging": "stg_", "intermediate": "int_"}
RULES = ("where", "duplicate_rows")
MAX_QUESTIONS = 3

CONTRACT_FIELDS = ("models", "assumptions", "questions")
MODEL_FIELDS = ("name", "layer", "grain", "key", "source", "columns", "exclusions", "totals")
COLUMN_FIELDS = ("name", "from", "description")
EXCLUSION_FIELDS = ("name", "reason", "rule", "where")
TOTAL_FIELDS = ("column", "source_column", "by", "source_by")


def _strict(properties: dict) -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
    }


TEXT = {"type": "string"}
TEXTS = {"type": "array", "items": TEXT}

SCHEMA = _strict(
    {
        "models": {
            "type": "array",
            "items": _strict(
                {
                    "name": TEXT,
                    "layer": {"enum": list(LAYERS)},
                    "grain": TEXT,
                    "key": TEXTS,
                    "source": TEXT,
                    "columns": {
                        "type": "array",
                        "items": _strict({"name": TEXT, "from": TEXTS, "description": TEXT}),
                    },
                    "exclusions": {
                        "type": "array",
                        "items": _strict(
                            {
                                "name": TEXT,
                                "reason": TEXT,
                                "rule": {"enum": list(RULES)},
                                "where": TEXT,
                            }
                        ),
                    },
                    "totals": {
                        "type": "array",
                        "items": _strict({field: TEXT for field in TOTAL_FIELDS}),
                    },
                }
            ),
        },
        "assumptions": TEXTS,
        "questions": TEXTS,
    }
)


class ContractError(Exception):
    """The contract cannot be used: it is malformed or names what does not exist."""
