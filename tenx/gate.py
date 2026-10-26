"""Check a dbt project against the raw files it was built from.

A dbt project can build cleanly and still be wrong, so the gate does not take
a green build as proof. It builds the project itself into an empty database
and then asks its own questions of the result:

    build     ``dbt build`` succeeds and every expected model was produced
    rows      each model holds the rows of its source, less the rows that
              were declared as excluded, and not one more or fewer
    key       each model's declared key has no nulls and no repeated values
    total     each declared money column adds up to exactly the raw total,
              less what the exclusions removed, for each value of a column
              such as the currency
    expected  a model that declares its expected rows holds exactly those
              rows in the named columns
    repeat    each model comes out the same when the project is built a
              second time into another empty database

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

Expected rows are declared as the columns to compare and a query that gives
them from the raw files, for a model whose right answer is not a count or a
total, such as one that matches records across sources:

    "expected": {
      "columns": ["customer_id", "account_id"],
      "query": "SELECT c.id AS customer_id, a.account_id FROM billing.customers c ..."
    }

The query reads each raw file by its table name, such as ``billing.customers``,
and runs before the project is built. Values are compared as text, and every
copy of a row counts.

The project reads the raw files from the folder passed to dbt as the variable
``raw_dir``.

Building the project runs its code, and a dbt Python model can run any
Python, so each build can run in a sandbox: it sees the project and the raw
files read-only and can write only its own work folder, and the declaration
and the expected rows are hidden from it, as is the first build's database
from the second. The gate's own checks run outside the sandbox.

Usage:
    python -m tenx.gate path/to/project --raw demo/data --checks gate.json

The command builds in a sandbox (bubblewrap, Linux) and refuses to run where
none can be set up, unless given --unisolated.

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

from tenx.profile import (
    ProfileError,
    discover,
    quote_identifier,
    quote_literal,
    relation,
    table_name,
)
from tenx.sandbox import (
    Mounts,
    Sandbox,
    SandboxError,
    Unisolated,
    default_sandbox,
    python_folders,
)

PASS, FAIL, SKIP = "pass", "fail", "skip"
# Statuses dbt records for a node that did what was asked. A test set to warn
# does not stop a dbt build, so it does not stop the gate either.
ACCEPTED_STATUSES = {"success", "pass", "warn"}
PROFILE_NAME = "tenx_gate"
DATABASE_FILE = "build.duckdb"
# The second build has a database file of its own name. dbt-duckdb names the
# catalog after the file, and a view refers to other models through that name,
# so two files with one name could not be opened side by side.
REPEAT_DIR = "repeat"
REPEAT_FILE = "repeat.duckdb"
# The rows each model is expected to hold are worked out from the raw files
# before the build and kept in a database of their own, under this name.
EXPECTED_FILE = "expected.duckdb"
EXPECTED_CATALOG = "tenx_expected"
OUTPUT_TAIL = 15
EXAMPLES = 3
MODEL_FIELDS = {"name", "key", "source", "exclusions", "totals", "expected"}
EXCLUSION_FIELDS = {"name", "where", "duplicate_rows"}
TOTAL_FIELDS = {"column", "source_column", "by", "source_by"}
EXPECTED_FIELDS = {"columns", "query"}
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
class Expected:
    """Rows a model must hold in the named columns, given by a query on the raw files."""

    columns: tuple[str, ...]
    query: str


@dataclass(frozen=True)
class ModelSpec:
    name: str
    key: tuple[str, ...]
    source: str | None = None
    exclusions: tuple[Exclusion, ...] = ()
    totals: tuple[Total, ...] = ()
    expected: Expected | None = None


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
    blank: int = 0


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


def _parse_expected(raw: object, model: str) -> Expected:
    _require(isinstance(raw, dict), f"{model}: expected must be an object")
    unknown = sorted(set(raw) - EXPECTED_FIELDS)
    _require(not unknown, f"{model}: unknown expected field {', '.join(unknown)}")
    columns = raw.get("columns")
    _require(
        isinstance(columns, list) and bool(columns) and all(map(_column_name, columns)),
        f"{model}: expected columns must be a list of one or more column names",
    )
    _require(
        len({column.lower() for column in columns}) == len(columns),
        f"{model}: expected names the same column twice",
    )
    query = raw.get("query")
    _require(
        isinstance(query, str) and bool(query.strip()),
        f"{model}: expected needs the query that gives the rows",
    )
    return Expected(columns=tuple(columns), query=query)


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
    expected = raw.get("expected")
    return ModelSpec(
        name=name,
        key=tuple(key),
        source=source,
        exclusions=exclusions,
        totals=totals,
        expected=None if expected is None else _parse_expected(expected, name),
    )


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
    return parse_declaration(payload, str(path))


def parse_declaration(payload: object, where: str = "the declaration") -> list[ModelSpec]:
    """Validate a declaration already read as JSON; where names it in messages."""
    _require(isinstance(payload, dict), f"{where} must hold a JSON object")
    unknown = sorted(set(payload) - {"models"})
    _require(not unknown, f"{where}: unknown field {', '.join(unknown)}")
    listed = payload.get("models")
    _require(isinstance(listed, list) and bool(listed), f"{where} must list at least one model")
    try:
        models = [_parse_model(item) for item in listed]
    except GateError as error:
        raise GateError(f"{where}: {error}") from error
    names = [model.name for model in models]
    repeated = sorted({name for name in names if names.count(name) > 1})
    _require(not repeated, f"{where}: {', '.join(repeated)} is declared more than once")
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
        (blank,) = con.execute(
            f"SELECT count(*) FROM ({kept_once}) WHERE coalesce({text}, '') = ''"
        ).fetchone()
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
        blank=blank,
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
        "blank": expectation.blank,
    }
    # Blank amounts add nothing, as SQL's sum ignores nulls, but are reported
    # so that missing money is seen rather than quietly left out.
    blank = (
        f"; {counted(expectation.blank, 'blank raw value')} added nothing"
        if expectation.blank
        else ""
    )
    if expectation.unreadable:
        shown = ", ".join(repr(value) for value in expectation.unreadable_examples)
        facts["unreadable"] = expectation.unreadable
        return Result(
            "total",
            model.name,
            FAIL,
            f"{label}: {counted(expectation.unreadable, 'raw value')} in "
            f"{model.source}.{total.source_column} not excluded and not a number, "
            f"for example {shown}{blank}",
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
            f"{blank}"
        )
        return Result("total", model.name, FAIL, "\n".join([head, *lines]), facts)
    return Result(
        "total",
        model.name,
        PASS,
        f"{label}: ties with the raw data, {_amounts(actual) or 'nothing to add up'}{blank}",
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


def _columns(con: duckdb.DuckDBPyConnection, relation_name: str) -> list[tuple[str, str]]:
    return [(name, kind) for name, kind, *_ in con.execute(f"DESCRIBE {relation_name}").fetchall()]


def _surplus(con: duckdb.DuckDBPyConnection, select: str, one: str, other: str) -> int:
    """Rows of one that other does not have, counting each copy of a row."""
    (count,) = con.execute(
        f"SELECT count(*) FROM (SELECT {select} FROM {one} EXCEPT ALL SELECT {select} FROM {other})"
    ).fetchone()
    return count


def _column_changes(first: list[tuple[str, str]], second: list[tuple[str, str]]) -> list[str]:
    """What differs between the columns of two builds of a model, in words."""
    one, other = dict(first), dict(second)
    changes = [f"{name} only in the first build" for name in one if name not in other]
    changes += [f"{name} only in the second build" for name in other if name not in one]
    changes += [
        f"{name} is {one[name]} in the first build and {other[name]} in the second"
        for name in one
        if name in other and one[name] != other[name]
    ]
    if not changes:
        changes.append("the columns are in a different order")
    return changes


def check_repeat(con: duckdb.DuckDBPyConnection, model: str, first: str, second: str) -> Result:
    """A model built twice from the same files must hold the same rows both times.

    The two relations are compared as bags of rows: row order does not
    matter, but every copy of a row does, so a row that one build repeats is
    found. Both builds must have the same columns, with the same types, in
    the same order. When rows differ, the columns whose values differ are
    named, so a load time or a random key is easy to spot.
    """
    try:
        columns, second_columns = _columns(con, first), _columns(con, second)
        if columns != second_columns:
            changes = _column_changes(columns, second_columns)
            return Result(
                "repeat",
                model,
                FAIL,
                f"the two builds have different columns: {'; '.join(changes)}",
                {"column_changes": changes},
            )
        names = [quote_identifier(name) for name, _ in columns]
        select = ", ".join(names)
        rows, second_rows = con.execute(
            f"SELECT (SELECT count(*) FROM {first}), (SELECT count(*) FROM {second})"
        ).fetchone()
        only_first = _surplus(con, select, first, second)
        only_second = _surplus(con, select, second, first)
        differing = []
        if only_first or only_second:
            differing = [
                name
                for (name, _), quoted in zip(columns, names)
                if _surplus(con, quoted, first, second) or _surplus(con, quoted, second, first)
            ]
    except duckdb.Error as error:
        return Result("repeat", model, FAIL, f"could not compare the two builds: {error}")
    facts = {
        "rows": rows,
        "second_rows": second_rows,
        "only_first": only_first,
        "only_second": only_second,
        "differing_columns": differing,
    }
    if not only_first and not only_second:
        return Result(
            "repeat", model, PASS, f"the same {counted(rows, 'row')} in both builds", facts
        )
    detail = (
        f"the builds differ: {rows} rows in the first and {second_rows} in the second, "
        f"of which {only_first} only in the first and {only_second} only in the second"
    )
    if differing:
        detail += f"; values differ in {', '.join(differing)}"
    else:
        detail += "; each column holds the same values, combined differently across rows"
    return Result("repeat", model, FAIL, detail, facts)


def _as_text(columns: tuple[str, ...]) -> str:
    return ", ".join(
        f"CAST({quote_identifier(column)} AS VARCHAR) AS {quote_identifier(column)}"
        for column in columns
    )


def _value(value: str | None) -> str:
    """A value as text, quoted when spaces around it or its being empty would not show."""
    if value is None:
        return "null"
    return f'"{value}"' if value != value.strip() or not value else value


def _shown(row: tuple) -> str:
    return "(" + ", ".join(map(_value, row)) + ")"


def check_expected(
    con: duckdb.DuckDBPyConnection, model: ModelSpec, built: str, reference: str
) -> Result:
    """The model must hold exactly the expected rows in the declared columns.

    ``reference`` holds the expected rows, worked out from the raw files, as
    text. The model's columns are read as text too, so the comparison is of
    values, not of the types the project chose. Rows are compared as bags:
    order does not matter, every copy of a row does, and nulls are equal.
    """
    columns = model.expected.columns
    label = ", ".join(columns)
    try:
        present = {row[0].lower() for row in con.execute(f"DESCRIBE {built}").fetchall()}
        missing = [column for column in columns if column.lower() not in present]
        if missing:
            return Result(
                "expected",
                model.name,
                FAIL,
                f"column {', '.join(missing)} is not in the model",
                {"missing_columns": missing},
            )
        actual = f"(SELECT {_as_text(columns)} FROM {built})"
        wanted = f"(SELECT {_as_text(columns)} FROM {reference})"
        names = ", ".join(quote_identifier(column) for column in columns)

        def surplus(one: str, other: str) -> tuple[int, list[tuple]]:
            rows = f"SELECT {names} FROM {one} EXCEPT ALL SELECT {names} FROM {other}"
            (count,) = con.execute(f"SELECT count(*) FROM ({rows})").fetchone()
            examples = con.execute(
                f"SELECT * FROM ({rows}) ORDER BY ALL NULLS FIRST LIMIT {EXAMPLES}"
            ).fetchall()
            return count, examples

        model_rows, expected_rows = con.execute(
            f"SELECT (SELECT count(*) FROM {built}), (SELECT count(*) FROM {reference})"
        ).fetchone()
        only_model, model_examples = surplus(actual, wanted)
        only_expected, expected_examples = surplus(wanted, actual)
    except duckdb.Error as error:
        return Result("expected", model.name, FAIL, f"could not read the model: {error}")
    facts = {
        "expected_rows": expected_rows,
        "model_rows": model_rows,
        "only_model": only_model,
        "only_expected": only_expected,
    }
    if not only_model and not only_expected:
        return Result(
            "expected",
            model.name,
            PASS,
            f"{label}: the {counted(expected_rows, 'expected row')}, and no other",
            facts,
        )
    lines = [
        (
            f"{label}: {counted(expected_rows, 'row')} expected and {model_rows} in the model, "
            f"of which {only_model} not expected and {only_expected} expected but missing"
        )
    ]
    if model_examples:
        lines.append("not expected: " + ", ".join(map(_shown, model_examples)))
    if expected_examples:
        lines.append("missing: " + ", ".join(map(_shown, expected_examples)))
    return Result("expected", model.name, FAIL, "\n".join(lines), facts)


def raw_tables(con: duckdb.DuckDBPyConnection, files: dict[str, Path]) -> None:
    """Make each raw file readable by its table name, such as ``billing.customers``.

    The first part of the name is a schema and the second a view of the file,
    read as the other checks read it. A file at the top of the raw folder is a
    view in the main schema. Files nested more deeply are not offered.
    """
    for name, path in sorted(files.items()):
        parts = name.split(".")
        if len(parts) > 2:
            continue
        schema, table = parts if len(parts) == 2 else ("main", parts[0])
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {quote_identifier(schema)}")
        con.execute(
            f"CREATE VIEW {quote_identifier(schema)}.{quote_identifier(table)} AS "
            f"SELECT * FROM {relation(path)}"
        )


def expected_relation(model: str) -> str:
    return f"{EXPECTED_CATALOG}.main.{quote_identifier(model)}"


def store_expected(models: list[ModelSpec], files: dict[str, Path], database: Path) -> None:
    """Run each model's expected query on the raw files and keep its rows as text.

    The rows go into a table named after the model in a database file of
    their own, so they are fixed before the project is built.
    """
    con = duckdb.connect()
    try:
        raw_tables(con, files)
        con.execute(f"ATTACH {quote_literal(str(database))} AS {EXPECTED_CATALOG}")
        for model in models:
            if model.expected is None:
                continue
            try:
                con.execute(
                    f"CREATE TABLE {expected_relation(model.name)} AS "
                    f"SELECT {_as_text(model.expected.columns)} FROM ({model.expected.query})"
                )
            except duckdb.Error as error:
                raise GateError(
                    f"{model.name}: could not work out the expected rows: {error}"
                ) from error
    finally:
        con.close()


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


def built_models(run_results: dict | None) -> dict[str, tuple[str, str]]:
    """Every model a build produced, by unique id, with its name and relation.

    The name is the model's own unless two packages use it, in which case it
    is the unique id.
    """
    built = {
        str(node["unique_id"]): str(node["relation_name"])
        for node in (run_results or {}).get("results", [])
        if str(node.get("unique_id", "")).startswith("model.")
        and node.get("status") == "success"
        and node.get("relation_name")
    }
    names = [unique_id.split(".", 2)[-1] for unique_id in built]
    return {
        unique_id: (name if names.count(name) == 1 else unique_id, relation_name)
        for (unique_id, relation_name), name in zip(built.items(), names)
    }


def check_repeat_build(
    workdir: Path,
    first: dict[str, tuple[str, str]],
    exit_code: int,
    run_results: dict | None,
    output: str,
) -> list[Result]:
    """Judge a second build of the project against the first, model by model.

    ``first`` holds the models the first build produced. Each must be
    produced again, with the same rows.
    """
    (judged,), _ = check_build(exit_code, run_results, [], output)
    result = Result("repeat", "project", judged.status, f"second build: {judged.detail}")
    results = [result]
    second = built_models(run_results)
    database = workdir / REPEAT_DIR / REPEAT_FILE
    con = duckdb.connect(str(workdir / DATABASE_FILE), read_only=True)
    try:
        if database.is_file():
            con.execute(f"ATTACH {quote_literal(str(database))} (READ_ONLY)")
        for unique_id, (name, relation_name) in first.items():
            if unique_id not in second:
                results.append(
                    Result("repeat", name, FAIL, "the second build did not produce this model")
                )
            else:
                results.append(check_repeat(con, name, relation_name, second[unique_id][1]))
    finally:
        con.close()
    return results


def build_project(
    project: Path,
    raw: Path,
    workdir: Path,
    database_file: str = DATABASE_FILE,
    sandbox: Sandbox | None = None,
    hidden: tuple[Path, ...] = (),
) -> tuple[int, dict | None, str]:
    """Run ``dbt build`` on the project into a new database under workdir.

    dbt runs as its own process, as its documentation asks of callers, and
    with a connection profile written here, so the project cannot choose
    which database it is built into. In ``sandbox`` the build sees the
    project and the raw files read-only, can write only workdir, and never
    sees the ``hidden`` paths. Returns the exit status, the parsed
    ``run_results.json`` if dbt wrote one, and what dbt printed.
    """
    if not dbt_installed():
        raise GateError(
            'dbt is not installed; install it with: pip install "dbt-core~=1.12" "dbt-duckdb~=1.11"'
        )
    profiles = workdir / "profiles"
    profiles.mkdir(parents=True)
    database = workdir / database_file
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
    seen = Mounts(
        workspace=workdir,
        read_only=(project, raw, *python_folders()),
        hidden=tuple(path for path in hidden if path.exists()),
    )
    try:
        command = (sandbox or Unisolated()).command(command, seen)
    except SandboxError as error:
        raise GateError(f"the build cannot be kept apart from the gate's files: {error}") from error
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


def _run(
    project: Path,
    raw: Path,
    models: list[ModelSpec],
    workdir: Path,
    sandbox: Sandbox | None,
    hidden: tuple[Path, ...],
) -> list[Result]:
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
    if any(model.expected is not None for model in models):
        store_expected(models, files, workdir / EXPECTED_FILE)

    # Neither build may see the rows it is compared with.
    hidden = (*hidden, workdir / EXPECTED_FILE)
    exit_code, run_results, output = build_project(
        project, raw, workdir, sandbox=sandbox, hidden=hidden
    )
    results, relations = check_build(exit_code, run_results, models, output)

    database = workdir / DATABASE_FILE
    first = built_models(run_results) if database.is_file() else {}
    con = duckdb.connect(str(database), read_only=True) if database.is_file() else None
    try:
        if con is not None and (workdir / EXPECTED_FILE).is_file():
            con.execute(
                f"ATTACH {quote_literal(str(workdir / EXPECTED_FILE))} "
                f"AS {EXPECTED_CATALOG} (READ_ONLY)"
            )
        for model in models:
            built = relations.get(model.name)
            checks = (["rows"] if model.name in expectations else []) + ["key"]
            checks += ["total"] * len(model.totals)
            checks += ["expected"] if model.expected is not None else []
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
            if model.expected is not None:
                results.append(check_expected(con, model, built, expected_relation(model.name)))
    finally:
        if con is not None:
            con.close()

    # The project is built again from nothing and must give the same tables.
    if first:
        # Nor may the second build see the first one's tables.
        second = build_project(
            project,
            raw,
            workdir / REPEAT_DIR,
            REPEAT_FILE,
            sandbox=sandbox,
            hidden=(*hidden, workdir / DATABASE_FILE),
        )
        results += check_repeat_build(workdir, first, *second)
    else:
        results.append(
            Result("repeat", "project", SKIP, "not checked: the first build produced no model")
        )
    built = {name for name, _ in first.values()}
    results += [
        Result("repeat", model.name, SKIP, "not checked: the model was not built")
        for model in models
        if model.name not in built
    ]
    return results


def run_gate(
    project: Path,
    raw: Path,
    checks: Path,
    workdir: Path | None = None,
    sandbox: Sandbox | None = None,
    hidden: tuple[Path, ...] = (),
) -> list[Result]:
    """Build the project from an empty database and run every check.

    The database, dbt's artifacts and its log are written to workdir and
    kept. Without a workdir they go to a temporary folder that is removed.
    With a sandbox, each build runs in it and cannot see the declaration,
    the expected rows or the ``hidden`` paths; without one, the builds can
    read and change any file the user can.
    """
    project = Path(project).resolve()
    raw = Path(raw).resolve()
    models = load_declaration(checks)
    hidden = (Path(checks).resolve(), *(Path(path).resolve() for path in hidden))
    if not (project / "dbt_project.yml").is_file():
        raise GateError(f"{project} is not a dbt project: it has no dbt_project.yml")
    if workdir is None:
        with tempfile.TemporaryDirectory(prefix="tenx-gate-") as temporary:
            return _run(project, raw, models, Path(temporary).resolve(), sandbox, hidden)
    workdir = Path(workdir).resolve()
    if workdir.exists() and (not workdir.is_dir() or any(workdir.iterdir())):
        raise GateError(f"{workdir} must be empty, so that the build starts from nothing")
    workdir.mkdir(parents=True, exist_ok=True)
    return _run(project, raw, models, workdir, sandbox, hidden)


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
    checks = max((len(result.check) for result in results), default=0)
    width = max((len(result.subject) for result in results), default=0)
    for result in results:
        first, *rest = result.detail.splitlines() or [""]
        lines.append(
            f"{result.status.upper():<4}  {result.check:<{checks}}  "
            f"{result.subject:<{width}}  {first}"
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
    parser.add_argument(
        "--unisolated",
        action="store_true",
        help="build without a sandbox, so that the project's code can read and change any "
        "file you can; only for a project you trust",
    )
    args = parser.parse_args(argv)
    try:
        sandbox = default_sandbox(unisolated=args.unisolated)
    except SandboxError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if not sandbox.isolated:
        print("warning: built without a sandbox (--unisolated)", file=sys.stderr)
    try:
        results = run_gate(args.project, args.raw, args.checks, args.workdir, sandbox)
    except GateError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(render(results), end="")
    return 0 if passed(results) else 1


if __name__ == "__main__":
    sys.exit(main())
