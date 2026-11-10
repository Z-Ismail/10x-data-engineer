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

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "CARRIED_DIRS",
    "CARRIED_FILES",
    "CARRIED_SUFFIXES",
    "MAX_FILE_BYTES",
    "MAX_TOTAL_BYTES",
    "PacketError",
    "Selection",
    "select",
]

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
