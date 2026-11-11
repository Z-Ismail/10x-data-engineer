"""The reviewer's answer, checked against the project files in its packet."""

from __future__ import annotations

import copy
import unittest

import jsonschema

from tenx.reviewer import (
    SCHEMA,
    ReviewError,
    check_answer,
    models_in,
    render,
    review_from_report,
)

CUSTOMERS = "models/staging/stg_customers.sql"
FILES = {
    "dbt_project.yml": "name: shop\n",
    CUSTOMERS: "select\n    id as customer_id,\n    email as contact_name\nfrom raw\n",
    "models/staging/schema.yml": "version: 2\n",
    "models/marts/orders.py": "def model(dbt, session):\n    return None\n",
    "macros/clean.sql": "{% macro clean(x) %}{{ x }}{% endmacro %}\n",
}


def finding(**changes) -> dict:
    found = {
        "severity": "blocking",
        "model": "stg_customers",
        "column": "contact_name",
        "file": CUSTOMERS,
        "line": 3,
        "claim": "The contact name is the email address.",
        "evidence": "Line 3 selects email as contact_name; the profile has a name column.",
    }
    return {**found, **changes}


def answer(*findings, verdict="request changes") -> dict:
    return {"verdict": verdict, "summary": "One column is wrong.", "findings": list(findings)}


class ModelsTest(unittest.TestCase):
    def test_models_are_the_sql_and_python_files_under_models(self):
        self.assertEqual(
            models_in(FILES),
            {"orders": "models/marts/orders.py", "stg_customers": CUSTOMERS},
        )


class CheckAnswerTest(unittest.TestCase):
    def test_a_good_answer_is_kept_as_it_is(self):
        given = answer(finding(), finding(severity="advisory", column="", line=1))
        self.assertEqual(check_answer(copy.deepcopy(given), FILES), given)

    def test_an_approval_with_no_findings_is_kept(self):
        given = answer(verdict="approve")
        self.assertEqual(check_answer(given, FILES), given)

    def test_a_column_is_found_in_any_case_and_quoted(self):
        files = {CUSTOMERS: 'select "Contact_Name" from raw\n'}
        check_answer(answer(finding(line=1)), files)

    def test_the_schema_accepts_a_good_answer_and_is_valid(self):
        jsonschema.Draft202012Validator.check_schema(SCHEMA)
        jsonschema.validate(answer(finding()), SCHEMA)

    def assertRefused(self, given, reason: str) -> None:
        with self.assertRaises(ReviewError) as caught:
            check_answer(given, FILES)
        self.assertEqual(str(caught.exception), reason)

    def test_each_mistake_is_refused_with_the_reason(self):
        cases = [
            (["not", "a", "dict"], "the answer is not a JSON object"),
            (
                {"verdict": "approve", "findings": []},
                "the answer has fields ['findings', 'verdict'], not verdict, summary, findings",
            ),
            (answer(verdict="merge"), "verdict 'merge' is not one of approve, request changes"),
            ({**answer(verdict="approve"), "summary": " "}, "the summary is not text"),
            ({**answer(verdict="approve"), "findings": {}}, "findings must be a list"),
            (
                answer({"severity": "blocking"}),
                (
                    "finding 1 does not have the fields "
                    "severity, model, column, file, line, claim, evidence"
                ),
            ),
            (
                answer(finding(severity="minor")),
                "finding 1 severity 'minor' is not one of blocking, advisory",
            ),
            (answer(finding(claim="")), "finding 1 claim is not text"),
            (answer(finding(column=None)), "finding 1 column is not text"),
            (
                answer(finding(model="stg_orders")),
                "finding 1 names model 'stg_orders', which the project does not define",
            ),
            (
                answer(finding(model="clean")),
                "finding 1 names model 'clean', which the project does not define",
            ),
            (
                answer(finding(file="project/" + CUSTOMERS)),
                f"finding 1 cites 'project/{CUSTOMERS}', which is not a project file in the packet",
            ),
            (answer(finding(line="3")), "finding 1 line is not a whole number"),
            (answer(finding(line=True)), "finding 1 line is not a whole number"),
            (answer(finding(line=0)), f"finding 1 cites line 0 of {CUSTOMERS}, which has 4 lines"),
            (answer(finding(line=5)), f"finding 1 cites line 5 of {CUSTOMERS}, which has 4 lines"),
            (
                answer(finding(column="contact")),
                f"finding 1 names column 'contact', which {CUSTOMERS} does not mention",
            ),
            (
                answer(finding(), finding(column="phone")),
                f"finding 2 names column 'phone', which {CUSTOMERS} does not mention",
            ),
            (
                answer(finding(), verdict="approve"),
                "the verdict is 'approve' with blocking findings",
            ),
            (
                answer(finding(severity="advisory")),
                "the verdict is 'request changes' with no blocking finding",
            ),
        ]
        for given, reason in cases:
            with self.subTest(reason=reason):
                self.assertRefused(given, reason)


class ReportTest(unittest.TestCase):
    def test_the_answer_is_read_from_the_structured_output(self):
        given = answer(verdict="approve")
        report = {"subtype": "success", "structured_output": given}
        self.assertEqual(review_from_report(report, FILES), given)

    def test_no_report_and_no_answer_are_refused(self):
        with self.assertRaisesRegex(ReviewError, "^the reviewer gave no report$"):
            review_from_report(None, FILES)
        with self.assertRaisesRegex(
            ReviewError, "^the reviewer ended error_max_turns without an answer$"
        ):
            review_from_report({"subtype": "error_max_turns"}, FILES)


class RenderTest(unittest.TestCase):
    def test_findings_are_listed_by_severity_with_their_evidence(self):
        given = answer(
            finding(severity="advisory", column="", line=1, claim="Emails  are\nnot trimmed."),
            finding(),
        )
        expected = (
            "# Review\n"
            "\n"
            "Advice from the reviewer on a project that passed every check. It does not\n"
            "change the checks' verdict.\n"
            "\n"
            "Verdict: request changes\n"
            "\n"
            "One column is wrong.\n"
            "\n"
            "## Blocking\n"
            "\n"
            "- `models/staging/stg_customers.sql:3`, `stg_customers.contact_name`: "
            "The contact name is the email address.\n"
            "  Evidence: Line 3 selects email as contact_name; the profile has a name column.\n"
            "\n"
            "## Advisory\n"
            "\n"
            "- `models/staging/stg_customers.sql:1`, `stg_customers`: Emails are not trimmed.\n"
            "  Evidence: Line 3 selects email as contact_name; the profile has a name column.\n"
        )
        self.assertEqual(render(given), expected)

    def test_an_approval_with_no_findings_says_so(self):
        given = {**answer(verdict="approve"), "summary": "Nothing is wrong."}
        expected = (
            "# Review\n"
            "\n"
            "Advice from the reviewer on a project that passed every check. It does not\n"
            "change the checks' verdict.\n"
            "\n"
            "Verdict: approve\n"
            "\n"
            "Nothing is wrong.\n"
            "\n"
            "No findings.\n"
        )
        self.assertEqual(render(given), expected)


if __name__ == "__main__":
    unittest.main()
