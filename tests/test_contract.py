"""Tests for checking a spec's contract.

The profiles are handmade, holding only the fields the check reads: the demo
tables and their columns as the profiler names them. The demo contract in
tests/fixtures is written by hand from the demo goal.
"""

import copy
import json
import tempfile
import unittest
from pathlib import Path

import jsonschema

from demo.generate import generate
from tenx.contract import SCHEMA, ContractError, check_contract, profile_columns
from tenx.profile import profile_folder

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CONTRACT = json.loads((FIXTURES / "demo_contract.json").read_text(encoding="utf-8"))


def table(name, *columns):
    return {"table": name, "columns": [{"name": column} for column in columns]}


PROFILES = [
    table("billing.customers", "id", "email", "name", "currency", "created", "livemode"),
    table(
        "billing.invoices",
        "id",
        "customer_id",
        "amount_minor",
        "currency",
        "status",
        "created",
        "livemode",
    ),
    table(
        "crm.accounts",
        "account_id",
        "company_name",
        "billing_email",
        "segment",
        "status",
        "created_date",
    ),
    table("events.events", "event_id", "event_name", "ts", "user_email", "workspace_id"),
]


def model(contract, name):
    return next(item for item in contract["models"] if item["name"] == name)


class Schema(unittest.TestCase):
    def test_the_demo_contract_has_the_schema_shape(self):
        jsonschema.validate(CONTRACT, SCHEMA)

    def test_the_schema_refuses_extra_and_missing_fields(self):
        extra = copy.deepcopy(CONTRACT)
        extra["models"][0]["owner"] = "data"
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(extra, SCHEMA)
        missing = copy.deepcopy(CONTRACT)
        del missing["models"][0]["grain"]
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(missing, SCHEMA)

    def test_the_schema_refuses_an_unknown_layer_or_rule(self):
        layer = copy.deepcopy(CONTRACT)
        layer["models"][0]["layer"] = "marts"
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(layer, SCHEMA)
        rule = copy.deepcopy(CONTRACT)
        rule["models"][0]["exclusions"][0]["rule"] = "filter"
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(rule, SCHEMA)


