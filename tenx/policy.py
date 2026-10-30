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
import threading
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


STOPPED = "stopped: budget"


class BudgetStop(Exception):
    """A call was not started, because its reservation does not fit the run's ceiling."""

    status = STOPPED


def measured(value) -> Decimal | None:
    """A cost from Claude Code's report as a decimal, or None when it is not a cost.

    The report's float is read through its shortest text form.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return Decimal(str(value))


@dataclass(frozen=True)
class Reservation:
    number: int
    role: str
    kind: str
    cap: Decimal


@dataclass(frozen=True)
class Settlement:
    """What one call was charged to the run, and how that figure was found."""

    reservation: Reservation
    # The cost Claude Code reported, when it reported one it knew.
    measured_usd: Decimal | None

    @property
    def charged_usd(self) -> Decimal:
        # With no known cost, the whole reservation is charged: the call may
        # have spent all of it, and nothing says it spent less.
        if self.measured_usd is None:
            return self.reservation.cap
        return self.measured_usd

    @property
    def overshoot_usd(self) -> Decimal:
        """How far the call passed its cap; Claude Code checks only after each response."""
        excess = self.charged_usd - self.reservation.cap
        return excess if excess > 0 else Decimal(0)

    def to_dict(self) -> dict:
        return {
            "role": self.reservation.role,
            "cap_usd": str(self.reservation.cap),
            "measured_usd": None if self.measured_usd is None else str(self.measured_usd),
            "charged_usd": str(self.charged_usd),
            "basis": "measured" if self.measured_usd is not None else "whole reservation",
            "overshoot_usd": str(self.overshoot_usd),
        }


class Budget:
    """One run's spending: reservations made before calls, settled after them.

    Safe to use from several threads: the check and the reservation happen
    together, so two calls cannot both take the last room under the ceiling.
    """

    def __init__(self, rules: Policy | None = None) -> None:
        self.policy = rules or Policy()
        self._lock = threading.Lock()
        self._open: dict[int, Reservation] = {}
        self._settled: list[Settlement] = []
        self._next = 0

    @property
    def spent_usd(self) -> Decimal:
        with self._lock:
            return sum((s.charged_usd for s in self._settled), Decimal(0))

    @property
    def reserved_usd(self) -> Decimal:
        with self._lock:
            return sum((r.cap for r in self._open.values()), Decimal(0))

    def reserve(self, role: str, kind: str) -> Reservation:
        """Reserve one call's whole cap, or raise BudgetStop before the call starts."""
        cap = self.policy.cap(kind)
        with self._lock:
            spent = sum((s.charged_usd for s in self._settled), Decimal(0))
            reserved = sum((r.cap for r in self._open.values()), Decimal(0))
            if spent + reserved + cap > self.policy.run_usd:
                raise BudgetStop(
                    f"{STOPPED}: {role} was not started; its cap of US${cap} with "
                    f"US${spent} spent and US${reserved} reserved would pass the run's "
                    f"ceiling of US${self.policy.run_usd}"
                )
            self._next += 1
            reservation = Reservation(self._next, role, kind, cap)
            self._open[reservation.number] = reservation
            return reservation

    def settle(self, reservation: Reservation, cost_usd, cost_known: bool) -> Settlement:
        """Replace a reservation with what the call cost.

        ``cost_usd`` is the cost Claude Code reported, and ``cost_known`` whether
        it knew every model's price; otherwise the whole reservation is charged.
        """
        with self._lock:
            if self._open.get(reservation.number) != reservation:
                raise ValueError(f"reservation {reservation.number} is not open")
            del self._open[reservation.number]
            settlement = Settlement(reservation, measured(cost_usd) if cost_known else None)
            self._settled.append(settlement)
            return settlement

    def restore(self, role: str, kind: str, cap, measured_usd) -> Settlement:
        """Put back a call settled before the run stopped, so a resumed run counts it.

        ``cap`` is what was reserved for it and ``measured_usd`` the known cost
        it was settled at, or None if it was charged its whole reservation.
        The call already ran, so the ceiling is not checked again; the next
        reservation is checked against it.
        """
        known = None if measured_usd is None else Decimal(str(measured_usd))
        if known is not None and known < 0:
            raise ValueError(f"a measured cost cannot be negative: {measured_usd}")
        with self._lock:
            self._next += 1
            reservation = Reservation(self._next, role, kind, amount(cap, f"{role} cap"))
            settlement = Settlement(reservation, known)
            self._settled.append(settlement)
            return settlement

    @property
    def settlements(self) -> tuple[Settlement, ...]:
        with self._lock:
            return tuple(self._settled)

    def to_dict(self) -> dict:
        with self._lock:
            settled = list(self._settled)
            still_open = list(self._open.values())
        spent = sum((s.charged_usd for s in settled), Decimal(0))
        return {
            "ceiling_usd": str(self.policy.run_usd),
            "spent_usd": str(spent),
            "reserved_usd": str(sum((r.cap for r in still_open), Decimal(0))),
            "overshoot_usd": str(sum((s.overshoot_usd for s in settled), Decimal(0))),
            "calls": [s.to_dict() for s in settled],
            "open": [{"role": r.role, "cap_usd": str(r.cap)} for r in still_open],
        }


def render(budget: Budget) -> str:
    """The run's spending as plain text: totals, a line per settled call, overshoots."""
    record = budget.to_dict()
    rows = [
        [
            call["role"],
            call["cap_usd"],
            "-" if call["measured_usd"] is None else call["measured_usd"],
            call["charged_usd"],
            call["basis"],
        ]
        for call in record["calls"]
    ]
    header = ["call", "cap", "measured", "charged", "charged as"]
    widths = [max(len(row[i]) for row in [header, *rows]) for i in range(len(header))]
    title = (
        f"budget (US$, Claude Code's own estimates): ceiling {record['ceiling_usd']}, "
        f"spent {record['spent_usd']}, still reserved {record['reserved_usd']}"
    )
    lines = [title, ""]
    for row in [header, *rows]:
        cells = [
            cell.ljust(width) if i in (0, 4) else cell.rjust(width)
            for i, (cell, width) in enumerate(zip(row, widths))
        ]
        lines.append("  ".join(cells).rstrip())
    over = [call for call in record["calls"] if Decimal(call["overshoot_usd"]) > 0]
    for call in over:
        lines.append(
            f"{call['role']} passed its cap by US${call['overshoot_usd']}: Claude Code "
            "checks the cap only after each model response"
        )
    return "\n".join(lines) + "\n"
