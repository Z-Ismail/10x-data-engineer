"""Tests for keeping sandboxed agents from opening Unix sockets.

Agents keep the network, so covering socket files one by one leaves out
those made after the agent starts, those in folders that are not hidden and,
on older kernels, abstract ones. A seccomp filter installed by the agents'
launcher refuses every new Unix socket instead. The command line and the
choice of filter are checked here; test_sandbox_unix_real tries the real
sandbox.
"""

import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tenx.sandbox import (
    FILTER_STEP,
    LANDLOCK_STEP,
    Bubblewrap,
    Mounts,
    SandboxError,
    default_sandbox,
    launcher,
)
from tenx.seccomp import unix_socket_filter

PROGRAM = unix_socket_filter("x86_64")


def after_options(arguments: list[str]) -> list[str]:
    return arguments[arguments.index("--") + 1 :]


class LauncherTest(unittest.TestCase):
    def test_the_filter_step_carries_the_program(self):
        source = launcher(False, PROGRAM)
        self.assertIn(f"PROGRAM = bytes.fromhex({PROGRAM.hex()!r})", source)
        self.assertIn(FILTER_STEP, source)
        self.assertNotIn(LANDLOCK_STEP, source)
        compile(source, "launcher", "exec")

    def test_both_steps_scope_first(self):
        source = launcher(True, PROGRAM)
        self.assertLess(source.index(LANDLOCK_STEP), source.index(FILTER_STEP))
        self.assertTrue(source.endswith("os.execvp(sys.argv[1], sys.argv[1:])\n"))
        compile(source, "launcher", "exec")

    def test_no_program_no_filter_step(self):
        for program in (None, b""):
            self.assertNotIn(FILTER_STEP, launcher(True, program))


class CommandTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="u")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        self.home, self.work = root / "home", root / "work"
        self.home.mkdir()
        self.work.mkdir()

    def command(self, program, scoped=False, **mounts) -> list[str]:
        sandbox = Bubblewrap(
            "bwrap",
            home=self.home,
            scratch=(),
            sockets=(),
            scope_abstract_sockets=scoped,
            unix_socket_filter=program,
        )
        return sandbox.command(["claude", "-p", "go"], Mounts(workspace=self.work, **mounts))

    def test_an_agent_starts_through_the_launcher_with_the_filter(self):
        self.assertEqual(
            after_options(self.command(PROGRAM)),
            [sys.executable, "-I", "-c", launcher(False, PROGRAM), "claude", "-p", "go"],
        )
        self.assertEqual(
            after_options(self.command(PROGRAM, scoped=True))[3],
            launcher(True, PROGRAM),
        )

    def test_a_command_without_the_network_starts_with_the_filter_alone(self):
        # As the gate's builds and the counting process do: no network, so
        # no abstract socket to scope, but socket files in shown folders.
        for scoped in (False, True):
            self.assertEqual(
                after_options(self.command(PROGRAM, scoped=scoped, network=False)),
                [sys.executable, "-I", "-c", launcher(False, PROGRAM), "claude", "-p", "go"],
            )

    def test_without_a_filter_a_command_without_the_network_is_not_wrapped(self):
        for scoped in (False, True):
            self.assertEqual(
                after_options(self.command(None, scoped=scoped, network=False)),
                ["claude", "-p", "go"],
            )

    def test_the_launcher_is_started_by_its_tidied_path(self):
        # As when the runner is started as ../venv/bin/python from a folder
        # the sandbox hides: the path is tidied, not resolved, since a
        # virtual environment's python is a link.
        given = "/opt/hidden/../venv/./bin/python"
        for network in (True, False):
            with mock.patch("tenx.sandbox.sys.executable", given):
                started = after_options(self.command(PROGRAM, network=network))[0]
            self.assertEqual(started, "/opt/venv/bin/python")

    def test_neither_limit_leaves_the_command_as_it_was(self):
        self.assertEqual(after_options(self.command(None)), ["claude", "-p", "go"])


# Stands in for bubblewrap: fails as the launcher does when the command it is
# given carries the filter step, and succeeds otherwise.
FAILING_FILTER = """#!/bin/sh
for a in "$@"; do
  case "$a" in
    *PROGRAM*)
      echo "cannot keep the agent from Unix sockets: Invalid argument" >&2
      exit 126 ;;
  esac
done
exit 0
"""
# The same, but only for a command without the network, as the start-up
# check's first probe is.
FAILING_OFFLINE_FILTER = """#!/bin/sh
case "$*" in
  *--unshare-net*PROGRAM*)
    echo "cannot keep the agent from Unix sockets: Invalid argument" >&2
    exit 126 ;;
esac
exit 0
"""


class DefaultSandboxTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.bwrap = Path(tmp.name) / "bwrap"
        self.write("#!/bin/sh\nexit 0\n")

    def write(self, text):
        self.bwrap.write_text(text, encoding="utf-8")
        self.bwrap.chmod(self.bwrap.stat().st_mode | stat.S_IXUSR)

    def sandbox(self, machine: str, abi: int = 0):
        with (
            mock.patch("tenx.sandbox.which", return_value=str(self.bwrap)),
            mock.patch("tenx.sandbox.landlock_abi", return_value=abi),
            mock.patch("tenx.sandbox.machine", return_value=machine),
        ):
            return default_sandbox(platform="linux")

    def test_the_filter_is_on_where_the_machine_has_one(self):
        for machine in ("x86_64", "aarch64"):
            sandbox = self.sandbox(machine)
            self.assertEqual(sandbox.unix_socket_filter, unix_socket_filter(machine))
            self.assertIn(
                "the agents and the gate's builds are kept from opening Unix sockets",
                sandbox.describe(),
            )

    def test_other_machines_run_without_it_and_say_so(self):
        sandbox = self.sandbox("riscv64")
        self.assertIsNone(sandbox.unix_socket_filter)
        self.assertIn("the agents are not kept from opening Unix sockets", sandbox.describe())

    def test_a_filter_that_cannot_be_installed_refuses_the_sandbox(self):
        # Even on a kernel without Landlock's scope.
        self.write(FAILING_FILTER)
        with self.assertRaisesRegex(
            SandboxError,
            r"cannot keep the agent from Unix sockets: Invalid argument\)\. "
            r"The agents could not be kept from sockets",
        ):
            self.sandbox("x86_64", abi=0)
        self.assertIsNone(self.sandbox("riscv64", abi=0).unix_socket_filter)

    def test_the_check_tries_the_filter_without_the_network_too(self):
        # As the gate's builds start, so a filter they cannot have fails
        # before any build, and is named as the cause.
        self.write(FAILING_OFFLINE_FILTER)
        with self.assertRaisesRegex(
            SandboxError, r"Unix sockets: Invalid argument\)\. The agents could not be kept"
        ):
            self.sandbox("x86_64")
        self.assertIsNone(self.sandbox("riscv64").unix_socket_filter)


if __name__ == "__main__":
    unittest.main()
