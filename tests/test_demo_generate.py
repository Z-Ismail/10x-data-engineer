"""Tests for the demo data generator.

The checks recount everything from the written files with their own logic, so
they do not share code with the generator's answer key.
"""

import csv
import hashlib
import io
import json
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from demo.generate import MIN_COMPANIES, generate

JUNK = {"", "null", "anonymous", "-1"}
OTHER_MAILBOXES = {"accounts", "finance", "ap", "invoices", "admin"}

# SHA-256 of each file written before CRM accounts could hold another mailbox.
# Planting that defect must leave everything else as it was, so these still
# hold once each changed address is put back to the billing one.
EARLIER_FILES = {
    (42, 200): {
        "billing/customers.csv": "1dc299baef0fb07e8ce3f25c502c2b8cbf1450722e6086dd880db1309ca66d0e",
        "billing/invoices.csv": "6ddabd6ee6d591fa212eff78695dded5d2508bd53e36c8772673159ed74e47cb",
        "crm/accounts.csv": "33dcca097034711875e881a440cf68e5be435ae6dbc59c1c04fa3fc29297a417",
        "events/events.jsonl": "1f3155f6cc9c403076e312e73a059c1720f40fea8969c469d4e1d59fbfc79e47",
        "answers.json": "e751c963fb0713928241d98b16b0cf7e45e14ceb975c281c4c1a7362b79402e8",
        "defects.json": "aa7114a2caba2bb189e97f57568d47e9479f5d366aa02202f6fbe317742179b5",
    },
    (7, 1000): {
        "billing/customers.csv": "48c2ce3d055fad6c0d6ad0d4280128bba63140704aa8534bf6a55d3b0c61f00b",
        "billing/invoices.csv": "08783e8e5827ffa75791705069c67c532b7b5edfefb8496eeaf514eb6c796cca",
        "crm/accounts.csv": "e468ec3e40b8afe9b82a24c4282a25dfe098a5fcaba9a6671ce21d13ccd303f2",
        "events/events.jsonl": "75dbbc255a330e6eecbec59a7abc7eaa4670984052d3de4e5a16894d7f987fe4",
        "answers.json": "a8a6cefd0e78f838b01e4d3d9a84d036487df6eaed9f8dcbb4c15871dd4788a2",
        "defects.json": "46b9592d0bf16f345ae98d17d297b401bf796898776c508eb128c0547b2049b2",
    },
    (1, 20): {
        "billing/customers.csv": "8e8a0848c1aa185de3b2fbfb124dc726d10b4d718be22012f7a8cac6aefd9a1a",
        "billing/invoices.csv": "c81ee0f8d0cd3ce308f682895da398356417bf7411683442c5b1a960af062440",
        "crm/accounts.csv": "0095e7f9b4ae441d5b46f9407a19f7407d9c9fae351154fa655600c5a3263104",
        "events/events.jsonl": "d5f2dd4fef5abd6008d59b8e671c7d9ab3eea91fddf0198f58b21b0cb359a519",
        "answers.json": "0706244b8aa12008183784435b81f7ebf4d3d6a458c9295390553e9760c61c31",
        "defects.json": "d69ef0ad79245a5df109d1c21a2361965694e692013f44c3a511638ee14670da",
    },
}


def read_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def domain(email: str) -> str:
    return email.strip().lower().split("@")[1]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def other_mailbox_accounts(customers: list[dict], accounts: list[dict]) -> list[dict]:
    """CRM accounts whose address is not a billing one, at a billing domain."""
    billing = {row["email"] for row in customers if row["livemode"] == "true"}
    domains = {domain(email) for email in billing}
    return [
        row
        for row in accounts
        if row["billing_email"].strip().lower() not in billing
        and domain(row["billing_email"]) in domains
    ]


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
        billing = {domain(row["email"]) for row in self.customers if row["livemode"] == "true"}
        conflicts = [
            row
            for row in self.accounts
            if row["status"] == "prospect" and domain(row["billing_email"]) in billing
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

    def test_companies_missing_from_either_system_are_counted_by_domain(self):
        billing = {domain(row["email"]) for row in self.customers if row["livemode"] == "true"}
        crm = {domain(row["billing_email"]) for row in self.accounts}
        missing = len(billing - crm)
        prospects = len(crm - billing)
        self.assertEqual(missing, self.defects["paying_customers_missing_from_crm"])
        email = {row["id"]: row["email"] for row in self.customers}
        paying = {
            domain(email[row["customer_id"]])
            for row in self.invoices
            if row["livemode"] == "true"
            and row["status"] == "paid"
            and int(row["amount_minor"]) > 0
        }
        self.assertEqual(len(paying - crm), self.answers["paying_customers_missing_from_crm"])
        self.assertEqual(prospects, self.defects["crm_prospects_without_billing"])
        self.assertEqual(prospects, self.answers["crm_accounts_without_live_billing"])

    def test_some_crm_accounts_hold_another_mailbox_at_the_company_domain(self):
        other = other_mailbox_accounts(self.customers, self.accounts)
        self.assertEqual(len(other), self.defects["crm_accounts_with_other_mailbox"])
        for row in other:
            local, _, _ = row["billing_email"].partition("@")
            self.assertIn(local, OTHER_MAILBOXES)
            # Stored cleanly, so only the mailbox differs from billing.
            self.assertEqual(row["billing_email"], row["billing_email"].strip().lower())
        # 15% of the paying companies in the CRM whose address is stored
        # cleanly, rounded: those that still match billing exactly, and these.
        billing = {row["email"] for row in self.customers if row["livemode"] == "true"}
        exact = sum(1 for row in self.accounts if row["billing_email"] in billing)
        self.assertEqual(len(other), round(0.15 * (exact + len(other))))

    def test_another_mailbox_leaves_every_other_value_as_it_was(self):
        for (seed, companies), digests in EARLIER_FILES.items():
            with self.subTest(seed=seed, companies=companies), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                generate(seed, companies, root / "data", root / "answers")
                for name in (
                    "billing/customers.csv",
                    "billing/invoices.csv",
                    "events/events.jsonl",
                ):
                    self.assertEqual(sha256((root / "data" / name).read_bytes()), digests[name])
                self.assertEqual(
                    sha256((root / "answers" / "answers.json").read_bytes()),
                    digests["answers.json"],
                )
                defects = json.loads((root / "answers" / "defects.json").read_text())
                planted = defects.pop("crm_accounts_with_other_mailbox")
                self.assertGreater(planted, 0)
                earlier = json.dumps(defects, indent=2, sort_keys=True) + "\n"
                self.assertEqual(sha256(earlier.encode()), digests["defects.json"])

                path = root / "data" / "crm" / "accounts.csv"
                accounts = read_csv(path)
                customers = read_csv(root / "data" / "billing" / "customers.csv")
                other = other_mailbox_accounts(customers, accounts)
                self.assertEqual(len(other), planted)
                for row in other:
                    row["billing_email"] = "billing@" + domain(row["billing_email"])
                restored = io.StringIO()
                writer = csv.DictWriter(restored, fieldnames=list(accounts[0]), lineterminator="\n")
                writer.writeheader()
                writer.writerows(accounts)
                self.assertNotEqual(sha256(path.read_bytes()), digests["crm/accounts.csv"])
                self.assertEqual(sha256(restored.getvalue().encode()), digests["crm/accounts.csv"])

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
