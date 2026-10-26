"""Profile raw source files with SQL and write one compact profile per table.

Every figure is exact and computed in DuckDB. Nothing is sampled. For each
table the profiler writes a short text profile, small enough to hand to a
model in place of the data, and a JSON file with the same facts for code.

CSV files are read as text so that nothing is lost to type inference: an
identifier such as ``0012`` keeps its leading zeros and a day-first date is
not silently reinterpreted. The profiler then reports what each column would
safely convert to.

Usage:
    python -m tenx.profile demo/data --out profiles
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import duckdb

from tenx import artefacts

FORMATS = {
    ".csv": "csv",
    ".tsv": "csv",
    ".json": "json",
    ".jsonl": "json",
    ".ndjson": "json",
    ".parquet": "parquet",
}
LINE_DELIMITED = {".csv", ".tsv", ".jsonl", ".ndjson"}
PLACEHOLDERS = (
    "null",
    "none",
    "nil",
    "n/a",
    "na",
    "nan",
    "unknown",
    "anonymous",
    "undefined",
    "-1",
    "-",
)
DATE_FORMATS = (
    ("%d/%m/%Y", "day/month/year"),
    ("%m/%d/%Y", "month/day/year"),
    ("%Y/%m/%d", "year/month/day"),
    ("%d-%m-%Y", "day-month-year"),
    ("%m-%d-%Y", "month-day-year"),
)
INTEGER = r"[+-]?[0-9]+"
DECIMAL = r"[+-]?([0-9]+\.[0-9]*|\.[0-9]+|[0-9]+)([eE][+-]?[0-9]+)?"
ISO_DATE = r"[0-9]{4}-[0-9]{2}-[0-9]{2}"
ISO_TIMESTAMP = r"[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}.*"
LEADING_ZEROS = r"[+-]?0[0-9]+"
UNIX_SECONDS = (946684800, 4102444800)  # 2000-01-01 to 2100-01-01
INTEGER_TYPES = (
    "TINYINT",
    "SMALLINT",
    "INTEGER",
    "BIGINT",
    "HUGEINT",
    "UTINYINT",
    "USMALLINT",
    "UINTEGER",
    "UBIGINT",
    "UHUGEINT",
)
NUMERIC_TYPES = INTEGER_TYPES + ("FLOAT", "DOUBLE", "DECIMAL")
TEMPORAL_TYPES = ("DATE", "TIMESTAMP")
TOP_VALUES = 3
MAX_VALUE_WIDTH = 40


class ProfileError(Exception):
    """Raised when there is nothing to profile or a file cannot be read."""


def quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def discover(root: Path) -> list[Path]:
    """Every supported file under root, in a stable order."""
    if not root.is_dir():
        raise ProfileError(f"{root} is not a folder")
    return sorted(
        path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in FORMATS
    )


def table_name(path: Path, root: Path) -> str:
    """`billing/invoices.csv` under root becomes `billing.invoices`."""
    return ".".join(path.relative_to(root).with_suffix("").parts)


def relation(path: Path) -> str:
    """The DuckDB expression that reads the file."""
    literal = quote_literal(str(path))
    kind = FORMATS[path.suffix.lower()]
    if kind == "csv":
        return f"read_csv({literal}, all_varchar=true, header=true)"
    if kind == "json":
        return f"read_json_auto({literal})"
    return f"read_parquet({literal})"


def count_file_records(path: Path) -> int | None:
    """Count records straight from the file, without the SQL reader.

    This is an independent figure to hold the loaded row count against, so a
    reader that silently drops rows is noticed. It is only meaningful for
    line-delimited formats, and a CSV with line breaks inside quoted fields
    will legitimately show more lines than rows.
    """
    suffix = path.suffix.lower()
    if suffix not in LINE_DELIMITED:
        return None
    with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        lines = sum(1 for line in handle if line.strip())
    return max(lines - 1, 0) if FORMATS[suffix] == "csv" else lines


def shorten(value: object) -> str:
    text = str(value)
    return text if len(text) <= MAX_VALUE_WIDTH else text[: MAX_VALUE_WIDTH - 1] + "…"


def counted(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.6g}"


def profile_column(con: duckdb.DuckDBPyConnection, name: str, native_type: str, rows: int) -> dict:
    column = quote_identifier(name)
    text = f"CAST({column} AS VARCHAR)"
    trimmed = f"trim({text})"
    date_counts = ", ".join(
        f"count(try_strptime({trimmed}, {quote_literal(fmt)}))" for fmt, _ in DATE_FORMATS
    )
    iso_date = f"regexp_full_match({trimmed}, {quote_literal(ISO_DATE)})"
    iso_timestamp = f"regexp_full_match({trimmed}, {quote_literal(ISO_TIMESTAMP)})"
    stamp = quote_literal("%Y-%m-%d %H:%M:%S")
    stats = con.execute(
        f"""
        SELECT
            count({column}),
            count(DISTINCT {column}),
            count(*) FILTER (WHERE {column} IS NOT NULL AND {trimmed} = ''),
            count(*) FILTER (WHERE {text} <> {trimmed}),
            count(DISTINCT lower({trimmed})) FILTER (WHERE {trimmed} <> ''),
            count(DISTINCT {text}) FILTER (WHERE {trimmed} <> ''),
            min(length({text})),
            max(length({text})),
            count(*) FILTER (WHERE regexp_full_match({trimmed}, {quote_literal(INTEGER)})),
            count(*) FILTER (WHERE regexp_full_match({trimmed}, {quote_literal(DECIMAL)})),
            count(*) FILTER (WHERE regexp_full_match({trimmed}, {quote_literal(LEADING_ZEROS)})),
            count(*) FILTER (WHERE lower({trimmed}) IN ('true', 'false')),
            count(*) FILTER (WHERE {iso_date} AND try_cast({trimmed} AS DATE) IS NOT NULL),
            count(*) FILTER (
                WHERE {iso_timestamp} AND try_cast({trimmed} AS TIMESTAMPTZ) IS NOT NULL
            ),
            min(try_cast({trimmed} AS DOUBLE)),
            max(try_cast({trimmed} AS DOUBLE)),
            CAST(min(try_cast({trimmed} AS DATE)) FILTER (WHERE {iso_date}) AS VARCHAR),
            CAST(max(try_cast({trimmed} AS DATE)) FILTER (WHERE {iso_date}) AS VARCHAR),
            strftime(min(try_cast({trimmed} AS TIMESTAMPTZ)) FILTER (WHERE {iso_timestamp}), {stamp}),
            strftime(max(try_cast({trimmed} AS TIMESTAMPTZ)) FILTER (WHERE {iso_timestamp}), {stamp}),
            {date_counts}
        FROM source
        """
    ).fetchone()
    (
        non_null,
        distinct,
        blanks,
        padded,
        normalised_distinct,
        raw_distinct,
        min_length,
        max_length,
        integers,
        decimals,
        leading_zeros,
        booleans,
        iso_dates,
        iso_timestamps,
        numeric_min,
        numeric_max,
        date_min,
        date_max,
        timestamp_min,
        timestamp_max,
        *date_matches,
    ) = stats
    filled = non_null - blanks
    native = native_type.upper()
    is_text = native.startswith("VARCHAR")
    all_digits = bool(filled) and integers == filled

    converts_to = native_type.lower()
    date_readings: list[str] = []
    if is_text:
        converts_to = "text"
        if filled:
            if all_digits and not leading_zeros:
                converts_to = "integer"
            elif all_digits:
                pass  # digits with leading zeros are an identifier, not a number
            elif decimals == filled and not leading_zeros:
                converts_to = "decimal"
            elif booleans == filled:
                converts_to = "boolean"
            elif iso_dates == filled:
                converts_to = "date"
            elif iso_timestamps == filled:
                converts_to = "timestamp"
            else:
                date_readings = [
                    label
                    for (_, label), matched in zip(DATE_FORMATS, date_matches)
                    if matched == filled
                ]
                if date_readings:
                    converts_to = "date"

    result = {
        "name": name,
        "native_type": native_type,
        "converts_to": converts_to,
        "nulls": rows - non_null,
        "null_rate": round((rows - non_null) / rows, 6) if rows else 0.0,
        "blanks": blanks,
        "distinct": distinct,
        "unique": rows > 0 and non_null == rows and distinct == rows,
        "min_length": min_length,
        "max_length": max_length,
    }

    notes: list[str] = []
    if is_text and padded:
        notes.append(f"{counted(padded, 'value')} with leading or trailing spaces")
    if is_text and converts_to == "text" and normalised_distinct < raw_distinct:
        notes.append(
            f"{raw_distinct} distinct values become {normalised_distinct} "
            "when trimmed and lower-cased"
        )
    if is_text and all_digits and leading_zeros:
        notes.append(
            f"every value is digits, {leading_zeros} with leading zeros; "
            "kept as text so the zeros are not lost"
        )
    is_integer = converts_to == "integer" or native.startswith(INTEGER_TYPES)
    if (is_integer or converts_to == "decimal" or native.startswith(NUMERIC_TYPES)) and (
        numeric_min is not None
    ):
        result["min"] = number(numeric_min)
        result["max"] = number(numeric_max)
        low, high = UNIX_SECONDS
        if is_integer and low <= numeric_min and numeric_max <= high:
            notes.append("every value fits a Unix timestamp in seconds")
    elif converts_to == "timestamp" or native.startswith("TIMESTAMP"):
        if timestamp_min is not None:
            result["min"] = f"{timestamp_min} UTC"
            result["max"] = f"{timestamp_max} UTC"
    elif (converts_to == "date" or native.startswith("DATE")) and date_min is not None:
        result["min"] = date_min
        result["max"] = date_max
    if len(date_readings) > 1:
        notes.append("dates are ambiguous: every value reads as " + " and as ".join(date_readings))
    elif date_readings:
        notes.append(f"dates are written {date_readings[0]}")

    if converts_to == "text" and is_text:
        placeholders = con.execute(
            f"""
            SELECT lower({trimmed}) AS value, count(*) AS n
            FROM source
            WHERE lower({trimmed}) IN ({", ".join(quote_literal(p) for p in PLACEHOLDERS)})
            GROUP BY 1 ORDER BY 1
            """
        ).fetchall()
        if placeholders:
            listed = ", ".join(f"{value} ({count})" for value, count in placeholders)
            notes.append(f"placeholder values: {listed}")
            result["placeholders"] = {value: count for value, count in placeholders}

    if not result["unique"] and non_null:
        top = con.execute(
            f"""
            SELECT {text} AS value, count(*) AS n
            FROM source WHERE {column} IS NOT NULL
            GROUP BY 1 ORDER BY n DESC, value LIMIT {TOP_VALUES}
            """
        ).fetchall()
        result["top_values"] = [{"value": value, "count": count} for value, count in top]
    result["notes"] = notes
    return result


def profile_table(con: duckdb.DuckDBPyConnection, path: Path, root: Path) -> dict:
    """Profile one file. Raises ProfileError if it cannot be read."""
    try:
        con.execute(f"CREATE OR REPLACE TEMP VIEW source AS SELECT * FROM {relation(path)}")
        described = con.execute("DESCRIBE source").fetchall()
        rows = con.execute("SELECT count(*) FROM source").fetchone()[0]
        distinct_rows = con.execute(
            "SELECT count(*) FROM (SELECT DISTINCT * FROM source)"
        ).fetchone()[0]
    except duckdb.Error as error:
        raise ProfileError(f"could not read {path.relative_to(root)}: {error}") from error

    columns = [profile_column(con, name, native, rows) for name, native, *_ in described]
    file_records = count_file_records(path)
    notes: list[str] = []
    if file_records is not None and file_records != rows:
        notes.append(
            f"the file has {file_records} records but {rows} rows were loaded; "
            "check for dropped rows or line breaks inside quoted fields"
        )
    if rows != distinct_rows:
        notes.append(f"{counted(rows - distinct_rows, 'exact duplicate row')}")
    return {
        "table": table_name(path, root),
        "file": path.relative_to(root).as_posix(),
        "format": FORMATS[path.suffix.lower()],
        "rows": rows,
        "file_records": file_records,
        "duplicate_rows": rows - distinct_rows,
        "key_candidates": [column["name"] for column in columns if column["unique"]],
        "columns": columns,
        "notes": notes,
    }


def render(profile: dict) -> str:
    """The compact text profile: one line per column, then the notes."""
    lines = [
        f"table: {profile['table']}",
        f"file: {profile['file']} ({profile['format']})",
        f"rows: {profile['rows']}",
    ]
    if profile["file_records"] is not None:
        lines.append(f"records in file: {profile['file_records']}")
    keys = ", ".join(profile["key_candidates"]) or "none"
    lines.append(f"key candidates: {keys}")
    lines.append("")
    lines.append("columns:")
    width = max(len(column["name"]) for column in profile["columns"])
    for column in profile["columns"]:
        parts = [
            f"  {column['name']:<{width}}",
            column["converts_to"],
            f"nulls {column['nulls']} ({column['null_rate']:.1%})"
            if column["nulls"]
            else "nulls 0",
            f"distinct {column['distinct']}",
        ]
        if column["blanks"]:
            parts.append(f"blank {column['blanks']}")
        if column["unique"]:
            parts.append("unique")
        if "min" in column:
            parts.append(f"range {column['min']} to {column['max']}")
        elif column["converts_to"] == "text" and column["min_length"] is not None:
            parts.append(f"length {column['min_length']} to {column['max_length']}")
        if "top_values" in column:
            top = ", ".join(
                f"{shorten(entry['value'])} ({entry['count']})" for entry in column["top_values"]
            )
            parts.append(f"top: {top}")
        lines.append("  ".join(parts))
    notes = list(profile["notes"])
    for column in profile["columns"]:
        notes.extend(f"{column['name']}: {note}" for note in column["notes"])
    if notes:
        lines.append("")
        lines.append("notes:")
        lines.extend(f"  - {note}" for note in notes)
    return "\n".join(lines) + "\n"


def profile_folder(root: Path, out: Path) -> list[dict]:
    """Profile every supported file under root and write the results to out."""
    written = out.resolve()
    files = [path for path in discover(root) if written not in path.resolve().parents]
    if not files:
        raise ProfileError(f"no CSV, JSON or Parquet files found under {root}")
    names: dict[str, Path] = {}
    for path in files:
        name = table_name(path, root)
        if name in names:
            raise ProfileError(
                f"{names[name].relative_to(root)} and {path.relative_to(root)} "
                f"would both be profiled as {name}; rename one of them"
            )
        names[name] = path
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    profiles = [profile_table(con, path, root) for path in files]
    con.close()
    out.mkdir(parents=True, exist_ok=True)
    for profile in profiles:
        artefacts.write_text(out / f"{profile['table']}.txt", render(profile))
        artefacts.write_text(
            out / f"{profile['table']}.json", json.dumps(profile, indent=2, sort_keys=True) + "\n"
        )
    return profiles


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("root", type=Path, help="folder of raw CSV, JSON or Parquet files")
    parser.add_argument("--out", type=Path, default=Path("profiles"))
    args = parser.parse_args(argv)
    try:
        profiles = profile_folder(args.root, args.out)
    except ProfileError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    for profile in profiles:
        keys = ", ".join(profile["key_candidates"]) or "none"
        print(f"{profile['table']}: {profile['rows']} rows, key candidates: {keys}")
    print(f"Wrote {len(profiles)} profiles to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
