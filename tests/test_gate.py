"""Tests for the gate.

Expected figures are worked out by hand for the handmade tables and recounted
with the csv and json modules for the demo data, never taken from the gate's
own SQL. The gate is run against a correct dbt project, which must pass, and
against copies of it that are each broken in one known way.
"""

import csv
import dataclasses
import io
import json
import shutil
import tempfile
import unittest
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from pathlib import Path
from unittest import mock

import duckdb

from demo.generate import generate
from tenx.gate import (
    FAIL,
    PASS,
    SKIP,
    Exclusion,
    Expected,
    GateError,
    ModelSpec,
    Result,
    Total,
    TotalExpectation,
    built_models,
    check_build,
    check_expected,
    check_key,
    check_repeat,
    check_rows,
    check_total,
    expected_rows,
    expected_totals,
    load_declaration,
    main,
    passed,
    render,
    run_gate,
    source_files,
    store_expected,
)

FIXTURES = Path(__file__).parent / "fixtures"
PROJECT = FIXTURES / "demo_project"
DECLARATION = FIXTURES / "demo_gate.json"

# Seven rows. Rows 3 and 4 are test mode, rows 2 and 4 are void, row 5 is
# written twice and row 6 has no mode at all.
ORDERS = """id,mode,status,amount
1,live,paid,10
2,live,void,5
3,test,paid,7
4,test,void,9
5,live,paid,3
5,live,paid,3
6,,paid,1
"""

# Eleven rows of money. Rows 3 and 4 are test mode, rows 2 and 4 are void,
# row 5 is written twice, row 6 has no mode, row 7 has no currency, row 8 has
# no amount, row 9's amount is not a number and row 10 has three decimal
# places. By currency the raw totals are:
#   usd  10.50 + 5 + 9 - 1.005 = 23.495
#   eur  7.25 + 3.10 + 3.10    = 13.45
#   jpy  1000
#   (no currency)  2
PAYMENTS = """id,mode,status,currency,amount
1,live,paid,usd,10.50
2,live,void,usd,5
3,test,paid,eur,7.25
4,test,void,usd,9
5,live,paid,eur,3.10
5,live,paid,eur,3.10
6,,paid,jpy,1000
7,live,paid,,2
8,live,paid,usd,
9,live,paid,usd,n/a
10,live,refund,usd,-1.005
"""


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def declaration(root: Path, payload: object) -> Path:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return write(root / "gate.json", text)


