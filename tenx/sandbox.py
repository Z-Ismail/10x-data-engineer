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

import ctypes
import os
import site
import stat
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from platform import machine
from shutil import which
from textwrap import indent
from typing import Protocol

from tenx import seccomp

BWRAP_INSTALL = (
    "sudo apt install bubblewrap (Debian, Ubuntu) or sudo dnf install bubblewrap (Fedora)"
)
# Folders every sandbox hides, as well as the home folder. /run/user holds
# the user's own sockets, such as an ssh agent's.
SCRATCH_FOLDERS = (Path("/tmp"), Path("/var/tmp"), Path("/run/user"))
# Where the system keeps its sockets, such as Docker's and the D-Bus system
# bus. A socket file stays reachable through a read-only mount and from
# another network namespace, so a process cut off from the network is not
# shown this folder either. Agents keep it: name lookups can depend on it.
SOCKETS_FOLDER = Path("/run")
# Agents keep the network but not the system's sockets: every socket found in
# these folders when the sandbox starts is covered. Docker's socket alone
# gives control of the computer to whoever may use it.
SOCKET_FOLDERS = (SOCKETS_FOLDER, Path("/var/run"))
# Except the ones that only answer name lookups, which can need them.
KEPT_SOCKETS = (Path("/run/systemd/resolve"), Path("/run/nscd"))
CHECK_SECONDS = 30
# A socket bound to an abstract name has no file to cover: it belongs to the
# network, which agents share with the computer. Landlock (Linux 6.12 and
# later, its ABI 6) can keep a process and its children from connecting to
# any abstract socket made outside them.
LANDLOCK_SCOPE_ABI = 6
# Exit code of the launcher when it cannot keep the command from those sockets.
SCOPE_FAILED = 126
# Run inside the sandbox before the agent: keep this process from sockets,
# then become the agent, which inherits each limit with everything it
# starts. Any failed step stops here, so the agent never runs without a
# limit it was meant to have.
LAUNCHER_START = (
    f"SCOPE_FAILED = {SCOPE_FAILED}\n"
    + """\
import ctypes, os, sys


def fail(what, error):
    sys.stderr.write(f"cannot keep the agent from {what}: {error}\\n")
    sys.exit(SCOPE_FAILED)
"""
)
# Landlock's scope: no connecting to abstract sockets made outside.
LANDLOCK_STEP = """
CREATE_RULESET, RESTRICT_SELF = 444, 446
SET_NO_NEW_PRIVS = 38
SCOPE_ABSTRACT_UNIX_SOCKET = 1


class Ruleset(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in ("fs", "net", "scoped")]


libc = ctypes.CDLL(None, use_errno=True)
ruleset = Ruleset(0, 0, SCOPE_ABSTRACT_UNIX_SOCKET)
size = ctypes.c_size_t(ctypes.sizeof(ruleset))
fd = libc.syscall(CREATE_RULESET, ctypes.byref(ruleset), size, ctypes.c_uint32(0))
if (
    fd < 0
    or libc.prctl(SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0
    or libc.syscall(RESTRICT_SELF, fd, ctypes.c_uint32(0)) != 0
):
    fail("abstract sockets", os.strerror(ctypes.get_errno()))
os.close(fd)
"""
# The seccomp filter: no new Unix socket at all (tenx.seccomp).
FILTER_STEP = (
    "\ntry:\n"
    + indent(seccomp.install_source, "    ")
    + """\
except OSError as error:
    fail("Unix sockets", error.strerror)
"""
)
LAUNCHER_END = "\nos.execvp(sys.argv[1], sys.argv[1:])\n"


def launcher(scope: bool, program: bytes | None) -> str:
    """The launcher's Python: Landlock's scope if ``scope``, the filter ``program`` if given."""
    source = LAUNCHER_START
    if scope:
        source += LANDLOCK_STEP
    if program:
        source += f"\nPROGRAM = bytes.fromhex({program.hex()!r})\n" + FILTER_STEP
    return source + LAUNCHER_END


