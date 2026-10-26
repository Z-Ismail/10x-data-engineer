"""Artefacts a run can be resumed from: atomic writes, input hashes and attempts.

A run that stops part way, through a crash, a kill or a closed laptop, must
never leave a half-written file that a later step reads as finished. Every
artefact is therefore written to a temporary file beside it, flushed to disk,
and renamed into place, which POSIX makes atomic.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

TEMPORARY_SUFFIX = ".tmp"


def _sync_folder(folder: Path) -> None:
    """Make a rename inside ``folder`` durable. Folders cannot be opened on Windows."""
    if os.name != "posix":
        return
    descriptor = os.open(folder, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_bytes(path: Path, data: bytes) -> None:
    """Replace ``path`` with ``data`` in one step: readers see the old file or the new one.

    The temporary file is in the same folder, so the rename never crosses a
    filesystem, and its name starts with a dot and ends with ``.tmp``, so it
    is never mistaken for an artefact if the process is killed first.
    """
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=TEMPORARY_SUFFIX
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    _sync_folder(path.parent)


def write_text(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` in UTF-8, atomically."""
    write_bytes(path, text.encode("utf-8"))


def replace(source: Path, path: Path) -> None:
    """Move a finished file into place, atomically and durably.

    For files written in pieces by another process, such as an agent's
    report, which must appear only once complete.
    """
    with open(source, "rb") as handle:
        os.fsync(handle.fileno())
    os.replace(source, path)
    _sync_folder(Path(path).parent)
