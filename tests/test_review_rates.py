import unittest

from tenx import review_rates as rates

SWAP = {
    "kind": "defect",
    "columns": [["stg_events__events", "user_email"], ["stg_events__events", "event_name"]],
}
GOOD = {"kind": "good", "columns": []}


def finding(model, column, severity="blocking"):
    return {
        "severity": severity,
        "model": model,
        "column": column,
        "file": f"models/staging/{model}.sql",
        "line": 3,
        "claim": "c",
        "evidence": "e",
    }


def answer(*findings):
    blocking = any(item["severity"] == "blocking" for item in findings)
    verdict = "request changes" if blocking else "approve"
    return {"verdict": verdict, "summary": "s", "findings": list(findings)}


class ScoreTest(unittest.TestCase):
    def test_blocking_finding_on_a_defect_column_is_a_catch(self):
        found = answer(finding("stg_events__events", "event_name"))
        self.assertEqual(rates.score(found, SWAP), rates.CAUGHT)

    def test_case_and_spaces_are_ignored_in_the_place(self):
        found = answer(finding(" STG_Events__Events", "User_Email "))
        self.assertEqual(rates.score(found, SWAP), rates.CAUGHT)

    def test_right_model_other_column_is_elsewhere(self):
        found = answer(finding("stg_events__events", "occurred_at"))
        self.assertEqual(rates.score(found, SWAP), rates.ELSEWHERE)

    def test_right_column_name_in_another_model_is_elsewhere(self):
        found = answer(finding("stg_billing__customers", "event_name"))
        self.assertEqual(rates.score(found, SWAP), rates.ELSEWHERE)

    def test_advisory_on_the_defect_column_with_approve_is_missed(self):
        found = answer(finding("stg_events__events", "user_email", "advisory"))
        self.assertEqual(found["verdict"], "approve")
        self.assertEqual(rates.score(found, SWAP), rates.MISSED)

    def test_advisory_on_the_column_beside_a_blocking_elsewhere_is_elsewhere(self):
        found = answer(
            finding("stg_events__events", "user_email", "advisory"),
            finding("stg_crm__accounts", "created_date"),
        )
        self.assertEqual(rates.score(found, SWAP), rates.ELSEWHERE)

    def test_approve_without_findings_is_missed(self):
        self.assertEqual(rates.score(answer(), SWAP), rates.MISSED)

    def test_good_project(self):
        self.assertEqual(rates.score(answer(), GOOD), rates.APPROVED)
        advisory = answer(finding("stg_events__events", "user_email", "advisory"))
        self.assertEqual(rates.score(advisory, GOOD), rates.APPROVED)
        blocking = answer(finding("stg_events__events", "user_email"))
        self.assertEqual(rates.score(blocking, GOOD), rates.REJECTED)

    def test_no_answer_is_no_review(self):
        self.assertEqual(rates.score(None, SWAP), rates.NO_REVIEW)
        self.assertEqual(rates.score(None, GOOD), rates.NO_REVIEW)

    def test_bad_answers_are_refused(self):
        cases = [
            ({"kind": "broken"}, "kind must be"),
            ({"kind": "defect"}, "at least one"),
            ({"kind": "defect", "columns": []}, "at least one"),
            ({"kind": "defect", "columns": [["m"]]}, "must be [model, column]"),
            ({"kind": "defect", "columns": [["m", " "]]}, "must be [model, column]"),
            ({"kind": "defect", "columns": [["m", 3]]}, "must be [model, column]"),
        ]
        for truth, reason in cases:
            with self.subTest(truth=truth), self.assertRaises(rates.RatesError) as raised:
                rates.score(None, truth)
            self.assertIn(reason, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
