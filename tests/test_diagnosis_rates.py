"""Scoring diagnoses against the bed's known causes, the rule and the report."""

import unittest

from tenx import diagnosis_rates as rates
from tenx.compare import Tally

TRUTH = {
    "variant": "refunds_dropped",
    "cause": "row filter",
    "origin": "planted",
    "model": "stg_billing__invoices",
}


def cause(kind: str, model: str, likelihood: str = "probable") -> dict:
    return {
        "likelihood": likelihood,
        "cause": kind,
        "model": model,
        "file": f"project/models/staging/{model}.sql",
        "line": 3,
        "claim": "c",
        "evidence": "e",
        "explains": [{"check": "rows", "subject": model}],
    }


def answer(*causes: dict, missing: tuple[str, ...] = ()) -> dict:
    return {"summary": "s", "facts": [], "causes": list(causes), "missing": list(missing)}


class ScoreTest(unittest.TestCase):
    def test_each_outcome(self):
        cases = {
            rates.RIGHT: answer(cause("row filter", "stg_billing__invoices")),
            rates.MODEL_ONLY: answer(cause("deduplication", "stg_billing__invoices")),
            rates.CAUSE_ONLY: answer(cause("row filter", "stg_billing__customers")),
            rates.WRONG: answer(cause("type", "stg_crm__accounts")),
            rates.NO_CAUSE: answer(missing=("the raw rows",)),
        }
        for outcome, given in cases.items():
            with self.subTest(outcome=outcome):
                self.assertEqual(rates.score(given, TRUTH), outcome)
        self.assertEqual(rates.score(None, TRUTH), rates.NO_DIAGNOSIS)

    def test_the_model_is_compared_without_case_or_padding(self):
        given = answer(cause("row filter", " STG_Billing__Invoices "))
        self.assertEqual(rates.score(given, TRUTH), rates.RIGHT)

    def test_only_the_first_cause_counts_and_listed_sees_the_rest(self):
        given = answer(
            cause("type", "stg_billing__invoices"),
            cause("row filter", "stg_billing__invoices", "hypothesis"),
        )
        self.assertEqual(rates.score(given, TRUTH), rates.MODEL_ONLY)
        self.assertTrue(rates.listed(given, TRUTH))
        self.assertFalse(rates.listed(answer(cause("type", "stg_billing__invoices")), TRUTH))
        self.assertFalse(rates.listed(None, TRUTH))

    def test_a_diagnosis_of_hypotheses_only_is_scored_by_its_first(self):
        given = answer(cause("row filter", "stg_billing__invoices", "hypothesis"))
        self.assertEqual(rates.score(given, TRUTH), rates.RIGHT)

    def test_other_is_never_the_known_cause(self):
        given = answer(cause("other", "stg_billing__invoices"))
        self.assertEqual(rates.score(given, TRUTH), rates.MODEL_ONLY)
        self.assertFalse(rates.listed(given, TRUTH))

    def test_answers_that_cannot_be_scored_are_refused(self):
        for change, message in (
            ({"cause": "other"}, "one of the listed causes"),
            ({"cause": "typo"}, "one of the listed causes"),
            ({"model": " "}, "must be a name"),
            ({"origin": "seen"}, "origin must be"),
        ):
            with self.subTest(change=change), self.assertRaisesRegex(rates.RatesError, message):
                rates.score(None, {**TRUTH, **change})


class VerdictTest(unittest.TestCase):
    # Beta(1 + k, 1 + n - k): with all n right the lower limit is
    # 0.025 ** (1 / (n + 1)), and with none right the upper limit is
    # 1 - 0.025 ** (1 / (n + 1)).
    def test_all_right_is_useful_from_seven_runs(self):
        self.assertEqual(rates.verdict(Tally(6, 0)), rates.UNCLEAR)  # lower 0.590
        self.assertEqual(rates.verdict(Tally(7, 0)), rates.USEFUL)  # lower 0.631
        self.assertEqual(rates.verdict(Tally(52, 0)), rates.USEFUL)

    def test_none_right_is_weak_from_four_runs(self):
        self.assertEqual(rates.verdict(Tally(0, 3)), rates.UNCLEAR)  # upper 0.602
        self.assertEqual(rates.verdict(Tally(0, 4)), rates.WEAK)  # upper 0.522

    def test_no_runs_is_unclear(self):
        self.assertEqual(rates.verdict(Tally(0, 0)), rates.UNCLEAR)


DIAGNOSES = [
    rates.Diagnosis("project-02", "s1", "b_variant", "planted", rates.MODEL_ONLY, True, True, 0.03),
    rates.Diagnosis("project-01", "s1", "a_variant", "observed", rates.RIGHT, True, True, 0.04),
    rates.Diagnosis(
        "project-02",
        "s2",
        "b_variant",
        "planted",
        rates.NO_DIAGNOSIS,
        called=False,
        problem="the project passed every check",
    ),
]

# 1 of 3 right, Beta(2, 3): CDF 6x^2 - 8x^3 + 3x^4, limits 0.068 and 0.806.
# Observed 1 of 1, Beta(2, 1): sqrt(0.025) and sqrt(0.975), 0.158 and 0.987.
# Planted 0 of 2, Beta(1, 3): 1 - 0.975^(1/3) and 1 - 0.025^(1/3), 0.008 and 0.708.
REPORT = """Diagnosis measurement: unclear

Right (first cause names the cause and the model): 1 of 3 (33%, 95% 7% to 81%)
  observed in live runs: 1 of 1 (100%, 95% 16% to 99%)
  planted: 0 of 2 (0%, 95% 1% to 71%)
Otherwise: model only 1, cause only 0, wrong 0, no cause 0, no diagnosis 1
The known cause anywhere in the diagnosis: 2 of 3
Variants right on every dataset: 1 of 2
Rule: useful when the accuracy's lower limit is at least 60%; weak when its upper limit is \
below 60%. Runs of one variant are not independent, so read the variants below as well.
Cost: US$0.0700 for 3 runs, US$0.0233 a run, US$0.0700 a right diagnosis \
(Claude Code's own estimates)

  a_variant                     observed  1 of 1
  b_variant                     planted   0 of 2

  a_variant                     s1          right
  b_variant                     s1          model only
  b_variant                     s2          no diagnosis (the project passed every check)
"""


class SummaryTest(unittest.TestCase):
    def test_counts_rates_and_report(self):
        summary = rates.summarise(DIAGNOSES)
        self.assertEqual(summary.accuracy, Tally(1, 2))
        self.assertEqual(summary.by_origin, {"observed": Tally(1, 0), "planted": Tally(0, 2)})
        self.assertEqual(summary.listed, 2)
        self.assertEqual(
            summary.variants(), [("a_variant", "observed", 1, 1), ("b_variant", "planted", 0, 2)]
        )
        self.assertAlmostEqual(summary.cost_usd, 0.07)
        self.assertEqual(rates.render(summary), REPORT)

    def test_a_run_without_a_diagnosis_is_counted_against_the_agent(self):
        summary = rates.summarise(DIAGNOSES[1:])
        self.assertEqual(summary.accuracy, Tally(1, 1))

    def test_an_unknown_cost_of_a_call_makes_the_total_unknown(self):
        unknown = rates.Diagnosis("p", "s3", "c", "planted", rates.WRONG, False, True, None)
        summary = rates.summarise([*DIAGNOSES, unknown])
        self.assertIsNone(summary.cost_usd)
        self.assertIn(
            "Cost: unknown for 4 runs, unknown a run, unknown a right", rates.render(summary)
        )


if __name__ == "__main__":
    unittest.main()
