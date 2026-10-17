"""Find columns in different tables that hold the same values.

Every pair of columns in different tables is compared on its distinct
values, and the share of each column's values found in the other is reported
both ways, since the two directions differ. This is the starting point for
deciding how tables join, within a source system and across systems. It
reports facts, not joins: two columns of currency codes or two timestamp
columns can share values without being related.

Values are compared as text in lower case with surrounding spaces removed,
so `Billing@x.example ` and `billing@x.example` are the same value. Rows
that match only exactly are also counted, to show what an exact join would
miss. Nulls, blanks and placeholder values such as `null`, `anonymous` and
`-1` are left out before comparing, and counted.

The report is written as compact text for an agent to read and as JSON for
code, beside the table profiles.

Usage:
    python -m tenx.overlap demo/data --out profiles
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import duckdb

from tenx.profile import (
    PLACEHOLDERS,
    ProfileError,
    discover,
    quote_identifier,
    quote_literal,
    relation,
    shorten,
    table_name,
)

# A pair is listed when at least this share of one column's distinct values
# is found in the other column.
MIN_CONTAINMENT = 0.5
NOT_FOUND_EXAMPLES = 3
NESTED_TYPES = ("STRUCT", "MAP", "UNION")
REPORT_NAME = "overlap"


def is_nested(native_type: str) -> bool:
    """Lists, arrays, structs, maps and unions are not compared."""
    return native_type.startswith(NESTED_TYPES) or native_type.endswith("]")


def load_values(
    con: duckdb.DuckDBPyConnection, files: list[tuple[str, Path]]
) -> tuple[list[dict], list[dict]]:
    """Collect every scalar column's values into the table `vals`.

    `vals` holds one row per column and raw value: the value as text, its
    normalised form and the number of rows that hold it. Returns the columns
    compared, with what was left out of each, and the columns skipped.
    """
    con.execute(
        "CREATE OR REPLACE TEMP TABLE vals "
        "(tbl VARCHAR, col VARCHAR, raw VARCHAR, norm VARCHAR, n BIGINT)"
    )
    placeholders = ", ".join(quote_literal(value) for value in PLACEHOLDERS)
    columns: list[dict] = []
    skipped: list[dict] = []
    for name, path in files:
        try:
            con.execute(f"CREATE OR REPLACE TEMP VIEW source AS SELECT * FROM {relation(path)}")
            described = con.execute("DESCRIBE source").fetchall()
            total = con.execute("SELECT count(*) FROM source").fetchone()[0]
        except duckdb.Error as error:
            raise ProfileError(f"could not read {path}: {error}") from error
        for column, native, *_ in described:
            if is_nested(native):
                skipped.append({"table": name, "column": column, "type": native})
                continue
            text = f"CAST({quote_identifier(column)} AS VARCHAR)"
            norm = f"lower(trim({text}))"
            nulls, blanks, placeholder_rows = con.execute(
                f"""
                SELECT
                    count(*) FILTER (WHERE {text} IS NULL),
                    count(*) FILTER (WHERE {text} IS NOT NULL AND {norm} = ''),
                    count(*) FILTER (WHERE {norm} IN ({placeholders}))
                FROM source
                """
            ).fetchone()
            con.execute(
                f"""
                INSERT INTO vals
                SELECT {quote_literal(name)}, {quote_literal(column)}, {text}, {norm}, count(*)
                FROM source
                WHERE {text} IS NOT NULL AND {norm} <> '' AND {norm} NOT IN ({placeholders})
                GROUP BY {text}
                """
            )
            columns.append(
                {
                    "table": name,
                    "column": column,
                    "rows_in_table": total,
                    "nulls": nulls,
                    "blanks": blanks,
                    "placeholders": placeholder_rows,
                }
            )
    stats = {
        (tbl, col): (rows, values)
        for tbl, col, rows, values in con.execute(
            "SELECT tbl, col, sum(n), count(DISTINCT norm) FROM vals GROUP BY tbl, col"
        ).fetchall()
    }
    for column in columns:
        rows, values = stats.get((column["table"], column["column"]), (0, 0))
        column["rows"] = int(rows)
        column["values"] = values
        column["one_row_per_value"] = rows > 0 and rows == values
    return columns, skipped


def shared_values(con: duckdb.DuckDBPyConnection) -> list[tuple[str, str, str, str, int]]:
    """Every pair of columns in different tables that shares a value.

    Each pair appears once, ordered by table and column name, with the number
    of distinct normalised values the two columns share.
    """
    return con.execute(
        """
        WITH d AS (SELECT DISTINCT tbl, col, norm FROM vals)
        SELECT a.tbl, a.col, b.tbl, b.col, count(*)
        FROM d AS a JOIN d AS b
          ON a.norm = b.norm AND a.tbl < b.tbl
        GROUP BY a.tbl, a.col, b.tbl, b.col
        ORDER BY a.tbl, a.col, b.tbl, b.col
        """
    ).fetchall()


def side(con: duckdb.DuckDBPyConnection, this: dict, other: dict, shared: int) -> dict:
    """How much of one column is found in the other."""
    params = [this["table"], this["column"], other["table"], other["column"]]
    matched, exact = con.execute(
        """
        WITH a AS (SELECT raw, norm, n FROM vals WHERE tbl = ? AND col = ?),
             b AS (SELECT raw, norm FROM vals WHERE tbl = ? AND col = ?)
        SELECT
            coalesce(sum(n) FILTER (WHERE norm IN (SELECT norm FROM b)), 0),
            coalesce(sum(n) FILTER (WHERE raw IN (SELECT raw FROM b)), 0)
        FROM a
        """,
        params,
    ).fetchone()
    examples = con.execute(
        f"""
        WITH a AS (SELECT raw, norm FROM vals WHERE tbl = ? AND col = ?),
             b AS (SELECT norm FROM vals WHERE tbl = ? AND col = ?)
        SELECT min(raw) FROM a
        WHERE norm NOT IN (SELECT norm FROM b)
        GROUP BY norm ORDER BY norm LIMIT {NOT_FOUND_EXAMPLES}
        """,
        params,
    ).fetchall()
    return {
        "table": this["table"],
        "column": this["column"],
        "values": this["values"],
        "found": shared,
        "containment": shared / this["values"],
        "rows": this["rows"],
        "rows_matched": int(matched),
        "rows_matched_exactly": int(exact),
        "one_row_per_value": this["one_row_per_value"],
        "not_found": [raw for (raw,) in examples],
    }


def compare(con: duckdb.DuckDBPyConnection, columns: list[dict]) -> list[dict]:
    """Every pair that shares a value, most overlapping first.

    The side with the larger share of its values found in the other comes
    first. A pair is `listed` when that share is at least MIN_CONTAINMENT.
    """
    by_name = {(column["table"], column["column"]): column for column in columns}
    pairs = []
    for table_a, column_a, table_b, column_b, shared in shared_values(con):
        a = side(con, by_name[(table_a, column_a)], by_name[(table_b, column_b)], shared)
        b = side(con, by_name[(table_b, column_b)], by_name[(table_a, column_a)], shared)
        first, second = (a, b) if a["containment"] >= b["containment"] else (b, a)
        pairs.append(
            {
                "sides": [first, second],
                "shared": shared,
                "listed": first["containment"] >= MIN_CONTAINMENT,
            }
        )
    pairs.sort(
        key=lambda pair: (
            -pair["sides"][0]["containment"],
            -pair["shared"],
            pair["sides"][0]["table"],
            pair["sides"][0]["column"],
            pair["sides"][1]["table"],
            pair["sides"][1]["column"],
        )
    )
    return pairs


def overlap_folder(root: Path, exclude: Path | None = None) -> dict:
    """Compare the columns of every table under root.

    Files under `exclude`, such as profiles written inside the source
    folder, are not read.
    """
    files = []
    seen: dict[str, Path] = {}
    excluded = exclude.resolve() if exclude is not None else None
    for path in discover(root):
        if excluded is not None and excluded in path.resolve().parents:
            continue
        name = table_name(path, root)
        if name in seen:
            raise ProfileError(
                f"{seen[name].relative_to(root)} and {path.relative_to(root)} "
                f"would both be read as {name}; rename one of them"
            )
        seen[name] = path
        files.append((name, path))
    if len(files) < 2:
        raise ProfileError(f"at least two tables are needed under {root}; found {len(files)}")
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    try:
        columns, skipped = load_values(con, files)
        pairs = compare(con, columns)
    finally:
        con.close()
    return {
        "tables": [name for name, _ in files],
        "columns": columns,
        "skipped": skipped,
        "min_containment": MIN_CONTAINMENT,
        "pairs": pairs,
    }


def qualified(side: dict) -> str:
    return f"{side['table']}.{side['column']}"


def example(value: str) -> str:
    """A value as it is in the file, quoted if it has surrounding spaces."""
    return json.dumps(value) if value != value.strip(" ") else shorten(value)


def describe_side(side: dict) -> str:
    parts = [
        f"{side['found']} of {side['values']} values found ({side['containment']:.1%})",
        (
            f"{side['rows_matched']} of {side['rows']} rows match "
            f"({side['rows_matched_exactly']} exactly)"
        ),
        "one row per value" if side["one_row_per_value"] else "many rows per value",
    ]
    if side["not_found"]:
        parts.append("not found: " + ", ".join(example(value) for value in side["not_found"]))
    return ", ".join(parts)


def render(report: dict) -> str:
    """The compact text report: the listed pairs, then what was left out."""
    listed = [pair for pair in report["pairs"] if pair["listed"]]
    below = len(report["pairs"]) - len(listed)
    lines = [
        f"overlap: {len(report['tables'])} tables, {len(report['columns'])} columns",
        (
            "values are compared in lower case with surrounding spaces removed; "
            "nulls, blanks and placeholders are left out"
        ),
        (
            f"pairs listed: {len(listed)}, where at least {report['min_containment']:.0%} "
            "of one column's values are found in the other; "
            f"{below} more {'pair shares' if below == 1 else 'pairs share'} fewer values"
        ),
    ]
    for pair in listed:
        width = max(len(qualified(side)) for side in pair["sides"])
        lines.append("")
        first, second = pair["sides"]
        noun = "value" if pair["shared"] == 1 else "values"
        lines.append(f"{qualified(first)} ~ {qualified(second)}: {pair['shared']} shared {noun}")
        for side in pair["sides"]:
            lines.append(f"  {qualified(side):<{width}}  {describe_side(side)}")
    left_out = []
    for column in report["columns"]:
        counts = [
            f"{column[key]} {label}"
            for key, label in (
                ("nulls", "null"),
                ("blanks", "blank"),
                ("placeholders", "placeholder"),
            )
            if column[key]
        ]
        if counts:
            left_out.append(f"  {column['table']}.{column['column']}: {', '.join(counts)}")
    if left_out:
        lines.append("")
        lines.append("rows left out before comparing:")
        lines.extend(left_out)
    if report["skipped"]:
        lines.append("")
        lines.append("not compared, because the values are nested:")
        lines.extend(
            f"  {column['table']}.{column['column']} ({column['type']})"
            for column in report["skipped"]
        )
    return "\n".join(lines) + "\n"


def write_report(report: dict, out: Path) -> None:
    """Write overlap.txt and overlap.json to out."""
    if REPORT_NAME in report["tables"]:
        raise ProfileError(
            f"a table named {REPORT_NAME} would have its profile overwritten by this report; "
            "rename the file"
        )
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{REPORT_NAME}.txt").write_text(render(report), encoding="utf-8")
    (out / f"{REPORT_NAME}.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("root", type=Path, help="folder of raw CSV, JSON or Parquet files")
    parser.add_argument("--out", type=Path, default=Path("profiles"))
    args = parser.parse_args(argv)
    try:
        report = overlap_folder(args.root, exclude=args.out)
        write_report(report, args.out)
    except ProfileError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    listed = sum(1 for pair in report["pairs"] if pair["listed"])
    print(
        f"{len(report['pairs'])} column pairs share values, {listed} listed; "
        f"wrote {REPORT_NAME}.txt and {REPORT_NAME}.json to {args.out}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
