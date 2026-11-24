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

import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from test_gate_sandbox import needs_sandbox

from tenx.sandbox import (
    LANDLOCK_SCOPE_ABI,
    LANDLOCK_STEP,
    SCOPE_FAILED,
    Bubblewrap,
    Mounts,
    SandboxError,
    default_sandbox,
    landlock_abi,
    launcher,
    python_folders,
)

# Tries each way out and prints what happened, as JSON.
PROBE = """
import json, os, socket, sys
outside, path, port = sys.argv[1], sys.argv[2], int(sys.argv[3])
found = {}


def attempt(name, family, address, kind=socket.SOCK_STREAM):
    peer = socket.socket(family, kind)
    try:
        peer.connect(address)
        peer.sendall(b"probe")
        found[name] = "connected"
    except OSError as error:
        found[name] = type(error).__name__
    finally:
        peer.close()


attempt("outside", socket.AF_UNIX, "\\0" + outside)
attempt("path", socket.AF_UNIX, path)
attempt("tcp", socket.AF_INET, ("127.0.0.1", port))
# An abstract socket of its own, between its own processes.
own = socket.socket(socket.AF_UNIX)
own.bind("\\0" + outside + "-own")
own.listen()
child = os.fork()
if child == 0:
    attempt("own", socket.AF_UNIX, "\\0" + outside + "-own")
    os._exit(0)
own.settimeout(10)
try:
    found["own"] = "connected" if own.accept()[0].recv(8) == b"probe" else "nothing"
except OSError as error:
    found["own"] = type(error).__name__
os.waitpid(child, 0)
print(json.dumps(found))
"""


def serve(server: socket.socket, received: list[bytes]) -> None:
    """Keep what each connection sends until the server is closed."""
    while True:
        try:
            peer, _ = server.accept()
        except OSError:
            return
        with peer:
            received.append(peer.recv(64))


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
            [sys.executable, "-I", "-c", launcher(True, None), "claude", "-p", "go"],
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
        self.assertIn("Ruleset(0, 0, SCOPE_ABSTRACT_UNIX_SOCKET)", LANDLOCK_STEP)
        self.assertIn("SCOPE_ABSTRACT_UNIX_SOCKET = 1\n", LANDLOCK_STEP)
        self.assertIn(LANDLOCK_STEP, launcher(True, None))
        self.assertNotIn(LANDLOCK_STEP, launcher(False, b"\0" * 8))
        compile(launcher(True, None), "launcher", "exec")


