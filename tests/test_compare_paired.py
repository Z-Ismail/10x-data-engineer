"""Tests for the paired comparison of the default runner and the graph."""

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from tenx import parity
from tenx.compare import (
    PAIRED,
    PAIRED_ARMS,
    PLAN_FILE,
    REPORT_JSON,
    REPORT_TEXT,
    RUNS_DIR,
    PairedSummary,
    Trial,
    agent_calls,
    estimate,
    paired_plan,
    read_plan,
    render_paired,
    report,
    summarise_paired,
    write_plan,
)
from tenx.parity import PairCost, Pairs
from tenx.policy import CROSS_SOURCE, ENGINEER, Policy

DATASETS = ["s1-200", "s7-1000", "s42-200"]


class PairedPlanTest(unittest.TestCase):
    def test_every_pair_holds_both_runners_next_to_each_other(self):
        order = paired_plan(DATASETS, trials=3, seed=5)
        self.assertEqual(len(order), 2 * 3 * 3)
        for first, second in zip(order[::2], order[1::2]):
            self.assertEqual((first.dataset, first.trial), (second.dataset, second.trial))
            self.assertEqual({first.arm, second.arm}, set(PAIRED_ARMS))
        pairs = [(t.dataset, t.trial) for t in order[::2]]
        self.assertEqual(len(set(pairs)), 9)

    def test_pairs_run_trial_by_trial(self):
        order = paired_plan(DATASETS, trials=2)
        self.assertEqual([t.trial for t in order], [1] * 6 + [2] * 6)
        self.assertEqual([t.dataset for t in order[:6:2]], DATASETS)

    def test_who_goes_first_varies_and_repeats_for_a_seed(self):
        firsts = [t.arm for t in paired_plan(DATASETS, trials=8, seed=0)[::2]]
        self.assertEqual(set(firsts), set(PAIRED_ARMS))
        self.assertEqual(paired_plan(DATASETS, 8, seed=0), paired_plan(DATASETS, 8, seed=0))
        self.assertNotEqual(paired_plan(DATASETS, 8, seed=0), paired_plan(DATASETS, 8, seed=1))

    def test_refuses_an_unusable_plan(self):
        for datasets, trials in (([], 1), (["a", "a"], 1), (["a b"], 1), (["a"], 0), (["a"], True)):
            with self.subTest(datasets=datasets, trials=trials), self.assertRaises(ValueError):
                paired_plan(datasets, trials)

    def test_the_plan_is_kept_with_its_kind(self):
        with tempfile.TemporaryDirectory() as folder:
            order = write_plan(Path(folder), DATASETS, PAIRED_ARMS, 2, 3, mode=PAIRED)
            document = json.loads((Path(folder) / PLAN_FILE).read_text(encoding="utf-8"))
            self.assertEqual(document["mode"], PAIRED)
            self.assertEqual(document["arms"], list(PAIRED_ARMS))
            kept, read = read_plan(Path(folder))
            self.assertEqual(read, order)
            self.assertEqual(order, paired_plan(DATASETS, 2, 3))
            self.assertEqual(kept["seed"], 3)

    def test_an_arm_comparison_plan_has_no_kind(self):
        with tempfile.TemporaryDirectory() as folder:
            write_plan(Path(folder), DATASETS, ["alone", "both"], 1, 0)
            document = json.loads((Path(folder) / PLAN_FILE).read_text(encoding="utf-8"))
            self.assertNotIn("mode", document)


class PairedEstimateTest(unittest.TestCase):
    def test_each_runner_makes_the_default_runs_calls(self):
        for arm in PAIRED_ARMS:
            self.assertEqual(agent_calls(arm, tables=4), [CROSS_SOURCE, ENGINEER])

    def test_the_planned_parity_run(self):
        # 8 datasets x 8 trials = 64 pairs, 128 runs. Typical: 128 x (0.08 +
        # 0.20) = 35.84. At the caps: 128 x (0.40 + 0.75) = 147.20.
        datasets = [f"d{i}" for i in range(8)]
        order = paired_plan(datasets, trials=8)
        cost = estimate(order, {name: 4 for name in datasets}, Policy())
        self.assertEqual(cost.runs, 128)
        self.assertEqual(cost.typical_usd, Decimal("35.84"))
        self.assertEqual(cost.ceiling_usd, Decimal("147.20"))

    def test_one_pair(self):
        cost = estimate([Trial("d", "default", 1), Trial("d", "graph", 1)], {"d": 4}, Policy())
        self.assertEqual(cost.typical_usd, Decimal("0.56"))


def leave(out: Path, dataset: str, arm: str, trial: int, gate: str, *costs, known=True) -> None:
    """Write the usage.json a finished run leaves: its verdict and each role's cost."""
    folder = out / RUNS_DIR / dataset / arm / str(trial)
    folder.mkdir(parents=True, exist_ok=True)
    roles = [{"role": "r", "cost_usd": cost, "cost_known": known} for cost in costs]
    ledger = {"gate": gate, "roles": roles}
    (folder / "usage.json").write_text(json.dumps(ledger), encoding="utf-8")


