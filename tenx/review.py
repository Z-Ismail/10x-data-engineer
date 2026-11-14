"""The review packet: what a reviewer is given after the validator passes a project.

A reviewer looks for what the checks cannot see, so it is given the goal,
the spec when there is one, the source profiles, the project's models and
a list of every check that already ran, with what an agent may be told of
each. It is never given what the checks themselves are built from: the
declaration, the expected rows, or anything the run hides from agents.

The project was written by an agent, and the packet is made outside the
sandbox, so the copy follows no link, opens nothing but regular files, and
keeps each file and the whole packet small. Whatever it leaves out is named
in ``PACKET.md`` with the reason, so the reviewer knows what it did not see.
This module reaches no platform, so the graph can make the packet itself.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from tenx import artefacts
from tenx.ports import ValidationReport

__all__ = [
    "CARRIED_DIRS",
    "CARRIED_FILES",
    "CARRIED_SUFFIXES",
    "MAX_FILE_BYTES",
    "MAX_TOTAL_BYTES",
    "PACKET_FILE",
    "Packet",
    "PacketError",
    "Selection",
    "assemble",
    "build",
    "render",
    "select",
]

PACKET_FILE = "PACKET.md"
MANIFEST_FILE = "packet.json"
GOAL_FILE = "GOAL.md"
SPEC_FILE = "SPEC.md"
PROFILES_DIR = "source_profiles"
PROJECT_DIR = "project"

# What of a project the packet carries: its settings, and the files under
# the folders dbt reads code and data from, of the kinds dbt reads.
CARRIED_FILES = ("dbt_project.yml",)
CARRIED_DIRS = ("models", "macros", "tests", "seeds", "snapshots", "analyses")
CARRIED_SUFFIXES = (".sql", ".py", ".yml", ".yaml", ".md", ".csv")
MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024

LINK = "a link, not followed"
SPECIAL = "not a regular file"
KIND = "not a kind of file the packet carries"
TOO_LARGE = f"larger than {MAX_FILE_BYTES // 1024} KiB"
FULL = f"past the packet's limit of {MAX_TOTAL_BYTES // (1024 * 1024)} MiB"


class PacketError(Exception):
    """The packet cannot be made, and why, in a sentence."""


@dataclass
class Selection:
    """The project files a packet carries, by path in the project, and those it leaves out."""

    files: dict[str, bytes] = field(default_factory=dict)
    left_out: list[tuple[str, str]] = field(default_factory=list)

    @property
    def size(self) -> int:
        return sum(len(data) for data in self.files.values())


def _read(path: Path) -> bytes | str:
    """The bytes of a regular file, or why it is not read; never follows a link or blocks."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as error:
        return LINK if os.path.islink(path) else f"could not be read ({error.strerror})"
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            return SPECIAL
        if info.st_size > MAX_FILE_BYTES:
            return TOO_LARGE
        data = handle.read(MAX_FILE_BYTES + 1)
    return TOO_LARGE if len(data) > MAX_FILE_BYTES else data


def _take(selection: Selection, project: Path, path: Path) -> None:
    name = path.relative_to(project).as_posix()
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode):
        selection.left_out.append((name, LINK))
    elif stat.S_ISDIR(info.st_mode):
        for child in sorted(path.iterdir()):
            _take(selection, project, child)
    elif not stat.S_ISREG(info.st_mode):
        selection.left_out.append((name, SPECIAL))
    elif path.suffix.lower() not in CARRIED_SUFFIXES and name not in CARRIED_FILES:
        selection.left_out.append((name, KIND))
    else:
        data = _read(path)
        if isinstance(data, str):
            selection.left_out.append((name, data))
        elif selection.size + len(data) > MAX_TOTAL_BYTES:
            selection.left_out.append((name, FULL))
        else:
            selection.files[name] = data


def select(project: Path) -> Selection:
    """What of ``project`` a packet carries, in a fixed order, and what it leaves out."""
    project = Path(project)
    if not project.is_dir() or project.is_symlink():
        raise PacketError(f"{project} is not a project folder")
    selection = Selection()
    for name in (*CARRIED_FILES, *CARRIED_DIRS):
        path = project / name
        if os.path.lexists(path):
            _take(selection, project, path)
    return selection


