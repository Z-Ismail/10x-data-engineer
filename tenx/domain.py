"""The records a run passes between its steps, and their JSON form.

A run's graph state is saved after every step and read back on resume. It
holds only JSON values: a saved object of a class of ours would be rebuilt
by importing and calling that class, which the checkpoint library warns it
will stop doing, and in its strict mode such an object comes back as a plain
dictionary instead. So each record here is written into the state as JSON
with ``dump()`` and checked again with ``load()`` by the step that reads it.

Reading is strict: a field of the wrong kind (``true`` or ``"5"`` for a
count), a field that is not part of the record, a missing field or an
unknown choice is refused with the record and field named. Money is decimal
text that follows the spending policy's rule, never a float.
"""

from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    model_validator,
)
from pydantic import ValidationError as _PydanticError

from tenx import artefacts
from tenx.ledger import RoleUsage
from tenx.policy import PolicyError, Settlement, amount

if TYPE_CHECKING:
    from typing_extensions import Self

__all__ = [
    "AgentFinding",
    "ArtefactRef",
    "Cost",
    "CostRecord",
    "Evidence",
    "ModelTokens",
    "Record",
    "RecordError",
    "Usd",
]


class RecordError(ValueError):
    """A value is not a valid record of the kind it was read as."""


def _usd(value) -> Decimal:
    try:
        return amount(value)
    except PolicyError as error:
        raise ValueError(str(error)) from None


# US dollars: plain decimal text of at least 0 with at most four places, as
# the spending policy reads amounts; written back as the same text.
Usd = Annotated[
    Decimal,
    BeforeValidator(_usd),
    PlainSerializer(lambda value: format(value, "f"), return_type=str),
]


def _cost(value) -> Decimal:
    # A measured cost keeps every place it was reported with. A float is read
    # through its shortest text, as the policy reads costs from reports.
    # Pydantic reports only ValueError as a field's error, so no TypeError here.
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise ValueError(f"a cost must be a number of US dollars, not {value!r}")  # noqa: TRY004
    if isinstance(value, float):
        value = Decimal(str(value))
    text = format(value, "f") if isinstance(value, Decimal) else str(value).strip()
    if not re.fullmatch(r"\d+(\.\d+)?", text):
        raise ValueError(f"a cost must be plain decimal US dollars of at least 0, not {value!r}")
    return Decimal(text)


# A measured cost in US dollars: plain decimal text of at least 0, any places.
Cost = Annotated[
    Decimal,
    BeforeValidator(_cost),
    PlainSerializer(lambda value: format(value, "f"), return_type=str),
]
Text = Annotated[str, Field(min_length=1)]
Count = Annotated[int, Field(ge=0)]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def _describe(name: str, error: _PydanticError) -> str:
    problems = []
    for item in error.errors(include_url=False):
        where = ".".join(str(part) for part in item["loc"]) or "(the record)"
        problems.append(f"{where}: {item['msg']}")
    return f"not a valid {name}: " + "; ".join(problems)


class Record(BaseModel):
    """A record that is immutable, strict, and kept in the state as JSON."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    def dump(self) -> dict:
        """The record as JSON values, as it is kept in the graph state."""
        return json.loads(self.model_dump_json())

    @classmethod
    def load(cls, value) -> Self:
        """Read a record back from its JSON form, refusing anything else."""
        try:
            text = json.dumps(value, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise RecordError(f"not a valid {cls.__name__}: not JSON ({error})") from None
        try:
            return cls.model_validate_json(text, strict=True)
        except _PydanticError as error:
            raise RecordError(_describe(cls.__name__, error)) from None


def _folder_hash(folder: Path) -> str:
    files = {
        path.relative_to(folder).as_posix(): artefacts.file_hash(path)
        for path in sorted(folder.rglob("*"))
        if path.is_file() and not artefacts.is_temporary(path)
    }
    return hashlib.sha256(artefacts.canonical(files)).hexdigest()


class ArtefactRef(Record):
    """A file or folder a step made, where it is, and the hash of what it held."""

    name: Text
    path: Text
    kind: Literal["file", "folder"]
    sha256: Sha256
    # The attempt of the step that made it; 0 for a step that has no attempts.
    attempt: int = Field(ge=0)

    @classmethod
    def of(cls, name: str, path: Path, attempt: int = 0) -> ArtefactRef:
        """Refer to ``path`` as it is now. A folder's hash covers each file and its place."""
        path = Path(path)
        if path.is_dir():
            kind, digest = "folder", _folder_hash(path)
        elif path.is_file():
            kind, digest = "file", artefacts.file_hash(path)
        else:
            raise FileNotFoundError(f"artefact {name!r} not found: {path}")
        return cls(name=name, path=str(path), kind=kind, sha256=digest, attempt=attempt)

    def unchanged(self) -> bool:
        """True when the file or folder still holds what it held when referred to."""
        path = Path(self.path)
        if self.kind == "folder":
            return path.is_dir() and _folder_hash(path) == self.sha256
        return path.is_file() and artefacts.file_hash(path) == self.sha256