class PairedSummaryTest(unittest.TestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.out))
        write_plan(self.out, ["a", "b"], PAIRED_ARMS, 2, 0, mode=PAIRED)

    def fill(self, known=True):
        leave(self.out, "a", "default", 1, "PASS", 0.05, 0.15, known=known)
        leave(self.out, "a", "graph", 1, "PASS", 0.20)
        leave(self.out, "a", "default", 2, "PASS", 0.20)
        leave(self.out, "a", "graph", 2, "FAIL", 0.10)
        leave(self.out, "b", "default", 1, "FAIL", 0.10)
        leave(self.out, "b", "graph", 1, "PASS", 0.30)
        leave(self.out, "b", "default", 2, "PASS", 0.20)  # its graph run left nothing

    def test_counts_only_pairs_with_both_verdicts(self):
        self.fill()
        summary = summarise_paired(self.out)
        self.assertEqual(summary.pairs, Pairs(both=1, first_only=1, second_only=1, neither=0))
        self.assertEqual(summary.left_out, ("b/2",))
        self.assertEqual(summary.planned, 4)
        self.assertEqual(summary.by_dataset["a"], Pairs(1, 1, 0, 0))
        self.assertEqual(summary.by_dataset["b"], Pairs(0, 0, 1, 0))

    def test_the_difference_is_default_minus_graph(self):
        leave(self.out, "a", "default", 1, "PASS", 0.2)
        leave(self.out, "a", "graph", 1, "FAIL", 0.2)
        leave(self.out, "a", "default", 2, "PASS", 0.2)
        leave(self.out, "a", "graph", 2, "FAIL", 0.2)
        summary = summarise_paired(self.out)
        self.assertEqual(summary.pairs, Pairs(0, 2, 0, 0))
        self.assertGreater(summary.interval[1], 0.5)
        self.assertEqual(summary.cost_per_pass["graph"], float("inf"))

    def test_cost_per_pass_worked_by_hand(self):
        # Counted pairs: default 0.20 + 0.20 + 0.10 over 2 passes = 0.25;
        # graph 0.20 + 0.10 + 0.30 over 2 passes = 0.30.
        self.fill()
        summary = summarise_paired(self.out)
        self.assertAlmostEqual(summary.cost_per_pass["default"], 0.25)
        self.assertAlmostEqual(summary.cost_per_pass["graph"], 0.30)
        costs = [PairCost(True, 0.2, True, 0.2), PairCost(True, 0.2, False, 0.1)]
        costs.append(PairCost(False, 0.1, True, 0.3))
        expected = parity.cost_interval(costs)
        self.assertAlmostEqual(summary.cost_interval[0], expected[0])
        self.assertAlmostEqual(summary.cost_interval[1], expected[1])

    def test_an_unknown_cost_leaves_the_cost_verdict_unknown(self):
        self.fill(known=False)
        summary = summarise_paired(self.out)
        self.assertEqual(summary.cost_verdict, parity.UNKNOWN)
        self.assertIsNone(summary.cost_interval)
        self.assertEqual(summary.parity, parity.INCONCLUSIVE)

    def test_report_text(self):
        self.fill()
        low, high = parity.interval(Pairs(1, 1, 1, 0))
        summary = summarise_paired(self.out)
        cost_low, cost_high = summary.cost_interval
        # A resample of three copies of pair a/2 has no graph pass: 1 in 27.
        self.assertEqual(cost_high, float("inf"))
        expected = f"""paired comparison: 2 datasets, 2 trials each, 3 of 4 pairs have both gate verdicts
difference = default's pass rate minus graph's; the graph is non-inferior when the 95% interval lies below +10 points

pairs                count
both pass                1
only default passes      1
only graph passes        1
neither passes           0

pass rate: default 67%, graph 67%
difference: +0.0 points, 95% interval (Tango) {100 * low:+.1f} to {100 * high:+.1f}
pass rate verdict: inconclusive

US$ per pass: default 0.2500, graph 0.3000
graph / default: 95% interval (bootstrap over pairs) {cost_low:.3f} to infinite; limit 1.10
cost verdict: inconclusive

parity: inconclusive

pairs by dataset (both, only default, only graph, neither)

dataset  both  default  graph  neither
a           1        1      0        0
b           0        0      1        0

pairs without both gate verdicts, not counted:
  b/2

cost is Claude Code's own estimate, not a bill
"""
        self.assertEqual(report(self.out), expected)
        self.assertEqual((self.out / REPORT_TEXT).read_text(encoding="utf-8"), expected)
        document = json.loads((self.out / REPORT_JSON).read_text(encoding="utf-8"))
        self.assertEqual(document["mode"], PAIRED)
        self.assertEqual(document["pairs"]["only_graph_passes"], 1)
        self.assertEqual(document["pass_rate"], {"default": 2 / 3, "graph": 2 / 3})
        self.assertEqual(document["pairs_left_out"], ["b/2"])
        self.assertEqual(document["verdict"], "inconclusive")

    def test_no_pairs_yet(self):
        text = report(self.out)
        self.assertIn("0 of 4 pairs have both gate verdicts", text)
        self.assertIn("no pairs to compare yet", text)
        self.assertEqual(summarise_paired(self.out).parity, "no pairs")

    def test_a_lower_rate_within_the_margin_is_said_so(self):
        pairs = Pairs(190, 10, 0, 0)
        low, high = parity.interval(pairs)
        self.assertGreater(low, 0)
        self.assertLess(high, parity.MARGIN)
        summary = PairedSummary(
            datasets=["a"],
            trials=200,
            pairs=pairs,
            interval=(low, high),
            verdict=parity.verdict(low, high),
            cost_per_pass={"default": 0.2, "graph": 0.21},
            cost_interval=(1.0, 1.08),
            cost_verdict=parity.WITHIN,
            parity=parity.NON_INFERIOR,
            by_dataset={"a": pairs},
            left_out=(),
            planned=200,
        )
        text = render_paired(summary)
        self.assertIn("pass rate verdict: non-inferior\n  (the graph's pass rate is lower", text)
        self.assertIn("parity: non-inferior", text)


if __name__ == "__main__":
    unittest.main()
