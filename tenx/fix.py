"""The fix an operate run proposes, as a diff a person reads and applies themselves.

The engineer fixes a copy of the project in the run's folder; the person's
own project is never written. ``proposed_diff`` compares the copy as it was
handed over with the engineer's attempt and writes the change as a unified
diff (``FIX.diff``), which ``git apply`` or ``patch -p1`` can apply.

The attempt was written by an agent, and this comparison runs outside the
sandbox, so it reads only regular files and never follows a link: a link
is named with its target, never opened, as the agent could point one at a
file it was not allowed to see. A file that is not UTF-8 text or is larger
than ``MAX_TEXT_BYTES`` is named as changed without its content. What a
build leaves and the files every attempt is given are left out.
"""

from __future__ import annotations

import difflib
import fnmatch
import os
import stat
from pathlib import Path

__all__ = ["FIX_FILE", "MAX_TEXT_BYTES", "proposed_diff"]

FIX_FILE = "FIX.diff"
MAX_TEXT_BYTES = 256 * 1024
NO_NEWLINE = "\\ No newline at end of file\n"


def _entries(folder: Path, leave_out: tuple[str, ...]) -> dict[str, tuple[str, str]]:
    """Each entry under ``folder`` by relative path: its kind and a link's target."""
    found: dict[str, tuple[str, str]] = {}

    def walk(current: Path, prefix: str) -> None:
        with os.scandir(current) as listing:
            for entry in sorted(listing, key=lambda item: item.name):
                if any(fnmatch.fnmatch(entry.name, pattern) for pattern in leave_out):
                    continue
                name = prefix + entry.name
                mode = entry.stat(follow_symlinks=False).st_mode
                if stat.S_ISLNK(mode):
                    found[name] = ("link", os.readlink(entry.path))
                elif stat.S_ISDIR(mode):
                    walk(Path(entry.path), name + "/")
                elif stat.S_ISREG(mode):
                    found[name] = ("file", "")
                else:
                    found[name] = ("special", "")

    if folder.is_dir():
        walk(folder, "")
    return found


def _read(path: Path, limit: int = -1) -> bytes:
    # O_NOFOLLOW: a file swapped for a link after it was listed is not read.
    handle = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(handle, "rb") as opened:
        return opened.read(limit)


def _text(path: Path) -> list[str] | None:
    """The file's lines, or None if it is not UTF-8 text within the size limit."""
    data = _read(path, MAX_TEXT_BYTES + 1)
    if len(data) > MAX_TEXT_BYTES or b"\0" in data:
        return None
    try:
        return data.decode("utf-8").splitlines(keepends=True)
    except UnicodeDecodeError:
        return None


def _hunks(name: str, before: list[str] | None, after: list[str] | None) -> list[str]:
    old = f"a/{name}" if before is not None else "/dev/null"
    new = f"b/{name}" if after is not None else "/dev/null"
    lines = difflib.unified_diff(before or [], after or [], old, new)
    return [line if line.endswith("\n") else line + "\n" + NO_NEWLINE for line in lines]


def proposed_diff(before: Path, after: Path, leave_out: tuple[str, ...] = ()) -> str:
    """The change from ``before`` (the project handed over) to ``after`` (the fix).

    ``leave_out`` holds name patterns skipped at every level, as the copy
    between attempts skips them.
    """
    old, new = _entries(Path(before), leave_out), _entries(Path(after), leave_out)
    notes: list[str] = []
    hunks: list[str] = []
    for name in sorted(set(old) | set(new)):
        was, now = old.get(name), new.get(name)
        if was == now and was is not None and was[0] != "file":
            continue
        text_before = _text(Path(before) / name) if was and was[0] == "file" else None
        text_after = _text(Path(after) / name) if now and now[0] == "file" else None
        if was and now and was[0] == now[0] == "file":
            if _read(Path(before) / name) == _read(Path(after) / name):
                continue
            if text_before is None or text_after is None:
                notes.append(f"changed, not shown (not text or over 256 KiB): {name}")
                continue
            hunks += _hunks(name, text_before, text_after)
            continue
        for kind, entry, side, text in (
            ("removed", was, "a", text_before),
            ("added", now, "b", text_after),
        ):
            if entry is None:
                continue
            if entry[0] == "link":
                notes.append(f"{kind}, a link to {entry[1]} (not followed): {name}")
            elif entry[0] == "special":
                notes.append(f"{kind}, not a file, folder or link: {name}")
            elif text is None:
                notes.append(f"{kind}, not shown (not text or over 256 KiB): {name}")
            elif side == "a":
                hunks += _hunks(name, text, None)
            else:
                hunks += _hunks(name, None, text)
    lines = [
        "# The fix proposed for the project: what the engineer changed in a copy of it.",
        "# Your project was not changed. Review this, then apply it in the project's",
        "# folder with: git apply FIX.diff (or: patch -p1 < FIX.diff)",
    ]
    if not notes and not hunks:
        lines.append("# The engineer changed nothing.")
    if notes:
        lines.append("# Changes not in the diff below:")
        lines += [f"#   {note}" for note in notes]
    return "\n".join(lines) + "\n" + "".join(hunks)
