"""Tests for the comparison of the graph run with and without the architect."""

import io
import json
import random
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

from demo.generate import generate
from tenx.agreement import declared_rows
from tenx.compare import (
    AGREEMENT_FILE,
    ARCHITECT,
    ARCHITECT_ARMS,
    REPORT_JSON,
    RUNS_DIR,
    ArmError,
    CostNotApproved,
    Trial,
    agent_calls,
    architect_plan,
    estimate,
    main,
    read_plan,
    report,
    run_architect,
    summarise,
    write_plan,
)
from tenx.gate import load_declaration
from tenx.policy import CROSS_SOURCE, ENGINEER, Policy

ROOT = Path(__file__).resolve().parent.parent
CHECKS = ROOT / "tests" / "fixtures" / "demo_gate.json"
BRIEF = ROOT / "demo" / "brief.md"
DEMO_CONTRACT = json.loads((ROOT / "tests" / "fixtures" / "demo_contract.json").read_text())

DATASETS = ["s1-200", "s7-1000"]
EIGHT = [f"s{seed}-{size}" for seed in (1, 7, 42, 2026) for size in (200, 1000)]


def leave(out: Path, trial: Trial, gate: str | None, *costs: float) -> None:
    """Write the usage.json a graph run leaves: its verdict and each role's cost."""
    folder = out / RUNS_DIR / trial.folder
    folder.mkdir(parents=True, exist_ok=True)
    roles = [{"role": f"r{i}", "cost_usd": c, "cost_known": True} for i, c in enumerate(costs)]
    (folder / "usage.json").write_text(json.dumps({"gate": gate, "roles": roles}))


class PlanTest(unittest.TestCase):
    def test_every_block_holds_both_arms_trial_by_trial(self):
        order = architect_plan(DATASETS, 3, seed=5)
        self.assertEqual(len(order), 12)
        blocks = [order[i : i + 2] for i in range(0, 12, 2)]
        expected = [(t, d) for t in (1, 2, 3) for d in DATASETS]
        self.assertEqual([(b[0].trial, b[0].dataset) for b in blocks], expected)
        for block in blocks:
            self.assertEqual({t.arm for t in block}, set(ARCHITECT_ARMS))
            self.assertEqual(len({(t.dataset, t.trial) for t in block}), 1)

    def test_the_order_within_blocks_comes_from_the_seed(self):
        # Worked out apart from the code: one shuffle of the two arms per block.
        rng = random.Random("5:architect")
        firsts = []
        for _ in range(6):
            arms = list(ARCHITECT_ARMS)
            rng.shuffle(arms)
            firsts.append(arms[0])
        order = architect_plan(DATASETS, 3, seed=5)
        self.assertEqual([order[i].arm for i in range(0, 12, 2)], firsts)
        self.assertEqual(architect_plan(DATASETS, 3, seed=5), order)
        self.assertNotEqual(architect_plan(DATASETS, 3, seed=6), order)

    def test_bad_plans_are_refused(self):
        for datasets, trials in (([], 1), (DATASETS, 0), (["a", "a"], 1), (["a b"], 1)):
            with self.subTest(datasets=datasets, trials=trials), self.assertRaises(ValueError):
                architect_plan(datasets, trials)


class PriceTest(unittest.TestCase):
    def test_the_architect_is_priced_as_its_own_call(self):
        self.assertEqual(agent_calls("graph", 4), [CROSS_SOURCE, ENGINEER])
        self.assertEqual(agent_calls(ARCHITECT, 4), [CROSS_SOURCE, CROSS_SOURCE, ENGINEER])

    def test_the_default_size_is_priced_by_hand(self):
        order = architect_plan(EIGHT, 4)
        found = estimate(order, {name: 4 for name in EIGHT}, Policy())
        # 32 x (0.08 + 0.20) + 32 x (0.08 + 0.08 + 0.20) = 8.96 + 11.52;
        # caps 32 x (0.40 + 0.75) + 32 x (0.40 + 0.40 + 0.75) = 36.80 + 49.60.
        self.assertEqual(found.runs, 64)
        self.assertEqual(found.typical_usd, Decimal("20.48"))
        self.assertEqual(found.ceiling_usd, Decimal("86.40"))