class Declaration(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def load(self, payload):
        return load_declaration(declaration(self.root, payload))

    def test_a_full_declaration_is_read(self):
        models = self.load(
            {
                "models": [
                    {
                        "name": "stg_orders",
                        "key": ["order_id", "line"],
                        "source": "shop.orders",
                        "exclusions": [
                            {"name": "test_mode", "where": "mode = 'test'"},
                            {"name": "repeats", "duplicate_rows": True},
                        ],
                        "totals": [
                            {"column": "amount_minor", "source_column": "amount", "by": "currency"},
                            {"column": "tax", "by": "currency", "source_by": "cur"},
                            {"column": "fee"},
                        ],
                    },
                    {
                        "name": "orders",
                        "key": ["order_id"],
                        "expected": {
                            "columns": ["order_id", "shop_id"],
                            "query": "SELECT id AS order_id, shop AS shop_id FROM shop.orders",
                        },
                    },
                ]
            }
        )
        self.assertEqual(
            models,
            [
                ModelSpec(
                    name="stg_orders",
                    key=("order_id", "line"),
                    source="shop.orders",
                    exclusions=(
                        Exclusion(name="test_mode", where="mode = 'test'"),
                        Exclusion(name="repeats", duplicate_rows=True),
                    ),
                    totals=(
                        Total("amount_minor", "amount", "currency", "currency"),
                        Total("tax", "tax", "currency", "cur"),
                        Total("fee", "fee"),
                    ),
                ),
                ModelSpec(
                    name="orders",
                    key=("order_id",),
                    expected=Expected(
                        columns=("order_id", "shop_id"),
                        query="SELECT id AS order_id, shop AS shop_id FROM shop.orders",
                    ),
                ),
            ],
        )

    def test_the_demo_declaration_is_valid(self):
        names = [model.name for model in load_declaration(DECLARATION)]
        self.assertEqual(
            names,
            [
                "stg_billing__customers",
                "stg_billing__invoices",
                "stg_crm__accounts",
                "stg_events__events",
                "int_customers_matched_to_accounts",
            ],
        )

    def test_mistakes_are_rejected(self):
        good = {"name": "m", "key": ["id"], "source": "s.t"}
        mistakes = {
            "not JSON": "{",
            "a list, not an object": [],
            "no models": {"models": []},
            "unknown top-level field": {"models": [good], "model": []},
            "model without a name": {"models": [{"key": ["id"]}]},
            "model without a key": {"models": [{"name": "m"}]},
            "empty key": {"models": [{"name": "m", "key": []}]},
            "key that is not a list": {"models": [{"name": "m", "key": "id"}]},
            "key naming a column twice": {"models": [{"name": "m", "key": ["id", "ID"]}]},
            "misspelt model field": {"models": [{**good, "exclusion": []}]},
            "model declared twice": {"models": [good, good]},
            "exclusions without a source": {
                "models": [
                    {"name": "m", "key": ["id"], "exclusions": [{"name": "x", "where": "a = 1"}]}
                ]
            },
            "exclusion without a name": {"models": [{**good, "exclusions": [{"where": "a = 1"}]}]},
            "exclusion that removes nothing": {"models": [{**good, "exclusions": [{"name": "x"}]}]},
            "exclusion of both kinds": {
                "models": [
                    {
                        **good,
                        "exclusions": [{"name": "x", "where": "a = 1", "duplicate_rows": True}],
                    }
                ]
            },
            "misspelt exclusion field": {
                "models": [{**good, "exclusions": [{"name": "x", "filter": "a = 1"}]}]
            },
            "two exclusions with one name": {
                "models": [
                    {
                        **good,
                        "exclusions": [
                            {"name": "x", "where": "a = 1"},
                            {"name": "x", "where": "a = 2"},
                        ],
                    }
                ]
            },
            "duplicate rows declared twice": {
                "models": [
                    {
                        **good,
                        "exclusions": [
                            {"name": "x", "duplicate_rows": True},
                            {"name": "y", "duplicate_rows": True},
                        ],
                    }
                ]
            },
            "totals without a source": {
                "models": [{"name": "m", "key": ["id"], "totals": [{"column": "amount"}]}]
            },
            "totals that are not a list": {"models": [{**good, "totals": {"column": "amount"}}]},
            "total without a column": {"models": [{**good, "totals": [{"by": "currency"}]}]},
            "total with an empty column": {"models": [{**good, "totals": [{"column": " "}]}]},
            "total grouped by a number": {
                "models": [{**good, "totals": [{"column": "amount", "by": 1}]}]
            },
            "source_by without by": {
                "models": [{**good, "totals": [{"column": "amount", "source_by": "currency"}]}]
            },
            "misspelt total field": {
                "models": [{**good, "totals": [{"column": "amount", "group_by": "currency"}]}]
            },
            "column totalled twice": {
                "models": [{**good, "totals": [{"column": "amount"}, {"column": "AMOUNT"}]}]
            },
            "expected that is not an object": {"models": [{**good, "expected": "SELECT 1"}]},
            "expected without columns": {"models": [{**good, "expected": {"query": "SELECT 1"}}]},
            "expected with no columns": {
                "models": [{**good, "expected": {"columns": [], "query": "SELECT 1 AS id"}}]
            },
            "expected naming a column twice": {
                "models": [{**good, "expected": {"columns": ["id", "Id"], "query": "SELECT 1"}}]
            },
            "expected column that is not a name": {
                "models": [{**good, "expected": {"columns": [1], "query": "SELECT 1"}}]
            },
            "expected without a query": {"models": [{**good, "expected": {"columns": ["id"]}}]},
            "expected with an empty query": {
                "models": [{**good, "expected": {"columns": ["id"], "query": " "}}]
            },
            "misspelt expected field": {
                "models": [{**good, "expected": {"columns": ["id"], "sql": "SELECT 1"}}]
            },
        }
        for label, payload in mistakes.items():
            with self.subTest(label), self.assertRaises(GateError):
                self.load(payload)

    def test_a_missing_file_is_reported(self):
        with self.assertRaises(GateError):
            load_declaration(self.root / "absent.json")


def node(unique_id, status, relation_name=None, message=None):
    return {
        "unique_id": unique_id,
        "status": status,
        "relation_name": relation_name,
        "message": message,
    }


class BuildCheck(unittest.TestCase):
    models = (ModelSpec("stg_a", ("id",)), ModelSpec("stg_b", ("id",)))

    def statuses(self, results):
        return {(result.check, result.subject): result.status for result in results}

    def test_a_clean_build_passes_and_names_each_relation(self):
        run = {
            "results": [
                node("model.shop.stg_a", "success", '"build"."main"."stg_a"'),
                node("model.shop.stg_b", "success", '"build"."main"."stg_b"'),
                node("test.shop.unique_stg_a_id.0a1b", "pass"),
            ]
        }
        results, relations = check_build(0, run, self.models)
        self.assertEqual(
            self.statuses(results),
            {("build", "project"): PASS, ("build", "stg_a"): PASS, ("build", "stg_b"): PASS},
        )
        self.assertEqual(
            relations, {"stg_a": '"build"."main"."stg_a"', "stg_b": '"build"."main"."stg_b"'}
        )

    def test_a_failed_model_and_what_it_blocked_both_fail(self):
        run = {
            "results": [
                node(
                    "model.shop.stg_a", "error", '"build"."main"."stg_a"', "Binder Error\n  x\n  y"
                ),
                node("model.shop.stg_b", "skipped", '"build"."main"."stg_b"'),
            ]
        }
        results, relations = check_build(1, run, self.models)
        self.assertEqual(
            self.statuses(results),
            {("build", "project"): FAIL, ("build", "stg_a"): FAIL, ("build", "stg_b"): FAIL},
        )
        self.assertEqual(relations, {})
        project = results[0].detail
        self.assertIn("2 of 2 nodes did not succeed", project)
        self.assertIn("model.shop.stg_a: error: Binder Error: x", project)
        self.assertIn("model.shop.stg_b: skipped", project)

    def test_a_failed_project_test_fails_the_build_but_not_the_model(self):
        run = {
            "results": [
                node("model.shop.stg_a", "success", '"build"."main"."stg_a"'),
                node("model.shop.stg_b", "success", '"build"."main"."stg_b"'),
                node("test.shop.unique_stg_a_id.0a1b", "fail", message="Got 2 results"),
            ]
        }
        results, relations = check_build(1, run, self.models)
        self.assertEqual(
            self.statuses(results),
            {("build", "project"): FAIL, ("build", "stg_a"): PASS, ("build", "stg_b"): PASS},
        )
        self.assertEqual(sorted(relations), ["stg_a", "stg_b"])

    def test_a_model_missing_from_a_green_build_fails(self):
        run = {"results": [node("model.shop.stg_a", "success", '"build"."main"."stg_a"')]}
        results, relations = check_build(0, run, self.models)
        self.assertEqual(
            self.statuses(results),
            {("build", "project"): PASS, ("build", "stg_a"): PASS, ("build", "stg_b"): FAIL},
        )
        self.assertEqual(sorted(relations), ["stg_a"])

    def test_a_model_is_matched_by_its_whole_name(self):
        run = {
            "results": [
                node("model.shop.stg_a_extra", "success", '"build"."main"."stg_a_extra"'),
                node("model.shop.old_stg_a", "success", '"build"."main"."old_stg_a"'),
                node("test.shop.stg_a", "pass"),
                node("snapshot.shop.stg_b", "success", '"build"."main"."stg_b"'),
            ]
        }
        results, relations = check_build(0, run, self.models)
        self.assertEqual(
            self.statuses(results),
            {("build", "project"): PASS, ("build", "stg_a"): FAIL, ("build", "stg_b"): FAIL},
        )
        self.assertEqual(relations, {})

    def test_two_models_with_one_name_fail(self):
        run = {
            "results": [
                node("model.shop.stg_a", "success", '"build"."main"."stg_a"'),
                node("model.other.stg_a", "success", '"build"."other"."stg_a"'),
                node("model.shop.stg_b", "success", '"build"."main"."stg_b"'),
            ]
        }
        results, relations = check_build(0, run, self.models)
        self.assertEqual(self.statuses(results)[("build", "stg_a")], FAIL)
        self.assertEqual(sorted(relations), ["stg_b"])

    def test_a_model_without_a_relation_fails(self):
        run = {
            "results": [
                node("model.shop.stg_a", "success", None),
                node("model.shop.stg_b", "success", '"build"."main"."stg_b"'),
            ]
        }
        results, relations = check_build(0, run, self.models)
        self.assertEqual(self.statuses(results)[("build", "stg_a")], FAIL)
        self.assertEqual(sorted(relations), ["stg_b"])

    def test_a_warning_does_not_fail_the_build(self):
        run = {
            "results": [
                node("model.shop.stg_a", "success", '"build"."main"."stg_a"'),
                node("model.shop.stg_b", "success", '"build"."main"."stg_b"'),
                node("test.shop.recent.0a1b", "warn"),
            ]
        }
        results, _ = check_build(0, run, self.models)
        self.assertEqual(results[0].status, PASS)
        self.assertIn("1 warning", results[0].detail)

    def test_a_non_zero_exit_fails_even_when_every_node_succeeded(self):
        run = {
            "results": [
                node("model.shop.stg_a", "success", '"build"."main"."stg_a"'),
                node("model.shop.stg_b", "success", '"build"."main"."stg_b"'),
            ]
        }
        results, _ = check_build(2, run, self.models)
        self.assertEqual(results[0].status, FAIL)

    def test_a_build_that_recorded_nothing_fails_everything(self):
        output = "\n".join(f"line {n}" for n in range(1, 41))
        results, relations = check_build(2, None, self.models, output)
        self.assertEqual(
            self.statuses(results),
            {("build", "project"): FAIL, ("build", "stg_a"): FAIL, ("build", "stg_b"): FAIL},
        )
        self.assertEqual(relations, {})
        self.assertIn("exited 2", results[0].detail)
        self.assertIn("line 40", results[0].detail)
        self.assertNotIn("line 20", results[0].detail)


class BuiltModels(unittest.TestCase):
    def test_only_models_that_were_built_are_listed(self):
        run_results = {
            "results": [
                node("model.shop.stg_orders", "success", '"build"."main"."stg_orders"'),
                node("model.shop.stg_items", "error", '"build"."main"."stg_items"'),
                node("model.shop.stg_lines", "skipped", '"build"."main"."stg_lines"'),
                node("test.shop.unique_stg_orders_id.abc", "pass"),
                node("seed.shop.rates", "success", '"build"."main"."rates"'),
                node("model.shop.no_relation", "success"),
            ]
        }
        self.assertEqual(
            built_models(run_results),
            {"model.shop.stg_orders": ("stg_orders", '"build"."main"."stg_orders"')},
        )

    def test_a_name_two_packages_share_is_given_in_full(self):
        run_results = {
            "results": [
                node("model.shop.orders", "success", '"build"."main"."orders"'),
                node("model.other.orders", "success", '"build"."other"."orders"'),
                node("model.shop.items", "success", '"build"."main"."items"'),
            ]
        }
        self.assertEqual(
            built_models(run_results),
            {
                "model.shop.orders": ("model.shop.orders", '"build"."main"."orders"'),
                "model.other.orders": ("model.other.orders", '"build"."other"."orders"'),
                "model.shop.items": ("items", '"build"."main"."items"'),
            },
        )

    def test_no_results_means_no_models(self):
        self.assertEqual(built_models(None), {})


class RowsCheck(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.path = write(Path(cls.tmp.name) / "shop" / "orders.csv", ORDERS)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.con = duckdb.connect()
        self.addCleanup(self.con.close)

    def expectation(self, *exclusions):
        model = ModelSpec("stg_orders", ("id",), "shop.orders", tuple(exclusions))
        return model, expected_rows(self.con, model, self.path)

    def test_without_exclusions_every_row_is_expected(self):
        _, expectation = self.expectation()
        self.assertEqual(expectation.source_rows, 7)
        self.assertEqual(expectation.excluded, ())
        self.assertEqual(expectation.expected_rows, 7)

    def test_a_filter_removes_the_rows_it_matches(self):
        _, expectation = self.expectation(Exclusion("test_mode", where="mode = 'test'"))
        self.assertEqual(expectation.excluded, (("test_mode", 2),))
        self.assertEqual(expectation.expected_rows, 5)

    def test_a_row_matched_by_two_filters_is_removed_once(self):
        test_mode = Exclusion("test_mode", where="mode = 'test'")
        void = Exclusion("void", where="status = 'void'")
        _, expectation = self.expectation(test_mode, void)
        self.assertEqual(expectation.excluded, (("test_mode", 2), ("void", 1)))
        self.assertEqual(expectation.expected_rows, 4)
        _, reordered = self.expectation(void, test_mode)
        self.assertEqual(reordered.excluded, (("void", 2), ("test_mode", 1)))
        self.assertEqual(reordered.expected_rows, 4)

    def test_a_filter_that_cannot_be_decided_keeps_the_row(self):
        # Row 6 has no mode, so `mode <> 'live'` is neither true nor false.
        _, expectation = self.expectation(Exclusion("not_live", where="mode <> 'live'"))
        self.assertEqual(expectation.excluded, (("not_live", 2),))
        self.assertEqual(expectation.expected_rows, 5)

    def test_exact_repeats_are_counted_after_the_filters(self):
        _, expectation = self.expectation(
            Exclusion("repeats", duplicate_rows=True),
            Exclusion("test_mode", where="mode = 'test'"),
            Exclusion("void", where="status = 'void'"),
        )
        self.assertEqual(expectation.excluded, (("repeats", 1), ("test_mode", 2), ("void", 1)))
        self.assertEqual(expectation.expected_rows, 3)

    def test_a_repeat_among_excluded_rows_is_not_counted_twice(self):
        # Both copies of row 5 are removed by the filter, leaving no repeats.
        _, expectation = self.expectation(
            Exclusion("small", where="amount = '3'"), Exclusion("repeats", duplicate_rows=True)
        )
        self.assertEqual(expectation.excluded, (("small", 2), ("repeats", 0)))
        self.assertEqual(expectation.expected_rows, 5)

    def test_a_filter_that_is_not_valid_sql_stops_the_gate(self):
        with self.assertRaises(GateError):
            self.expectation(Exclusion("broken", where="no_such_column = 1"))

    def model_with(self, rows):
        self.con.execute("CREATE OR REPLACE TABLE built (id INTEGER)")
        self.con.executemany("INSERT INTO built VALUES (?)", [(n,) for n in range(rows)])

    def test_the_model_must_hold_exactly_the_expected_rows(self):
        model, expectation = self.expectation(
            Exclusion("test_mode", where="mode = 'test'"),
            Exclusion("void", where="status = 'void'"),
            Exclusion("repeats", duplicate_rows=True),
        )
        self.model_with(3)
        result = check_rows(self.con, model, "built", expectation)
        self.assertEqual(result.status, PASS)
        self.assertEqual(
            result.facts,
            {
                "source_rows": 7,
                "excluded": {"test_mode": 2, "void": 1, "repeats": 1},
                "expected_rows": 3,
                "model_rows": 3,
            },
        )
        self.assertEqual(
            result.detail,
            "7 rows in shop.orders, 4 excluded (test_mode 2, void 1, repeats 1), "
            "3 expected, 3 in the model",
        )

    def test_one_row_short_fails(self):
        model, expectation = self.expectation(Exclusion("test_mode", where="mode = 'test'"))
        self.model_with(4)
        result = check_rows(self.con, model, "built", expectation)
        self.assertEqual(result.status, FAIL)
        self.assertTrue(result.detail.endswith("1 fewer than expected"), result.detail)

    def test_one_row_over_fails(self):
        model, expectation = self.expectation(Exclusion("test_mode", where="mode = 'test'"))
        self.model_with(6)
        result = check_rows(self.con, model, "built", expectation)
        self.assertEqual(result.status, FAIL)
        self.assertTrue(result.detail.endswith("1 more than expected"), result.detail)

    def test_a_model_that_cannot_be_read_fails(self):
        model, expectation = self.expectation()
        result = check_rows(self.con, model, "no_such_table", expectation)
        self.assertEqual(result.status, FAIL)


def amounts(**values):
    """A mapping of group to exact amount; the key `none` stands for a null group."""
    return {(None if key == "none" else key): Decimal(value) for key, value in values.items()}


class TotalsExpectation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.path = write(Path(cls.tmp.name) / "shop" / "payments.csv", PAYMENTS)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.con = duckdb.connect()
        self.addCleanup(self.con.close)

    def expectation(self, total, *exclusions):
        model = ModelSpec("stg_payments", ("id",), "shop.payments", tuple(exclusions), (total,))
        return expected_totals(self.con, model, total, self.path)

    def test_an_ungrouped_total_adds_every_readable_amount(self):
        expectation = self.expectation(Total("amount", "amount"))
        self.assertEqual(expectation.source_totals, {"all": Decimal("1038.945")})
        self.assertEqual(expectation.expected, {"all": Decimal("1038.945")})
        self.assertEqual(expectation.excluded, ())

    def test_amounts_are_added_by_group_without_rounding(self):
        expectation = self.expectation(Total("amount", "amount", "currency", "currency"))
        raw = amounts(USD="23.495", EUR="13.45", JPY="1000", none="2")
        self.assertEqual(expectation.source_totals, raw)
        self.assertEqual(expectation.expected, raw)
        # The three decimal places of row 10 survive the addition.
        self.assertEqual(str(expectation.source_totals["USD"]), "23.495")

    def test_each_exclusion_is_charged_to_the_first_filter_that_matches(self):
        expectation = self.expectation(
            Total("amount", "amount", "currency", "currency"),
            Exclusion("test_mode", where="mode = 'test'"),
            Exclusion("void", where="status = 'void'"),
            Exclusion("repeats", duplicate_rows=True),
        )
        self.assertEqual(
            expectation.excluded,
            (
                ("test_mode", amounts(USD="9", EUR="7.25")),
                ("void", amounts(USD="5")),
                ("repeats", amounts(USD="0", EUR="3.10", JPY="0", none="0")),
            ),
        )
        self.assertEqual(
            expectation.expected, amounts(USD="9.495", EUR="3.10", JPY="1000", none="2")
        )

    def test_raw_codes_in_any_case_or_spacing_are_one_group(self):
        path = write(
            Path(self.tmp.name) / "shop" / "mixed.csv",
            "id,currency,amount\n1,usd,1.50\n2,USD,2\n3, Usd ,0.25\n4,eur,7\n5,,1\n6,EUR,-2\n",
        )
        total = Total("amount", "amount", "currency", "currency")
        model = ModelSpec("stg_mixed", ("id",), "shop.mixed", (), (total,))
        expectation = expected_totals(self.con, model, total, path)
        self.assertEqual(expectation.expected, amounts(USD="3.75", EUR="5", none="1"))

    def test_the_source_columns_can_have_other_names(self):
        expectation = self.expectation(Total("amount_minor", "amount", "cur", "currency"))
        self.assertEqual(
            expectation.expected, amounts(USD="23.495", EUR="13.45", JPY="1000", none="2")
        )

    def test_text_that_is_not_a_number_is_counted_and_shown(self):
        expectation = self.expectation(Total("amount", "amount"))
        self.assertEqual(expectation.unreadable, 1)
        self.assertEqual(expectation.unreadable_examples, ("n/a",))

    def test_excluded_text_is_not_counted(self):
        expectation = self.expectation(
            Total("amount", "amount"), Exclusion("unreadable", where="amount = 'n/a'")
        )
        self.assertEqual(expectation.unreadable, 0)
        self.assertEqual(expectation.excluded, (("unreadable", {"all": Decimal(0)}),))
        self.assertEqual(expectation.expected, {"all": Decimal("1038.945")})

    def test_blank_amounts_add_nothing_and_are_counted(self):
        self.assertEqual(self.expectation(Total("amount", "amount")).blank, 1)
        excluded = self.expectation(
            Total("amount", "amount"), Exclusion("no_amount", where="id = '8'")
        )
        self.assertEqual(excluded.blank, 0)
        self.assertEqual(excluded.expected, {"all": Decimal("1038.945")})

    def test_spaces_and_missing_json_fields_are_blank(self):
        csv_path = write(
            Path(self.tmp.name) / "shop" / "spaces.csv",
            'id,amount\n1,5\n2,"   "\n3,\n4,-1\n',
        )
        jsonl_path = write(
            Path(self.tmp.name) / "shop" / "partial.jsonl",
            '{"id": 1, "amount": "5"}\n{"id": 2}\n{"id": 3, "amount": null}\n',
        )
        total = Total("amount", "amount")
        for source, path, blank, expected in (
            ("shop.spaces", csv_path, 2, "4"),
            ("shop.partial", jsonl_path, 2, "5"),
        ):
            with self.subTest(source):
                model = ModelSpec("stg", ("id",), source, (), (total,))
                expectation = expected_totals(self.con, model, total, path)
                self.assertEqual(expectation.blank, blank)
                self.assertEqual(expectation.unreadable, 0)
                self.assertEqual(expectation.expected, {"all": Decimal(expected)})

    def test_numbers_typed_by_a_json_file_are_added_exactly(self):
        # Read as numbers, 0.1 and 0.2 would add up to 0.30000000000000004.
        path = write(
            Path(self.tmp.name) / "shop" / "refunds.jsonl",
            '{"amount": 0.1, "currency": "usd"}\n'
            '{"amount": 0.2, "currency": "usd"}\n'
            '{"amount": -0.5, "currency": "eur"}\n'
            '{"amount": 1e2, "currency": "eur"}\n',
        )
        total = Total("amount", "amount", "currency", "currency")
        model = ModelSpec("stg_refunds", ("id",), "shop.refunds", (), (total,))
        expectation = expected_totals(self.con, model, total, path)
        self.assertEqual(expectation.expected, amounts(USD="0.3", EUR="99.5"))
        self.assertEqual(expectation.unreadable, 0)

    def test_a_missing_source_column_stops_the_gate(self):
        with self.assertRaises(GateError):
            self.expectation(Total("amount", "no_such_column"))


class TotalCheck(unittest.TestCase):
    BY_CURRENCY = Total("amount", "amount", "currency", "currency")
    # Raw usd 30 less 5 test mode, eur 12.50 less 2.50 test mode, and 4 with
    # no currency: the model must hold USD 25, EUR 10.00 and 4.
    EXPECTATION = TotalExpectation(
        total=BY_CURRENCY,
        source_totals=amounts(USD="30", EUR="12.50", none="4"),
        excluded=(("test_mode", amounts(USD="5", EUR="2.50")),),
        expected=amounts(USD="25", EUR="10.00", none="4"),
    )

    def setUp(self):
        self.con = duckdb.connect()
        self.addCleanup(self.con.close)

    def model(self, rows, amount_type="DECIMAL(18,2)"):
        self.con.execute(
            f"CREATE OR REPLACE TABLE built (id INTEGER, currency VARCHAR, amount {amount_type})"
        )
        self.con.executemany(
            "INSERT INTO built VALUES (?, ?, ?)",
            [(n, currency, amount) for n, (currency, amount) in enumerate(rows)],
        )

    def check(self, expectation=EXPECTATION):
        spec = ModelSpec("stg_payments", ("id",), "shop.payments", (), (expectation.total,))
        return check_total(self.con, spec, "built", expectation)

    def test_money_that_ties_in_every_currency_passes(self):
        self.model([("usd", "20"), ("usd", "5"), ("eur", "7.25"), ("eur", "2.75"), (None, "4")])
        result = self.check()
        self.assertEqual(result.status, PASS, result.detail)
        self.assertEqual(result.facts["model"], amounts(USD="25", EUR="10", none="4"))
        self.assertEqual(
            result.detail,
            "amount by currency: ties with the raw data, EUR 10.00, USD 25.00, (null) 4.00",
        )

    def test_blank_raw_amounts_are_reported(self):
        expectation = TotalExpectation(
            total=self.BY_CURRENCY,
            source_totals=self.EXPECTATION.source_totals,
            excluded=self.EXPECTATION.excluded,
            expected=self.EXPECTATION.expected,
            blank=2,
        )
        self.model([("usd", "25"), ("eur", "10"), (None, "4")])
        result = self.check(expectation)
        self.assertEqual(result.status, PASS)
        self.assertEqual(result.facts["blank"], 2)
        self.assertEqual(
            result.detail,
            "amount by currency: ties with the raw data, EUR 10.00, USD 25.00, (null) 4.00; "
            "2 blank raw values added nothing",
        )
        self.model([("usd", "25"), ("eur", "9"), (None, "4")])
        result = self.check(dataclasses.replace(expectation, blank=1))
        self.assertEqual(result.status, FAIL)
        self.assertTrue(
            result.detail.startswith(
                "amount by currency: 1 of 3 currency values do not tie with the raw data; "
                "1 blank raw value added nothing\n"
            ),
            result.detail,
        )

    def test_whole_numbers_are_accepted(self):
        self.model([("usd", 25), ("eur", 10), (None, 4)], "BIGINT")
        self.assertEqual(self.check().status, PASS)

    def test_one_currency_short_fails_and_explains_the_figures(self):
        self.model([("usd", "20"), ("eur", "10"), (None, "4")])
        result = self.check()
        self.assertEqual(result.status, FAIL)
        self.assertEqual(
            result.detail,
            "amount by currency: 1 of 3 currency values do not tie with the raw data\n"
            "USD: raw 30, test_mode 5, expected 25, model 20.00, 5.00 short",
        )

    def test_a_cent_over_fails(self):
        self.model([("usd", "25"), ("eur", "10.01"), (None, "4")])
        result = self.check()
        self.assertEqual(result.status, FAIL)
        self.assertIn(
            "EUR: raw 12.50, test_mode 2.50, expected 10.00, model 10.01, 0.01 over", result.detail
        )

    def test_currency_codes_tie_whatever_their_case_or_spacing(self):
        self.model([("USD", "20"), ("usd", "3"), (" Usd ", "2"), ("EUR", "10"), (None, "4")])
        result = self.check()
        self.assertEqual(result.status, PASS, result.detail)
        self.assertEqual(result.facts["model"], amounts(USD="25", EUR="10", none="4"))

    def test_money_moved_to_another_currency_fails_on_both_sides(self):
        # The euros are labelled as dollars: the grand total is right, but
        # neither currency is.
        self.model([("usd", "25"), ("USD", "10"), (None, "4")])
        result = self.check()
        self.assertEqual(result.status, FAIL)
        self.assertIn("2 of 3 currency values", result.detail)
        self.assertIn(
            "EUR: raw 12.50, test_mode 2.50, expected 10.00, model 0, 10.00 short", result.detail
        )
        self.assertIn(
            "USD: raw 30, test_mode 5, expected 25, model 35.00, 10.00 over", result.detail
        )

    def test_a_currency_spelt_differently_is_not_the_same_currency(self):
        self.model([("US$", "25"), ("eur", "10"), (None, "4")])
        result = self.check()
        self.assertEqual(result.status, FAIL)
        self.assertIn("US$: raw 0, expected 0, model 25.00, 25.00 over", result.detail)
        self.assertIn("USD: raw 30, test_mode 5, expected 25, model 0, 25 short", result.detail)

    def test_a_null_currency_is_a_group_of_its_own(self):
        self.model([("usd", "25"), ("eur", "14")])
        result = self.check()
        self.assertEqual(result.status, FAIL)
        self.assertIn("(null): raw 4, expected 4, model 0, 4 short", result.detail)

    def test_an_ungrouped_total(self):
        expectation = TotalExpectation(
            total=Total("amount", "amount"),
            source_totals={"all": Decimal(39)},
            excluded=(),
            expected={"all": Decimal(39)},
        )
        self.model([("usd", "25"), ("eur", "10"), (None, "4")])
        self.assertEqual(self.check(expectation).status, PASS)
        self.model([("usd", "25"), ("eur", "10")])
        result = self.check(expectation)
        self.assertEqual(result.status, FAIL)
        self.assertEqual(
            result.detail,
            "amount: 1 of 1 totals do not tie with the raw data\n"
            "raw 39, expected 39, model 35.00, 4.00 short",
        )

    def test_floating_point_money_fails_even_when_it_adds_up(self):
        for kind in ("DOUBLE", "FLOAT"):
            with self.subTest(kind):
                self.model([("usd", 25), ("eur", 10), (None, 4)], kind)
                result = self.check()
                self.assertEqual(result.status, FAIL)
                self.assertIn("floating point", result.detail)

    def test_money_held_as_text_fails(self):
        self.model([("usd", "25"), ("eur", "10"), (None, "4")], "VARCHAR")
        result = self.check()
        self.assertEqual(result.status, FAIL)
        self.assertIn("VARCHAR, not a number", result.detail)

    def test_a_missing_column_fails(self):
        self.con.execute("CREATE TABLE built (id INTEGER, cur VARCHAR, amount BIGINT)")
        result = self.check()
        self.assertEqual(result.status, FAIL)
        self.assertEqual(result.facts["missing_columns"], ["currency"])

    def test_raw_values_that_are_not_numbers_fail(self):
        expectation = TotalExpectation(
            total=self.BY_CURRENCY,
            source_totals=self.EXPECTATION.source_totals,
            excluded=self.EXPECTATION.excluded,
            expected=self.EXPECTATION.expected,
            unreadable=2,
            unreadable_examples=("12,50", "n/a"),
        )
        self.model([("usd", "25"), ("eur", "10"), (None, "4")])
        result = self.check(expectation)
        self.assertEqual(result.status, FAIL)
        self.assertEqual(
            result.detail,
            "amount by currency: 2 raw values in shop.payments.amount not excluded and "
            "not a number, for example '12,50', 'n/a'",
        )

    def test_a_model_that_cannot_be_read_fails(self):
        spec = ModelSpec("stg_payments", ("id",), "shop.payments", (), (self.BY_CURRENCY,))
        result = check_total(self.con, spec, "no_such_table", self.EXPECTATION)
        self.assertEqual(result.status, FAIL)


class KeyCheck(unittest.TestCase):
    def setUp(self):
        self.con = duckdb.connect()
        self.addCleanup(self.con.close)
        self.con.execute("CREATE TABLE clean (a INTEGER, b VARCHAR)")
        self.con.execute("INSERT INTO clean VALUES (1, 'x'), (2, 'x'), (3, 'y')")
        self.con.execute("CREATE TABLE messy (a INTEGER, b VARCHAR)")
        self.con.execute(
            "INSERT INTO messy VALUES (1, 'x'), (2, 'x'), (2, 'y'), (2, 'y'), (2, 'y'), "
            "(3, NULL), (4, 'z'), (4, 'z')"
        )
        self.con.execute("CREATE TABLE nothing (a INTEGER)")

    def check(self, table, *key):
        return check_key(self.con, ModelSpec(table, tuple(key)), table)

    def test_a_unique_column_passes(self):
        result = self.check("clean", "a")
        self.assertEqual(result.status, PASS)
        self.assertEqual(
            result.facts, {"rows": 3, "null_rows": 0, "repeated_keys": 0, "surplus_rows": 0}
        )

    def test_column_names_are_matched_without_regard_to_case(self):
        self.assertEqual(self.check("clean", "A").status, PASS)

    def test_a_repeated_value_fails(self):
        result = self.check("clean", "b")
        self.assertEqual(result.status, FAIL)
        self.assertEqual(
            result.facts, {"rows": 3, "null_rows": 0, "repeated_keys": 1, "surplus_rows": 1}
        )
        self.assertIn("for example x", result.detail)

    def test_two_columns_can_be_unique_together(self):
        self.assertEqual(self.check("clean", "a", "b").status, PASS)

    def test_repeats_are_counted_by_value_and_by_surplus_row(self):
        # 2 appears four times and 4 twice: two values, four rows too many.
        result = self.check("messy", "a")
        self.assertEqual(result.status, FAIL)
        self.assertEqual(
            result.facts, {"rows": 8, "null_rows": 0, "repeated_keys": 2, "surplus_rows": 4}
        )
        self.assertIn("for example 2; 4", result.detail)

    def test_nulls_and_repeats_in_a_two_column_key(self):
        # (3, NULL) is a null key. (2, y) appears three times and (4, z) twice.
        result = self.check("messy", "a", "b")
        self.assertEqual(result.status, FAIL)
        self.assertEqual(
            result.facts, {"rows": 8, "null_rows": 1, "repeated_keys": 2, "surplus_rows": 3}
        )
        self.assertIn("1 row with a null key", result.detail)
        self.assertIn("for example 2, y; 4, z", result.detail)

    def test_a_null_key_alone_fails(self):
        self.con.execute("CREATE TABLE gap (a INTEGER)")
        self.con.execute("INSERT INTO gap VALUES (1), (NULL), (NULL)")
        result = self.check("gap", "a")
        self.assertEqual(result.status, FAIL)
        self.assertEqual(
            result.facts, {"rows": 3, "null_rows": 2, "repeated_keys": 0, "surplus_rows": 0}
        )

    def test_a_key_column_missing_from_the_model_fails(self):
        result = self.check("clean", "a", "c")
        self.assertEqual(result.status, FAIL)
        self.assertEqual(result.facts, {"missing_columns": ["c"]})

    def test_an_empty_model_has_no_key_problems(self):
        self.assertEqual(self.check("nothing", "a").status, PASS)

    def test_a_model_that_cannot_be_read_fails(self):
        self.assertEqual(self.check("no_such_table", "a").status, FAIL)


class RepeatCheck(unittest.TestCase):
    """Two builds of one table, in two database files named as the gate names them."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def check(self, first_rows, second_rows, columns="id INTEGER, name VARCHAR"):
        """Build table t from VALUES rows in each database and compare them."""
        second_columns = columns
        if isinstance(columns, tuple):
            columns, second_columns = columns
        for stem, rows, shape in (
            ("build", first_rows, columns),
            ("repeat", second_rows, second_columns),
        ):
            con = duckdb.connect(str(self.root / f"{stem}.duckdb"))
            con.execute(f"CREATE TABLE t ({shape})")
            if rows:
                con.execute(f"INSERT INTO t VALUES {rows}")
            con.close()
        con = duckdb.connect(str(self.root / "build.duckdb"), read_only=True)
        self.addCleanup(con.close)
        con.execute(f"ATTACH '{self.root / 'repeat.duckdb'}' (READ_ONLY)")
        return check_repeat(con, "t", '"build"."main"."t"', '"repeat"."main"."t"')

    def test_the_same_rows_in_another_order_pass(self):
        result = self.check("(1, 'a'), (2, 'b'), (3, 'c')", "(3, 'c'), (1, 'a'), (2, 'b')")
        self.assertEqual(result.status, PASS)
        self.assertEqual(result.detail, "the same 3 rows in both builds")
        self.assertEqual(
            result.facts,
            {
                "rows": 3,
                "second_rows": 3,
                "only_first": 0,
                "only_second": 0,
                "differing_columns": [],
            },
        )

    def test_nulls_and_repeated_rows_held_by_both_builds_pass(self):
        rows = "(1, NULL), (1, NULL), (NULL, 'x')"
        self.assertEqual(self.check(rows, rows).status, PASS)

    def test_empty_builds_pass(self):
        self.assertEqual(self.check("", "").detail, "the same 0 rows in both builds")

    def test_a_row_repeated_by_one_build_fails(self):
        # The second build holds (2, 'b') twice: one row more, and nothing
        # missing. Both columns' values differ because 2 and 'b' appear twice.
        result = self.check("(1, 'a'), (2, 'b')", "(1, 'a'), (2, 'b'), (2, 'b')")
        self.assertEqual(result.status, FAIL)
        self.assertEqual(
            result.facts,
            {
                "rows": 2,
                "second_rows": 3,
                "only_first": 0,
                "only_second": 1,
                "differing_columns": ["id", "name"],
            },
        )

    def test_a_column_that_changes_between_builds_is_named(self):
        result = self.check(
            "(1, 'a', TIMESTAMP '2026-10-14 09:00:00'), (2, 'b', TIMESTAMP '2026-10-14 09:00:00')",
            "(1, 'a', TIMESTAMP '2026-10-14 09:00:05'), (2, 'b', TIMESTAMP '2026-10-14 09:00:05')",
            "id INTEGER, name VARCHAR, loaded_at TIMESTAMP",
        )
        self.assertEqual(result.status, FAIL)
        self.assertEqual(result.facts["only_first"], 2)
        self.assertEqual(result.facts["only_second"], 2)
        self.assertEqual(result.facts["differing_columns"], ["loaded_at"])
        self.assertEqual(
            result.detail,
            "the builds differ: 2 rows in the first and 2 in the second, "
            "of which 2 only in the first and 2 only in the second; values differ in loaded_at",
        )

    def test_values_swapped_between_rows_fail_with_no_column_named(self):
        result = self.check("(1, 'a'), (2, 'b')", "(1, 'b'), (2, 'a')")
        self.assertEqual(result.status, FAIL)
        self.assertEqual(result.facts["differing_columns"], [])
        self.assertIn("combined differently across rows", result.detail)

    def test_a_column_of_another_type_fails_without_comparing_rows(self):
        # As a bag of rows the values would compare equal, since DuckDB casts
        # one side to the other's type, so the columns are compared first.
        result = self.check(
            "(1, 'a')", "(1, 'a')", ("id INTEGER, name VARCHAR", "id BIGINT, name VARCHAR")
        )
        self.assertEqual(result.status, FAIL)
        self.assertEqual(
            result.detail,
            "the two builds have different columns: "
            "id is INTEGER in the first build and BIGINT in the second",
        )

    def test_a_missing_column_fails(self):
        result = self.check("(1, 'a')", "(1)", ("id INTEGER, name VARCHAR", "id INTEGER"))
        self.assertEqual(result.status, FAIL)
        self.assertEqual(result.facts, {"column_changes": ["name only in the first build"]})

    def test_columns_in_another_order_fail(self):
        # Set operations match columns by position, so the order is part of
        # what must repeat.
        result = self.check(
            "(1, 'a')", "('a', 1)", ("id INTEGER, name VARCHAR", "name VARCHAR, id INTEGER")
        )
        self.assertEqual(result.status, FAIL)
        self.assertEqual(result.facts, {"column_changes": ["the columns are in a different order"]})

    def test_a_relation_the_second_build_lacks_fails(self):
        con = duckdb.connect()
        self.addCleanup(con.close)
        con.execute("CREATE TABLE t AS SELECT 1 AS id")
        result = check_repeat(con, "t", "t", "absent")
        self.assertEqual(result.status, FAIL)
        self.assertIn("could not compare the two builds", result.detail)


class ExpectedCheck(unittest.TestCase):
    """A model table m compared with a table of expected rows r, held as text."""

    COLUMNS = ("customer_id", "account_id")

    def setUp(self):
        self.con = duckdb.connect()
        self.addCleanup(self.con.close)

    def check(self, model_rows, expected_rows, shape="customer_id VARCHAR, account_id VARCHAR"):
        self.con.execute(f"CREATE TABLE m ({shape})")
        if model_rows:
            self.con.execute(f"INSERT INTO m VALUES {model_rows}")
        self.con.execute("CREATE TABLE r (customer_id VARCHAR, account_id VARCHAR)")
        if expected_rows:
            self.con.execute(f"INSERT INTO r VALUES {expected_rows}")
        model = ModelSpec("int_matches", ("customer_id",), expected=Expected(self.COLUMNS, "-"))
        return check_expected(self.con, model, "m", "r")

    def test_the_expected_rows_in_another_order_pass(self):
        result = self.check(
            "('c3', 'a3'), ('c1', 'a1'), ('c2', NULL)", "('c1', 'a1'), ('c2', NULL), ('c3', 'a3')"
        )
        self.assertEqual(result.status, PASS)
        self.assertEqual(
            result.detail, "customer_id, account_id: the 3 expected rows, and no other"
        )
        self.assertEqual(
            result.facts,
            {"expected_rows": 3, "model_rows": 3, "only_model": 0, "only_expected": 0},
        )

    def test_one_row(self):
        result = self.check("('c1', 'a1')", "('c1', 'a1')")
        self.assertEqual(result.detail, "customer_id, account_id: the 1 expected row, and no other")

    def test_values_are_compared_as_text_and_other_columns_are_ignored(self):
        # 12 as an integer reads as '12'. The model's extra column is not compared.
        result = self.check(
            "(12, 'a1', 'x'), (13, NULL, 'y')",
            "('12', 'a1'), ('13', NULL)",
            shape="CUSTOMER_ID INTEGER, Account_Id VARCHAR, note VARCHAR",
        )
        self.assertEqual(result.status, PASS)

    def test_a_wrong_match_is_shown_both_ways(self):
        # c2 should match a2 but was left unmatched: one row the model should
        # not have and one row it lacks.
        result = self.check(
            "('c1', 'a1'), ('c2', NULL), ('c3', 'a3')", "('c1', 'a1'), ('c2', 'a2'), ('c3', 'a3')"
        )
        self.assertEqual(result.status, FAIL)
        self.assertEqual(
            result.detail,
            "customer_id, account_id: 3 rows expected and 3 in the model, "
            "of which 1 not expected and 1 expected but missing\n"
            "not expected: (c2, null)\n"
            "missing: (c2, a2)",
        )
        self.assertEqual(
            result.facts,
            {"expected_rows": 3, "model_rows": 3, "only_model": 1, "only_expected": 1},
        )

    def test_a_row_the_model_repeats_fails(self):
        result = self.check("('c1', 'a1'), ('c1', 'a1')", "('c1', 'a1')")
        self.assertEqual(result.status, FAIL)
        self.assertEqual(
            result.facts,
            {"expected_rows": 1, "model_rows": 2, "only_model": 1, "only_expected": 0},
        )
        self.assertEqual(result.detail.splitlines()[1:], ["not expected: (c1, a1)"])

    def test_a_missing_row_fails(self):
        result = self.check("('c1', 'a1')", "('c1', 'a1'), ('c2', NULL)")
        self.assertEqual(
            result.facts,
            {"expected_rows": 2, "model_rows": 1, "only_model": 0, "only_expected": 1},
        )
        self.assertEqual(result.detail.splitlines()[1:], ["missing: (c2, null)"])

    def test_nulls_held_by_both_sides_are_equal(self):
        rows = "(NULL, NULL), ('c1', NULL), ('c1', NULL)"
        self.assertEqual(self.check(rows, rows).status, PASS)

    def test_an_identifier_that_lost_its_leading_zeros_fails(self):
        result = self.check(
            "(7, 'a1')", "('007', 'a1')", shape="customer_id INTEGER, account_id VARCHAR"
        )
        self.assertEqual(result.status, FAIL)
        self.assertEqual(
            result.detail.splitlines()[1:], ["not expected: (7, a1)", "missing: (007, a1)"]
        )

    def test_values_with_spaces_around_them_or_empty_are_quoted(self):
        result = self.check("(' c1', 'a1'), ('', 'a2')", "('c1', 'a1'), ('c2', 'a2')")
        self.assertEqual(
            result.detail.splitlines()[1:],
            ['not expected: ("", a2), (" c1", a1)', "missing: (c1, a1), (c2, a2)"],
        )

    def test_at_most_three_examples_are_shown_each_way_in_order(self):
        result = self.check(
            "('c5', 'x'), ('c4', 'x'), ('c3', 'x'), ('c2', 'x'), ('c1', NULL)",
            "('c5', 'a5'), ('c1', 'a1')",
        )
        self.assertEqual(
            result.facts,
            {"expected_rows": 2, "model_rows": 5, "only_model": 5, "only_expected": 2},
        )
        self.assertEqual(
            result.detail.splitlines()[1:],
            ["not expected: (c1, null), (c2, x), (c3, x)", "missing: (c1, a1), (c5, a5)"],
        )

    def test_an_empty_model_misses_every_expected_row(self):
        result = self.check("", "('c1', 'a1')")
        self.assertEqual(
            result.facts,
            {"expected_rows": 1, "model_rows": 0, "only_model": 0, "only_expected": 1},
        )

    def test_a_missing_column_fails(self):
        result = self.check("('c1')", "('c1', 'a1')", shape="customer_id VARCHAR")
        self.assertEqual(result.status, FAIL)
        self.assertEqual(result.detail, "column account_id is not in the model")
        self.assertEqual(result.facts, {"missing_columns": ["account_id"]})

    def test_a_model_that_cannot_be_read_fails(self):
        self.con.execute("CREATE TABLE r (customer_id VARCHAR, account_id VARCHAR)")
        model = ModelSpec("int_matches", ("customer_id",), expected=Expected(self.COLUMNS, "-"))
        result = check_expected(self.con, model, "absent", "r")
        self.assertEqual(result.status, FAIL)
        self.assertTrue(result.detail.startswith("could not read the model: "))


class ExpectedRows(unittest.TestCase):
    """Expected rows worked out from handmade raw files: two sources, a file at the
    top of the raw folder and one nested too deeply to be offered.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.raw = self.root / "raw"
        write(
            self.raw / "shop" / "orders.csv",
            "id,email\n001,A@x.example\n002, b@x.example\n003,c@x.example\n",
        )
        write(
            self.raw / "crm" / "accounts.jsonl",
            '{"account": 7, "email": "a@x.example"}\n{"account": 8, "email": "b@x.example"}\n',
        )
        write(self.raw / "regions.csv", "code\nEU\n")
        write(self.raw / "deep" / "er" / "table.csv", "x\n1\n")
        self.database = self.root / "expected.duckdb"

    def store(self, *models):
        store_expected(list(models), source_files(self.raw), self.database)
        con = duckdb.connect(str(self.database), read_only=True)
        self.addCleanup(con.close)
        return con

    def test_a_query_over_two_sources_is_kept_as_text(self):
        query = (
            "SELECT o.id AS order_id, a.account AS account_id FROM shop.orders o "
            "LEFT JOIN crm.accounts a ON lower(trim(o.email)) = a.email"
        )
        model = ModelSpec(
            "int_orders", ("order_id",), expected=Expected(("order_id", "account_id"), query)
        )
        con = self.store(model, ModelSpec("stg_orders", ("id",)))
        self.assertEqual(
            con.execute("SELECT * FROM int_orders ORDER BY order_id").fetchall(),
            [("001", "7"), ("002", "8"), ("003", None)],
        )
        self.assertEqual(
            [row[:2] for row in con.execute("DESCRIBE int_orders").fetchall()],
            [("order_id", "VARCHAR"), ("account_id", "VARCHAR")],
        )
        # Only models that declare expected rows get a table.
        self.assertEqual(con.execute("SHOW TABLES").fetchall(), [("int_orders",)])

    def test_a_file_at_the_top_of_the_raw_folder_is_read_by_its_name(self):
        model = ModelSpec("m", ("code",), expected=Expected(("code",), "SELECT code FROM regions"))
        con = self.store(model)
        self.assertEqual(con.execute("SELECT * FROM m").fetchall(), [("EU",)])

    def test_mistakes_in_the_query_stop_the_gate(self):
        mistakes = {
            "a declared column the query does not give": (
                "SELECT id FROM shop.orders",
                ("id", "account_id"),
            ),
            "a table that does not exist": ("SELECT id FROM shop.refunds", ("id",)),
            "a file nested more deeply": ("SELECT x FROM deep.er.table", ("x",)),
            "not SQL": ("SELEC id FROM shop.orders", ("id",)),
        }
        for label, (query, columns) in mistakes.items():
            with self.subTest(label):
                self.database.unlink(missing_ok=True)
                model = ModelSpec("int_orders", ("id",), expected=Expected(columns, query))
                with self.assertRaisesRegex(
                    GateError, "^int_orders: could not work out the expected rows: "
                ):
                    store_expected([model], source_files(self.raw), self.database)


class ExpectedEndToEnd(unittest.TestCase):
    """The expected check run by the gate on a small handmade dbt project."""

    def test_each_declared_model_is_compared_with_its_expected_rows(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        write(root / "raw" / "shop" / "orders.csv", "id,status\n001,paid\n002,void\n003,paid\n")
        project = root / "project"
        write(
            project / "dbt_project.yml", 'name: shop\nconfig-version: 2\nmodel-paths: ["models"]\n'
        )
        write(
            project / "models" / "sources.yml",
            "version: 2\nsources:\n  - name: shop\n    tables:\n      - name: orders\n"
            "        config:\n          external_location: \"read_csv('{{ var('raw_dir') }}"
            "/shop/orders.csv', all_varchar=true, header=true)\"\n",
        )
        # The right ids, the ids as numbers (leading zeros lost), and every
        # order where only paid ones are expected.
        write(
            project / "models" / "paid.sql",
            "select id from {{ source('shop', 'orders') }} where status = 'paid'",
        )
        write(
            project / "models" / "paid_as_numbers.sql",
            "select cast(id as integer) as id from {{ source('shop', 'orders') }} "
            "where status = 'paid'",
        )
        write(
            project / "models" / "paid_and_void.sql",
            "select id from {{ source('shop', 'orders') }}",
        )
        query = "SELECT id FROM shop.orders WHERE status = 'paid'"
        checks = declaration(
            root,
            {
                "models": [
                    {"name": name, "key": ["id"], "expected": {"columns": ["id"], "query": query}}
                    for name in ("paid", "paid_as_numbers", "paid_and_void", "absent")
                ]
            },
        )
        results = {
            (result.check, result.subject): result
            for result in run_gate(project, root / "raw", checks, root / "work")
        }
        self.assertTrue((root / "work" / "expected.duckdb").is_file())
        statuses = {
            subject: result.status
            for (check, subject), result in results.items()
            if check == "expected"
        }
        self.assertEqual(
            statuses, {"paid": PASS, "paid_as_numbers": FAIL, "paid_and_void": FAIL, "absent": SKIP}
        )
        self.assertEqual(
            results[("expected", "paid")].detail, "id: the 2 expected rows, and no other"
        )
        self.assertEqual(
            results[("expected", "paid_as_numbers")].detail.splitlines()[1:],
            ["not expected: (1), (3)", "missing: (001), (003)"],
        )
        self.assertEqual(
            results[("expected", "paid_and_void")].facts,
            {"expected_rows": 2, "model_rows": 3, "only_model": 1, "only_expected": 0},
        )


class Report(unittest.TestCase):
    def test_a_gate_passes_only_when_every_check_ran_and_passed(self):
        ok = Result("key", "m", PASS, "fine")
        self.assertTrue(passed([ok]))
        self.assertFalse(passed([]))
        self.assertFalse(passed([ok, Result("rows", "m", FAIL, "short")]))
        self.assertFalse(passed([ok, Result("rows", "m", SKIP, "not checked")]))

    def test_the_report_lists_every_result(self):
        text = render(
            [
                Result("build", "project", PASS, "dbt build exited 0"),
                Result("rows", "stg_orders", FAIL, "first line\nsecond line"),
                Result("key", "stg_orders", SKIP, "not checked"),
            ]
        )
        self.assertEqual(
            text,
            "gate: FAIL (1 passed, 1 failed, 1 not run)\n"
            "\n"
            "PASS  build  project     dbt build exited 0\n"
            "FAIL  rows   stg_orders  first line\n"
            "      second line\n"
            "SKIP  key    stg_orders  not checked\n",
        )

    def test_columns_are_as_wide_as_their_longest_entry(self):
        text = render(
            [
                Result("key", "m", PASS, "fine"),
                Result("compare", "model_b", FAIL, "differs"),
            ]
        )
        self.assertEqual(
            text,
            "gate: FAIL (1 passed, 1 failed, 0 not run)\n"
            "\n"
            "PASS  key      m        fine\n"
            "FAIL  compare  model_b  differs\n",
        )


class CannotRun(unittest.TestCase):
    """Problems found before dbt is started."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.project = self.root / "project"
        write(self.project / "dbt_project.yml", "name: shop\n")
        write(self.root / "raw" / "shop" / "orders.csv", ORDERS)
        self.checks = declaration(
            self.root, {"models": [{"name": "stg_orders", "key": ["id"], "source": "shop.orders"}]}
        )

    def test_a_folder_that_is_not_a_dbt_project(self):
        with self.assertRaises(GateError):
            run_gate(self.root / "raw", self.root / "raw", self.checks)

    def test_a_source_with_no_raw_file(self):
        checks = declaration(
            self.root, {"models": [{"name": "stg_orders", "key": ["id"], "source": "shop.missing"}]}
        )
        with self.assertRaises(GateError):
            run_gate(self.project, self.root / "raw", checks)

    def test_an_expected_query_that_cannot_run(self):
        checks = declaration(
            self.root,
            {
                "models": [
                    {
                        "name": "stg_orders",
                        "key": ["id"],
                        "expected": {"columns": ["id"], "query": "SELECT id FROM shop.refunds"},
                    }
                ]
            },
        )
        build = mock.patch("tenx.gate.build_project")
        with build as started, self.assertRaisesRegex(GateError, "could not work out"):
            run_gate(self.project, self.root / "raw", checks)
        started.assert_not_called()

    def test_a_raw_folder_that_does_not_exist(self):
        with self.assertRaises(GateError):
            run_gate(self.project, self.root / "absent", self.checks)

    def test_a_workdir_that_already_holds_files(self):
        with self.assertRaises(GateError):
            run_gate(self.project, self.root / "raw", self.checks, self.root / "raw")

    def test_dbt_not_being_installed(self):
        missing = mock.patch("tenx.gate.dbt_installed", return_value=False)
        with missing, self.assertRaisesRegex(GateError, "dbt is not installed"):
            run_gate(self.project, self.root / "raw", self.checks)

    def test_the_command_exits_2_and_explains(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr), redirect_stdout(io.StringIO()):
            code = main(
                [
                    str(self.project),
                    "--raw",
                    str(self.root / "raw"),
                    "--checks",
                    str(self.root / "absent.json"),
                ]
            )
        self.assertEqual(code, 2)
        self.assertIn("error: could not read", stderr.getvalue())


def replace_in(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if old not in text:
        raise AssertionError(f"{path.name} no longer contains {old!r}")
    path.write_text(text.replace(old, new), encoding="utf-8")


def currency_of(row: dict) -> str:
    """The currency of a raw invoice, as the totals check groups it: in capitals."""
    return row["currency"].strip().upper()


def company(identifier: str) -> int:
    """The demo company a billing customer or CRM account belongs to, from its ID.

    The generator writes the company's number into both IDs (``cus_000042A``,
    ``001000000000000042``), so this is the true match, worked out without
    looking at emails.
    """
    if identifier.startswith("cus_"):
        return int(identifier[4:10])
    return int(identifier[3:])


def bag_difference(one: list, other: list) -> int:
    """How many rows of one are not in other, counting every copy."""
    return sum((Counter(one) - Counter(other)).values())


class DemoProject(unittest.TestCase):
    """The gate run end to end with dbt, on generated demo data."""

    SEED = 11
    COMPANIES = 60

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.raw = cls.root / "data"
        generate(cls.SEED, cls.COMPANIES, cls.raw, cls.root / "answers")
        with (cls.raw / "billing" / "customers.csv").open(newline="", encoding="utf-8") as handle:
            cls.customers = list(csv.DictReader(handle))
        with (cls.raw / "billing" / "invoices.csv").open(newline="", encoding="utf-8") as handle:
            cls.invoices = list(csv.DictReader(handle))
        with (cls.raw / "crm" / "accounts.csv").open(newline="", encoding="utf-8") as handle:
            cls.accounts = list(csv.DictReader(handle))
        with (cls.raw / "events" / "events.jsonl").open(encoding="utf-8") as handle:
            cls.events = [line for line in handle if line.strip()]
        cls.models = [model.name for model in load_declaration(DECLARATION)]
        cls.matched = [model.name for model in load_declaration(DECLARATION) if model.expected]
        # Each CRM account by the company it belongs to.
        cls.account_of = {company(row["account_id"]): row["account_id"] for row in cls.accounts}
        cls.count = 0

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def broken_copy(self) -> Path:
        type(self).count += 1
        project = self.root / f"project_{self.count}"
        shutil.copytree(PROJECT, project)
        return project

    def run_on(self, project, workdir=None):
        results = run_gate(project, self.raw, DECLARATION, workdir)
        return {(result.check, result.subject): result for result in results}

    def assert_only_failures(self, results, expected):
        """Every check passes except the named ones, which have the named status."""
        actual = {key: result.status for key, result in results.items() if result.status != PASS}
        self.assertEqual(actual, expected)
        self.assertEqual(
            set(results),
            {("build", "project"), ("repeat", "project"), ("total", "stg_billing__invoices")}
            | {("expected", model) for model in self.matched}
            | {
                (check, model)
                for check in ("build", "rows", "key", "repeat")
                for model in self.models
            },
        )

    def test_the_correct_project_passes_with_the_recounted_figures(self):
        workdir = self.root / "kept"
        results = self.run_on(PROJECT, workdir)
        self.assert_only_failures(results, {})
        self.assertTrue(passed(list(results.values())))

        test_customers = sum(1 for row in self.customers if row["livemode"] == "false")
        test_invoices = sum(1 for row in self.invoices if row["livemode"] == "false")
        repeats = len(self.events) - len(set(self.events))
        for count in (test_customers, test_invoices, repeats):
            self.assertGreater(count, 0)
        expected = {
            "stg_billing__customers": (len(self.customers), {"test_mode": test_customers}),
            "stg_billing__invoices": (len(self.invoices), {"test_mode": test_invoices}),
            "stg_crm__accounts": (len(self.accounts), {}),
            "stg_events__events": (len(self.events), {"repeat_deliveries": repeats}),
            "int_customers_matched_to_accounts": (
                len(self.customers),
                {"test_mode": test_customers},
            ),
        }
        for model, (source_rows, excluded) in expected.items():
            with self.subTest(model):
                kept = source_rows - sum(excluded.values())
                self.assertEqual(
                    results[("rows", model)].facts,
                    {
                        "source_rows": source_rows,
                        "excluded": excluded,
                        "expected_rows": kept,
                        "model_rows": kept,
                    },
                )
                self.assertEqual(
                    results[("key", model)].facts,
                    {"rows": kept, "null_rows": 0, "repeated_keys": 0, "surplus_rows": 0},
                )
                self.assertEqual(
                    results[("repeat", model)].facts,
                    {
                        "rows": kept,
                        "second_rows": kept,
                        "only_first": 0,
                        "only_second": 0,
                        "differing_columns": [],
                    },
                )

        # Money by currency, recounted from the file in whole minor units.
        raw, test_mode = {}, {}
        for row in self.invoices:
            amount, currency = int(row["amount_minor"]), currency_of(row)
            raw[currency] = raw.get(currency, 0) + amount
            if row["livemode"] == "false":
                test_mode[currency] = test_mode.get(currency, 0) + amount
        live = {currency: raw[currency] - test_mode.get(currency, 0) for currency in raw}
        self.assertIn("JPY", live)
        self.assertGreater(len(test_mode), 0)
        facts = results[("total", "stg_billing__invoices")].facts
        self.assertEqual(facts["source"], raw)
        self.assertEqual(facts["excluded"], {"test_mode": test_mode})
        self.assertEqual(facts["expected"], live)
        self.assertEqual(facts["model"], live)
        # Every demo invoice has an amount: int() above would fail on a blank.
        self.assertEqual(facts["blank"], 0)

        # Each live customer is matched to its own company's account, and the
        # customers whose company is not in the CRM have none. Matching emails
        # without regard to case and surrounding spaces finds the same pairs.
        truth = self.true_matches()
        self.assertTrue(any(account is None for _, account in truth))
        self.assertTrue(any(account is not None for _, account in truth))
        by_email = {
            row["billing_email"].strip().lower(): row["account_id"] for row in self.accounts
        }
        self.assertEqual(
            truth,
            [
                (row["id"], by_email.get(row["email"].strip().lower()))
                for row in self.customers
                if row["livemode"] == "true"
            ],
        )
        self.assertEqual(
            results[("expected", "int_customers_matched_to_accounts")].facts,
            {
                "expected_rows": len(truth),
                "model_rows": len(truth),
                "only_model": 0,
                "only_expected": 0,
            },
        )

        # The build went into the workdir, and the project folder is untouched.
        self.assertTrue((workdir / "build.duckdb").is_file())
        self.assertTrue((workdir / "target" / "run_results.json").is_file())
        self.assertTrue((workdir / "repeat" / "repeat.duckdb").is_file())
        self.assertTrue((workdir / "repeat" / "target" / "run_results.json").is_file())
        self.assertFalse((PROJECT / "target").exists())
        self.assertFalse((PROJECT / "logs").exists())

    def test_the_command_reports_a_pass_and_exits_0(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = main([str(PROJECT), "--raw", str(self.raw), "--checks", str(DECLARATION)])
        self.assertEqual(code, 0)
        self.assertTrue(stdout.getvalue().startswith("gate: PASS (24 passed, 0 failed, 0 not run)"))

    def test_lost_and_surplus_rows_fail_the_rows_check(self):
        project = self.broken_copy()
        staging = project / "models" / "staging"
        # One account dropped.
        (staging / "stg_crm__accounts.sql").write_text(
            (staging / "stg_crm__accounts.sql").read_text(encoding="utf-8")
            + "where account_id <> (select min(account_id) from {{ source('crm', 'accounts') }})\n",
            encoding="utf-8",
        )
        # Refunds, which are negative invoices, filtered out without being declared.
        replace_in(
            staging / "stg_billing__invoices.sql",
            "where livemode = 'true'",
            "where livemode = 'true' and cast(amount_minor as bigint) > 0",
        )
        # Test-mode customers kept although their exclusion is declared.
        replace_in(staging / "stg_billing__customers.sql", "where livemode = 'true'", "")

        results = self.run_on(project)
        self.assert_only_failures(
            results,
            {
                ("rows", "stg_crm__accounts"): FAIL,
                ("rows", "stg_billing__invoices"): FAIL,
                ("rows", "stg_billing__customers"): FAIL,
                # Dropping refunds also leaves too much money.
                ("total", "stg_billing__invoices"): FAIL,
                # The crosswalk is built from the broken customers and accounts.
                ("rows", "int_customers_matched_to_accounts"): FAIL,
                ("expected", "int_customers_matched_to_accounts"): FAIL,
            },
        )
        live_invoices = [row for row in self.invoices if row["livemode"] == "true"]
        refunds = sum(1 for row in live_invoices if int(row["amount_minor"]) <= 0)
        self.assertGreater(refunds, 0)
        self.assertEqual(
            results[("rows", "stg_crm__accounts")].facts["model_rows"], len(self.accounts) - 1
        )
        self.assertEqual(
            results[("rows", "stg_billing__invoices")].facts["model_rows"],
            len(live_invoices) - refunds,
        )
        refunded = {}
        for row in live_invoices:
            if int(row["amount_minor"]) <= 0:
                currency = currency_of(row)
                refunded[currency] = refunded.get(currency, 0) + int(row["amount_minor"])
        facts = results[("total", "stg_billing__invoices")].facts
        self.assertEqual(
            {
                currency: facts["model"].get(currency, 0) - facts["expected"][currency]
                for currency in facts["expected"]
                if facts["model"].get(currency, 0) != facts["expected"][currency]
            },
            {currency: -amount for currency, amount in refunded.items() if amount},
        )
        self.assertEqual(
            results[("rows", "stg_billing__customers")].facts["model_rows"], len(self.customers)
        )
        # The crosswalk is built from both models: it holds the test-mode
        # customers, with no account, and the dropped account's customers lose
        # their match.
        dropped = min(row["account_id"] for row in self.accounts)
        truth = self.true_matches()
        model = [
            (
                row["id"],
                None if row["livemode"] == "false" else self.account_of.get(company(row["id"])),
            )
            for row in self.customers
        ]
        model = [(customer, None if account == dropped else account) for customer, account in model]
        self.assertEqual(
            results[("rows", "int_customers_matched_to_accounts")].facts["model_rows"],
            len(self.customers),
        )
        facts = results[("expected", "int_customers_matched_to_accounts")].facts
        self.assertEqual(
            (facts["only_model"], facts["only_expected"]),
            (bag_difference(model, truth), bag_difference(truth, model)),
        )
        self.assertGreater(facts["only_model"], 0)

    def true_matches(self) -> list[tuple[str, str | None]]:
        """Each live billing customer with its company's CRM account, if it has one."""
        return [
            (row["id"], self.account_of.get(company(row["id"])))
            for row in self.customers
            if row["livemode"] == "true"
        ]

    def live_money(self, change=lambda row, amount: amount):
        """Live invoice money by currency, after changing each amount as a model might."""
        totals = {}
        for row in self.invoices:
            if row["livemode"] == "true":
                amount = change(row, int(row["amount_minor"]))
                totals[currency_of(row)] = totals.get(currency_of(row), 0) + amount
        return totals

    def assert_money_fails(self, project, model_money):
        """Only the totals check fails, and the model holds the money worked out here."""
        results = self.run_on(project)
        self.assert_only_failures(results, {("total", "stg_billing__invoices"): FAIL})
        result = results[("total", "stg_billing__invoices")]
        self.assertEqual(result.facts["expected"], self.live_money())
        self.assertEqual(
            {currency: amount for currency, amount in result.facts["model"].items() if amount},
            {currency: amount for currency, amount in model_money.items() if amount},
        )
        return result

    def test_a_row_swapped_for_an_excluded_one_fails_the_totals_check_alone(self):
        # The right number of rows and unique keys, but the wrong money: the
        # largest live invoice is dropped and the smallest test-mode one kept.
        by_amount = sorted(self.invoices, key=lambda row: (int(row["amount_minor"]), row["id"]))
        dropped = [row for row in by_amount if row["livemode"] == "true"][-1]
        added = next(row for row in by_amount if row["livemode"] == "false")
        self.assertNotEqual(int(dropped["amount_minor"]), int(added["amount_minor"]))
        project = self.broken_copy()
        replace_in(
            project / "models" / "staging" / "stg_billing__invoices.sql",
            "where livemode = 'true'",
            f"where (livemode = 'true' and id <> '{dropped['id']}') or id = '{added['id']}'",
        )
        expected = self.live_money()
        expected[currency_of(dropped)] -= int(dropped["amount_minor"])
        expected[currency_of(added)] = expected.get(currency_of(added), 0) + int(
            added["amount_minor"]
        )
        self.assert_money_fails(project, expected)

    def test_yen_converted_as_if_it_had_cents_fails_the_totals_check_alone(self):
        project = self.broken_copy()
        replace_in(
            project / "models" / "staging" / "stg_billing__invoices.sql",
            "cast(amount_minor as bigint) as amount_minor",
            "case when currency = 'jpy' then cast(amount_minor as bigint) * 100"
            " else cast(amount_minor as bigint) end as amount_minor",
        )
        model_money = self.live_money(
            lambda row, amount: amount * 100 if row["currency"] == "jpy" else amount
        )
        self.assertNotEqual(model_money, self.live_money())
        result = self.assert_money_fails(project, model_money)
        self.assertIn("1 of", result.detail)
        self.assertIn("\nJPY: ", result.detail)

    def test_refunds_made_positive_fail_the_totals_check_alone(self):
        project = self.broken_copy()
        replace_in(
            project / "models" / "staging" / "stg_billing__invoices.sql",
            "cast(amount_minor as bigint) as amount_minor",
            "abs(cast(amount_minor as bigint)) as amount_minor",
        )
        model_money = self.live_money(lambda row, amount: abs(amount))
        self.assertNotEqual(model_money, self.live_money())
        self.assert_money_fails(project, model_money)

    def test_money_held_as_floating_point_fails_the_totals_check_alone(self):
        project = self.broken_copy()
        replace_in(
            project / "models" / "staging" / "stg_billing__invoices.sql",
            "cast(amount_minor as bigint) as amount_minor",
            "cast(amount_minor as double) as amount_minor",
        )
        results = self.run_on(project)
        self.assert_only_failures(results, {("total", "stg_billing__invoices"): FAIL})
        self.assertIn("floating point (DOUBLE)", results[("total", "stg_billing__invoices")].detail)

    def test_a_project_that_writes_currency_in_capitals_passes(self):
        # A correct project written differently from the reference: the raw
        # file holds `usd`, the model `USD`.
        project = self.broken_copy()
        replace_in(
            project / "models" / "staging" / "stg_billing__invoices.sql",
            "    currency,\n",
            "    upper(currency) as currency,\n",
        )
        self.assertTrue(any(row["currency"] != currency_of(row) for row in self.invoices))
        results = self.run_on(project)
        self.assert_only_failures(results, {})
        facts = results[("total", "stg_billing__invoices")].facts
        self.assertEqual(facts["expected"], self.live_money())
        self.assertEqual(facts["model"], self.live_money())

    def test_money_moved_to_another_currency_fails_the_totals_check_alone(self):
        # Every amount is kept and the grand total is right, but the yen is
        # labelled as another currency.
        live = self.live_money()
        other = min(currency for currency in live if currency != "JPY")
        self.assertNotEqual(live["JPY"], 0)
        project = self.broken_copy()
        replace_in(
            project / "models" / "staging" / "stg_billing__invoices.sql",
            "    currency,\n",
            f"    case when currency = 'jpy' then '{other.lower()}' else currency end as currency,\n",
        )
        moved = dict(live)
        moved[other] += moved.pop("JPY")
        result = self.assert_money_fails(project, moved)
        self.assertIn("2 of", result.detail)
        self.assertIn("\nJPY: ", result.detail)
        self.assertIn(f"\n{other}: ", result.detail)

    def write_crosswalk(self, project: Path, sql: str) -> None:
        write(project / "models" / "intermediate" / "int_customers_matched_to_accounts.sql", sql)

    def test_matching_emails_exactly_fails_the_expected_check_alone(self):
        project = self.broken_copy()
        self.write_crosswalk(
            project,
            "select customers.id as customer_id, accounts.account_id\n"
            "from {{ source('billing', 'customers') }} as customers\n"
            "left join {{ source('crm', 'accounts') }} as accounts\n"
            "    on customers.email = accounts.billing_email\n"
            "where customers.livemode = 'true'\n",
        )
        results = self.run_on(project)
        self.assert_only_failures(
            results, {("expected", "int_customers_matched_to_accounts"): FAIL}
        )
        exact = {row["billing_email"]: row["account_id"] for row in self.accounts}
        model = [
            (row["id"], exact.get(row["email"]))
            for row in self.customers
            if row["livemode"] == "true"
        ]
        truth = self.true_matches()
        missed = bag_difference(truth, model)
        self.assertGreater(missed, 0)
        result = results[("expected", "int_customers_matched_to_accounts")]
        self.assertEqual(
            result.facts,
            {
                "expected_rows": len(truth),
                "model_rows": len(truth),
                "only_model": missed,
                "only_expected": missed,
            },
        )
        self.assertRegex(result.detail, r"\nnot expected: \(cus_\w+, null\)")
        self.assertRegex(result.detail, r"\nmissing: \(cus_\w+, 001\d{15}\)")

    def test_leaving_out_customers_with_no_account_fails_the_rows_and_expected_checks(self):
        project = self.broken_copy()
        replace_in(
            project / "models" / "intermediate" / "int_customers_matched_to_accounts.sql",
            "left join",
            "inner join",
        )
        results = self.run_on(project)
        self.assert_only_failures(
            results,
            {
                ("rows", "int_customers_matched_to_accounts"): FAIL,
                ("expected", "int_customers_matched_to_accounts"): FAIL,
            },
        )
        truth = self.true_matches()
        unmatched = sum(1 for _, account in truth if account is None)
        self.assertGreater(unmatched, 0)
        self.assertEqual(
            results[("rows", "int_customers_matched_to_accounts")].facts["model_rows"],
            len(truth) - unmatched,
        )
        self.assertEqual(
            results[("expected", "int_customers_matched_to_accounts")].facts,
            {
                "expected_rows": len(truth),
                "model_rows": len(truth) - unmatched,
                "only_model": 0,
                "only_expected": unmatched,
            },
        )

    def test_a_crosswalk_written_another_way_passes(self):
        # Built from the raw sources as a view, with its own normalisation,
        # column names in capitals and an extra column.
        project = self.broken_copy()
        self.write_crosswalk(
            project,
            "{{ config(materialized='view') }}\n"
            "select\n"
            "    customers.id as CUSTOMER_ID,\n"
            "    accounts.account_id as Account_Id,\n"
            "    case when accounts.account_id is null then 'none' else 'email' end as match_method\n"
            "from {{ source('billing', 'customers') }} as customers\n"
            "left join {{ source('crm', 'accounts') }} as accounts\n"
            "    on upper(trim(accounts.billing_email)) = upper(trim(customers.email))\n"
            "where customers.livemode <> 'false'\n",
        )
        results = self.run_on(project)
        self.assert_only_failures(results, {})

    def test_a_random_identifier_fails_the_repeat_check_alone(self):
        # Every row gets a new identifier on each build: the right rows, keys
        # and money, but not the same tables twice.
        project = self.broken_copy()
        replace_in(
            project / "models" / "staging" / "stg_crm__accounts.sql",
            "    account_id,\n",
            "    account_id,\n    uuid() as row_uid,\n",
        )
        results = self.run_on(project)
        self.assert_only_failures(results, {("repeat", "stg_crm__accounts"): FAIL})
        rows = len(self.accounts)
        self.assertEqual(
            results[("repeat", "stg_crm__accounts")].facts,
            {
                "rows": rows,
                "second_rows": rows,
                "only_first": rows,
                "only_second": rows,
                "differing_columns": ["row_uid"],
            },
        )

    def test_a_load_time_fails_the_repeat_check_alone(self):
        project = self.broken_copy()
        replace_in(
            project / "models" / "staging" / "stg_billing__invoices.sql",
            "    status,\n",
            "    status,\n    current_timestamp as loaded_at,\n",
        )
        results = self.run_on(project)
        self.assert_only_failures(results, {("repeat", "stg_billing__invoices"): FAIL})
        result = results[("repeat", "stg_billing__invoices")]
        live = sum(1 for row in self.invoices if row["livemode"] == "true")
        self.assertEqual(result.facts["only_first"], live)
        self.assertEqual(result.facts["differing_columns"], ["loaded_at"])
        self.assertIn("values differ in loaded_at", result.detail)

    def test_views_and_an_undeclared_model_are_compared(self):
        # A correct project written differently from the reference: models
        # are views, and a mart reads a staging view through ref(). The mart
        # is not declared but is compared all the same.
        project = self.broken_copy()
        replace_in(project / "dbt_project.yml", "+materialized: table", "+materialized: view")
        write(
            project / "models" / "marts" / "invoices_by_currency.sql",
            "select upper(currency) as currency, sum(amount_minor) as amount_minor,"
            " count(*) as invoices\nfrom {{ ref('stg_billing__invoices') }}\ngroup by 1\n",
        )
        results = self.run_on(project)
        mart = results.pop(("repeat", "invoices_by_currency"))
        self.assert_only_failures(results, {})
        self.assertEqual(mart.status, PASS)
        self.assertEqual(mart.facts["rows"], len(self.live_money()))

    def test_an_undeclared_model_that_does_not_repeat_fails(self):
        project = self.broken_copy()
        write(
            project / "models" / "marts" / "invoice_snapshot.sql",
            "select upper(currency) as currency, count(*) as invoices,"
            " current_timestamp as taken_at\nfrom {{ ref('stg_billing__invoices') }}\ngroup by 1\n",
        )
        results = self.run_on(project)
        mart = results.pop(("repeat", "invoice_snapshot"))
        self.assert_only_failures(results, {})
        self.assertEqual(mart.status, FAIL)
        currencies = len(self.live_money())
        self.assertEqual(mart.facts["only_first"], currencies)
        self.assertEqual(mart.facts["only_second"], currencies)
        self.assertEqual(mart.facts["differing_columns"], ["taken_at"])

    def test_a_repeated_key_and_a_null_key_fail_the_key_checks(self):
        project = self.broken_copy()
        staging = project / "models" / "staging"
        # The project's own key tests are removed, as a careless fix would.
        (staging / "staging.yml").unlink()
        (project / "models" / "intermediate" / "intermediate.yml").unlink()
        # The last account is given the first account's identifier.
        replace_in(
            staging / "stg_crm__accounts.sql",
            "    account_id,",
            "    case when account_id = (select max(account_id) from {{ source('crm', 'accounts') }})"
            " then (select min(account_id) from {{ source('crm', 'accounts') }})"
            " else account_id end as account_id,",
        )
        # The first live customer loses its identifier.
        replace_in(
            staging / "stg_billing__customers.sql",
            "    id as customer_id,",
            "    nullif(id, (select min(id) from {{ source('billing', 'customers') }}"
            " where livemode = 'true')) as customer_id,",
        )

        results = self.run_on(project)
        self.assert_only_failures(
            results,
            {
                ("key", "stg_crm__accounts"): FAIL,
                ("key", "stg_billing__customers"): FAIL,
                # The crosswalk carries both mistakes.
                ("key", "int_customers_matched_to_accounts"): FAIL,
                ("expected", "int_customers_matched_to_accounts"): FAIL,
            },
        )
        accounts = results[("key", "stg_crm__accounts")]
        self.assertEqual(
            accounts.facts,
            {"rows": len(self.accounts), "null_rows": 0, "repeated_keys": 1, "surplus_rows": 1},
        )
        self.assertIn(min(row["account_id"] for row in self.accounts), accounts.detail)
        live_customers = sum(1 for row in self.customers if row["livemode"] == "true")
        self.assertEqual(
            results[("key", "stg_billing__customers")].facts,
            {"rows": live_customers, "null_rows": 1, "repeated_keys": 0, "surplus_rows": 0},
        )
        self.assertEqual(
            results[("key", "int_customers_matched_to_accounts")].facts,
            {"rows": live_customers, "null_rows": 1, "repeated_keys": 0, "surplus_rows": 0},
        )
        first, last = min(self.account_of.values()), max(self.account_of.values())
        nulled = min(row["id"] for row in self.customers if row["livemode"] == "true")
        truth = self.true_matches()
        model = [
            (None if customer == nulled else customer, first if account == last else account)
            for customer, account in truth
        ]
        facts = results[("expected", "int_customers_matched_to_accounts")].facts
        self.assertEqual(
            (facts["only_model"], facts["only_expected"]),
            (bag_difference(model, truth), bag_difference(truth, model)),
        )

    def test_a_model_that_does_not_run_fails_the_build_and_is_not_checked(self):
        project = self.broken_copy()
        replace_in(
            project / "models" / "staging" / "stg_crm__accounts.sql", "segment,", "no_such_column,"
        )
        results = self.run_on(project)
        self.assert_only_failures(
            results,
            {
                ("build", "project"): FAIL,
                ("build", "stg_crm__accounts"): FAIL,
                ("rows", "stg_crm__accounts"): SKIP,
                ("key", "stg_crm__accounts"): SKIP,
                # The second build fails the same way; the other models repeat.
                ("repeat", "project"): FAIL,
                ("repeat", "stg_crm__accounts"): SKIP,
                # dbt skips the model built from the one that failed.
                ("build", "int_customers_matched_to_accounts"): FAIL,
                ("rows", "int_customers_matched_to_accounts"): SKIP,
                ("key", "int_customers_matched_to_accounts"): SKIP,
                ("expected", "int_customers_matched_to_accounts"): SKIP,
                ("repeat", "int_customers_matched_to_accounts"): SKIP,
            },
        )
        self.assertIn("model.demo.stg_crm__accounts: error", results[("build", "project")].detail)
        self.assertIn("no_such_column", results[("build", "stg_crm__accounts")].detail)
        self.assertTrue(
            results[("build", "int_customers_matched_to_accounts")].detail.startswith("skipped")
        )

    def test_a_model_left_out_of_a_green_build_fails(self):
        project = self.broken_copy()
        staging = project / "models" / "staging"
        (staging / "staging.yml").unlink()
        (staging / "stg_events__events.sql").unlink()
        results = self.run_on(project)
        self.assert_only_failures(
            results,
            {
                ("build", "stg_events__events"): FAIL,
                ("rows", "stg_events__events"): SKIP,
                ("key", "stg_events__events"): SKIP,
                ("repeat", "stg_events__events"): SKIP,
            },
        )

    def test_a_failing_test_inside_the_project_fails_the_build(self):
        project = self.broken_copy()
        # Repeat deliveries are no longer removed, so the project's own
        # uniqueness test on event_id fails.
        replace_in(
            project / "models" / "staging" / "stg_events__events.sql", "select distinct", "select"
        )
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = main([str(project), "--raw", str(self.raw), "--checks", str(DECLARATION)])
        self.assertEqual(code, 1)
        report = stdout.getvalue()
        self.assertTrue(report.startswith("gate: FAIL (20 passed, 4 failed, 0 not run)"), report)
        self.assertRegex(report, r"FAIL  build     project +dbt build exited 1, 1 of 15 nodes")
        self.assertRegex(report, r"FAIL  repeat    project +second build: dbt build exited 1")
        self.assertRegex(report, r"test\.demo\.unique_stg_events__events_event_id\.\w+: fail")
        self.assertRegex(report, r"FAIL  rows      stg_events__events")
        self.assertRegex(report, r"FAIL  key       stg_events__events")
        self.assertRegex(report, r"PASS  build     stg_events__events")
        self.assertRegex(report, r"PASS  repeat    stg_events__events")

    def test_a_project_dbt_cannot_parse_fails_without_results(self):
        project = self.broken_copy()
        write(project / "models" / "staging" / "orphan.sql", "select * from {{ ref('nowhere') }}\n")
        results = self.run_on(project)
        failures = {key: result.status for key, result in results.items() if result.status != PASS}
        self.assertEqual(
            failures,
            {
                ("build", "project"): FAIL,
                ("repeat", "project"): SKIP,
                ("total", "stg_billing__invoices"): SKIP,
                ("expected", "int_customers_matched_to_accounts"): SKIP,
            }
            | {("build", model): FAIL for model in self.models}
            | {
                (check, model): SKIP for check in ("rows", "key", "repeat") for model in self.models
            },
        )
        self.assertIn("nowhere", results[("build", "project")].detail)


if __name__ == "__main__":
    unittest.main()
