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


def review(variant, kind, outcome, cost=0.05, problem="", dataset="s1-200"):
    return rates.Review(f"project-{variant}", dataset, variant, kind, outcome, cost, problem)


def tallies(caught, missed, rejected, approved):
    reviews = [review("d", "defect", rates.CAUGHT)] * caught
    reviews += [review("d", "defect", rates.MISSED)] * missed
    reviews += [review("g", "good", rates.REJECTED)] * rejected
    reviews += [review("g", "good", rates.APPROVED)] * approved
    return rates.summarise(reviews)


class RuleTest(unittest.TestCase):
    # Every boundary is worked out from the Beta distribution's closed form
    # when one count is 0: the limit is 1 - 0.025 ** (1 / (n + 1)) or its
    # mirror, for n reviews.
    def test_all_caught_none_rejected_is_useful(self):
        self.assertEqual(tallies(24, 0, 0, 24).verdict, rates.USEFUL)

    def test_false_rejection_upper_limit_at_the_boundary(self):
        # 0 of 12: upper limit 0.2470, within; 0 of 11: 0.2646, not.
        self.assertEqual(tallies(24, 0, 0, 12).verdict, rates.USEFUL)
        self.assertEqual(tallies(24, 0, 0, 11).verdict, rates.UNCLEAR)

    def test_catch_lower_limit_at_the_boundary(self):
        # 7 of 7: lower limit 0.6306, enough; 6 of 6: 0.5904, not.
        self.assertEqual(tallies(7, 0, 0, 24).verdict, rates.USEFUL)
        self.assertEqual(tallies(6, 0, 0, 24).verdict, rates.UNCLEAR)

    def test_harmful_when_rejections_are_surely_above_the_limit(self):
        # 2 of 2 rejected: lower limit 0.2924, above; 1 of 1: 0.1581, not.
        self.assertEqual(tallies(24, 0, 2, 0).verdict, rates.HARMFUL)
        self.assertEqual(tallies(24, 0, 1, 0).verdict, rates.UNCLEAR)

    def test_harmful_wins_over_a_good_catch_rate(self):
        self.assertEqual(tallies(24, 0, 12, 12).verdict, rates.HARMFUL)

    def test_nothing_reviewed_is_unclear(self):
        self.assertEqual(tallies(0, 0, 0, 0).verdict, rates.UNCLEAR)
        self.assertEqual(tallies(24, 0, 0, 0).verdict, rates.UNCLEAR)
        self.assertEqual(tallies(0, 0, 0, 24).verdict, rates.UNCLEAR)

    def test_elsewhere_is_not_a_catch(self):
        found = rates.summarise([review("d", "defect", rates.ELSEWHERE)] * 3)
        self.assertEqual((found.catches.passes, found.catches.runs), (0, 3))


class SummaryTest(unittest.TestCase):
    def setUp(self):
        self.summary = rates.summarise(
            [
                review("d_good", "good", rates.NO_REVIEW, None, "the reviewer crashed"),
                review("c_good", "good", rates.APPROVED, 0.07),
                review("b_defect", "defect", rates.MISSED, 0.06),
                review("a_defect", "defect", rates.CAUGHT, 0.05),
            ]
        )

    def test_counts_and_cost(self):
        found = self.summary
        self.assertEqual(found.counts[rates.NO_REVIEW], 1)
        self.assertEqual(found.scored, 3)
        self.assertAlmostEqual(found.cost_usd, 0.18)
        self.assertAlmostEqual(found.per_review, 0.06)
        self.assertAlmostEqual(found.per_catch, 0.18)

    def test_unknown_cost_makes_the_total_unknown(self):
        found = rates.summarise([review("a", "defect", rates.CAUGHT, None)])
        self.assertIsNone(found.cost_usd)
        self.assertIsNone(found.per_review)

    def test_report_text(self):
        # 1 of 2: Beta(2, 2), 3x^2 - 2x^3 = 0.025 at 0.094; 0 of 1: Beta(1, 2),
        # 1 - (1 - x)^2, 0.0126 and 0.8419.
        expected = (
            "Reviewer measurement: unclear\n"
            "\n"
            "Caught (blocking, right model and column): 1 of 2 (50%, 95% 9% to 91%)\n"
            "Defect projects with any request for changes: 1 of 2\n"
            "Good projects rejected: 0 of 1 (0%, 95% 1% to 84%)\n"
            "Rule: useful when the catch rate's lower limit is at least 60% and the "
            "false-rejection rate's upper limit at most 25%; harmful when the "
            "false-rejection rate's lower limit is above 25%.\n"
            "Cost: US$0.1800 for 3 reviews, US$0.0600 a review, US$0.1800 a catch "
            "(Claude Code's own estimates)\n"
            "\n"
            "  defect  a_defect                        s1-200      caught\n"
            "  defect  b_defect                        s1-200      missed\n"
            "  good    c_good                          s1-200      approved\n"
            "  good    d_good                          s1-200      no review (the reviewer crashed)\n"
        )
        self.assertEqual(rates.render(self.summary), expected)


if __name__ == "__main__":
    unittest.main()
