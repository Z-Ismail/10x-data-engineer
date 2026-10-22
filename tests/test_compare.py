"""Tests for comparing arms of runs.

Expected probabilities and intervals are worked out by hand from the Beta
densities, or by numerical integration written here, not by the code under
test.
"""

import math
import unittest

from tenx.compare import ARMS, Tally, Trial, interval, plan, prob_better


def beta_pdf(x: float, a: int, b: int) -> float:
    # (a + b - 1)! / ((a - 1)! (b - 1)!) x^(a-1) (1-x)^(b-1)
    scale = math.factorial(a + b - 1) / (math.factorial(a - 1) * math.factorial(b - 1))
    return scale * x ** (a - 1) * (1 - x) ** (b - 1)


def integrated_prob_better(base: Tally, other: Tally, steps: int = 4000) -> float:
    """P(other > base) by the midpoint rule over the two densities."""
    width = 1 / steps
    below = 0.0  # P(base < x), built up step by step
    total = 0.0
    for i in range(steps):
        x = (i + 0.5) * width
        base_here = beta_pdf(x, base.passes + 1, base.failures + 1) * width
        total += beta_pdf(x, other.passes + 1, other.failures + 1) * width * (below + base_here / 2)
        below += base_here
    return total


class ProbBetterTest(unittest.TestCase):
    def test_two_arms_with_no_runs_are_even(self):
        self.assertAlmostEqual(prob_better(Tally(0, 0), Tally(0, 0)), 0.5, places=12)

    def test_one_pass_against_no_runs(self):
        # Beta(2, 1) has density 2x; P(X > Y) with Y uniform is the
        # integral of 2x * x over [0, 1], which is 2/3.
        self.assertAlmostEqual(prob_better(Tally(0, 0), Tally(1, 0)), 2 / 3, places=12)

    def test_one_pass_against_one_failure(self):
        # Densities 2x and 2(1 - y): the integral of 2x (2x - x^2) is 5/6.
        self.assertAlmostEqual(prob_better(Tally(0, 1), Tally(1, 0)), 5 / 6, places=12)
        self.assertAlmostEqual(prob_better(Tally(1, 0), Tally(0, 1)), 1 / 6, places=12)

    def test_the_two_directions_add_up_to_one(self):
        for a, b in [((3, 9), (10, 4)), ((0, 32), (32, 0)), ((12, 20), (12, 20)), ((5, 1), (2, 7))]:
            with self.subTest(a=a, b=b):
                forward = prob_better(Tally(*a), Tally(*b))
                backward = prob_better(Tally(*b), Tally(*a))
                self.assertAlmostEqual(forward + backward, 1.0, places=10)

    def test_equal_counts_are_even(self):
        self.assertAlmostEqual(prob_better(Tally(12, 20), Tally(12, 20)), 0.5, places=10)

    def test_matches_numerical_integration(self):
        for a, b in [((3, 9), (10, 4)), ((13, 19), (22, 10)), ((8, 24), (9, 23))]:
            with self.subTest(a=a, b=b):
                self.assertAlmostEqual(
                    prob_better(Tally(*a), Tally(*b)),
                    integrated_prob_better(Tally(*a), Tally(*b)),
                    places=5,
                )

    def test_a_clear_difference_is_near_certain(self):
        self.assertGreater(prob_better(Tally(0, 32), Tally(32, 0)), 0.999999)


