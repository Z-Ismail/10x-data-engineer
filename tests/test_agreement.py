"""Scoring an architect's contract against a declaration written apart from it."""

import copy
import unittest

from tenx.agreement import Agreement, ModelAgreement, score
from tenx.gate import parse_declaration


def model(name, key, source, totals=(), exclusions=()):
    return {
        "name": name,
        "layer": "staging" if name.startswith("stg_") else "intermediate",
        "grain": "One row per thing.",
        "key": list(key),
        "source": source,
        "columns": [{"name": key[0], "from": [f"{source}.id"], "description": "It."}],
        "exclusions": list(exclusions),
        "totals": [
            {"column": c, "source_column": s, "by": b, "source_by": sb} for c, s, b, sb in totals
        ],
    }


TEST_MODE = {"name": "tests", "reason": "Not real.", "rule": "where", "where": "live = 'no'"}
CONTRACT = {
    "models": [
        model("stg_shop__orders", ["order_id"], "shop.orders", [("amount", "", "currency", "")]),
        model("stg_shop__lines", ["order_id", "line"], "shop.lines", exclusions=[TEST_MODE]),
    ],
    "assumptions": [],
    "questions": [],
}
DECLARED = parse_declaration(
    {
        "models": [
            {
                "name": "stg_shop__orders",
                "key": ["order_id"],
                "source": "shop.orders",
                "totals": [{"column": "amount", "by": "currency"}],
            },
            {"name": "stg_shop__lines", "key": ["order_id", "line"], "source": "shop.lines"},
        ]
    }
)
# What the spec said each model keeps, and what the declaration wants.
KEPT = {"stg_shop__orders": 6, "stg_shop__lines": 9}
WANTED = {"stg_shop__orders": 6, "stg_shop__lines": 9}


def changed(edit):
    contract = copy.deepcopy(CONTRACT)
    edit(contract["models"])
    return contract


class ScoreTest(unittest.TestCase):
    def differences(self, contract, kept=KEPT):
        return {m.model: m.differences for m in score(contract, kept, DECLARED, WANTED).models}

    def test_a_contract_saying_the_same_agrees_on_every_model(self):
        agreement = score(CONTRACT, KEPT, DECLARED, WANTED)
        self.assertEqual(
            agreement,
            Agreement(
                (ModelAgreement("stg_shop__orders", ()), ModelAgreement("stg_shop__lines", ())),
                (),
            ),
        )
        self.assertEqual(agreement.describe(), "2 of 2 models agree")

    def test_rules_are_judged_by_the_rows_they_keep_not_their_text(self):
        # The contract leaves out test rows the declaration does not name, yet
        # keeps the rows the declaration wants: they agree.
        self.assertEqual(self.differences(CONTRACT)["stg_shop__lines"], ())
        found = self.differences(CONTRACT, {**KEPT, "stg_shop__lines": 7})
        self.assertEqual(found["stg_shop__lines"], ("rows to keep 7 not 9",))

    def test_a_key_in_another_order_or_case_agrees_and_another_key_does_not(self):
        reordered = changed(lambda m: m[1].update(key=["line", "order_id"]))
        self.assertEqual(self.differences(reordered)["stg_shop__lines"], ())
        shorter = changed(lambda m: m[1].update(key=["order_id"]))
        self.assertEqual(
            self.differences(shorter)["stg_shop__lines"], ("key order_id not line, order_id",)
        )

    def test_another_source_is_named(self):
        other = changed(lambda m: m[0].update(source="shop.lines"))
        self.assertEqual(
            self.differences(other)["stg_shop__orders"],
            ("source shop.lines not shop.orders",),
        )

    def test_money_must_name_the_same_columns_and_grouping(self):
        cases = {
            "no grouping": ([("amount", "", "", "")], "money amount not amount by currency"),
            "other column": (
                [("amount", "total", "currency", "")],
                "money amount from total by currency not amount by currency",
            ),
            "no money": ([], "money none not amount by currency"),
        }
        for name, (totals, text) in cases.items():
            with self.subTest(name):
                contract = changed(
                    lambda m, t=totals: m[0].update(totals=model("x", ["a"], "s", t)["totals"])
                )
                self.assertEqual(self.differences(contract)["stg_shop__orders"], (text,))
        # The source names given explicitly, as the gate would read them.
        explicit = changed(
            lambda m: m[0].update(
                totals=model("x", ["a"], "s", [("amount", "amount", "currency", "currency")])[
                    "totals"
                ]
            )
        )
        self.assertEqual(self.differences(explicit)["stg_shop__orders"], ())

    def test_a_missing_model_and_an_extra_one_are_reported(self):
        contract = changed(
            lambda m: m.__setitem__(1, model("int_orders_x", ["order_id"], "shop.orders"))
        )
        agreement = score(contract, {"stg_shop__orders": 6, "int_orders_x": 6}, DECLARED, WANTED)
        self.assertEqual(agreement.extra, ("int_orders_x",))
        self.assertEqual(
            agreement.describe(),
            "1 of 2 models agree; stg_shop__lines: not in the spec; extra: int_orders_x",
        )

    def test_every_difference_of_a_model_is_listed_in_order(self):
        contract = changed(lambda m: m[1].update(key=["line"], source="shop.orders"))
        agreement = score(contract, {**KEPT, "stg_shop__lines": 3}, DECLARED, WANTED)
        self.assertEqual(
            agreement.describe(),
            "1 of 2 models agree; stg_shop__lines: key line not line, order_id, "
            "source shop.orders not shop.lines, rows to keep 3 not 9",
        )
        self.assertEqual(
            agreement.to_dict(),
            {
                "agreeing": 1,
                "declared": 2,
                "models": {
                    "stg_shop__orders": [],
                    "stg_shop__lines": [
                        "key line not line, order_id",
                        "source shop.orders not shop.lines",
                        "rows to keep 3 not 9",
                    ],
                },
                "extra": [],
            },
        )


if __name__ == "__main__":
    unittest.main()
