"""Compare how often the engineer passes the gate with and without other roles.

Each arm is one way of running the engineer (alone, with scouts, with the
cross-source agent, or with both). Every arm runs the same number of times on
every dataset, and the gate decides each run. A pass rate from a few dozen
runs is uncertain, so each arm's rate is given as a Beta posterior: with a
uniform prior, Beta(1 + passes, 1 + failures). The arm is then compared with
the engineer alone by the probability that its pass rate is the higher one,
worked out exactly rather than by sampling, so the same runs always give the
same figures.

    python -m tenx.compare run --raw NAME=FOLDER [--raw NAME=FOLDER ...]
        --goal <file> --checks <file> --out <folder> [--trials 4] [--parallel 3]
    python -m tenx.compare paired --raw NAME=FOLDER [--raw NAME=FOLDER ...]
        --goal <file> --checks <file> --out <folder> [--trials 8] [--parallel 3]
    python -m tenx.compare repairs --raw NAME=FOLDER [--raw NAME=FOLDER ...]
        --goal <file> --checks <file> --out <folder> [--trials 4] [--repairs 2]
        [--no-cross-source] [--parallel 3]
    python -m tenx.compare architect --raw NAME=FOLDER [--raw NAME=FOLDER ...]
        --goal <file> --checks <file> --out <folder> [--trials 4] [--parallel 3]
    python -m tenx.compare report <folder>

``paired`` compares two runners rather than arms: today's runner and the
same run as a graph, each with the cross-source agent, on the same datasets
and trials, the two runs of a pair next to each other. The pairs are
compared by Tango's interval on the difference in pass rates against a
10-point margin fixed in advance, and by the cost per pass (tenx.parity).

``repairs`` runs the graph with repairs and pairs each run with its own
first round, which is a run without repairs: nothing the engineer sees in
its first attempt depends on the number of repairs, and a repair is made
only after a round fails. Repairs are better when Tango's interval on the
difference lies wholly above 0; the cost per pass of each is reported.

``architect`` runs the graph with and without the architect agent, both
with the cross-source agent, on the same datasets in shuffled blocks, and
judges them as the arms are judged, the run without the architect as the
baseline. Each architect run's spec is scored against the declaration
(tenx.agreement), which the agents never see, and shown beside its verdict.

Each run is an ordinary engineer run, in its own folder:

    <out>/
        plan.json                       the datasets, arms, trials and run order
        runs/<dataset>/<arm>/<trial>/   one engineer run, with its usage.json
        comparison.txt                  the report
        comparison.json                 the same figures as JSON

A comparison that stops part way can be started again with the same
command: runs that already have a gate verdict are kept, and the others are
run again from an empty folder.

Before any run starts, the runs still to do are priced: typically, from what
each agent call usually costs, and at most, from each call's cap. A
comparison estimated above US$5 does not start unless --approve-cost is
given. The figures are Claude Code's own estimates, not bills.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import shutil
import sys
import threading
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from decimal import Decimal
from math import comb, exp, lgamma, log
from pathlib import Path

from tenx import artefacts, parity
from tenx.agreement import declared_rows, last_contract, score
from tenx.engineer import ARCHITECT as ARCHITECT_ROLE
from tenx.engineer import EngineerError
from tenx.engineer import run as engineer_run
from tenx.gate import GateError, load_declaration
from tenx.policy import CROSS_SOURCE, ENGINEER, SCOUT, Policy
from tenx.profile import discover

# Each arm and how the engineer is run in it: (scouts, cross_source).
ARMS = {
    "alone": (False, False),
    "scouts": (True, False),
    "cross-source": (False, True),
    "both": (True, True),
}
BASELINE = "alone"
# The paired comparison: today's runner with the cross-source agent, and the
# same run as a graph. Each dataset and trial runs both, back to back.
PAIRED = "paired"
PAIRED_ARMS = ("default", "graph")
# The repair comparison: graph runs that may repair a failed project, each
# compared with its own first round, which is a run without repairs.
REPAIRS = "repairs"
REPAIR_ARM = "repair"
# The architect comparison: the graph run on the same datasets with and
# without the architect agent, both with the cross-source agent, in
# shuffled blocks as the arm comparison runs them.
ARCHITECT = "architect"
ARCHITECT_ARMS = ("graph", "architect")
ARCHITECT_BASELINE = "graph"
# Each architect run's contract scored against the declaration, written
# into the run's folder when it finishes.
AGREEMENT_FILE = "agreement.json"
# An arm is said to help (or hurt) when the probability that its pass rate
# is higher (or lower) than the baseline's reaches this. With three arms
# compared with one baseline, 0.98 keeps the chance of wrongly keeping any
# of them near 5%.
DECISION = 0.98
PLAN_FILE = "plan.json"
RUNS_DIR = "runs"
USAGE_FILE = "usage.json"
REPORT_TEXT = "comparison.txt"
REPORT_JSON = "comparison.json"
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


def _check_plan(datasets: Sequence[str], arms: Sequence[str], trials: int) -> None:
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


def _blocks(datasets: Sequence[str], arms: Sequence[str], trials: int, rng) -> list[Trial]:
    order = []
    for trial in range(1, trials + 1):
        for dataset in datasets:
            block = list(arms)
            rng.shuffle(block)
            order += [Trial(dataset, arm, trial) for arm in block]
    return order


def plan(datasets: Sequence[str], arms: Sequence[str], trials: int, seed: int = 0) -> list[Trial]:
    """The order to run every arm on every dataset, trials times.

    A block is one dataset and one trial number, and holds each arm once, in
    a random order drawn from the seed. Blocks run trial by trial, so a
    comparison cut short still has every arm on nearly the same datasets.
    """
    _check_plan(datasets, arms, trials)
    unknown = [arm for arm in arms if arm not in ARMS]
    if unknown:
        raise ValueError(f"unknown arm {unknown[0]!r}; the arms are {', '.join(ARMS)}")
    if BASELINE not in arms:
        raise ValueError(f"the arms must include {BASELINE!r}, which the others are compared with")
    return _blocks(datasets, arms, trials, random.Random(f"{seed}:compare"))


def paired_plan(datasets: Sequence[str], trials: int, seed: int = 0) -> list[Trial]:
    """The order of a paired comparison: each pair's two runs next to each other.

    A pair is one dataset and one trial number; which runner goes first is
    drawn from the seed.
    """
    _check_plan(datasets, PAIRED_ARMS, trials)
    return _blocks(datasets, PAIRED_ARMS, trials, random.Random(f"{seed}:paired"))


def repairs_plan(datasets: Sequence[str], trials: int, seed: int = 0) -> list[Trial]:
    """The order of a repair comparison: one run per dataset and trial.

    Runs go trial by trial, the datasets of each trial in an order drawn
    from the seed.
    """
    _check_plan(datasets, [REPAIR_ARM], trials)
    rng = random.Random(f"{seed}:repairs")
    order = []
    for trial in range(1, trials + 1):
        names = list(datasets)
        rng.shuffle(names)
        order += [Trial(dataset, REPAIR_ARM, trial) for dataset in names]
    return order


def architect_plan(datasets: Sequence[str], trials: int, seed: int = 0) -> list[Trial]:
    """The order of an architect comparison: blocks of one dataset and trial, as plan() makes.

    Each block holds a run with the architect and one without, in an order
    drawn from the seed.
    """
    _check_plan(datasets, ARCHITECT_ARMS, trials)
    return _blocks(datasets, ARCHITECT_ARMS, trials, random.Random(f"{seed}:architect"))


def _planned(datasets, arms, trials, seed, mode) -> list[Trial]:
    if mode == ARCHITECT:
        return architect_plan(datasets, trials, seed)
    if mode == PAIRED:
        return paired_plan(datasets, trials, seed)
    if mode == REPAIRS:
        return repairs_plan(datasets, trials, seed)
    return plan(datasets, arms, trials, seed)


def write_plan(
    out: Path,
    datasets: Sequence[str],
    arms: Sequence[str],
    trials: int,
    seed: int,
    mode: str | None = None,
    settings: dict | None = None,
) -> list[Trial]:
    """Plan the runs and keep the plan in out, so the comparison can resume.

    ``settings`` are kept beside the plan, and a resume must ask for the same.
    """
    order = _planned(datasets, arms, trials, seed, mode)
    document = {
        **({"mode": mode} if mode else {}),
        **(settings or {}),
        "datasets": list(datasets),
        "arms": list(arms),
        "trials": trials,
        "seed": seed,
        "order": [{"dataset": t.dataset, "arm": t.arm, "trial": t.trial} for t in order],
    }
    artefacts.write_text(Path(out) / PLAN_FILE, json.dumps(document, indent=2) + "\n")
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


def _ledger(out: Path, trial: Trial) -> tuple[str, list] | None:
    """The run's gate verdict and role rows from its usage.json, if it has a verdict."""
    path = Path(out) / RUNS_DIR / trial.folder / USAGE_FILE
    try:
        ledger = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    gate = ledger.get("gate") if isinstance(ledger, dict) else None
    if gate not in ("PASS", "FAIL"):
        return None
    roles = ledger.get("roles")
    return gate, roles if isinstance(roles, list) else []