# What a sandboxed process is given of the caller's environment. Anything
# else stays outside: a token, a cloud key or a database address in the
# environment would otherwise be readable by the agent and by any model it
# writes (dbt's env_var, a Python model, a shell command).
#
# Every sandboxed process: where programs are, who and where it is, and how
# to print text. The temporary-folder variables are left out, so programs use
# the sandbox's own empty /tmp.
PROCESS_NAMES = frozenset({"PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "TZ"})
PROCESS_PREFIXES = ("LC_",)
# An agent as well: what Claude Code needs to sign in and reach its service,
# through a proxy and with extra certificates if the user has them.
AGENT_NAMES = frozenset(
    {
        "HTTPS_PROXY",
        "https_proxy",
        "HTTP_PROXY",
        "http_proxy",
        "NO_PROXY",
        "no_proxy",
        "NODE_EXTRA_CA_CERTS",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "DISABLE_TELEMETRY",
        "DISABLE_ERROR_REPORTING",
        "DISABLE_AUTOUPDATER",
        "DO_NOT_TRACK",
        "API_TIMEOUT_MS",
    }
)
AGENT_PREFIXES = ("ANTHROPIC_", "CLAUDE_")
# Names a user asks for on purpose, separated by commas.
PASS_VARIABLE = "TENX_SANDBOX_ENV"


class SandboxError(Exception):
    """No sandbox can be set up here, or the paths asked for cannot be kept apart."""


@dataclass(frozen=True)
class Mounts:
    """What an agent may see, and what it must never see.

    ``workspace`` is the agent's working folder, and the only place it can
    write to and keep. ``writable`` paths can also be written, for a tool's
    own state. ``read_only`` paths are visible and cannot be changed.
    ``hidden`` paths are never visible, wherever they are. ``files`` are
    single files shown read-write at another path, as (source, target)
    pairs; the target must be inside the workspace or a writable folder.
    With ``network`` off, the command has no network but its own loopback
    and none of the system's sockets folder. With it on, the system's
    sockets are covered one by one, except those for name lookups.
    """

    workspace: Path
    read_only: tuple[Path, ...] = ()
    writable: tuple[Path, ...] = ()
    hidden: tuple[Path, ...] = ()
    files: tuple[tuple[Path, Path], ...] = ()
    network: bool = True


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


def environment(
    sandbox: Sandbox, source: Mapping[str, str] | None = None, agent: bool = False
) -> dict[str, str]:
    """The environment a process started in ``sandbox`` is given.

    Without isolation, the whole of ``source`` (the caller's environment by
    default), as the user chose. In a sandbox, only the names every process
    needs, those an agent needs when ``agent`` is set, and any the user
    names in TENX_SANDBOX_ENV.
    """
    source = os.environ if source is None else source
    # Anything not marked unisolated is treated as a sandbox.
    if not getattr(sandbox, "isolated", True):
        return dict(source)
    names, prefixes = set(PROCESS_NAMES), PROCESS_PREFIXES
    if agent:
        names |= AGENT_NAMES
        prefixes += AGENT_PREFIXES
    names |= {name.strip() for name in source.get(PASS_VARIABLE, "").split(",") if name.strip()}
    return {
        name: value for name, value in source.items() if name in names or name.startswith(prefixes)
    }


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


def landlock_abi() -> int:
    """The Landlock ABI version this kernel offers; 0 where it has none or it is off."""
    if not sys.platform.startswith("linux"):
        return 0
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        # landlock_create_ruleset(NULL, 0, LANDLOCK_CREATE_RULESET_VERSION)
        version = libc.syscall(444, None, ctypes.c_size_t(0), ctypes.c_uint32(1))
    except (OSError, AttributeError):
        return 0
    return max(int(version), 0)


def _inside(path: Path, folder: Path) -> bool:
    return path == folder or folder in path.parents


def _order(path: Path) -> tuple[int, str]:
    # Shallowest first, so that a deeper mount lands on top of its parent.
    return len(path.parts), str(path)


def _sockets(folder: Path):
    """Every socket in a folder and the folders in it, links to sockets included."""
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return
    for entry in entries:
        try:
            if entry.is_dir(follow_symlinks=False):
                yield from _sockets(Path(entry.path))
            elif stat.S_ISSOCK(os.stat(entry.path).st_mode):
                yield Path(entry.path)
        except OSError:
            continue


@dataclass(frozen=True)
class Bubblewrap:
    """A bubblewrap sandbox (Linux)."""

    executable: str = "bwrap"
    home: Path = field(default_factory=Path.home)
    scratch: tuple[Path, ...] = SCRATCH_FOLDERS
    sockets: tuple[Path, ...] = SOCKET_FOLDERS
    kept_sockets: tuple[Path, ...] = KEPT_SOCKETS
    # Keep commands that keep the network from abstract sockets made outside
    # them, through the launcher. Needs Landlock ABI 6; default_sandbox
    # turns it on where the kernel has it.
    scope_abstract_sockets: bool = False
    # Keep commands that keep the network from opening any Unix socket, with
    # this seccomp filter (tenx.seccomp), through the same launcher. Covers
    # socket files the sandbox does not hide and abstract sockets on every
    # kernel; default_sandbox sets it where the machine has a filter.
    unix_socket_filter: bytes | None = None
    name: str = "bubblewrap"
    isolated: bool = True

    @property
    def launched(self) -> bool:
        """Whether commands that keep the network start through the launcher."""
        return self.scope_abstract_sockets or bool(self.unix_socket_filter)

    def command(self, command: list[str], mounts: Mounts) -> list[str]:
        if mounts.network and self.launched:
            # The launcher runs with the runner's own Python, shown read-only.
            source = launcher(self.scope_abstract_sockets, self.unix_socket_filter)
            command = [sys.executable, "-I", "-c", source, *command]
            shown = tuple(p for p in python_folders() if p not in mounts.read_only)
            mounts = replace(mounts, read_only=(*mounts.read_only, *shown))
        workspace = Path(mounts.workspace).resolve()
        hidden = [Path(path).resolve() for path in mounts.hidden]
        # Hidden folders are replaced by empty ones; files are dealt with last.
        scratch = [Path(self.home).resolve(), *self.scratch]
        if not mounts.network:
            scratch.append(SOCKETS_FOLDER)
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
        files = self._files(mounts.files, [path for path, flag in binds if flag == "--bind"])
        # Read-only paths need putting back only from under a mask, since
        # everything else is read-only already; writable ones always.
        binds = [
            (path, flag)
            for path, flag in binds
            if flag == "--bind" or any(_inside(path, masked) for masked in masks)
        ]
        binds.sort(key=lambda bind: _order(bind[0]))
        arguments = [self.executable, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]
        for path in masks:
            arguments += ["--tmpfs", str(path)]
        if mounts.network:
            for covering in self._socket_masks(masks):
                arguments += covering
        for path, flag in binds:
            arguments += [flag, str(path), str(path)]
        for source, target in files:
            arguments += ["--bind", str(source), str(target)]
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
        if not mounts.network:
            arguments.append("--unshare-net")
        arguments += ["--unshare-pid", "--die-with-parent", "--chdir", str(workspace), "--"]
        return arguments + list(command)

    def _socket_masks(self, masks: list[Path]) -> list[list[str]]:
        """What covers each system socket, for a sandbox that keeps the network.

        A socket in its own folder is covered by emptying that folder, so a
        service that makes its socket again while the agent runs is still
        hidden; one straight in a sockets folder, or kept company by a folder
        for name lookups, by an empty file. A link to a socket covers the
        socket it leads to. Folders the user cannot open are passed over: no
        one in the sandbox could reach a socket in them either.
        """
        roots = sorted({Path(path).resolve() for path in self.sockets if Path(path).is_dir()})
        kept = [Path(path).resolve() for path in self.kept_sockets]
        found = {path.resolve() for root in roots for path in _sockets(root)}
        folders, files = set(), set()
        for path in found:
            if any(_inside(path, folder) for folder in kept + masks):
                continue
            parent = path.parent
            own_folder = any(parent != root and _inside(parent, root) for root in roots)
            if own_folder and not any(_inside(folder, parent) for folder in kept):
                folders.add(parent)
            else:
                files.add(path)
        folders = {f for f in folders if not any(f != o and _inside(f, o) for o in folders)}
        files = {f for f in files if not any(_inside(f, folder) for folder in folders)}
        covered = [(path, ["--tmpfs", str(path)]) for path in folders]
        covered += [(path, ["--ro-bind", os.devnull, str(path)]) for path in files]
        return [arguments for _, arguments in sorted(covered, key=lambda item: _order(item[0]))]

    @staticmethod
    def _files(
        files: tuple[tuple[Path, Path], ...], writable: list[Path]
    ) -> list[tuple[Path, Path]]:
        checked = []
        for source, target in files:
            source = Path(source).resolve()
            target = Path(target).parent.resolve() / Path(target).name
            if not source.is_file():
                raise SandboxError(f"{source} is not a file, so it cannot be shown to the agent")
            if not any(_inside(target.parent, folder) for folder in writable):
                raise SandboxError(
                    f"{target} is outside every folder the agent may write to, "
                    "so a file cannot be shown there"
                )
            checked.append((source, target))
        return checked

    def describe(self) -> str:
        unix = (
            "kept from opening Unix sockets"
            if self.unix_socket_filter
            else "not kept from opening Unix sockets (no filter for this machine)"
        )
        abstract = (
            "from abstract sockets outside"
            if self.scope_abstract_sockets
            else "not from abstract sockets outside by Landlock (needs Linux 6.12 or later)"
        )
        return (
            "bubblewrap: the agents and the gate's builds see only their own folder "
            f"and the files their work needs; the agents are {unix}, and {abstract}"
        )


def check(sandbox: Bubblewrap) -> None:
    """Start an empty command in the sandbox, so a broken one fails before any agent runs.

    The command is cut off from the network, as the gate's builds are, so a
    sandbox that cannot do that fails here too. Where agents are kept from
    abstract sockets, a second command keeps the network, as agents do, and
    starts through the launcher, so a launcher that cannot run fails here.
    Where agents are kept from Unix sockets, so is that second command.
    """
    _probe(
        sandbox,
        network=False,
        hint="On Ubuntu 24.04 and later, AppArmor blocks it unless bubblewrap has a "
        "profile that allows user namespaces",
    )
    if sandbox.launched:
        _probe(
            sandbox,
            network=True,
            hint="The agents could not be kept from sockets with Landlock's scope or "
            "the seccomp filter, which this computer should allow",
        )


def _probe(sandbox: Bubblewrap, network: bool, hint: str) -> None:
    with tempfile.TemporaryDirectory() as folder:
        probe = sandbox.command(["true"], Mounts(workspace=Path(folder), network=network))
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
            f"bubblewrap is installed but cannot set up a sandbox here ({reason}). {hint}"
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
    sandbox = Bubblewrap(
        executable,
        scope_abstract_sockets=landlock_abi() >= LANDLOCK_SCOPE_ABI,
        unix_socket_filter=seccomp.unix_socket_filter(machine()),
    )
    check(sandbox)
    return sandbox
