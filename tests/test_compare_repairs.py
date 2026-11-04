"""Tests for the comparison of repairing runs with their own first rounds."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from tenx.compare import (
    PLAN_FILE,
    REPAIR_ARM,
    REPAIRS,
    RUNS_DIR,
    Rounds,
    Trial,
    _start,
    read_plan,
    repairs_plan,
    rounds,
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


if __name__ == "__main__":
    unittest.main()
