"""Tests for reading and checking the scouts' answers.

The profiles are handmade, holding only the fields the check reads, and the
expected TABLE_NOTES.md text is written out by hand.
"""

import copy
import json
import unittest
from pathlib import Path

import jsonschema

from tenx.scouts import SCHEMA, ScoutError, check_answer, notes_from_report, render

AGENT = Path(__file__).resolve().parent.parent / "agents" / "scout.md"

PROFILE = {
    "table": "crm.accounts",
    "columns": [{"name": "account_id"}, {"name": "billing_email"}, {"name": "created_date"}],
}
COLUMNS = {"account_id", "billing_email", "created_date"}

ANSWER = {
    "findings": [
        {
            "columns": ["billing_email"],
            "issue": "Addresses written in capitals\n and padded.",
            "evidence": "PROFILE.txt: 13 values with spaces. SAMPLE.jsonl: 'BILLING@COMPANY0034.EXAMPLE'.",
            "handling": "Trim and lower-case it in staging.",
        },
        {
            "columns": [],
            "issue": "No repeated rows.",
            "evidence": "PROFILE.txt: 180 rows, 180 distinct account_id.",
            "handling": "Nothing to remove.",
        },
        {
            "columns": ["account_id", "created_date"],
            "issue": "Text that must be converted with care.",
            "evidence": "PROFILE.txt: leading zeros; dates day/month/year.",
            "handling": "Keep account_id as text; parse dates as %d/%m/%Y.",
        },
    ],
    "questions": ["Is a prospect an account the goal wants?"],
}

RENDERED = """# Notes on each raw table

Notes from the scouts, one per table, worked out from each table's profile
and up to 20 of its rows before any model was built. A pattern seen only
in those rows is not proven for the whole table.

## `crm.accounts`

### `billing_email`: Addresses written in capitals and padded.

- Evidence: PROFILE.txt: 13 values with spaces. SAMPLE.jsonl: 'BILLING@COMPANY0034.EXAMPLE'.
- Handling: Trim and lower-case it in staging.

### Whole table: No repeated rows.

- Evidence: PROFILE.txt: 180 rows, 180 distinct account_id.
- Handling: Nothing to remove.

### `account_id`, `created_date`: Text that must be converted with care.

- Evidence: PROFILE.txt: leading zeros; dates day/month/year.
- Handling: Keep account_id as text; parse dates as %d/%m/%Y.

### Questions

- Is a prospect an account the goal wants?

## `events.events`

No findings.

## Tables without notes

- `billing.invoices`
"""


class Schema(unittest.TestCase):
    def test_accepts_the_example_answer(self):
        jsonschema.validate(ANSWER, SCHEMA)

    def test_rejects_extra_fields_and_a_missing_one(self):
        extra = copy.deepcopy(ANSWER)
        extra["findings"][0]["severity"] = "high"
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(extra, SCHEMA)
        missing = copy.deepcopy(ANSWER)
        del missing["findings"][0]["handling"]
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(missing, SCHEMA)

    def test_the_agent_is_asked_for_every_field_of_the_schema(self):
        body = AGENT.read_text(encoding="utf-8")
        fields = set(SCHEMA["properties"]) | set(
            SCHEMA["properties"]["findings"]["items"]["properties"]
        )
        self.assertEqual(
            fields, {"findings", "questions", "columns", "issue", "evidence", "handling"}
        )
        for field in fields:
            with self.subTest(field):
                self.assertIn(f"`{field}`", body)

    def test_the_schema_is_plain_json(self):
        self.assertEqual(json.loads(json.dumps(SCHEMA)), SCHEMA)


class CheckAnswer(unittest.TestCase):
    def test_a_good_answer_is_returned_unchanged(self):
        self.assertEqual(check_answer(copy.deepcopy(ANSWER), COLUMNS), ANSWER)

    def test_no_findings_and_no_questions_is_an_answer(self):
        empty = {"findings": [], "questions": []}
        self.assertEqual(check_answer(empty, COLUMNS), empty)

    def test_a_column_of_another_table_is_rejected(self):
        answer = copy.deepcopy(ANSWER)
        answer["findings"][2]["columns"] = ["account_id", "email"]
        with self.assertRaisesRegex(
            ScoutError, r"finding 3 names 'email', which is not a column of the table"
        ):
            check_answer(answer, COLUMNS)

    def test_a_column_named_twice_is_rejected(self):
        answer = copy.deepcopy(ANSWER)
        answer["findings"][0]["columns"] = ["billing_email", "billing_email"]
        with self.assertRaisesRegex(ScoutError, "finding 1 names a column more than once"):
            check_answer(answer, COLUMNS)

    def test_the_shape_is_checked_again(self):
        cases = [
            ([], "the answer is not a JSON object"),
            ({"findings": []}, "not findings and questions"),
            ({"findings": {}, "questions": []}, "findings and questions must be lists"),
            ({"findings": ["x"], "questions": []}, "finding 1 does not have the fields"),
            (
                {"findings": [dict(ANSWER["findings"][0], issue="  ")], "questions": []},
                "finding 1 issue is not text",
            ),
            (
                {
                    "findings": [dict(ANSWER["findings"][0], columns="billing_email")],
                    "questions": [],
                },
                "finding 1 columns must be a list of column names",
            ),
            (
                {"findings": [dict(ANSWER["findings"][0], columns=[1])], "questions": []},
                "finding 1 columns must be a list of column names",
            ),
            ({"findings": [], "questions": ["ok", ""]}, "question 2 is not text"),
        ]
        for answer, message in cases:
            with self.subTest(message), self.assertRaisesRegex(ScoutError, message):
                check_answer(answer, COLUMNS)


class FromReport(unittest.TestCase):
    def test_reads_the_structured_answer(self):
        report = {"subtype": "success", "structured_output": copy.deepcopy(ANSWER)}
        self.assertEqual(notes_from_report(report, PROFILE), ANSWER)

    def test_a_report_without_an_answer_is_rejected_with_how_it_ended(self):
        with self.assertRaisesRegex(ScoutError, "ended error_max_turns without an answer"):
            notes_from_report({"subtype": "error_max_turns"}, PROFILE)
        with self.assertRaisesRegex(ScoutError, "ended unknown without an answer"):
            notes_from_report({}, PROFILE)

    def test_no_report(self):
        with self.assertRaisesRegex(ScoutError, "the scout gave no report"):
            notes_from_report(None, PROFILE)

    def test_the_columns_are_those_of_the_scouts_own_table(self):
        other = {"table": "billing.customers", "columns": [{"name": "email"}]}
        report = {"structured_output": copy.deepcopy(ANSWER)}
        with self.assertRaisesRegex(ScoutError, "names 'billing_email'"):
            notes_from_report(report, other)


class Render(unittest.TestCase):
    def test_exact_text(self):
        self.assertEqual(
            render(
                [
                    ("crm.accounts", ANSWER),
                    ("events.events", {"findings": [], "questions": []}),
                    ("billing.invoices", None),
                ]
            ),
            RENDERED,
        )

    def test_every_table_with_notes(self):
        text = render([("crm.accounts", ANSWER)])
        self.assertNotIn("Tables without notes", text)
        self.assertTrue(text.endswith("- Is a prospect an account the goal wants?\n"))


if __name__ == "__main__":
    unittest.main()
