"""Count what a checked contract keeps of the raw files.

Each model's rules for leaving rows out are run on its raw table, as the
gate runs them, so that the spec states how many rows each rule removes and
how many the model must hold. A rule that does not run, such as one naming a
column the table does not have, makes the contract unusable.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

from tenx.contract import ContractError, to_declaration
from tenx.gate import (
    GateError,
    RowExpectation,
    expected_rows,
    parse_declaration,
    source_files,
)


def count_rows(contract: dict, raw: Path) -> dict[str, RowExpectation]:
    """For each model, its source rows, what each exclusion removes and what is kept."""
    try:
        files = source_files(Path(raw))
        models = parse_declaration(to_declaration(contract), "the contract")
    except GateError as error:
        raise ContractError(str(error)) from error
    counts = {}
    con = duckdb.connect()
    try:
        for model in models:
            if model.source not in files:
                raise ContractError(f"model {model.name}: no raw file for {model.source}")
            try:
                counts[model.name] = expected_rows(con, model, files[model.source])
            except GateError as error:
                raise ContractError(f"model {error}") from error
    finally:
        con.close()
    return counts
