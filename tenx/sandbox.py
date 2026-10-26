"""Run an agent where it can see only the files its work needs.

An agent's tools reach further than their names suggest: ``dbt`` reaches
DuckDB, which reads any file the process can, and a dbt Python model runs
any Python. So limits on which tools an agent may call do not keep files
from it. A sandbox does: the operating system decides what the agent's
process, and every process it starts, can see.

On Linux the sandbox is bubblewrap. Inside it the computer's files are
read-only; the home folder, the temporary folders and every path the caller
hides are replaced by empty folders that vanish when the sandbox ends; then
only the paths the work needs are put back. The agent's processes get their
own process ids and stop when the runner stops.

Where no sandbox can be set up, the run is refused, unless the caller asks
for an unisolated run on purpose, and that run is then marked as such.
"""

from __future__ import annotations

import os
import site
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from shutil import which
from typing import Protocol

BWRAP_INSTALL = (
    "sudo apt install bubblewrap (Debian, Ubuntu) or sudo dnf install bubblewrap (Fedora)"
)
# Folders every sandbox hides, as well as the home folder. /run/user holds
# the user's own sockets, such as an ssh agent's.
SCRATCH_FOLDERS = (Path("/tmp"), Path("/var/tmp"), Path("/run/user"))
CHECK_SECONDS = 30


class SandboxError(Exception):
    """No sandbox can be set up here, or the paths asked for cannot be kept apart."""


@dataclass(frozen=True)
class Mounts:
    """What an agent may see, and what it must never see.

    ``workspace`` is the agent's working folder, and the only place it can
    write to and keep. ``writable`` paths can also be written, for a tool's
    own state. ``read_only`` paths are visible and cannot be changed.
    ``hidden`` paths are never visible, wherever they are.
    """

    workspace: Path
    read_only: tuple[Path, ...] = ()
    writable: tuple[Path, ...] = ()
    hidden: tuple[Path, ...] = ()


class Sandbox(Protocol):
    """Starts a command so that it sees only what its mounts allow."""

    name: str
    isolated: bool

    def command(self, command: list[str], mounts: Mounts) -> list[str]:
        """The command line that runs ``command`` inside the sandbox."""
        ...

    def describe(self) -> str:
        """One line for the run's report."""
        ...


@dataclass(frozen=True)
class Unisolated:
    """No sandbox: the command sees every file the user can. Only on request."""

    name: str = "none"
    isolated: bool = False

    def command(self, command: list[str], mounts: Mounts) -> list[str]:
        return list(command)

    def describe(self) -> str:
        return "none (--unisolated): the agents could read and change any file you can"


def python_folders() -> tuple[Path, ...]:
    """The folders holding the running Python and the packages it imports.

    The sandbox hides the home folder, where a virtual environment or a
    user's own packages are often installed, so a command run with this
    Python inside the sandbox needs these shown again, read-only.
    """
    folders = [Path(sys.prefix), Path(sys.base_prefix), Path(sys.executable).parent]
    if site.ENABLE_USER_SITE:
        folders.append(Path(site.getusersitepackages()))
    unique: list[Path] = []
    for folder in folders:
        folder = folder.resolve()
        if folder.is_dir() and folder not in unique:
            unique.append(folder)
    return tuple(unique)


def _inside(path: Path, folder: Path) -> bool:
    return path == folder or folder in path.parents


def _order(path: Path) -> tuple[int, str]:
    # Shallowest first, so that a deeper mount lands on top of its parent.
    return len(path.parts), str(path)


