"""Score an architect's contract against a declaration written apart from it.

The architect writes a contract for the engineer; the gate checks the
engineer's project against a declaration a person wrote and the agents
never see. Comparing the two says how close the spec came to what was
wanted, model by model, on what both describe:

- the model is in the contract;
- its key, as a set of columns (the gate checks the columns together);
- its source table;
- the rows it keeps, as a count on the same raw files, so two rules written
  differently that leave out the same rows agree;
- its money totals, after the gate reads both (a source column or grouping
  left unsaid is the model's own name).

A declared reference query (``expected``, such as how two systems are
matched) has no counterpart in a contract, so it is not scored here; the
gate's own check judges it. Contract models the declaration does not name
are listed as extra.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import duckdb

from tenx import artefacts
from tenx.contract import to_declaration
from tenx.gate import ModelSpec, Total, expected_rows, parse_declaration, source_files

ARCHITECT_DIR = "architect"
CONTRACT_FILE = "contract.json"
COUNTS_FILE = "counts.json"


@dataclass(frozen=True)
class ModelAgreement:
    """One declared model: what about it the contract says otherwise."""

    model: str
    differences: tuple[str, ...]

    @property
    def agrees(self) -> bool:
        return not self.differences


@dataclass(frozen=True)
class Agreement:
    """Every declared model's agreement, and the contract's models beyond them."""

    models: tuple[ModelAgreement, ...]
    extra: tuple[str, ...]

    @property
    def agreeing(self) -> int:
        return sum(model.agrees for model in self.models)

    def describe(self) -> str:
        text = f"{self.agreeing} of {len(self.models)} models agree"
        parts = [f"{m.model}: {', '.join(m.differences)}" for m in self.models if not m.agrees]
        if self.extra:
            parts.append(f"extra: {', '.join(self.extra)}")
        return "; ".join([text, *parts])

    def to_dict(self) -> dict:
        return {
            "agreeing": self.agreeing,
            "declared": len(self.models),
            "models": {m.model: list(m.differences) for m in self.models},
            "extra": list(self.extra),
        }


def _columns(names) -> str:
    return ", ".join(sorted(names)) or "none"


def _totals(totals: tuple[Total, ...]) -> set[tuple]:
    return {
        (
            total.column.lower(),
            total.source_column.lower(),
            (total.by or "").lower(),
            (total.source_by or "").lower(),
        )
        for total in totals
    }


def _money(totals: set[tuple]) -> str:
    shown = []
    for column, source, by, source_by in sorted(totals):
        text = column if column == source else f"{column} from {source}"
        if by:
            text += f" by {by}" if by == source_by else f" by {by} from {source_by}"
        shown.append(text)
    return "; ".join(shown) or "none"


def _differences(spec: ModelSpec, declared: ModelSpec, kept: int, wanted: int) -> list[str]:
    found = []
    key, wanted_key = {c.lower() for c in spec.key}, {c.lower() for c in declared.key}
    if key != wanted_key:
        found.append(f"key {_columns(key)} not {_columns(wanted_key)}")
    if spec.source != declared.source:
        found.append(f"source {spec.source} not {declared.source}")
    if kept != wanted:
        found.append(f"rows to keep {kept} not {wanted}")
    totals, wanted_totals = _totals(spec.totals), _totals(declared.totals)
    if totals != wanted_totals:
        found.append(f"money {_money(totals)} not {_money(wanted_totals)}")
    return found


def score(
    contract: dict,
    kept: Mapping[str, int],
    declared: list[ModelSpec],
    wanted: Mapping[str, int],
) -> Agreement:
    """Compare a checked contract, with the rows each model keeps, with a declaration.

    ``kept`` is what each contract model keeps of its source, as counted for
    the spec; ``wanted`` is the declaration's own expected rows for each of
    its models, on the same raw files.
    """
    specs = {model.name: model for model in parse_declaration(to_declaration(contract))}
    models = []
    for model in declared:
        spec = specs.get(model.name)
        if spec is None:
            models.append(ModelAgreement(model.name, ("not in the spec",)))
            continue
        found = _differences(spec, model, kept[model.name], wanted[model.name])
        models.append(ModelAgreement(model.name, tuple(found)))
    names = {model.name for model in declared}
    return Agreement(tuple(models), tuple(name for name in specs if name not in names))


def declared_rows(declared: list[ModelSpec], raw: Path) -> dict[str, int]:
    """The rows each declared model must hold, as the gate works them out."""
    files = source_files(Path(raw))
    con = duckdb.connect()
    try:
        return {
            model.name: expected_rows(con, model, files[model.source]).expected_rows
            for model in declared
        }
    finally:
        con.close()


def last_contract(run: Path) -> tuple[dict, dict[str, int]] | None:
    """The contract and rows kept from the architect's last attempt, if it gave a usable one."""
    numbers = artefacts.attempt_numbers(Path(run), ARCHITECT_DIR)
    if not numbers:
        return None
    folder = Path(run) / ARCHITECT_DIR / f"{artefacts.ATTEMPT_PREFIX}{max(numbers)}"
    try:
        contract = json.loads((folder / CONTRACT_FILE).read_text(encoding="utf-8"))
        counts = json.loads((folder / COUNTS_FILE).read_text(encoding="utf-8"))
        return contract, {name: rows["kept"] for name, rows in counts.items()}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
