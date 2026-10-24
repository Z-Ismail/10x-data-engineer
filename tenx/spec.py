"""Count what a checked contract keeps of the raw files, and write it as SPEC.md.

Each model's rules for leaving rows out are run on its raw table, as the
gate runs them, so that the spec states how many rows each rule removes and
how many the model must hold. A rule that does not run, such as one naming a
column the table does not have, makes the contract unusable.

SPEC.md is the contract written out for people and for the engineer: a
section per model with its grain, key, source, the rows left out and why,
the money that must add up, and where each column comes from.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

from tenx.contract import ContractError, to_declaration
from tenx.gate import (
    GateError,
    RowExpectation,
    counted,
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


def _line(text: str) -> str:
    return " ".join(text.split())


def _cell(text: str) -> str:
    return _line(text).replace("|", "\\|")


def _exclusion(exclusion: dict, removed: int) -> str:
    if exclusion["rule"] == "duplicate_rows":
        rule = "exact copies of another row; one copy is kept"
    else:
        rule = f"`{_line(exclusion['where'])}`"
    return (
        f"  - `{exclusion['name']}`, {counted(removed, 'row')}: "
        f"{_line(exclusion['reason'])} Rule: {rule}"
    )


def _total(total: dict) -> str:
    text = f"- Money: `{total['column']}` must add up to `{total['source_column']}` in the source"
    if total["by"]:
        text += f", for each `{total['by']}` (`{total['source_by']}` in the source)"
    return text


def _model(model: dict, rows: RowExpectation) -> list[str]:
    removed = dict(rows.excluded)
    lines = [
        f"## `{model['name']}` ({model['layer']})",
        "",
        _line(model["grain"]),
        "",
        f"- Source: `{model['source']}`, {counted(rows.source_rows, 'row')}",
        f"- Key: {', '.join(f'`{column}`' for column in model['key'])}",
    ]
    if model["exclusions"]:
        lines.append("- Leave out:")
        lines += [_exclusion(item, removed[item["name"]]) for item in model["exclusions"]]
    else:
        lines.append("- Leave out: nothing")
    lines.append(f"- Rows to keep: {rows.expected_rows}")
    lines += [_total(total) for total in model["totals"]]
    lines += ["", "| Column | From | Meaning |", "| --- | --- | --- |"]
    lines += [
        f"| `{column['name']}` | {', '.join(f'`{origin}`' for origin in column['from'])} "
        f"| {_cell(column['description'])} |"
        for column in model["columns"]
    ]
    return lines


def _items(items: list[str]) -> list[str]:
    return [f"- {_line(item)}" for item in items] or ["None."]


def render(contract: dict, counts: dict[str, RowExpectation]) -> str:
    """The contract as SPEC.md, with the row counts from count_rows."""
    lines = [
        "# Spec",
        "",
        "The models to build, one section each. Row counts are from the raw files",
        "as they were when the spec was written.",
    ]
    for model in contract["models"]:
        lines += ["", *_model(model, counts[model["name"]])]
    lines += ["", "## Assumptions", "", *_items(contract["assumptions"])]
    lines += ["", "## Questions for the requester", "", *_items(contract["questions"])]
    return "\n".join(lines) + "\n"
