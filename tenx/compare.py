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
    python -m tenx.compare report <folder>

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

from tenx import artefacts
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
        f"(or lower) than {BASELINE}'s is at least {DECISION}"
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
        f"P(> {BASELINE})",
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
    if summary.without_verdict:
        lines += ["", "runs with no gate verdict, not counted:"]
        lines += [f"  {trial.folder}" for trial in summary.without_verdict]
    lines += ["", "cost is Claude Code's own estimate, not a bill"]
    return "\n".join(lines) + "\n"


def _table(header: list[str], rows: list[list[str]], left: int) -> list[str]:
    widths = [max(len(row[i]) for row in [header, *rows]) for i in range(len(header))]
    return [
        "  ".join(
            cell.ljust(width) if i < left else cell.rjust(width)
            for i, (cell, width) in enumerate(zip(row, widths))
        ).rstrip()
        for row in [header, *rows]
    ]


def report(out: Path) -> str:
    """Summarise the comparison in out, and write it there as text and JSON."""
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
    """The kind of each agent call one run of the arm makes: a scout per raw table."""
    scouts, cross_source = ARMS[arm]
    return [SCOUT] * (tables if scouts else 0) + [CROSS_SOURCE] * cross_source + [ENGINEER]


def estimate(trials: Sequence[Trial], tables: dict[str, int], rules: Policy) -> Estimate:
    """Price the given runs from each dataset's number of raw tables."""
    calls = [kind for t in trials for kind in agent_calls(t.arm, tables[t.dataset])]
    return Estimate(
        len(trials),
        sum((rules.typical_usd[kind] for kind in calls), Decimal(0)),
        sum((rules.cap(kind) for kind in calls), Decimal(0)),
    )


def _order(
    out: Path, datasets: Sequence[str], arms: Sequence[str], trials: int, seed: int
) -> list[Trial]:
    """The planned order: the one kept in out, or a new one. Writes nothing."""
    if (out / PLAN_FILE).is_file():
        document, order = read_plan(out)
        asked = (list(datasets), list(arms), trials, seed)
        kept = (document["datasets"], document["arms"], document["trials"], document.get("seed"))
        if asked != kept:
            raise ValueError(
                f"{out} holds a comparison of other datasets, arms, trials or seed; "
                "use another folder"
            )
        return order
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise ValueError(f"{out} must be empty or hold a comparison to resume")
    return plan(datasets, arms, trials, seed)


def _start(
    out: Path, datasets: Sequence[str], arms: Sequence[str], trials: int, seed: int
) -> list[Trial]:
    """Check out and keep the plan there, so the comparison can resume."""
    order = _order(out, datasets, arms, trials, seed)
    if not (out / PLAN_FILE).is_file():
        out.mkdir(parents=True, exist_ok=True)
        write_plan(out, datasets, arms, trials, seed)
    return order


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
    if not isinstance(parallel, int) or isinstance(parallel, bool) or parallel < 1:
        raise ValueError(f"parallel must be a whole number of at least 1, not {parallel!r}")
    for name, folder in datasets.items():
        if not Path(folder).is_dir():
            raise ValueError(f"the raw folder for {name}, {folder}, is not a folder")
    if not Path(goal).is_file():
        raise ValueError(f"the goal {goal} is not a file")
    load_declaration(checks)
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
    pending = [trial for trial in order if outcome(out, trial).gate is None]
    lock = threading.Lock()
    finished = [len(order) - len(pending)]

    def one(trial: Trial) -> None:
        folder = out / RUNS_DIR / trial.folder
        if folder.exists():
            shutil.rmtree(folder)
        scouts, cross_source = ARMS[trial.arm]
        try:
            runner(
                datasets[trial.dataset],
                goal,
                checks,
                folder,
                claude=claude,
                scouts=scouts,
                cross_source=cross_source,
            )
            gate = outcome(out, trial).gate
            note = f"gate {gate}" if gate else "no gate verdict"
        except (EngineerError, GateError, OSError) as error:
            note = f"error: {error}"
        with lock:
            finished[0] += 1
            progress(f"[{finished[0]}/{len(order)}] {trial.folder}: {note}")

    with ThreadPoolExecutor(max_workers=parallel) as pool:
        for future in [pool.submit(one, trial) for trial in pending]:
            future.result()
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
