"""How much a run may spend, and how each agent call is paid for.

Claude Code stops an agent once its estimated spend reaches the cap given
with ``--max-budget-usd``, but it checks after each model response, so a
call can pass its cap by up to one response. Checking a run's total before
each call therefore does not cap the run. Instead, before a call starts its
whole cap is reserved, and the call starts only if what was spent, what is
still reserved and the new reservation together fit the run's ceiling.
Afterwards the reservation is settled with the call's measured cost, or with
the whole reservation when the call reported none.

Amounts are decimals, never binary floating point, so that sums are exact
and a call that fits the ceiling exactly is not refused by a rounding error.

On a Claude Code sign-in the dollar figures are Claude Code's own estimates,
not bills.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

ENGINEER = "engineer"
CROSS_SOURCE = "cross-source"
SCOUT = "scout"
KINDS = (ENGINEER, CROSS_SOURCE, SCOUT)

# Plain decimal text: digits, and at most four places after the point.
_AMOUNT = re.compile(r"\d+(\.\d{1,4})?")


class PolicyError(Exception):
    """A policy is malformed, or asks for something it cannot allow."""


def amount(value, what: str = "amount") -> Decimal:
    """A non-negative amount of US dollars, from a number or decimal text.

    A float is read through its shortest text form, so 0.1 is 0.1 and not the
    binary value nearest to it.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise PolicyError(f"{what} must be a number of US dollars, not {value!r}")
    text = str(value).strip() if not isinstance(value, Decimal) else format(value, "f")
    if not _AMOUNT.fullmatch(text):
        raise PolicyError(
            f"{what} must be plain decimal US dollars of at least 0, "
            f"with at most four places, not {value!r}"
        )
    return Decimal(text)


def _per_kind(value, what: str) -> dict[str, Decimal]:
    if not isinstance(value, dict):
        raise PolicyError(f"{what} must be an object naming each kind of agent")
    unknown = sorted(set(value) - set(KINDS))
    if unknown:
        raise PolicyError(f"{what} names unknown kinds of agent: {', '.join(unknown)}")
    missing = [kind for kind in KINDS if kind not in value]
    if missing:
        raise PolicyError(f"{what} is missing: {', '.join(missing)}")
    return {kind: amount(value[kind], f"{what}.{kind}") for kind in KINDS}


@dataclass(frozen=True)
class Policy:
    """The ceilings a run works within, and the figures used to estimate cost."""

    # The most one run may spend, all its agent calls together.
    run_usd: Decimal = Decimal("2.00")
    # Each call's cap, passed to Claude Code and reserved before the call.
    call_usd: dict[str, Decimal] = field(
        default_factory=lambda: {
            ENGINEER: Decimal("0.75"),
            CROSS_SOURCE: Decimal("0.40"),
            SCOUT: Decimal("0.10"),
        }
    )
    # What a call usually costs, measured on the demo data and rounded up;
    # used only to estimate a comparison before it starts.
    typical_usd: dict[str, Decimal] = field(
        default_factory=lambda: {
            ENGINEER: Decimal("0.20"),
            CROSS_SOURCE: Decimal("0.08"),
            SCOUT: Decimal("0.02"),
        }
    )
    # A comparison estimated above this needs an explicit approval to start.
    approve_above_usd: Decimal = Decimal("5.00")

    def __post_init__(self):
        for kind, cap in self.call_usd.items():
            if cap > self.run_usd:
                raise PolicyError(
                    f"a {kind} call may spend US${cap}, more than the run's ceiling of "
                    f"US${self.run_usd}, so it could never start"
                )

    def cap(self, kind: str) -> Decimal:
        """The cap of one call of this kind of agent."""
        if kind not in self.call_usd:
            raise PolicyError(f"no cap for {kind!r}; the kinds are {', '.join(KINDS)}")
        return self.call_usd[kind]

    def to_dict(self) -> dict:
        return {
            "run_usd": str(self.run_usd),
            "call_usd": {kind: str(cap) for kind, cap in self.call_usd.items()},
            "typical_usd": {kind: str(cost) for kind, cost in self.typical_usd.items()},
            "approve_above_usd": str(self.approve_above_usd),
        }


FIELDS = ("run_usd", "call_usd", "typical_usd", "approve_above_usd")


def from_dict(document) -> Policy:
    """A policy from a JSON object; fields left out keep their defaults."""
    if not isinstance(document, dict):
        raise PolicyError("a policy must be a JSON object")
    unknown = sorted(set(document) - set(FIELDS))
    if unknown:
        raise PolicyError(f"unknown policy fields: {', '.join(unknown)}")
    given = {}
    for name in ("run_usd", "approve_above_usd"):
        if name in document:
            given[name] = amount(document[name], name)
    for name in ("call_usd", "typical_usd"):
        if name in document:
            given[name] = _per_kind(document[name], name)
    return Policy(**given)


def load(path: Path) -> Policy:
    """Read a policy file."""
    try:
        document = json.loads(Path(path).read_text())
    except OSError as error:
        raise PolicyError(f"cannot read the policy {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise PolicyError(f"the policy {path} is not JSON: {error}") from error
    return from_dict(document)
