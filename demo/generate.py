"""Generate three messy demo source systems with a known answer key.

Billing (CSV), CRM (CSV) and product events (JSON lines) describe the same set
of companies. Defects are planted at known rates and counted, and an answer
key is written to a separate folder so that it can be kept away from whatever
is being tested. The same seed always gives byte-identical files, and every
defect is present whatever the seed.

Usage:
    python demo/generate.py --seed 42 --companies 200
"""

from __future__ import annotations

import argparse
import csv
import json
import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

MONTHS = 6
PLANS = {"starter": 29, "team": 99, "scale": 399}
CURRENCIES = ["usd", "usd", "usd", "gbp", "eur", "jpy"]
ZERO_DECIMAL = {"jpy"}
JPY_PER_UNIT = 150
JUNK_USER_IDS = ["", "null", "anonymous", "-1"]
EVENT_NAMES = ["login", "report_viewed", "export_run", "invite_sent"]
SEGMENTS = {"starter": "smb", "team": "mid_market", "scale": "enterprise"}
OTHER_MAILBOXES = ["accounts", "finance", "ap", "invoices", "admin"]


def month_start(month: int) -> datetime:
    return datetime(2026, 1 + month, 1, tzinfo=timezone.utc)


def to_minor(price: int, currency: str) -> int:
    """Billing stores amounts in the smallest unit; yen has no smaller unit."""
    return price * JPY_PER_UNIT if currency in ZERO_DECIMAL else price * 100


def to_major(amount_minor: int, currency: str) -> Decimal:
    if currency in ZERO_DECIMAL:
        return Decimal(amount_minor)
    return Decimal(amount_minor) / Decimal(100)


def normalise_email(value: str) -> str:
    return value.strip().lower()


def email_domain(value: str) -> str:
    """The company an address belongs to: every demo company has its own domain."""
    return normalise_email(value).rpartition("@")[2]


def messy_email(rng: random.Random, email: str) -> str:
    """Return the email as a careless system might have stored it."""
    roll = rng.random()
    if roll < 0.15:
        return email.upper()
    if roll < 0.25:
        return f" {email} "
    if roll < 0.35:
        return email.capitalize()
    return email


MIN_COMPANIES = 20


def pick(rng: random.Random, pool: list[int], rate: float) -> set[int]:
    """Choose a share of the pool, and always at least one.

    Defects are allocated this way, not by independent coin flips, so every
    defect is present whatever the seed or the size of the dataset.
    """
    if not pool:
        return set()
    return set(rng.sample(pool, max(1, round(rate * len(pool)))))


