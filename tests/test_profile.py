"""Tests for the source profiler.

Expected figures are worked out by hand or recounted with the csv and json
modules, never by calling the profiler's own SQL.
"""

import csv
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import duckdb

from demo.generate import generate
from tenx.profile import ProfileError, main, profile_folder, render

HANDMADE = """id,code,email,amount,price,active,joined,seen,note
1,007,a@x.example,10,1.50,true,2026-01-05,03/02/2026,hello
2,012,A@X.example ,-3,2.25,false,2026-02-11,04/02/2026,
3,100,b@x.example,7,0.10,true,2026-03-01,05/02/2026,n/a
4,101, b@x.example,7,3.00,TRUE,2026-03-01,06/02/2026,"  "
5,102,c@x.example,0,12,false,2025-12-31,07/02/2026,hello
"""


def write(root: Path, name: str, text: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class HandmadeFile(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        write(root / "raw", "shop/people.csv", HANDMADE)
        cls.out = root / "profiles"
        (cls.profile,) = profile_folder(root / "raw", cls.out)
        cls.columns = {column["name"]: column for column in cls.profile["columns"]}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_table_is_named_after_its_folder_and_file(self):
        self.assertEqual(self.profile["table"], "shop.people")
        self.assertEqual(self.profile["file"], "shop/people.csv")

    def test_row_counts(self):
        self.assertEqual(self.profile["rows"], 5)
        self.assertEqual(self.profile["file_records"], 5)
        self.assertEqual(self.profile["duplicate_rows"], 0)
        self.assertEqual(self.profile["notes"], [])

    def test_only_fully_unique_columns_are_key_candidates(self):
        self.assertEqual(self.profile["key_candidates"], ["id", "code", "email", "price", "seen"])

    def test_types_a_column_safely_converts_to(self):
        expected = {
            "id": "integer",
            "code": "text",
            "email": "text",
            "amount": "integer",
            "price": "decimal",
            "active": "boolean",
            "joined": "date",
            "seen": "date",
            "note": "text",
        }
        self.assertEqual({n: c["converts_to"] for n, c in self.columns.items()}, expected)

    def test_leading_zeros_keep_a_column_as_text(self):
        (note,) = self.columns["code"]["notes"]
        self.assertIn("2 with leading zeros", note)
        self.assertNotIn("min", self.columns["code"])

    def test_numeric_ranges(self):
        self.assertEqual(
            (self.columns["amount"]["min"], self.columns["amount"]["max"]), ("-3", "10")
        )
        self.assertEqual(
            (self.columns["price"]["min"], self.columns["price"]["max"]), ("0.1", "12")
        )

    def test_date_range(self):
        joined = self.columns["joined"]
        self.assertEqual((joined["min"], joined["max"]), ("2025-12-31", "2026-03-01"))

    def test_ambiguous_dates_are_called_out(self):
        (note,) = self.columns["seen"]["notes"]
        self.assertIn("ambiguous", note)
        self.assertIn("day/month/year", note)
        self.assertIn("month/day/year", note)

    def test_nulls_and_blanks_are_counted_separately(self):
        note = self.columns["note"]
        self.assertEqual(note["nulls"], 1)
        self.assertEqual(note["null_rate"], 0.2)
        self.assertEqual(note["blanks"], 1)
        self.assertEqual(note["distinct"], 3)
        self.assertEqual(note["placeholders"], {"n/a": 1})
        self.assertEqual(note["top_values"][0], {"value": "hello", "count": 2})
        self.assertIn("1 value with leading or trailing spaces", note["notes"])

    def test_padding_and_case_differences(self):
        email = self.columns["email"]
        self.assertEqual(email["distinct"], 5)
        self.assertIn("2 values with leading or trailing spaces", email["notes"])
        self.assertIn("5 distinct values become 3 when trimmed and lower-cased", email["notes"])

    def test_text_and_json_are_written_and_agree(self):
        saved = json.loads((self.out / "shop.people.json").read_text(encoding="utf-8"))
        self.assertEqual(saved, self.profile)
        text = (self.out / "shop.people.txt").read_text(encoding="utf-8")
        self.assertEqual(text, render(self.profile))
        self.assertIn("key candidates: id, code, email, price, seen", text)

    def test_profiles_inside_the_source_folder_are_not_profiled(self):
        root = Path(self.tmp.name) / "raw"
        first = profile_folder(root, root / "profiles")
        second = profile_folder(root, root / "profiles")
        self.assertEqual([p["table"] for p in second], ["shop.people"])
        self.assertEqual(first, second)


class OtherInputs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "raw"
        self.out = Path(self.tmp.name) / "out"
        self.root.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_parquet(self):
        target = self.root / "orders.parquet"
        duckdb.sql(
            "COPY (SELECT * FROM (VALUES (1, 'a', DATE '2026-01-02'), (2, 'a', DATE '2026-01-09'),"
            " (3, NULL, DATE '2026-01-09')) AS t(order_id, code, placed))"
            f" TO '{target}' (FORMAT parquet)"
        )
        (profile,) = profile_folder(self.root, self.out)
        columns = {column["name"]: column for column in profile["columns"]}
        self.assertEqual(profile["format"], "parquet")
        self.assertEqual(profile["rows"], 3)
        self.assertIsNone(profile["file_records"])
        self.assertEqual(profile["key_candidates"], ["order_id"])
        self.assertEqual(columns["code"]["nulls"], 1)
        self.assertEqual((columns["order_id"]["min"], columns["order_id"]["max"]), ("1", "3"))
        self.assertEqual(
            (columns["placed"]["min"], columns["placed"]["max"]), ("2026-01-02", "2026-01-09")
        )

    def test_duplicate_rows_are_reported(self):
        write(self.root, "t.csv", "a,b\n1,x\n1,x\n2,y\n1,x\n")
        (profile,) = profile_folder(self.root, self.out)
        self.assertEqual(profile["duplicate_rows"], 2)
        self.assertEqual(profile["key_candidates"], [])
        self.assertIn("2 exact duplicate rows", profile["notes"])

    def test_line_break_inside_a_quoted_field_is_noted(self):
        write(self.root, "t.csv", 'a,b\n1,"two\nlines"\n2,x\n')
        (profile,) = profile_folder(self.root, self.out)
        self.assertEqual(profile["rows"], 2)
        self.assertEqual(profile["file_records"], 3)
        self.assertEqual(len(profile["notes"]), 1)
        self.assertIn("3 records but 2 rows", profile["notes"][0])

    def test_file_with_a_header_and_no_rows(self):
        write(self.root, "t.csv", "a,b\n")
        (profile,) = profile_folder(self.root, self.out)
        self.assertEqual(profile["rows"], 0)
        self.assertEqual(profile["key_candidates"], [])
        self.assertIn("rows: 0", render(profile))

    def test_awkward_column_names(self):
        write(self.root, "t.csv", 'Order ID,"say ""hi""",select\n1,a,x\n2,b,x\n')
        (profile,) = profile_folder(self.root, self.out)
        self.assertEqual(
            [c["name"] for c in profile["columns"]], ["Order ID", 'say "hi"', "select"]
        )
        self.assertEqual(profile["key_candidates"], ["Order ID", 'say "hi"'])

    def test_two_files_with_the_same_table_name_are_rejected(self):
        write(self.root, "t.csv", "a\n1\n")
        write(self.root, "t.jsonl", '{"a": 1}\n')
        with self.assertRaisesRegex(ProfileError, "both be profiled as t"):
            profile_folder(self.root, self.out)

    def test_empty_folder_is_an_error(self):
        with self.assertRaisesRegex(ProfileError, "no CSV, JSON or Parquet files"):
            profile_folder(self.root, self.out)

    def test_command_line_reports_errors_without_a_traceback(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main([str(self.root / "missing"), "--out", str(self.out)])
        self.assertEqual(code, 1)
        self.assertIn("is not a folder", stderr.getvalue())

    def test_command_line_writes_profiles(self):
        write(self.root, "t.csv", "a\n1\n2\n")
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = main([str(self.root), "--out", str(self.out)])
        self.assertEqual(code, 0)
        self.assertIn("t: 2 rows, key candidates: a", stdout.getvalue())
        self.assertTrue((self.out / "t.txt").is_file())


class DemoData(unittest.TestCase):
    """The profiler must surface the defects planted in the demo data."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.data = root / "data"
        generate(seed=42, companies=200, out=cls.data, answers_out=root / "answers")
        cls.defects = json.loads((root / "answers" / "defects.json").read_text(encoding="utf-8"))
        cls.out = root / "profiles"
        cls.profiles = {p["table"]: p for p in profile_folder(cls.data, cls.out)}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def column(self, table, name):
        return next(c for c in self.profiles[table]["columns"] if c["name"] == name)

    def csv_rows(self, relative):
        with (self.data / relative).open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))

    def test_all_four_sources_are_found(self):
        self.assertEqual(
            sorted(self.profiles),
            ["billing.customers", "billing.invoices", "crm.accounts", "events.events"],
        )

    def test_row_counts_match_an_independent_recount(self):
        for relative, table in (
            ("billing/customers.csv", "billing.customers"),
            ("billing/invoices.csv", "billing.invoices"),
            ("crm/accounts.csv", "crm.accounts"),
        ):
            self.assertEqual(self.profiles[table]["rows"], len(self.csv_rows(relative)), table)
        lines = (self.data / "events/events.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(self.profiles["events.events"]["rows"], len(lines))

    def test_distinct_and_null_counts_match_an_independent_recount(self):
        rows = self.csv_rows("billing/invoices.csv")
        for name in rows[0]:
            column = self.column("billing.invoices", name)
            self.assertEqual(column["distinct"], len({row[name] for row in rows}), name)
            self.assertEqual(column["nulls"], sum(1 for row in rows if row[name] == ""), name)

    def test_keys(self):
        self.assertEqual(self.profiles["billing.customers"]["key_candidates"], ["id"])
        self.assertEqual(self.profiles["billing.invoices"]["key_candidates"], ["id"])
        self.assertIn("account_id", self.profiles["crm.accounts"]["key_candidates"])
        self.assertEqual(self.profiles["events.events"]["key_candidates"], [])

    def test_duplicate_events_match_the_planted_count(self):
        self.assertEqual(
            self.profiles["events.events"]["duplicate_rows"], self.defects["duplicate_events"]
        )

    def test_billing_email_is_not_unique_because_of_duplicate_customers(self):
        email = self.column("billing.customers", "email")
        self.assertFalse(email["unique"])
        self.assertEqual(
            self.profiles["billing.customers"]["rows"] - email["distinct"],
            self.defects["duplicate_billing_customers"],
        )

    def test_crm_ids_keep_their_leading_zeros(self):
        account_id = self.column("crm.accounts", "account_id")
        self.assertEqual(account_id["converts_to"], "text")
        self.assertTrue(any("leading zeros" in note for note in account_id["notes"]))

    def test_day_first_dates_are_identified(self):
        created = self.column("crm.accounts", "created_date")
        self.assertEqual(created["converts_to"], "date")
        self.assertEqual(created["notes"], ["dates are written day/month/year"])

    def test_unix_timestamps_are_identified(self):
        created = self.column("billing.invoices", "created")
        self.assertIn("every value fits a Unix timestamp in seconds", created["notes"])

    def test_refunds_show_as_a_negative_minimum(self):
        self.assertTrue(self.column("billing.invoices", "amount_minor")["min"].startswith("-"))

    def test_junk_event_users_are_listed_as_placeholders(self):
        events = [
            json.loads(line)
            for line in (self.data / "events/events.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        expected = {}
        for event in events:
            value = (event["user_email"] or "").strip().lower()
            if value in ("-1", "anonymous", "null"):
                expected[value] = expected.get(value, 0) + 1
        self.assertTrue(expected)
        self.assertEqual(self.column("events.events", "user_email")["placeholders"], expected)

    def test_profiles_are_much_smaller_than_the_data(self):
        data = sum(path.stat().st_size for path in self.data.rglob("*") if path.is_file())
        text = sum(path.stat().st_size for path in self.out.glob("*.txt"))
        self.assertLess(text * 20, data)

    def test_profiling_twice_gives_identical_files(self):
        with tempfile.TemporaryDirectory() as again:
            profile_folder(self.data, Path(again))
            for path in sorted(self.out.iterdir()):
                self.assertEqual(
                    path.read_bytes(), (Path(again) / path.name).read_bytes(), path.name
                )


if __name__ == "__main__":
    unittest.main()