def _counted(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def render(report: ValidationReport, selection: Selection, spec: bool) -> str:
    """``PACKET.md``: what is in the packet, the checks that ran, and what was left out.

    Each check is shown with what the validator lets an agent be told of it
    (its feedback), never its full detail, so a check built from something
    hidden says only what it chose to.
    """
    if not report.passed:
        raise PacketError("a project the validator failed is not reviewed")
    lines = [
        "# Review packet",
        "",
        "The project in `project/` was built from an empty database and passed every",
        f"check the validator ran ({_counted(len(report.results), 'check')}), listed below",
        "with what may be said of each. The checks ran on every row.",
        "",
        "## Files",
        "",
        "- `GOAL.md`: the goal the project was built for.",
    ]
    if spec:
        lines.append("- `SPEC.md`: the spec the engineer was given.")
    lines += [
        "- `source_profiles/`: a profile of each raw table, and the overlap report.",
        f"- `project/`: the project's settings and models, {_counted(len(selection.files), 'file')}.",
        "",
        "## Checks that passed",
        "",
    ]
    for result in report.results:
        said = result.feedback or "No more is said about this check."
        first, *rest = said.splitlines() or [""]
        lines.append(f"- {result.check} on {result.subject}: {first}")
        lines += [f"  {line}" for line in rest]
    lines += ["", "## Left out of `project/`", ""]
    if selection.left_out:
        lines += [f"- `{name}`: {reason}." for name, reason in selection.left_out]
    else:
        lines.append("Nothing.")
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class Packet:
    """A packet made: its folder, each file's SHA-256 by path in it, and what was left out."""

    folder: Path
    files: dict[str, str]
    left_out: tuple[tuple[str, str], ...]


def _secret_hashes(secrets: Iterable[Path]) -> set[str]:
    found = set()
    for secret in secrets:
        secret = Path(secret)
        paths = sorted(secret.rglob("*")) if secret.is_dir() else [secret]
        for path in paths:
            if path.is_file() and not path.is_symlink():
                found.add(artefacts.file_hash(path))
    return found


def assemble(
    folder: Path,
    *,
    goal: Path,
    profiles: Path,
    files: dict[str, bytes],
    left_out: Iterable[tuple[str, str]],
    spec: Path | None = None,
    secrets: Iterable[Path] = (),
) -> Packet:
    """Write a packet into ``folder``, all of it or nothing.

    ``files`` maps each further file's path in the packet to its bytes. The
    goal and spec come from the run's own files, not the copies in the
    project, which the engineer could have changed. ``secrets`` are the files
    and folders an agent must never see (the validator's declaration and
    working files): a packet file with the same bytes as any of them refuses
    the whole packet, which is then not made.
    """
    folder = Path(folder)
    if os.path.lexists(folder) and (not folder.is_dir() or any(folder.iterdir())):
        raise PacketError(f"{folder} already exists and is not an empty folder")
    left_out = tuple(left_out)
    folder.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f".{folder.name}.", dir=folder.parent))
    try:
        shutil.copyfile(goal, work / GOAL_FILE)
        if spec is not None:
            shutil.copyfile(spec, work / SPEC_FILE)
        shutil.copytree(profiles, work / PROFILES_DIR)
        for name, data in files.items():
            path = work / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        written = {
            path.relative_to(work).as_posix(): artefacts.file_hash(path)
            for path in sorted(work.rglob("*"))
            if path.is_file()
        }
        hidden = _secret_hashes(secrets)
        for name, digest in written.items():
            if digest in hidden:
                raise PacketError(f"{name} has the same bytes as a file hidden from agents")
        manifest = {"files": written, "left_out": [list(item) for item in left_out]}
        (work / MANIFEST_FILE).write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        if folder.is_dir():
            folder.rmdir()
        os.replace(work, folder)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    return Packet(folder, written, left_out)


def build(
    folder: Path,
    *,
    goal: Path,
    profiles: Path,
    project: Path,
    report: ValidationReport,
    spec: Path | None = None,
    secrets: Iterable[Path] = (),
) -> Packet:
    """Make the review packet for a project the validator passed, in ``folder``.

    The goal, spec and secrets are as ``assemble`` takes them.
    """
    folder = Path(folder)
    if os.path.lexists(folder) and (not folder.is_dir() or any(folder.iterdir())):
        raise PacketError(f"{folder} already exists and is not an empty folder")
    chosen = select(project)
    files = {f"{PROJECT_DIR}/{name}": data for name, data in chosen.files.items()}
    files[PACKET_FILE] = render(report, chosen, spec is not None).encode("utf-8")
    return assemble(
        folder,
        goal=goal,
        profiles=profiles,
        files=files,
        left_out=chosen.left_out,
        spec=spec,
        secrets=secrets,
    )
