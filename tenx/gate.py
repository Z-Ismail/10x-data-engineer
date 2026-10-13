"""Check a dbt project against the raw files it was built from.

A dbt project can build cleanly and still be wrong, so the gate does not take
a green build as proof. It builds the project itself into an empty database
and then asks its own questions of the result:

    build  ``dbt build`` succeeds and every expected model was produced
    rows   each model holds the rows of its source, less the rows that were
           declared as excluded, and not one more or fewer
    key    each model's declared key has no nulls and no repeated values
    total  each declared money column adds up to exactly the raw total, less
           what the exclusions removed, for each value of a column such as
           the currency

What to expect is declared in a JSON file kept outside the dbt project, so
that whoever writes the models cannot relax a check to make it pass:

    {
      "models": [
        {
          "name": "stg_billing__invoices",
          "key": ["invoice_id"],
          "source": "billing.invoices",
          "exclusions": [
            {"name": "test_mode", "where": "livemode = 'false'"}
          ],
          "totals": [
            {"column": "amount_minor", "by": "currency"}
          ]
        }
      ]
    }

``source`` names a raw file the way the profiler does: ``billing/invoices.csv``
is ``billing.invoices``. A ``where`` exclusion is a SQL condition on the raw
file; CSV columns are read as text, as they are by the profiler. An exclusion
written as ``{"name": ..., "duplicate_rows": true}`` removes exact repeats of a
row. Rows are only ever removed by a declared exclusion, and the report counts
what each one removed.

A total names a model column and, if it is named differently in the raw file,
``source_column``; likewise ``by`` and ``source_by`` for the grouping column.
Group values are compared in capitals with surrounding spaces removed, so
``usd`` in the raw file and ``USD`` in the model are the same currency.
The model column must hold the amount in the same unit as the raw file, as an
integer or DECIMAL. Raw amounts must be plain decimal numbers and are added
without rounding.

The project reads the raw files from the folder passed to dbt as the variable
``raw_dir``.

Usage:
    python -m tenx.gate path/to/project --raw demo/data --checks gate.json

Exit status is 0 when every check passes, 1 when any check fails and 2 when
the gate could not run.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import duckdb

from tenx.profile import ProfileError, discover, quote_identifier, relation, table_name

PASS, FAIL, SKIP = "pass", "fail", "skip"
# Statuses dbt records for a node that did what was asked. A test set to warn
# does not stop a dbt build, so it does not stop the gate either.
ACCEPTED_STATUSES = {"success", "pass", "warn"}
PROFILE_NAME = "tenx_gate"
DATABASE_FILE = "build.duckdb"
OUTPUT_TAIL = 15
EXAMPLES = 3
MODEL_FIELDS = {"name", "key", "source", "exclusions", "totals"}
EXCLUSION_FIELDS = {"name", "where", "duplicate_rows"}
TOTAL_FIELDS = {"column", "source_column", "by", "source_by"}
# Raw amounts are read from text, and only plain decimal numbers are accepted:
# an optional sign, digits and an optional decimal point. Anything else, such
# as `1e3`, `12,50` or `n/a`, is not taken as an amount.
PLAIN_NUMBER = r"^[+-]?([0-9]+([.][0-9]*)?|[.][0-9]+)$"
DECIMAL_DIGITS = 38
# The group of a total that is not split by any column.
ALL = "all"
INTEGER_TYPES = {
    "TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT",
    "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT", "UHUGEINT",
}  # fmt: skip
FLOAT_TYPES = {"FLOAT", "REAL", "DOUBLE"}


class GateError(Exception):
    """Raised when the gate cannot run, as opposed to a check failing."""


@dataclass(frozen=True)
class Exclusion:
    """A population of raw rows that is left out on purpose."""

    name: str
    where: str | None = None
    duplicate_rows: bool = False


@dataclass(frozen=True)
class Total:
    """A money column whose total must tie to the same column in the source.

    The model column holds the amount in the same unit as the raw column.
    With ``by``, totals are compared separately for each value of that
    column, such as each currency.
    """

    column: str
    source_column: str
    by: str | None = None
    source_by: str | None = None


@dataclass(frozen=True)
class ModelSpec:
    name: str
    key: tuple[str, ...]
    source: str | None = None
    exclusions: tuple[Exclusion, ...] = ()
    totals: tuple[Total, ...] = ()


@dataclass(frozen=True)
class RowExpectation:
    """How many rows a model must hold, worked out from the raw file alone."""

    source: str
    source_rows: int
    excluded: tuple[tuple[str, int], ...]
    expected_rows: int


@dataclass(frozen=True)
class TotalExpectation:
    """What a money column must add up to, worked out from the raw file alone.

    Each mapping goes from a value of the grouping column, as text in
    capitals, to an amount. ``expected`` is the raw total less every
    exclusion, group by group.
    """

    total: Total
    source_totals: dict[str | None, Decimal]
    excluded: tuple[tuple[str, dict[str | None, Decimal]], ...]
    expected: dict[str | None, Decimal]
    unreadable: int = 0
    unreadable_examples: tuple[str, ...] = ()


@dataclass
class Result:
    check: str
    subject: str
    status: str
    detail: str
    facts: dict = field(default_factory=dict)


def counted(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise GateError(message)


def _parse_exclusion(raw: object, model: str) -> Exclusion:
    _require(isinstance(raw, dict), f"{model}: each exclusion must be an object")
    unknown = sorted(set(raw) - EXCLUSION_FIELDS)
    _require(not unknown, f"{model}: unknown exclusion field {', '.join(unknown)}")
    name = raw.get("name")
    _require(isinstance(name, str) and bool(name.strip()), f"{model}: an exclusion needs a name")
    where = raw.get("where")
    duplicate_rows = raw.get("duplicate_rows", False)
    _require(
        isinstance(duplicate_rows, bool),
        f"{model}: exclusion {name}: duplicate_rows must be true or false",
    )
    _require(
        where is None or (isinstance(where, str) and bool(where.strip())),
        f"{model}: exclusion {name}: where must be a SQL condition",
    )
    _require(
        (where is not None) != duplicate_rows,
        f"{model}: exclusion {name} needs either a where condition or duplicate_rows",
    )
    return Exclusion(name=name, where=where, duplicate_rows=duplicate_rows)


def _column_name(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _parse_total(raw: object, model: str) -> Total:
    _require(isinstance(raw, dict), f"{model}: each total must be an object")
    unknown = sorted(set(raw) - TOTAL_FIELDS)
    _require(not unknown, f"{model}: unknown total field {', '.join(unknown)}")
    column = raw.get("column")
    _require(_column_name(column), f"{model}: a total needs the column to add up")
    for name in ("source_column", "by", "source_by"):
        _require(
            raw.get(name) is None or _column_name(raw[name]),
            f"{model}: total of {column}: {name} must be a column name",
        )
    _require(
        raw.get("source_by") is None or raw.get("by") is not None,
        f"{model}: total of {column}: source_by is given without by",
    )
    by = raw.get("by")
    return Total(
        column=column,
        source_column=raw.get("source_column") or column,
        by=by,
        source_by=raw.get("source_by") or by,
    )


def _parse_model(raw: object) -> ModelSpec:
    _require(isinstance(raw, dict), "each model must be an object")
    name = raw.get("name")
    _require(isinstance(name, str) and bool(name.strip()), "every model needs a name")
    unknown = sorted(set(raw) - MODEL_FIELDS)
    _require(not unknown, f"{name}: unknown field {', '.join(unknown)}")
    key = raw.get("key")
    _require(
        isinstance(key, list)
        and bool(key)
        and all(isinstance(column, str) and column.strip() for column in key),
        f"{name}: key must be a list of one or more column names",
    )
    _require(
        len({column.lower() for column in key}) == len(key),
        f"{name}: key names the same column twice",
    )
    source = raw.get("source")
    _require(
        source is None or (isinstance(source, str) and bool(source.strip())),
        f"{name}: source must be the name of a raw table",
    )
    listed = raw.get("exclusions", [])
    _require(isinstance(listed, list), f"{name}: exclusions must be a list")
    _require(
        not listed or source is not None,
        f"{name}: exclusions are counted against a source, and none is declared",
    )
    exclusions = tuple(_parse_exclusion(item, name) for item in listed)
    names = [exclusion.name for exclusion in exclusions]
    _require(len(set(names)) == len(names), f"{name}: two exclusions share a name")
    _require(
        sum(exclusion.duplicate_rows for exclusion in exclusions) <= 1,
        f"{name}: duplicate_rows can be declared once",
    )
    listed = raw.get("totals", [])
    _require(isinstance(listed, list), f"{name}: totals must be a list")
    _require(
        not listed or source is not None,
        f"{name}: totals are compared with a source, and none is declared",
    )
    totals = tuple(_parse_total(item, name) for item in listed)
    columns = [total.column.lower() for total in totals]
    _require(len(set(columns)) == len(columns), f"{name}: a column is totalled twice")
    return ModelSpec(name=name, key=tuple(key), source=source, exclusions=exclusions, totals=totals)


def load_declaration(path: Path) -> list[ModelSpec]:
    """Read and validate the file that says what the gate should expect.

    Validation is strict. A misspelt field would otherwise be ignored, and the
    check it was meant to configure would silently not run.
    """
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as error:
        raise GateError(f"could not read {path}: {error.strerror or error}") from error
    except json.JSONDecodeError as error:
        raise GateError(f"{path} is not valid JSON: {error}") from error
    _require(isinstance(payload, dict), f"{path} must hold a JSON object")
    unknown = sorted(set(payload) - {"models"})
    _require(not unknown, f"{path}: unknown field {', '.join(unknown)}")
    listed = payload.get("models")
    _require(isinstance(listed, list) and bool(listed), f"{path} must list at least one model")
    try:
        models = [_parse_model(item) for item in listed]
    except GateError as error:
        raise GateError(f"{path}: {error}") from error
    names = [model.name for model in models]
    repeated = sorted({name for name in names if names.count(name) > 1})
    _require(not repeated, f"{path}: {', '.join(repeated)} is declared more than once")
    return models


def source_files(raw: Path) -> dict[str, Path]:
    """Raw files by table name, named the way the profiler names them."""
    try:
        return {table_name(path, raw): path for path in discover(raw)}
    except ProfileError as error:
        raise GateError(str(error)) from error


def claimed_by_filters(model: ModelSpec) -> tuple[list[tuple[str, str]], str]:
    """SQL conditions that split the raw rows between the declared filters.

    Filters are applied in the order declared and a row is charged to the
    first one that matches it, so a row matched by two filters is removed
    once. A condition that is neither true nor false does not match.

    Returns each filter's name with the condition for the rows charged to
    it, and the condition for the rows that no filter removes.
    """
    claimed: list[tuple[str, str]] = []
    earlier: list[str] = []
    for exclusion in model.exclusions:
        if exclusion.where is None:
            continue
        condition = f"coalesce(({exclusion.where}), false)"
        claimed.append((exclusion.name, " AND ".join([*(f"NOT {e}" for e in earlier), condition])))
        earlier.append(condition)
    kept = " AND ".join(f"NOT {condition}" for condition in earlier) or "true"
    return claimed, kept


def expected_rows(con: duckdb.DuckDBPyConnection, model: ModelSpec, path: Path) -> RowExpectation:
    """Count the raw rows and what each declared exclusion removes from them.

    A row matched by two filters is charged to the first and removed once.
    Exact repeats are counted among the rows that the filters leave.
    """
    source = relation(path)
    excluded: dict[str, int] = {}
    claimed, kept_condition = claimed_by_filters(model)
    try:
        source_rows = con.execute(f"SELECT count(*) FROM {source}").fetchone()[0]
        for name, where in claimed:
            (excluded[name],) = con.execute(
                f"SELECT count(*) FROM {source} WHERE {where}"
            ).fetchone()
        kept = f"SELECT * FROM {source} WHERE {kept_condition}"
        kept_rows, distinct_rows = con.execute(
            f"SELECT (SELECT count(*) FROM ({kept})), "
            f"(SELECT count(*) FROM (SELECT DISTINCT * FROM ({kept})))"
        ).fetchone()
    except duckdb.Error as error:
        raise GateError(
            f"{model.name}: could not count the rows of {model.source}: {error}"
        ) from error
    expected = kept_rows
    for exclusion in model.exclusions:
        if exclusion.duplicate_rows:
            excluded[exclusion.name] = kept_rows - distinct_rows
            expected = distinct_rows
    return RowExpectation(
        source=model.source,
        source_rows=source_rows,
        excluded=tuple(
            (exclusion.name, excluded[exclusion.name]) for exclusion in model.exclusions
        ),
        expected_rows=expected,
    )


def _group(column: str | None) -> str:
    """SQL for the group a row's amount is added to.

    Codes such as currencies are written in capitals by the standard but in
    lower case by some sources, so both sides are compared in capitals with
    surrounding spaces removed. Nothing else is changed: a value spelt
    differently is a different group.
    """
    if column is None:
        return f"'{ALL}'"
    return f"upper(trim(CAST({quote_identifier(column)} AS VARCHAR)))"


def _sums(con: duckdb.DuckDBPyConnection, query: str) -> dict[str | None, Decimal]:
    """Run a query of (group, amount) rows into a mapping of exact amounts."""
    return {
        group: Decimal(0) if amount is None else Decimal(amount)
        for group, amount in con.execute(query).fetchall()
    }


def expected_totals(
    con: duckdb.DuckDBPyConnection, model: ModelSpec, total: Total, path: Path
) -> TotalExpectation:
    """Add up a raw money column, and what each declared exclusion removes from it.

    Raw values are read as text and converted to a DECIMAL with as many
    decimal places as the longest value has, so nothing is rounded. Empty and
    missing values add nothing. Rows are charged to exclusions exactly as the
    rows check charges them.
    """
    source = relation(path)
    text = f"trim(CAST({quote_identifier(total.source_column)} AS VARCHAR))"
    is_number = f"regexp_full_match({text}, '{PLAIN_NUMBER}')"
    group = _group(total.source_by if total.by else None)
    claimed, kept_condition = claimed_by_filters(model)
    distinct = any(exclusion.duplicate_rows for exclusion in model.exclusions)
    kept = f"SELECT * FROM {source} WHERE {kept_condition}"
    kept_once = f"SELECT DISTINCT * FROM ({kept})" if distinct else kept
    try:
        (scale,) = con.execute(
            f"SELECT coalesce(max(length(split_part({text}, '.', 2))), 0) "
            f"FROM {source} WHERE {is_number}"
        ).fetchone()
        if scale > DECIMAL_DIGITS:
            raise GateError(
                f"{model.name}: {model.source}.{total.source_column} has a value with "
                f"{scale} decimal places, more than the {DECIMAL_DIGITS} that can be added exactly"
            )
        amount = (
            f"CASE WHEN {is_number} THEN CAST({text} AS DECIMAL({DECIMAL_DIGITS}, {scale})) END"
        )

        def sums(rows: str) -> dict[str | None, Decimal]:
            return _sums(con, f"SELECT {group}, sum({amount}) FROM ({rows}) GROUP BY ALL")

        source_totals = sums(f"SELECT * FROM {source}")
        excluded = [
            (name, sums(f"SELECT * FROM {source} WHERE {where}")) for name, where in claimed
        ]
        if distinct:
            kept_totals, once_totals = sums(kept), sums(kept_once)
            removed = {
                key: kept_totals[key] - once_totals.get(key, Decimal(0)) for key in kept_totals
            }
            excluded += [
                (exclusion.name, removed)
                for exclusion in model.exclusions
                if exclusion.duplicate_rows
            ]
        unreadable_rows = (
            f"SELECT {text} AS value FROM ({kept_once}) WHERE {text} <> '' AND NOT {is_number}"
        )
        (unreadable,) = con.execute(f"SELECT count(*) FROM ({unreadable_rows})").fetchone()
        examples = tuple(
            value
            for (value,) in con.execute(
                f"SELECT DISTINCT value FROM ({unreadable_rows}) ORDER BY value LIMIT {EXAMPLES}"
            ).fetchall()
        )
    except duckdb.Error as error:
        raise GateError(
            f"{model.name}: could not add up {model.source}.{total.source_column}: {error}"
        ) from error
    by_name = dict(excluded)
    ordered = tuple((exclusion.name, by_name[exclusion.name]) for exclusion in model.exclusions)
    expected = dict(source_totals)
    for _, amounts in ordered:
        for key, value in amounts.items():
            expected[key] = expected.get(key, Decimal(0)) - value
    return TotalExpectation(
        total=total,
        source_totals=source_totals,
        excluded=ordered,
        expected=expected,
        unreadable=unreadable,
        unreadable_examples=examples,
    )


def check_rows(
    con: duckdb.DuckDBPyConnection, model: ModelSpec, built: str, expectation: RowExpectation
) -> Result:
    """The model must hold exactly the rows its source accounts for."""
    facts = {
        "source_rows": expectation.source_rows,
        "excluded": dict(expectation.excluded),
        "expected_rows": expectation.expected_rows,
    }
    try:
        actual = con.execute(f"SELECT count(*) FROM {built}").fetchone()[0]
    except duckdb.Error as error:
        return Result("rows", model.name, FAIL, f"could not read the model: {error}", facts)
    facts["model_rows"] = actual
    removed = sum(count for _, count in expectation.excluded)
    listed = ", ".join(f"{name} {count}" for name, count in expectation.excluded)
    detail = (
        f"{counted(expectation.source_rows, 'row')} in {expectation.source}, "
        f"{removed} excluded{f' ({listed})' if listed else ''}, "
        f"{expectation.expected_rows} expected, {actual} in the model"
    )
    if actual == expectation.expected_rows:
        return Result("rows", model.name, PASS, detail, facts)
    difference = actual - expectation.expected_rows
    direction = "more" if difference > 0 else "fewer"
    return Result(
        "rows", model.name, FAIL, f"{detail}: {abs(difference)} {direction} than expected", facts
    )


def _group_label(group: str | None) -> str:
    return "(null)" if group is None else group


def _amounts(values: dict[str | None, Decimal]) -> str:
    ordered = sorted(values.items(), key=lambda item: (item[0] is None, item[0] or ""))
    return ", ".join(f"{_group_label(group)} {amount}" for group, amount in ordered)


def check_total(
    con: duckdb.DuckDBPyConnection, model: ModelSpec, built: str, expectation: TotalExpectation
) -> Result:
    """The model's money must equal the raw money less the exclusions, exactly.

    Totals are compared group by group, in capitals, and a group found on one
    side only is compared with zero. The model column must hold whole numbers
    or DECIMAL: floating point cannot hold money exactly.
    """
    total = expectation.total
    label = f"{total.column} by {total.by}" if total.by else total.column
    facts: dict = {
        "source": expectation.source_totals,
        "excluded": dict(expectation.excluded),
        "expected": expectation.expected,
    }
    if expectation.unreadable:
        shown = ", ".join(repr(value) for value in expectation.unreadable_examples)
        facts["unreadable"] = expectation.unreadable
        return Result(
            "total",
            model.name,
            FAIL,
            f"{label}: {counted(expectation.unreadable, 'raw value')} in "
            f"{model.source}.{total.source_column} not excluded and not a number, "
            f"for example {shown}",
            facts,
        )
    try:
        types = {
            name.lower(): kind for name, kind, *_ in con.execute(f"DESCRIBE {built}").fetchall()
        }
        missing = [
            column for column in (total.column, total.by) if column and column.lower() not in types
        ]
        if missing:
            return Result(
                "total",
                model.name,
                FAIL,
                f"{label}: column {', '.join(missing)} is not in the model",
                {**facts, "missing_columns": missing},
            )
        kind = types[total.column.lower()]
        if kind in FLOAT_TYPES:
            return Result(
                "total",
                model.name,
                FAIL,
                f"{label}: the model holds money as floating point ({kind}), "
                "which cannot add up exactly; use an integer or DECIMAL",
                facts,
            )
        if kind not in INTEGER_TYPES and not kind.startswith("DECIMAL"):
            return Result(
                "total",
                model.name,
                FAIL,
                f"{label}: the model column is {kind}, not a number; use an integer or DECIMAL",
                facts,
            )
        group = _group(total.by)
        actual = _sums(
            con,
            f"SELECT {group}, sum({quote_identifier(total.column)}) FROM {built} GROUP BY ALL",
        )
    except duckdb.Error as error:
        return Result("total", model.name, FAIL, f"could not read the model: {error}", facts)
    facts["model"] = actual
    groups = sorted(
        set(expectation.expected) | set(actual), key=lambda group: (group is None, group or "")
    )
    lines = []
    for group in groups:
        expected = expectation.expected.get(group, Decimal(0))
        found = actual.get(group, Decimal(0))
        if expected == found:
            continue
        parts = [f"raw {expectation.source_totals.get(group, Decimal(0))}"]
        parts += [
            f"{name} {amounts[group]}"
            for name, amounts in expectation.excluded
            if amounts.get(group, Decimal(0)) != 0
        ]
        difference = found - expected
        direction = "over" if difference > 0 else "short"
        prefix = f"{_group_label(group)}: " if total.by else ""
        lines.append(
            f"{prefix}{', '.join(parts)}, expected {expected}, model {found}, "
            f"{abs(difference)} {direction}"
        )
    if lines:
        kind_of_group = f"{total.by} values" if total.by else "totals"
        head = (
            f"{label}: {len(lines)} of {len(groups)} {kind_of_group} do not tie with the raw data"
        )
        return Result("total", model.name, FAIL, "\n".join([head, *lines]), facts)
    return Result(
        "total",
        model.name,
        PASS,
        f"{label}: ties with the raw data, {_amounts(actual) or 'nothing to add up'}",
        facts,
    )


def check_key(con: duckdb.DuckDBPyConnection, model: ModelSpec, built: str) -> Result:
    """The declared key must be present, never null and never repeated."""
    label = ", ".join(model.key)
    try:
        present = {row[0].lower() for row in con.execute(f"DESCRIBE {built}").fetchall()}
        missing = [column for column in model.key if column.lower() not in present]
        if missing:
            return Result(
                "key",
                model.name,
                FAIL,
                f"key column {', '.join(missing)} is not in the model",
                {"missing_columns": missing},
            )
        columns = [quote_identifier(column) for column in model.key]
        any_null = " OR ".join(f"{column} IS NULL" for column in columns)
        grouped = (
            f"SELECT {', '.join(columns)}, count(*) AS key_rows FROM {built} "
            f"WHERE NOT ({any_null}) GROUP BY {', '.join(columns)} HAVING count(*) > 1"
        )
        rows, null_rows = con.execute(
            f"SELECT count(*), count(*) FILTER (WHERE {any_null}) FROM {built}"
        ).fetchone()
        repeated_keys, surplus_rows = con.execute(
            f"SELECT count(*), coalesce(sum(key_rows - 1), 0) FROM ({grouped})"
        ).fetchone()
        text = [f"CAST({column} AS VARCHAR)" for column in columns]
        examples = con.execute(
            f"SELECT {', '.join(text)} FROM ({grouped}) "
            f"ORDER BY key_rows DESC, {', '.join(text)} LIMIT {EXAMPLES}"
        ).fetchall()
    except duckdb.Error as error:
        return Result("key", model.name, FAIL, f"could not read the model: {error}")
    facts = {
        "rows": rows,
        "null_rows": null_rows,
        "repeated_keys": repeated_keys,
        "surplus_rows": int(surplus_rows),
    }
    problems = []
    if null_rows:
        problems.append(f"{counted(null_rows, 'row')} with a null key")
    if repeated_keys:
        shown = "; ".join(", ".join(values) for values in examples)
        problems.append(
            f"{counted(repeated_keys, 'key value')} repeated, "
            f"{counted(int(surplus_rows), 'surplus row')}, for example {shown}"
        )
    if problems:
        return Result("key", model.name, FAIL, f"{label}: " + " and ".join(problems), facts)
    return Result(
        "key", model.name, PASS, f"{label}: unique and not null over {counted(rows, 'row')}", facts
    )


def _reason(message: object) -> str:
    """The start of a dbt message: what failed, then the database's own error."""
    lines = [line.strip() for line in str(message or "").splitlines() if line.strip()]
    return ": ".join(lines[:2])


