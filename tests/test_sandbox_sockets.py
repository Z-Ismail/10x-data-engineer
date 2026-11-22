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

import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tenx.sandbox import Bubblewrap, Mounts


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


if __name__ == "__main__":
    unittest.main()
