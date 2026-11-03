"""The repair loop's report for the engineer and its rule for trying again."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from demo.generate import generate
from tenx.adapters.duckdb import DuckDBGate
from tenx.domain import Failure, ValidationResult
from tenx.repair import MAX_REPAIRS, decide, render
from tenx.sandbox import Unisolated

FIXTURES = Path(__file__).parent / "fixtures"
CROSSWALK = Path("models/intermediate/int_customers_matched_to_accounts.sql")


def verdict(checks: int, *failures: Failure) -> ValidationResult:
    return ValidationResult(validator="gate", passed=not failures, checks=checks, failures=failures)


ROWS = Failure(
    check="rows",
    subject="stg_billing__invoices",
    status="fail",
    feedback="12 rows in billing.invoices, 2 excluded (test_mode 2), 10 expected, "
    "11 in the model: 1 more than expected",
)
EXPECTED = Failure(
    check="expected",
    subject="int_customers_matched_to_accounts",
    status="fail",
    feedback="customer_id, account_id: 5 rows expected and 4 in the model, of which 0 not "
    "expected and 1 expected but missing\n(the rows themselves are not shown)",
)
QUIET = Failure(check="key", subject="stg_crm__accounts", status="fail")
SKIPPED = Failure(check="repeat", subject="project", status="skip")


class RenderTest(unittest.TestCase):
    def test_the_report_for_failures_with_and_without_feedback(self):
        text = render(verdict(24, ROWS, EXPECTED, QUIET, SKIPPED))
        self.assertEqual(
            text,
            "# What the checks found\n"
            "\n"
            "Your previous attempt was built from an empty database and checked after you\n"
            "finished. 3 checks failed, 20 passed and 1 could not run.\n"
            "The project in this folder is that attempt. Find the cause of each failure\n"
            "and fix it in the models, for every row it affects, not only for the rows or\n"
            "values named here, and run `dbt build` again before you stop.\n"
            "\n"
            "## rows: stg_billing__invoices\n"
            "\n"
            "12 rows in billing.invoices, 2 excluded (test_mode 2), 10 expected, "
            "11 in the model: 1 more than expected\n"
            "\n"
            "## expected: int_customers_matched_to_accounts\n"
            "\n"
            "customer_id, account_id: 5 rows expected and 4 in the model, of which 0 not "
            "expected and 1 expected but missing\n"
            "(the rows themselves are not shown)\n"
            "\n"
            "## key: stg_crm__accounts\n"
            "\n"
            "Failed. No more is said about this check.\n"
            "\n"
            "## Could not run\n"
            "\n"
            "- repeat: project\n",
        )

    def test_one_failure_and_nothing_skipped(self):
        text = render(verdict(1, QUIET))
        self.assertIn("finished. 1 check failed, 0 passed.\n", text)
        self.assertNotIn("Could not run", text)

    def test_a_pass_has_nothing_to_repair(self):
        with self.assertRaises(ValueError):
            render(verdict(3))


class DecideTest(unittest.TestCase):
    def test_a_pass_ends_the_loop(self):
        self.assertEqual(decide([verdict(2, QUIET), verdict(2)], 2).reason, "passed")
        self.assertFalse(decide([verdict(2)], 2).repair)

    def test_no_repairs_asked_for(self):
        decision = decide([verdict(2, QUIET)], 0)
        self.assertEqual((decision.repair, decision.reason), (False, "no repairs were asked for"))

    def test_the_first_failure_is_repaired_when_repairs_were_asked_for(self):
        decision = decide([verdict(2, QUIET)], 1)
        self.assertEqual((decision.repair, decision.reason), (True, "repair 1 of 1"))

    def test_the_cap_stops_the_loop_exactly_when_reached(self):
        three = [verdict(9, ROWS, EXPECTED, QUIET), verdict(9, ROWS, EXPECTED), verdict(9, ROWS)]
        self.assertTrue(decide(three[:2], 2).repair)
        self.assertEqual(decide(three[:2], 2).reason, "repair 2 of 2")
        stop = decide(three, 2)
        self.assertEqual((stop.repair, stop.reason), (False, "the repairs allowed (2) were made"))
        self.assertTrue(decide(three, 3).repair)

    def test_a_repair_that_fails_as_many_checks_ends_the_loop(self):
        same = decide([verdict(9, EXPECTED), verdict(9, ROWS)], 3)
        self.assertEqual(
            (same.repair, same.reason),
            (
                False,
                (
                    "repair 1 left 1 check failing or not run, against 1 before it, "
                    "so no further repair was made"
                ),
            ),
        )
        worse = decide([verdict(9, EXPECTED), verdict(9, ROWS, SKIPPED)], 3)
        self.assertFalse(worse.repair)

    def test_a_build_that_now_runs_counts_as_progress(self):
        broken = verdict(
            9,
            Failure(check="build", subject="project", status="fail"),
            *[Failure(check="rows", subject=f"m{n}", status="skip") for n in range(8)],
        )
        self.assertTrue(decide([broken, verdict(9, EXPECTED)], 2).repair)

    def test_nothing_validated_is_a_fault(self):
        with self.assertRaises(ValueError):
            decide([], 1)

    def test_the_cap_is_the_designs_three(self):
        self.assertEqual(MAX_REPAIRS, 3)


class RealGateTest(unittest.TestCase):
    """The gate's own report names the missing rows; the engineer's never does."""

    def test_a_whole_address_crosswalk(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            generate(11, 60, root / "data", root / "answers")
            project = root / "project"
            shutil.copytree(FIXTURES / "demo_project", project)
            (project / CROSSWALK).write_text(
                "select customers.customer_id, accounts.account_id\n"
                "from {{ ref('stg_billing__customers') }} as customers\n"
                "left join {{ ref('stg_crm__accounts') }} as accounts\n"
                "    on customers.email = accounts.billing_email\n",
                encoding="utf-8",
            )
            declaration = FIXTURES / "demo_gate.json"
            report = DuckDBGate().validate(
                project, root / "data", declaration, root / "gate", Unisolated()
            )
        verdict = ValidationResult.of("gate", report)
        self.assertEqual(
            [(f.check, f.subject) for f in verdict.failures],
            [("expected", "int_customers_matched_to_accounts")],
        )
        # Positive control: the gate's own report lists rows of the answer.
        self.assertRegex(report.text, r"missing: \(cus_\w+, 001\d{15}\)")
        text = render(verdict)
        self.assertRegex(text, r"\d+ rows expected and \d+ in the model, of which \d+ not")
        self.assertIn("(the rows themselves are not shown)", text)
        for leak in ("cus_", "001000", "missing:", "not expected:"):
            self.assertNotIn(leak, text)
        for model in json.loads(declaration.read_text(encoding="utf-8"))["models"]:
            if "expected" in model:
                self.assertNotIn(model["expected"]["query"][:40], text)


if __name__ == "__main__":
    unittest.main()