def _is_model(unique_id: str, name: str) -> bool:
    """`model.<package>.<name>` is the model called name."""
    parts = unique_id.split(".", 2)
    return len(parts) == 3 and parts[0] == "model" and parts[2] == name


def check_build(
    exit_code: int, run_results: dict | None, models: list[ModelSpec], output: str = ""
) -> tuple[list[Result], dict[str, str]]:
    """Judge a finished ``dbt build`` from its exit status and run results.

    Returns the results and, for each model that was built, the name of its
    relation in the database. dbt records only the nodes it ran, so a model
    that is missing from the results was not built and fails here, even when
    nothing else did.
    """
    if run_results is None:
        tail = "\n".join(output.strip().splitlines()[-OUTPUT_TAIL:])
        detail = f"dbt build exited {exit_code} and recorded no results"
        results = [Result("build", "project", FAIL, f"{detail}\n{tail}" if tail else detail)]
        results += [
            Result("build", model.name, FAIL, "the model was not built") for model in models
        ]
        return results, {}

    nodes = run_results.get("results", [])
    failed = [node for node in nodes if node.get("status") not in ACCEPTED_STATUSES]
    warned = sum(1 for node in nodes if node.get("status") == "warn")
    facts = {"exit_code": exit_code, "nodes": len(nodes), "failed": len(failed), "warned": warned}
    if exit_code == 0 and not failed:
        detail = f"dbt build exited 0 after running {counted(len(nodes), 'node')}"
        if warned:
            detail += f", {counted(warned, 'warning')}"
        results = [Result("build", "project", PASS, detail, facts)]
    else:
        lines = [
            f"dbt build exited {exit_code}, {len(failed)} of {len(nodes)} nodes did not succeed"
        ]
        for node in failed:
            reason = _reason(node.get("message"))
            lines.append(
                f"{node.get('unique_id')}: {node.get('status')}" + (f": {reason}" if reason else "")
            )
        results = [Result("build", "project", FAIL, "\n".join(lines), facts)]

    relations: dict[str, str] = {}
    for model in models:
        matches = [node for node in nodes if _is_model(str(node.get("unique_id", "")), model.name)]
        if not matches:
            results.append(
                Result("build", model.name, FAIL, "the build did not produce this model")
            )
        elif len(matches) > 1:
            ids = ", ".join(str(node.get("unique_id")) for node in matches)
            results.append(
                Result("build", model.name, FAIL, f"more than one model has this name: {ids}")
            )
        elif matches[0].get("status") != "success":
            node = matches[0]
            reason = _reason(node.get("message"))
            results.append(
                Result(
                    "build",
                    model.name,
                    FAIL,
                    f"{node.get('status')}" + (f": {reason}" if reason else ""),
                )
            )
        elif not matches[0].get("relation_name"):
            results.append(Result("build", model.name, FAIL, "the model has no table or view"))
        else:
            relations[model.name] = matches[0]["relation_name"]
            results.append(Result("build", model.name, PASS, f"built as {relations[model.name]}"))
    return results, relations


