"""Record what each role in a run used: tokens, turns, time and outcome.

Each role runs as its own headless Claude Code session, which ends by
printing a JSON report. The ledger reads that report:

- tokens come from ``modelUsage``, one entry per model, because the
  report's ``usage`` counts only the main loop and leaves out other
  requests the session made;
- cost is ``total_cost_usd``, Claude Code's own estimate at list prices.
  It is not a bill, and on a subscription it is not what is charged. When
  Claude Code does not know a model's price the cost is treated as unknown;
- turns, time, refused tool calls and how the session ended are read from
  the same report.

A role that reported nothing (it was stopped, or crashed before printing a
report) is still recorded, with its outcome named and no figures.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelUsage:
    model: str
    input: int | None
    cache_write: int | None
    cache_read: int | None
    output: int | None
    cost_basis: str | None = None

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "input_tokens": self.input,
            "cache_write_tokens": self.cache_write,
            "cache_read_tokens": self.cache_read,
            "output_tokens": self.output,
            "cost_basis": self.cost_basis,
        }


@dataclass(frozen=True)
class RoleUsage:
    role: str
    outcome: str
    turns: int | None = None
    duration_ms: int | None = None
    api_duration_ms: int | None = None
    cost_usd: float | None = None
    refused: int | None = None
    models: tuple[ModelUsage, ...] = ()

    @property
    def cost_known(self) -> bool:
        """True when Claude Code reported a cost and knew every model's price."""
        return self.cost_usd is not None and all(
            model.cost_basis != "unknown" for model in self.models
        )

    def to_dict(self) -> dict:
        return {
            "role": self.role,
            "outcome": self.outcome,
            "turns": self.turns,
            "duration_ms": self.duration_ms,
            "api_duration_ms": self.api_duration_ms,
            "cost_usd": self.cost_usd,
            "cost_known": self.cost_known,
            "refused_tool_calls": self.refused,
            "models": [model.to_dict() for model in self.models],
        }


def _count(value) -> int | None:
    # bool is a subclass of int in Python; a true/false here is not a count.
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _amount(value) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return float(value)
    return None


def _models(report: dict) -> tuple[ModelUsage, ...]:
    entries = report.get("modelUsage")
    if not isinstance(entries, dict):
        return ()
    models = []
    for name, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        basis = entry.get("costBasis")
        models.append(
            ModelUsage(
                model=str(name),
                input=_count(entry.get("inputTokens")),
                cache_write=_count(entry.get("cacheCreationInputTokens")),
                cache_read=_count(entry.get("cacheReadInputTokens")),
                output=_count(entry.get("outputTokens")),
                cost_basis=basis if isinstance(basis, str) else None,
            )
        )
    return tuple(models)


def role_usage(role: str, report: dict | None, missing: str = "no report") -> RoleUsage:
    """What one role used, from the JSON report its Claude Code session printed.

    ``missing`` names the outcome when there is no report, for example
    "timed out".
    """
    if not isinstance(report, dict):
        return RoleUsage(role=role, outcome=missing)
    subtype = report.get("subtype")
    denials = report.get("permission_denials")
    return RoleUsage(
        role=role,
        outcome=subtype if isinstance(subtype, str) and subtype else "unknown",
        turns=_count(report.get("num_turns")),
        duration_ms=_count(report.get("duration_ms")),
        api_duration_ms=_count(report.get("duration_api_ms")),
        cost_usd=_amount(report.get("total_cost_usd")),
        refused=len(denials) if isinstance(denials, list) else None,
        models=_models(report),
    )
