"""Tests for the artefacts a run can be resumed from."""

import os
import random
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tenx import artefacts

ROOT = Path(__file__).resolve().parent.parent

# Writes alternating whole payloads to a file until killed, and says when it has started.
KEEPS_WRITING = """
import sys
from pathlib import Path
from tenx.artefacts import write_bytes
target, size = Path(sys.argv[1]), int(sys.argv[2])
payloads = [b"A" * size, b"B" * size]
write_bytes(target, payloads[0])
print("ready", flush=True)
n = 1
while True:
    write_bytes(target, payloads[n % 2])
    n += 1
"""


def leftovers(folder: Path) -> list[str]:
    return sorted(path.name for path in folder.iterdir() if path.name.endswith(".tmp"))


class WriteTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)
        self.path = self.folder / "report.json"

    def test_writes_the_content_and_leaves_nothing_else(self):
        artefacts.write_text(self.path, "first\n")
        self.assertEqual(self.path.read_text(encoding="utf-8"), "first\n")
        artefacts.write_bytes(self.path, b"second")
        self.assertEqual(self.path.read_bytes(), b"second")
        self.assertEqual([path.name for path in self.folder.iterdir()], ["report.json"])

    def test_text_is_written_as_utf_8(self):
        artefacts.write_text(self.path, "café")
        self.assertEqual(self.path.read_bytes(), "café".encode())

    def test_an_error_while_writing_keeps_the_old_file_and_removes_the_temporary_one(self):
        artefacts.write_text(self.path, "old")
        failing = mock.patch("os.fsync", side_effect=OSError("disk full"))
        with failing, self.assertRaisesRegex(OSError, "disk full"):
            artefacts.write_text(self.path, "new")
        self.assertEqual(self.path.read_text(encoding="utf-8"), "old")
        self.assertEqual(leftovers(self.folder), [])

    def test_the_file_and_its_folder_are_synced_to_disk(self):
        synced = []
        real_fsync = os.fsync

        def record(descriptor):
            synced.append(stat.S_ISDIR(os.fstat(descriptor).st_mode))
            real_fsync(descriptor)

        with mock.patch("os.fsync", side_effect=record):
            artefacts.write_text(self.path, "content")
        self.assertEqual(synced, [False, True])

    def test_replace_moves_a_finished_file_into_place_and_syncs_both(self):
        partial = self.folder / "agent.json.partial"
        partial.write_text("{}", encoding="utf-8")
        synced = []
        real_fsync = os.fsync

        def record(descriptor):
            synced.append(stat.S_ISDIR(os.fstat(descriptor).st_mode))
            real_fsync(descriptor)

        with mock.patch("os.fsync", side_effect=record):
            artefacts.replace(partial, self.path)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{}")
        self.assertFalse(partial.exists())
        self.assertEqual(synced, [False, True])


class KilledWriterTest(unittest.TestCase):
    """A writer killed at any moment leaves the old file or the new one, never part of one."""

    SIZE = 4 * 1024 * 1024
    KILLS = 25

    def test_a_killed_writer_never_leaves_a_partial_file(self):
        generator = random.Random(2026)
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            target = folder / "big.bin"
            whole = {b"A" * self.SIZE, b"B" * self.SIZE}
            for _ in range(self.KILLS):
                writer = subprocess.Popen(
                    [sys.executable, "-c", KEEPS_WRITING, str(target), str(self.SIZE)],
                    cwd=ROOT,
                    stdout=subprocess.PIPE,
                    text=True,
                )
                self.assertEqual(writer.stdout.readline().strip(), "ready")
                time.sleep(generator.uniform(0, 0.05))
                writer.send_signal(signal.SIGKILL)
                writer.wait()
                writer.stdout.close()
                found = target.read_bytes()
                self.assertTrue(
                    found in whole, f"{len(found)} bytes, starting {found[:1]!r}: a partial file"
                )
                for name in leftovers(folder):
                    # A killed writer may leave its temporary file, never the artefact.
                    self.assertTrue(name.startswith(".big.bin."), name)
                    (folder / name).unlink()


if __name__ == "__main__":
    unittest.main()
