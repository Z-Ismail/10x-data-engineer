"""Decide whether one runner is no worse than another from paired runs.

Each pair is one dataset and trial run by both runners, and the gate
decides each run. Of n pairs, a both pass, b only the first runner passes,
c only the second passes and d neither does. The difference in pass rates,
first minus second, is (b - c) / n.

Its 95% interval is Tango's score interval for paired proportions (Tango
1998, Statistics in Medicine 17:891-908): the values of the difference that
the score test does not reject. It behaves well when few pairs disagree, or
none do, where the simple Wald interval collapses to a point.

A margin is fixed before any run. The second runner is non-inferior when the
whole interval lies below the margin, inferior when the whole interval lies
above it, and the comparison is inconclusive otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import inf, sqrt
from statistics import NormalDist

MARGIN = 0.10
LEVEL = 0.95
NON_INFERIOR = "non-inferior"
INFERIOR = "inferior"
INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True)
class Pairs:
    """Counts of paired verdicts: both pass, only the first, only the second, neither."""

    both: int
    first_only: int
    second_only: int
    neither: int

    def __post_init__(self):
        for value in (self.both, self.first_only, self.second_only, self.neither):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"counts must be whole numbers of at least 0, not {value!r}")

    @property
    def n(self) -> int:
        return self.both + self.first_only + self.second_only + self.neither

    @property
    def difference(self) -> float:
        """The first runner's pass rate minus the second's."""
        return (self.first_only - self.second_only) / self.n


def score(pairs: Pairs, delta: float) -> float:
    """Tango's score statistic for a hypothesised difference delta.

    The variance uses the estimate of the second-only cell's probability
    that maximises the likelihood when the difference is delta: the larger
    root of 2n x^2 + W x - c delta (1 - delta) = 0, with
    W = -b - c + (2n - b + c) delta.
    """
    b, c, n = pairs.first_only, pairs.second_only, pairs.n
    w = -b - c + (2 * n - b + c) * delta
    q = (sqrt(max(0.0, w * w - 8 * n * c * delta * (delta - 1))) - w) / (4 * n)
    variance = n * (2 * q + delta * (1 - delta))
    excess = b - c - n * delta
    if variance <= 0:
        # Only when no pair disagrees and delta is 0: nothing to reject.
        return 0.0 if excess == 0 else (inf if excess > 0 else -inf)
    return excess / sqrt(variance)


def interval(pairs: Pairs, level: float = LEVEL) -> tuple[float, float]:
    """Tango's score interval for the difference, found by bisection.

    The statistic falls as delta rises, so each limit is where it crosses
    the normal quantile, one between -1 and the estimate, one between the
    estimate and 1.
    """
    if pairs.n == 0:
        raise ValueError("at least one pair is needed")
    if not 0 < level < 1:
        raise ValueError(f"level must be between 0 and 1, not {level}")
    z = NormalDist().inv_cdf(0.5 + level / 2)
    estimate = pairs.difference

    def crossing(target: float, low: float, high: float) -> float:
        for _ in range(200):
            middle = (low + high) / 2
            if score(pairs, middle) > target:
                low = middle
            else:
                high = middle
        return (low + high) / 2

    return crossing(z, -1.0, estimate), crossing(-z, estimate, 1.0)


def verdict(low: float, high: float, margin: float = MARGIN) -> str:
    """Non-inferior below the margin, inferior above it, inconclusive across it."""
    if high < margin:
        return NON_INFERIOR
    if low > margin:
        return INFERIOR
    return INCONCLUSIVE
