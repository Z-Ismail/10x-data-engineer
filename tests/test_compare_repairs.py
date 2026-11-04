"""Tests for the comparison of repairing runs with their own first rounds."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from tenx import parity
from tenx.compare import (
    PLAN_FILE,
    REPAIR_ARM,
    REPAIRS,
    REPORT_JSON,
    REPORT_TEXT,
    RUNS_DIR,
    Rounds,
    Trial,
    _start,
    read_plan,
    repairs_plan,
    report,
    rounds,
    summarise_repairs,
    write_plan,
)

DATASETS = ["s1-200", "s7-1000", "s42-200"]
SETTINGS = {"repairs": 2, "cross_source": False}


def leave(out: Path, trial: Trial, gate: str | None, *rows, known=True) -> None:
    """Write the usage.json a run leaves: its verdict and (role, cost) rows."""
    folder = out / RUNS_DIR / trial.folder
    folder.mkdir(parents=True, exist_ok=True)
    roles = [{"role": name, "cost_usd": cost, "cost_known": known} for name, cost in rows]
    ledger = {"gate": gate, "roles": roles}
    (folder / "usage.json").write_text(json.dumps(ledger), encoding="utf-8")


class RoundsTest(unittest.TestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.out)
        self.trial = Trial("d", "repair", 1)

    def read(self, gate, *rows, known=True) -> Rounds:
        leave(self.out, self.trial, gate, *rows, known=known)
        return rounds(self.out, self.trial)

    def test_passed_first_time(self):
        found = self.read("PASS", ("cross-source", 0.08), ("engineer", 0.11))
        self.assertEqual((found.first, found.final), ("PASS", "PASS"))
        self.assertAlmostEqual(found.first_usd, 0.19)
        self.assertAlmostEqual(found.total_usd, 0.19)

    def test_repaired(self):
        found = self.read("PASS", ("engineer", 0.11), ("engineer repair 1", 0.08))
        self.assertEqual((found.first, found.final), ("FAIL", "PASS"))
        self.assertAlmostEqual(found.first_usd, 0.11)
        self.assertAlmostEqual(found.total_usd, 0.19)

    def test_repaired_twice(self):
        found = self.read(
            "PASS",
            ("cross-source", 0.07),
            ("engineer", 0.10),
            ("engineer repair 1", 0.09),
            ("engineer repair 2", 0.05),
        )
        self.assertEqual((found.first, found.final), ("FAIL", "PASS"))
        self.assertAlmostEqual(found.first_usd, 0.17)
        self.assertAlmostEqual(found.total_usd, 0.31)

    def test_failed_after_repairs(self):
        found = self.read("FAIL", ("engineer", 0.10), ("engineer repair 1", 0.09))
        self.assertEqual((found.first, found.final), ("FAIL", "FAIL"))
        self.assertAlmostEqual(found.first_usd, 0.10)

    def test_failed_with_no_repair_made(self):
        # No repairs asked for, or none the budget could hold: no repair row.
        found = self.read("FAIL", ("engineer", 0.10))
        self.assertEqual((found.first, found.final), ("FAIL", "FAIL"))
        self.assertAlmostEqual(found.total_usd, 0.10)

    def test_only_a_repair_row_counts_as_a_repair(self):
        for name in ("engineer repair", "engineer repair 0", "repair 1 engineer"):
            with self.subTest(name=name):
                found = self.read("PASS", (name, 0.10))
                self.assertEqual(found.first, "PASS")

    def test_an_unknown_cost(self):
        found = self.read("PASS", ("engineer", 0.10), ("engineer repair 1", 0.08), known=False)
        self.assertEqual((found.first, found.final), ("FAIL", "PASS"))
        self.assertIsNone(found.first_usd)
        self.assertIsNone(found.total_usd)

    def test_no_verdict(self):
        self.assertEqual(rounds(self.out, self.trial), Rounds(self.trial, None, None))
        self.assertEqual(self.read("not run", ("engineer", 0.1)), Rounds(self.trial, None, None))


class RepairsPlanTest(unittest.TestCase):
    def test_one_run_per_dataset_and_trial_trial_by_trial(self):
        order = repairs_plan(DATASETS, trials=3, seed=5)
        self.assertEqual(len(order), 9)
        self.assertEqual({t.arm for t in order}, {REPAIR_ARM})
        self.assertEqual([t.trial for t in order], [1] * 3 + [2] * 3 + [3] * 3)
        for trial in (1, 2, 3):
            names = [t.dataset for t in order if t.trial == trial]
            self.assertEqual(sorted(names), sorted(DATASETS))

    def test_the_order_varies_and_repeats_for_a_seed(self):
        firsts = {repairs_plan(DATASETS, 1, seed=s)[0].dataset for s in range(20)}
        self.assertEqual(firsts, set(DATASETS))
        self.assertEqual(repairs_plan(DATASETS, 4, seed=0), repairs_plan(DATASETS, 4, seed=0))
        self.assertNotEqual(repairs_plan(DATASETS, 4, seed=0), repairs_plan(DATASETS, 4, seed=1))

    def test_refuses_an_unusable_plan(self):
        for datasets, trials in (([], 1), (["a", "a"], 1), (["a b"], 1), (["a"], 0)):
            with self.subTest(datasets=datasets, trials=trials), self.assertRaises(ValueError):
                repairs_plan(datasets, trials)

    def test_the_plan_is_kept_with_its_kind_and_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            order = write_plan(Path(folder), DATASETS, [REPAIR_ARM], 2, 3, REPAIRS, SETTINGS)
            document = json.loads((Path(folder) / PLAN_FILE).read_text(encoding="utf-8"))
            self.assertEqual(document["mode"], REPAIRS)
            self.assertEqual((document["repairs"], document["cross_source"]), (2, False))
            self.assertEqual(read_plan(Path(folder))[1], order)
            self.assertEqual(order, repairs_plan(DATASETS, 2, 3))

    def test_a_resume_must_ask_for_the_same_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder)
            order = _start(out, DATASETS, [REPAIR_ARM], 2, 0, REPAIRS, SETTINGS)
            self.assertEqual(_start(out, DATASETS, [REPAIR_ARM], 2, 0, REPAIRS, SETTINGS), order)
            for other in (
                {"repairs": 3, "cross_source": False},
                {"repairs": 2, "cross_source": True},
            ):
                with self.subTest(other=other), self.assertRaises(ValueError) as caught:
                    _start(out, DATASETS, [REPAIR_ARM], 2, 0, REPAIRS, other)
                self.assertIn("settings", str(caught.exception))
            with self.assertRaises(ValueError):
                _start(out, DATASETS, [REPAIR_ARM], 2, 0, "paired", SETTINGS)


class RepairSummaryTest(unittest.TestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.out)

    def plan(self, datasets, trials):
        write_plan(self.out, datasets, [REPAIR_ARM], trials, 0, REPAIRS, SETTINGS)

    def ran(self, dataset, trial, gate, *rows, known=True):
        leave(self.out, Trial(dataset, REPAIR_ARM, trial), gate, *rows, known=known)

    def fill(self, known=True):
        self.plan(["a", "b"], 2)
        self.ran("a", 1, "PASS", ("engineer", 0.10), known=known)
        self.ran("a", 2, "PASS", ("engineer", 0.12), ("engineer repair 1", 0.08))
        self.ran("b", 1, "FAIL", ("engineer", 0.10), *self.repairs(0.06, 0.04))
        # b/2 left no verdict.

    @staticmethod
    def repairs(*costs):
        return [(f"engineer repair {n}", cost) for n, cost in enumerate(costs, start=1)]

    def test_each_run_is_paired_with_its_first_round(self):
        self.fill()
        summary = summarise_repairs(self.out)
        # First: with repairs; second: the first round alone.
        self.assertEqual(
            summary.pairs, parity.Pairs(both=1, first_only=1, second_only=0, neither=1)
        )
        self.assertEqual(summary.left_out, ("b/repair/2",))
        self.assertEqual(summary.interval, parity.interval(parity.Pairs(1, 1, 0, 1)))
        self.assertEqual(summary.verdict, parity.INCONCLUSIVE)

    def test_cost_per_pass_worked_by_hand(self):
        self.fill()
        summary = summarise_repairs(self.out)
        # First round: 0.10 + 0.12 + 0.10 = 0.32 over 1 pass. With repairs:
        # 0.10 + 0.20 + 0.20 = 0.50 over 2 passes.
        self.assertAlmostEqual(summary.cost_per_pass["first round"], 0.32)
        self.assertAlmostEqual(summary.cost_per_pass["with repairs"], 0.25)

    def test_an_unknown_cost_leaves_cost_unknown(self):
        self.fill(known=False)
        summary = summarise_repairs(self.out)
        self.assertIsNone(summary.cost_interval)
        self.assertIn("cost per pass: unknown", report(self.out))

    def test_many_rescued_runs_are_better(self):
        self.plan(["a"], 12)
        for trial in range(1, 13):
            if trial <= 2:
                self.ran("a", trial, "PASS", ("engineer", 0.1))
            elif trial <= 10:
                self.ran("a", trial, "PASS", ("engineer", 0.1), *self.repairs(0.1))
            else:
                self.ran("a", trial, "FAIL", ("engineer", 0.1), *self.repairs(0.1, 0.1))
        summary = summarise_repairs(self.out)
        self.assertEqual(summary.pairs, parity.Pairs(2, 8, 0, 2))
        self.assertGreater(summary.interval[0], 0)
        self.assertEqual(summary.verdict, parity.BETTER)
        self.assertAlmostEqual(summary.pairs.difference, 8 / 12)

    def test_report_text(self):
        self.fill()
        low, high = parity.interval(parity.Pairs(1, 1, 0, 1))
        # A resample with no first-round pass but a repaired run has ratio 0
        # (8 in 27 resamples have no first-round pass); three copies of b/1
        # have no pass at all, an infinite ratio, 1 in 27, above the 2.5% tail.
        expected = f"""repair comparison: 2 datasets, 2 trials each, up to 2 repairs, engineer alone; 3 of 4 runs have a gate verdict
