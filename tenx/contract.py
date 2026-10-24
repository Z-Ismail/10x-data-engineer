"""Check a spec's contract against the source profiles, and turn it into checks.

A contract says, for each model a goal asks for, what one row of it means
(its grain), its key, the raw table its rows come from, the rows it leaves
out and why, where each of its columns comes from, and which money columns
must add up to the raw data. It arrives as JSON in the shape of SCHEMA.

Nothing in it is taken on trust: every table and column it names must exist
in the profiles, and a staging model may take columns only from its own raw
table. A checked contract can be given to the gate as its declaration.
"""

from __future__ import annotations

import re

LAYERS = {"staging": "stg_", "intermediate": "int_"}
RULES = ("where", "duplicate_rows")
MAX_QUESTIONS = 3

CONTRACT_FIELDS = ("models", "assumptions", "questions")
MODEL_FIELDS = ("name", "layer", "grain", "key", "source", "columns", "exclusions", "totals")
COLUMN_FIELDS = ("name", "from", "description")
EXCLUSION_FIELDS = ("name", "reason", "rule", "where")
TOTAL_FIELDS = ("column", "source_column", "by", "source_by")

NAME = re.compile(r"^[a-z][a-z0-9_]*$")


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


def _object(value, fields: tuple[str, ...], where: str) -> dict:
    if not isinstance(value, dict):
        raise ContractError(f"{where} is not a JSON object")
    if set(value) != set(fields):
        raise ContractError(f"{where} has fields {sorted(value)}, not {', '.join(fields)}")
    return value


def _list(value, where: str) -> list:
    if not isinstance(value, list):
        raise ContractError(f"{where} is not a list")
    return value


