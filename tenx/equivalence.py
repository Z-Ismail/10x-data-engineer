"""Compare a run of the old runner with a graph run of the same inputs, byte for byte.

    compare_runs(old, graph, old_gate_report) -> Comparison

The graph run (``tenx.run``) does what the old runner (``tenx.engineer``)
does, in a different folder layout. Given the same inputs and an agent that
answers the same way, the two must leave the same artefacts: the engineer's
project, the cross-source agent's folder, the notes, the agents' reports,
the usage by role and the gate's report. This module maps one layout onto
the other and compares every mapped file byte for byte.

Every file in either run must be accounted for: compared with its
counterpart, or listed as belonging to the graph by design (its checkpoints,
spending record, attempt manifests and the earlier attempts of a resumed
run). A file in neither list is a difference, so a new artefact in one
runner cannot go unnoticed. The gate's working folders are compared by file
names only: their databases and dbt artifacts differ between any two builds.

The old runner keeps no gate report on disk; it prints it. The caller gives
that text as ``old_gate_report``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

ATTEMPT = re.compile(r"attempt-(\d+)")
STEPS = ("cross-source", "engineer", "gate")
# Characters shown either side of the first change in a differing line.
CONTEXT = 30
GRAPH_ONLY_FILES = (
    "budget.json",
    "checkpoints.sqlite",
    "checkpoints.sqlite-wal",
    "checkpoints.sqlite-shm",
)


@dataclass(frozen=True)
class Comparison:
    """What was compared, what belongs to the graph alone, and every difference."""

    compared: tuple[str, ...]
    graph_only: tuple[str, ...]
    differences: tuple[str, ...]

    @property
    def identical(self) -> bool:
        return not self.differences

    def render(self) -> str:
        count = len(self.differences)
        verdict = f"{count} difference{'s' if count != 1 else ''}" if count else "identical"
        lines = [
            f"{verdict}: {len(self.compared)} files compared, {len(self.graph_only)} graph only"
        ]
        lines += [f"  {difference}" for difference in self.differences]
        return "\n".join(lines) + "\n"


def latest_attempts(graph: Path) -> dict[str, str]:
    """The folder of the last attempt of each step that has one, as ``attempt-<n>``."""
    found = {}
    for step in STEPS:
        numbers = [
            int(match.group(1))
            for folder in (graph / step).glob("attempt-*")
            if (match := ATTEMPT.fullmatch(folder.name))
        ]
        if numbers:
            found[step] = f"attempt-{max(numbers)}"
    return found


def _earlier_attempt(name: str, used: dict[str, str]) -> bool:
    """Whether a graph file belongs to an attempt the run did not end with (a resumed run)."""
    parts = name.split("/")
    return (
        len(parts) > 2
        and parts[0] in used
        and ATTEMPT.fullmatch(parts[1]) is not None
        and parts[1] != used[parts[0]]
    )


def _files(folder: Path) -> set[str]:
    if not folder.is_dir():
        return set()
    return {str(path.relative_to(folder)) for path in folder.rglob("*") if path.is_file()}


def _first_difference(old: bytes, new: bytes) -> str:
    try:
        old_lines = old.decode("utf-8").splitlines()
        new_lines = new.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return f"sizes {len(old)} and {len(new)} bytes"
    for number, (left, right) in enumerate(zip(old_lines, new_lines), start=1):
        if left != right:
            # Reports are one long line, so show the text around the first change.
            at = next(
                i for i, pair in enumerate(zip(left + "\0", right + "\0")) if len(set(pair)) > 1
            )
            start = max(0, at - CONTEXT)
            near = (left[start : at + CONTEXT], right[start : at + CONTEXT])
            return f"line {number}, character {at + 1}: {near[0]!r} against {near[1]!r}"
    if len(old_lines) != len(new_lines):
        return f"{len(old_lines)} lines against {len(new_lines)}"
    return "line endings differ"


class _Map:
    """Walks the pairs of the map, keeping track of what each side has accounted for."""

    def __init__(self, old: Path, graph: Path):
        self.old, self.graph = old, graph
        self.old_left, self.graph_left = _files(old), _files(graph)
        self.compared: list[str] = []
        self.graph_only: list[str] = []
        self.differences: list[str] = []

    def bytes(self, old_name: str, graph_name: str, old_bytes: bytes | None = None) -> None:
        old_path, graph_path = self.old / old_name, self.graph / graph_name
        self.old_left.discard(old_name)
        self.graph_left.discard(graph_name)
        if old_bytes is None and old_path.is_file():
            old_bytes = old_path.read_bytes()
        if old_bytes is None and not graph_path.is_file():
            return
        if old_bytes is None:
            self.differences.append(f"only in the graph run: {graph_name} (no {old_name})")
            return
        if not graph_path.is_file():
            self.differences.append(f"only in the old run: {old_name} (no {graph_name})")
            return
        self.compared.append(graph_name)
        new_bytes = graph_path.read_bytes()
        if old_bytes != new_bytes:
            where = _first_difference(old_bytes, new_bytes)
            self.differences.append(f"differs: {old_name} and {graph_name}, {where}")

    def folder(self, old_name: str, graph_name: str) -> None:
        names = _files(self.old / old_name) | _files(self.graph / graph_name)
        for name in sorted(names):
            self.bytes(f"{old_name}/{name}", f"{graph_name}/{name}")

    def names(self, old_name: str, graph_name: str) -> None:
        old_names, graph_names = _files(self.old / old_name), _files(self.graph / graph_name)
        self.old_left -= {f"{old_name}/{name}" for name in old_names}
        self.graph_left -= {f"{graph_name}/{name}" for name in graph_names}
        for name in sorted(old_names - graph_names):
            self.differences.append(f"only in the old run: {old_name}/{name}")
        for name in sorted(graph_names - old_names):
            self.differences.append(f"only in the graph run: {graph_name}/{name}")
        if old_names or graph_names:
            self.compared.append(f"{graph_name}/ (file names)")

    def graph_alone(self, names) -> None:
        for name in sorted(set(names) & self.graph_left):
            self.graph_left.discard(name)
            self.graph_only.append(name)


def compare_runs(old: Path, graph: Path, old_gate_report: str | None) -> Comparison:
    """Compare an old-runner run folder with a graph run folder, file by file."""
    old, graph = Path(old), Path(graph)
    used = latest_attempts(graph)
    cross_source = f"cross-source/{used.get('cross-source', 'attempt-1')}"
    engineer = f"engineer/{used.get('engineer', 'attempt-1')}"
    gate = f"gate/{used.get('gate', 'attempt-1')}"
    walk = _Map(old, graph)
    walk.folder("plugin", "plugin")
    walk.folder("project", f"{engineer}/work")
    walk.folder("cross_source", f"{cross_source}/work")
    walk.folder("project/source_profiles", "profile")
    for name in ("overlap.txt", "overlap.json"):
        walk.bytes(f"cross_source/source_profiles/{name}", f"overlap/{name}")
    walk.bytes("project/JOINS.md", f"{cross_source}/JOINS.md")
    walk.bytes("agent.json", f"{engineer}/agent.json")
    walk.bytes("cross_source.json", f"{cross_source}/agent.json")
    walk.bytes("usage.json", "usage.json")
    report = None if old_gate_report is None else old_gate_report.encode("utf-8")
    walk.bytes("(gate report as printed)", f"{gate}/report.txt", report)
    walk.names("gate", f"{gate}/work")
    walk.graph_alone(GRAPH_ONLY_FILES)
    walk.graph_alone(f"{folder}/manifest.json" for folder in (cross_source, engineer, gate))
    walk.graph_alone(name for name in walk.graph_left if _earlier_attempt(name, used))
    walk.differences += [f"only in the old run, not in the map: {n}" for n in sorted(walk.old_left)]
    walk.differences += [
        f"only in the graph run, not in the map: {name}" for name in sorted(walk.graph_left)
    ]
    return Comparison(tuple(walk.compared), tuple(walk.graph_only), tuple(walk.differences))