each run is paired with its own first round; difference = pass rate with repairs minus first round's; repairs are better when the 95% interval lies above 0

runs               count
passed first time      1
repaired               1
failed                 1

pass rate: first round 33%, with repairs 67%
difference: +33.3 points, 95% interval (Tango) {100 * low:+.1f} to {100 * high:+.1f}
verdict: inconclusive

US$ per pass: first round 0.3200, with repairs 0.2500
with repairs / first round: 95% interval (bootstrap over runs) 0.000 to infinite

runs by dataset (first time, repaired, failed)

dataset  first time  repaired  failed
a                 1         1       0
b                 0         0       1

runs without a gate verdict, not counted:
  b/repair/2

cost is Claude Code's own estimate, not a bill
"""
        self.assertEqual(report(self.out), expected)
        self.assertEqual((self.out / REPORT_TEXT).read_text(encoding="utf-8"), expected)
        document = json.loads((self.out / REPORT_JSON).read_text(encoding="utf-8"))
        self.assertEqual(document["mode"], REPAIRS)
        self.assertEqual((document["repairs"], document["cross_source"]), (2, False))
        self.assertEqual(document["runs"]["repaired"], 1)
        self.assertEqual(document["pass_rate"], {"first_round": 1 / 3, "with_repairs": 2 / 3})
        self.assertEqual(document["cost_ratio_interval_95"], [0.0, "infinity"])
        self.assertEqual(document["runs_left_out"], ["b/repair/2"])

    def test_no_runs_yet(self):
        self.plan(["a"], 1)
        text = report(self.out)
        self.assertIn("0 of 1 runs have a gate verdict", text)
        self.assertIn("no runs to compare yet", text)


if __name__ == "__main__":
    unittest.main()
