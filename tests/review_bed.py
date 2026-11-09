"""Projects that pass the gate, for measuring a reviewer.

    python tests/review_bed.py --out DIR [--seed N]

Each variant is the reference project with a few exact edits. A defect
variant carries one planted mistake in a column the gate does not check, so
the gate passes it while its values are wrong. A good variant is correct but
written differently, so a reviewer that rejects it is wrong. What each
variant is stays in this file and in the bed's answers, never in the
variant's own folder: the bed's projects are named project-01, project-02
and so on, in an order shuffled by the seed, and answers.json beside them
says which is which.
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import duckdb

from tenx.gate import DATABASE_FILE, load_declaration

REFERENCE = ROOT / "tests" / "fixtures" / "demo_project"
DECLARATION = ROOT / "tests" / "fixtures" / "demo_gate.json"

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


class BedError(Exception):
    """A variant's build cannot be compared with the reference's."""


def _rows(database: Path, model: str, key: tuple[str, ...], columns: dict[str, str]) -> dict:
    """Each row's columns as text, by key: {key: {column: value}}."""
    con = duckdb.connect(str(database), read_only=True)
    try:
        names = [row[0] for row in con.execute(f"describe {model}").fetchall()]
        picked = [*key, *columns.values()]
        missing = [name for name in picked if name.isidentifier() and name not in names]
        if missing:
            raise BedError(f"{model} has no column {', '.join(missing)}")
        select = ", ".join(f"cast({expression} as varchar)" for expression in picked)
        found = con.execute(f"select {select} from {model}").fetchall()
    finally:
        con.close()
    rows = {}
    for row in found:
        rows[row[: len(key)]] = dict(zip(columns, row[len(key) :]))
    if len(rows) != len(found):
        raise BedError(f"{model} repeats a key")
    return rows


def differences(reference: Path, built: Path, chosen: Variant) -> dict[tuple[str, str], int]:
    """How many rows of each (model, column) differ between two gate work folders.

    Every column of the reference build is compared, by the model's declared
    key, after reading the variant through the variant's mapping. Columns
    the variant adds are not compared.
    """
    mapped = {(model, column): sql for model, column, sql in chosen.mapping}
    counts = {}
    for spec in load_declaration(DECLARATION):
        con = duckdb.connect(str(Path(reference) / DATABASE_FILE), read_only=True)
        try:
            names = [row[0] for row in con.execute(f"describe {spec.name}").fetchall()]
        finally:
            con.close()
        columns = [name for name in names if name not in spec.key]
        ours = _rows(Path(reference) / DATABASE_FILE, spec.name, spec.key, {c: c for c in columns})
        theirs = _rows(
            Path(built) / DATABASE_FILE,
            spec.name,
            spec.key,
            {c: mapped.get((spec.name, c), c) for c in columns},
        )
        if set(ours) != set(theirs):
            raise BedError(f"{spec.name} holds other keys than the reference")
        for column in columns:
            counts[(spec.name, column)] = sum(
                ours[key][column] != theirs[key][column] for key in ours
            )
    return counts


def write_bed(out: Path, seed: int = 0) -> dict:
    """Write every variant under neutral names in out/projects, and out/answers.json."""
    out = Path(out)
    if out.exists() and any(out.iterdir()):
        raise BedError(f"{out} is not empty")
    order = list(VARIANTS)
    random.Random(f"{seed}:review-bed").shuffle(order)
    answers = {"seed": seed, "projects": {}}
    for number, chosen in enumerate(order, start=1):
        name = f"project-{number:02d}"
        make(chosen, out / "projects" / name)
        answers["projects"][name] = {
            "variant": chosen.name,
            "kind": chosen.kind,
            "summary": chosen.summary,
            "columns": [list(pair) for pair in chosen.columns],
        }
    text = json.dumps(answers, indent=2, sort_keys=True) + "\n"
    (out / "answers.json").write_text(text, encoding="utf-8")
    return answers


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write the reviewer test bed.")
    parser.add_argument("--out", type=Path, required=True, help="empty folder to write into")
    parser.add_argument("--seed", type=int, default=0, help="seed for the projects' order")
    args = parser.parse_args(argv)
    try:
        answers = write_bed(args.out, args.seed)
    except BedError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    defects = sum(entry["kind"] == DEFECT for entry in answers["projects"].values())
    print(
        f"{len(answers['projects'])} projects ({defects} with a defect) in {args.out / 'projects'}"
    )
    print(f"answers in {args.out / 'answers.json'}; keep them away from any reviewer")
    return 0


if __name__ == "__main__":
    sys.exit(main())
