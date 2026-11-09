"""Projects that pass the gate, for measuring a reviewer.

Each variant is the reference project with a few exact edits. A defect
variant carries one planted mistake in a column the gate does not check, so
the gate passes it while its values are wrong. A good variant is correct but
written differently, so a reviewer that rejects it is wrong. What each
variant is stays in this file and in the bed's answers, never in the
variant's own folder.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REFERENCE = ROOT / "tests" / "fixtures" / "demo_project"

STAGING = "models/staging/"
CUSTOMERS = STAGING + "stg_billing__customers.sql"
INVOICES = STAGING + "stg_billing__invoices.sql"
ACCOUNTS = STAGING + "stg_crm__accounts.sql"
EVENTS = STAGING + "stg_events__events.sql"
CROSSWALK = "models/intermediate/int_customers_matched_to_accounts.sql"

DEFECT = "defect"
GOOD = "good"


@dataclass(frozen=True)
class Edit:
    """Replace old, which must appear exactly once in the file, with new."""

    path: str
    old: str
    new: str


@dataclass(frozen=True)
class Variant:
    """One project of the bed.

    ``columns`` names the (model, column) pairs a defect makes wrong.
    ``mapping`` says, for a good variant, how to read a reference column
    from the variant: (model, reference column, SQL on the variant).
    Unlisted columns are read under their own name.
    """

    name: str
    kind: str
    summary: str
    edits: tuple[Edit, ...] = ()
    columns: tuple[tuple[str, str], ...] = ()
    mapping: tuple[tuple[str, str, str], ...] = field(default=())


VARIANTS = (
    Variant(
        "invoice_time_in_milliseconds",
        DEFECT,
        "Invoice times are read as milliseconds, so every invoice lands in January 1970.",
        (
            Edit(
                INVOICES,
                "to_timestamp(cast(created as bigint))",
                "to_timestamp(cast(created as bigint) / 1000)",
            ),
        ),
        columns=(("stg_billing__invoices", "created_at"),),
    ),
    Variant(
        "account_date_month_first",
        DEFECT,
        "CRM dates are day/month/year but read month first, so days above 12 are lost"
        " and the others have day and month swapped.",
        (
            Edit(
                ACCOUNTS,
                "cast(strptime(created_date, '%d/%m/%Y') as date)",
                "cast(try_strptime(created_date, '%m/%d/%Y') as date)",
            ),
        ),
        columns=(("stg_crm__accounts", "created_date"),),
    ),
    Variant(
        "event_columns_swapped",
        DEFECT,
        "The event's user email and event name are swapped.",
        (
            Edit(
                EVENTS,
                "    user_email,\n    event_name,",
                "    event_name as user_email,\n    user_email as event_name,",
            ),
        ),
        columns=(("stg_events__events", "user_email"), ("stg_events__events", "event_name")),
    ),
    Variant(
        "customer_currency_constant",
        DEFECT,
        "Every customer's currency is written as usd, whatever the raw file says.",
        (Edit(CUSTOMERS, "    currency,", "    'usd' as currency,"),),
        columns=(("stg_billing__customers", "currency"),),
    ),
    Variant(
        "contact_name_from_email",
        DEFECT,
        "The customer's contact name is filled from the email address.",
        (Edit(CUSTOMERS, "name as contact_name", "email as contact_name"),),
        columns=(("stg_billing__customers", "contact_name"),),
    ),
    Variant(
        "event_time_truncated_to_day",
        DEFECT,
        "Event times keep only the day, so the time of day is lost.",
        (
            Edit(
                EVENTS,
                "cast(ts as timestamptz) as occurred_at",
                "date_trunc('day', cast(ts as timestamptz)) as occurred_at",
            ),
        ),
        columns=(("stg_events__events", "occurred_at"),),
    ),
    Variant("reference", GOOD, "The reference project unchanged."),
    Variant(
        "views",
        GOOD,
        "Every model is a view instead of a table.",
        (Edit("dbt_project.yml", "+materialized: table", "+materialized: view"),),
    ),
    Variant(
        "invoice_currency_upper_case",
        GOOD,
        "Invoice currency codes are written in capitals.",
        (Edit(INVOICES, "    currency,", "    upper(currency) as currency,"),),
        mapping=(("stg_billing__invoices", "currency", "lower(currency)"),),
    ),
    Variant(
        "crosswalk_with_regexp",
        GOOD,
        "The crosswalk takes each email's domain with a regular expression.",
        (
            Edit(
                CROSSWALK,
                "split_part(customers.email, '@', 2) = split_part(accounts.billing_email, '@', 2)",
                "regexp_extract(customers.email, '@(.*)$', 1)"
                " = regexp_extract(accounts.billing_email, '@(.*)$', 1)",
            ),
        ),
    ),
    Variant(
        "columns_renamed",
        GOOD,
        "Two columns have other names.",
        (
            Edit(CUSTOMERS, "name as contact_name", "name as customer_name"),
            Edit(
                INVOICES,
                "to_timestamp(cast(created as bigint)) as created_at",
                "to_timestamp(cast(created as bigint)) as created_timestamp",
            ),
        ),
        mapping=(
            ("stg_billing__customers", "contact_name", "customer_name"),
            ("stg_billing__invoices", "created_at", "created_timestamp"),
        ),
    ),
    Variant(
        "invoice_refund_flag",
        GOOD,
        "Invoices gain a column saying whether the invoice is a refund.",
        (
            Edit(
                INVOICES,
                "    status,",
                "    status,\n    cast(amount_minor as bigint) < 0 as is_refund,",
            ),
        ),
    ),
)


def variant(name: str) -> Variant:
    for candidate in VARIANTS:
        if candidate.name == name:
            return candidate
    raise KeyError(name)


def make(chosen: Variant, destination: Path, reference: Path = REFERENCE) -> Path:
    """Write the variant's project to destination, which must not exist yet."""
    destination = Path(destination)
    shutil.copytree(reference, destination)
    for edit in chosen.edits:
        path = destination / edit.path
        text = path.read_text(encoding="utf-8")
        found = text.count(edit.old)
        if found != 1:
            shutil.rmtree(destination)
            raise ValueError(
                f"{chosen.name}: {edit.path} holds {edit.old!r} {found} times, not once"
            )
        path.write_text(text.replace(edit.old, edit.new), encoding="utf-8")
    return destination
