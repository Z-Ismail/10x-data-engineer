"""Tests for the cross-table overlap report.

Expected figures are worked out by hand from the files written here, or
recounted with the csv and json modules, never by calling the module's own
SQL.
"""

import tempfile
import unittest
from pathlib import Path

from tenx.overlap import overlap_folder
from tenx.profile import ProfileError

CUSTOMERS = """id,email,country,signup
c1,ann@x.example,GB,2026-01-01
c2,Bob@x.example ,GB,2026-01-02
c3,cat@x.example,FR,2026-01-03
c4,"  ",FR,2026-01-04
c5,null,GB,2026-01-05
"""
ORDERS = """order_id,customer_id,amount
o1,c1,10
o2,c1,20
o3,c2,5
o4,c9,7
o5,,3
"""
CONTACTS = """contact,email,country,score
k1,ANN@x.example,GB,3
k2,bob@x.example,US,50
k3,dan@x.example,DE,60
k4,anonymous,ES,
k5, ann@x.example,IT,
"""


def write(root: Path, name: str, text: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def find(report: dict, first: str, second: str) -> dict:
    """The pair whose sides are the two `table.column` names, in that order."""
    for pair in report["pairs"]:
        names = [f"{side['table']}.{side['column']}" for side in pair["sides"]]
        if names == [first, second]:
            return pair
    raise AssertionError(f"no pair {first} ~ {second}")


class HandmadeTables(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        write(root, "shop/customers.csv", CUSTOMERS)
        write(root, "shop/orders.csv", ORDERS)
        write(root, "crm/contacts.csv", CONTACTS)
        cls.report = overlap_folder(root)
        cls.columns = {
            f"{column['table']}.{column['column']}": column for column in cls.report["columns"]
        }

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_tables_and_columns(self):
        self.assertEqual(self.report["tables"], ["crm.contacts", "shop.customers", "shop.orders"])
        self.assertEqual(len(self.columns), 11)
        self.assertEqual(self.report["skipped"], [])

    def test_nulls_and_placeholders_are_left_out_and_counted(self):
        email = self.columns["shop.customers.email"]
        self.assertEqual(
            (email["rows_in_table"], email["nulls"], email["blanks"], email["placeholders"]),
            (5, 0, 1, 1),
        )
        self.assertEqual((email["rows"], email["values"]), (3, 3))
        contacts = self.columns["crm.contacts.email"]
        self.assertEqual(
            (contacts["placeholders"], contacts["rows"], contacts["values"]), (1, 4, 3)
        )

    def test_one_row_per_value_is_judged_after_normalising(self):
        # k1 and k5 hold the same address in different case and spacing.
        self.assertFalse(self.columns["crm.contacts.email"]["one_row_per_value"])
        self.assertTrue(self.columns["shop.customers.email"]["one_row_per_value"])
        self.assertTrue(self.columns["shop.customers.id"]["one_row_per_value"])
        self.assertFalse(self.columns["shop.orders.customer_id"]["one_row_per_value"])

    def test_only_pairs_that_share_a_value_are_reported(self):
        names = sorted(
            tuple(f"{side['table']}.{side['column']}" for side in pair["sides"])
            for pair in self.report["pairs"]
        )
        self.assertEqual(
            names,
            [
                ("crm.contacts.email", "shop.customers.email"),
                ("crm.contacts.score", "shop.orders.amount"),
                ("shop.customers.country", "crm.contacts.country"),
                ("shop.orders.customer_id", "shop.customers.id"),
            ],
        )

    def test_reference_within_a_source(self):
        pair = find(self.report, "shop.orders.customer_id", "shop.customers.id")
        orders, customers = pair["sides"]
        self.assertEqual(pair["shared"], 2)
        self.assertTrue(pair["listed"])
        self.assertEqual((orders["values"], orders["found"]), (3, 2))
        self.assertAlmostEqual(orders["containment"], 2 / 3)
        self.assertEqual(
            (orders["rows"], orders["rows_matched"], orders["rows_matched_exactly"]), (4, 3, 3)
        )
        self.assertEqual(orders["not_found"], ["c9"])
        self.assertEqual((customers["values"], customers["found"]), (5, 2))
        self.assertAlmostEqual(customers["containment"], 0.4)
        self.assertEqual((customers["rows"], customers["rows_matched"]), (5, 2))
        self.assertEqual(customers["not_found"], ["c3", "c4", "c5"])

    def test_values_match_across_case_and_spaces_but_not_exactly(self):
        pair = find(self.report, "crm.contacts.email", "shop.customers.email")
        contacts, customers = pair["sides"]
        self.assertEqual(pair["shared"], 2)
        self.assertEqual(
            (contacts["rows"], contacts["rows_matched"], contacts["rows_matched_exactly"]),
            (4, 3, 0),
        )
        self.assertEqual(
            (customers["rows"], customers["rows_matched"], customers["rows_matched_exactly"]),
            (3, 2, 0),
        )
        self.assertEqual(contacts["not_found"], ["dan@x.example"])
        self.assertEqual(customers["not_found"], ["cat@x.example"])

    def test_a_pair_is_listed_at_exactly_half(self):
        pair = find(self.report, "shop.customers.country", "crm.contacts.country")
        self.assertEqual([side["containment"] for side in pair["sides"]], [0.5, 0.2])
        self.assertTrue(pair["listed"])

    def test_a_pair_below_half_is_reported_but_not_listed(self):
        pair = find(self.report, "crm.contacts.score", "shop.orders.amount")
        self.assertAlmostEqual(pair["sides"][0]["containment"], 1 / 3)
        self.assertAlmostEqual(pair["sides"][1]["containment"], 0.2)
        self.assertFalse(pair["listed"])

    def test_pairs_are_ordered_by_overlap(self):
        firsts = [pair["sides"][0]["containment"] for pair in self.report["pairs"]]
        self.assertEqual(firsts, sorted(firsts, reverse=True))
        # Ties are broken by name, so the order does not depend on the files.
        self.assertEqual(self.report["pairs"][0]["sides"][0]["table"], "crm.contacts")


class TypedAndNestedJson(unittest.TestCase):
    def test_numbers_match_their_text_and_nested_columns_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root, "shop/orders.csv", ORDERS)
            write(
                root,
                "app/events.jsonl",
                '{"order": "o1", "amount": 10, "tags": ["a"], "meta": {"k": 1}}\n'
                '{"order": "O2 ", "amount": 99, "tags": [], "meta": {"k": 2}}\n',
            )
            report = overlap_folder(root)
        self.assertEqual(
            sorted((column["column"], column["type"]) for column in report["skipped"]),
            [("meta", "STRUCT(k BIGINT)"), ("tags", "VARCHAR[]")],
        )
        amount = find(report, "app.events.amount", "shop.orders.amount")
        self.assertEqual(amount["shared"], 1)
        self.assertEqual(amount["sides"][0]["rows_matched_exactly"], 1)
        order = find(report, "app.events.order", "shop.orders.order_id")
        self.assertEqual(order["sides"][0]["found"], 2)
        self.assertEqual(order["sides"][0]["rows_matched_exactly"], 1)


class Errors(unittest.TestCase):
    def test_one_table_is_not_enough(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(Path(tmp), "shop/orders.csv", ORDERS)
            with self.assertRaisesRegex(ProfileError, "at least two tables"):
                overlap_folder(Path(tmp))

    def test_two_files_with_the_same_table_name_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(Path(tmp), "shop/orders.csv", ORDERS)
            write(Path(tmp), "shop/orders.jsonl", '{"a": 1}\n')
            with self.assertRaisesRegex(ProfileError, "would both be read as shop.orders"):
                overlap_folder(Path(tmp))


if __name__ == "__main__":
    unittest.main()
