"""Tests for the paired non-inferiority figures.

Expected intervals come from published tables, from a closed form worked out
by hand, or from a brute-force score test written here that finds the
constrained likelihood maximum numerically instead of by the formula.
"""

import math
import random
import unittest
from statistics import NormalDist

from tenx.parity import (
    INCONCLUSIVE,
    INFERIOR,
    MARGIN,
    NON_INFERIOR,
    Pairs,
    interval,
    score,
    verdict,
)

Z95 = NormalDist().inv_cdf(0.975)


def brute_score(pairs: Pairs, delta: float) -> float:
    """The score statistic with the cell probability found by a ternary search.

    The log-likelihood b log(x + delta) + c log(x) + (a + d) log(1 - 2x - delta)
    is concave in x on the values where every probability is positive.
    """
    a, b, c, d = pairs.both, pairs.first_only, pairs.second_only, pairs.neither
    n = pairs.n
    low, high = max(0.0, -delta), (1 - delta) / 2

    def loglik(x):
        total = 0.0
        for count, p in ((b, x + delta), (c, x), (a + d, 1 - 2 * x - delta)):
            if count:
                if p <= 0:
                    return -math.inf
                total += count * math.log(p)
        return total

    for _ in range(300):
        one, two = low + (high - low) / 3, high - (high - low) / 3
        if loglik(one) < loglik(two):
            low = one
        else:
            high = two
    x = (low + high) / 2
    variance = n * (2 * x + delta * (1 - delta))
    return (b - c - n * delta) / math.sqrt(variance) if variance > 0 else 0.0


def brute_interval(pairs: Pairs, step: float = 1e-3) -> tuple[float, float]:
    """The deltas the brute-force test does not reject: a grid scan, then halving."""
    kept = [
        k * step
        for k in range(-int(1 / step) + 1, int(1 / step))
        if abs(brute_score(pairs, k * step)) <= Z95
    ]

    def edge(inside: float, outside: float) -> float:
        for _ in range(40):
            middle = (inside + outside) / 2
            if abs(brute_score(pairs, middle)) <= Z95:
                inside = middle
            else:
                outside = middle
        return inside

    low, high = min(kept), max(kept)
    low = edge(low, max(-1.0, low - step))
    high = high if high + step >= 1 else edge(high, high + step)
    return low, min(1.0, high)


class PublishedValuesTest(unittest.TestCase):
    def test_tangos_sleep_study(self):
        # Tango 1998, table VII: b = 9, c = 3, n = 32; 95% interval -0.027 to 0.390.
        low, high = interval(Pairs(both=0, first_only=9, second_only=3, neither=20))
        self.assertAlmostEqual(low, -0.027, places=3)
        self.assertAlmostEqual(high, 0.390, places=3)

    def test_airway_reactivity(self):
        # ratesci's AS(Tango) on counts 1, 1, 7, 12: -0.5172 to -0.0260.
        low, high = interval(Pairs(both=1, first_only=1, second_only=7, neither=12))
        self.assertAlmostEqual(low, -0.5172, places=4)
        self.assertAlmostEqual(high, -0.0260, places=4)

    def test_tangos_contact_lens_trial(self):
        # Tango 1998, table V: a = 43, b = 0, c = 1; 90% lower limit -0.096.
        low, _ = interval(Pairs(both=43, first_only=0, second_only=1, neither=0), level=0.90)
        self.assertAlmostEqual(low, -0.096, places=3)

    def test_mcnemar_at_zero(self):
        # At delta 0 the score statistic is McNemar's (b - c) / sqrt(b + c).
        self.assertAlmostEqual(score(Pairs(5, 6, 2, 3), 0.0), 4 / math.sqrt(8))


class IntervalTest(unittest.TestCase):
    def test_no_disagreement_has_a_closed_form(self):
        # With b = c = 0, Z^2 = n delta / (1 - delta), so the limits are
        # +-z^2 / (n + z^2): 0.0566 for 64 pairs.
        for pairs in (Pairs(64, 0, 0, 0), Pairs(0, 0, 0, 64), Pairs(30, 0, 0, 34)):
            limit = Z95**2 / (64 + Z95**2)
            low, high = interval(pairs)
            self.assertAlmostEqual(low, -limit, places=9)
            self.assertAlmostEqual(high, limit, places=9)

    def test_one_pair(self):
        low, high = interval(Pairs(0, 1, 0, 0))
        self.assertLess(low, 0)
        self.assertEqual(high, 1.0)
        self.assertLessEqual(abs(low - brute_interval(Pairs(0, 1, 0, 0))[0]), 1e-6)

    def test_matches_a_brute_force_inversion(self):
        rng = random.Random(1)
        tables = [Pairs(60, 3, 1, 0), Pairs(0, 0, 5, 0), Pairs(10, 2, 2, 10)]
        for _ in range(12):
            tables.append(Pairs(*(rng.randrange(0, 12) for _ in range(3)), rng.randrange(1, 12)))
        for pairs in tables:
            with self.subTest(pairs=pairs):
                low, high = interval(pairs)
                brute_low, brute_high = brute_interval(pairs)
                self.assertLessEqual(abs(low - brute_low), 1e-6)
                self.assertLessEqual(abs(high - brute_high), 1e-6)

    def test_score_falls_as_delta_rises(self):
        for pairs in (Pairs(60, 3, 1, 0), Pairs(1, 1, 7, 12), Pairs(64, 0, 0, 0)):
            values = [score(pairs, k / 100) for k in range(-99, 100)]
            self.assertEqual(values, sorted(values, reverse=True))

    def test_refusals(self):
        with self.assertRaises(ValueError):
            interval(Pairs(0, 0, 0, 0))
        with self.assertRaises(ValueError):
            interval(Pairs(1, 0, 0, 0), level=1.0)
        for bad in (-1, 1.5, True):
            with self.assertRaises(ValueError):
                Pairs(bad, 0, 0, 0)


class VerdictTest(unittest.TestCase):
    def test_the_three_verdicts(self):
        self.assertEqual(verdict(-0.05, 0.08), NON_INFERIOR)
        self.assertEqual(verdict(0.02, 0.09), NON_INFERIOR)
        self.assertEqual(verdict(0.11, 0.30), INFERIOR)
        self.assertEqual(verdict(-0.02, 0.15), INCONCLUSIVE)

    def test_limits_on_the_margin_are_inconclusive(self):
        self.assertEqual(MARGIN, 0.10)
        self.assertEqual(verdict(-0.05, 0.10), INCONCLUSIVE)
        self.assertEqual(verdict(0.10, 0.20), INCONCLUSIVE)

    def test_from_counts(self):
        # 64 pairs, 2 lost by the graph and 1 gained: upper limit 0.094.
        self.assertEqual(verdict(*interval(Pairs(60, 2, 1, 1))), NON_INFERIOR)
        # 64 pairs, 20 lost and 1 gained: the whole interval above 0.10.
        self.assertEqual(verdict(*interval(Pairs(40, 20, 1, 3))), INFERIOR)
        # 16 pairs, 1 lost: too few pairs to tell.
        self.assertEqual(verdict(*interval(Pairs(15, 1, 0, 0))), INCONCLUSIVE)


if __name__ == "__main__":
    unittest.main()