class SummaryTest(unittest.TestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.out)

    def test_the_graph_run_is_the_baseline(self):
        order = write_plan(self.out, ["d"], ARCHITECT_ARMS, 1, 0, ARCHITECT)
        for trial in order:
            gate = "PASS" if trial.arm == ARCHITECT else "FAIL"
            leave(self.out, trial, gate, 0.07, 0.09, 0.11)
        summary = summarise(self.out)
        self.assertEqual(summary.baseline, "graph")
        arms = {arm.arm: arm for arm in summary.arms}
        self.assertEqual(arms["graph"].verdict, "baseline")
        # Beta(1, 2) for the graph against Beta(2, 1) with the architect:
        # P = 2 * (1 - 1/2 - 1/3 + 1/4) = 5/6, worked out by hand.
        self.assertAlmostEqual(arms[ARCHITECT].better, float(Fraction(5, 6)), places=12)
        self.assertEqual(arms[ARCHITECT].verdict, "not shown")
        self.assertAlmostEqual(arms[ARCHITECT].cost_per_pass, 0.27, places=12)
        self.assertEqual(summary.to_dict()["baseline"], "graph")

    def test_helps_at_the_threshold(self):
        # Graph 0 of 6, architect 6 of 6: P(architect lower) = 1 / C(14, 7) = 1/3432.
        order = write_plan(self.out, ["d"], ARCHITECT_ARMS, 6, 0, ARCHITECT)
        for trial in order:
            leave(self.out, trial, "PASS" if trial.arm == ARCHITECT else "FAIL", 0.1)
        (architect,) = [a for a in summarise(self.out).arms if a.arm == ARCHITECT]
        self.assertAlmostEqual(architect.better, 1 - 1 / 3432, places=12)
        self.assertEqual(architect.verdict, "helps")


