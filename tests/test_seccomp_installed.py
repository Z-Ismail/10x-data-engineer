"""Tests for the Unix socket filter installed in a real process.

The filter is installed in a child process, which tries each way to get a
Unix socket; a filter that allows everything is the positive control.
"""

import json
import platform
import subprocess
import sys
import unittest

from tenx.seccomp import assemble, install_source, unix_socket_filter

# Installs the filter, then tries each way to a Unix socket and prints what
# happened, as JSON.
PROBE = """
import ctypes, json, socket, subprocess, sys

PROGRAM = bytes.fromhex(sys.argv[1])
exec(sys.argv[2])
found = {}


def attempt(name, make):
    try:
        make().close()
        found[name] = "made"
    except OSError as error:
        found[name] = error.errno


def pair(kind):
    def make():
        first, second = socket.socketpair(socket.AF_UNIX, kind)
        second.close()
        return first
    return make


attempt("unix stream", lambda: socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
attempt("unix datagram", lambda: socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM))
attempt("tcp", lambda: socket.socket(socket.AF_INET, socket.SOCK_STREAM))
attempt("stream pair", pair(socket.SOCK_STREAM | socket.SOCK_CLOEXEC))
attempt("datagram pair", pair(socket.SOCK_DGRAM))
libc = ctypes.CDLL(None, use_errno=True)
params = ctypes.create_string_buffer(120)
ring = libc.syscall(425, 4, params)
found["io_uring"] = "made" if ring >= 0 else ctypes.get_errno()
child = subprocess.run(
    [sys.executable, "-c", "import socket; socket.socket(socket.AF_UNIX)"],
    capture_output=True, text=True,
)
found["child"] = "made" if child.returncode == 0 else "refused"
print(json.dumps(found))
"""


@unittest.skipUnless(
    sys.platform.startswith("linux") and unix_socket_filter(platform.machine()),
    f"no filter for {sys.platform} on {platform.machine()}",
)
class InstalledTest(unittest.TestCase):
    """The filter in a real process."""

    def probe(self, program: bytes) -> dict:
        finished = subprocess.run(
            [sys.executable, "-I", "-c", PROBE, program.hex(), install_source],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(finished.returncode, 0, finished.stderr)
        return json.loads(finished.stdout)

    def test_no_new_unix_socket_in_the_process_or_its_children(self):
        found = self.probe(unix_socket_filter(platform.machine()))
        self.assertEqual(
            found,
            {
                "unix stream": 13,
                "unix datagram": 13,
                "tcp": "made",
                "stream pair": "made",
                "datagram pair": 13,
                "io_uring": 38,
                "child": "refused",
            },
        )

    def test_without_the_filter_each_is_made(self):
        # The positive control: a filter that allows everything.
        found = self.probe(assemble([(0x06, 0x7FFF0000)]))
        self.assertEqual(found["unix stream"], "made")
        self.assertEqual(found["datagram pair"], "made")
        self.assertEqual(found["child"], "made")
        self.assertIn(found["io_uring"], ("made", 1, 38))  # io_uring may be off

    def test_an_unknown_program_is_refused_by_the_kernel(self):
        finished = subprocess.run(
            [sys.executable, "-I", "-c", "PROGRAM = b'\\xff' * 8\n" + install_source],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(finished.returncode, 0)
        self.assertIn("OSError", finished.stderr)

    @unittest.skipUnless(platform.machine() == "x86_64", "the 32-bit entry is on x86_64")
    def test_the_32_bit_entry_is_closed(self):
        # int $0x80 makes an i386 call from a 64-bit program; 359 is socket.
        # Machine code: push rbx; mov eax,359; mov ebx,1; mov ecx,1; xor edx,edx;
        # int 0x80; pop rbx; ret, called through ctypes from executable memory.
        code = bytes.fromhex("53b867010000bb01000000b90100000031d2cd805bc3")
        probe = (
            "import ctypes, mmap, sys\n"
            "PROGRAM = bytes.fromhex(sys.argv[1])\n"
            "if sys.argv[2] == 'filter':\n"
            f"    exec({install_source!r})\n"
            "memory = mmap.mmap(-1, 4096, prot=mmap.PROT_READ | mmap.PROT_WRITE | mmap.PROT_EXEC)\n"
            f"memory.write({code!r})\n"
            "address = ctypes.addressof(ctypes.c_char.from_buffer(memory))\n"
            "call = ctypes.CFUNCTYPE(ctypes.c_int)(address)\n"
            "print(call())\n"
        )
        program = unix_socket_filter("x86_64").hex()
        results = {}
        for mode in ("filter", "none"):
            finished = subprocess.run(
                [sys.executable, "-I", "-c", probe, program, mode],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(finished.returncode, 0, finished.stderr)
            results[mode] = int(finished.stdout)
        if results["none"] < 0:
            self.skipTest(f"no 32-bit entry on this kernel ({results['none']})")
        self.assertEqual(results["filter"], -38)


if __name__ == "__main__":
    unittest.main()
