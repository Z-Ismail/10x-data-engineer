"""Tests for the paired comparison of the default runner and the graph."""

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from tenx.compare import (
    PAIRED,
    PAIRED_ARMS,
    PLAN_FILE,
    Trial,
    agent_calls,
    estimate,
    paired_plan,
    read_plan,
    write_plan,
)
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


if __name__ == "__main__":
    unittest.main()
