"""Tests for counting what a contract keeps of the raw files.

The raw files are handmade and small enough to count by eye.
"""

import copy
import tempfile
import unittest
from pathlib import Path

from tenx.contract import ContractError
from tenx.spec import count_rows

ORDERS = """id,amount,currency,livemode
o1,100,usd,true
o2,250,eur,true
o3,999,usd,false
o4,-50,usd,true
"""

VISITS = """id,page
v1,home
v1,home
v2,cart
v2,cart
v3,home
"""

CONTRACT = {
    "models": [
        {
            "name": "stg_shop__orders",
            "layer": "staging",
            "grain": "One row per live order,\n  refunds included.",
            "key": ["order_id"],
            "source": "shop.orders",
            "columns": [
                {"name": "order_id", "from": ["shop.orders.id"], "description": "The order ID."},
                {
                    "name": "amount",
                    "from": ["shop.orders.amount"],
                    "description": "Minor units | negative for refunds.",
                },
                {"name": "currency", "from": ["shop.orders.currency"], "description": "Code."},
            ],
            "exclusions": [
                {
                    "name": "test_mode",
                    "reason": "Test orders are not real.",
                    "rule": "where",
                    "where": "livemode = 'false'",
                }
            ],
            "totals": [
                {
                    "column": "amount",
                    "source_column": "amount",
                    "by": "currency",
                    "source_by": "currency",
                }
            ],
        },
        {
            "name": "stg_shop__visits",
            "layer": "staging",
            "grain": "One row per visit.",
            "key": ["visit_id", "page"],
            "source": "shop.visits",
            "columns": [
                {"name": "visit_id", "from": ["shop.visits.id"], "description": "The visit ID."},
                {"name": "page", "from": ["shop.visits.page"], "description": "The page."},
            ],
            "exclusions": [
                {
                    "name": "repeat_visits",
                    "reason": "Visits are sometimes logged twice.",
                    "rule": "duplicate_rows",
                    "where": "",
                }
            ],
            "totals": [],
        },
    ],
    "assumptions": ["Amounts are in minor units."],
    "questions": [],
}


class SpecTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.raw = Path(self.folder.name)
        (self.raw / "shop").mkdir()
        (self.raw / "shop" / "orders.csv").write_text(ORDERS, encoding="utf-8")
        (self.raw / "shop" / "visits.csv").write_text(VISITS, encoding="utf-8")

    def tearDown(self):
        self.folder.cleanup()

    def test_rows_are_counted_from_the_raw_files(self):
        counts = count_rows(CONTRACT, self.raw)
        orders, visits = counts["stg_shop__orders"], counts["stg_shop__visits"]
        self.assertEqual(
            (orders.source_rows, orders.excluded, orders.expected_rows),
            (4, (("test_mode", 1),), 3),
        )
        self.assertEqual(
            (visits.source_rows, visits.excluded, visits.expected_rows),
            (5, (("repeat_visits", 2),), 3),
        )

    def test_a_rule_that_does_not_run_is_refused(self):
        contract = copy.deepcopy(CONTRACT)
        contract["models"][0]["exclusions"][0]["where"] = "mode = 'test'"
        with self.assertRaisesRegex(
            ContractError, r"^model stg_shop__orders: could not count the rows of shop.orders: "
        ):
            count_rows(contract, self.raw)

    def test_a_source_with_no_raw_file_is_refused(self):
        (self.raw / "shop" / "visits.csv").unlink()
        with self.assertRaisesRegex(
            ContractError, r"^model stg_shop__visits: no raw file for shop.visits$"
        ):
            count_rows(CONTRACT, self.raw)


if __name__ == "__main__":
    unittest.main()