class Evidence(Record):
    """What a finding rests on: a file, a query and its result, or a check."""

    kind: Literal["file", "query", "check"]
    # The file's path, the query's text or the check's name.
    ref: Text
    # What it showed: the lines read, the query's result or the check's detail.
    detail: str = ""


class AgentFinding(Record):
    """One claim an agent made, how sure it is, and what it rests on."""

    agent: Text
    claim: Text
    kind: Literal["fact", "inference", "hypothesis", "recommendation"]
    evidence: tuple[Evidence, ...] = ()
    confidence: Literal["high", "medium", "low"]
    # Tables, columns or models the claim is about.
    assets: tuple[Text, ...] = ()
    next_action: str = ""
    risks: tuple[Text, ...] = ()
    open_questions: tuple[Text, ...] = ()


class ModelTokens(Record):
    """The tokens one model used in an agent call, by type."""

    model: Text
    input: Count | None
    cache_write: Count | None
    cache_read: Count | None
    output: Count | None
    cost_basis: str | None = None


class CostRecord(Record):
    """What one agent call used and cost, and what the run was charged for it.

    A figure that was not reported is null, never 0. On a Claude Code sign-in
    the cost is Claude Code's estimate, not a bill.
    """

    run_id: Text
    node: Text
    role: Text
    attempt: Count
    backend: Text
    outcome: Text
    turns: Count | None = None
    duration_ms: Count | None = None
    refused_calls: Count | None = None
    models: tuple[ModelTokens, ...] = ()
    estimated_usd: Cost | None = None
    cost_basis: Literal["estimate", "billed", "unknown"]
    # The call's reservation, what the run was charged and how far past its cap.
    reserved_usd: Usd | None = None
    charged_usd: Cost | None = None
    overshoot_usd: Cost | None = None

    @model_validator(mode="after")
    def _adds_up(self):
        settled = (self.reserved_usd, self.charged_usd, self.overshoot_usd)
        if all(value is None for value in settled):
            return self
        if any(value is None for value in settled):
            raise ValueError("a settled call needs its reservation, charge and overshoot")
        if self.estimated_usd is not None and self.cost_basis != "unknown":
            charged = self.estimated_usd
        else:
            charged = self.reserved_usd
        overshoot = max(charged - self.reserved_usd, Decimal(0))
        if (self.charged_usd, self.overshoot_usd) != (charged, overshoot):
            raise ValueError(
                f"charged {self.charged_usd} with overshoot {self.overshoot_usd} does not "
                f"follow from the cost and reservation (charged {charged}, overshoot {overshoot})"
            )
        return self

    @classmethod
    def of(
        cls,
        run_id: str,
        node: str,
        attempt: int,
        backend: str,
        usage: RoleUsage,
        settlement: Settlement | None = None,
    ) -> CostRecord:
        """The record of one call, from its ledger usage and its settlement if any."""
        known = usage.cost_known
        settled = {}
        if settlement is not None:
            settled = {
                "reserved_usd": settlement.reservation.cap,
                "charged_usd": settlement.charged_usd,
                "overshoot_usd": settlement.overshoot_usd,
            }
        return cls(
            run_id=run_id,
            node=node,
            role=usage.role,
            attempt=attempt,
            backend=backend,
            outcome=usage.outcome,
            turns=usage.turns,
            duration_ms=usage.duration_ms,
            refused_calls=usage.refused,
            models=tuple(
                ModelTokens(
                    model=model.model,
                    input=model.input,
                    cache_write=model.cache_write,
                    cache_read=model.cache_read,
                    output=model.output,
                    cost_basis=model.cost_basis,
                )
                for model in usage.models
            ),
            estimated_usd=Decimal(str(usage.cost_usd)) if known else None,
            cost_basis="estimate" if known else "unknown",
            **settled,
        )
