"""The diagnosis packet: what a diagnostician is given of a failed project."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from tenx import diagnosis, review
from tenx.ports import CheckResult, ValidationReport

KIB = 1024
LIMIT = review.MAX_FILE_BYTES


def _write(path: Path, text: str = "select 1\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _report(passed: bool = False) -> ValidationReport:
    results = (
        CheckResult("build", "stg_a", "pass", "built as table", feedback="built as table"),
        CheckResult(
            "expected",
            "int_b",
            "fail" if not passed else "pass",
            "int_b: 2 rows wrong\ncus_000001A only expected",
            feedback="int_b: 2 rows wrong\n(the rows themselves are not shown)",
        ),
        CheckResult(
            "repeat", "int_b", "skip" if not passed else "pass", "not checked: the full report"
        ),
    )
    return ValidationReport(results, passed, "the full report")


class ReadBuildFileTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)

    def test_renames_each_folder_longest_first(self):
        path = _write(
            self.root / "run_results.json",
            '{"a": "/runs/r1/gate/target", "b": "/runs/r1/raw/x.csv", "c": "/runs/r1",'
            ' "d": "/runs/r10/gate", "e": "/runs/r1.old"}\n',
        )
        data, reason = diagnosis.read_build_file(
            path, {"/runs/r1": "<project>", "/runs/r1/gate": "<build>", "/runs/r1/raw": "<raw>"}
        )
        self.assertIsNone(reason)
        self.assertEqual(
            data,
            b'{"a": "<build>/target", "b": "<raw>/x.csv", "c": "<project>",'
            b' "d": "/runs/r10/gate", "e": "/runs/r1.old"}\n',
        )

    def test_a_long_log_keeps_its_end_from_a_line_start(self):
        # 300 lines of 1,000 bytes: the last 256 KiB start part way into a line.
        lines = [f"{n:04d} " + "x" * 994 + "\n" for n in range(300)]
        path = _write(self.root / "dbt.log", "".join(lines))
        data, reason = diagnosis.read_build_file(path, {})
        size = 300 * 1000
        kept = 262  # whole lines in the last 262,144 bytes: lines 38 to 299
        self.assertEqual(data, "".join(lines[-kept:]).encode())
        self.assertEqual(
            reason, f"its first {size - kept * 1000} bytes cut, to keep the last 256 KiB"
        )

    def test_a_log_at_the_limit_is_kept_whole(self):
        path = _write(self.root / "dbt.log", "y" * LIMIT)
        self.assertEqual(diagnosis.read_build_file(path, {}), (b"y" * LIMIT, None))

    def test_a_long_file_of_another_kind_is_left_out(self):
        path = _write(self.root / "run_results.json", "z" * (LIMIT + 1))
        self.assertEqual(diagnosis.read_build_file(path, {}), (None, review.TOO_LARGE))

    def test_a_link_a_pipe_or_a_missing_file_is_left_out(self):
        secret = _write(self.root / "secret.json", "{}")
        (self.root / "link.log").symlink_to(secret)
        os.mkfifo(self.root / "pipe.log")
        cases = {
            "link.log": review.LINK,
            "pipe.log": review.SPECIAL,
            "absent.log": diagnosis.MISSING,
        }
        for name, reason in cases.items():
            with self.subTest(name=name):
                self.assertEqual(diagnosis.read_build_file(self.root / name, {}), (None, reason))
