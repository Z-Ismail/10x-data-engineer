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

import random
import re
from collections.abc import Sequence
from dataclasses import dataclass
from math import comb, exp, lgamma, log

# Each arm and how the engineer is run in it: (scouts, cross_source).
ARMS = {
    "alone": (False, False),
    "scouts": (True, False),
    "cross-source": (False, True),
    "both": (True, True),
}
BASELINE = "alone"
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


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


@dataclass(frozen=True)
class Trial:
    """One run of one arm on one dataset."""

    dataset: str
    arm: str
    trial: int

    @property
    def folder(self) -> str:
        return f"{self.dataset}/{self.arm}/{self.trial}"


def plan(datasets: Sequence[str], arms: Sequence[str], trials: int, seed: int = 0) -> list[Trial]:
    """The order to run every arm on every dataset, trials times.

    A block is one dataset and one trial number, and holds each arm once, in
    a random order drawn from the seed. Blocks run trial by trial, so a
    comparison cut short still has every arm on nearly the same datasets.
    """
    if not isinstance(trials, int) or isinstance(trials, bool) or trials < 1:
        raise ValueError(f"trials must be a whole number of at least 1, not {trials!r}")
    for kind, names in (("dataset", datasets), ("arm", arms)):
        if not names:
            raise ValueError(f"at least one {kind} is needed")
        if len(set(names)) != len(names):
            raise ValueError(f"each {kind} may be named only once")
        for name in names:
            if not isinstance(name, str) or not _NAME.fullmatch(name):
                raise ValueError(f"{kind} name {name!r} must be letters, digits, '_', '.' or '-'")
    unknown = [arm for arm in arms if arm not in ARMS]
    if unknown:
        raise ValueError(f"unknown arm {unknown[0]!r}; the arms are {', '.join(ARMS)}")
    if BASELINE not in arms:
        raise ValueError(f"the arms must include {BASELINE!r}, which the others are compared with")
    rng = random.Random(f"{seed}:compare")
    order = []
    for trial in range(1, trials + 1):
        for dataset in datasets:
            block = list(arms)
            rng.shuffle(block)
            order += [Trial(dataset, arm, trial) for arm in block]
    return order
