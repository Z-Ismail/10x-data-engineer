"""Tests for comparing arms of runs.

Expected probabilities and intervals are worked out by hand from the Beta
densities, or by numerical integration written here, not by the code under
test.
"""

import json
import math
import tempfile
import unittest
from pathlib import Path

from tenx.compare import (
    ARMS,
    PLAN_FILE,
    Tally,
    Trial,
    interval,
    plan,
    prob_better,
    read_plan,
    summarise,
    write_plan,
)


def beta_pdf(x: float, a: int, b: int) -> float:
    # (a + b - 1)! / ((a - 1)! (b - 1)!) x^(a-1) (1-x)^(b-1)
    scale = math.factorial(a + b - 1) / (math.factorial(a - 1) * math.factorial(b - 1))
    return scale * x ** (a - 1) * (1 - x) ** (b - 1)


def integrated_prob_better(base: Tally, other: Tally, steps: int = 4000) -> float:
    """P(other > base) by the midpoint rule over the two densities."""
    width = 1 / steps
    below = 0.0  # P(base < x), built up step by step
    total = 0.0
    for i in range(steps):
        x = (i + 0.5) * width
        base_here = beta_pdf(x, base.passes + 1, base.failures + 1) * width
        total += beta_pdf(x, other.passes + 1, other.failures + 1) * width * (below + base_here / 2)
        below += base_here
    return total


class ProbBetterTest(unittest.TestCase):
    def test_two_arms_with_no_runs_are_even(self):
        self.assertAlmostEqual(prob_better(Tally(0, 0), Tally(0, 0)), 0.5, places=12)

    def test_one_pass_against_no_runs(self):
        # Beta(2, 1) has density 2x; P(X > Y) with Y uniform is the
        # integral of 2x * x over [0, 1], which is 2/3.
        self.assertAlmostEqual(prob_better(Tally(0, 0), Tally(1, 0)), 2 / 3, places=12)

    def test_one_pass_against_one_failure(self):
        # Densities 2x and 2(1 - y): the integral of 2x (2x - x^2) is 5/6.
        self.assertAlmostEqual(prob_better(Tally(0, 1), Tally(1, 0)), 5 / 6, places=12)
        self.assertAlmostEqual(prob_better(Tally(1, 0), Tally(0, 1)), 1 / 6, places=12)

    def test_the_two_directions_add_up_to_one(self):
        for a, b in [((3, 9), (10, 4)), ((0, 32), (32, 0)), ((12, 20), (12, 20)), ((5, 1), (2, 7))]:
            with self.subTest(a=a, b=b):
                forward = prob_better(Tally(*a), Tally(*b))
                backward = prob_better(Tally(*b), Tally(*a))
                self.assertAlmostEqual(forward + backward, 1.0, places=10)

    def test_equal_counts_are_even(self):
        self.assertAlmostEqual(prob_better(Tally(12, 20), Tally(12, 20)), 0.5, places=10)

    def test_matches_numerical_integration(self):
        for a, b in [((3, 9), (10, 4)), ((13, 19), (22, 10)), ((8, 24), (9, 23))]:
            with self.subTest(a=a, b=b):
                self.assertAlmostEqual(
                    prob_better(Tally(*a), Tally(*b)),
                    integrated_prob_better(Tally(*a), Tally(*b)),
                    places=5,
                )

    def test_a_clear_difference_is_near_certain(self):
        self.assertGreater(prob_better(Tally(0, 32), Tally(32, 0)), 0.999999)


