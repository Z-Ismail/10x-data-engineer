"""Tests for the comparison of repairing runs with their own first rounds."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from tenx.compare import RUNS_DIR, Rounds, Trial, rounds


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


if __name__ == "__main__":
    unittest.main()
