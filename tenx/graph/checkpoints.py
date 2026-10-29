"""Where a run's graph saves its state after every step, so that it can resume.

Checkpoints are kept in one SQLite file per run. Reading one back never
constructs an object of a class named in the file: the serializer allows
only its built-in safe types, and the state holds only JSON values anyway
(``tenx.graph.state``). So a checkpoint file that was tampered with cannot
make a resume run code, and a resumed state has the same form as a fresh one.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver


def serializer() -> JsonPlusSerializer:
    """The checkpoint serializer, limited to the library's safe built-in types."""
    return JsonPlusSerializer(allowed_msgpack_modules=None)


@contextmanager
def open_checkpoints(path: Path) -> Iterator[SqliteSaver]:
    """Open (or create) the checkpoint file at ``path`` for the length of a run."""
    connection = sqlite3.connect(Path(path), check_same_thread=False)
    try:
        yield SqliteSaver(connection, serde=serializer())
    finally:
        connection.close()