class IntervalTest(unittest.TestCase):
    def test_no_runs_gives_the_middle_95_percent_of_a_uniform(self):
        low, high = interval(Tally(0, 0))
        self.assertAlmostEqual(low, 0.025, places=9)
        self.assertAlmostEqual(high, 0.975, places=9)

    def test_no_passes_in_three_runs(self):
        # Beta(1, 4): P(X <= x) = 1 - (1 - x)^4.
        low, high = interval(Tally(0, 3))
        self.assertAlmostEqual(low, 1 - 0.975**0.25, places=9)
        self.assertAlmostEqual(high, 1 - 0.025**0.25, places=9)

    def test_one_pass(self):
        # Beta(2, 1): P(X <= x) = x^2.
        low, high = interval(Tally(1, 0))
        self.assertAlmostEqual(low, math.sqrt(0.025), places=9)
        self.assertAlmostEqual(high, math.sqrt(0.975), places=9)

    def test_another_level(self):
        low, high = interval(Tally(0, 0), level=0.5)
        self.assertAlmostEqual(low, 0.25, places=9)
        self.assertAlmostEqual(high, 0.75, places=9)

    def test_refuses_a_level_outside_zero_to_one(self):
        for level in (0, 1, 1.5):
            with self.subTest(level=level), self.assertRaises(ValueError):
                interval(Tally(1, 1), level=level)


