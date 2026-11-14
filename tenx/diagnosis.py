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
from collections.abc import Iterable, Mapping
from pathlib import Path

from tenx.ports import ValidationReport
from tenx.review import (
    LINK,
    MAX_FILE_BYTES,
    SPECIAL,
    TOO_LARGE,
    PacketError,
    Selection,
)

__all__ = [
    "BUILD_DIR",
    "BUILD_NAME",
    "PROJECT_NAME",
    "RAW_NAME",
    "read_build_file",
    "render",
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


def _counted(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def render(
    report: ValidationReport,
    selection: Selection,
    build_files: Iterable[str],
    left_out: Iterable[tuple[str, str]],
    spec: bool,
) -> str:
    """``PACKET.md``: what is in the packet, every check, and what was left out or cut.

    Each check is shown with what the validator lets an agent be told of it
    (its feedback), never its full detail, so a check built from something
    hidden says only what it chose to. ``build_files`` are the names under
    ``build/``; ``left_out`` pairs each packet path with the reason.
    """
    if report.passed:
        raise PacketError("a project the validator passed has nothing to diagnose")
    groups = {
        status: [r for r in report.results if r.status == status] for status in ("fail", "skip")
    }
    passed = [r for r in report.results if r.status not in groups]
    build_files = list(build_files)
    tally = (
        f"{_counted(len(report.results), 'check')}, {len(groups['fail'])} failed, "
        f"{len(groups['skip'])} could not run and {len(passed)} passed"
    )
    lines = [
        "# Diagnosis packet",
        "",
        "The project in `project/` was built from an empty database and failed the",
        f"validator. Of its {tally};",
        "each is listed below with what may be said of it. The checks ran on every row.",
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
    ]
    if build_files:
        named = ", ".join(f"`{BUILD_DIR}/{name}`" for name in build_files)
        lines += [
            f"- `{BUILD_DIR}/`: what the build wrote about itself: {named}. In these files",
            f"  `{PROJECT_NAME}` stands for the project's folder, `{RAW_NAME}` for the raw",
            f"  files' folder and `{BUILD_NAME}` for the folder the build wrote to.",
        ]
    for status, heading in (("fail", "Checks that failed"), ("skip", "Checks that could not run")):
        if groups[status]:
            lines += ["", f"## {heading}", ""]
            lines += _checks(groups[status])
    if passed:
        lines += ["", "## Checks that passed", ""]
        lines += _checks(passed)
    lines += ["", "## Left out or cut", ""]
    left_out = list(left_out)
    lines += [f"- `{name}`: {reason}." for name, reason in left_out] or ["Nothing."]
    return "\n".join(lines) + "\n"


def _checks(results) -> list[str]:
    lines = []
    for result in results:
        said = result.feedback or "No more is said about this check."
        first, *rest = said.splitlines() or [""]
        lines.append(f"- {result.check} on {result.subject}: {first}")
        lines += [f"  {line}" for line in rest]
    return lines
