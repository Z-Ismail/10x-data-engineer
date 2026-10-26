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
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from dataclasses import replace as replace_fields
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


MANIFEST_FILE = "manifest.json"
WORK_DIR = "work"
ATTEMPT_PREFIX = "attempt-"
STARTED, COMPLETE, FAILED = "started", "complete", "failed"


@dataclass(frozen=True)
class Attempt:
    """One attempt at one step of a run, and what it was made from.

    The attempt's folder holds its manifest and a ``work`` folder, the only
    part an agent is given; so an agent cannot change the record of its own
    attempt. Outputs are named relative to the attempt's folder.
    """

    folder: Path
    run_id: str
    node: str
    number: int
    files: dict[str, str]
    values: dict[str, str]
    status: str = STARTED
    outputs: dict[str, str] = field(default_factory=dict)

    @property
    def work(self) -> Path:
        return self.folder / WORK_DIR

    @property
    def input_hash(self) -> str:
        return input_hash(self.files, self.values)

    @property
    def action_id(self) -> str:
        return action_id(self.run_id, self.node, self.number, self.input_hash)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "node": self.node,
            "attempt": self.number,
            "action_id": self.action_id,
            "input_hash": self.input_hash,
            "inputs": {"files": self.files, "values": self.values},
            "status": self.status,
            "outputs": self.outputs,
        }


def _write_manifest(attempt: Attempt) -> None:
    text = json.dumps(attempt.to_dict(), indent=2, sort_keys=True) + "\n"
    write_text(attempt.folder / MANIFEST_FILE, text)


def attempt_numbers(root: Path, node: str) -> list[int]:
    """The numbers of a step's attempts so far, in order."""
    folder = Path(root) / node
    if not folder.is_dir():
        return []
    numbers = []
    for path in folder.iterdir():
        suffix = path.name.removeprefix(ATTEMPT_PREFIX)
        if path.is_dir() and path.name.startswith(ATTEMPT_PREFIX) and suffix.isdigit():
            numbers.append(int(suffix))
    return sorted(numbers)


def start(
    root: Path,
    run_id: str,
    node: str,
    files: Mapping[str, str],
    values: Mapping[str, str],
    copy_from: Path | None = None,
) -> Attempt:
    """Begin a new attempt in a folder of its own, numbered after the last one.

    ``copy_from`` is copied into the attempt's work folder, so the attempt
    changes its own copy and never the last good one.
    """
    number = (attempt_numbers(root, node) or [0])[-1] + 1
    (Path(root) / node).mkdir(parents=True, exist_ok=True)
    while True:
        folder = Path(root) / node / f"{ATTEMPT_PREFIX}{number}"
        try:
            folder.mkdir()
            break
        except FileExistsError:
            number += 1  # another start took this number first
    attempt = Attempt(folder, run_id, node, number, dict(files), dict(values))
    if copy_from is not None:
        shutil.copytree(copy_from, attempt.work)
    else:
        attempt.work.mkdir()
    _write_manifest(attempt)
    return attempt


def finish(attempt: Attempt, status: str, outputs: list[str] = ()) -> Attempt:
    """Record how an attempt ended, with the hash of each output it made."""
    if status not in (COMPLETE, FAILED):
        raise ValueError(f"an attempt ends {COMPLETE} or {FAILED}, not {status!r}")
    hashes = {name: file_hash(attempt.folder / name) for name in sorted(outputs)}
    finished = replace_fields(attempt, status=status, outputs=hashes)
    _write_manifest(finished)
    return finished


def load(folder: Path) -> Attempt | None:
    """The attempt recorded in ``folder``, or None if its manifest is missing or unreadable."""
    try:
        document = json.loads((Path(folder) / MANIFEST_FILE).read_text(encoding="utf-8"))
        attempt = Attempt(
            Path(folder),
            document["run_id"],
            document["node"],
            document["attempt"],
            dict(document["inputs"]["files"]),
            dict(document["inputs"]["values"]),
            document["status"],
            dict(document["outputs"]),
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    if document.get("action_id") != attempt.action_id:
        return None  # the manifest was changed after it was written
    return attempt


def _outputs_intact(attempt: Attempt) -> bool:
    for name, recorded in attempt.outputs.items():
        path = attempt.folder / name
        if not path.is_file() or file_hash(path) != recorded:
            return False
    return True


def reusable(
    root: Path, run_id: str, node: str, files: Mapping[str, str], values: Mapping[str, str]
) -> Attempt | None:
    """The latest finished attempt a resumed run can use instead of running the step again.

    An attempt is reused only if it completed, was made from exactly these
    inputs for this run and step (its action ID, worked out again, matches),
    and every output it recorded is still there unchanged. Otherwise there is
    nothing to reuse and the step runs again, in a new attempt.

    The manifest itself is trusted: it is written atomically, outside the
    work folder an agent is given. Someone who edits it by hand can make an
    attempt look reusable; the check guards against crashes, not editors.
    """
    wanted = input_hash(files, values)
    for number in reversed(attempt_numbers(root, node)):
        attempt = load(Path(root) / node / f"{ATTEMPT_PREFIX}{number}")
        if (
            attempt is not None
            and attempt.status == COMPLETE
            and attempt.number == number
            and attempt.action_id == action_id(run_id, node, number, wanted)
            and _outputs_intact(attempt)
        ):
            return attempt
    return None
