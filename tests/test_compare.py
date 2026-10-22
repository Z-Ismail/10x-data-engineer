"""Tests for comparing arms of runs.

Expected probabilities and intervals are worked out by hand from the Beta
densities, or by numerical integration written here, not by the code under
test.
"""

import math
import unittest

from tenx.compare import Tally, interval, prob_better


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


if __name__ == "__main__":
    unittest.main()
