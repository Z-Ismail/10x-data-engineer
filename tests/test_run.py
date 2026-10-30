"""The graph run's command line with the real DuckDB ports and a stand-in claude.

The stand-in is the one the old runner's tests use: the engineer copies the
reference models, which pass the gate, and the cross-source agent answers
with a fixed answer. Runs are unisolated, as there, because the stand-in
records what it saw outside its folder; the sandbox has its own tests.
"""

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_engineer import ANSWER, DECLARATION, DEMO_GOAL, REFERENCE_MODELS, STUB

from demo.generate import generate

ROOT = Path(__file__).resolve().parent.parent

# Started as the engineer while the file STUB_KILL_ONCE names does not exist,
# the stand-in makes it and kills the runner that started it.
KILL_ONCE = """
import os, signal, sys
from pathlib import Path
marker = os.environ.get("STUB_KILL_ONCE")
if "tenx:engineer" in sys.argv and marker and not Path(marker).exists():
    Path(marker).write_text("killed")
    os.kill(os.getppid(), signal.SIGKILL)
    sys.exit(1)
"""


class CommandLineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.raw = root / "data"
        generate(11, 20, cls.raw, root / "answers")
        bin_dir = root / "bin"
        bin_dir.mkdir()
        claude = bin_dir / "claude"
        claude.write_text(f"#!{sys.executable}\n{KILL_ONCE}\n{STUB}", encoding="utf-8")
        claude.chmod(claude.stat().st_mode | stat.S_IXUSR)
        cls.bin = bin_dir

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.out = self.root / "run"
        self.record = self.root / "record.json"

    def tenx(self, *args: str, kill: bool = False) -> subprocess.CompletedProcess:
        environment = {
            **os.environ,
            "STUB_RECORD": str(self.record),
            "STUB_MODELS": str(REFERENCE_MODELS),
            "STUB_ANSWER": json.dumps(ANSWER),
            "STUB_KILL_ONCE": str(self.root / "killed") if kill else "",
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
        }
        return subprocess.run(
            [sys.executable, "-m", "tenx.run", *args],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )

    def start(self, kill: bool = False, *extra: str) -> subprocess.CompletedProcess:
        return self.tenx(
            "run",
            "--raw",
            str(self.raw),
            "--goal",
            str(DEMO_GOAL),
            "--checks",
            str(DECLARATION),
            "--out",
            str(self.out),
            "--unisolated",
            *extra,
            kill=kill,
        )

    def test_a_run_that_passes_and_its_status(self):
        done = self.start()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(done.stdout.startswith("status: passed\n"), done.stdout)
        self.assertIn("gate: PASS (24 passed, 0 failed, 0 not run)", done.stdout)
        self.assertIn("cross-source  success", done.stdout)
        self.assertIn("run           gate PASS", done.stdout)
        seen = json.loads(self.record.read_text())
        self.assertIn("JOINS.md", seen["files"])
        self.assertEqual(Path(seen["cwd"]), self.out / "engineer" / "attempt-1" / "work")
        status = self.tenx("status", "--out", str(self.out))
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(
            status.stdout,
            "run: run\n"
            "status: passed\n"
            "attempts: cross-source 1, engineer 1, gate 1\n"
            # 0.1204 + 0.0812, the stand-in's reported costs.
            "charged to the budget: US$0.2016 (Claude Code's estimates, not a bill)\n",
        )
        # A finished run resumes to nothing and says how it ended.
        again = self.tenx("resume", "--out", str(self.out))
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertTrue(again.stdout.startswith("status: passed\n"))
        self.assertFalse((self.out / "engineer" / "attempt-2").exists())

    def test_refusals_write_nothing(self):
        self.out.mkdir()
        (self.out / "left.txt").write_text("from before")
        refused = self.start()
        self.assertEqual(refused.returncode, 2)
        self.assertIn("must be empty", refused.stderr)
        self.assertEqual(sorted(path.name for path in self.out.iterdir()), ["left.txt"])
        for command in ("status", "resume"):
            with self.subTest(command=command):
                done = self.tenx(command, "--out", str(self.root / "nothing"))
                self.assertEqual(done.returncode, 2)
                self.assertIn("no run is saved in", done.stderr)
                self.assertFalse((self.root / "nothing").exists())


if __name__ == "__main__":
    unittest.main()
