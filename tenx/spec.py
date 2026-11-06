"""Write a checked contract, with what it keeps of the raw files, as SPEC.md.

The row counts come from ``tenx.counting``, which runs each model's rules
for leaving rows out on its raw table, apart from the run, so that the spec
states how many rows each rule removes and how many the model must hold.
This module reaches no platform, so the graph can write the spec itself.

SPEC.md is the contract written out for people and for the engineer: a
section per model with its grain, key, source, the rows left out and why,
the money that must add up, and where each column comes from.
"""

from __future__ import annotations

from collections.abc import Mapping

from tenx.ports import KeptRows


def counted(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


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


def _model(model: dict, rows: KeptRows) -> list[str]:
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
    lines.append(f"- Rows to keep: {rows.kept}")
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


def render(contract: dict, counts: Mapping[str, KeptRows]) -> str:
    """The contract as SPEC.md, with the row counts from ``tenx.counting.count_kept``."""
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
