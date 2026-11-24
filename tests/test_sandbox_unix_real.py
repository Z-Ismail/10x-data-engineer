"""The Unix socket filter in the real sandbox.

A process in bubblewrap, network kept as agents have it, tries to reach a
socket file in a folder shown to it (as a socket made after the sandbox
started, or outside the covered folders, would be) and an abstract socket
outside, with Landlock's scope off so that the filter alone decides. The
same sandbox without the filter is the positive control.

Skipped where bubblewrap cannot run, unless TENX_REQUIRE_SANDBOX is set, as
it is in CI; then they fail instead.
"""

import json
import os
import platform
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from test_gate_sandbox import needs_sandbox

from tenx.sandbox import SCOPE_FAILED, Bubblewrap, Mounts, default_sandbox, python_folders
from tenx.seccomp import unix_socket_filter

PROGRAM = unix_socket_filter(platform.machine())

PROBE = """
import json, socket, subprocess, sys
outside, path, port = sys.argv[1], sys.argv[2], int(sys.argv[3])
found = {}


def attempt(name, family, address):
    try:
        peer = socket.socket(family, socket.SOCK_STREAM)
    except OSError as error:
        found[name] = type(error).__name__
        return
    try:
        peer.connect(address)
        peer.sendall(b"probe")
        found[name] = "connected"
    except OSError as error:
        found[name] = type(error).__name__
    finally:
        peer.close()


attempt("abstract", socket.AF_UNIX, "\\0" + outside)
attempt("path", socket.AF_UNIX, path)
attempt("tcp", socket.AF_INET, ("127.0.0.1", port))
first, second = socket.socketpair()
first.sendall(b"x")
found["pair"] = "connected" if second.recv(1) == b"x" else "nothing"
child = subprocess.run(
    [sys.executable, "-c", "import socket, sys; socket.socket(socket.AF_UNIX).connect(sys.argv[1])",
     path],
    capture_output=True, text=True,
)
found["child"] = "connected" if child.returncode == 0 else "refused"
print(json.dumps(found))
"""


def serve(server: socket.socket, received: list[bytes]) -> None:
    while True:
        try:
            peer, _ = server.accept()
        except OSError:
            return
        with peer:
            received.append(peer.recv(64))


@needs_sandbox
@unittest.skipUnless(PROGRAM, f"no filter for {platform.machine()}")
class FilterSandboxTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="u")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        self.work, self.shown = root / "work", root / "shown"
        self.work.mkdir()
        self.shown.mkdir()
        self.name = f"tenx-unix-{os.getpid()}-{id(self)}"
        self.received = {"abstract": [], "path": [], "tcp": []}
        self.serve("abstract", socket.AF_UNIX, "\0" + self.name)
        self.serve("path", socket.AF_UNIX, str(self.shown / "s.sock"))
        self.port = self.serve("tcp", socket.AF_INET, ("127.0.0.1", 0))

    def serve(self, name, family, address):
        server = socket.socket(family)
        server.bind(address)
        server.listen()
        self.addCleanup(server.close)
        threading.Thread(target=serve, args=(server, self.received[name]), daemon=True).start()
        return server.getsockname()[1] if family == socket.AF_INET else None

    def sandbox(self, program) -> Bubblewrap:
        executable = default_sandbox().executable
        return Bubblewrap(executable, scope_abstract_sockets=False, unix_socket_filter=program)

    def run_probe(self, sandbox: Bubblewrap, command=None) -> subprocess.CompletedProcess:
        probe = [sys.executable, "-I", "-c", PROBE, self.name, str(self.shown / "s.sock")]
        command = command or [*probe, str(self.port)]
        mounts = Mounts(workspace=self.work, read_only=(self.shown, *python_folders()))
        return subprocess.run(
            sandbox.command(command, mounts),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def found(self, program) -> dict:
        finished = self.run_probe(self.sandbox(program))
        self.assertEqual(finished.returncode, 0, finished.stderr)
        return json.loads(finished.stdout)

    def test_no_unix_socket_outside_can_be_reached(self):
        found = self.found(PROGRAM)
        self.assertEqual(
            {name: found[name] for name in ("abstract", "path", "child")},
            {"abstract": "PermissionError", "path": "PermissionError", "child": "refused"},
        )
        self.assertEqual((self.received["abstract"], self.received["path"]), ([], []))

    def test_without_the_filter_each_can(self):
        found = self.found(None)
        self.assertEqual(
            {name: found[name] for name in ("abstract", "path", "child")},
            {"abstract": "connected", "path": "connected", "child": "connected"},
        )

    def test_what_agents_need_still_works(self):
        found = self.found(PROGRAM)
        self.assertEqual((found["tcp"], found["pair"]), ("connected", "connected"))

    def test_the_launcher_starts_nothing_when_it_cannot_install_the_filter(self):
        marker = self.work / "started"
        command = [sys.executable, "-I", "-c", f"open({str(marker)!r}, 'w')"]
        # A program the kernel refuses: one instruction it does not know.
        finished = self.run_probe(self.sandbox(b"\xff" * 8), command)
        self.assertEqual(finished.returncode, SCOPE_FAILED)
        self.assertIn("cannot keep the agent from Unix sockets", finished.stderr)
        self.assertFalse(marker.exists())

    def test_the_default_sandbox_carries_the_filter(self):
        self.assertEqual(default_sandbox().unix_socket_filter, PROGRAM)


if __name__ == "__main__":
    unittest.main()
