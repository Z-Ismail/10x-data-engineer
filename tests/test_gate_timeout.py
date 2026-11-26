"""Tests for the time limit on the gate's dbt builds.

A project's model can loop forever, or start a process that outlives the
build. The gate stops a build at its time limit with every process it
started, does not wait for a process the build left behind, and reads back
only the end of what a build printed. The processes started here carry a
token on their command line, so the tests can look for them afterwards.

The sandboxed tests are skipped where bubblewrap cannot run, unless
TENX_REQUIRE_SANDBOX is set, as it is in CI; then they fail instead.
"""

import io
import os
import shutil
import sys
import tempfile
import time
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from demo.generate import generate
from tenx.gate import (
    BUILD_TIMEOUT_SECONDS,
    FAIL,
    OUTPUT_LIMIT,
    SKIP,
    _build,
    build_project,
    check_build,
    main,
    run_gate,
)
from tenx.sandbox import SandboxError, Unisolated, default_sandbox

FIXTURES = Path(__file__).parent / "fixtures"
PROJECT = FIXTURES / "demo_project"
DECLARATION = FIXTURES / "demo_gate.json"
# A stopped build must end well before this: the limits used are seconds.
PROMPTLY = 60.0


def _sandbox_problem() -> str:
    try:
        default_sandbox()
    except SandboxError as error:
        return str(error)
    return ""


SANDBOX_PROBLEM = _sandbox_problem()
needs_sandbox = unittest.skipIf(
    bool(SANDBOX_PROBLEM) and not os.environ.get("TENX_REQUIRE_SANDBOX"),
    f"no working sandbox: {SANDBOX_PROBLEM}",
)


def running(token: str) -> list[int]:
    """Process ids whose command line holds token, other than this one's."""
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            command = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if token.encode() in command:
            found.append(int(entry.name))
    return found


def gone(token: str, within: float = 5.0) -> bool:
    """Whether every process with token has ended, allowing a moment to reap."""
    deadline = time.monotonic() + within
    while running(token):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.1)
    return True


def sleeper(token: str, seconds: int = 600) -> str:
    """Python code that sleeps, with token on its command line."""
    return f"import time; time.sleep({seconds}); {token!r}"


class BuildProcessTest(unittest.TestCase):
    """The build's process: stopped at the limit, not waited for afterwards, output capped."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.token = f"tenx-test-{uuid.uuid4().hex}"
        self.addCleanup(self.kill_leftovers)

    def kill_leftovers(self):
        for pid in running(self.token):
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass

    def test_a_build_past_its_limit_is_stopped_with_its_children(self):
        child = f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', {sleeper(self.token)!r}]); time.sleep(600)"
        started = time.monotonic()
        code, output = _build([sys.executable, "-c", child], self.root, dict(os.environ), 2)
        self.assertLess(time.monotonic() - started, PROMPTLY)
        self.assertIsNone(code)
        self.assertTrue(output.endswith("the gate stopped the build after 2 seconds\n"), output)
        self.assertTrue(gone(self.token))

    def test_a_process_left_holding_the_output_is_not_waited_for(self):
        # The build ends at once; a process it started keeps its output open.
        left = f"import subprocess, sys; subprocess.Popen([sys.executable, '-c', {sleeper(self.token)!r}]); print('done')"
        started = time.monotonic()
        code, output = _build([sys.executable, "-c", left], self.root, dict(os.environ), 300)
        self.assertLess(time.monotonic() - started, PROMPTLY)
        self.assertEqual((code, output), (0, "done\n"))
        self.assertTrue(gone(self.token))

    def test_a_build_that_ends_keeps_its_exit_status_and_output(self):
        script = "import sys; print('first'); print('second', file=sys.stderr); sys.exit(3)"
        code, output = _build([sys.executable, "-c", script], self.root, dict(os.environ), 300)
        self.assertEqual(code, 3)
        self.assertEqual(sorted(output.split()), ["first", "second"])

    def test_only_the_end_of_a_long_output_is_read(self):
        script = "import sys; sys.stdout.write('a' * 3_000_000 + 'END')"
        code, output = _build([sys.executable, "-c", script], self.root, dict(os.environ), 300)
        self.assertEqual(code, 0)
        self.assertEqual(len(output), OUTPUT_LIMIT)
        self.assertTrue(output.endswith("aEND"))

    def test_a_stopped_build_fails_the_build_check_and_every_model(self):
        results, relations = check_build(
            None, None, [], "the gate stopped the build after 9 seconds"
        )
        self.assertEqual(relations, {})
        (result,) = results
        self.assertEqual(result.status, FAIL)
        self.assertTrue(
            result.detail.startswith(
                "dbt build did not finish within the gate's time limit and was stopped\n"
            ),
            result.detail,
        )
        self.assertIn("after 9 seconds", result.detail)


HANGING_MODEL = """
import subprocess
import sys
import time


