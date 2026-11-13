"""Projects that fail the gate for one known reason, for measuring a diagnosis.

    python tests/failing_bed.py --out DIR [--seed N]

Each variant is the reference project with a few exact edits that make one
root cause: a wrong match rule, a dropped filter, money read as the wrong
type, and so on. The gate fails each one on a known set of checks, and
putting the edited lines back makes it pass again. Some variants copy
mistakes engineer agents made in live runs (``origin`` "observed"); the
others are planted. What each variant is stays in this file and in the
bed's answers, never in the variant's own folder: the projects are named
project-01, project-02 and so on, in an order shuffled by the seed, and
answers.json beside them says which is which.
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

REFERENCE = ROOT / "tests" / "fixtures" / "demo_project"

STAGING = "models/staging/"
CUSTOMERS = STAGING + "stg_billing__customers.sql"
INVOICES = STAGING + "stg_billing__invoices.sql"
ACCOUNTS = STAGING + "stg_crm__accounts.sql"
EVENTS = STAGING + "stg_events__events.sql"
CROSSWALK = "models/intermediate/int_customers_matched_to_accounts.sql"

# The reference crosswalk opens with a comment saying how it matches. Every
# bed project goes without it, so a broken match is not given away by a
# comment describing the right one.
CROSSWALK_COMMENT = (
    "-- A company's CRM account may hold a different mailbox from its billing\n"
    "-- customers, so customers and accounts are matched on the email domain, which\n"
    "-- belongs to one company.\n"
)

OBSERVED = "observed"
PLANTED = "planted"

# Every cause a variant can have, with what it means. A diagnosis is scored
# against this list, so that two wordings of one cause count as the same.
CAUSES = {
    "match rule": "records of two sources are matched on the wrong value",
    "join type": "a join drops the rows that have no match",
    "row filter": "rows are left out or kept against the goal",
    "deduplication": "rows are merged or repeated against the goal",
    "type": "a value is read as the wrong type",
    "unit or sign": "amounts are scaled or their sign is changed",
    "nondeterminism": "a value changes from one build to the next",
    "missing column": "a model names a column its source does not have",
}

CROSSWALK_MODEL = "int_customers_matched_to_accounts"


@dataclass(frozen=True)
class Edit:
    """Replace old, which must appear exactly once in the file, with new."""

    path: str
    old: str
    new: str


@dataclass(frozen=True)
class Variant:
    """One project of the bed, with one root cause.

    ``model`` and ``path`` say where the cause is; ``failing`` lists every
    (check, subject, status) the gate gives other than a pass, which may
    reach models built from the broken one.
    """

    name: str
    cause: str
    origin: str
    summary: str
    model: str
    path: str
    edits: tuple[Edit, ...]
    failing: tuple[tuple[str, str, str], ...]


def _crosswalk_join(condition: str) -> Edit:
    return Edit(
        CROSSWALK,
        "    on split_part(customers.email, '@', 2) = split_part(accounts.billing_email, '@', 2)",
        "    on " + condition,
    )


VARIANTS = (
    Variant(
        "crosswalk_whole_email",
        "match rule",
        OBSERVED,
        "Customers are matched to accounts on the whole email address, so the"
        " accounts that hold another mailbox at the company's domain are missed.",
        CROSSWALK_MODEL,
        CROSSWALK,
        (_crosswalk_join("customers.email = accounts.billing_email"),),
        (("expected", CROSSWALK_MODEL, "fail"),),
    ),
    Variant(
        "customers_one_per_email",
        "deduplication",
        OBSERVED,
        "Billing customers are kept once per email address, so customers who share"
        " an address with another customer are dropped.",
        "stg_billing__customers",
        CUSTOMERS,
        (
            Edit(
                CUSTOMERS,
                "where livemode = 'true'",
                "where livemode = 'true'\n"
                "qualify row_number() over (partition by lower(trim(email)) order by id) = 1",
            ),
        ),
        (
            ("rows", "stg_billing__customers", "fail"),
            ("rows", CROSSWALK_MODEL, "fail"),
            ("expected", CROSSWALK_MODEL, "fail"),
        ),
    ),
    Variant(
        "crosswalk_exact_email",
        "match rule",
        PLANTED,
        "Customers are matched to accounts on the email exactly as each source stores it,"
        " so addresses that differ only in case or spaces are missed.",
        CROSSWALK_MODEL,
        CROSSWALK,
        (
            Edit(
                CROSSWALK,
                "select\n    customers.customer_id,\n    accounts.account_id\n"
                "from {{ ref('stg_billing__customers') }} as customers\n"
                "left join {{ ref('stg_crm__accounts') }} as accounts\n"
                "    on split_part(customers.email, '@', 2) = split_part(accounts.billing_email, '@', 2)",
                "select\n    customers.id as customer_id,\n    accounts.account_id\n"
                "from {{ source('billing', 'customers') }} as customers\n"
                "left join {{ source('crm', 'accounts') }} as accounts\n"
                "    on customers.email = accounts.billing_email\n"
                "where customers.livemode = 'true'",
            ),
        ),
        (("expected", CROSSWALK_MODEL, "fail"),),
    ),
    Variant(
        "crosswalk_inner_join",
        "join type",
        PLANTED,
        "The crosswalk uses an inner join, so customers with no CRM account are dropped.",
        CROSSWALK_MODEL,
        CROSSWALK,
        (Edit(CROSSWALK, "left join", "inner join"),),
        (("rows", CROSSWALK_MODEL, "fail"), ("expected", CROSSWALK_MODEL, "fail")),
    ),
    Variant(
        "refunds_dropped",
        "row filter",
        PLANTED,
        "Invoices with a negative amount, which are refunds, are left out.",
        "stg_billing__invoices",
        INVOICES,
        (
            Edit(
                INVOICES,
                "where livemode = 'true'",
                "where livemode = 'true' and cast(amount_minor as bigint) > 0",
            ),
        ),
        (("rows", "stg_billing__invoices", "fail"), ("total", "stg_billing__invoices", "fail")),
    ),
    Variant(
        "test_mode_customers_kept",
        "row filter",
        PLANTED,
        "Billing customers created in test mode are kept.",
        "stg_billing__customers",
        CUSTOMERS,
        (Edit(CUSTOMERS, "\nwhere livemode = 'true'", ""),),
        (
            ("rows", "stg_billing__customers", "fail"),
            ("rows", CROSSWALK_MODEL, "fail"),
            ("expected", CROSSWALK_MODEL, "fail"),
        ),
    ),
    Variant(
        "events_not_deduplicated",
        "deduplication",
        PLANTED,
        "Events delivered twice are kept twice.",
        "stg_events__events",
        EVENTS,
        (Edit(EVENTS, "select distinct", "select"),),
        (
            ("build", "project", "fail"),
            ("rows", "stg_events__events", "fail"),
            ("key", "stg_events__events", "fail"),
            ("repeat", "project", "fail"),
        ),
    ),
    Variant(
        "money_as_double",
        "type",
        PLANTED,
        "Invoice amounts are held as floating-point numbers.",
        "stg_billing__invoices",
        INVOICES,
        (
            Edit(
                INVOICES,
                "cast(amount_minor as bigint) as amount_minor",
                "cast(amount_minor as double) as amount_minor",
            ),
        ),
        (("total", "stg_billing__invoices", "fail"),),
    ),
    Variant(
        "account_id_as_number",
        "type",
        PLANTED,
        "CRM account identifiers are read as numbers, so their leading zeros are lost.",
        "stg_crm__accounts",
        ACCOUNTS,
        (Edit(ACCOUNTS, "    account_id,", "    cast(account_id as bigint) as account_id,"),),
        (("expected", CROSSWALK_MODEL, "fail"),),
    ),
    Variant(
        "yen_scaled_by_100",
        "unit or sign",
        PLANTED,
        "Yen amounts are multiplied by 100 as if yen had cents.",
        "stg_billing__invoices",
        INVOICES,
        (
            Edit(
                INVOICES,
                "cast(amount_minor as bigint) as amount_minor",
                "case when currency = 'jpy' then cast(amount_minor as bigint) * 100"
                " else cast(amount_minor as bigint) end as amount_minor",
            ),
        ),
        (("total", "stg_billing__invoices", "fail"),),
    ),
    Variant(
        "refunds_made_positive",
        "unit or sign",
        PLANTED,
        "Refunds lose their minus sign.",
        "stg_billing__invoices",
        INVOICES,
        (
            Edit(
                INVOICES,
                "cast(amount_minor as bigint) as amount_minor",
                "abs(cast(amount_minor as bigint)) as amount_minor",
            ),
        ),
        (("total", "stg_billing__invoices", "fail"),),
    ),
    Variant(
        "invoice_load_time",
        "nondeterminism",
        PLANTED,
        "Invoices carry the time they were loaded, which differs on every build.",
        "stg_billing__invoices",
        INVOICES,
        (Edit(INVOICES, "    status,\n", "    status,\n    current_timestamp as loaded_at,\n"),),
        (("repeat", "stg_billing__invoices", "fail"),),
    ),
    Variant(
        "account_column_misspelt",
        "missing column",
        PLANTED,
        "The accounts model names a segment column the CRM file does not have.",
        "stg_crm__accounts",
        ACCOUNTS,
        (Edit(ACCOUNTS, "    segment,", "    segment_name,"),),
        (
            ("build", "project", "fail"),
            ("build", "stg_crm__accounts", "fail"),
            ("rows", "stg_crm__accounts", "skip"),
            ("key", "stg_crm__accounts", "skip"),
            ("repeat", "project", "fail"),
            ("repeat", "stg_crm__accounts", "skip"),
            ("build", CROSSWALK_MODEL, "fail"),
            ("rows", CROSSWALK_MODEL, "skip"),
            ("key", CROSSWALK_MODEL, "skip"),
            ("expected", CROSSWALK_MODEL, "skip"),
            ("repeat", CROSSWALK_MODEL, "skip"),
        ),
    ),
)


def variant(name: str) -> Variant:
    for candidate in VARIANTS:
        if candidate.name == name:
            return candidate
    raise KeyError(name)


def _replace_once(path: Path, old: str, new: str, name: str) -> None:
    text = path.read_text(encoding="utf-8")
    found = text.count(old)
    if found != 1:
        raise ValueError(f"{name}: {path.name} holds {old!r} {found} times, not once")
    path.write_text(text.replace(old, new), encoding="utf-8")


def make(chosen: Variant, destination: Path, reference: Path = REFERENCE) -> Path:
    """Write the variant's project to destination, which must not exist yet."""
    destination = Path(destination)
    shutil.copytree(reference, destination)
    try:
        _replace_once(destination / CROSSWALK, CROSSWALK_COMMENT, "", chosen.name)
        for edit in chosen.edits:
            _replace_once(destination / edit.path, edit.old, edit.new, chosen.name)
    except ValueError:
        shutil.rmtree(destination)
        raise
    return destination


