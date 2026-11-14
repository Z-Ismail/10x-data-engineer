"""The diagnostician's answer: its shape, and that everything it cites is there."""

from __future__ import annotations

import copy
import unittest

import jsonschema

from tenx.diagnostics import (
    CAUSES,
    KINDS,
    SCHEMA,
    DiagnosisError,
    check_answer,
    diagnosis_from_report,
    render,
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


class CheckAnswerTest(unittest.TestCase):
    def test_takes_a_good_answer(self):
        good = answer()
        self.assertIs(check_answer(good, FILES, REPORT), good)

    def test_takes_hypotheses_after_probable_causes_and_no_facts(self):
        second = cause(likelihood="hypothesis", cause="other", line=1)
        good = answer(facts=[], causes=[cause(), second, copy.deepcopy(second)])
        check_answer(good, FILES, REPORT)

    def test_takes_no_cause_when_it_says_what_is_missing(self):
        check_answer(answer(causes=[], missing=["The raw accounts file."]), FILES, REPORT)

    def test_a_cause_may_sit_in_a_model_no_check_names(self):
        # A broken staging model can fail only the crosswalk built from it.
        files = {**FILES, "project/models/stg_b.sql": "select 1\n"}
        check_answer(answer(causes=[cause(model="stg_b")]), files, REPORT)

    def test_refuses_an_answer_that_cites_what_is_not_there(self):
        hypothesis = cause(likelihood="hypothesis")
        cases = {
            "the answer is not a JSON object": [],
            "the answer has fields ['facts', 'summary'], not summary, facts, causes, missing": {
                "summary": "x",
                "facts": [],
            },
            "the summary is not text": answer(summary="  "),
            "facts must be a list": answer(facts={}),
            "fact 1 does not have the fields claim, file, quote": answer(facts=[{"claim": "x"}]),
            "fact 1 cites 'build/run_results.json', which is not a file in the packet": answer(
                facts=[fact(file="build/run_results.json")]
            ),
            "fact 1 quotes fewer than 8 characters": answer(facts=[fact(quote=" Binder ")]),
            "fact 1 quotes text that build/dbt.log does not have": answer(
                facts=[fact(quote='Referenced column "segment" not found')]
            ),
            "fact 1 quote is not text": answer(facts=[fact(quote=7)]),
            "cause 1 likelihood 'certain' is not one of probable, hypothesis": answer(
                causes=[cause(likelihood="certain")]
            ),
            "cause 1 cause 'typo' is not one of the listed causes": answer(
                causes=[cause(cause="typo")]
            ),
            "cause 1 names model 'stg_crm_accounts', which the project does not define": answer(
                causes=[cause(model="stg_crm_accounts")]
            ),
            "cause 1 cites 'build/dbt.log', which is not a project file in the packet": answer(
                causes=[cause(file="build/dbt.log")]
            ),
            "cause 1 cites 'project/models/x.sql', which is not a project file in the packet": (
                answer(causes=[cause(file="project/models/x.sql")])
            ),
            "cause 1 line is not a whole number": answer(causes=[cause(line=True)]),
            f"cause 1 cites line 5 of {ACCOUNTS}, which has 4 lines": answer(
                causes=[cause(line=5)]
            ),
            "cause 1 explains no check": answer(causes=[cause(explains=[])]),
            "cause 1 explains key on stg_billing__customers, which did not fail or go unrun": (
                answer(
                    causes=[cause(explains=[{"check": "key", "subject": "stg_billing__customers"}])]
                )
            ),
            "cause 1 explains build on stg_crm__accounts twice": answer(
                causes=[cause(explains=[{"check": "build", "subject": "stg_crm__accounts"}] * 2)]
            ),
            "cause 1 explains an item that does not have the fields check, subject": answer(
                causes=[cause(explains=[{"check": "build"}])]
            ),
            "the answer gives 6 causes, more than 5": answer(causes=[cause()] * 6),
            "a probable cause comes after a hypothesis": answer(causes=[hypothesis, cause()]),
            "missing item 1 is not text": answer(missing=[""]),
            "the answer names no cause and says nothing is missing": answer(causes=[]),
        }
        for reason, bad in cases.items():
            with self.subTest(reason=reason):
                with self.assertRaises(DiagnosisError) as raised:
                    check_answer(bad, FILES, REPORT)
                self.assertEqual(str(raised.exception), reason)


class FromReportTest(unittest.TestCase):
    def test_reads_the_structured_output(self):
        good = answer()
        report = {"subtype": "success", "structured_output": good}
        self.assertIs(diagnosis_from_report(report, FILES, REPORT), good)

    def test_refuses_no_report_or_no_answer(self):
        cases = {
            "the diagnostician gave no report": None,
            "the diagnostician ended error_max_turns without an answer": {
                "subtype": "error_max_turns"
            },
            "the diagnostician ended unknown without an answer": {},
        }
        for reason, report in cases.items():
            with self.subTest(reason=reason):
                with self.assertRaises(DiagnosisError) as raised:
                    diagnosis_from_report(report, FILES, REPORT)
                self.assertEqual(str(raised.exception), reason)

    def test_checks_the_answer_it_reads(self):
        report = {"structured_output": answer(causes=[cause(line=9)])}
        with self.assertRaisesRegex(DiagnosisError, "cites line 9"):
            diagnosis_from_report(report, FILES, REPORT)


class RenderTest(unittest.TestCase):
    def test_causes_in_rank_then_facts_and_what_is_missing(self):
        second = cause(
            likelihood="hypothesis",
            cause="other",
            line=1,
            claim="The raw\nfile may   have changed.",
            explains=[{"check": "build", "subject": "project"}],
        )
        diagnosis = answer(causes=[cause(), second], missing=["The raw accounts file."])
        self.assertEqual(
            render(diagnosis),
            "# Diagnosis\n"
            "\n"
            "Advice from the diagnostician on why the project failed the checks. It changes\n"
            "no file and no verdict.\n"
            "\n"
            "The accounts model names a column the source does not have.\n"
            "\n"
            "## Probable causes\n"
            "\n"
            f"1. `{ACCOUNTS}:3`, `stg_crm__accounts`, missing column: The model reads"
            " segment_name, which the raw accounts do not have.\n"
            "   Evidence: The log's binder error; the profile lists segment.\n"
            "   Explains: build on stg_crm__accounts, rows on stg_crm__accounts.\n"
            "\n"
            "## Hypotheses\n"
            "\n"
            f"1. `{ACCOUNTS}:1`, `stg_crm__accounts`, other: The raw file may have changed.\n"
            "   Evidence: The log's binder error; the profile lists segment.\n"
            "   Explains: build on project.\n"
            "\n"
            "## Confirmed facts\n"
            "\n"
            "- dbt could not find the column segment_name.\n"
            '  Quoted from `build/dbt.log`: Referenced column "segment_name" not found\n'
            "\n"
            "## Missing information\n"
            "\n"
            "- The raw accounts file.\n",
        )

    def test_without_causes_or_facts(self):
        text = render(answer(facts=[], causes=[], missing=["The log."]))
        self.assertIn("\n\nNo cause was named.\n\n## Missing information\n\n- The log.\n", text)
        self.assertNotIn("## Confirmed facts", text)


if __name__ == "__main__":
    unittest.main()
