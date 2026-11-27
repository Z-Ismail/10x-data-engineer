"""Tests for the memory limit on the gate's dbt builds.

A project's model can take memory without end. Each build starts with a
limit on the memory it can address, which everything it starts inherits,
in the sandbox as well, so an allocation past it fails inside the build
rather than taking the computer's memory.

The sandboxed tests are skipped where bubblewrap cannot run, unless
TENX_REQUIRE_SANDBOX is set, as it is in CI; then they fail instead.
"""

import io
import os
import resource
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from demo.generate import generate
from tenx.gate import (
    BUILD_MEMORY_FLOOR,
    FAIL,
    GIB,
    PASS,
    _build,
    build_memory,
    build_project,
    check_build,
    limited,
    load_declaration,
    main,
    run_gate,
)
from tenx.sandbox import Mounts, SandboxError, default_sandbox

FIXTURES = Path(__file__).parent / "fixtures"
PROJECT = FIXTURES / "demo_project"
DECLARATION = FIXTURES / "demo_gate.json"
MIB = 2**20
# Small enough to reach quickly, large enough for Python to start in.
CAP = 1 * GIB
# Prints the address-space limits of a process the build started.
CHILD_LIMITS = (
    "import subprocess, sys; "
    "subprocess.run([sys.executable, '-c', "
    "'import resource; print(*resource.getrlimit(resource.RLIMIT_AS))'], check=True)"
)


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


class LimitedBuildTest(unittest.TestCase):
    """The limit holds for the build and what it starts, and fails allocations past it."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()

    def run_python(self, code: str, memory: int | None = CAP) -> tuple[int | None, str]:
        return _build([sys.executable, "-c", code], self.root, dict(os.environ), 120, memory)

    def test_a_process_the_build_starts_has_the_limit(self):
        code, output = self.run_python(CHILD_LIMITS)
        self.assertEqual((code, output.split()), (0, [str(CAP), str(CAP)]))

    def test_an_allocation_past_the_limit_fails_inside_the_build(self):
        code, output = self.run_python(f"bytearray({3 * CAP})")
        self.assertEqual(code, 1)
        self.assertIn("MemoryError", output)

    def test_shared_memory_counts_towards_the_limit(self):
        # Not counted by RLIMIT_DATA, which covers private mappings only.
        script = f"import mmap; mmap.mmap(-1, {2 * CAP}, flags=mmap.MAP_SHARED)"
        code, output = self.run_python(script)
        self.assertEqual(code, 1)
        self.assertIn("Cannot allocate memory", output)

    def test_an_allocation_under_the_limit_succeeds(self):
        code, output = self.run_python(f"print(len(bytearray({64 * MIB})))")
        self.assertEqual((code, output), (0, f"{64 * MIB}\n"))

    def test_without_a_limit_the_build_keeps_the_callers(self):
        code, output = self.run_python(CHILD_LIMITS, memory=None)
        expected = [str(value) for value in resource.getrlimit(resource.RLIMIT_AS)]
        self.assertEqual((code, output.split()), (0, expected))

    def test_a_lower_hard_limit_already_set_is_kept(self):
        lower = CAP - 64 * MIB
        command = limited([sys.executable, "-c", CHILD_LIMITS], 2 * CAP)
        script = (
            "import os, resource, sys; "
            f"resource.setrlimit(resource.RLIMIT_AS, ({lower}, {lower})); "
            f"os.execv(sys.executable, {command!r})"
        )
        code, output = self.run_python(script, memory=None)
        self.assertEqual((code, output.split()), (0, [str(lower), str(lower)]))

    @needs_sandbox
    def test_in_the_sandbox_the_limit_holds_inside(self):
        sandbox = default_sandbox()
        mounts = Mounts(workspace=self.root, network=False)
        for script, expected in (
            (CHILD_LIMITS, (0, f"{CAP} {CAP}\n")),
            (f"bytearray({3 * CAP})", 1),
        ):
            with self.subTest(script=script):
                command = sandbox.command([sys.executable, "-c", script], mounts)
                code, output = _build(command, self.root, dict(os.environ), 120, CAP)
                if isinstance(expected, tuple):
                    self.assertEqual((code, output), expected)
                else:
                    self.assertEqual(code, expected, output)
                    self.assertIn("MemoryError", output)


GREEDY_MODEL = """
def model(dbt, session):
    hoard = bytearray({size})
    return session.sql("select 1 as x")