def _spend(roles: list) -> float | None:
    """What the roles cost together, known only when every role's cost is."""
    costs = [_cost(role) for role in roles]
    return sum(costs) if costs and all(cost is not None for cost in costs) else None


def outcome(out: Path, trial: Trial) -> Outcome:
    """The run's verdict from its usage.json, or no verdict if it left none.

    The run's cost is known only when every role's cost is.
    """
    found = _ledger(out, trial)
    if found is None:
        return Outcome(trial, None)
    gate, roles = found
    return Outcome(trial, gate, _spend(roles))


# A repair round's row in usage.json: "engineer repair 1", "engineer repair 2".
REPAIR_ROW = re.compile(r" repair [1-9][0-9]*$")


@dataclass(frozen=True)
class Rounds:
    """A repairing run read as two: its first round alone, and the whole run.

    A repair is made only after a round fails, so the first round passed
    exactly when the run passed with no repair row in its usage.
    """

    trial: Trial
    first: str | None
    final: str | None
    first_usd: float | None = None
    total_usd: float | None = None


def rounds(out: Path, trial: Trial) -> Rounds:
    """The first round's and the run's verdicts and spend, or none if the run has no verdict."""
    found = _ledger(out, trial)
    if found is None:
        return Rounds(trial, None, None)
    gate, roles = found
    names = [role.get("role") if isinstance(role, dict) else None for role in roles]
    repaired = [isinstance(n, str) and REPAIR_ROW.search(n) is not None for n in names]
    first_roles = [role for role, repair in zip(roles, repaired) if not repair]
    first = "FAIL" if any(repaired) else gate
    return Rounds(trial, first, gate, _spend(first_roles), _spend(roles))


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
    baseline: str = BASELINE
    # For an architect comparison: each architect run with a verdict, its
    # verdict and its agreement.json (None if the run left none).
    agreements: tuple[tuple[Trial, str, dict | None], ...] = ()

    def to_dict(self) -> dict:
        return {
            **({"spec_agreement": self._agreements()} if self.agreements else {}),
            "baseline": self.baseline,
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

    def _agreements(self) -> list[dict]:
        return [
            {"run": trial.folder, "gate": gate, **(found or {"spec": None})}
            for trial, gate, found in self.agreements
        ]


def read_agreement(out: Path, trial: Trial) -> dict | None:
    """The run's agreement.json, or None if it has none that can be read."""
    path = Path(out) / RUNS_DIR / trial.folder / AGREEMENT_FILE
    try:
        found = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return found if isinstance(found, dict) and isinstance(found.get("spec"), bool) else None


def agreement_text(found: dict | None) -> str:
    """One run's agreement in words."""
    if found is None:
        return "not scored"
    if not found["spec"]:
        return "no spec"
    differ = [f"{name}: {', '.join(d)}" for name, d in found["models"].items() if d]
    extra = [f"extra: {', '.join(found['extra'])}"] if found["extra"] else []
    return "; ".join([f"{found['agreeing']} of {found['declared']} models agree", *differ, *extra])


def _verdict(arm: str, better: float | None, worse: float | None, baseline: str) -> str:
    if arm == baseline:
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
    baseline = ARCHITECT_BASELINE if document.get("mode") == ARCHITECT else BASELINE
    base = _tally([o for o in done if o.trial.arm == baseline])
    summaries = []
    for arm in arms:
        mine = [o for o in done if o.trial.arm == arm]
        tally = _tally(mine)
        better = worse = None
        if arm != baseline and tally.runs and base.runs:
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
                verdict=_verdict(arm, better, worse, baseline),
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
        baseline=baseline,
        agreements=tuple(
            (o.trial, o.gate, read_agreement(out, o.trial))
            for o in done
            if document.get("mode") == ARCHITECT and o.trial.arm == ARCHITECT
        ),
    )


