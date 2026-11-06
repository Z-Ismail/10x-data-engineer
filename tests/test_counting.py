"""Counting what a contract keeps, in a separate process, on handmade files counted by eye."""

import copy
import tempfile
import unittest
from pathlib import Path

import duckdb
from test_spec import CONTRACT, ORDERS, VISITS

from tenx.adapters.duckdb import DuckDBProfiler
from tenx.contract import ContractError
from tenx.counting import count_kept
from tenx.ports import COUNT_KEPT, KeptRows, PlatformError


class CountCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.raw = self.root / "raw"
        (self.raw / "shop").mkdir(parents=True)
        (self.raw / "shop" / "orders.csv").write_text(ORDERS, encoding="utf-8")
        (self.raw / "shop" / "visits.csv").write_text(VISITS, encoding="utf-8")
        self.work = self.root / "count"

    def with_rule(self, where: str) -> dict:
        contract = copy.deepcopy(CONTRACT)
        contract["models"][0]["exclusions"][0]["where"] = where
        return contract


class CountTest(CountCase):
    def test_rows_are_counted_from_the_raw_files(self):
        self.assertEqual(
            count_kept(CONTRACT, self.raw, self.work),
            {
                "stg_shop__orders": KeptRows(4, (("test_mode", 1),), 3),
                "stg_shop__visits": KeptRows(5, (("repeat_visits", 2),), 3),
            },
        )

    def test_a_row_two_rules_match_is_charged_to_the_first(self):
        contract = copy.deepcopy(CONTRACT)
        rules = contract["models"][0]["exclusions"]
        rules.append({**rules[0], "name": "dollars", "where": "currency = 'usd'"})
        # o3 is in test mode and in dollars; o1 and o4 are only in dollars.
        counts = count_kept(contract, self.raw, self.work)
        self.assertEqual(
            counts["stg_shop__orders"], KeptRows(4, (("test_mode", 1), ("dollars", 2)), 1)
        )

    def test_a_rule_that_does_not_run_is_refused(self):
        with self.assertRaisesRegex(
            ContractError,
            r"^model stg_shop__orders: could not count the rows of shop.orders: .*mode",
        ):
            count_kept(self.with_rule("mode = 'test'"), self.raw, self.work)

    def plainly(self, rule: str) -> int:
        """The count in an ordinary DuckDB connection: the positive control."""
        source = f"read_csv('{self.raw}/shop/orders.csv', all_varchar=true, header=true)"
        con = duckdb.connect()
        try:
            return con.execute(
                f"SELECT count(*) FROM {source} WHERE coalesce(({rule}), false)"
            ).fetchone()[0]
        finally:
            con.close()

    def test_a_rule_holding_a_statement_of_its_own_is_refused(self):
        written = self.root / "written.csv"
        rule = f"true), false); COPY (SELECT 1) TO '{written}'; SELECT coalesce((1"
        self.plainly(rule)
        self.assertTrue(written.exists())
        written.unlink()
        with self.assertRaisesRegex(ContractError, "not hold statements of its own"):
            count_kept(self.with_rule(rule), self.raw, self.work)
        self.assertFalse(written.exists())

    def test_a_rule_cannot_write_into_the_raw_folder(self):
        # DuckDB may read and write the folders it is allowed; only the
        # one-statement rule (and the sandbox's read-only mount) stop this.
        written = self.raw / "shop" / "planted.csv"
        rule = f"true), false); COPY (SELECT 1 AS id) TO '{written}'; SELECT coalesce((1"
        with self.assertRaisesRegex(ContractError, "not hold statements of its own"):
            count_kept(self.with_rule(rule), self.raw, self.work)
        self.assertEqual(
            sorted(path.name for path in self.raw.rglob("*")), ["orders.csv", "shop", "visits.csv"]
        )

    def test_a_rule_cannot_read_outside_the_raw_folder(self):
        secret = self.root / "secret.txt"
        secret.write_text("s3cret\n")
        rule = f"(SELECT content FROM read_text('{secret}')) LIKE 's%'"
        self.assertEqual(self.plainly(rule), 4)
        with self.assertRaisesRegex(ContractError, "disabled by configuration"):
            count_kept(self.with_rule(rule), self.raw, self.work)

    def test_a_rule_that_does_not_finish_is_stopped(self):
        rule = "(SELECT count(*) FROM range(100000000000) a, range(100000000000) b) > 0"
        with self.assertRaisesRegex(ContractError, "did not finish within 2 seconds"):
            count_kept(self.with_rule(rule), self.raw, self.work, timeout=2)

    def test_a_source_with_no_raw_file_is_refused(self):
        (self.raw / "shop" / "visits.csv").unlink()
        with self.assertRaisesRegex(
            ContractError, r"^model stg_shop__visits: no raw file for shop.visits$"
        ):
            count_kept(CONTRACT, self.raw, self.work)


class AdapterTest(CountCase):
    def test_the_profiler_counts_and_says_so(self):
        profiler = DuckDBProfiler()
        self.assertIn(COUNT_KEPT, profiler.capabilities())
        counts = profiler.count_kept(CONTRACT, self.raw, self.work)
        self.assertEqual(counts["stg_shop__visits"], KeptRows(5, (("repeat_visits", 2),), 3))

    def test_a_refusal_is_a_platform_error(self):
        with self.assertRaisesRegex(PlatformError, "could not count the rows"):
            DuckDBProfiler().count_kept(self.with_rule("mode = 1"), self.raw, self.work)


if __name__ == "__main__":
    unittest.main()
