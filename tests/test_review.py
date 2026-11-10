"""The review packet: what it carries of a project an agent wrote, and what it leaves out."""

from __future__ import annotations

import os
import signal
import socket
import tempfile
import unittest
from pathlib import Path

from tenx import review


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


if __name__ == "__main__":
    unittest.main()