def _percent(value: float) -> str:
    return f"{100 * value:.0f}%"


def _money(value: float | None) -> str:
    return "-" if value is None else f"{value:.4f}"


def render(summary: Summary) -> str:
    """The comparison as plain text tables."""
    counted = summary.planned - len(summary.without_verdict)
    trials = f"{summary.trials} trial{'s' if summary.trials != 1 else ''}"
    heading = (
        f"comparison: {len(summary.datasets)} datasets, {trials} each, "
        f"{counted} of {summary.planned} runs have a gate verdict"
    )
    rule = (
        f"an arm helps (or hurts) when the chance that its pass rate is higher "
        f"(or lower) than {summary.baseline}'s is at least {DECISION}"
    )
    lines = [heading, rule, ""]
    rows = []
    for arm in summary.arms:
        tally = arm.tally
        rows.append(
            [
                arm.arm,
                arm.verdict,
                str(tally.runs),
                str(tally.passes),
                _percent(tally.passes / tally.runs) if tally.runs else "-",
                f"{_percent(arm.low)} to {_percent(arm.high)}",
                "-" if arm.better is None else f"{arm.better:.3f}",
                _money(arm.cost_usd / tally.runs if arm.cost_usd is not None else None),
                _money(arm.cost_per_pass),
            ]
        )
    header = [
        "arm",
        "verdict",
        "runs",
        "passes",
        "rate",
        "95% interval",
        f"P(> {summary.baseline})",
        "US$ per run",
        "US$ per pass",
    ]
    lines += _table(header, rows, left=2)
    lines += ["", "passes by dataset", ""]
    names = [arm.arm for arm in summary.arms]
    rows = [
        [dataset]
        + [f"{t.passes}/{t.runs}" for t in (summary.by_dataset[dataset][n] for n in names)]
        for dataset in summary.datasets
    ]
    lines += _table(["dataset", *names], rows, left=1)
    if summary.agreements:
        lines += ["", *_agreement_lines(summary.agreements)]
    if summary.without_verdict:
        lines += ["", "runs with no gate verdict, not counted:"]
        lines += [f"  {trial.folder}" for trial in summary.without_verdict]
    lines += ["", "cost is Claude Code's own estimate, not a bill"]
    return "\n".join(lines) + "\n"


def _agreement_lines(agreements) -> list[str]:
    agree = sum(
        f is not None and f["spec"] and f["agreeing"] == f["declared"] for _, _, f in agreements
    )
    no_spec = sum(f is not None and not f["spec"] for _, _, f in agreements)
    lines = [
        "each architect run's spec against the declaration (keys, sources, rows to keep",
        "and money; how sources are matched is judged by the gate alone)",
        (
            f"{agree} of {len(agreements)} specs agree on every declared model; "
            f"{no_spec} {'run' if no_spec == 1 else 'runs'} had no spec"
        ),
        "",
    ]
    rows = [[trial.folder, gate, agreement_text(found)] for trial, gate, found in agreements]
    return lines + _table(["run", "gate", "spec"], rows, left=3)


def _table(header: list[str], rows: list[list[str]], left: int) -> list[str]:
    widths = [max(len(row[i]) for row in [header, *rows]) for i in range(len(header))]
    return [
        "  ".join(
            cell.ljust(width) if i < left else cell.rjust(width)
            for i, (cell, width) in enumerate(zip(row, widths))
        ).rstrip()
        for row in [header, *rows]
    ]


@dataclass(frozen=True)
class PairedSummary:
    """The paired verdicts, the pass-rate difference and the cost of each runner."""

    datasets: list[str]
    trials: int
    pairs: parity.Pairs
    interval: tuple[float, float] | None
    verdict: str
    cost_per_pass: dict[str, float | None]
    cost_interval: tuple[float, float] | None
    cost_verdict: str
    parity: str
    by_dataset: dict[str, parity.Pairs]
    left_out: tuple[str, ...]
    planned: int

    def to_dict(self) -> dict:
        def counts(pairs: parity.Pairs) -> dict:
            return {
                "both_pass": pairs.both,
                "only_default_passes": pairs.first_only,
                "only_graph_passes": pairs.second_only,
                "neither_passes": pairs.neither,
            }

        n = self.pairs.n

        def finite(value: float | None) -> float | str | None:
            return "infinity" if value == float("inf") else value

        return {
            "mode": PAIRED,
            "datasets": self.datasets,
            "trials": self.trials,
            "planned_pairs": self.planned,
            "pairs": counts(self.pairs),
            "pass_rate": {
                "default": (self.pairs.both + self.pairs.first_only) / n if n else None,
                "graph": (self.pairs.both + self.pairs.second_only) / n if n else None,
            },
            "difference": self.pairs.difference if n else None,
            "interval_95": list(self.interval) if self.interval else None,
            "margin": parity.MARGIN,
            "verdict": self.verdict,
            "cost_per_pass_usd": {k: finite(v) for k, v in self.cost_per_pass.items()},
            "cost_ratio_interval_95": (
                [finite(v) for v in self.cost_interval] if self.cost_interval else None
            ),
            "cost_limit": parity.COST_LIMIT,
            "cost_verdict": self.cost_verdict,
            "parity": self.parity,
            "by_dataset": {name: counts(pairs) for name, pairs in self.by_dataset.items()},
            "pairs_left_out": list(self.left_out),
        }


def _count(pairs: list[tuple[Outcome, Outcome]]) -> parity.Pairs:
    verdicts = [(d.gate == "PASS", g.gate == "PASS") for d, g in pairs]
    return parity.Pairs(
        both=verdicts.count((True, True)),
        first_only=verdicts.count((True, False)),
        second_only=verdicts.count((False, True)),
        neither=verdicts.count((False, False)),
    )