@dataclass(frozen=True)
class Bubblewrap:
    """A bubblewrap sandbox (Linux)."""

    executable: str = "bwrap"
    home: Path = field(default_factory=Path.home)
    scratch: tuple[Path, ...] = SCRATCH_FOLDERS
    name: str = "bubblewrap"
    isolated: bool = True

    def command(self, command: list[str], mounts: Mounts) -> list[str]:
        workspace = Path(mounts.workspace).resolve()
        hidden = [Path(path).resolve() for path in mounts.hidden]
        # Hidden folders are replaced by empty ones; files are dealt with last.
        scratch = [Path(self.home).resolve(), *self.scratch]
        masks = scratch + [path for path in hidden if path.is_dir()]
        masks = sorted({path for path in masks if path.is_dir()}, key=_order)
        binds = [(workspace, "--bind")]
        binds += [(Path(path).resolve(), "--bind") for path in mounts.writable]
        binds += [(Path(path).resolve(), "--ro-bind") for path in mounts.read_only]
        for path, _ in binds:
            if not path.exists():
                raise SandboxError(f"{path} does not exist, so it cannot be shown to the agent")
            for masked in scratch:
                if _inside(masked, path):
                    raise SandboxError(
                        f"{path} holds {masked}, which the agent must not see; "
                        "use a folder that does not contain it"
                    )
        # Only paths under a mask need putting back, and the workspace, since
        # everything else is read-only.
        binds = [
            (path, flag)
            for path, flag in binds
            if path == workspace or any(_inside(path, masked) for masked in masks)
        ]
        binds.sort(key=lambda bind: _order(bind[0]))
        arguments = [self.executable, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]
        for path in masks:
            arguments += ["--tmpfs", str(path)]
        for path, flag in binds:
            arguments += [flag, str(path), str(path)]
        # Folders put back are mounted after the empty ones, so a hidden path
        # inside one is visible again, as is a hidden file under no empty
        # folder at all. Either is covered last.
        for path in hidden:
            put_back = any(_inside(path, bound) for bound, _ in binds)
            emptied = any(_inside(path, masked) for masked in masks)
            if emptied and not put_back:
                continue
            if any(_inside(bound, path) for bound, _ in binds):
                raise SandboxError(f"{path} cannot be hidden: a folder shown to the agent is in it")
            if path.is_dir():
                arguments += ["--tmpfs", str(path)]
            else:
                arguments += ["--ro-bind", os.devnull, str(path)]
        arguments += ["--unshare-pid", "--die-with-parent", "--chdir", str(workspace), "--"]
        return arguments + list(command)

    def describe(self) -> str:
        return (
            "bubblewrap: the agents and the gate's builds see only their own folder "
            "and the files their work needs"
        )


def check(sandbox: Bubblewrap) -> None:
    """Start an empty command in the sandbox, so a broken one fails before any agent runs."""
    with tempfile.TemporaryDirectory() as folder:
        probe = sandbox.command(["true"], Mounts(workspace=Path(folder)))
        try:
            finished = subprocess.run(
                probe,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=CHECK_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise SandboxError(f"bubblewrap could not be started: {error}") from error
    if finished.returncode != 0:
        detail = finished.stdout.strip().splitlines()
        reason = detail[-1] if detail else f"exit {finished.returncode}"
        raise SandboxError(
            f"bubblewrap is installed but cannot set up a sandbox here ({reason}). "
            "On Ubuntu 24.04 and later, AppArmor blocks it unless bubblewrap has a "
            "profile that allows user namespaces"
        )


def default_sandbox(unisolated: bool = False, platform: str | None = None) -> Sandbox:
    """The sandbox for agent runs: bubblewrap on Linux, or a refusal.

    ``unisolated`` gives an unisolated run instead, on purpose.
    """
    if unisolated:
        return Unisolated()
    platform = sys.platform if platform is None else platform
    if not platform.startswith("linux"):
        raise SandboxError(
            f"no tested sandbox on {platform} yet, so agents are not run here; "
            "pass --unisolated to run them without one, on data they may read in full"
        )
    executable = which("bwrap")
    if executable is None:
        raise SandboxError(f"bubblewrap (bwrap) is needed to keep agents apart; {BWRAP_INSTALL}")
    sandbox = Bubblewrap(executable)
    check(sandbox)
    return sandbox