def dbt_installed() -> bool:
    try:
        return importlib.util.find_spec("dbt.cli.main") is not None
    except ModuleNotFoundError:
        return False


def build_project(project: Path, raw: Path, workdir: Path) -> tuple[int, dict | None, str]:
    """Run ``dbt build`` on the project into a new database under workdir.

    dbt runs as its own process, as its documentation asks of callers, and
    with a connection profile written here, so the project cannot choose
    which database it is built into. Returns the exit status, the parsed
    ``run_results.json`` if dbt wrote one, and what dbt printed.
    """
    if not dbt_installed():
        raise GateError(
            'dbt is not installed; install it with: pip install "dbt-core~=1.12" "dbt-duckdb~=1.11"'
        )
    profiles = workdir / "profiles"
    profiles.mkdir(parents=True)
    database = workdir / DATABASE_FILE
    (profiles / "profiles.yml").write_text(
        f"{PROFILE_NAME}:\n"
        "  target: gate\n"
        "  outputs:\n"
        "    gate:\n"
        "      type: duckdb\n"
        f"      path: {json.dumps(str(database))}\n"
        "      threads: 1\n",
        encoding="utf-8",
    )
    target = workdir / "target"
    command = [
        sys.executable,
        "-c",
        "from dbt.cli.main import cli; cli(prog_name='dbt')",
        "build",
        "--no-use-colors",
        "--no-send-anonymous-usage-stats",
        "--project-dir",
        str(project),
        "--profiles-dir",
        str(profiles),
        "--profile",
        PROFILE_NAME,
        "--target-path",
        str(target),
        "--log-path",
        str(workdir / "logs"),
        "--vars",
        json.dumps({"raw_dir": str(raw)}),
    ]
    finished = subprocess.run(
        command,
        cwd=project,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    run_results = None
    results_file = target / "run_results.json"
    if results_file.is_file():
        try:
            run_results = json.loads(results_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            run_results = None
    return finished.returncode, run_results, finished.stdout


def _run(project: Path, raw: Path, models: list[ModelSpec], workdir: Path) -> list[Result]:
    files = source_files(raw)
    for model in models:
        if model.source is not None and model.source not in files:
            raise GateError(f"{model.name}: no raw file for source {model.source} under {raw}")
    expectations: dict[str, RowExpectation] = {}
    totals: dict[str, list[TotalExpectation]] = {}
    con = duckdb.connect()
    try:
        for model in models:
            if model.source is not None:
                path = files[model.source]
                expectations[model.name] = expected_rows(con, model, path)
                totals[model.name] = [
                    expected_totals(con, model, total, path) for total in model.totals
                ]
    finally:
        con.close()

    exit_code, run_results, output = build_project(project, raw, workdir)
    results, relations = check_build(exit_code, run_results, models, output)

    database = workdir / DATABASE_FILE
    con = duckdb.connect(str(database), read_only=True) if database.is_file() else None
    try:
        for model in models:
            built = relations.get(model.name)
            checks = (["rows"] if model.name in expectations else []) + ["key"]
            checks += ["total"] * len(model.totals)
            if built is None or con is None:
                results += [
                    Result(check, model.name, SKIP, "not checked: the model was not built")
                    for check in checks
                ]
                continue
            if model.name in expectations:
                results.append(check_rows(con, model, built, expectations[model.name]))
            results.append(check_key(con, model, built))
            results += [
                check_total(con, model, built, expectation)
                for expectation in totals.get(model.name, [])
            ]
    finally:
        if con is not None:
            con.close()
    return results


def run_gate(project: Path, raw: Path, checks: Path, workdir: Path | None = None) -> list[Result]:
    """Build the project from an empty database and run every check.

    The database, dbt's artifacts and its log are written to workdir and
    kept. Without a workdir they go to a temporary folder that is removed.
    """
    project = Path(project).resolve()
    raw = Path(raw).resolve()
    models = load_declaration(checks)
    if not (project / "dbt_project.yml").is_file():
        raise GateError(f"{project} is not a dbt project: it has no dbt_project.yml")
    if workdir is None:
        with tempfile.TemporaryDirectory(prefix="tenx-gate-") as temporary:
            return _run(project, raw, models, Path(temporary))
    workdir = Path(workdir).resolve()
    if workdir.exists() and (not workdir.is_dir() or any(workdir.iterdir())):
        raise GateError(f"{workdir} must be empty, so that the build starts from nothing")
    workdir.mkdir(parents=True, exist_ok=True)
    return _run(project, raw, models, workdir)


def passed(results: list[Result]) -> bool:
    """A gate passes only when every check ran and passed."""
    return bool(results) and all(result.status == PASS for result in results)


def render(results: list[Result]) -> str:
    tally = {
        status: sum(1 for result in results if result.status == status)
        for status in (PASS, FAIL, SKIP)
    }
    verdict = "PASS" if passed(results) else "FAIL"
    lines = [
        f"gate: {verdict} ({tally[PASS]} passed, {tally[FAIL]} failed, {tally[SKIP]} not run)",
        "",
    ]
    width = max((len(result.subject) for result in results), default=0)
    for result in results:
        first, *rest = result.detail.splitlines() or [""]
        lines.append(
            f"{result.status.upper():<4}  {result.check:<5}  {result.subject:<{width}}  {first}"
        )
        lines.extend(f"      {line}" for line in rest)
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("project", type=Path, help="the dbt project to check")
    parser.add_argument("--raw", type=Path, required=True, help="folder of raw source files")
    parser.add_argument(
        "--checks", type=Path, required=True, help="JSON file declaring what to expect"
    )
    parser.add_argument(
        "--workdir",
        type=Path,
        help="empty folder to keep the built database, dbt artifacts and log in",
    )
    args = parser.parse_args(argv)
    try:
        results = run_gate(args.project, args.raw, args.checks, args.workdir)
    except GateError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(render(results), end="")
    return 0 if passed(results) else 1


if __name__ == "__main__":
    sys.exit(main())
