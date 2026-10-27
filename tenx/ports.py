"""What the pipeline needs from a platform, named for the capability, not the tool.

The steps of a run talk to these interfaces only. Each interface has one or
more adapters that do the work on a particular platform: today DuckDB and
dbt for sources, builds and checks, and Claude Code for agents. Another
platform is a new adapter; the steps do not change.

An adapter states what it can do with ``capabilities()``. Asking for
something it cannot do raises ``UnsupportedCapability``, whose message says
so in plain words, rather than failing part way through.

This module imports nothing that reaches a database or a transform tool, so
that the code built on it cannot reach one by accident.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "BUILD",
    "OVERLAP",
    "PROFILE",
    "RUN_AGENT",
    "SCAFFOLD",
    "STRUCTURED_ANSWER",
    "VALIDATE",
    "AgentRun",
    "BuildResult",
    "CheckResult",
    "Role",
    "UnsupportedCapability",
    "ValidationReport",
    "require",
]

# Capabilities, each named for what it does.
PROFILE = "profile"
OVERLAP = "overlap"
SCAFFOLD = "scaffold"
BUILD = "build"
VALIDATE = "validate"
RUN_AGENT = "run agent"
STRUCTURED_ANSWER = "structured answer"


class UnsupportedCapability(Exception):
    """An adapter was asked for something its platform cannot do."""

    def __init__(self, capability: str, implementation: str) -> None:
        super().__init__(f"{implementation} cannot {capability}")
        self.capability = capability
        self.implementation = implementation


def require(port: object, capability: str) -> None:
    """Refuse, with the reason, unless ``port`` declares ``capability``."""
    if capability not in port.capabilities():
        raise UnsupportedCapability(capability, port.name)


@dataclass(frozen=True)
class Role:
    """One agent of the add-on, and how it is started."""

    name: str
    agent: str
    prompt: str
    allowed_tools: tuple[str, ...]
    max_turns: int
    json_schema: dict | None = None


def _duration(seconds: float) -> str:
    if seconds >= 60 and seconds % 60 == 0:
        minutes = int(seconds // 60)
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    return f"{seconds:g} seconds"


@dataclass(frozen=True)
class AgentRun:
    """How one agent session ended, and what it reported."""

    exit_code: int | None
    report: dict | None
    timed_out_after: float | None = None
    message: str = ""

    def describe(self) -> str:
        if self.timed_out_after is not None:
            return f"stopped: did not finish within {_duration(self.timed_out_after)}"
        if self.report is None:
            reason = f": {self.message}" if self.message else ""
            return f"exited {self.exit_code} without a report{reason}"
        outcome = self.report.get("subtype") or "unknown"
        turns = self.report.get("num_turns")
        detail = f"{outcome} after {turns} turns" if turns is not None else outcome
        return f"{detail} (exit {self.exit_code})"


@dataclass(frozen=True)
class CheckResult:
    """One check of a validation: what was checked, on what, and how it went."""

    check: str
    subject: str
    status: str
    detail: str


@dataclass(frozen=True)
class ValidationReport:
    """Every check of one validation, its verdict, and the report as text."""

    results: tuple[CheckResult, ...]
    passed: bool
    text: str


@dataclass(frozen=True)
class BuildResult:
    """What one build of a project gave: its exit status, results and output."""

    exit_code: int
    results: dict | None
    output: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0