"""
# Sorting this many random numbers needs more memory than DuckDB has under a
# 3 GiB build limit unless it writes part of the work to disk.
LARGE_SORT = "select range as x, random() as r from range(40000000) order by r"


@needs_sandbox
class RealBuildTest(unittest.TestCase):
    """Real dbt builds of the reference project in the sandbox, under the limit."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.project = self.root / "project"
        shutil.copytree(PROJECT, self.project)
        self.raw = self.root / "data"
        generate(11, 20, self.raw, self.root / "answers")

    def add_model(self, name: str, text: str) -> None:
        (self.project / "models" / name).write_text(text, encoding="utf-8")

    def test_duckdb_is_told_a_quarter_of_the_limit(self):
        work = self.root / "work"
        code, _, output = build_project(
            self.project, self.raw, work, sandbox=default_sandbox(), memory=4 * GIB
        )
        self.assertEqual(code, 0, output)
        profile = (work / "profiles" / "profiles.yml").read_text(encoding="utf-8")
        self.assertIn("      settings:\n        memory_limit: '1024MiB'\n", profile)

    def test_the_default_limit_builds_the_reference_project(self):
        results = run_gate(self.project, self.raw, DECLARATION, None, default_sandbox())
        self.assertTrue(results and all(result.status == PASS for result in results))

    def test_a_model_that_takes_too_much_memory_fails_its_build(self):
        self.add_model("greedy.py", GREEDY_MODEL.format(size=4 * GIB))
        code, run_results, output = build_project(
            self.project, self.raw, self.root / "work", sandbox=default_sandbox(), memory=2 * GIB
        )
        self.assertEqual(code, 1, output[-2000:])
        nodes = {node["unique_id"]: node for node in run_results["results"]}
        self.assertEqual(nodes["model.demo.greedy"]["status"], "error")
        self.assertIn("MemoryError", nodes["model.demo.greedy"]["message"])
        results, _ = check_build(code, run_results, load_declaration(DECLARATION), output)
        self.assertEqual((results[0].subject, results[0].status), ("project", FAIL))
        self.assertIn("model.demo.greedy: error", results[0].detail)

    def test_a_large_sort_is_written_to_disk_within_the_limit(self):
        self.add_model("large_sort.sql", LARGE_SORT)
        code, results, output = build_project(
            self.project, self.raw, self.root / "work", sandbox=default_sandbox(), memory=3 * GIB
        )
        self.assertEqual(code, 0, output[-2000:])
        statuses = {result["unique_id"]: result["status"] for result in results["results"]}
        self.assertEqual(statuses["model.demo.large_sort"], "success")


class DefaultLimitTest(unittest.TestCase):
    """Half the computer's memory, never less than the floor."""

    def physical(self, total: int):
        pages = {"SC_PAGE_SIZE": 4096, "SC_PHYS_PAGES": total // 4096}
        return mock.patch("tenx.gate.os.sysconf", side_effect=pages.__getitem__)

    def test_half_the_computers_memory(self):
        with self.physical(64 * GIB):
            self.assertEqual(build_memory(), 32 * GIB)

    def test_never_less_than_the_floor(self):
        self.assertEqual(BUILD_MEMORY_FLOOR, 4 * GIB)
        for total in (2 * GIB, 6 * GIB, 8 * GIB):
            with self.subTest(total=total), self.physical(total):
                self.assertEqual(build_memory(), 4 * GIB)

    def test_the_floor_when_the_memory_cannot_be_read(self):
        with mock.patch("tenx.gate.os.sysconf", side_effect=ValueError("unknown")):
            self.assertEqual(build_memory(), BUILD_MEMORY_FLOOR)


class CommandLineTest(unittest.TestCase):
    """--build-memory sets the limit in GiB; without it the gate's default applies."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.raw = self.root / "data"
        generate(11, 20, self.raw, self.root / "answers")

    def main(self, project: Path, *flags: str) -> tuple[int, str]:
        arguments = [str(project), "--raw", str(self.raw), "--checks", str(DECLARATION)]
        with redirect_stdout(io.StringIO()) as printed, redirect_stderr(io.StringIO()):
            code = main([*arguments, "--unisolated", *flags])
        return code, printed.getvalue()

    def test_the_limit_reaches_the_gate(self):
        with mock.patch("tenx.gate.run_gate", return_value=[]) as gate:
            self.main(PROJECT)
            self.main(PROJECT, "--build-memory", "6")
            self.main(PROJECT, "--build-memory", "2.5")
        limits = [call.kwargs["memory"] for call in gate.call_args_list]
        self.assertEqual(limits, [None, 6 * 1024**3, 2684354560])

    def test_a_limit_that_is_not_a_positive_number_is_refused(self):
        for value in ("0", "-1", "nan", "inf", "lots", ""):
            with self.subTest(value=value), mock.patch("tenx.gate.run_gate") as gate:
                with self.assertRaises(SystemExit) as stopped:
                    self.main(PROJECT, "--build-memory", value)
                self.assertEqual(stopped.exception.code, 2)
                gate.assert_not_called()

    def test_a_project_past_the_limit_given_fails(self):
        project = self.root / "project"
        shutil.copytree(PROJECT, project)
        (project / "models" / "greedy.py").write_text(
            GREEDY_MODEL.format(size=4 * GIB), encoding="utf-8"
        )
        code, printed = self.main(project, "--build-memory", "2")
        self.assertEqual(code, 1, printed)
        self.assertTrue(printed.startswith("gate: FAIL"), printed)
        self.assertIn("model.demo.greedy: error", printed)


if __name__ == "__main__":
    unittest.main()