def summarise_paired(out: Path) -> PairedSummary:
    """Count the pairs where both runs have a verdict, and compare the runners on them."""
    document, order = read_plan(out)
    found = {(t.dataset, t.trial, t.arm): outcome(out, t) for t in order}
    keys = sorted({(t.dataset, t.trial) for t in order}, key=lambda k: (k[1], k[0]))
    counted, left_out = [], []
    for dataset, trial in keys:
        first, second = (found[(dataset, trial, arm)] for arm in PAIRED_ARMS)
        if first.gate is None or second.gate is None:
            left_out.append(f"{dataset}/{trial}")
        else:
            counted.append((first, second))
    pairs = _count(counted)
    low_high = parity.interval(pairs) if counted else None
    verdict = parity.verdict(*low_high) if low_high else "no pairs"
    costs = [
        parity.PairCost(d.gate == "PASS", d.cost_usd, g.gate == "PASS", g.cost_usd)
        for d, g in counted
    ]
    known = bool(costs) and all(c.first_usd is not None and c.second_usd is not None for c in costs)
    per_pass = {arm: None for arm in PAIRED_ARMS}
    cost_low_high = None
    cost_verdict = parity.UNKNOWN
    if known:
        per_pass = {
            "default": parity.cost_per_pass(
                [c.first_usd for c in costs], pairs.both + pairs.first_only
            ),
            "graph": parity.cost_per_pass(
                [c.second_usd for c in costs], pairs.both + pairs.second_only
            ),
        }
        cost_low_high = parity.cost_interval(costs, seed=document.get("seed", 0))
        cost_verdict = parity.cost_verdict(*cost_low_high)
    datasets = document["datasets"]
    return PairedSummary(
        datasets=datasets,
        trials=document["trials"],
        pairs=pairs,
        interval=low_high,
        verdict=verdict,
        cost_per_pass=per_pass,
        cost_interval=cost_low_high,
        cost_verdict=cost_verdict,
        parity=parity.parity(verdict, cost_verdict) if low_high else "no pairs",
        by_dataset={
            name: _count([(d, g) for d, g in counted if d.trial.dataset == name])
            for name in datasets
        },
        left_out=tuple(left_out),
        planned=len(keys),
    )


def _points(value: float) -> str:
    return f"{100 * value:+.1f}"


def _ratio(value: float) -> str:
    return "infinite" if value == float("inf") else f"{value:.3f}"


def render_paired(summary: PairedSummary) -> str:
    """The paired comparison as plain text."""
    pairs = summary.pairs
    trials = f"{summary.trials} trial{'s' if summary.trials != 1 else ''}"
    lines = [
        (
            f"paired comparison: {len(summary.datasets)} datasets, {trials} each, "
            f"{pairs.n} of {summary.planned} pairs have both gate verdicts"
        ),
        (
            "difference = default's pass rate minus graph's; the graph is non-inferior "
            f"when the 95% interval lies below +{100 * parity.MARGIN:.0f} points"
        ),
        "",
    ]
    if not pairs.n:
        return "\n".join(lines + ["no pairs to compare yet"]) + "\n"
    rows = [
        ["both pass", str(pairs.both)],
        ["only default passes", str(pairs.first_only)],
        ["only graph passes", str(pairs.second_only)],
        ["neither passes", str(pairs.neither)],
    ]
    lines += _table(["pairs", "count"], rows, left=1)
    low, high = summary.interval
    default_rate = (pairs.both + pairs.first_only) / pairs.n
    graph_rate = (pairs.both + pairs.second_only) / pairs.n
    lines += [
        "",
        f"pass rate: default {_percent(default_rate)}, graph {_percent(graph_rate)}",
        (
            f"difference: {_points(pairs.difference)} points, 95% interval (Tango) "
            f"{_points(low)} to {_points(high)}"
        ),
        f"pass rate verdict: {summary.verdict}",
    ]
    if summary.verdict == parity.NON_INFERIOR and low > 0:
        lines.append("  (the graph's pass rate is lower, by less than the margin)")
    cost = summary.cost_per_pass
    lines.append("")
    if summary.cost_interval is None:
        lines.append("cost per pass: unknown (a counted run's cost was not reported)")
    else:
        cost_low, cost_high = summary.cost_interval
        shown = {arm: "-" if v == float("inf") else _money(v) for arm, v in cost.items()}
        lines += [
            f"US$ per pass: default {shown['default']}, graph {shown['graph']}",
            (
                "graph / default: 95% interval (bootstrap over pairs) "
                f"{_ratio(cost_low)} to {_ratio(cost_high)}; limit {parity.COST_LIMIT:.2f}"
            ),
        ]
    lines += [f"cost verdict: {summary.cost_verdict}", "", f"parity: {summary.parity}"]
    lines += ["", "pairs by dataset (both, only default, only graph, neither)", ""]
    rows = [
        [name, *(str(v) for v in (p.both, p.first_only, p.second_only, p.neither))]
        for name, p in summary.by_dataset.items()
    ]
    lines += _table(["dataset", "both", "default", "graph", "neither"], rows, left=1)
    if summary.left_out:
        lines += ["", "pairs without both gate verdicts, not counted:"]
        lines += [f"  {name}" for name in summary.left_out]
    lines += ["", "cost is Claude Code's own estimate, not a bill"]
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class RepairSummary:
    """Repairing runs against their own first rounds, and the cost per pass of each."""

    datasets: list[str]
    trials: int
    repairs: int
    cross_source: bool
    # First: the run with its repairs; second: its first round alone.
    pairs: parity.Pairs
    interval: tuple[float, float] | None
    verdict: str
    cost_per_pass: dict[str, float | None]
    cost_interval: tuple[float, float] | None
    by_dataset: dict[str, parity.Pairs]
    left_out: tuple[str, ...]
    planned: int

    def to_dict(self) -> dict:
        def counts(pairs: parity.Pairs) -> dict:
            return {
                "passed_first_time": pairs.both,
                "repaired": pairs.first_only,
                "failed_after_passing": pairs.second_only,
                "failed": pairs.neither,
            }

        def finite(value: float | None) -> float | str | None:
            return "infinity" if value == float("inf") else value

        n = self.pairs.n
        return {
            "mode": REPAIRS,
            "datasets": self.datasets,
            "trials": self.trials,
            "repairs": self.repairs,
            "cross_source": self.cross_source,
            "planned_runs": self.planned,
            "runs": counts(self.pairs),
            "pass_rate": {
                "first_round": (self.pairs.both + self.pairs.second_only) / n if n else None,
                "with_repairs": (self.pairs.both + self.pairs.first_only) / n if n else None,
            },
            "difference": self.pairs.difference if n else None,
            "interval_95": list(self.interval) if self.interval else None,
            "verdict": self.verdict,
            "cost_per_pass_usd": {k: finite(v) for k, v in self.cost_per_pass.items()},
            "cost_ratio_interval_95": (
                [finite(v) for v in self.cost_interval] if self.cost_interval else None
            ),
            "by_dataset": {name: counts(pairs) for name, pairs in self.by_dataset.items()},
            "runs_left_out": list(self.left_out),
        }


