"""A system call filter that keeps a process from opening Unix sockets.

A Unix socket reaches whatever listens on it: Docker's socket gives control
of the computer, a session bus runs programs, an ssh agent signs for the
user. Covering socket files one by one misses those made after the sandbox
starts and those outside the folders it covers, and a socket bound to an
abstract name has no file at all. Every one of them needs a new socket from
``socket(AF_UNIX, ...)``, so a seccomp filter that refuses that call keeps
the process, and everything it starts, from all of them at once.

The filter allows ``socketpair`` for stream sockets, which programs use to
talk to their own children and which cannot reach anything else. It refuses
io_uring, which can open sockets without the ``socket`` call, and any call
made through another architecture's entry (a 64-bit x86 program can make
32-bit calls), where the numbers differ.

The program is classic BPF, as the kernel takes it; ``install_source`` is
Python that installs a program in the process that runs it.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

# Classic BPF instructions used here: load a 32-bit word of the call's data,
# compare, mask and return.
LOAD = 0x20  # BPF_LD | BPF_W | BPF_ABS
EQUAL = 0x15  # BPF_JMP | BPF_JEQ | BPF_K
AT_LEAST = 0x35  # BPF_JMP | BPF_JGE | BPF_K
MASK = 0x54  # BPF_ALU | BPF_AND | BPF_K
RETURN = 0x06  # BPF_RET | BPF_K

# What the filter returns: run the call, or fail it with an error number.
ALLOW = 0x7FFF0000  # SECCOMP_RET_ALLOW
ERROR = 0x00050000  # SECCOMP_RET_ERRNO, error number in the low 16 bits
EACCES = 13
ENOSYS = 38

# Offsets in struct seccomp_data: the call's number, its architecture and
# the low 32 bits of each argument (little-endian, as both machines are).
NUMBER, ARCH = 0, 4
ARGUMENTS = 16

AF_UNIX = 1
SOCK_STREAM = 1
# The socket type shares its argument with SOCK_CLOEXEC and SOCK_NONBLOCK.
SOCKET_TYPE = 0xF
# io_uring_setup, io_uring_enter and io_uring_register: the same on every
# architecture, as are all calls numbered from 403 on.
IO_URING = (425, 428)
# On x86_64, calls with this bit use the x32 numbering, under the same arch.
X32_BIT = 0x40000000


@dataclass(frozen=True)
class Machine:
    """What the filter needs to know about one architecture."""

    arch: int  # AUDIT_ARCH_*
    socket: int
    socketpair: int
    x32: bool = False


MACHINES = {
    "x86_64": Machine(arch=0xC000003E, socket=41, socketpair=53, x32=True),
    "aarch64": Machine(arch=0xC00000B7, socket=198, socketpair=199),
}
# What platform.machine() calls the same architectures elsewhere.
MACHINES["amd64"] = MACHINES["x86_64"]
MACHINES["arm64"] = MACHINES["aarch64"]

# An instruction is (code, k) or (code, k, jump if true, jump if false), the
# jumps naming labels; a string is a label for the next instruction.
Line = str | tuple


def filter_lines(machine: Machine) -> list[Line]:
    """The filter for one architecture, before assembly."""
    lines: list[Line] = [
        (LOAD, ARCH),
        (EQUAL, machine.arch, None, "no_such_call"),
        (LOAD, NUMBER),
    ]
    if machine.x32:
        lines.append((AT_LEAST, X32_BIT, "no_such_call", None))
    lines += [
        (EQUAL, machine.socket, "socket", None),
        (EQUAL, machine.socketpair, "socketpair", None),
        (AT_LEAST, IO_URING[0], None, "allow"),
        (AT_LEAST, IO_URING[1], "allow", "no_such_call"),
        "socket",
        (LOAD, ARGUMENTS),
        (EQUAL, AF_UNIX, "refuse", "allow"),
        "socketpair",
        (LOAD, ARGUMENTS),
        (EQUAL, AF_UNIX, None, "allow"),
        (LOAD, ARGUMENTS + 8),
        (MASK, SOCKET_TYPE),
        (EQUAL, SOCK_STREAM, "allow", "refuse"),
        "allow",
        (RETURN, ALLOW),
        "no_such_call",
        (RETURN, ERROR | ENOSYS),
        "refuse",
        (RETURN, ERROR | EACCES),
    ]
    return lines


def assemble(lines: list[Line]) -> bytes:
    """Classic BPF for the kernel: struct sock_filter, eight bytes each."""
    labels: dict[str, int] = {}
    count = 0
    for line in lines:
        if isinstance(line, str):
            if line in labels:
                raise ValueError(f"label {line} is used twice")
            labels[line] = count
        else:
            count += 1
    program, index = [], 0
    for line in lines:
        if isinstance(line, str):
            continue
        code, k, *jumps = line
        offsets = [0, 0]
        for n, label in enumerate(jumps):
            if label is None:
                continue
            offset = labels[label] - index - 1
            if not 0 <= offset <= 255:
                raise ValueError(f"cannot jump to {label} from instruction {index}")
            offsets[n] = offset
        program.append(struct.pack("<HBBI", code, offsets[0], offsets[1], k))
        index += 1
    return b"".join(program)


def unix_socket_filter(machine: str) -> bytes | None:
    """The filter for a machine as platform.machine() names it, or None if there is none."""
    known = MACHINES.get(machine.lower())
    return None if known is None else assemble(filter_lines(known))


# Installs PROGRAM (bytes) in the running process, or raises OSError. The
# process must not gain privileges afterwards, which the kernel requires of
# an unprivileged filter and which setuid programs then respect.
install_source = """\
import ctypes, os

SET_NO_NEW_PRIVS, SET_SECCOMP, MODE_FILTER = 38, 22, 2


class Program(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]


libc = ctypes.CDLL(None, use_errno=True)
code = ctypes.create_string_buffer(PROGRAM, len(PROGRAM))
program = Program(len(PROGRAM) // 8, ctypes.cast(code, ctypes.c_void_p))
if (
    libc.prctl(SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0
    or libc.prctl(SET_SECCOMP, MODE_FILTER, ctypes.byref(program), 0, 0) != 0
):
    raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))
"""
