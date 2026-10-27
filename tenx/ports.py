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

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from tenx.sandbox import Mounts, Sandbox

__all__ = [
    "BUILD",
    "OVERLAP",
    "PROFILE",
    "RUN_AGENT",
    "SCAFFOLD",
    "STRUCTURED_ANSWER",
    "VALIDATE",
    "AgentBackend",
    "AgentRun",
    "BuildResult",
    "CheckResult",
    "Mounts",
    "PlatformError",
    "Role",
    "Sandbox",
    "SourceProfiler",
    "TransformEngine",
    "UnsupportedCapability",
    "ValidationReport",
    "Validator",
    "require",
]

# Capabilities, each named for what it does, so that a refusal reads as a sentence.
PROFILE = "profile sources"
OVERLAP = "compare columns across tables"
SCAFFOLD = "scaffold a project"
BUILD = "build a project"
VALIDATE = "validate a project"
RUN_AGENT = "run an agent"
STRUCTURED_ANSWER = "give a structured answer"


class UnsupportedCapability(Exception):
    """An adapter was asked for something its platform cannot do."""

    def __init__(self, capability: str, implementation: str) -> None:
        super().__init__(f"{implementation} cannot {capability}")
        self.capability = capability
        self.implementation = implementation


class PlatformError(Exception):
    """An adapter could not do what it was asked, for the reason given.

    Adapters raise this in place of their platform's own errors, so that the
    code calling them need not know which platform it is.
    """


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
    facts: dict = field(default_factory=dict)


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


@runtime_checkable
class SourceProfiler(Protocol):
    """Describes raw sources: one profile per table, and values tables share."""

    name: str

    def capabilities(self) -> frozenset[str]: ...

    def profile(self, raw: Path, out: Path) -> list[dict]:
        """Profile every table under ``raw``, write the profiles to ``out``, return them."""
        ...

    def overlap(self, raw: Path, out: Path) -> dict:
        """Compare columns across the tables under ``raw``; write the report to ``out``."""
        ...


@runtime_checkable
class TransformEngine(Protocol):
    """Turns a project of models into tables."""

    name: str

    def capabilities(self) -> frozenset[str]: ...

    def scaffold(self, project: Path, raw: Path) -> None:
        """Write an empty project in ``project`` that reads the raw files under ``raw``."""
        ...

    def build(
        self,
        project: Path,
        raw: Path,
        workdir: Path,
        sandbox: Sandbox | None = None,
        hidden: tuple[Path, ...] = (),
    ) -> BuildResult:
        """Build the project from nothing into ``workdir``, in ``sandbox`` if given."""
        ...


@runtime_checkable
class Validator(Protocol):
    """Decides, in code, whether a built project does what was declared."""

    name: str

    def capabilities(self) -> frozenset[str]: ...

    def validate(
        self,
        project: Path,
        raw: Path,
        declaration: Path,
        workdir: Path,
        sandbox: Sandbox | None = None,
        hidden: tuple[Path, ...] = (),
    ) -> ValidationReport:
        """Build the project afresh and run every declared check on it."""
        ...


@runtime_checkable
class AgentBackend(Protocol):
    """Runs one agent session in a folder and returns what it reported."""

    name: str

    def capabilities(self) -> frozenset[str]: ...

    def run(
        self,
        role: Role,
        workspace: Path,
        report_file: Path,
        timeout: float,
        sandbox: Sandbox | None = None,
        mounts: Mounts | None = None,
    ) -> AgentRun:
        """Run ``role`` in ``workspace``; keep its report in ``report_file``."""
        ...
