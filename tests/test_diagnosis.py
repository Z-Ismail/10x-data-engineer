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


class RenderTest(unittest.TestCase):
    def test_lists_files_failures_first_and_what_was_left_out(self):
        chosen = review.Selection(files={"dbt_project.yml": b"a", "models/stg_a.sql": b"b"})
        left = [("project/models/leak.sql", review.LINK), ("build/dbt.log", "cut short")]
        self.assertEqual(
            diagnosis.render(_report(), chosen, ["dbt.log", "run_results.json"], left, spec=True),
            "# Diagnosis packet\n"
            "\n"
            "The project in `project/` was built from an empty database and failed the\n"
            "validator. Of its 3 checks, 1 failed, 1 could not run and 1 passed;\n"
            "each is listed below with what may be said of it. The checks ran on every row.\n"
            "\n"
            "## Files\n"
            "\n"
            "- `GOAL.md`: the goal the project was built for.\n"
            "- `SPEC.md`: the spec the engineer was given.\n"
            "- `source_profiles/`: a profile of each raw table, and the overlap report.\n"
            "- `project/`: the project's settings and models, 2 files.\n"
            "- `build/`: what the build wrote about itself: `build/dbt.log`, "
            "`build/run_results.json`. In these files\n"
            "  `<project>` stands for the project's folder, `<raw>` for the raw\n"
            "  files' folder and `<build>` for the folder the build wrote to.\n"
            "\n"
            "## Checks that failed\n"
            "\n"
            "- expected on int_b: int_b: 2 rows wrong\n"
            "  (the rows themselves are not shown)\n"
            "\n"
            "## Checks that could not run\n"
            "\n"
            "- repeat on int_b: No more is said about this check.\n"
            "\n"
            "## Checks that passed\n"
            "\n"
            "- build on stg_a: built as table\n"
            "\n"
            "## Left out or cut\n"
            "\n"
            "- `project/models/leak.sql`: a link, not followed.\n"
            "- `build/dbt.log`: cut short.\n",
        )

    def test_without_a_spec_build_files_or_anything_left_out(self):
        text = diagnosis.render(_report(), review.Selection(), [], [], spec=False)
        self.assertNotIn("SPEC.md", text)
        self.assertNotIn("build/", text)
        self.assertTrue(text.endswith("## Left out or cut\n\nNothing.\n"))

    def test_never_shows_a_checks_detail(self):
        text = diagnosis.render(_report(), review.Selection(), [], [], spec=False)
        for hidden in ("cus_000001A", "only expected", "the full report"):
            self.assertNotIn(hidden, text)

    def test_refuses_a_passed_validation(self):
        with self.assertRaisesRegex(review.PacketError, "passed has nothing to diagnose"):
            diagnosis.render(_report(passed=True), review.Selection(), [], [], spec=False)
