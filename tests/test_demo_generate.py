"""Tests for the demo data generator.

The checks recount everything from the written files with their own logic, so
they do not share code with the generator's answer key.
"""

import csv
import json
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from demo.generate import MIN_COMPANIES, generate  # noqa: E402

JUNK = {"", "null", "anonymous", "-1"}


def read_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def file_bytes(root: Path) -> dict:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class DemoGeneratorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.out = root / "data"
        cls.answers_dir = root / "answers"
        cls.result = generate(42, 200, cls.out, cls.answers_dir)
        cls.customers = read_csv(cls.out / "billing" / "customers.csv")
        cls.invoices = read_csv(cls.out / "billing" / "invoices.csv")
        cls.accounts = read_csv(cls.out / "crm" / "accounts.csv")
        lines = (cls.out / "events" / "events.jsonl").read_text(encoding="utf-8").splitlines()
        cls.events = [json.loads(line) for line in lines]
        cls.answers = json.loads((cls.answers_dir / "answers.json").read_text(encoding="utf-8"))
        cls.defects = json.loads((cls.answers_dir / "defects.json").read_text(encoding="utf-8"))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_same_seed_gives_identical_files(self):
        with tempfile.TemporaryDirectory() as again:
            root = Path(again)
            generate(42, 200, root / "data", root / "answers")
            self.assertEqual(file_bytes(self.out), file_bytes(root / "data"))
            self.assertEqual(file_bytes(self.answers_dir), file_bytes(root / "answers"))

    def test_different_seed_gives_different_files(self):
        with tempfile.TemporaryDirectory() as other:
            root = Path(other)
            generate(7, 200, root / "data", root / "answers")
            self.assertNotEqual(file_bytes(self.out), file_bytes(root / "data"))

    def test_every_defect_is_planted_for_any_seed_and_size(self):
        for seed in range(1, 26):
            for companies in (MIN_COMPANIES, 50, 200):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    result = generate(seed, companies, root / "data", root / "answers")
                for name, count in result["defects"].items():
                    self.assertGreater(count, 0, f"no {name} for seed {seed}, size {companies}")

    def test_too_few_companies_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(ValueError):
                generate(1, MIN_COMPANIES - 1, root / "data", root / "answers")

    def test_status_conflicts_are_paying_customers_marked_as_prospects(self):
        billing = {row["email"] for row in self.customers if row["livemode"] == "true"}
        conflicts = [
            row
            for row in self.accounts
            if row["status"] == "prospect" and row["billing_email"].strip().lower() in billing
        ]
        self.assertEqual(len(conflicts), self.defects["crm_status_conflicts"])

    def test_crm_emails_that_need_normalising_are_counted(self):
        billing = {row["email"] for row in self.customers if row["livemode"] == "true"}
        messy = [
            row
            for row in self.accounts
            if row["billing_email"] not in billing
            and row["billing_email"].strip().lower() in billing
        ]
        self.assertEqual(len(messy), self.defects["crm_emails_needing_normalisation"])

    def test_revenue_matches_recount_from_files(self):
        totals: dict[str, Decimal] = {}
        for row in self.invoices:
            if row["livemode"] != "true" or row["status"] != "paid":
                continue
            amount = Decimal(row["amount_minor"])
            if row["currency"] != "jpy":
                amount = amount / 100
            totals[row["currency"]] = totals.get(row["currency"], Decimal(0)) + amount
        expected = {
            currency: Decimal(value)
            for currency, value in self.answers["live_paid_revenue_by_currency"].items()
        }
        self.assertEqual(totals, expected)

    def test_test_mode_rows_are_excluded_from_revenue(self):
        test_total = sum(
            int(row["amount_minor"]) for row in self.invoices if row["livemode"] == "false"
        )
        self.assertGreater(test_total, 0)
        test_customers = [row for row in self.customers if row["livemode"] == "false"]
        self.assertEqual(len(test_customers), self.defects["test_mode_customers"])

    def test_duplicate_billing_customers_share_an_email(self):
        emails = [row["email"] for row in self.customers if row["livemode"] == "true"]
        duplicates = len(emails) - len(set(emails))
        self.assertEqual(duplicates, self.defects["duplicate_billing_customers"])
        self.assertEqual(duplicates, self.answers["live_billing_emails_with_duplicates"])

    def test_cross_system_matches_need_normalised_emails(self):
        billing = {row["email"] for row in self.customers if row["livemode"] == "true"}
        crm_raw = {row["billing_email"] for row in self.accounts}
        crm_clean = {email.strip().lower() for email in crm_raw}
        self.assertLess(len(billing & crm_raw), len(billing & crm_clean))
        self.assertEqual(
            len(billing - crm_clean), self.defects["paying_customers_missing_from_crm"]
        )
        self.assertEqual(len(crm_clean - billing), self.defects["crm_prospects_without_billing"])

    def test_refunds_are_negative_paid_invoices(self):
        refunds = [row for row in self.invoices if int(row["amount_minor"]) < 0]
        self.assertEqual(len(refunds), self.defects["refunds"])
        self.assertTrue(all(row["status"] == "paid" for row in refunds))

    def test_yen_amounts_have_no_minor_unit(self):
        yen = {int(row["amount_minor"]) for row in self.invoices if row["currency"] == "jpy"}
        self.assertTrue(yen)
        self.assertTrue(all(abs(amount) in {4350, 14850, 59850} for amount in yen))

    def test_events_contain_duplicates_and_junk_users(self):
        ids = [row["event_id"] for row in self.events]
        self.assertEqual(len(ids) - len(set(ids)), self.defects["duplicate_events"])
        self.assertEqual(len(set(ids)), self.answers["unique_events"])
        unique = {row["event_id"]: row for row in self.events}
        junk = sum(1 for row in unique.values() if row["user_email"] in JUNK)
        self.assertEqual(junk, self.defects["junk_user_events"])

    def test_crm_dates_are_day_first_text(self):
        days = {int(row["created_date"].split("/")[0]) for row in self.accounts}
        self.assertTrue(any(day > 12 for day in days))


if __name__ == "__main__":
    unittest.main()
