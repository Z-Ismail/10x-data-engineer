"""Artefacts a run can be resumed from: atomic writes, input hashes and attempts.

A run that stops part way, through a crash, a kill or a closed laptop, must
never leave a half-written file that a later step reads as finished. Every
artefact is therefore written to a temporary file beside it, flushed to disk,
and renamed into place, which POSIX makes atomic.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

TEMPORARY_SUFFIX = ".tmp"
CHUNK = 1024 * 1024


def canonical(value) -> bytes:
    """One byte form for a JSON value: sorted keys, no spaces, ASCII only."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def file_hash(path: Path) -> str:
    """SHA-256 of a file's bytes, as hex."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while block := handle.read(CHUNK):
            digest.update(block)
    return digest.hexdigest()


def is_temporary(path: Path) -> bool:
    """A file left by a write that never finished, which no step may read."""
    return path.name.startswith(".") and path.name.endswith(TEMPORARY_SUFFIX)


def input_files(inputs: Mapping[str, Path]) -> dict[str, str]:
    """Each input file's hash, by label. A folder counts as every file in it.

    A file inside a folder is labelled ``<label>/<path inside the folder>``,
    with ``/`` between parts on every platform, so the labels do not depend
    on the order the files were made in or on the operating system.
    Temporary files left by an unfinished write are not inputs.
    """
    hashes = {}
    for label, path in inputs.items():
        path = Path(path)
        if path.is_dir():
            for file in sorted(path.rglob("*")):
                if file.is_file() and not is_temporary(file):
                    hashes[f"{label}/{file.relative_to(path).as_posix()}"] = file_hash(file)
        elif path.is_file():
            hashes[label] = file_hash(path)
        else:
            raise FileNotFoundError(f"input {label!r} not found: {path}")
    return dict(sorted(hashes.items()))


def input_hash(files: Mapping[str, str], values: Mapping[str, str]) -> str:
    """One hash for an attempt's inputs: its files' hashes and named values such as the model."""
    return hashlib.sha256(canonical({"files": dict(files), "values": dict(values)})).hexdigest()


def action_id(run_id: str, node: str, attempt: int, inputs: str) -> str:
    """A stable name for one attempt at one step: the same parts always give the same ID."""
    return hashlib.sha256(canonical([run_id, node, attempt, inputs])).hexdigest()


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
