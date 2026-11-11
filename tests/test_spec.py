"""Tests for writing SPEC.md, with its text and its counts written out by hand.

The counts are those of the handmade files in tests/test_counting.py, which
checks that they are what the raw files give.
"""

import copy
import unittest

from tenx.ports import KeptRows
from tenx.spec import render

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

SPEC = """# Spec

The models to build, one section each. Row counts are from the raw files
as they were when the spec was written.

## `stg_shop__orders` (staging)

One row per live order, refunds included.

- Source: `shop.orders`, 4 rows
- Key: `order_id`
- Leave out:
  - `test_mode`, 1 row: Test orders are not real. Rule: `livemode = 'false'`
- Rows to keep: 3
- Money: `amount` must add up to `amount` in the source, for each `currency` (`currency` in the source)

| Column | From | Meaning |
| --- | --- | --- |
| `order_id` | `shop.orders.id` | The order ID. |
| `amount` | `shop.orders.amount` | Minor units \\| negative for refunds. |
| `currency` | `shop.orders.currency` | Code. |

## `stg_shop__visits` (staging)

One row per visit.

- Source: `shop.visits`, 5 rows
- Key: `visit_id`, `page`
- Leave out:
  - `repeat_visits`, 2 rows: Visits are sometimes logged twice. Rule: exact copies of another row; one copy is kept
- Rows to keep: 3

| Column | From | Meaning |
| --- | --- | --- |
| `visit_id` | `shop.visits.id` | The visit ID. |
| `page` | `shop.visits.page` | The page. |

## Assumptions

- Amounts are in minor units.

## Questions for the requester

None.
"""


COUNTS = {
    "stg_shop__orders": KeptRows(4, (("test_mode", 1),), 3),
    "stg_shop__visits": KeptRows(5, (("repeat_visits", 2),), 3),
}


class SpecTest(unittest.TestCase):
    def test_the_spec_is_written_out(self):
        self.assertEqual(render(CONTRACT, COUNTS), SPEC)

    def test_a_model_with_nothing_left_out_and_questions(self):
        contract = copy.deepcopy(CONTRACT)
        contract["models"] = contract["models"][1:]
        contract["models"][0]["exclusions"] = []
        contract["assumptions"] = []
        contract["questions"] = ["Should a repeated visit count twice?"]
        text = render(contract, {"stg_shop__visits": KeptRows(5, (), 5)})
        self.assertIn("- Leave out: nothing\n- Rows to keep: 5\n", text)
        self.assertTrue(
            text.endswith(
                "## Assumptions\n\nNone.\n\n## Questions for the requester\n\n"
                "- Should a repeated visit count twice?\n"
            )
        )

    def test_a_total_without_grouping(self):
        contract = copy.deepcopy(CONTRACT)
        contract["models"][0]["totals"][0].update(by="", source_by="")
        text = render(contract, COUNTS)
        self.assertIn("- Money: `amount` must add up to `amount` in the source\n\n", text)


if __name__ == "__main__":
    unittest.main()
