"""Tests for reading and checking the cross-source pass's answer.

The profiles are handmade, holding only the fields the check reads, and the
expected JOINS.md text is written out by hand.
"""

import copy
import json
import unittest
from pathlib import Path

import jsonschema

from tenx.cross_source import (
    SCHEMA,
    NotesError,
    check_answer,
    notes_from_report,
    profile_columns,
    render,
)

AGENT = Path(__file__).resolve().parent.parent / "agents" / "cross-source.md"

PROFILES = [
    {"table": "billing.customers", "columns": [{"name": "id"}, {"name": "email"}]},
    {"table": "crm.accounts", "columns": [{"name": "account_id"}, {"name": "billing_email"}]},
]

ANSWER = {
    "joins": [
        {
            "left": "billing.customers.email",
            "right": "crm.accounts.billing_email",
            "prepare": "Trim and lower-case both sides,\n  then compare the part after @.",
            "cardinality": "many-to-one",
            "unmatched": "3 billing rows: test-mode addresses.",
            "evidence": "overlap.txt: 119 of 150 values found.",
        }
    ],
    "cautions": ["Check that no two accounts share a domain."],
}

RENDERED = """# How the raw tables join

Notes from the cross-source pass, worked out from the source profiles and the
overlap report before any model was built.

## `billing.customers.email` to `crm.accounts.billing_email`

- Prepare: Trim and lower-case both sides, then compare the part after @.
- Cardinality, left to right: many-to-one
- Unmatched: 3 billing rows: test-mode addresses.
- Evidence: overlap.txt: 119 of 150 values found.

## Cautions

- Check that no two accounts share a domain.
"""


def report(answer) -> dict:
    return {"type": "result", "subtype": "success", "structured_output": answer}


class ProfileColumnsTest(unittest.TestCase):
    def test_names_every_column_with_its_table(self):
        self.assertEqual(
            profile_columns(PROFILES),
            {
                "billing.customers.id",
                "billing.customers.email",
                "crm.accounts.account_id",
                "crm.accounts.billing_email",
            },
        )


class SchemaTest(unittest.TestCase):
    def test_accepts_the_example_answer(self):
        jsonschema.validate(ANSWER, SCHEMA)

    def test_rejects_an_unknown_cardinality_and_extra_fields(self):
        wrong = copy.deepcopy(ANSWER)
        wrong["joins"][0]["cardinality"] = "several"
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(wrong, SCHEMA)
        extra = dict(ANSWER, confidence="high")
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(extra, SCHEMA)

    def test_the_agent_is_asked_for_every_field_of_the_schema(self):
        body = AGENT.read_text(encoding="utf-8")
        fields = list(SCHEMA["properties"]) + list(
            SCHEMA["properties"]["joins"]["items"]["properties"]
        )
        for field in fields:
            with self.subTest(field):
                self.assertIn(f"`{field}`", body)


class CheckAnswerTest(unittest.TestCase):
    columns = profile_columns(PROFILES)

    def rejected(self, answer, message: str):
        with self.assertRaisesRegex(NotesError, message):
            check_answer(answer, self.columns)

    def test_a_good_answer_is_returned_unchanged(self):
        self.assertEqual(check_answer(copy.deepcopy(ANSWER), self.columns), ANSWER)

    def test_no_joins_and_no_cautions_is_an_answer(self):
        empty = {"joins": [], "cautions": []}
        self.assertEqual(check_answer(empty, self.columns), empty)

    def test_a_column_missing_from_the_profiles_is_rejected(self):
        wrong = copy.deepcopy(ANSWER)
        wrong["joins"][0]["right"] = "crm.accounts.email"
        self.rejected(wrong, "join 1 names 'crm.accounts.email', which is not a column")
        bare = copy.deepcopy(ANSWER)
        bare["joins"][0]["left"] = "email"
        self.rejected(bare, "join 1 names 'email'")

    def test_a_column_joined_to_itself_is_rejected(self):
        wrong = copy.deepcopy(ANSWER)
        wrong["joins"][0]["right"] = wrong["joins"][0]["left"]
        self.rejected(wrong, "joins 'billing.customers.email' to itself")

    def test_the_shape_is_checked_again(self):
        self.rejected(["joins"], "not a JSON object")
        self.rejected({"joins": []}, "not joins and cautions")
        self.rejected({"joins": {}, "cautions": []}, "must be lists")
        missing = copy.deepcopy(ANSWER)
        del missing["joins"][0]["evidence"]
        self.rejected(missing, "join 1 does not have the fields")
        wrong = copy.deepcopy(ANSWER)
        wrong["joins"][0]["cardinality"] = "several"
        self.rejected(wrong, "cardinality 'several' is not one of")
        blank = copy.deepcopy(ANSWER)
        blank["joins"][0]["prepare"] = "  "
        self.rejected(blank, "join 1 prepare is not text")
        self.rejected({"joins": [], "cautions": [3]}, "caution 1 is not text")


class NotesFromReportTest(unittest.TestCase):
    def test_reads_the_structured_answer(self):
        self.assertEqual(notes_from_report(report(ANSWER), PROFILES), ANSWER)

    def test_a_report_without_an_answer_is_rejected_with_how_it_ended(self):
        with self.assertRaisesRegex(NotesError, "ended error_max_turns without an answer"):
            notes_from_report({"subtype": "error_max_turns", "result": "..."}, PROFILES)
        with self.assertRaisesRegex(NotesError, "gave no report"):
            notes_from_report(None, PROFILES)

    def test_an_answer_naming_an_unknown_column_is_rejected(self):
        wrong = copy.deepcopy(ANSWER)
        wrong["joins"][0]["left"] = "events.events.user_email"
        with self.assertRaisesRegex(NotesError, "events.events.user_email"):
            notes_from_report(report(wrong), PROFILES)


class RenderTest(unittest.TestCase):
    def test_exact_text(self):
        self.assertEqual(render(ANSWER), RENDERED)

    def test_no_joins(self):
        self.assertEqual(
            render({"joins": [], "cautions": []}),
            "# How the raw tables join\n\n"
            "Notes from the cross-source pass, worked out from the source profiles and the\n"
            "overlap report before any model was built.\n\n"
            "No joins were proposed.\n",
        )

    def test_the_schema_is_plain_json(self):
        self.assertEqual(json.loads(json.dumps(SCHEMA)), SCHEMA)


if __name__ == "__main__":
    unittest.main()
