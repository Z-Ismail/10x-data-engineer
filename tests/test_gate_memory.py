"""Tests for the memory limit on the gate's dbt builds.

A project's model can take memory without end. Each build starts with a
limit on the memory it can address, which everything it starts inherits,
in the sandbox as well, so an allocation past it fails inside the build
rather than taking the computer's memory.

The sandboxed tests are skipped where bubblewrap cannot run, unless
TENX_REQUIRE_SANDBOX is set, as it is in CI; then they fail instead.
"""

import os
import resource
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tenx.gate import BUILD_MEMORY_FLOOR, GIB, _build, build_memory, limited
from tenx.sandbox import Mounts, SandboxError, default_sandbox

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


if __name__ == "__main__":
    unittest.main()
