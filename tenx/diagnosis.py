"""The diagnosis packet: what a diagnostician is given of a project the validator failed.

A diagnostician works out why a project fails, so it is given what a person
would look at first: the goal, the spec when there is one, the source
profiles, the project's models, every check with what an agent may be told
of it, and what the build wrote about itself (its results and its log). It
is never given what the checks are built from: the declaration, the expected
rows, or anything the run hides from agents.

The build's files name the folders the build ran in, which tell where the
run keeps its files, so each of those folders is replaced by a neutral name.
A long log keeps its end, where the errors and the summary are. The packet
is made outside the sandbox from files an agent's code wrote, so it follows
no link, opens nothing but regular files, and keeps every file small; what
it leaves out or cuts is named in ``PACKET.md``. This module reaches no
platform, so the graph can make the packet itself.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path

from tenx.review import (
    LINK,
    MAX_FILE_BYTES,
    SPECIAL,
    TOO_LARGE,
)

__all__ = [
    "BUILD_DIR",
    "BUILD_NAME",
    "PROJECT_NAME",
    "RAW_NAME",
    "read_build_file",
]

BUILD_DIR = "build"
# What each folder of the run is called in the build's files.
PROJECT_NAME = "<project>"
RAW_NAME = "<raw>"
BUILD_NAME = "<build>"
# A file of these kinds keeps its end when it is too long; any other is left out.
TAILED_SUFFIXES = (".log",)
MISSING = "not written by the build"


def _cut(dropped: int) -> str:
    return f"its first {dropped} bytes cut, to keep the last {MAX_FILE_BYTES // 1024} KiB"


def read_build_file(path: Path, renames: Mapping[str, str]) -> tuple[bytes | None, str | None]:
    """A build file's bytes with each folder renamed, and why any of it is missing.

    Returns (bytes, None) for a whole file, (bytes, reason) for a log cut to
    its end, and (None, reason) for a file left out. ``renames`` maps each
    folder's absolute path to its neutral name; longer paths are replaced
    first, so a folder inside another keeps its own name.
    """
    if not os.path.lexists(path):
        return None, MISSING
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as error:
        return None, LINK if os.path.islink(path) else f"could not be read ({error.strerror})"
    reason = None
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            return None, SPECIAL
        if info.st_size > MAX_FILE_BYTES:
            if Path(path).suffix.lower() not in TAILED_SUFFIXES:
                return None, TOO_LARGE
            handle.seek(info.st_size - MAX_FILE_BYTES)
            data = handle.read(MAX_FILE_BYTES)
            start = data.find(b"\n") + 1
            data = data[start:] if start else data
            reason = _cut(info.st_size - len(data))
        else:
            data = handle.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES:
                return None, TOO_LARGE
    for folder in sorted(renames, key=len, reverse=True):
        # The whole folder name only: /runs/r1 is not the start of /runs/r10.
        pattern = re.escape(folder.encode("utf-8")) + rb"(?![\w.-])"
        data = re.sub(pattern, renames[folder].encode("utf-8").replace(b"\\", b"\\\\"), data)
    return data, reason
