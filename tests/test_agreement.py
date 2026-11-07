"""Scoring an architect's contract against a declaration written apart from it."""

import copy
import csv
import json
import tempfile
import unittest
from pathlib import Path

from demo.generate import generate
from tenx.agreement import Agreement, ModelAgreement, declared_rows, last_contract, score
from tenx.counting import count_kept
from tenx.gate import load_declaration, parse_declaration

ROOT = Path(__file__).resolve().parent.parent
DEMO_CONTRACT = json.loads((ROOT / "tests" / "fixtures" / "demo_contract.json").read_text())
DEMO_DECLARATION = load_declaration(ROOT / "tests" / "fixtures" / "demo_gate.json")


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


class LastContractTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.run = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def attempt(self, number, kept=None):
        folder = self.run / "architect" / f"attempt-{number}"
        folder.mkdir(parents=True)
        if kept is not None:
            (folder / "contract.json").write_text(json.dumps(CONTRACT))
            counts = {n: {"source_rows": 10, "excluded": {}, "kept": k} for n, k in kept.items()}
            (folder / "counts.json").write_text(json.dumps(counts))

    def test_the_last_attempt_is_read(self):
        self.attempt(1, {"stg_shop__orders": 1, "stg_shop__lines": 1})
        self.attempt(2, KEPT)
        self.assertEqual(last_contract(self.run), (CONTRACT, KEPT))

    def test_no_usable_contract_reads_as_none(self):
        self.assertIsNone(last_contract(self.run))
        self.attempt(1)
        self.assertIsNone(last_contract(self.run))
        (self.run / "architect" / "attempt-1" / "contract.json").write_text("{}")
        (self.run / "architect" / "attempt-1" / "counts.json").write_text("[1]")
        self.assertIsNone(last_contract(self.run))


class DemoDataTest(unittest.TestCase):
    """The hand-written demo contract, counted on demo data, against the demo declaration."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.raw = root / "data"
        generate(42, 200, cls.raw, root / "answers")
        cls.wanted = declared_rows(DEMO_DECLARATION, cls.raw)
        cls.work = root / "count"

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def scored(self, contract):
        counts = count_kept(contract, self.raw, self.work)
        kept = {name: rows.kept for name, rows in counts.items()}
        return score(contract, kept, DEMO_DECLARATION, self.wanted)

    def test_the_demo_contract_agrees_on_every_declared_model(self):
        agreement = self.scored(DEMO_CONTRACT)
        self.assertEqual(agreement.describe(), "5 of 5 models agree")

    def test_a_contract_keeping_test_mode_invoices_differs_by_exactly_those(self):
        with open(self.raw / "billing" / "invoices.csv", newline="") as file:
            rows = list(csv.DictReader(file))
        test_mode = sum(row["livemode"] == "false" for row in rows)
        self.assertGreater(test_mode, 0)
        contract = copy.deepcopy(DEMO_CONTRACT)
        (invoices,) = [m for m in contract["models"] if m["name"] == "stg_billing__invoices"]
        invoices["exclusions"] = []
        agreement = self.scored(contract)
        self.assertEqual(
            agreement.describe(),
            f"4 of 5 models agree; stg_billing__invoices: rows to keep {len(rows)} "
            f"not {len(rows) - test_mode}",
        )


if __name__ == "__main__":
    unittest.main()