def _text(value, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{where} is not text")
    return value


def _name(value, where: str) -> str:
    if not isinstance(value, str) or not NAME.match(value):
        raise ContractError(f"{where} {value!r} is not a name in lower case letters, digits and _")
    return value


def _unique(names: list[str], what: str) -> None:
    seen: set[str] = set()
    for name in names:
        if name in seen:
            raise ContractError(f"{what} {name} is named twice")
        seen.add(name)


def profile_columns(profiles: list[dict]) -> dict[str, set[str]]:
    """The columns of each profiled table, by table name such as billing.customers."""
    return {
        profile["table"]: {column["name"] for column in profile["columns"]} for profile in profiles
    }


def _check_columns(model: dict, tables: dict[str, set[str]], where: str) -> set[str]:
    listed = _list(model["columns"], f"{where} columns")
    if not listed:
        raise ContractError(f"{where} lists no columns")
    names = []
    for number, column in enumerate(listed, start=1):
        place = f"{where} column {number}"
        _object(column, COLUMN_FIELDS, place)
        name = _name(column["name"], place)
        place = f"{where} column {name}"
        _text(column["description"], f"{place} description")
        origins = _list(column["from"], f"{place} from")
        if not origins:
            raise ContractError(f"{place} does not say which raw column it comes from")
        for origin in origins:
            table, _, raw_column = _text(origin, f"{place} from").rpartition(".")
            if raw_column not in tables.get(table, set()):
                raise ContractError(f"{place} comes from {origin}, which is not a profiled column")
            if model["layer"] == "staging" and table != model["source"]:
                raise ContractError(
                    f"{place} comes from {origin}; a staging model takes columns "
                    f"only from its own source, {model['source']}"
                )
        names.append(name)
    _unique(names, f"{where}: column")
    return set(names)


def _check_exclusions(model: dict, where: str) -> None:
    listed = _list(model["exclusions"], f"{where} exclusions")
    names = []
    for number, exclusion in enumerate(listed, start=1):
        place = f"{where} exclusion {number}"
        _object(exclusion, EXCLUSION_FIELDS, place)
        name = _name(exclusion["name"], place)
        place = f"{where} exclusion {name}"
        _text(exclusion["reason"], f"{place} reason")
        if exclusion["rule"] not in RULES:
            raise ContractError(
                f"{place} rule {exclusion['rule']!r} is not where or duplicate_rows"
            )
        if not isinstance(exclusion["where"], str):
            raise ContractError(f"{place} where is not text")
        if exclusion["rule"] == "where":
            _text(exclusion["where"], f"{place} where")
        elif exclusion["where"].strip():
            raise ContractError(
                f"{place} removes duplicate rows and must not give a where condition"
            )
        names.append(name)
    _unique(names, f"{where}: exclusion")
    if sum(exclusion["rule"] == "duplicate_rows" for exclusion in listed) > 1:
        raise ContractError(f"{where} removes duplicate rows more than once")


def _check_totals(model: dict, columns: set[str], raw: set[str], where: str) -> None:
    listed = _list(model["totals"], f"{where} totals")
    totalled = []
    for number, total in enumerate(listed, start=1):
        place = f"{where} total {number}"
        _object(total, TOTAL_FIELDS, place)
        for field in TOTAL_FIELDS:
            if not isinstance(total[field], str):
                raise ContractError(f"{place} {field} is not text")
        if total["column"] not in columns:
            raise ContractError(
                f"{place} adds up {total['column']!r}, which is not a column of the model"
            )
        if total["source_column"] not in raw:
            raise ContractError(
                f"{place} ties to {total['source_column']!r}, which is not a column of {model['source']}"
            )
        if bool(total["by"]) != bool(total["source_by"]):
            raise ContractError(f"{place} must give both by and source_by, or neither")
        if total["by"] and total["by"] not in columns:
            raise ContractError(
                f"{place} groups by {total['by']!r}, which is not a column of the model"
            )
        if total["source_by"] and total["source_by"] not in raw:
            raise ContractError(
                f"{place} groups the source by {total['source_by']!r}, "
                f"which is not a column of {model['source']}"
            )
        totalled.append(total["column"])
    _unique(totalled, f"{where}: totalled column")


def _check_model(model, number: int, tables: dict[str, set[str]]) -> None:
    _object(model, MODEL_FIELDS, f"model {number}")
    name = _name(model["name"], f"model {number} name")
    where = f"model {name}"
    prefix = LAYERS.get(model["layer"])
    if prefix is None:
        raise ContractError(f"{where} layer {model['layer']!r} is not staging or intermediate")
    if not name.startswith(prefix):
        raise ContractError(
            f"{where} is in the {model['layer']} layer, so its name starts {prefix}"
        )
    _text(model["grain"], f"{where} grain")
    if not isinstance(model["source"], str) or model["source"] not in tables:
        raise ContractError(f"{where} source {model['source']!r} is not a profiled table")
    columns = _check_columns(model, tables, where)
    key = _list(model["key"], f"{where} key")
    if not key:
        raise ContractError(f"{where} has no key")
    for column in key:
        if column not in columns:
            raise ContractError(f"{where} key {column!r} is not one of its columns")
    _unique(key, f"{where}: key column")
    _check_exclusions(model, where)
    _check_totals(model, columns, tables[model["source"]], where)


def check_contract(contract, profiles: list[dict]) -> dict:
    """Return the contract if it has the agreed shape and names only what exists."""
    _object(contract, CONTRACT_FIELDS, "the contract")
    models = _list(contract["models"], "models")
    if not models:
        raise ContractError("the contract lists no models")
    tables = profile_columns(profiles)
    for number, model in enumerate(models, start=1):
        _check_model(model, number, tables)
    _unique([model["name"] for model in models], "model")
    for field in ("assumptions", "questions"):
        for number, item in enumerate(_list(contract[field], field), start=1):
            _text(item, f"{field} {number}")
    if len(contract["questions"]) > MAX_QUESTIONS:
        raise ContractError(
            f"the contract asks {len(contract['questions'])} questions; at most {MAX_QUESTIONS}"
        )
    return contract


def to_declaration(contract: dict) -> dict:
    """The gate's declaration for a checked contract.

    The gate checks each model's rows against its source less the exclusions,
    its key, and its money totals. Grain sentences, columns and reasons are
    for people and the engineer; the gate does not read them.
    """
    models = []
    for model in contract["models"]:
        declared = {"name": model["name"], "key": list(model["key"]), "source": model["source"]}
        exclusions = [
            {"name": exclusion["name"], "where": exclusion["where"]}
            if exclusion["rule"] == "where"
            else {"name": exclusion["name"], "duplicate_rows": True}
            for exclusion in model["exclusions"]
        ]
        if exclusions:
            declared["exclusions"] = exclusions
        totals = [
            {field: total[field] for field in TOTAL_FIELDS if total[field]}
            for total in model["totals"]
        ]
        if totals:
            declared["totals"] = totals
        models.append(declared)
    return {"models": models}