def build(seed: int, companies: int) -> dict:
    """Build every row in memory. Returns the tables and the defect counts."""
    if companies < MIN_COMPANIES:
        raise ValueError(f"companies must be at least {MIN_COMPANIES}, got {companies}")
    rng = random.Random(seed)
    ids = list(range(1, companies + 1))

    # Who is who.
    paying = pick(rng, ids, 0.70)
    non_paying = [i for i in ids if i not in paying]
    paying_list = sorted(paying)
    currency = {i: rng.choice(CURRENCIES) for i in ids}
    plan = {i: rng.choice(sorted(PLANS)) for i in ids}
    first_month = {i: rng.randrange(MONTHS) for i in ids}
    if not any(currency[i] in ZERO_DECIMAL for i in paying_list):
        currency[rng.choice(paying_list)] = "jpy"
    if not any(first_month[i] < MONTHS - 1 for i in paying_list):
        first_month[rng.choice(paying_list)] = 0

    # Which companies carry which defect.
    duplicated = pick(rng, paying_list, 0.05)
    missing_from_crm = pick(rng, paying_list, 0.06)
    paying_in_crm = [i for i in paying_list if i not in missing_from_crm]
    status_conflict = pick(rng, paying_in_crm, 0.05)
    refunded = pick(rng, [i for i in paying_list if first_month[i] < MONTHS - 1], 0.04)
    prospects = pick(rng, non_paying, 0.80)
    in_crm = set(paying_in_crm) | prospects
    messy_in_crm = pick(rng, paying_in_crm, 0.35)
    uses_product = paying | pick(rng, non_paying, 0.50)

    customers: list[dict] = []
    invoices: list[dict] = []
    accounts: list[dict] = []
    events: list[dict] = []
    account_of_company: dict[int, dict] = {}
    invoice_n = 0

    def add_invoice(customer_id, amount, cur, status, created, live=True):
        nonlocal invoice_n
        invoice_n += 1
        invoices.append(
            {
                "id": f"in_{invoice_n:07d}",
                "customer_id": customer_id,
                "amount_minor": amount,
                "currency": cur,
                "status": status,
                "created": int(created.timestamp()),
                "livemode": "true" if live else "false",
            }
        )

    for i in ids:
        email = f"billing@company{i:04d}.example"
        signup = month_start(first_month[i]) + timedelta(days=rng.randrange(25))
        price = PLANS[plan[i]]

        if i in paying:
            # Billing cannot merge customers, so duplicates stay for good.
            customer_ids = [f"cus_{i:06d}A"] + ([f"cus_{i:06d}B"] if i in duplicated else [])
            for customer_id in customer_ids:
                customers.append(
                    {
                        "id": customer_id,
                        "email": email,
                        "name": f"Contact {i:04d}",
                        "currency": currency[i],
                        "created": int(signup.timestamp()),
                        "livemode": "true",
                    }
                )
            amount = to_minor(price, currency[i])
            for month in range(first_month[i], MONTHS):
                status = "paid" if rng.random() < 0.92 else rng.choice(["open", "void"])
                created = month_start(month) + timedelta(
                    days=rng.randrange(5), hours=rng.randrange(24)
                )
                add_invoice(rng.choice(customer_ids), amount, currency[i], status, created)
            if i in refunded:
                # A refund lands as a negative invoice in a later month.
                month = rng.randrange(first_month[i] + 1, MONTHS)
                created = month_start(month) + timedelta(days=10 + rng.randrange(10))
                add_invoice(customer_ids[0], -amount, currency[i], "paid", created)

        if i in in_crm:
            status = "customer" if i in paying and i not in status_conflict else "prospect"
            stored_email = email
            if i in messy_in_crm:
                stored_email = rng.choice([email.upper(), f" {email} ", email.capitalize()])
            elif i not in paying:
                stored_email = messy_email(rng, email)
            accounts.append(
                {
                    "account_id": f"001{i:015d}",
                    "company_name": f"Company {i:04d} Ltd",
                    "billing_email": stored_email,
                    "segment": SEGMENTS[plan[i]],
                    "status": status,
                    # The CRM exports local dates as day/month/year text.
                    "created_date": signup.strftime("%d/%m/%Y"),
                }
            )
            account_of_company[i] = accounts[-1]

        if i in uses_product:
            for _ in range(rng.randrange(3, 12)):
                when = signup + timedelta(days=rng.randrange(1, 150), seconds=rng.randrange(86400))
                stamp = when.strftime("%Y-%m-%dT%H:%M:%S")
                events.append(
                    {
                        "event_id": f"evt_{len(events) + 1:08d}",
                        "workspace_id": f"ws_{i:04d}",
                        "user_email": messy_email(rng, email),
                        "event_name": rng.choice(EVENT_NAMES),
                        "ts": stamp + ("Z" if rng.random() < 0.5 else "+00:00"),
                    }
                )

    positions = list(range(len(events)))
    junk_positions = pick(rng, positions, 0.02)
    for position in junk_positions:
        events[position]["user_email"] = rng.choice(JUNK_USER_IDS)
    duplicate_positions = pick(rng, positions, 0.01)
    # The same event delivered twice.
    events.extend(dict(events[position]) for position in sorted(duplicate_positions))

    test_customers = max(1, companies // 20)
    for t in range(1, test_customers + 1):
        # Test-mode rows that must never count as revenue.
        customer_id = f"cus_test_{t:04d}"
        customers.append(
            {
                "id": customer_id,
                "email": f"test{t}@internal.example",
                "name": f"Test Customer {t}",
                "currency": "usd",
                "created": int(month_start(0).timestamp()),
                "livemode": "false",
            }
        )
        for month in (0, 1):
            add_invoice(customer_id, 999900, "usd", "paid", month_start(month), live=False)

    # The CRM holds whoever sales deals with, so some accounts carry another
    # mailbox at the company's domain; only the domain still matches billing.
    # These are drawn from a stream of their own, so that every other value
    # stays as it was for a given seed and size.
    mailbox_rng = random.Random(f"{seed}:crm-mailboxes")
    clean_in_crm = [i for i in paying_in_crm if i not in messy_in_crm]
    other_mailbox = pick(mailbox_rng, clean_in_crm, 0.15)
    for i in sorted(other_mailbox):
        account = account_of_company[i]
        mailbox = mailbox_rng.choice(OTHER_MAILBOXES)
        account["billing_email"] = f"{mailbox}@{email_domain(account['billing_email'])}"

    for table in (customers, invoices, accounts, events):
        rng.shuffle(table)
    defects = {
        "duplicate_billing_customers": len(duplicated),
        "test_mode_customers": test_customers,
        "zero_decimal_currency_customers": sum(
            1 for i in paying_list if currency[i] in ZERO_DECIMAL
        ),
        "refunds": len(refunded),
        "paying_customers_missing_from_crm": len(missing_from_crm),
        "crm_prospects_without_billing": len(prospects),
        "crm_status_conflicts": len(status_conflict),
        "crm_emails_needing_normalisation": len(messy_in_crm),
        "crm_accounts_with_other_mailbox": len(other_mailbox),
        "junk_user_events": len(junk_positions),
        "duplicate_events": len(duplicate_positions),
    }
    return {
        "customers": customers,
        "invoices": invoices,
        "accounts": accounts,
        "events": events,
        "defects": defects,
    }


def answer_key(data: dict) -> dict:
    """The numbers a correct pipeline must reproduce from the raw files."""
    live_paid = [
        row for row in data["invoices"] if row["livemode"] == "true" and row["status"] == "paid"
    ]
    revenue: dict[str, Decimal] = {}
    for row in live_paid:
        major = to_major(row["amount_minor"], row["currency"])
        revenue[row["currency"]] = revenue.get(row["currency"], Decimal(0)) + major

    email_by_customer = {row["id"]: row["email"] for row in data["customers"]}
    paying_emails = {
        normalise_email(email_by_customer[row["customer_id"]])
        for row in live_paid
        if row["amount_minor"] > 0
    }
    # Companies are matched across systems on the email domain, which is
    # theirs alone, rather than on the whole address.
    paying_domains = {email_domain(email) for email in paying_emails}
    crm_domains = {email_domain(row["billing_email"]) for row in data["accounts"]}
    live_customers = [row for row in data["customers"] if row["livemode"] == "true"]
    live_emails = [normalise_email(row["email"]) for row in live_customers]
    live_domains = {email_domain(email) for email in live_emails}
    unique_events = {row["event_id"]: row for row in data["events"]}

    return {
        "live_paid_revenue_by_currency": {
            currency: str(total) for currency, total in sorted(revenue.items())
        },
        "paying_customers": len(paying_emails),
        "live_billing_customer_rows": len(live_customers),
        "live_billing_emails_with_duplicates": len(live_emails) - len(set(live_emails)),
        "paying_customers_missing_from_crm": len(paying_domains - crm_domains),
        "crm_accounts": len(data["accounts"]),
        "crm_accounts_without_live_billing": len(crm_domains - live_domains),
        "event_rows": len(data["events"]),
        "unique_events": len(unique_events),
        "unique_events_with_junk_user": sum(
            1 for row in unique_events.values() if row["user_email"] in JUNK_USER_IDS
        ),
    }


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def generate(seed: int, companies: int, out: Path, answers_out: Path) -> dict:
    data = build(seed, companies)
    write_csv(out / "billing" / "customers.csv", data["customers"])
    write_csv(out / "billing" / "invoices.csv", data["invoices"])
    write_csv(out / "crm" / "accounts.csv", data["accounts"])
    events_path = out / "events" / "events.jsonl"
    events_path.parent.mkdir(parents=True, exist_ok=True)
    with events_path.open("w", encoding="utf-8") as handle:
        for row in data["events"]:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    answers = answer_key(data)
    write_json(answers_out / "answers.json", answers)
    write_json(answers_out / "defects.json", data["defects"])
    return {"answers": answers, "defects": data["defects"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--companies", type=int, default=200)
    parser.add_argument("--out", type=Path, default=Path("demo/data"))
    parser.add_argument("--answers-out", type=Path, default=Path("demo/answers"))
    args = parser.parse_args()
    result = generate(args.seed, args.companies, args.out, args.answers_out)
    print(f"Wrote demo sources to {args.out} and the answer key to {args.answers_out}")
    for name, count in sorted(result["defects"].items()):
        print(f"  {name}: {count}")


if __name__ == "__main__":
    main()
