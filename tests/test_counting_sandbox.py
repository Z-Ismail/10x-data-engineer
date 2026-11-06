"""Counting a contract's rows in the real sandbox, which holds even without DuckDB's own guards.

A rule can be written to read a hidden file or to write into the raw folder.
DuckDB's settings and the one-SELECT rule stop both (tests/test_counting.py);
here they are taken out of the counting process, so that the sandbox alone
must stop them. The same unguarded process without the sandbox reads and
writes both, so the probes are shown able to see a leak.

Skipped where bubblewrap cannot run, unless TENX_REQUIRE_SANDBOX is set.
"""

import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_gate_sandbox import needs_sandbox
from test_spec import CONTRACT, ORDERS, VISITS

from tenx import counting
from tenx.contract import ContractError
from tenx.counting import count_kept
from tenx.ports import KeptRows
from tenx.sandbox import Bubblewrap, Unisolated, default_sandbox

GUARDS = (
    'con.execute("SET enable_external_access = false")\n',
    'con.execute("SET lock_configuration = true")\n',
    (
        "        if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:\n"
        '            raise ValueError("a rule must be part of one SELECT, not hold statements of its own")\n'
    ),
)


def unguarded() -> str:
    script = counting.SCRIPT
    for guard in GUARDS:
        if guard not in script:
            raise AssertionError(f"the counting script no longer holds {guard!r}")
        script = script.replace(guard, "")
    return script.replace("statements = con.extract_statements(query)\n", "")


class Probes:
    """Files for one test: raw data, a hidden secret, and rules that reach for them."""

    def __init__(self, root: Path) -> None:
        self.raw = root / "raw"
        (self.raw / "shop").mkdir(parents=True)
        (self.raw / "shop" / "orders.csv").write_text(ORDERS, encoding="utf-8")
        (self.raw / "shop" / "visits.csv").write_text(VISITS, encoding="utf-8")
        self.run = root / "run"
        self.run.mkdir()
        self.secret = self.run / "checks.json"
        self.secret.write_text("s3cret\n")
        self.work = self.run / "architect" / "count"
        self.planted = self.raw / "shop" / "planted.csv"

    def contract(self, where: str) -> dict:
        made = copy.deepcopy(CONTRACT)
        made["models"][0]["exclusions"][0]["where"] = where
        return made

    def read(self) -> dict:
        return self.contract(f"(SELECT content FROM read_text('{self.secret}')) LIKE 's%'")

    def write(self) -> dict:
        return self.contract(
            f"true), false); COPY (SELECT 1 AS id) TO '{self.planted}'; SELECT coalesce((1"
        )


class UnguardedWithoutSandboxTest(unittest.TestCase):
    """The positive control: no guards and no sandbox, and both probes succeed."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.probes = Probes(Path(folder.name))

    def test_the_secret_is_read_and_the_raw_folder_written(self):
        probes = self.probes
        with mock.patch.object(counting, "SCRIPT", unguarded()):
            counts = count_kept(probes.read(), probes.raw, probes.work, Unisolated())
            # The secret starts with s, so the rule left out every order.
            self.assertEqual(counts["stg_shop__orders"].excluded, (("test_mode", 4),))
            # The second statement runs; the counts after it then fail to parse.
            with self.assertRaises(ContractError):
                count_kept(probes.write(), probes.raw, probes.work, Unisolated())
        self.assertTrue(probes.planted.exists())


@needs_sandbox
class UnguardedInSandboxTest(unittest.TestCase):
    location = None

    @staticmethod
    def sandbox(home: Path) -> Bubblewrap:
        return default_sandbox()

    def setUp(self):
        folder = tempfile.TemporaryDirectory(dir=self.location)
        self.addCleanup(folder.cleanup)
        root = Path(folder.name).resolve()
        self.probes = Probes(root)
        self.box = self.sandbox(root)
        self.hidden = (self.probes.run,)

    def count(self, contract):
        with mock.patch.object(counting, "SCRIPT", unguarded()):
            return count_kept(contract, self.probes.raw, self.probes.work, self.box, self.hidden)

    def test_a_hidden_file_cannot_be_read(self):
        # The sandbox shows a hidden file as empty, so the rule matches nothing.
        counts = self.count(self.probes.read())
        self.assertEqual(counts["stg_shop__orders"].excluded, (("test_mode", 0),))

    def test_the_raw_folder_cannot_be_written(self):
        with self.assertRaisesRegex(ContractError, "Read-only file system|Permission denied"):
            self.count(self.probes.write())
        self.assertFalse(self.probes.planted.exists())

    def test_an_interpreter_named_through_a_hidden_folder_still_starts(self):
        # As when the runner is started as ../venv/bin/python from a folder
        # the sandbox hides: the path goes through that folder.
        hidden = tempfile.TemporaryDirectory()
        self.addCleanup(hidden.cleanup)
        depth = len(Path(hidden.name).resolve().parts) - 1
        given = hidden.name + "/.." * depth + sys.executable
        with mock.patch.object(counting.sys, "executable", given):
            counts = count_kept(CONTRACT, self.probes.raw, self.probes.work, self.box, self.hidden)
        self.assertEqual(counts["stg_shop__orders"].kept, 3)

    def test_the_counts_are_the_same_in_the_sandbox_with_the_guards(self):
        counts = count_kept(CONTRACT, self.probes.raw, self.probes.work, self.box, self.hidden)
        self.assertEqual(counts["stg_shop__orders"], KeptRows(4, (("test_mode", 1),), 3))
        self.assertEqual(counts["stg_shop__visits"], KeptRows(5, (("repeat_visits", 2),), 3))


class UnguardedInSandboxOutsideTemporaryFoldersTest(UnguardedInSandboxTest):
    """The same with the files in the real home folder, which the sandbox is told is the test's.

    In a temporary folder the secret is hidden whatever else the sandbox
    does; here only the hidden run folder hides it.
    """

    location = Path.home()

    @staticmethod
    def sandbox(home: Path) -> Bubblewrap:
        return Bubblewrap(default_sandbox().executable, home=home / "home")


if __name__ == "__main__":
    unittest.main()