def model(dbt, session):
    subprocess.Popen([sys.executable, "-c", {child!r}])
    time.sleep(600)
    return session.sql("select 1 as x")
"""


def hanging_project(root: Path, token: str) -> Path:
    """The reference project with a Python model that never finishes."""
    project = root / "project"
    shutil.copytree(PROJECT, project)
    model = HANGING_MODEL.format(child=sleeper(token))
    (project / "models" / "hang.py").write_text(model, encoding="utf-8")
    return project


class HangingProjectTest(unittest.TestCase):
    """A real dbt build whose model hangs, unisolated and in the sandbox."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.token = f"tenx-test-{uuid.uuid4().hex}"
        self.project = hanging_project(self.root, self.token)
        self.raw = self.root / "data"
        generate(11, 20, self.raw, self.root / "answers")

    def build(self, sandbox) -> tuple:
        started = time.monotonic()
        code, results, output = build_project(
            self.project, self.raw, self.root / "work", sandbox=sandbox, timeout=20
        )
        self.assertLess(time.monotonic() - started, 20 + PROMPTLY)
        return code, results, output

    def test_unisolated_the_build_is_stopped(self):
        code, results, output = self.build(Unisolated())
        self.assertEqual((code, results), (None, None))
        self.assertIn("the gate stopped the build after 20 seconds", output)
        self.assertTrue(gone(self.token))

    @needs_sandbox
    def test_in_the_sandbox_the_build_is_stopped(self):
        code, results, output = self.build(default_sandbox())
        self.assertEqual((code, results), (None, None))
        self.assertIn("the gate stopped the build after 20 seconds", output)
        self.assertTrue(gone(self.token))

    @needs_sandbox
    def test_the_gate_fails_the_project_and_skips_the_rest(self):
        results = run_gate(
            self.project,
            self.raw,
            DECLARATION,
            self.root / "gate",
            default_sandbox(),
            timeout=20,
        )
        self.assertTrue(gone(self.token))
        build = [result for result in results if result.check == "build"]
        self.assertTrue(build and all(result.status == FAIL for result in build))
        self.assertIn("time limit", build[0].detail)
        others = [result for result in results if result.check != "build"]
        self.assertTrue(others and all(result.status == SKIP for result in others))


class CommandLineTest(unittest.TestCase):
    """--build-timeout sets the limit; the default is the gate's own."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.token = f"tenx-test-{uuid.uuid4().hex}"
        self.raw = self.root / "data"
        generate(11, 20, self.raw, self.root / "answers")

    def main(self, project: Path, *flags: str) -> tuple[int, str, str]:
        arguments = [str(project), "--raw", str(self.raw), "--checks", str(DECLARATION)]
        with (
            redirect_stdout(io.StringIO()) as printed,
            redirect_stderr(io.StringIO()) as errors,
        ):
            code = main([*arguments, "--unisolated", *flags])
        return code, printed.getvalue(), errors.getvalue()

    def test_a_hanging_project_fails_at_the_limit_given(self):
        project = hanging_project(self.root, self.token)
        code, printed, _ = self.main(project, "--build-timeout", "15")
        self.assertEqual(code, 1, printed)
        self.assertTrue(printed.startswith("gate: FAIL"), printed)
        self.assertIn("did not finish within the gate's time limit", printed)
        self.assertTrue(gone(self.token))

    def test_the_limit_reaches_the_gate(self):
        with mock.patch("tenx.gate.run_gate", return_value=[]) as gate:
            self.main(PROJECT)
            self.main(PROJECT, "--build-timeout", "2.5")
        timeouts = [call.kwargs["timeout"] for call in gate.call_args_list]
        self.assertEqual(timeouts, [BUILD_TIMEOUT_SECONDS, 2.5])
        self.assertEqual(BUILD_TIMEOUT_SECONDS, 1200.0)

    def test_a_limit_that_is_not_a_positive_number_is_refused(self):
        for value in ("0", "-5", "nan", "inf", "soon", ""):
            with self.subTest(value=value), mock.patch("tenx.gate.run_gate") as gate:
                with self.assertRaises(SystemExit) as stopped:
                    self.main(PROJECT, "--build-timeout", value)
                self.assertEqual(stopped.exception.code, 2)
                gate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
