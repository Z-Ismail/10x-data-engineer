"""Tests for hiding the system's sockets from agents, who keep the network.

A socket file can be reached through a read-only mount, so an agent could
talk to any service listening in /run: Docker's socket alone gives control
of the computer to whoever may use it. Agents keep the network, so the
sockets are covered one by one. The command line is checked against paths
worked out by hand, in a folder that stands for /run; then, in the real
sandbox, a process tries to reach the sockets.

The sandbox tests are skipped where bubblewrap cannot run, unless
TENX_REQUIRE_SANDBOX is set, as it is in CI; then they fail instead.
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from test_gate_sandbox import needs_sandbox

from tenx.sandbox import KEPT_SOCKETS, Bubblewrap, Mounts, default_sandbox, python_folders


def options(arguments: list[str]) -> list[str]:
    return arguments[1 : arguments.index("--")]


def listen(path: Path) -> socket.socket:
    """A socket file at ``path``, closed when the test ends."""
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(path))
    server.listen()
    return server


class SocketMasksTest(unittest.TestCase):
    """Which mounts cover the sockets, from a folder standing for /run."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="s")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.run_folder = self.root / "run"
        self.home = self.root / "home"
        self.work = self.root / "work"
        for folder in (self.run_folder, self.home, self.work):
            folder.mkdir()

    def socket(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.addCleanup(listen(path).close)
        return path

    def sandbox(self, **changes) -> Bubblewrap:
        values = {"home": self.home, "scratch": (), "sockets": (self.run_folder,)}
        values.update(changes)
        return Bubblewrap("bwrap", **values)

    def masks(self, sandbox=None, **mounts) -> list[str]:
        sandbox = sandbox or self.sandbox()
        arguments = options(sandbox.command(["claude"], Mounts(workspace=self.work, **mounts)))
        return arguments[arguments.index("/proc") + 1 :]

    def test_a_socket_in_its_own_folder_empties_the_folder(self):
        run = self.run_folder
        self.socket(run / "docker" / "docker.sock")
        self.socket(run / "dbus" / "system_bus_socket")
        (run / "lock").mkdir()
        (run / "utmp").write_text("not a socket")
        self.assertEqual(
            self.masks(),
            [
                "--tmpfs", str(self.home),
                "--tmpfs", str(run / "dbus"),
                "--tmpfs", str(run / "docker"),
                "--bind", str(self.work), str(self.work),
                "--unshare-pid",
                "--die-with-parent",
                "--chdir", str(self.work),
            ],
        )  # fmt: skip

    def test_a_socket_straight_in_the_folder_is_covered_by_an_empty_file(self):
        docker = self.socket(self.run_folder / "docker.sock")
        self.assertEqual(self.masks()[2:5], ["--ro-bind", os.devnull, str(docker)])

    def test_only_the_outermost_folder_holding_sockets_is_emptied(self):
        run = self.run_folder
        self.socket(run / "podman" / "podman.sock")
        self.socket(run / "podman" / "inner" / "other.sock")
        self.socket(run / "containerd" / "s" / "deep.sock")
        self.assertEqual(
            self.masks()[2:6],
            ["--tmpfs", str(run / "podman"), "--tmpfs", str(run / "containerd" / "s")],
        )

    def test_sockets_for_name_lookups_stay(self):
        run = self.run_folder
        self.socket(run / "systemd" / "resolve" / "io.systemd.Resolve")
        self.socket(run / "nscd" / "socket")
        sandbox = self.sandbox(kept_sockets=(run / "systemd" / "resolve", run / "nscd"))
        self.assertEqual(self.masks(sandbox)[2], "--bind")

    def test_a_folder_holding_a_kept_folder_is_not_emptied(self):
        run = self.run_folder
        self.socket(run / "systemd" / "resolve" / "io.systemd.Resolve")
        notify = self.socket(run / "systemd" / "notify")
        journal = self.socket(run / "systemd" / "journal" / "socket")
        sandbox = self.sandbox(kept_sockets=(run / "systemd" / "resolve",))
        self.assertEqual(
            self.masks(sandbox)[2:7],
            ["--tmpfs", str(journal.parent), "--ro-bind", os.devnull, str(notify)],
        )

    def test_a_link_to_a_socket_covers_the_socket_it_leads_to(self):
        elsewhere = self.socket(self.root / "elsewhere" / "service.sock")
        (self.run_folder / "service.sock").symlink_to(elsewhere)
        (self.run_folder / "linked").symlink_to(elsewhere.parent, target_is_directory=True)
        self.assertEqual(self.masks()[2:6], ["--ro-bind", os.devnull, str(elsewhere), "--bind"])

    def test_a_socket_already_in_an_emptied_folder_needs_nothing_more(self):
        self.socket(self.run_folder / "user" / "1000" / "bus")
        (self.run_folder / "agent.sock").symlink_to(self.socket(self.home / "agent.sock"))
        sandbox = self.sandbox(scratch=(self.run_folder / "user",))
        self.assertEqual(
            self.masks(sandbox)[:5],
            ["--tmpfs", str(self.home), "--tmpfs", str(self.run_folder / "user"), "--bind"],
        )

    def test_a_folder_that_cannot_be_opened_is_passed_over(self):
        self.socket(self.run_folder / "locked" / "a.sock")
        seen = self.socket(self.run_folder / "open" / "b.sock")
        real = os.scandir

        def scandir(path):
            if Path(path).name == "locked":
                raise PermissionError(13, "Permission denied", str(path))
            return real(path)

        with mock.patch("tenx.sandbox.os.scandir", scandir):
            self.assertEqual(self.masks()[2:5], ["--tmpfs", str(seen.parent), "--bind"])

    def test_a_sockets_folder_that_cannot_be_opened_covers_nothing(self):
        self.socket(self.run_folder / "docker.sock")
        denied = PermissionError(13, "Permission denied")
        with mock.patch("tenx.sandbox.os.scandir", side_effect=denied):
            self.assertEqual(self.masks()[2], "--bind")

    def test_a_missing_sockets_folder_covers_nothing(self):
        sandbox = self.sandbox(sockets=(self.root / "absent",))
        self.assertEqual(self.masks(sandbox)[2], "--bind")

    def test_offline_the_whole_folder_is_emptied_instead(self):
        self.socket(self.run_folder / "docker.sock")
        self.socket(self.run_folder / "dbus" / "system_bus_socket")
        with mock.patch("tenx.sandbox.SOCKETS_FOLDER", self.run_folder):
            found = self.masks(network=False)
        self.assertEqual(
            found[:5], ["--tmpfs", str(self.home), "--tmpfs", str(self.run_folder), "--bind"]
        )
        self.assertEqual(found.count("--tmpfs"), 2)
        self.assertNotIn(os.devnull, found)


# From inside the sandbox: says it is ready, waits for the word to go, then
# tries a TCP listener on the host's loopback and each Unix socket named.
PROBE = """
import json, os, socket, sys, time
work, port, paths = sys.argv[1], int(sys.argv[2]), sys.argv[3:]
open(os.path.join(work, "ready"), "w").close()
while not os.path.exists(os.path.join(work, "go")):
    time.sleep(0.05)
found = {}
for name, family, address in [("tcp", socket.AF_INET, ("127.0.0.1", port))] + [
    (path, socket.AF_UNIX, path) for path in paths
]:
    try:
        with socket.socket(family) as client:
            client.settimeout(5)
            client.connect(address)
            client.sendall(b"hello")
        found[name] = "connected"
    except OSError as error:
        found[name] = "blocked: " + type(error).__name__
print(json.dumps(found))
"""

# Every socket under the folders named, following links, one path a line.
FIND_SOCKETS = """
import os, stat, sys
for top in sys.argv[1:]:
    for folder, _, names in os.walk(top, followlinks=True):
        for name in names:
            path = os.path.join(folder, name)
            try:
                if stat.S_ISSOCK(os.stat(path).st_mode):
                    print(path)
            except OSError:
                pass
"""


@needs_sandbox
class SocketsInTheSandboxTest(unittest.TestCase):
    """In the real sandbox, with the network kept, as an agent has it.

    The test's files are kept in the home folder and the sandbox is told
    another home, so they are visible unless the sandbox hides them; the
    folder standing for /run is given to the sandbox as its sockets folder.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=Path.home(), prefix="s")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.run_folder = self.root / "run"
        self.work = self.root / "work"
        self.work.mkdir()
        self.received: dict[str, list[bytes]] = {}

    def serve(self, server: socket.socket, name: str) -> None:
        self.received[name] = []

        def accept():
            while True:
                try:
                    connection, _ = server.accept()
                except OSError:
                    return
                with connection:
                    self.received[name].append(connection.recv(16))

        thread = threading.Thread(target=accept, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 10)
        self.addCleanup(server.close)
        self.addCleanup(server.shutdown, socket.SHUT_RDWR)

    def unix(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.serve(listen(path), str(path))
        return path

    def start(self, paths: list[Path], **changes) -> subprocess.Popen:
        tcp = socket.create_server(("127.0.0.1", 0))
        self.serve(tcp, "tcp")
        values = {"home": self.root / "home", "sockets": (self.run_folder,)}
        values.update(changes)
        sandbox = Bubblewrap(default_sandbox().executable, **values)
        python = os.path.normpath(os.path.abspath(sys.executable))
        port = str(tcp.getsockname()[1])
        command = [python, "-I", "-c", PROBE, str(self.work), port, *map(str, paths)]
        seen = Mounts(workspace=self.work, read_only=python_folders())
        return subprocess.Popen(
            sandbox.command(command, seen), stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )

    def finish(self, process: subprocess.Popen) -> dict:
        (self.work / "go").touch()
        stdout, stderr = process.communicate(timeout=60)
        self.assertEqual(process.returncode, 0, stderr)
        found = json.loads(stdout)
        # Give the listeners time to take what was sent.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and any(
            found.get(name) == "connected" and not data for name, data in self.received.items()
        ):
            time.sleep(0.05)
        return found

    def test_the_network_stays_and_the_system_sockets_are_out_of_reach(self):
        own = self.unix(self.run_folder / "docker" / "docker.sock")
        loose = self.unix(self.run_folder / "docker.sock")
        found = self.finish(self.start([own, loose]))
        self.assertEqual(found["tcp"], "connected")
        self.assertTrue(found[str(own)].startswith("blocked"), found)
        self.assertTrue(found[str(loose)].startswith("blocked"), found)
        self.assertEqual(self.received[str(own)] + self.received[str(loose)], [])
        self.assertEqual(self.received["tcp"], [b"hello"])

    def test_sockets_for_name_lookups_and_unlisted_folders_are_reached(self):
        kept = self.unix(self.run_folder / "nscd" / "socket")
        elsewhere = self.unix(self.root / "elsewhere" / "s.sock")
        process = self.start([kept, elsewhere], kept_sockets=(kept.parent,))
        found = self.finish(process)
        self.assertEqual(
            found, {"tcp": "connected", str(kept): "connected", str(elsewhere): "connected"}
        )

    def test_a_socket_made_again_while_the_agent_runs_stays_out_of_reach(self):
        own = self.unix(self.run_folder / "docker" / "docker.sock")
        process = self.start([own])
        deadline = time.monotonic() + 30
        while not (self.work / "ready").exists():
            self.assertLess(time.monotonic(), deadline, "the probe never started")
            time.sleep(0.05)
        # The service restarts: its old socket goes and a new one is made.
        own.unlink()
        self.unix(own)
        found = self.finish(process)
        self.assertTrue(found[str(own)].startswith("blocked"), found)
        self.assertEqual(self.received[str(own)], [])

    def test_no_socket_of_this_computer_is_left_but_those_for_name_lookups(self):
        # find trusts the folder's entry types, which still say socket for
        # one covered by an empty file, so each path is looked up instead.
        python = os.path.normpath(os.path.abspath(sys.executable))
        find = [python, "-I", "-c", FIND_SOCKETS, "/run", "/var/run"]
        kept = [Path(folder).resolve() for folder in KEPT_SOCKETS]

        def stray(command: list[str]) -> list[Path]:
            finished = subprocess.run(
                command, capture_output=True, text=True, timeout=60, check=False
            )
            # /var/run is usually a link to /run, so a kept socket shows twice.
            found = [Path(line).resolve() for line in finished.stdout.splitlines()]
            return [path for path in found if not any(f == path or f in path.parents for f in kept)]

        # Without the sandbox the same search finds what is to be hidden,
        # where this computer has any such socket the user may reach.
        outside = [path for path in stray(find) if not path.is_relative_to("/run/user")]
        seen = Mounts(workspace=self.work, read_only=python_folders())
        sandboxed = default_sandbox().command(find, seen)
        self.assertEqual(stray(sandboxed), [], f"outside the sandbox: {outside}")


if __name__ == "__main__":
    unittest.main()
