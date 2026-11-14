"""The diagnostician's answer: its shape, and that everything it cites is there."""

from __future__ import annotations

import unittest

import jsonschema

from tenx.diagnostics import (
    CAUSES,
    KINDS,
    SCHEMA,
)
from tenx.ports import CheckResult, ValidationReport

ACCOUNTS = "project/models/staging/stg_crm__accounts.sql"
FILES = {
    "PACKET.md": "# Diagnosis packet\n\n- build on stg_crm__accounts: error\n",
    "build/dbt.log": 'Binder Error: Referenced column "segment_name" not found in FROM clause!\n',
    "project/dbt_project.yml": "name: demo\n",
    ACCOUNTS: "select\n    account_id,\n    segment_name as segment\nfrom raw\n",
    "project/models/staging/staging.yml": "models:\n  - name: stg_crm__accounts\n",
}
REPORT = ValidationReport(
    (
        CheckResult("build", "project", "fail", "dbt build exited 1"),
        CheckResult("build", "stg_crm__accounts", "fail", "error"),
        CheckResult("rows", "stg_crm__accounts", "skip", "not checked"),
        CheckResult("key", "stg_billing__customers", "pass", "unique"),
    ),
    False,
    "the full report",
)


def fact(**changes) -> dict:
    item = {
        "claim": "dbt could not find the column segment_name.",
        "file": "build/dbt.log",
        "quote": 'Referenced column "segment_name"   not found',
    }
    return {**item, **changes}


def cause(**changes) -> dict:
    item = {
        "likelihood": "probable",
        "cause": "missing column",
        "model": "stg_crm__accounts",
        "file": ACCOUNTS,
        "line": 3,
        "claim": "The model reads segment_name, which the raw accounts do not have.",
        "evidence": "The log's binder error; the profile lists segment.",
        "explains": [
            {"check": "build", "subject": "stg_crm__accounts"},
            {"check": "rows", "subject": "stg_crm__accounts"},
        ],
    }
    return {**item, **changes}


def answer(**changes) -> dict:
    item = {
        "summary": "The accounts model names a column the source does not have.",
        "facts": [fact()],
        "causes": [cause()],
        "missing": [],
    }
    return {**item, **changes}


class SchemaTest(unittest.TestCase):
    def test_the_schema_is_valid_and_takes_a_good_answer(self):
        jsonschema.Draft202012Validator.check_schema(SCHEMA)
        jsonschema.validate(answer(), SCHEMA)

    def test_the_schema_lists_every_cause_and_other(self):
        enum = SCHEMA["properties"]["causes"]["items"]["properties"]["cause"]["enum"]
        self.assertEqual(enum, [*CAUSES, "other"])
        self.assertEqual(list(KINDS), enum)
        self.assertEqual(len(CAUSES), 8)


if __name__ == "__main__":
    unittest.main()
