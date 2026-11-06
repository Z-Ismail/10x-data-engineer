"""Counting what a contract keeps, in a separate process, on handmade files counted by eye."""

import copy
import tempfile
import unittest
from pathlib import Path

from test_spec import CONTRACT, ORDERS, VISITS

from tenx.contract import ContractError
from tenx.counting import count_kept
from tenx.ports import KeptRows


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

    def test_a_source_with_no_raw_file_is_refused(self):
        (self.raw / "shop" / "visits.csv").unlink()
        with self.assertRaisesRegex(
            ContractError, r"^model stg_shop__visits: no raw file for shop.visits$"
        ):
            count_kept(CONTRACT, self.raw, self.work)


if __name__ == "__main__":
    unittest.main()