def _count_rounds(found: list[Rounds]) -> parity.Pairs:
    verdicts = [(r.final == "PASS", r.first == "PASS") for r in found]
    return parity.Pairs(
        both=verdicts.count((True, True)),
        first_only=verdicts.count((True, False)),
        second_only=verdicts.count((False, True)),
        neither=verdicts.count((False, False)),
    )


def summarise_repairs(out: Path) -> RepairSummary:
    """Pair each run that has a verdict with its own first round, and compare them."""
    document, order = read_plan(out)
    found = [rounds(out, trial) for trial in order]
    counted = [r for r in found if r.final is not None]
    pairs = _count_rounds(counted)
    low_high = parity.interval(pairs) if counted else None
    per_pass = {"first round": None, "with repairs": None}
    cost_low_high = None
    if counted and all(r.first_usd is not None and r.total_usd is not None for r in counted):
        costs = [
            parity.PairCost(r.first == "PASS", r.first_usd, r.final == "PASS", r.total_usd)
            for r in counted
        ]
        per_pass = {
            "first round": parity.cost_per_pass(
                [c.first_usd for c in costs], pairs.both + pairs.second_only
            ),
            "with repairs": parity.cost_per_pass(
                [c.second_usd for c in costs], pairs.both + pairs.first_only
            ),
        }
        cost_low_high = parity.cost_interval(costs, seed=document.get("seed", 0))
    datasets = document["datasets"]
    return RepairSummary(
        datasets=datasets,
        trials=document["trials"],
        repairs=document.get("repairs"),
        cross_source=document.get("cross_source"),
        pairs=pairs,
        interval=low_high,
        verdict=parity.superiority(*low_high) if low_high else "no runs",
        cost_per_pass=per_pass,
        cost_interval=cost_low_high,
        by_dataset={
            name: _count_rounds([r for r in counted if r.trial.dataset == name])
            for name in datasets
        },
        left_out=tuple(r.trial.folder for r in found if r.final is None),
        planned=len(order),
    )


def render_repairs(summary: RepairSummary) -> str:
    """The repair comparison as plain text."""
    pairs = summary.pairs
    trials = f"{summary.trials} trial{'s' if summary.trials != 1 else ''}"
    who = "with the cross-source agent" if summary.cross_source else "engineer alone"
    lines = [
        (
            f"repair comparison: {len(summary.datasets)} datasets, {trials} each, "
            f"up to {summary.repairs} repairs, {who}; "
            f"{pairs.n} of {summary.planned} runs have a gate verdict"
        ),
        (
            "each run is paired with its own first round; difference = pass rate with "
            "repairs minus first round's; repairs are better when the 95% interval lies above 0"
        ),
        "",
    ]
    if not pairs.n:
        return "\n".join(lines + ["no runs to compare yet"]) + "\n"
    rows = [
        ["passed first time", str(pairs.both)],
        ["repaired", str(pairs.first_only)],
        ["failed", str(pairs.neither)],
    ]
    if pairs.second_only:
        rows.insert(2, ["failed after passing", str(pairs.second_only)])
    lines += _table(["runs", "count"], rows, left=1)
    low, high = summary.interval
    first = (pairs.both + pairs.second_only) / pairs.n
    final = (pairs.both + pairs.first_only) / pairs.n
    lines += [
        "",
        f"pass rate: first round {_percent(first)}, with repairs {_percent(final)}",
        (
            f"difference: {_points(pairs.difference)} points, 95% interval (Tango) "
            f"{_points(low)} to {_points(high)}"
        ),
        f"verdict: {summary.verdict}",
        "",
    ]
    if summary.cost_interval is None:
        lines.append("cost per pass: unknown (a counted run's cost was not reported)")
    else:
        cost_low, cost_high = summary.cost_interval
        shown = {
            name: "-" if v == float("inf") else _money(v)
            for name, v in summary.cost_per_pass.items()
        }
        lines += [
            (
                f"US$ per pass: first round {shown['first round']}, "
                f"with repairs {shown['with repairs']}"
            ),
            (
                "with repairs / first round: 95% interval (bootstrap over runs) "
                f"{_ratio(cost_low)} to {_ratio(cost_high)}"
            ),
        ]
    lines += ["", "runs by dataset (first time, repaired, failed)", ""]
    rows = [
        [name, str(p.both), str(p.first_only), str(p.neither + p.second_only)]
        for name, p in summary.by_dataset.items()
    ]
    lines += _table(["dataset", "first time", "repaired", "failed"], rows, left=1)
    if summary.left_out:
        lines += ["", "runs without a gate verdict, not counted:"]
        lines += [f"  {name}" for name in summary.left_out]
    lines += ["", "cost is Claude Code's own estimate, not a bill"]
    return "\n".join(lines) + "\n"


def report(out: Path) -> str:
    """Summarise the comparison in out, and write it there as text and JSON."""
    document, _ = read_plan(out)
    if document.get("mode") == PAIRED:
        summary = summarise_paired(out)
        text = render_paired(summary)
    elif document.get("mode") == REPAIRS:
        summary = summarise_repairs(out)
        text = render_repairs(summary)
    else:
        summary = summarise(out)
        text = render(summary)
    artefacts.write_text(Path(out) / REPORT_TEXT, text)
    artefacts.write_text(Path(out) / REPORT_JSON, json.dumps(summary.to_dict(), indent=2) + "\n")
    return text


class CostNotApproved(ValueError):
    """The comparison is estimated above the policy's limit and was not approved."""


@dataclass(frozen=True)
class Estimate:
    """What the runs still to do are expected to cost, and the most they can reserve."""

    runs: int
    typical_usd: Decimal
    ceiling_usd: Decimal

    def describe(self) -> str:
        runs = f"{self.runs} run{'s' if self.runs != 1 else ''}"
        return (
            f"{runs} to do, estimated at US${self.typical_usd}, at most "
            f"US${self.ceiling_usd} at each agent call's cap (Claude Code's own estimates)"
        )


def agent_calls(arm: str, tables: int) -> list[str]:
    """The kind of each agent call one run of the arm makes: a scout per raw table.

    The architect is priced by the kind of call its role is capped as.
    """
    if arm == ARCHITECT:
        return [CROSS_SOURCE, ARCHITECT_ROLE.budget_kind, ENGINEER]
    scouts, cross_source = (False, True) if arm in PAIRED_ARMS else ARMS[arm]
    return [SCOUT] * (tables if scouts else 0) + [CROSS_SOURCE] * cross_source + [ENGINEER]