class Check(unittest.TestCase):
    def test_the_demo_contract_passes(self):
        self.assertIs(check_contract(CONTRACT, PROFILES), CONTRACT)

    def assertRefused(self, change, message):
        contract = copy.deepcopy(CONTRACT)
        change(contract)
        with self.assertRaisesRegex(ContractError, message):
            check_contract(contract, PROFILES)

    def test_mistakes_are_refused_with_the_reason(self):
        def edit(name, action):
            return lambda contract: action(model(contract, name))

        invoices, crosswalk = "stg_billing__invoices", "int_customers_matched_to_accounts"
        events = "stg_events__events"
        mistakes = {
            "not an object": (lambda c: c.clear(), r"^the contract has fields \[\]"),
            "no models": (lambda c: c["models"].clear(), r"^the contract lists no models$"),
            "unknown table": (
                edit(invoices, lambda m: m.update(source="billing.charges")),
                r"^model stg_billing__invoices source 'billing.charges' is not a profiled table$",
            ),
            "unknown raw column": (
                edit(
                    invoices,
                    lambda m: m["columns"][2].update({"from": ["billing.invoices.amount"]}),
                ),
                r"column amount_minor comes from billing.invoices.amount, which is not a profiled",
            ),
            "staging joins another table": (
                edit(
                    invoices, lambda m: m["columns"][1].update({"from": ["billing.customers.id"]})
                ),
                r"a staging model takes columns only from its own source, billing.invoices$",
            ),
            "column without an origin": (
                edit(invoices, lambda m: m["columns"][0].update({"from": []})),
                r"column invoice_id does not say which raw column it comes from$",
            ),
            "column named twice": (
                edit(invoices, lambda m: m["columns"][1].update(name="invoice_id")),
                r"^model stg_billing__invoices: column invoice_id is named twice$",
            ),
            "capitalised column name": (
                edit(invoices, lambda m: m["columns"][0].update(name="Invoice_ID")),
                r"'Invoice_ID' is not a name in lower case",
            ),
            "blank description": (
                edit(invoices, lambda m: m["columns"][0].update(description=" ")),
                r"column invoice_id description is not text$",
            ),
            "key not a column": (
                edit(invoices, lambda m: m.update(key=["id"])),
                r"^model stg_billing__invoices key 'id' is not one of its columns$",
            ),
            "no key": (
                edit(invoices, lambda m: m.update(key=[])),
                r"^model stg_billing__invoices has no key$",
            ),
            "blank grain": (
                edit(invoices, lambda m: m.update(grain="")),
                r"^model stg_billing__invoices grain is not text$",
            ),
            "name and layer disagree": (
                edit(crosswalk, lambda m: m.update(layer="staging")),
                r"is in the staging layer, so its name starts stg_$",
            ),
            "where rule without a condition": (
                edit(invoices, lambda m: m["exclusions"][0].update(where="  ")),
                r"exclusion test_mode where is not text$",
            ),
            "duplicate rule with a condition": (
                edit(events, lambda m: m["exclusions"][0].update(where="ts is null")),
                r"removes duplicate rows and must not give a where condition$",
            ),
            "duplicates removed twice": (
                edit(
                    events,
                    lambda m: m["exclusions"].append({**m["exclusions"][0], "name": "again"}),
                ),
                r"^model stg_events__events removes duplicate rows more than once$",
            ),
            "exclusion named twice": (
                edit(invoices, lambda m: m["exclusions"].append(dict(m["exclusions"][0]))),
                r"exclusion test_mode is named twice$",
            ),
            "total of a missing column": (
                edit(invoices, lambda m: m["totals"][0].update(column="amount")),
                r"adds up 'amount', which is not a column of the model$",
            ),
            "total tied to a missing raw column": (
                edit(invoices, lambda m: m["totals"][0].update(source_column="amount")),
                r"ties to 'amount', which is not a column of billing.invoices$",
            ),
            "grouping on one side only": (
                edit(invoices, lambda m: m["totals"][0].update(source_by="")),
                r"must give both by and source_by, or neither$",
            ),
            "model named twice": (
                lambda c: c["models"].append(copy.deepcopy(c["models"][0])),
                r"^model stg_billing__customers is named twice$",
            ),
            "four questions": (
                lambda c: c.update(questions=["a?", "b?", "c?", "d?"]),
                r"^the contract asks 4 questions; at most 3$",
            ),
            "blank assumption": (
                lambda c: c.update(assumptions=[""]),
                r"^assumptions 1 is not text$",
            ),
        }
        for label, (change, message) in mistakes.items():
            with self.subTest(label):
                self.assertRefused(change, message)

    def test_the_demo_contract_passes_against_the_profiler_s_own_profiles(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            generate(seed=42, companies=20, out=root / "raw", answers_out=root / "answers")
            profiles = profile_folder(root / "raw", root / "profiles")
        self.assertEqual(
            {name: sorted(columns) for name, columns in profile_columns(profiles).items()},
            {name: sorted(columns) for name, columns in profile_columns(PROFILES).items()},
        )
        check_contract(CONTRACT, profiles)

    def test_an_intermediate_model_may_take_columns_from_several_tables(self):
        contract = copy.deepcopy(CONTRACT)
        crosswalk = model(contract, "int_customers_matched_to_accounts")
        crosswalk["columns"][1]["from"] = ["crm.accounts.account_id", "billing.customers.email"]
        check_contract(contract, PROFILES)

    def test_a_total_without_grouping_passes(self):
        contract = copy.deepcopy(CONTRACT)
        total = model(contract, "stg_billing__invoices")["totals"][0]
        total.update(by="", source_by="")
        check_contract(contract, PROFILES)


if __name__ == "__main__":
    unittest.main()
