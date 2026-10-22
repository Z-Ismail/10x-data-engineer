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

import json
import random
import re
from collections.abc import Sequence
from dataclasses import dataclass
from math import comb, exp, lgamma, log
from pathlib import Path

# Each arm and how the engineer is run in it: (scouts, cross_source).
ARMS = {
    "alone": (False, False),
    "scouts": (True, False),
    "cross-source": (False, True),
    "both": (True, True),
}
BASELINE = "alone"
# An arm is said to help (or hurt) when the probability that its pass rate
# is higher (or lower) than the baseline's reaches this. With three arms
# compared with one baseline, 0.98 keeps the chance of wrongly keeping any
# of them near 5%.
DECISION = 0.98
PLAN_FILE = "plan.json"
RUNS_DIR = "runs"
USAGE_FILE = "usage.json"
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


def write_plan(
    out: Path, datasets: Sequence[str], arms: Sequence[str], trials: int, seed: int
) -> list[Trial]:
    """Plan the runs and keep the plan in out, so the comparison can resume."""
    order = plan(datasets, arms, trials, seed)
    document = {
        "datasets": list(datasets),
        "arms": list(arms),
        "trials": trials,
        "seed": seed,
        "order": [{"dataset": t.dataset, "arm": t.arm, "trial": t.trial} for t in order],
    }
    (Path(out) / PLAN_FILE).write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return order


def read_plan(out: Path) -> tuple[dict, list[Trial]]:
    path = Path(out) / PLAN_FILE
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        order = [Trial(t["dataset"], t["arm"], t["trial"]) for t in document["order"]]
        document["datasets"], document["arms"], document["trials"]
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ValueError(f"{path} is not a comparison plan: {error}") from error
    return document, order


@dataclass(frozen=True)
class Outcome:
    """What one finished run left: the gate verdict and what the run cost."""

    trial: Trial
    gate: str | None
    cost_usd: float | None = None


def _cost(role) -> float | None:
    cost = role.get("cost_usd") if isinstance(role, dict) else None
    if isinstance(cost, bool) or not isinstance(cost, (int, float)):
        return None
    return cost if role.get("cost_known") is True else None


def outcome(out: Path, trial: Trial) -> Outcome:
    """The run's verdict from its usage.json, or no verdict if it left none.

    The run's cost is known only when every role's cost is.
    """
    path = Path(out) / RUNS_DIR / trial.folder / USAGE_FILE
    try:
        ledger = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Outcome(trial, None)
    gate = ledger.get("gate") if isinstance(ledger, dict) else None
    if gate not in ("PASS", "FAIL"):
        return Outcome(trial, None)
    roles = ledger.get("roles")
    costs = [_cost(role) for role in roles] if isinstance(roles, list) else []
    known = bool(costs) and all(cost is not None for cost in costs)
    return Outcome(trial, gate, sum(costs) if known else None)


@dataclass(frozen=True)
class ArmSummary:
    arm: str
    tally: Tally
    low: float
    high: float
    better: float | None
    worse: float | None
    verdict: str
    cost_usd: float | None
    cost_per_pass: float | None
    no_verdict: int

    def to_dict(self) -> dict:
        return {
            "arm": self.arm,
            "runs": self.tally.runs,
            "passes": self.tally.passes,
            "pass_rate": self.tally.passes / self.tally.runs if self.tally.runs else None,
            "interval_95": [self.low, self.high],
            "prob_better_than_baseline": self.better,
            "prob_worse_than_baseline": self.worse,
            "verdict": self.verdict,
            "cost_usd": self.cost_usd,
            "cost_per_pass_usd": self.cost_per_pass,
            "runs_without_verdict": self.no_verdict,
        }


@dataclass(frozen=True)
class Summary:
    datasets: list[str]
    trials: int
    arms: tuple[ArmSummary, ...]
    by_dataset: dict[str, dict[str, Tally]]
    without_verdict: tuple[Trial, ...]
    planned: int

    def to_dict(self) -> dict:
        return {
            "baseline": BASELINE,
            "decision": DECISION,
            "datasets": self.datasets,
            "trials": self.trials,
            "planned_runs": self.planned,
            "arms": [arm.to_dict() for arm in self.arms],
            "by_dataset": {
                dataset: {arm: [t.passes, t.runs] for arm, t in arms.items()}
                for dataset, arms in self.by_dataset.items()
            },
            "runs_without_verdict": [t.folder for t in self.without_verdict],
        }


def _verdict(arm: str, better: float | None, worse: float | None) -> str:
    if arm == BASELINE:
        return "baseline"
    if better is None or worse is None:
        return "no runs"
    if better >= DECISION:
        return "helps"
    if worse >= DECISION:
        return "hurts"
    return "not shown"


def _tally(outcomes: list[Outcome]) -> Tally:
    return Tally(sum(o.gate == "PASS" for o in outcomes), sum(o.gate == "FAIL" for o in outcomes))


def summarise(out: Path) -> Summary:
    """Every arm's passes, its pass rate and how it compares with the baseline."""
    document, order = read_plan(out)
    outcomes = [outcome(out, trial) for trial in order]
    done = [o for o in outcomes if o.gate is not None]
    datasets = document["datasets"]
    arms = document["arms"]
    base = _tally([o for o in done if o.trial.arm == BASELINE])
    summaries = []
    for arm in arms:
        mine = [o for o in done if o.trial.arm == arm]
        tally = _tally(mine)
        better = worse = None
        if arm != BASELINE and tally.runs and base.runs:
            better = prob_better(base, tally)
            worse = prob_better(tally, base)
        costs = [o.cost_usd for o in mine]
        cost = sum(costs) if costs and all(c is not None for c in costs) else None
        low, high = interval(tally)
        summaries.append(
            ArmSummary(
                arm=arm,
                tally=tally,
                low=low,
                high=high,
                better=better,
                worse=worse,
                verdict=_verdict(arm, better, worse),
                cost_usd=cost,
                cost_per_pass=cost / tally.passes if cost is not None and tally.passes else None,
                no_verdict=sum(o.trial.arm == arm and o.gate is None for o in outcomes),
            )
        )
    by_dataset = {
        dataset: {
            arm: _tally([o for o in done if o.trial.arm == arm and o.trial.dataset == dataset])
            for arm in arms
        }
        for dataset in datasets
    }
    return Summary(
        datasets=datasets,
        trials=document["trials"],
        arms=tuple(summaries),
        by_dataset=by_dataset,
        without_verdict=tuple(o.trial for o in outcomes if o.gate is None),
        planned=len(order),
    )
