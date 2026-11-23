"""Tests for keeping agents from abstract sockets.

A socket bound to an abstract name has no file, so covering files cannot
hide it, and it belongs to the network, which agents share with the
computer: an X server or a session bus listening on such a name would
answer an agent. Landlock can scope a process so that it connects only to
abstract sockets made inside its own scope. The command line is checked
first; then, in the real sandbox, a process tries to reach a listener
outside.

The sandbox tests are skipped where bubblewrap cannot run, unless
TENX_REQUIRE_SANDBOX is set, as it is in CI; then they fail instead. Those
that need Landlock's scoping are skipped on kernels without it.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tenx.sandbox import SCOPE_LAUNCHER, Bubblewrap, Mounts


def after_options(arguments: list[str]) -> list[str]:
    return arguments[arguments.index("--") + 1 :]


class ScopeCommandTest(unittest.TestCase):
    """How the command line changes when abstract sockets are scoped."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="a")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        self.home = root / "home"
        self.work = root / "work"
        self.home.mkdir()
        self.work.mkdir()

    def command(self, scoped: bool, **mounts) -> list[str]:
        sandbox = Bubblewrap(
            "bwrap", home=self.home, scratch=(), sockets=(), scope_abstract_sockets=scoped
        )
        return sandbox.command(["claude", "-p", "go"], Mounts(workspace=self.work, **mounts))

    def test_an_agent_starts_through_the_launcher(self):
        self.assertEqual(
            after_options(self.command(True)),
            [sys.executable, "-I", "-c", SCOPE_LAUNCHER, "claude", "-p", "go"],
        )

    def test_the_launcher_is_given_its_python(self):
        # Python installed in the home folder, which the sandbox empties, as
        # a virtual environment often is, is put back for the launcher.
        python = self.home / "venv"
        python.mkdir()
        with mock.patch("tenx.sandbox.python_folders", return_value=(python,)):
            plain, scoped = self.command(False), self.command(True)
        work, home = str(self.work), str(self.home)
        start = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]
        start += ["--tmpfs", home, "--bind", work, work]
        end = ["--unshare-pid", "--die-with-parent", "--chdir", work, "--"]
        self.assertEqual(plain[: plain.index("--") + 1], start + end)
        shown = ["--ro-bind", str(python), str(python)]
        self.assertEqual(scoped[: scoped.index("--") + 1], start + shown + end)

    def test_a_command_without_the_network_is_not_wrapped(self):
        self.assertEqual(self.command(True, network=False), self.command(False, network=False))
        self.assertEqual(after_options(self.command(True, network=False)), ["claude", "-p", "go"])

    def test_scoping_off_leaves_the_command_as_it_was(self):
        self.assertEqual(after_options(self.command(False)), ["claude", "-p", "go"])

    def test_the_launcher_asks_for_the_abstract_socket_scope(self):
        # The ruleset handles no file or port access, only the scope, whose
        # first bit is LANDLOCK_SCOPE_ABSTRACT_UNIX_SOCKET.
        self.assertIn("Ruleset(0, 0, SCOPE_ABSTRACT_UNIX_SOCKET)", SCOPE_LAUNCHER)
        self.assertIn("SCOPE_ABSTRACT_UNIX_SOCKET = 1\n", SCOPE_LAUNCHER)
        compile(SCOPE_LAUNCHER, "launcher", "exec")


if __name__ == "__main__":
    unittest.main()
