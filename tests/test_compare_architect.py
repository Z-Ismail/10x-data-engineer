"""Tests for the comparison of the graph run with and without the architect."""

import json
import random
import shutil
import tempfile
import unittest
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

from tenx.compare import (
    ARCHITECT,
    ARCHITECT_ARMS,
    RUNS_DIR,
    Trial,
    agent_calls,
    architect_plan,
    estimate,
    paired_plan,
    summarise,
    write_plan,
)
from tenx.policy import CROSS_SOURCE, ENGINEER, Policy

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
        # Not the paired comparison's stream, whose arms differ anyway.
        self.assertNotEqual([t.arm for t in paired_plan(DATASETS, 3, 5)], [t.arm for t in order])

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


if __name__ == "__main__":
    unittest.main()
