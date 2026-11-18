"""Scoring the fix measurement: pairs, the rule and the report."""

import unittest
from statistics import NormalDist

from tenx.fix_rates import (
    HELPS,
    HURTS,
    NO_GAIN,
    UNCLEAR,
    WITH,
    WITHOUT,
    Pair,
    count,
    render,
    summarise,
    verdict,
)


def shown(ratio: float) -> str:
    return "infinite" if ratio == float("inf") else f"{ratio:.3f}"


def pair(fixed_with, fixed_without, variant="yen", dataset="s1", **changes):
    fields = {"cost_with": 0.10, "cost_without": 0.06}
    return Pair(
        "project-01", dataset, variant, "planted", fixed_with, fixed_without, **(fields | changes)
    )


def many(with_only: int, without_only: int, both: int = 0, neither: int = 0) -> list[Pair]:
    found = [pair(True, False) for _ in range(with_only)]
    found += [pair(False, True) for _ in range(without_only)]
    found += [pair(True, True) for _ in range(both)]
    return found + [pair(False, False) for _ in range(neither)]


class RuleTest(unittest.TestCase):
    def test_the_verdict_from_the_interval(self):
        self.assertEqual(verdict(0.001, 0.2), HELPS)
        self.assertEqual(verdict(-0.2, -0.001), HURTS)
        self.assertEqual(verdict(-0.05, 0.099), NO_GAIN)
        self.assertEqual(verdict(-0.05, 0.10), UNCLEAR)
        # A limit exactly on 0 decides nothing.
        self.assertEqual(verdict(0.0, 0.2), UNCLEAR)
        self.assertEqual(verdict(-0.2, 0.0), NO_GAIN)

    def test_pairs_are_counted_with_the_diagnosis_first(self):
        counted = count(many(3, 1, both=5, neither=2))
        self.assertEqual(
            (counted.both, counted.first_only, counted.second_only, counted.neither), (5, 3, 1, 2)
        )

    def test_boundaries_at_fifty_two_pairs(self):
        # With no pair in disagreement Tango's limits are -/+ z^2 / (n + z^2).
        z2 = NormalDist().inv_cdf(0.975) ** 2
        summary = summarise(many(0, 0, both=52))
        self.assertEqual(summary.verdict, NO_GAIN)
        self.assertAlmostEqual(summary.interval[1], z2 / (52 + z2), places=6)
        self.assertAlmostEqual(summary.interval[0], -z2 / (52 + z2), places=6)
        self.assertEqual(summarise(many(4, 0, both=48)).verdict, HELPS)
        self.assertEqual(summarise(many(3, 0, both=49)).verdict, UNCLEAR)
        self.assertEqual(summarise(many(0, 4, both=48)).verdict, HURTS)
        self.assertEqual(summarise(many(2, 0, both=50)).verdict, UNCLEAR)
        # Disagreement both ways, evenly: surely less than 10 points either way.
        self.assertEqual(summarise(many(2, 2, both=48)).verdict, NO_GAIN)

    def test_no_pairs_is_unclear(self):
        summary = summarise([])
        self.assertEqual(
            (summary.verdict, summary.interval, summary.per_fix), (UNCLEAR, None, None)
        )
        self.assertEqual(render(summary), "Fix measurement: unclear\n\nNo pairs finished yet.\n")


class CostTest(unittest.TestCase):
    def test_cost_per_passing_fix_counts_every_run_of_an_arm(self):
        pairs = [
            pair(True, True, cost_with=0.10, cost_without=0.06),
            pair(True, False, cost_with=0.12, cost_without=0.07, dataset="s7"),
        ]
        summary = summarise(pairs)
        self.assertAlmostEqual(summary.per_fix[WITH], 0.11)
        self.assertAlmostEqual(summary.per_fix[WITHOUT], 0.13)
        low, high = summary.ratio_interval
        self.assertLessEqual(low, high)

    def test_an_unknown_cost_leaves_the_cost_unknown(self):
        summary = summarise([pair(True, True), pair(True, False, cost_with=None)])
        self.assertIsNone(summary.per_fix)
        self.assertIsNone(summary.ratio_interval)
        self.assertIn("Cost per passing fix: unknown", render(summary))

    def test_an_arm_that_fixed_nothing_has_no_cost_per_fix(self):
        summary = summarise([pair(True, False), pair(True, False, dataset="s7")])
        self.assertEqual(summary.per_fix[WITHOUT], float("inf"))


class ReportTest(unittest.TestCase):
    def test_the_report_text(self):
        pairs = [
            pair(True, True, cost_with=0.10, cost_without=0.06, diagnosis="right"),
            pair(True, False, dataset="s7", cost_with=0.12, cost_without=0.07, diagnosis="right"),
            Pair(
                "project-02", "s1", "refunds", "observed", False, False, 0.2, 0.1,
                "no diagnosis", "the fix still failed",
            ),
        ]  # fmt: skip
        summary = summarise(pairs)
        low, high = summary.interval
        cost_low, cost_high = summary.ratio_interval
        expected = (
            "Fix measurement: unclear\n"
            "\n"
            "Pairs: 3 (each project fixed once on each dataset with the diagnosis and once without)\n"
            "  both fixed 1, only with the diagnosis 1, only without 0, neither 1\n"
            "Fixed: with the diagnosis 2 of 3 (67%), without 1 of 3 (33%)\n"
            f"Difference: +33.3 points, 95% interval (Tango) {low * 100:+.1f} to {high * 100:+.1f}\n"
            "Rule: helps when the interval lies above 0, hurts when it lies below 0, no gain "
            "when its upper limit is below 10 points. Pairs of one variant are not independent, "
            "so read the variants below as well.\n"
            "US$ per passing fix (the diagnosis included): with US$0.2100, without US$0.2300\n"
            f"With / without: 95% interval (bootstrap over pairs) {shown(cost_low)} to "
            f"{shown(cost_high)}\n"
            "Cost is Claude Code's own estimate, not a bill.\n"
            "\n"
            "  refunds                       observed  with 0 of 1, without 0 of 1\n"
            "  yen                           planted   with 2 of 2, without 1 of 2\n"
            "\n"
            "  refunds                       s1          with not fixed, without not fixed; "
            "diagnosis no diagnosis (the fix still failed)\n"
            "  yen                           s1          with fixed, without fixed; diagnosis right\n"
            "  yen                           s7          with fixed, without not fixed; "
            "diagnosis right\n"
        )
        self.assertEqual(render(summary), expected)


if __name__ == "__main__":
    unittest.main()
