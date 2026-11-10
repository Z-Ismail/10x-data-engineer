"""The review packet: what it carries of a project an agent wrote, and what it leaves out."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import socket
import tempfile
import unittest
from pathlib import Path

from tenx import review
from tenx.ports import CheckResult, ValidationReport


def _write(path: Path, text: str = "select 1\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class NoHang:
    """Fails the test if the block takes longer than a few seconds, instead of hanging."""

    def __enter__(self):
        def stop(signum, frame):
            raise AssertionError("the selection blocked")

        self.before = signal.signal(signal.SIGALRM, stop)
        signal.alarm(5)

    def __exit__(self, *exc):
        signal.alarm(0)
        signal.signal(signal.SIGALRM, self.before)


class SelectTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: os.system(f"rm -rf '{self.root}'"))
        self.project = self.root / "project"
        self.secret = _write(self.root / "secret.json", '{"hidden": "the answer"}\n')
        _write(self.project / "dbt_project.yml", "name: demo\n")
        _write(self.project / "models" / "staging" / "stg_a.sql")
        _write(self.project / "models" / "staging" / "staging.yml", "version: 2\n")

    def select(self) -> review.Selection:
        with NoHang():
            return review.select(self.project)

    def test_carries_the_settings_and_models_in_a_fixed_order(self):
        _write(self.project / "macros" / "cents.sql", "{% macro cents() %}{% endmacro %}\n")
        _write(self.project / "models" / "python_model.py", "def model(dbt, s): pass\n")
        _write(self.project / "models" / "UPPER.SQL")
        chosen = self.select()
        self.assertEqual(
            list(chosen.files),
            [
                "dbt_project.yml",
                "models/UPPER.SQL",
                "models/python_model.py",
                "models/staging/staging.yml",
                "models/staging/stg_a.sql",
                "macros/cents.sql",
            ],
        )
        self.assertEqual(chosen.files["dbt_project.yml"], b"name: demo\n")
        self.assertEqual(chosen.left_out, [])

    def test_does_not_carry_what_the_build_left_or_the_engineers_inputs(self):
        for name in (
            "target/manifest.json",
            "logs/dbt.log",
            "dev.duckdb",
            "profiles.yml",
            "GOAL.md",
            "SPEC.md",
            "JOINS.md",
            "GATE_REPORT.md",
            "source_profiles/billing.customers.txt",
            "dbt_packages/utils/macros/x.sql",
        ):
            _write(self.project / name)
        chosen = self.select()
        self.assertEqual(
            sorted(chosen.files),
            ["dbt_project.yml", "models/staging/staging.yml", "models/staging/stg_a.sql"],
        )
        self.assertEqual(chosen.left_out, [])

    def test_a_link_is_left_out_and_never_followed(self):
        (self.project / "models" / "leak.sql").symlink_to(self.secret)
        (self.project / "models" / "folder").symlink_to(self.root, target_is_directory=True)
        chosen = self.select()
        self.assertEqual(
            chosen.left_out,
            [("models/folder", review.LINK), ("models/leak.sql", review.LINK)],
        )
        self.assertFalse(any(b"the answer" in data for data in chosen.files.values()))

    def test_the_models_folder_or_the_settings_as_a_link_are_left_out(self):
        os.system(f"rm -rf '{self.project / 'models'}' '{self.project / 'dbt_project.yml'}'")
        (self.project / "models").symlink_to(self.root, target_is_directory=True)
        (self.project / "dbt_project.yml").symlink_to(self.secret)
        chosen = self.select()
        self.assertEqual(chosen.files, {})
        self.assertEqual(
            chosen.left_out, [("dbt_project.yml", review.LINK), ("models", review.LINK)]
        )

    def test_a_named_pipe_or_socket_is_left_out_without_blocking(self):
        os.mkfifo(self.project / "models" / "pipe.sql")
        server = socket.socket(socket.AF_UNIX)
        self.addCleanup(server.close)
        server.bind(str(self.project / "models" / "socket.sql"))
        chosen = self.select()
        self.assertEqual(
            chosen.left_out,
            [("models/pipe.sql", review.SPECIAL), ("models/socket.sql", review.SPECIAL)],
        )

    def test_a_file_of_another_kind_is_left_out(self):
        _write(self.project / "models" / "notes.txt", "x")
        _write(self.project / "models" / ".gitkeep", "")
        chosen = self.select()
        self.assertEqual(
            chosen.left_out,
            [("models/.gitkeep", review.KIND), ("models/notes.txt", review.KIND)],
        )

    def test_files_over_the_limits_are_left_out(self):
        _write(self.project / "models" / "big.sql", "x" * (review.MAX_FILE_BYTES + 1))
        _write(self.project / "models" / "edge.sql", "y" * review.MAX_FILE_BYTES)
        chosen = self.select()
        self.assertEqual(len(chosen.files["models/edge.sql"]), review.MAX_FILE_BYTES)
        self.assertEqual(chosen.left_out, [("models/big.sql", review.TOO_LARGE)])

    def test_files_past_the_packet_limit_are_left_out(self):
        # Eight files of the largest size fill 2 MiB; with the three small
        # files already carried, the eighth no longer fits.
        for number in range(9):
            _write(self.project / "seeds" / f"s{number}.csv", "z" * review.MAX_FILE_BYTES)
        chosen = self.select()
        self.assertLessEqual(chosen.size, review.MAX_TOTAL_BYTES)
        self.assertEqual(
            chosen.left_out, [("seeds/s7.csv", review.FULL), ("seeds/s8.csv", review.FULL)]
        )
        self.assertIn("seeds/s6.csv", chosen.files)

    def test_refuses_a_project_that_is_not_a_folder(self):
        link = self.root / "linked"
        link.symlink_to(self.project, target_is_directory=True)
        for path in (self.root / "missing", self.secret, link):
            with self.subTest(path=path.name), self.assertRaises(review.PacketError):
                review.select(path)


def _report(passed: bool = True) -> ValidationReport:
    results = (
        CheckResult("build", "stg_a", "pass", "built as table", feedback="built as table"),
        CheckResult(
            "expected",
            "int_b",
            "pass" if passed else "fail",
            "int_b: 2 rows expected\n  only expected: cus_000001A, 001\n  only expected: x",
            feedback="int_b: 2 rows expected\n(the rows themselves are not shown)",
        ),
        CheckResult("repeat", "stg_a", "pass", "the same 3 rows in both builds"),
    )
    return ValidationReport(results, passed, "the full report\n")


class RenderTest(unittest.TestCase):
    def test_lists_the_files_the_checks_and_what_was_left_out(self):
        chosen = review.Selection(
            files={"dbt_project.yml": b"a", "models/stg_a.sql": b"b"},
            left_out=[("models/leak.sql", review.LINK)],
        )
        self.assertEqual(
            review.render(_report(), chosen, spec=True),
            "# Review packet\n"
            "\n"
            "The project in `project/` was built from an empty database and passed every\n"
            "check the validator ran (3 checks), listed below\n"
            "with what may be said of each. The checks ran on every row.\n"
            "\n"
            "## Files\n"
            "\n"
            "- `GOAL.md`: the goal the project was built for.\n"
            "- `SPEC.md`: the spec the engineer was given.\n"
            "- `source_profiles/`: a profile of each raw table, and the overlap report.\n"
            "- `project/`: the project's settings and models, 2 files.\n"
            "\n"
            "## Checks that passed\n"
            "\n"
            "- build on stg_a: built as table\n"
            "- expected on int_b: int_b: 2 rows expected\n"
            "  (the rows themselves are not shown)\n"
            "- repeat on stg_a: No more is said about this check.\n"
            "\n"
            "## Left out of `project/`\n"
            "\n"
            "- `models/leak.sql`: a link, not followed.\n",
        )

    def test_without_a_spec_or_anything_left_out(self):
        chosen = review.Selection(files={"models/stg_a.sql": b"b"})
        text = review.render(_report(), chosen, spec=False)
        self.assertNotIn("SPEC.md", text)
        self.assertIn("models, 1 file.\n", text)
        self.assertTrue(text.endswith("## Left out of `project/`\n\nNothing.\n"))

    def test_never_shows_a_checks_detail(self):
        text = review.render(_report(), review.Selection(), spec=False)
        for hidden in ("cus_000001A", "only expected", "the same 3 rows", "the full report"):
            self.assertNotIn(hidden, text)

    def test_refuses_a_failed_validation(self):
        with self.assertRaisesRegex(review.PacketError, "failed is not reviewed"):
            review.render(_report(passed=False), review.Selection(), spec=False)


class BuildTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: os.system(f"rm -rf '{self.root}'"))
        self.goal = _write(self.root / "run" / "goal.md", "# Goal\n")
        self.spec = _write(self.root / "run" / "SPEC.md", "# Spec\n")
        self.profiles = self.root / "run" / "profiles"
        _write(self.profiles / "billing.customers.txt", "profile\n")
        _write(self.profiles / "overlap.txt", "overlap\n")
        self.project = self.root / "engineer"
        _write(self.project / "dbt_project.yml", "name: demo\n")
        _write(self.project / "models" / "stg_a.sql")
        # The engineer's own copies, which it could have changed.
        _write(self.project / "GOAL.md", "# A goal the engineer rewrote\n")
        _write(self.project / "SPEC.md", "# A spec the engineer rewrote\n")
        self.declaration = _write(self.root / "checks" / "declaration.json", '{"models": []}\n')
        self.out = self.root / "out" / "packet"

    def build(self, **changes) -> review.Packet:
        arguments = {
            "goal": self.goal,
            "profiles": self.profiles,
            "project": self.project,
            "report": _report(),
            "spec": self.spec,
            "secrets": (self.declaration,),
        }
        return review.build(self.out, **{**arguments, **changes})

    def test_holds_exactly_the_packet_files(self):
        packet = self.build()
        found = sorted(p.relative_to(self.out).as_posix() for p in self.out.rglob("*"))
        self.assertEqual(
            found,
            [
                "GOAL.md",
                "PACKET.md",
                "SPEC.md",
                "packet.json",
                "project",
                "project/dbt_project.yml",
                "project/models",
                "project/models/stg_a.sql",
                "source_profiles",
                "source_profiles/billing.customers.txt",
                "source_profiles/overlap.txt",
            ],
        )
        self.assertEqual((self.out / "GOAL.md").read_text(), "# Goal\n")
        self.assertEqual((self.out / "SPEC.md").read_text(), "# Spec\n")
        self.assertEqual((self.out / "project" / "models" / "stg_a.sql").read_text(), "select 1\n")
        manifest = json.loads((self.out / "packet.json").read_text())
        self.assertEqual(manifest["left_out"], [])
        self.assertEqual(
            set(manifest["files"]),
            set(found) - {"packet.json", "project", "project/models", "source_profiles"},
        )
        for name, digest in manifest["files"].items():
            data = (self.out / name).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), digest)
        self.assertEqual(packet.files, manifest["files"])
        self.assertEqual(os.listdir(self.root / "out"), ["packet"])

    def test_without_a_spec_and_with_files_left_out(self):
        (self.project / "models" / "leak.sql").symlink_to(self.declaration)
        packet = self.build(spec=None)
        self.assertFalse((self.out / "SPEC.md").exists())
        self.assertEqual(packet.left_out, (("models/leak.sql", review.LINK),))
        manifest = json.loads((self.out / "packet.json").read_text())
        self.assertEqual(manifest["left_out"], [["models/leak.sql", review.LINK]])
        self.assertIn(
            "- `models/leak.sql`: a link, not followed.", (self.out / "PACKET.md").read_text()
        )

    def test_a_copy_of_a_secret_refuses_the_packet(self):
        workdir = self.root / "gate"
        _write(workdir / "expected" / "rows.csv", "cus_000001A,001\n")
        cases = {
            "the declaration": self.declaration.read_text(),
            "a file in a secret folder": "cus_000001A,001\n",
        }
        for case, text in cases.items():
            with self.subTest(case=case):
                _write(self.project / "models" / "copy.yml", text)
                with self.assertRaisesRegex(review.PacketError, "project/models/copy.yml"):
                    self.build(secrets=(self.declaration, workdir, self.root / "missing"))
                self.assertFalse(self.out.exists())
                self.assertEqual(os.listdir(self.root / "out"), [])

    def test_refuses_a_folder_that_holds_something(self):
        _write(self.out / "old.txt", "x")
        with self.assertRaisesRegex(review.PacketError, "not an empty folder"):
            self.build()
        (self.out / "old.txt").unlink()
        self.build()
        self.assertTrue((self.out / "PACKET.md").is_file())

    def test_refuses_a_failed_validation_and_writes_nothing(self):
        with self.assertRaises(review.PacketError):
            self.build(report=_report(passed=False))
        self.assertFalse((self.root / "out").exists() and os.listdir(self.root / "out"))


if __name__ == "__main__":
    unittest.main()