class IntervalTest(unittest.TestCase):
    def test_no_runs_gives_the_middle_95_percent_of_a_uniform(self):
        low, high = interval(Tally(0, 0))
        self.assertAlmostEqual(low, 0.025, places=9)
        self.assertAlmostEqual(high, 0.975, places=9)

    def test_no_passes_in_three_runs(self):
        # Beta(1, 4): P(X <= x) = 1 - (1 - x)^4.
        low, high = interval(Tally(0, 3))
        self.assertAlmostEqual(low, 1 - 0.975**0.25, places=9)
        self.assertAlmostEqual(high, 1 - 0.025**0.25, places=9)

    def test_one_pass(self):
        # Beta(2, 1): P(X <= x) = x^2.
        low, high = interval(Tally(1, 0))
        self.assertAlmostEqual(low, math.sqrt(0.025), places=9)
        self.assertAlmostEqual(high, math.sqrt(0.975), places=9)

    def test_another_level(self):
        low, high = interval(Tally(0, 0), level=0.5)
        self.assertAlmostEqual(low, 0.25, places=9)
        self.assertAlmostEqual(high, 0.75, places=9)

    def test_refuses_a_level_outside_zero_to_one(self):
        for level in (0, 1, 1.5):
            with self.subTest(level=level), self.assertRaises(ValueError):
                interval(Tally(1, 1), level=level)


class TallyTest(unittest.TestCase):
    def test_refuses_counts_that_are_not_whole_numbers_of_at_least_zero(self):
        for value in (-1, 1.0, True, "3", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Tally(value, 0)
            with self.subTest(value=value), self.assertRaises(ValueError):
                Tally(0, value)

    def test_runs(self):
        self.assertEqual(Tally(3, 5).runs, 8)


class PlanTest(unittest.TestCase):
    DATASETS = ("s1-200", "s7-200", "s1-1000")
    ARMS = ("alone", "scouts", "cross-source", "both")

    def test_runs_every_arm_on_every_dataset_once_per_trial(self):
        order = plan(self.DATASETS, self.ARMS, trials=2)
        self.assertEqual(len(order), 3 * 4 * 2)
        self.assertEqual(
            sorted((t.dataset, t.arm, t.trial) for t in order),
            sorted((d, a, n) for d in self.DATASETS for a in self.ARMS for n in (1, 2)),
        )

    def test_each_block_is_one_dataset_and_trial_holding_every_arm(self):
        order = plan(self.DATASETS, self.ARMS, trials=2)
        blocks = [order[i : i + 4] for i in range(0, len(order), 4)]
        self.assertEqual(
            [(b[0].dataset, b[0].trial) for b in blocks],
            [(d, n) for n in (1, 2) for d in self.DATASETS],
        )
        for block in blocks:
            self.assertEqual({t.dataset for t in block}, {block[0].dataset})
            self.assertEqual({t.trial for t in block}, {block[0].trial})
            self.assertEqual(sorted(t.arm for t in block), sorted(self.ARMS))

    def test_the_order_within_blocks_varies_and_repeats_for_a_seed(self):
        datasets = [f"d{i}" for i in range(25)]
        order = plan(datasets, self.ARMS, trials=4, seed=3)
        self.assertEqual(order, plan(datasets, self.ARMS, trials=4, seed=3))
        self.assertNotEqual(order, plan(datasets, self.ARMS, trials=4, seed=4))
        firsts = [order[i].arm for i in range(0, len(order), 4)]
        self.assertEqual(set(firsts), set(self.ARMS))

    def test_folder_names_the_dataset_arm_and_trial(self):
        self.assertEqual(Trial("s42-200", "cross-source", 3).folder, "s42-200/cross-source/3")

    def test_known_arms(self):
        self.assertEqual(
            ARMS,
            {
                "alone": (False, False),
                "scouts": (True, False),
                "cross-source": (False, True),
                "both": (True, True),
            },
        )

    def test_refuses_an_unusable_plan(self):
        cases = [
            (["d"], ["alone"], 0),
            (["d"], ["alone"], True),
            ([], ["alone"], 1),
            (["d"], [], 1),
            (["d", "d"], ["alone"], 1),
            (["d"], ["alone", "alone"], 1),
            (["d"], ["alone", "reviewer"], 1),
            (["d"], ["scouts"], 1),
            (["a/b"], ["alone"], 1),
            ([".."], ["alone"], 1),
        ]
        for datasets, arms, trials in cases:
            with (
                self.subTest(datasets=datasets, arms=arms, trials=trials),
                self.assertRaises(ValueError),
            ):
                plan(datasets, arms, trials)


if __name__ == "__main__":
    unittest.main()
