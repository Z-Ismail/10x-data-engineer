"""Tests for the filter that keeps a process from opening Unix sockets.

A small interpreter, written here apart from the assembler, runs the
program on the calls that matter and compares what it returns with values
worked out by hand from the kernel's tables; copies broken on purpose must
fail that comparison. test_seccomp_installed runs it in a real process.
"""

import struct
import unittest

from tenx.seccomp import (
    MACHINES,
    assemble,
    filter_lines,
    unix_socket_filter,
)

# Values from the kernel's headers, written out again rather than imported.
X86_64, AARCH64, I386 = 0xC000003E, 0xC00000B7, 0x40000003
RUN = "allow"
REFUSED = "EACCES"
NO_SUCH_CALL = "ENOSYS"


def run(program: bytes, arch: int, number: int, *arguments: int) -> str:
    """What the kernel would do with a call, by running the program on it."""
    words = list(struct.iter_unpack("<HBBI", program))
    data = struct.pack("<iI", number, arch) + b"\0" * 8
    data += b"".join(struct.pack("<Q", value) for value in arguments)
    data = data.ljust(64, b"\0")
    accumulator, pc = 0, 0
    while True:
        code, if_true, if_false, k = words[pc]
        pc += 1
        if code == 0x20:
            (accumulator,) = struct.unpack_from("<I", data, k)
        elif code == 0x54:
            accumulator &= k
        elif code in (0x15, 0x35):
            taken = accumulator == k if code == 0x15 else accumulator >= k
            pc += if_true if taken else if_false
        elif code == 0x06:
            if k == 0x7FFF0000:
                return RUN
            if k >> 16 == 0x0005:
                return {13: REFUSED, 38: NO_SUCH_CALL}[k & 0xFFFF]
            return hex(k)
        else:
            raise AssertionError(f"unexpected instruction {code:#x}")


# (arch, call number, arguments) -> what must happen, per machine. Numbers:
# x86_64 socket 41, socketpair 53; aarch64 198 and 199; io_uring 425 to 427
# on both; read is 0 on x86_64 and 63 on aarch64.
CASES = {
    "x86_64": [
        ((X86_64, 41, 1, 1), REFUSED),  # socket(AF_UNIX, SOCK_STREAM)
        ((X86_64, 41, 1, 2 | 0x80000), REFUSED),  # datagram, close-on-exec
        ((X86_64, 41, 2, 1), RUN),  # socket(AF_INET, ...)
        ((X86_64, 41, 10, 2), RUN),  # socket(AF_INET6, ...)
        ((X86_64, 41, 16, 3), RUN),  # netlink
        ((X86_64, 53, 1, 1), RUN),  # socketpair(AF_UNIX, SOCK_STREAM)
        ((X86_64, 53, 1, 1 | 0x80000 | 0x800), RUN),  # with both flags
        ((X86_64, 53, 1, 2), REFUSED),  # datagram pair
        ((X86_64, 53, 1, 5 | 0x80000), REFUSED),  # seqpacket pair
        ((X86_64, 425), NO_SUCH_CALL),
        ((X86_64, 426), NO_SUCH_CALL),
        ((X86_64, 427), NO_SUCH_CALL),
        ((X86_64, 424), RUN),
        ((X86_64, 428), RUN),
        ((X86_64, 0), RUN),
        ((X86_64, 41 | 0x40000000, 1, 1), NO_SUCH_CALL),  # x32 socket
        ((X86_64, 0 | 0x40000000), NO_SUCH_CALL),  # any x32 call
        ((I386, 359, 1, 1), NO_SUCH_CALL),  # 32-bit socket
        ((I386, 102, 1), NO_SUCH_CALL),  # 32-bit socketcall
        ((AARCH64, 198, 1, 1), NO_SUCH_CALL),
    ],
    "aarch64": [
        ((AARCH64, 198, 1, 1), REFUSED),
        ((AARCH64, 198, 2, 1), RUN),
        ((AARCH64, 199, 1, 1 | 0x80000), RUN),
        ((AARCH64, 199, 1, 2), REFUSED),
        ((AARCH64, 425), NO_SUCH_CALL),
        ((AARCH64, 427), NO_SUCH_CALL),
        ((AARCH64, 63), RUN),
        # 41 is not socket here (it is pivot_root); x32 does not exist.
        ((AARCH64, 41, 1, 1), RUN),
        ((AARCH64, 0x40000000), RUN),
        ((X86_64, 41, 1, 1), NO_SUCH_CALL),
        ((0x40000028, 281, 1, 1), NO_SUCH_CALL),  # 32-bit arm socket
    ],
}


def mismatches(program: bytes, machine: str) -> list:
    return [
        (call, want, got) for call, want in CASES[machine] if (got := run(program, *call)) != want
    ]


class ProgramTest(unittest.TestCase):
    def test_each_machine_does_what_the_kernel_tables_say(self):
        for machine in CASES:
            with self.subTest(machine):
                self.assertEqual(mismatches(unix_socket_filter(machine), machine), [])

    def test_other_names_for_the_same_machines(self):
        self.assertEqual(unix_socket_filter("amd64"), unix_socket_filter("x86_64"))
        self.assertEqual(unix_socket_filter("ARM64"), unix_socket_filter("aarch64"))

    def test_other_machines_have_no_filter(self):
        for machine in ("i686", "armv7l", "riscv64", "ppc64le", "s390x", ""):
            self.assertIsNone(unix_socket_filter(machine), machine)

    def test_the_program_fits_the_kernel(self):
        for machine in CASES:
            program = unix_socket_filter(machine)
            self.assertEqual(len(program) % 8, 0)
            self.assertLessEqual(len(program) // 8, 4096)

    def test_copies_broken_on_purpose_fail(self):
        # Each break removes or changes one thing the filter relies on.
        def without(lines, line):
            return [item for item in lines if item != line]

        x86 = MACHINES["x86_64"]
        lines = filter_lines(x86)
        broken = {
            "no architecture check": [lines[0], *lines[2:]],
            "no x32 check": without(lines, (0x35, 0x40000000, "no_such_call", None)),
            "io_uring allowed": [
                (0x35, 425, "allow", "allow") if item == (0x35, 425, None, "allow") else item
                for item in lines
            ],
            "socket type not masked": without(lines, (0x54, 0xF)),
            "every pair allowed": [
                (0x15, 1, "allow", "allow") if item == (0x15, 1, "allow", "refuse") else item
                for item in lines
            ],
            "domain read from the second argument": [
                (0x20, 24) if item == (0x20, 16) else item for item in lines
            ],
        }
        for name, copy in broken.items():
            with self.subTest(name):
                self.assertNotEqual(copy, lines)
                self.assertNotEqual(mismatches(assemble(copy), "x86_64"), [])

    def test_labels_are_checked(self):
        with self.assertRaisesRegex(ValueError, "used twice"):
            assemble(["a", (0x06, 0), "a", (0x06, 0)])
        with self.assertRaises(KeyError):
            assemble([(0x15, 1, "nowhere", None), (0x06, 0)])


if __name__ == "__main__":
    unittest.main()