def repair(chosen: Variant, project: Path, reference: Path = REFERENCE) -> Path:
    """Write each file the variant edits back as a bed project of the reference has it."""
    project = Path(project)
    for path in sorted({edit.path for edit in chosen.edits}):
        text = (Path(reference) / path).read_text(encoding="utf-8")
        if path == CROSSWALK:
            text = text.replace(CROSSWALK_COMMENT, "", 1)
        (project / path).write_text(text, encoding="utf-8")
    return project


class BedError(Exception):
    """The bed cannot be written where asked."""


def write_bed(out: Path, seed: int = 0) -> dict:
    """Write every variant under neutral names in out/projects, and out/answers.json."""
    out = Path(out)
    if out.exists() and any(out.iterdir()):
        raise BedError(f"{out} is not empty")
    order = list(VARIANTS)
    random.Random(f"{seed}:failing-bed").shuffle(order)
    answers = {"seed": seed, "causes": dict(CAUSES), "projects": {}}
    for number, chosen in enumerate(order, start=1):
        name = f"project-{number:02d}"
        make(chosen, out / "projects" / name)
        answers["projects"][name] = {
            "variant": chosen.name,
            "cause": chosen.cause,
            "origin": chosen.origin,
            "model": chosen.model,
            "file": chosen.path,
            "summary": chosen.summary,
            "failing": sorted(list(result) for result in chosen.failing),
        }
    text = json.dumps(answers, indent=2, sort_keys=True) + "\n"
    (out / "answers.json").write_text(text, encoding="utf-8")
    return answers


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write the failing test bed.")
    parser.add_argument("--out", type=Path, required=True, help="empty folder to write into")
    parser.add_argument("--seed", type=int, default=0, help="seed for the projects' order")
    args = parser.parse_args(argv)
    try:
        answers = write_bed(args.out, args.seed)
    except BedError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    observed = sum(entry["origin"] == OBSERVED for entry in answers["projects"].values())
    print(
        f"{len(answers['projects'])} failing projects ({observed} observed in live runs)"
        f" in {args.out / 'projects'}"
    )
    print(f"answers in {args.out / 'answers.json'}; keep them away from any agent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
