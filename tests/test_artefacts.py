"""Tests for the artefacts a run can be resumed from."""

import os
import random
import shutil
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


HELLO = "5891b5b522d5df086d0ff0b110fbd9d21bb4fc7163af34d08286a2e846f6be03"


class HashTest(unittest.TestCase):
    """Expected hashes were worked out separately with `printf ... | sha256sum`."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def make(self, files: dict[str, str], under: str = "inputs") -> Path:
        folder = self.root / under
        for name, text in files.items():
            (folder / name).parent.mkdir(parents=True, exist_ok=True)
            (folder / name).write_text(text, encoding="utf-8")
        return folder

    def test_known_values(self):
        goal = self.make({"goal.md": "hello\n"}) / "goal.md"
        self.assertEqual(artefacts.file_hash(goal), HELLO)
        files = artefacts.input_files({"goal": goal})
        self.assertEqual(files, {"goal": HELLO})
        self.assertEqual(
            artefacts.input_hash(files, {"model": "sonnet"}),
            "a7c7caa80c1c3d82751e40cd4a5ba32523777e927d10ae3a2e7d7bd5b7b23f83",
        )
        self.assertEqual(
            artefacts.action_id("run-1", "engineer", 1, "abc"),
            "c854f6edfd02811e13b7c60574e295addc6faa281531c8de1dd1902bba52d6b9",
        )

    def test_a_folder_is_every_file_in_it_whatever_order_they_were_made_in(self):
        first = self.make({"b.txt": "2", "a.txt": "1", "sub/c.txt": "3"}, "one")
        second = self.make({"sub/c.txt": "3", "a.txt": "1", "b.txt": "2"}, "two")
        (second / "empty").mkdir()
        (second / ".a.txt.k2j9.tmp").write_text("half", encoding="utf-8")
        files = artefacts.input_files({"profiles": first})
        self.assertEqual(list(files), ["profiles/a.txt", "profiles/b.txt", "profiles/sub/c.txt"])
        self.assertEqual(files, artefacts.input_files({"profiles": second}))

    def test_any_change_to_the_inputs_changes_the_hash(self):
        folder = self.make({"a.txt": "1", "b.txt": "2"})
        base = artefacts.input_hash(artefacts.input_files({"in": folder}), {"model": "sonnet"})
        changes = {
            "one byte": lambda: (folder / "a.txt").write_text("2", encoding="utf-8"),
            "renamed": lambda: (folder / "a.txt").rename(folder / "c.txt"),
            "added": lambda: (folder / "d.txt").write_text("", encoding="utf-8"),
            "removed": lambda: (folder / "b.txt").unlink(),
        }
        for name, change in changes.items():
            with self.subTest(name):
                shutil.rmtree(folder)
                self.make({"a.txt": "1", "b.txt": "2"})
                change()
                files = artefacts.input_files({"in": folder})
                self.assertNotEqual(artefacts.input_hash(files, {"model": "sonnet"}), base)
        shutil.rmtree(folder)
        self.make({"a.txt": "1", "b.txt": "2"})
        files = artefacts.input_files({"in": folder})
        self.assertEqual(artefacts.input_hash(files, {"model": "sonnet"}), base)
        self.assertNotEqual(artefacts.input_hash(files, {"model": "opus"}), base)
        self.assertNotEqual(artefacts.input_hash(files, {}), base)

    def test_labels_and_values_cannot_run_together(self):
        self.assertNotEqual(
            artefacts.input_hash({"ab": HELLO}, {"c": "d"}),
            artefacts.input_hash({"a": HELLO}, {"bc": "d"}),
        )
        self.assertNotEqual(
            artefacts.input_hash({}, {"a": "bc"}), artefacts.input_hash({}, {"ab": "c"})
        )
        self.assertNotEqual(
            artefacts.action_id("run-1", "engineer", 12, "abc"),
            artefacts.action_id("run-1", "engineer1", 2, "abc"),
        )

    def test_each_part_of_an_action_id_counts(self):
        base = artefacts.action_id("run-1", "engineer", 1, "abc")
        for changed in [
            ("run-2", "engineer", 1, "abc"),
            ("run-1", "gate", 1, "abc"),
            ("run-1", "engineer", 2, "abc"),
            ("run-1", "engineer", 1, "abd"),
        ]:
            with self.subTest(changed):
                self.assertNotEqual(artefacts.action_id(*changed), base)

    def test_a_missing_input_is_an_error_not_an_empty_input(self):
        with self.assertRaisesRegex(FileNotFoundError, "input 'goal' not found"):
            artefacts.input_files({"goal": self.root / "nowhere.md"})


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