def repair_calls(repairs: int, cross_source: bool) -> list[str]:
    """Every agent call a repairing run can make: each repair is another engineer call."""
    return [CROSS_SOURCE] * cross_source + [ENGINEER] * (1 + repairs)


def estimate(
    trials: Sequence[Trial], tables: dict[str, int], rules: Policy, calls: list[str] | None = None
) -> Estimate:
    """Price the given runs from each dataset's number of raw tables.

    ``calls``, when given, are the calls every run may make, as for a
    repair comparison, priced as if each were made.
    """
    calls = [
        kind
        for t in trials
        for kind in (calls if calls is not None else agent_calls(t.arm, tables[t.dataset]))
    ]
    return Estimate(
        len(trials),
        sum((rules.typical_usd[kind] for kind in calls), Decimal(0)),
        sum((rules.cap(kind) for kind in calls), Decimal(0)),
    )


def _order(
    out: Path,
    datasets: Sequence[str],
    arms: Sequence[str],
    trials: int,
    seed: int,
    mode: str | None = None,
    settings: dict | None = None,
) -> list[Trial]:
    """The planned order: the one kept in out, or a new one. Writes nothing."""
    if (out / PLAN_FILE).is_file():
        document, order = read_plan(out)
        settings = settings or {}
        asked = (mode, list(datasets), list(arms), trials, seed, settings)
        kept = (
            document.get("mode"),
            document["datasets"],
            document["arms"],
            document["trials"],
            document.get("seed"),
            {name: document.get(name) for name in settings},
        )
        if asked != kept:
            raise ValueError(
                f"{out} holds a comparison of another kind, or of other datasets, arms, "
                "trials, seed or settings; use another folder"
            )
        return order
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise ValueError(f"{out} must be empty or hold a comparison to resume")
    return _planned(datasets, arms, trials, seed, mode)


def _start(
    out: Path,
    datasets: Sequence[str],
    arms: Sequence[str],
    trials: int,
    seed: int,
    mode: str | None = None,
    settings: dict | None = None,
) -> list[Trial]:
    """Check out and keep the plan there, so the comparison can resume."""
    order = _order(out, datasets, arms, trials, seed, mode, settings)
    if not (out / PLAN_FILE).is_file():
        out.mkdir(parents=True, exist_ok=True)
        write_plan(out, datasets, arms, trials, seed, mode, settings)
    return order


def _check_inputs(datasets: dict[str, Path], goal: Path, checks: Path, parallel: int) -> None:
    if not isinstance(parallel, int) or isinstance(parallel, bool) or parallel < 1:
        raise ValueError(f"parallel must be a whole number of at least 1, not {parallel!r}")
    for name, folder in datasets.items():
        if not Path(folder).is_dir():
            raise ValueError(f"the raw folder for {name}, {folder}, is not a folder")
    if not Path(goal).is_file():
        raise ValueError(f"the goal {goal} is not a file")
    load_declaration(checks)


def _execute(out: Path, order: list[Trial], call, parallel: int, progress) -> None:
    """Run every planned run without a verdict, at most `parallel` at a time.

    `call(trial, folder)` makes one run in an empty folder; an error is
    reported and the run left without a verdict.
    """
    pending = [trial for trial in order if outcome(out, trial).gate is None]
    lock = threading.Lock()
    finished = [len(order) - len(pending)]

    def one(trial: Trial) -> None:
        folder = out / RUNS_DIR / trial.folder
        if folder.exists():
            shutil.rmtree(folder)
        try:
            call(trial, folder)
            gate = outcome(out, trial).gate
            note = f"gate {gate}" if gate else "no gate verdict"
        except (ArmError, EngineerError, GateError, OSError) as error:
            note = f"error: {error}"
        with lock:
            finished[0] += 1
            progress(f"[{finished[0]}/{len(order)}] {trial.folder}: {note}")

    with ThreadPoolExecutor(max_workers=parallel) as pool:
        for future in [pool.submit(one, trial) for trial in pending]:
            future.result()


class ArmError(Exception):
    """A run of one arm of a paired comparison stopped with an error."""


def run_comparison(
    datasets: dict[str, Path],
    goal: Path,
    checks: Path,
    out: Path,
    arms: Sequence[str] = tuple(ARMS),
    trials: int = 4,
    seed: int = 0,
    parallel: int = 3,
    claude: str = "claude",
    runner=None,
    progress=print,
    approve_cost: bool = False,
) -> str:
    """Run every arm on every dataset, then report the comparison.

    Runs start in the planned order, at most `parallel` at a time. A run
    that cannot start, or stops with an error, is reported and left without
    a verdict; the comparison carries on. `runner` stands in for the
    engineer runner in tests.

    The runs still to do are priced first; above the policy's limit nothing
    is written or run unless `approve_cost` is true.
    """
    runner = runner or engineer_run
    _check_inputs(datasets, goal, checks, parallel)
    out = Path(out)
    rules = Policy()
    tables = {name: len(discover(Path(folder))) for name, folder in datasets.items()}
    order = _order(out, list(datasets), arms, trials, seed)
    cost = estimate([t for t in order if outcome(out, t).gate is None], tables, rules)
    if cost.typical_usd > rules.approve_above_usd and not approve_cost:
        raise CostNotApproved(
            f"{cost.describe()}; above US${rules.approve_above_usd}, so it needs approval: "
            "run it again with --approve-cost to go ahead"
        )
    progress(cost.describe())
    order = _start(out, list(datasets), arms, trials, seed)

    def call(trial: Trial, folder: Path) -> None:
        scouts, cross_source = ARMS[trial.arm]
        runner(
            datasets[trial.dataset],
            goal,
            checks,
            folder,
            claude=claude,
            scouts=scouts,
            cross_source=cross_source,
        )

    _execute(out, order, call, parallel, progress)
    return report(out)


def run_default(raw: Path, goal: Path, checks: Path, out: Path, claude: str) -> None:
    """Today's runner with the cross-source agent, as make engineer runs it."""
    engineer_run(raw, goal, checks, out, claude=claude, cross_source=True)


