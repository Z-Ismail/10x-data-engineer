"""Tests for the memory limit on the contract's counting process.

A contract's rules are SQL an agent may have written, and one can take
memory without end. The counting process starts with a limit on the private
memory it can use, which everything it starts inherits, in the sandbox as
well, and DuckDB is told a quarter of it and cannot be told more.

The sandboxed tests are skipped where bubblewrap cannot run, unless
TENX_REQUIRE_SANDBOX is set, as it is in CI; then they fail instead.
"""

import json
import unittest
from pathlib import Path
from unittest import mock

from test_counting import CountCase
from test_gate_sandbox import needs_sandbox
from test_spec import CONTRACT

from tenx import counting
from tenx.contract import ContractError
from tenx.counting import count_kept
from tenx.gate import GIB, build_memory
from tenx.ports import KeptRows
from tenx.sandbox import Unisolated, default_sandbox

CAP = 1 * GIB
# Stands in for the counting script: reports, as a failed count, the limit
# on its own data memory, the limit a process it starts has, and the memory
# DuckDB was to be told.
LIMIT_PROBE = """
import json, resource, subprocess, sys
job = json.loads(sys.stdin.read())
child = subprocess.run(
    [sys.executable, "-c", "import resource; print(resource.getrlimit(resource.RLIMIT_DATA)[0])"],
    capture_output=True, text=True,
).stdout.strip()
found = [resource.getrlimit(resource.RLIMIT_DATA)[0], int(child), job["duckdb_memory"]]
print(json.dumps({"failed": "", "error": json.dumps(found)}))
sys.exit(1)
"""


def probe(raw: Path, work: Path, sandbox, memory: int | None) -> list:
    """What the limit probe found in the counting process."""
    with (
        mock.patch.object(counting, "SCRIPT", LIMIT_PROBE),
        unittest.TestCase().assertRaises(ContractError) as caught,
    ):
        count_kept(CONTRACT, raw, work, sandbox, memory=memory)
    return json.loads(str(caught.exception).rpartition(": ")[2])


class LimitTest(CountCase):
    def test_the_process_and_what_it_starts_have_the_limit(self):
        found = probe(self.raw, self.work, Unisolated(), CAP)
        self.assertEqual(found, [CAP, CAP, "256MiB"])

    def test_by_default_the_limit_is_a_gate_builds(self):
        memory = build_memory()
        found = probe(self.raw, self.work, Unisolated(), None)
        self.assertEqual(found, [memory, memory, f"{memory // 4 // 2**20}MiB"])

    def test_duckdb_counts_with_a_quarter_of_the_limit(self):
        # A rule that holds only when DuckDB's own limit is a quarter of the
        # process's leaves out every order.
        rule = "current_setting('memory_limit') = '256.0 MiB'"
        counts = count_kept(self.with_rule(rule), self.raw, self.work, memory=CAP)
        self.assertEqual(counts["stg_shop__orders"], KeptRows(4, (("test_mode", 4),), 0))
        counts = count_kept(self.with_rule(rule), self.raw, self.work, memory=2 * CAP)
        self.assertEqual(counts["stg_shop__orders"], KeptRows(4, (("test_mode", 0),), 4))


@needs_sandbox
class SandboxLimitTest(CountCase):
    def test_the_limit_holds_inside_the_sandbox(self):
        found = probe(self.raw, self.work, default_sandbox(), CAP)
        self.assertEqual(found, [CAP, CAP, "256MiB"])

    def test_the_contract_is_counted_under_the_default_limit(self):
        counts = count_kept(CONTRACT, self.raw, self.work, default_sandbox())
        self.assertEqual(counts["stg_shop__orders"], KeptRows(4, (("test_mode", 1),), 3))


if __name__ == "__main__":
    unittest.main()
