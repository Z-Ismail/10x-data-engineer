"""The Unix socket filter for the gate's builds and the counting process, in the real sandbox.

Neither has the network or the system's sockets folder, but a socket file
in a folder shown to them can still be reached without the filter: here a
socket in the raw data folder, which both are shown. A dbt Python model in
the gate's build and a stand-in for the counting script each try it. The
same sandbox without the filter is the positive control.

Skipped where bubblewrap cannot run, unless TENX_REQUIRE_SANDBOX is set, as
it is in CI; then they fail instead.
"""

import json
import platform
import shutil
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import duckdb
from test_gate_sandbox import PROJECT, generate, needs_sandbox
from test_spec import CONTRACT, ORDERS, VISITS

from tenx import counting
from tenx.contract import ContractError
from tenx.counting import count_kept
from tenx.gate import DATABASE_FILE, build_project
from tenx.sandbox import Bubblewrap, default_sandbox
from tenx.seccomp import unix_socket_filter

PROGRAM = unix_socket_filter(platform.machine())

# A dbt Python model that tries the socket and says what its process is.
MODEL = """
import json, socket


def model(dbt, session):
    found = {{}}
    try:
        peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        peer.connect({path!r})
        peer.sendall(b"build")
        peer.close()
        found["socket"] = "connected"
    except OSError as error:
        found["socket"] = type(error).__name__
    with open("/proc/self/status") as status:
        found["seccomp"] = [line.split()[1] for line in status if line.startswith("Seccomp:")]
    text = json.dumps(found)
    return session.sql(f"select '{{text}}' as found")
"""

# Stands in for the counting script: tries the socket and reports what
# happened as a failed count.
COUNT_PROBE = """
import json, socket, sys
job = json.loads(sys.stdin.read())
try:
    peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    peer.connect(job["raw"] + "probe.sock")
    peer.sendall(b"count")
    found = "connected"
except OSError as error:
    found = type(error).__name__
print(json.dumps({"failed": "", "error": found}))
sys.exit(1)
"""


class SocketServer:
    """A Unix socket at ``path`` that keeps what each peer sends."""

    def __init__(self, path: Path) -> None:
        self.received: list[bytes] = []
        self.accepted = 0
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(path))
        self.server.listen()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                peer, _ = self.server.accept()
            except OSError:
                return
            self.accepted += 1
            with peer:
                self.received.append(peer.recv(64))

    def close(self) -> None:
        # A peer that was accepted may still be being read.
        deadline = time.monotonic() + 2
        while self.accepted > len(self.received) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.server.close()


def sandbox(program) -> Bubblewrap:
    return Bubblewrap(default_sandbox().executable, unix_socket_filter=program)


@needs_sandbox
@unittest.skipUnless(PROGRAM, f"no filter for {platform.machine()}")
class GateBuildTest(unittest.TestCase):
    def build(self, program) -> tuple[dict, list[bytes]]:
        with tempfile.TemporaryDirectory(prefix="g") as tmp:
            root = Path(tmp).resolve()
            raw = root / "data"
            generate(11, 20, raw, root / "answers")
            project = root / "project"
            shutil.copytree(PROJECT, project)
            path = raw / "probe.sock"
            model = MODEL.format(path=str(path))
            (project / "models" / "socket_probe.py").write_text(model, encoding="utf-8")
            server = SocketServer(path)
            try:
                code, _, printed = build_project(
                    project, raw, root / "work", sandbox=sandbox(program)
                )
            finally:
                server.close()
            self.assertEqual(code, 0, printed)
            con = duckdb.connect(str(root / "work" / DATABASE_FILE), read_only=True)
            try:
                found = json.loads(con.execute("select found from socket_probe").fetchone()[0])
            finally:
                con.close()
        return found, server.received

    def test_a_python_model_cannot_reach_a_socket_in_a_shown_folder(self):
        found, received = self.build(PROGRAM)
        self.assertEqual(found, {"socket": "PermissionError", "seccomp": ["2"]})
        self.assertEqual(received, [])

    def test_without_the_filter_it_can(self):
        found, received = self.build(None)
        self.assertEqual(found, {"socket": "connected", "seccomp": ["0"]})
        self.assertEqual(received, [b"build"])

    def test_the_default_sandbox_builds_with_the_filter(self):
        self.assertEqual(default_sandbox().unix_socket_filter, PROGRAM)


@needs_sandbox
@unittest.skipUnless(PROGRAM, f"no filter for {platform.machine()}")
class CountingTest(unittest.TestCase):
    def count(self, box) -> tuple[str, list[bytes]]:
        with tempfile.TemporaryDirectory(prefix="c") as tmp:
            root = Path(tmp).resolve()
            raw = root / "raw"
            (raw / "shop").mkdir(parents=True)
            (raw / "shop" / "orders.csv").write_text(ORDERS, encoding="utf-8")
            (raw / "shop" / "visits.csv").write_text(VISITS, encoding="utf-8")
            server = SocketServer(raw / "probe.sock")
            try:
                with (
                    mock.patch.object(counting, "SCRIPT", COUNT_PROBE),
                    self.assertRaises(ContractError) as caught,
                ):
                    count_kept(CONTRACT, raw, root / "count", box)
            finally:
                server.close()
        return str(caught.exception).rpartition(": ")[2], server.received

    def test_the_counting_process_cannot_reach_a_socket_in_the_raw_folder(self):
        self.assertEqual(self.count(sandbox(PROGRAM)), ("PermissionError", []))
        self.assertEqual(self.count(default_sandbox())[0], "PermissionError")

    def test_without_the_filter_it_can(self):
        self.assertEqual(self.count(sandbox(None)), ("connected", [b"count"]))


if __name__ == "__main__":
    unittest.main()