def run_graph(raw: Path, goal: Path, checks: Path, out: Path, claude: str, **options) -> None:
    """The same run as a graph. Imported here, so other modes do not need the graph extra.

    ``options`` go to the graph run in place of the cross-source agent alone.
    """
    from tenx.policy import PolicyError
    from tenx.ports import PlatformError
    from tenx.run import RunError, start
    from tenx.sandbox import SandboxError

    try:
        start(raw, goal, checks, out, **(options or {"cross_source": True}), claude=claude)
    except (RunError, PlatformError, SandboxError, PolicyError) as error:
        raise ArmError(str(error)) from error


PAIRED_RUNNERS = {"default": run_default, "graph": run_graph}


def run_paired(
    datasets: dict[str, Path],
    goal: Path,
    checks: Path,
    out: Path,
    trials: int = 8,
    seed: int = 0,
    parallel: int = 3,
    claude: str = "claude",
    runners: dict | None = None,
    progress=print,
    approve_cost: bool = False,
) -> str:
    """Run the default runner and the graph on every dataset, trials times, then report.

    Each pair's two runs are next to each other in the order. A run that
    cannot start, or stops with an error, is reported and left without a
    verdict, and its pair is not counted; the comparison carries on and a
    second start runs it again. `runners` stands in for the two runners in
    tests. Priced first; above the policy's limit nothing is written or run
    unless `approve_cost` is true.
    """
    runners = runners or PAIRED_RUNNERS
    if set(runners) != set(PAIRED_ARMS):
        raise ValueError(f"a runner is needed for each of {', '.join(PAIRED_ARMS)}")
    _check_inputs(datasets, goal, checks, parallel)
    out = Path(out)
    rules = Policy()
    tables = {name: len(discover(Path(folder))) for name, folder in datasets.items()}
    order = _order(out, list(datasets), PAIRED_ARMS, trials, seed, PAIRED)
    cost = estimate([t for t in order if outcome(out, t).gate is None], tables, rules)
    if cost.typical_usd > rules.approve_above_usd and not approve_cost:
        raise CostNotApproved(
            f"{cost.describe()}; above US${rules.approve_above_usd}, so it needs approval: "
            "run it again with --approve-cost to go ahead"
        )
    progress(cost.describe())
    order = _start(out, list(datasets), PAIRED_ARMS, trials, seed, PAIRED)

    def call(trial: Trial, folder: Path) -> None:
        runners[trial.arm](datasets[trial.dataset], goal, checks, folder, claude)

    _execute(out, order, call, parallel, progress)
    return report(out)


def run_repairs(
    datasets: dict[str, Path],
    goal: Path,
    checks: Path,
    out: Path,
    trials: int = 4,
    repairs: int = 2,
    cross_source: bool = True,
    seed: int = 0,
    parallel: int = 3,
    claude: str = "claude",
    runner=None,
    progress=print,
    approve_cost: bool = False,
) -> str:
    """Run the graph with repairs on every dataset, trials times, then report.

    Each run is compared with its own first round, which is a run without
    repairs. An error leaves the run without a verdict and out of every
    figure; a second start runs it again. `runner` stands in for the graph
    run in tests. Every call a run may make is priced first; above the
    policy's limit nothing is written or run unless `approve_cost` is true.
    """
    if not isinstance(repairs, int) or isinstance(repairs, bool) or not 1 <= repairs <= 3:
        raise ValueError(f"repairs must be a whole number from 1 to 3, not {repairs!r}")
    runner = runner or run_graph
    _check_inputs(datasets, goal, checks, parallel)
    out = Path(out)
    rules = Policy()
    tables = {name: len(discover(Path(folder))) for name, folder in datasets.items()}
    settings = {"repairs": repairs, "cross_source": cross_source}
    order = _order(out, list(datasets), [REPAIR_ARM], trials, seed, REPAIRS, settings)
    pending = [t for t in order if outcome(out, t).gate is None]
    cost = estimate(pending, tables, rules, repair_calls(repairs, cross_source))
    if cost.typical_usd > rules.approve_above_usd and not approve_cost:
        raise CostNotApproved(
            f"{cost.describe()}; above US${rules.approve_above_usd}, so it needs approval: "
            "run it again with --approve-cost to go ahead"
        )
    progress(cost.describe())
    order = _start(out, list(datasets), [REPAIR_ARM], trials, seed, REPAIRS, settings)

    def call(trial: Trial, folder: Path) -> None:
        runner(
            datasets[trial.dataset],
            goal,
            checks,
            folder,
            claude,
            cross_source=cross_source,
            repairs=repairs,
        )

    _execute(out, order, call, parallel, progress)
    return report(out)


def score_run(folder: Path, declared: list, wanted: dict[str, int]) -> dict:
    """Score the run's spec against the declaration and keep it in the run's folder."""
    found = last_contract(folder)
    if found is None:
        result = {"spec": False}
    else:
        contract, kept = found
        result = {"spec": True, **score(contract, kept, declared, wanted).to_dict()}
    artefacts.write_text(Path(folder) / AGREEMENT_FILE, json.dumps(result, indent=2) + "\n")
    return result


def run_architect(
    datasets: dict[str, Path],
    goal: Path,
    checks: Path,
    out: Path,
    trials: int = 4,
    seed: int = 0,
    parallel: int = 3,
    claude: str = "claude",
    runner=None,
    progress=print,
    approve_cost: bool = False,
) -> str:
    """Run the graph with and without the architect on every dataset, trials times, then report.

    Both arms run the cross-source agent; only the architect arm runs the
    architect. Each architect run's spec is scored against the declaration
    when the run finishes. An error leaves the run without a verdict, and a
    second start runs it again. `runner` stands in for the graph run in
    tests. Priced first; above the policy's limit nothing is written or run
    unless `approve_cost` is true.
    """
    runner = runner or run_graph
    _check_inputs(datasets, goal, checks, parallel)
    out = Path(out)
    rules = Policy()
    tables = {name: len(discover(Path(folder))) for name, folder in datasets.items()}
    order = _order(out, list(datasets), ARCHITECT_ARMS, trials, seed, ARCHITECT)
    cost = estimate([t for t in order if outcome(out, t).gate is None], tables, rules)
    if cost.typical_usd > rules.approve_above_usd and not approve_cost:
        raise CostNotApproved(
            f"{cost.describe()}; above US${rules.approve_above_usd}, so it needs approval: "
            "run it again with --approve-cost to go ahead"
        )
    progress(cost.describe())
    declared = load_declaration(checks)
    wanted = {name: declared_rows(declared, folder) for name, folder in datasets.items()}
    order = _start(out, list(datasets), ARCHITECT_ARMS, trials, seed, ARCHITECT)

    def call(trial: Trial, folder: Path) -> None:
        architect = trial.arm == ARCHITECT
        runner(
            datasets[trial.dataset],
            goal,
            checks,
            folder,
            claude,
            cross_source=True,
            architect=architect,
        )
        if architect:
            score_run(folder, declared, wanted[trial.dataset])

    _execute(out, order, call, parallel, progress)
    return report(out)


