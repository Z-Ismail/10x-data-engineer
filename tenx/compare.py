"""Compare how often the engineer passes the gate with and without other roles.

Each arm is one way of running the engineer (alone, with scouts, with the
cross-source agent, or with both). Every arm runs the same number of times on
every dataset, and the gate decides each run. A pass rate from a few dozen
runs is uncertain, so each arm's rate is given as a Beta posterior: with a
uniform prior, Beta(1 + passes, 1 + failures). The arm is then compared with
the engineer alone by the probability that its pass rate is the higher one,
worked out exactly rather than by sampling, so the same runs always give the
same figures.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import comb, exp, lgamma, log


@dataclass(frozen=True)
class Tally:
    """Passes and failures of one arm."""

    passes: int
    failures: int

    def __post_init__(self):
        for value in (self.passes, self.failures):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"counts must be whole numbers of at least 0, not {value!r}")

    @property
    def runs(self) -> int:
        return self.passes + self.failures

    @property
    def alpha(self) -> int:
        return self.passes + 1

    @property
    def beta(self) -> int:
        return self.failures + 1


def _log_beta(a: float, b: float) -> float:
    return lgamma(a) + lgamma(b) - lgamma(a + b)


def prob_better(base: Tally, other: Tally) -> float:
    """The probability that other's pass rate is higher than base's.

    Evan Miller's closed form for two Beta posteriors with whole-number
    parameters: a sum over i from 0 to alpha_other - 1 of
    B(alpha_base + i, beta_base + beta_other)
    / ((beta_other + i) B(1 + i, beta_other) B(alpha_base, beta_base)).
    """
    total = 0.0
    for i in range(other.alpha):
        total += exp(
            _log_beta(base.alpha + i, base.beta + other.beta)
            - log(other.beta + i)
            - _log_beta(1 + i, other.beta)
            - _log_beta(base.alpha, base.beta)
        )
    return min(1.0, max(0.0, total))


def _beta_cdf(x: float, a: int, b: int) -> float:
    # For whole-number a and b, P(Beta(a, b) <= x) is the chance of at least
    # a successes in a + b - 1 trials that each succeed with probability x.
    n = a + b - 1
    return sum(comb(n, k) * x**k * (1 - x) ** (n - k) for k in range(a, n + 1))


def interval(tally: Tally, level: float = 0.95) -> tuple[float, float]:
    """The equal-tailed credible interval of the pass rate."""
    if not 0 < level < 1:
        raise ValueError(f"level must be between 0 and 1, not {level}")
    tail = (1 - level) / 2

    def quantile(p: float) -> float:
        low, high = 0.0, 1.0
        for _ in range(60):
            middle = (low + high) / 2
            if _beta_cdf(middle, tally.alpha, tally.beta) < p:
                low = middle
            else:
                high = middle
        return (low + high) / 2

    return quantile(tail), quantile(1 - tail)
