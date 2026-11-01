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

import random
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


# The second runner may cost at most this much more per pass than the first.
COST_LIMIT = 1.10
RESAMPLES = 2000
WITHIN = "within"
OVER = "over"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class PairCost:
    """One pair's verdicts and estimated costs, first runner then second."""

    first_passed: bool
    first_usd: float
    second_passed: bool
    second_usd: float


def cost_per_pass(costs: list[float], passes: int) -> float:
    """All spend divided by passes; infinite when nothing passed."""
    return sum(costs) / passes if passes else inf


def cost_ratio(pairs: list[PairCost]) -> float:
    """The second runner's cost per pass over the first's."""
    first = cost_per_pass([p.first_usd for p in pairs], sum(p.first_passed for p in pairs))
    second = cost_per_pass([p.second_usd for p in pairs], sum(p.second_passed for p in pairs))
    if first == inf:
        return inf if second == inf else 0.0
    return inf if second == inf else second / first


def cost_interval(
    pairs: list[PairCost], level: float = LEVEL, resamples: int = RESAMPLES, seed: int = 0
) -> tuple[float, float]:
    """A percentile bootstrap interval of the cost ratio, resampling whole pairs.

    The resamples are drawn from a fixed seed, so the same pairs always give
    the same interval. A resample in which the second runner never passes
    gives an infinite ratio, which counts like any other.
    """
    if not pairs:
        raise ValueError("at least one pair is needed")
    if not 0 < level < 1:
        raise ValueError(f"level must be between 0 and 1, not {level}")
    rng = random.Random(f"{seed}:cost")
    ratios = sorted(
        cost_ratio([pairs[rng.randrange(len(pairs))] for _ in pairs]) for _ in range(resamples)
    )
    tail = (1 - level) / 2
    low = ratios[int(tail * resamples)]
    high = ratios[min(resamples - 1, int((1 - tail) * resamples))]
    return low, high


def cost_verdict(low: float, high: float, limit: float = COST_LIMIT) -> str:
    """Within the limit, over it, or inconclusive across it."""
    if high < limit:
        return WITHIN
    if low > limit:
        return OVER
    return INCONCLUSIVE


def parity(passes: str, cost: str) -> str:
    """Non-inferior only when the pass rate is and the cost is within the limit."""
    if passes == INFERIOR or cost == OVER:
        return INFERIOR
    if passes == NON_INFERIOR and cost == WITHIN:
        return NON_INFERIOR
    return INCONCLUSIVE