def _dataset(text: str) -> tuple[str, Path]:
    name, sep, folder = text.partition("=")
    if not sep or not name or not folder:
        raise argparse.ArgumentTypeError(f"expected NAME=FOLDER, not {text!r}")
    return name, Path(folder)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("run", help="run the comparison, or resume it, then report it")
    start.add_argument(
        "--raw",
        type=_dataset,
        action="append",
        required=True,
        metavar="NAME=FOLDER",
        help="a dataset: its name and its folder of raw files; give one --raw per dataset",
    )
    start.add_argument("--goal", type=Path, required=True, help="what to build, in plain English")
    start.add_argument(
        "--checks", type=Path, required=True, help="the gate's JSON declaration of what to expect"
    )
    start.add_argument("--out", type=Path, required=True, help="folder for the comparison")
    start.add_argument(
        "--arms", nargs="+", default=list(ARMS), choices=list(ARMS), help="arms to compare"
    )
    start.add_argument("--trials", type=int, default=4, help="runs of each arm on each dataset")
    start.add_argument("--seed", type=int, default=0, help="seed for the order of the runs")
    start.add_argument("--parallel", type=int, default=3, help="runs at the same time")
    start.add_argument(
        "--approve-cost",
        action="store_true",
        help="run even when the comparison is estimated above US$5",
    )
    paired = commands.add_parser(
        "paired",
        help="run the default runner and the graph on the same datasets, or resume, then report",
    )
    paired.add_argument(
        "--raw",
        type=_dataset,
        action="append",
        required=True,
        metavar="NAME=FOLDER",
        help="a dataset: its name and its folder of raw files; give one --raw per dataset",
    )
    paired.add_argument("--goal", type=Path, required=True, help="what to build, in plain English")
    paired.add_argument(
        "--checks", type=Path, required=True, help="the gate's JSON declaration of what to expect"
    )
    paired.add_argument("--out", type=Path, required=True, help="folder for the comparison")
    paired.add_argument("--trials", type=int, default=8, help="pairs on each dataset")
    paired.add_argument("--seed", type=int, default=0, help="seed for the order of the runs")
    paired.add_argument("--parallel", type=int, default=3, help="runs at the same time")
    paired.add_argument(
        "--approve-cost",
        action="store_true",
        help="run even when the comparison is estimated above US$5",
    )
    fix = commands.add_parser(
        "repairs",
        help="run the graph with repairs, each run against its own first round, then report",
    )
    fix.add_argument(
        "--raw",
        type=_dataset,
        action="append",
        required=True,
        metavar="NAME=FOLDER",
        help="a dataset: its name and its folder of raw files; give one --raw per dataset",
    )
    fix.add_argument("--goal", type=Path, required=True, help="what to build, in plain English")
    fix.add_argument(
        "--checks", type=Path, required=True, help="the gate's JSON declaration of what to expect"
    )
    fix.add_argument("--out", type=Path, required=True, help="folder for the comparison")
    fix.add_argument("--trials", type=int, default=4, help="runs on each dataset")
    fix.add_argument(
        "--repairs", type=int, default=2, choices=range(1, 4), help="repairs a run may make"
    )
    fix.add_argument(
        "--cross-source",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="run the cross-source agent first (default: on); --no-cross-source for the engineer alone",
    )
    fix.add_argument("--seed", type=int, default=0, help="seed for the order of the runs")
    fix.add_argument("--parallel", type=int, default=3, help="runs at the same time")
    fix.add_argument(
        "--approve-cost",
        action="store_true",
        help="run even when the comparison is estimated above US$5",
    )
    spec = commands.add_parser(
        "architect",
        help="run the graph with and without the architect on the same datasets, then report",
    )
    spec.add_argument(
        "--raw",
        type=_dataset,
        action="append",
        required=True,
        metavar="NAME=FOLDER",
        help="a dataset: its name and its folder of raw files; give one --raw per dataset",
    )
    spec.add_argument("--goal", type=Path, required=True, help="what to build, in plain English")
    spec.add_argument(
        "--checks", type=Path, required=True, help="the gate's JSON declaration of what to expect"
    )
    spec.add_argument("--out", type=Path, required=True, help="folder for the comparison")
    spec.add_argument("--trials", type=int, default=4, help="runs of each arm on each dataset")
    spec.add_argument("--seed", type=int, default=0, help="seed for the order of the runs")
    spec.add_argument("--parallel", type=int, default=3, help="runs at the same time")
    spec.add_argument(
        "--approve-cost",
        action="store_true",
        help="run even when the comparison is estimated above US$5",
    )
    show = commands.add_parser("report", help="report a comparison from its runs so far")
    show.add_argument("out", type=Path, help="the comparison's folder")
    args = parser.parse_args(argv)
    try:
        if args.command == "report":
            print(report(args.out), end="")
            return 0
        datasets = dict(args.raw)
        if len(datasets) != len(args.raw):
            raise ValueError("each dataset may be named only once")
        if args.command == "architect":
            text = run_architect(
                datasets,
                args.goal,
                args.checks,
                args.out,
                trials=args.trials,
                seed=args.seed,
                parallel=args.parallel,
                approve_cost=args.approve_cost,
            )
        elif args.command == "repairs":
            text = run_repairs(
                datasets,
                args.goal,
                args.checks,
                args.out,
                trials=args.trials,
                repairs=args.repairs,
                cross_source=args.cross_source,
                seed=args.seed,
                parallel=args.parallel,
                approve_cost=args.approve_cost,
            )
        elif args.command == "paired":
            text = run_paired(
                datasets,
                args.goal,
                args.checks,
                args.out,
                trials=args.trials,
                seed=args.seed,
                parallel=args.parallel,
                approve_cost=args.approve_cost,
            )
        else:
            text = run_comparison(
                datasets,
                args.goal,
                args.checks,
                args.out,
                arms=args.arms,
                trials=args.trials,
                seed=args.seed,
                parallel=args.parallel,
                approve_cost=args.approve_cost,
            )
    except (ValueError, EngineerError, GateError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print()
    print(text, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
