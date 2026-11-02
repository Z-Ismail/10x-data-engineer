"""The parallel test runner: every module's verdict, counts and clean-up."""

from __future__ import annotations

import io
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

import parallel

PASSING = """
import unittest

class T(unittest.TestCase):
    def test_one(self):
        pass

    def test_two(self):
        self.assertEqual(2, 1 + 1)
"""

FAILING = """
import unittest

class T(unittest.TestCase):
    def test_passes(self):
        pass

    def test_fails(self):
        self.assertEqual(3, 1 + 1)
"""

BROKEN_IMPORT = """
import a_module_that_does_not_exist
"""

# Leaves a child that holds the output open and writes a heartbeat file
# beside the module, then hangs.
HANGS_WITH_A_CHILD = """
import subprocess, sys, time, unittest
from pathlib import Path

BEAT = Path(__file__).with_name("heartbeat")

class T(unittest.TestCase):
    def test_hangs(self):
        subprocess.Popen([sys.executable, "-c", f'''
import time
from pathlib import Path
while True:
    Path({str(BEAT)!r}).write_text(str(time.time()))
    print("beat", flush=True)
    time.sleep(0.1)
'''])
        time.sleep(600)
"""


# Passes once its child has written a heartbeat, leaving the child running.
LEAVES_A_CHILD = """
import subprocess, sys, time, unittest
from pathlib import Path

BEAT = Path(__file__).with_name("heartbeat")

class T(unittest.TestCase):
    def test_leaves_a_child(self):
        subprocess.Popen([sys.executable, "-c", f'''
import time
from pathlib import Path
while True:
    Path({str(BEAT)!r}).write_text(str(time.time()))
    time.sleep(0.1)
'''])
        while not BEAT.exists():
            time.sleep(0.05)
"""


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.names = []

    def tearDown(self):
        for name in self.names:
            sys.modules.pop(name, None)
        while str(self.folder) in sys.path:
            sys.path.remove(str(self.folder))
        self.tmp.cleanup()

    def module(self, kind: str, source: str) -> str:
        # Unique names: counting imports each module into this process.
        name = f"test_{kind}_{uuid.uuid4().hex[:8]}"
        (self.folder / f"{name}.py").write_text(source)
        self.names.append(name)
        return name

    def run_all(self, timeout: float = 120.0, jobs: int = 2):
        out = io.StringIO()
        outcomes = parallel.run(self.folder, jobs, timeout, out=out)
        return {outcome.module: outcome for outcome in outcomes}, out.getvalue()

    def test_passing_modules_pass_with_their_counts(self):
        first = self.module("a", PASSING)
        second = self.module("b", PASSING)
        outcomes, text = self.run_all()
        self.assertEqual(set(outcomes), {first, second})
        for name in (first, second):
            self.assertTrue(outcomes[name].passed)
            self.assertEqual((outcomes[name].expected, outcomes[name].ran), (2, 2))
        self.assertIn(f"===== {first}: OK, 2 tests", text)
        self.assertIn("test_two", text)
        self.assertEqual(parallel.report(list(outcomes.values()), 1.0).splitlines()[-1], "OK")

    def test_a_failing_test_fails_its_module_and_the_run(self):
        good = self.module("good", PASSING)
        bad = self.module("bad", FAILING)
        outcomes, text = self.run_all()
        self.assertTrue(outcomes[good].passed)
        self.assertFalse(outcomes[bad].passed)
        self.assertEqual(outcomes[bad].returncode, 1)
        self.assertEqual(outcomes[bad].ran, 2)
        self.assertIn(f"{bad}: FAILED (exit 1), 2 tests", text)
        self.assertIn("AssertionError: 3 != 2", text)
        last = parallel.report(list(outcomes.values()), 1.0).splitlines()
        self.assertEqual(last[-2], "Ran 4 of 4 tests in 2 modules in 1.0 s")
        self.assertEqual(last[-1], f"FAILED: {bad}")

    def test_a_module_that_cannot_be_imported_fails(self):
        bad = self.module("import", BROKEN_IMPORT)
        outcomes, text = self.run_all()
        self.assertFalse(outcomes[bad].passed)
        self.assertNotEqual(outcomes[bad].returncode, 0)
        self.assertIn("a_module_that_does_not_exist", text)

    def test_a_module_that_runs_fewer_tests_than_counted_fails(self):
        name = self.module("short", PASSING)
        with mock.patch.object(parallel, "count", return_value=3):
            outcomes, text = self.run_all()
        self.assertEqual(outcomes[name].returncode, 0)
        self.assertFalse(outcomes[name].passed)
        self.assertIn(f"{name}: FAILED (ran 2 of 3 tests)", text)

    def test_a_hanging_module_is_stopped_with_everything_it_started(self):
        hangs = self.module("hangs", HANGS_WITH_A_CHILD)
        good = self.module("fine", PASSING)
        beat = self.folder / "heartbeat"
        started = time.monotonic()
        with mock.patch.object(parallel, "GRACE", 1.0):
            outcomes, text = self.run_all(timeout=3.0)
        self.assertLess(time.monotonic() - started, 20)
        self.assertTrue(outcomes[hangs].timed_out)
        self.assertFalse(outcomes[hangs].passed)
        self.assertIn(f"{hangs}: TIMED OUT", text)
        self.assertIn("beat", outcomes[hangs].output)
        self.assertTrue(outcomes[good].passed)
        self.assertTrue(beat.exists())
        last = beat.read_text()
        time.sleep(1.0)
        self.assertEqual(beat.read_text(), last, "the child kept running")

    def test_a_child_left_by_a_passing_module_is_stopped(self):
        name = self.module("leaves", LEAVES_A_CHILD)
        beat = self.folder / "heartbeat"
        outcomes, _ = self.run_all()
        self.assertTrue(outcomes[name].passed)
        last = beat.read_text()
        time.sleep(1.0)
        self.assertEqual(beat.read_text(), last, "the child kept running")

    def test_modules_start_slowest_first_then_by_name(self):
        for name in ("test_b", "test_sandbox", "test_a", "test_gate", "helper"):
            (self.folder / f"{name}.py").write_text("")
        self.assertEqual(
            parallel.modules(self.folder), ["test_gate", "test_sandbox", "test_a", "test_b"]
        )

    def test_the_command_line_exits_1_on_a_failure_and_0_otherwise(self):
        self.module("good", PASSING)
        with mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(parallel.main(["--folder", str(self.folder), "--jobs", "1"]), 0)
            self.module("bad", FAILING)
            self.assertEqual(parallel.main(["--folder", str(self.folder), "--jobs", "1"]), 1)

    def test_an_empty_folder_fails(self):
        with mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(parallel.main(["--folder", str(self.folder)]), 1)


if __name__ == "__main__":
    unittest.main()
