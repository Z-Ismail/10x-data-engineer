"""Pick a few rows of a raw file, as written, for an agent to look at.

A profile gives exact figures but not what the values look like side by
side. A small sample shows that, such as an address in capitals or a
timestamp written two ways. Rows are taken in file order and picked by
reservoir sampling with a random generator seeded on the table's name, so
the same file always gives the same rows. DuckDB's own repeatable sample is
only guaranteed to repeat with one thread, which is why the choice is made
here.

The sample is written as JSON lines, one row per line:

- a JSON lines file gives its lines exactly as they are in the file;
- a CSV file is read as text, so padding, case and leading zeros show as
  they are in the file;
- a JSON array or Parquet file is read by DuckDB, so its values are shown as
  DuckDB reads them (a timestamp in DuckDB's own layout, for example).
"""

from __future__ import annotations

import json
import random
from collections.abc import Iterable
from pathlib import Path

import duckdb

from tenx.profile import FORMATS, relation

SAMPLE_ROWS = 20
BATCH = 10_000
JSON_LINES = {".jsonl", ".ndjson"}


def pick(items: Iterable, seed: str, size: int = SAMPLE_ROWS) -> list:
    """Up to size of the items, in their original order, the same for the same seed."""
    if size < 1:
        raise ValueError("size must be at least 1")
    generator = random.Random(seed)
    chosen: list[tuple[int, object]] = []
    for position, item in enumerate(items):
        if position < size:
            chosen.append((position, item))
        else:
            slot = generator.randrange(position + 1)
            if slot < size:
                chosen[slot] = (position, item)
    return [item for _, item in sorted(chosen, key=lambda entry: entry[0])]


def _lines(path: Path) -> Iterable[str]:
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if line.strip():
                yield line.rstrip("\r\n")


def _rows(path: Path) -> Iterable[str]:
    con = duckdb.connect()
    try:
        con.execute("SET TimeZone = 'UTC'")
        cursor = con.execute(f"SELECT * FROM {relation(path)}")
        names = [column[0] for column in cursor.description]
        while batch := cursor.fetchmany(BATCH):
            for row in batch:
                yield json.dumps(dict(zip(names, row)), default=str, ensure_ascii=False)
    finally:
        con.close()


def sample(path: Path, seed: str, size: int = SAMPLE_ROWS) -> str:
    """Up to size rows of the file as JSON lines, in file order."""
    path = Path(path)
    if path.suffix.lower() not in FORMATS:
        raise ValueError(f"{path} is not a CSV, JSON or Parquet file")
    rows = _lines(path) if path.suffix.lower() in JSON_LINES else _rows(path)
    return "".join(row + "\n" for row in pick(rows, seed, size))