class TallyTest(unittest.TestCase):
    def test_refuses_counts_that_are_not_whole_numbers_of_at_least_zero(self):
        for value in (-1, 1.0, True, "3", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Tally(value, 0)
            with self.subTest(value=value), self.assertRaises(ValueError):
                Tally(0, value)

    def test_runs(self):
        self.assertEqual(Tally(3, 5).runs, 8)


class PlanTest(unittest.TestCase):
    DATASETS = ("s1-200", "s7-200", "s1-1000")
    ARMS = ("alone", "scouts", "cross-source", "both")

    def test_runs_every_arm_on_every_dataset_once_per_trial(self):
        order = plan(self.DATASETS, self.ARMS, trials=2)
        self.assertEqual(len(order), 3 * 4 * 2)
        self.assertEqual(
            sorted((t.dataset, t.arm, t.trial) for t in order),
            sorted((d, a, n) for d in self.DATASETS for a in self.ARMS for n in (1, 2)),
        )

    def test_each_block_is_one_dataset_and_trial_holding_every_arm(self):
        order = plan(self.DATASETS, self.ARMS, trials=2)
        blocks = [order[i : i + 4] for i in range(0, len(order), 4)]
        self.assertEqual(
            [(b[0].dataset, b[0].trial) for b in blocks],
            [(d, n) for n in (1, 2) for d in self.DATASETS],
        )
        for block in blocks:
            self.assertEqual({t.dataset for t in block}, {block[0].dataset})
            self.assertEqual({t.trial for t in block}, {block[0].trial})
            self.assertEqual(sorted(t.arm for t in block), sorted(self.ARMS))

    def test_the_order_within_blocks_varies_and_repeats_for_a_seed(self):
        datasets = [f"d{i}" for i in range(25)]
        order = plan(datasets, self.ARMS, trials=4, seed=3)
        self.assertEqual(order, plan(datasets, self.ARMS, trials=4, seed=3))
        self.assertNotEqual(order, plan(datasets, self.ARMS, trials=4, seed=4))
        firsts = [order[i].arm for i in range(0, len(order), 4)]
        self.assertEqual(set(firsts), set(self.ARMS))

    def test_folder_names_the_dataset_arm_and_trial(self):
        self.assertEqual(Trial("s42-200", "cross-source", 3).folder, "s42-200/cross-source/3")

    def test_known_arms(self):
        self.assertEqual(
            ARMS,
            {
                "alone": (False, False),
                "scouts": (True, False),
                "cross-source": (False, True),
                "both": (True, True),
            },
        )

    def test_refuses_an_unusable_plan(self):
        cases = [
            (["d"], ["alone"], 0),
            (["d"], ["alone"], True),
            ([], ["alone"], 1),
            (["d"], [], 1),
            (["d", "d"], ["alone"], 1),
            (["d"], ["alone", "alone"], 1),
            (["d"], ["alone", "reviewer"], 1),
            (["d"], ["scouts"], 1),
            (["a/b"], ["alone"], 1),
            ([".."], ["alone"], 1),
        ]
        for datasets, arms, trials in cases:
            with (
                self.subTest(datasets=datasets, arms=arms, trials=trials),
                self.assertRaises(ValueError),
            ):
                plan(datasets, arms, trials)


def write_usage(out: Path, folder: str, gate: str, costs: list) -> None:
    """A usage.json as the engineer runner writes it, one role per cost."""
    roles = [
        {
            "role": f"role {i}",
            "outcome": "success",
            "turns": 5,
            "cost_usd": cost,
            "cost_known": cost is not None and cost != "unknown",
        }
        for i, cost in enumerate(costs)
    ]
    for role in roles:
        if role["cost_usd"] == "unknown":
            role["cost_usd"] = 0.01
    path = out / "runs" / folder
    path.mkdir(parents=True, exist_ok=True)
    (path / "usage.json").write_text(json.dumps({"gate": gate, "roles": roles}), encoding="utf-8")


class SummaryTest(unittest.TestCase):
    ARMS = ("alone", "scouts", "cross-source", "both")

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.out = Path(tmp.name)

    def comparison(self) -> None:
        """Two datasets, two trials, with results chosen by hand.

        alone         a: PASS FAIL   b: FAIL FAIL     1 of 4, US$0.10 a run
        scouts        a: PASS PASS   b: PASS PASS     4 of 4, US$0.12 a run
        cross-source  a: FAIL FAIL   b: FAIL (none)   0 of 3, US$0.15 a run
        both          a: PASS PASS   b: PASS FAIL     3 of 4, one cost unknown
        """
        write_plan(self.out, ["a", "b"], self.ARMS, trials=2, seed=0)
        results = {
            "alone": ["PASS", "FAIL", "FAIL", "FAIL"],
            "scouts": ["PASS", "PASS", "PASS", "PASS"],
            "cross-source": ["FAIL", "FAIL", "FAIL", None],
            "both": ["PASS", "PASS", "PASS", "FAIL"],
        }
        costs = {
            "alone": [0.1],
            "scouts": [0.02, 0.1],
            "cross-source": [0.05, 0.1],
            "both": [0.02, 0.05, 0.1],
        }
        folders = ["a/{}/1", "a/{}/2", "b/{}/1", "b/{}/2"]
        for arm, gates in results.items():
            for folder, gate in zip(folders, gates):
                if gate is None:
                    continue
                arm_costs = costs[arm]
                if arm == "both" and folder == "b/{}/2":
                    arm_costs = [0.02, "unknown", 0.1]
                write_usage(self.out, folder.format(arm), gate, arm_costs)

    def test_counts_passes_by_arm_and_dataset(self):
        self.comparison()
        summary = summarise(self.out)
        arms = {arm.arm: arm for arm in summary.arms}
        self.assertEqual([arm.arm for arm in summary.arms], list(self.ARMS))
        self.assertEqual(arms["alone"].tally, Tally(1, 3))
        self.assertEqual(arms["scouts"].tally, Tally(4, 0))
        self.assertEqual(arms["cross-source"].tally, Tally(0, 3))
        self.assertEqual(arms["both"].tally, Tally(3, 1))
        self.assertEqual(summary.by_dataset["a"]["alone"], Tally(1, 1))
        self.assertEqual(summary.by_dataset["b"]["cross-source"], Tally(0, 1))
        self.assertEqual([t.folder for t in summary.without_verdict], ["b/cross-source/2"])
        self.assertEqual(arms["cross-source"].no_verdict, 1)
        self.assertEqual(summary.planned, 16)

    def test_compares_each_arm_with_the_engineer_alone(self):
        self.comparison()
        arms = {arm.arm: arm for arm in summarise(self.out).arms}
        # Beta(5, 1) against Beta(2, 4): 1 - 5 B(5, 6) - 25 B(6, 5), and
        # B(5, 6) = B(6, 5) = 4! 5! / 10! = 1/1260, so 1 - 30/1260 = 41/42.
        self.assertAlmostEqual(arms["scouts"].better, 41 / 42, places=10)
        self.assertAlmostEqual(arms["scouts"].worse, 1 / 42, places=10)
        # 41/42 is about 0.976, short of 0.98: four passes in four is not
        # enough to say the arm helps.
        self.assertEqual(arms["scouts"].verdict, "not shown")
        self.assertIsNone(arms["alone"].better)
        self.assertEqual(arms["alone"].verdict, "baseline")
        self.assertAlmostEqual(
            arms["both"].better, integrated_prob_better(Tally(1, 3), Tally(3, 1)), places=5
        )

    def test_cost_per_run_and_per_pass(self):
        self.comparison()
        arms = {arm.arm: arm for arm in summarise(self.out).arms}
        self.assertAlmostEqual(arms["alone"].cost_usd, 0.4)
        self.assertAlmostEqual(arms["alone"].cost_per_pass, 0.4)
        self.assertAlmostEqual(arms["scouts"].cost_usd, 0.48)
        self.assertAlmostEqual(arms["scouts"].cost_per_pass, 0.12)
        self.assertAlmostEqual(arms["cross-source"].cost_usd, 0.45)
        self.assertIsNone(arms["cross-source"].cost_per_pass)
        self.assertIsNone(arms["both"].cost_usd)
        self.assertIsNone(arms["both"].cost_per_pass)

    def runs(self, arms: tuple[str, ...], gates: dict[str, str]) -> dict:
        datasets = [f"d{i}" for i in range(6)]
        write_plan(self.out, datasets, arms, trials=1, seed=0)
        for arm, gate in gates.items():
            for dataset in datasets:
                write_usage(self.out, f"{dataset}/{arm}/1", gate, [0.1])
        return {arm.arm: arm for arm in summarise(self.out).arms}

    def test_six_of_six_against_none_helps(self):
        # Beta(7, 1) against Beta(1, 7): 1 - 7 B(7, 8) = 1 - 7 * 6! 7! / 14!.
        expected = 1 - 7 * math.factorial(6) * math.factorial(7) / math.factorial(14)
        arms = self.runs(("alone", "scouts", "both"), {"alone": "FAIL", "scouts": "PASS"})
        self.assertAlmostEqual(arms["scouts"].better, expected, places=10)
        self.assertEqual(arms["scouts"].verdict, "helps")
        self.assertEqual(arms["both"].verdict, "no runs")

    def test_six_of_six_against_none_the_other_way_hurts(self):
        arms = self.runs(
            ("alone", "scouts", "both"), {"alone": "PASS", "scouts": "FAIL", "both": "PASS"}
        )
        self.assertEqual(arms["scouts"].verdict, "hurts")
        self.assertEqual(arms["both"].verdict, "not shown")
        self.assertAlmostEqual(arms["both"].better, 0.5, places=10)

    def test_a_run_without_a_usable_verdict_is_not_counted(self):
        write_plan(self.out, ["d"], ("alone", "scouts"), trials=1, seed=0)
        folder = self.out / "runs" / "d" / "scouts" / "1"
        folder.mkdir(parents=True)
        (folder / "usage.json").write_text('{"gate": "maybe", "roles": []}', encoding="utf-8")
        (self.out / "runs" / "d" / "alone" / "1").mkdir(parents=True)
        (self.out / "runs" / "d" / "alone" / "1" / "usage.json").write_text("{", encoding="utf-8")
        summary = summarise(self.out)
        self.assertEqual(
            sorted(t.folder for t in summary.without_verdict), ["d/alone/1", "d/scouts/1"]
        )
        self.assertEqual([arm.verdict for arm in summary.arms], ["baseline", "no runs"])

    def test_the_plan_is_kept_and_read_back(self):
        order = write_plan(self.out, ["a", "b"], self.ARMS, trials=3, seed=9)
        document, read = read_plan(self.out)
        self.assertEqual(read, order)
        self.assertEqual(order, plan(["a", "b"], self.ARMS, 3, 9))
        self.assertEqual(
            (document["datasets"], document["arms"], document["trials"], document["seed"]),
            (["a", "b"], list(self.ARMS), 3, 9),
        )

    def test_refuses_a_folder_without_a_plan(self):
        with self.assertRaises(ValueError):
            summarise(self.out)
        (self.out / PLAN_FILE).write_text("[]", encoding="utf-8")
        with self.assertRaises(ValueError):
            summarise(self.out)


if __name__ == "__main__":
    unittest.main()