@needs_sandbox
@unittest.skipUnless(
    landlock_abi() >= LANDLOCK_SCOPE_ABI,
    f"Landlock ABI {landlock_abi()} on Linux {os.uname().release}, scoping needs "
    f"{LANDLOCK_SCOPE_ABI}",
)
class ScopeSandboxTest(unittest.TestCase):
    """A process in the real sandbox, network kept, tries to reach sockets outside."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="a")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        self.work = root / "work"
        self.shown = root / "shown"
        self.work.mkdir()
        self.shown.mkdir()
        self.name = f"tenx-test-{os.getpid()}-{id(self)}"
        self.received = {"outside": [], "path": [], "tcp": []}
        self.serve("outside", socket.AF_UNIX, "\0" + self.name)
        self.serve("path", socket.AF_UNIX, str(self.shown / "s.sock"))
        self.port = self.serve("tcp", socket.AF_INET, ("127.0.0.1", 0))

    def serve(self, name, family, address):
        server = socket.socket(family)
        server.bind(address)
        server.listen()
        self.addCleanup(server.close)
        threading.Thread(target=serve, args=(server, self.received[name]), daemon=True).start()
        return server.getsockname()[1] if family == socket.AF_INET else None

    def sandbox(self, scoped: bool) -> Bubblewrap:
        return Bubblewrap(default_sandbox().executable, scope_abstract_sockets=scoped)

    def run_probe(self, sandbox: Bubblewrap) -> subprocess.CompletedProcess:
        probe = [sys.executable, "-I", "-c", PROBE, self.name, str(self.shown / "s.sock")]
        mounts = Mounts(workspace=self.work, read_only=(self.shown, *python_folders()))
        return subprocess.run(
            sandbox.command([*probe, str(self.port)], mounts),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def found(self, sandbox: Bubblewrap) -> dict:
        finished = self.run_probe(sandbox)
        self.assertEqual(finished.returncode, 0, finished.stderr)
        return json.loads(finished.stdout)

    def test_a_scoped_agent_cannot_reach_an_abstract_socket_outside(self):
        found = self.found(self.sandbox(True))
        self.assertEqual(found["outside"], "PermissionError")
        self.assertEqual(self.received["outside"], [])

    def test_without_the_scope_it_can(self):
        # The positive control: the gap the scope closes is there without it.
        self.assertEqual(self.found(self.sandbox(False))["outside"], "connected")

    def test_what_agents_need_still_works(self):
        found = self.found(self.sandbox(True))
        self.assertEqual(
            {name: found[name] for name in ("own", "path", "tcp")},
            {"own": "connected", "path": "connected", "tcp": "connected"},
        )

    def test_the_launcher_starts_nothing_when_it_cannot_scope(self):
        # The real launcher with a system call the kernel does not have.
        broken = LANDLOCK_STEP.replace("= 444, 446", "= 99999, 446")
        self.assertNotEqual(broken, LANDLOCK_STEP)
        marker = self.work / "started"
        command = [sys.executable, "-I", "-c", f"open({str(marker)!r}, 'w')"]
        sandbox = self.sandbox(True)
        with mock.patch("tenx.sandbox.LANDLOCK_STEP", broken):
            arguments = sandbox.command(command, Mounts(workspace=self.work))
        finished = subprocess.run(arguments, capture_output=True, text=True, check=False)
        self.assertEqual(finished.returncode, SCOPE_FAILED)
        self.assertIn("cannot keep the agent from abstract sockets", finished.stderr)
        self.assertFalse(marker.exists())


# A stand-in for bubblewrap: fails as the launcher does when it is asked to
# start a command through the launcher, and succeeds otherwise.
FAILING_LAUNCHER = """#!/bin/sh
for a in "$@"; do
  if [ "$a" = -I ]; then
    echo "cannot keep the agent from abstract sockets: Invalid argument" >&2
    exit 126
  fi
done
exit 0
"""


class DefaultSandboxTest(unittest.TestCase):
    """Where the scope is turned on, and how a sandbox that cannot use it is refused."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.bwrap = Path(tmp.name) / "bwrap"
        self.bwrap.write_text(FAILING_LAUNCHER, encoding="utf-8")
        self.bwrap.chmod(self.bwrap.stat().st_mode | stat.S_IXUSR)

    def sandbox(self, abi: int):
        # Without the Unix socket filter, so that only Landlock decides.
        with (
            mock.patch("tenx.sandbox.which", return_value=str(self.bwrap)),
            mock.patch("tenx.sandbox.landlock_abi", return_value=abi),
            mock.patch("tenx.sandbox.seccomp.unix_socket_filter", return_value=None),
        ):
            return default_sandbox(platform="linux")

    def test_the_scope_is_off_on_kernels_without_it(self):
        for abi in (0, 1, 5):
            sandbox = self.sandbox(abi)
            self.assertFalse(sandbox.scope_abstract_sockets, abi)
            self.assertIn("not from abstract sockets outside by Landlock", sandbox.describe())

    def test_a_kernel_with_the_scope_but_a_launcher_that_fails_is_refused(self):
        for abi in (6, 7):
            with self.assertRaisesRegex(
                SandboxError,
                r"cannot set up a sandbox here \(cannot keep the agent from abstract "
                r"sockets: Invalid argument\)\. The agents could not be kept from sockets",
            ):
                self.sandbox(abi)

    def test_the_scope_is_on_where_the_kernel_has_it(self):
        self.bwrap.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        for abi in (6, 7, 8):
            sandbox = self.sandbox(abi)
            self.assertTrue(sandbox.scope_abstract_sockets, abi)
            self.assertIn("and from abstract sockets outside", sandbox.describe())


if __name__ == "__main__":
    unittest.main()
