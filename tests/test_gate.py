"""Tests for the gate.

Expected figures are worked out by hand for the handmade tables and recounted
with the csv and json modules for the demo data, never taken from the gate's
own SQL. The gate is run against a correct dbt project, which must pass, and
against copies of it that are each broken in one known way.
"""

import csv
import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import duckdb

from demo.generate import generate
from tenx.gate import (
    FAIL,
    PASS,
    SKIP,
    Exclusion,
    GateError,
    ModelSpec,
    Result,
    Total,
    check_build,
    check_key,
    check_rows,
    expected_rows,
    load_declaration,
    main,
    passed,
    render,
    run_gate,
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
                    {"name": "orders", "key": ["order_id"]},
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
                ModelSpec(name="orders", key=("order_id",)),
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
            {("build", "project")}
            | {(check, model) for check in ("build", "rows", "key") for model in self.models},
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

        # The build went into the workdir, and the project folder is untouched.
        self.assertTrue((workdir / "build.duckdb").is_file())
        self.assertTrue((workdir / "target" / "run_results.json").is_file())
        self.assertFalse((PROJECT / "target").exists())
        self.assertFalse((PROJECT / "logs").exists())

    def test_the_command_reports_a_pass_and_exits_0(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = main([str(PROJECT), "--raw", str(self.raw), "--checks", str(DECLARATION)])
        self.assertEqual(code, 0)
        self.assertTrue(stdout.getvalue().startswith("gate: PASS (13 passed, 0 failed, 0 not run)"))

    def test_lost_and_surplus_rows_fail_the_rows_check_alone(self):
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
        self.assertEqual(
            results[("rows", "stg_billing__customers")].facts["model_rows"], len(self.customers)
        )

    def test_a_repeated_key_and_a_null_key_fail_the_key_check_alone(self):
        project = self.broken_copy()
        staging = project / "models" / "staging"
        # The project's own key tests are removed, as a careless fix would.
        (staging / "staging.yml").unlink()
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
            {("key", "stg_crm__accounts"): FAIL, ("key", "stg_billing__customers"): FAIL},
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
            },
        )
        self.assertIn("model.demo.stg_crm__accounts: error", results[("build", "project")].detail)
        self.assertIn("no_such_column", results[("build", "stg_crm__accounts")].detail)

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
        self.assertTrue(report.startswith("gate: FAIL (10 passed, 3 failed, 0 not run)"), report)
        self.assertRegex(report, r"FAIL  build  project +dbt build exited 1, 1 of 12 nodes")
        self.assertRegex(report, r"test\.demo\.unique_stg_events__events_event_id\.\w+: fail")
        self.assertRegex(report, r"FAIL  rows   stg_events__events")
        self.assertRegex(report, r"FAIL  key    stg_events__events")
        self.assertRegex(report, r"PASS  build  stg_events__events")

    def test_a_project_dbt_cannot_parse_fails_without_results(self):
        project = self.broken_copy()
        write(project / "models" / "staging" / "orphan.sql", "select * from {{ ref('nowhere') }}\n")
        results = self.run_on(project)
        failures = {key: result.status for key, result in results.items() if result.status != PASS}
        self.assertEqual(
            failures,
            {("build", "project"): FAIL}
            | {("build", model): FAIL for model in self.models}
            | {(check, model): SKIP for check in ("rows", "key") for model in self.models},
        )
        self.assertIn("nowhere", results[("build", "project")].detail)


if __name__ == "__main__":
    unittest.main()