AGREES = {"spec": True, "agreeing": 5, "declared": 5, "models": {"m": []}, "extra": []}
DIFFERS = {
    "spec": True,
    "agreeing": 4,
    "declared": 5,
    "models": {"m": [], "stg_x": ["rows to keep 157 not 147"]},
    "extra": ["int_y"],
}
# Graph 1 of 2 at US$0.18 a run; architect 1 of 2 at US$0.27. Beta(2, 2) on
# both sides: P = 0.5 by symmetry, and 3x^2 - 2x^3 = 0.025 at x = 0.094,
# so the interval is 9% to 91%.
REPORT = """comparison: 2 datasets, 1 trial each, 4 of 4 runs have a gate verdict
an arm helps (or hurts) when the chance that its pass rate is higher (or lower) than graph's is at least 0.98

arm        verdict    runs  passes  rate  95% interval  P(> graph)  US$ per run  US$ per pass
graph      baseline      2       1   50%     9% to 91%           -       0.1800        0.3600
architect  not shown     2       1   50%     9% to 91%       0.500       0.2700        0.5400

passes by dataset

dataset  graph  architect
d1         0/1        1/1
d2         1/1        0/1

each architect run's spec against the declaration (keys, sources, rows to keep
and money; how sources are matched is judged by the gate alone)
1 of 2 specs agree on every declared model; 0 runs had no spec

run             gate  spec
d1/architect/1  PASS  5 of 5 models agree
d2/architect/1  FAIL  4 of 5 models agree; stg_x: rows to keep 157 not 147; extra: int_y

cost is Claude Code's own estimate, not a bill
"""


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.out)
        self.order = write_plan(self.out, ["d1", "d2"], ARCHITECT_ARMS, 1, 0, ARCHITECT)
        for trial in self.order:
            passes = (trial.dataset == "d1") == (trial.arm == ARCHITECT)
            costs = (0.07, 0.09, 0.11) if trial.arm == ARCHITECT else (0.07, 0.11)
            leave(self.out, trial, "PASS" if passes else "FAIL", *costs)

    def agreement(self, dataset: str, found) -> None:
        path = self.out / RUNS_DIR / dataset / ARCHITECT / "1" / AGREEMENT_FILE
        path.write_text(found if isinstance(found, str) else json.dumps(found))

    def test_each_architect_run_is_shown_with_its_spec_agreement(self):
        self.agreement("d1", AGREES)
        self.agreement("d2", DIFFERS)
        self.assertEqual(report(self.out), REPORT)
        shown = json.loads((self.out / REPORT_JSON).read_text())
        self.assertEqual(
            shown["spec_agreement"],
            [
                {"run": "d1/architect/1", "gate": "PASS", **AGREES},
                {"run": "d2/architect/1", "gate": "FAIL", **DIFFERS},
            ],
        )
        self.assertEqual(shown["baseline"], "graph")

    def test_no_spec_and_no_score_are_said_so(self):
        self.agreement("d1", {"spec": False})
        self.agreement("d2", "{not json")
        text = report(self.out)
        self.assertIn("0 of 2 specs agree on every declared model; 1 run had no spec\n", text)
        self.assertIn("d1/architect/1  PASS  no spec\n", text)
        self.assertIn("d2/architect/1  FAIL  not scored\n", text)
        shown = json.loads((self.out / REPORT_JSON).read_text())["spec_agreement"]
        self.assertEqual([run["spec"] for run in shown], [False, None])

    def test_runs_without_a_verdict_are_not_scored_and_other_modes_show_nothing(self):
        (self.out / RUNS_DIR / "d2" / ARCHITECT / "1" / "usage.json").unlink()
        self.agreement("d1", AGREES)
        self.assertEqual(
            [t.folder for t, _, _ in summarise(self.out).agreements], ["d1/architect/1"]
        )
        other = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, other)
        for trial in write_plan(other, ["d1"], ["alone", "cross-source"], 1, 0):
            leave(other, trial, "PASS", 0.1)
        self.assertNotIn("spec", report(other))
        self.assertNotIn("spec_agreement", json.loads((other / REPORT_JSON).read_text()))


class FakeGraph:
    """Stands in for the graph run: records each call, leaves a usage.json and,
    when the architect runs, an architect attempt with the demo contract and
    the rows it keeps (`kept`, by dataset). A run named in `errors` raises
    instead, as a run that cannot start does.
    """

    def __init__(self, kept, errors=()):
        self.kept = kept
        self.errors = set(errors)
        self.calls = []

    def __call__(self, raw, goal, checks, out, claude, **options):
        self.calls.append((Path(out), Path(raw), Path(goal), claude, options))
        folder = "/".join(Path(out).parts[-3:])
        if folder in self.errors:
            raise ArmError("the sandbox cannot start")
        Path(out).mkdir(parents=True)
        if options["architect"]:
            attempt = Path(out) / "architect" / "attempt-1"
            attempt.mkdir(parents=True)
            (attempt / "contract.json").write_text(json.dumps(DEMO_CONTRACT))
            dataset = Path(out).parts[-3]
            counts = {
                n: {"source_rows": 0, "excluded": {}, "kept": k}
                for n, k in self.kept[dataset].items()
            }
            (attempt / "counts.json").write_text(json.dumps(counts))
        roles = [{"role": "engineer", "cost_usd": 0.1, "cost_known": True}]
        (Path(out) / "usage.json").write_text(json.dumps({"gate": "PASS", "roles": roles}))


class RunnerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.datasets = {"a": root / "a", "b": root / "b"}
        generate(11, 20, cls.datasets["a"], root / "answers-a")
        generate(12, 20, cls.datasets["b"], root / "answers-b")
        # What the gate wants of each model, worked out by the declaration.
        declared = load_declaration(CHECKS)
        cls.wanted = {n: declared_rows(declared, f) for n, f in cls.datasets.items()}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.out = Path(tempfile.mkdtemp()) / "compare"
        self.addCleanup(shutil.rmtree, self.out.parent)
        # Dataset b's spec keeps one row too many in the crosswalk.
        off = {**self.wanted["b"]}
        off["int_customers_matched_to_accounts"] += 1
        self.graph = FakeGraph({"a": self.wanted["a"], "b": off})

    def start(self, trials=2, **options):
        return run_architect(
            self.datasets,
            BRIEF,
            CHECKS,
            self.out,
            trials=trials,
            claude="stand-in",
            runner=self.graph,
            progress=lambda line: None,
            **options,
        )

    def test_each_arm_runs_the_graph_and_only_one_runs_the_architect(self):
        self.start()
        _, order = read_plan(self.out)
        self.assertEqual(
            sorted((c[0], c[1], c[2], c[3], tuple(sorted(c[4].items()))) for c in self.graph.calls),
            sorted(
                (
                    self.out / RUNS_DIR / t.folder,
                    self.datasets[t.dataset],
                    BRIEF,
                    "stand-in",
                    (("architect", t.arm == ARCHITECT), ("cross_source", True)),
                )
                for t in order
            ),
        )

    def test_each_architect_run_is_scored_against_the_declaration(self):
        text = self.start()
        crosswalk = "int_customers_matched_to_accounts"
        rows = self.wanted["b"][crosswalk]
        for trial in (1, 2):
            self.assertIn(f"a/architect/{trial}  PASS  5 of 5 models agree\n", text)
            self.assertIn(
                f"b/architect/{trial}  PASS  4 of 5 models agree; {crosswalk}: "
                f"rows to keep {rows + 1} not {rows}\n",
                text,
            )
        self.assertIn("2 of 4 specs agree on every declared model; 0 runs had no spec\n", text)
        self.assertFalse((self.out / RUNS_DIR / "a" / "graph" / "1" / AGREEMENT_FILE).exists())

    def test_a_run_that_fails_is_left_out_and_run_again_on_the_next_start(self):
        self.graph.errors = {"a/architect/2"}
        text = self.start()
        self.assertIn("7 of 8 runs have a gate verdict", text)
        self.assertIn("runs with no gate verdict, not counted:\n  a/architect/2\n", text)
        self.graph.errors = set()
        self.graph.calls = []
        text = self.start()
        self.assertEqual([c[0] for c in self.graph.calls], [self.out / RUNS_DIR / "a/architect/2"])
        self.assertIn("8 of 8 runs have a gate verdict", text)

    def test_above_five_dollars_nothing_runs_without_approval(self):
        # 8 runs each arm: 8 x 0.28 + 8 x 0.36 = 5.12, above 5; 2 trials: 2.56.
        with self.assertRaises(CostNotApproved) as refused:
            self.start(trials=4)
        self.assertIn("16 runs to do, estimated at US$5.12", str(refused.exception))
        self.assertFalse(self.out.exists())
        self.assertEqual(self.graph.calls, [])
        self.start(trials=4, approve_cost=True)
        self.assertEqual(len(self.graph.calls), 16)

    def test_another_kind_of_comparison_in_the_folder_is_refused(self):
        self.out.mkdir()
        write_plan(self.out, ["a", "b"], ["alone"], 2, 0)
        with self.assertRaises(ValueError):
            self.start()

    def test_the_command_line_refuses_without_approval(self):
        args = ["architect", "--goal", str(BRIEF), "--checks", str(CHECKS), "--out", str(self.out)]
        args += [f"--raw={n}={f}" for n, f in self.datasets.items()]
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            self.assertEqual(main(args), 2)
        self.assertIn("US$5.12", err.getvalue())
        self.assertFalse(self.out.exists())


if __name__ == "__main__":
    unittest.main()
