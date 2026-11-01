"""Tests for the paired comparison of the default runner and the graph."""

import io
import json
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from pathlib import Path
from unittest import mock

from tenx import parity
from tenx.compare import (
    PAIRED,
    PAIRED_ARMS,
    PLAN_FILE,
    REPORT_JSON,
    REPORT_TEXT,
    RUNS_DIR,
    ArmError,
    CostNotApproved,
    PairedSummary,
    Trial,
    agent_calls,
    estimate,
    main,
    paired_plan,
    read_plan,
    render_paired,
    report,
    run_default,
    run_graph,
    run_paired,
    summarise_paired,
    write_plan,
)
from tenx.engineer import EngineerError
from tenx.parity import PairCost, Pairs
from tenx.policy import CROSS_SOURCE, ENGINEER, Policy

DATASETS = ["s1-200", "s7-1000", "s42-200"]
ROOT = Path(__file__).resolve().parent.parent
DECLARATION = ROOT / "tests" / "fixtures" / "demo_gate.json"
GOAL = ROOT / "demo" / "goal.md"


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


class FakeRunner:
    """Stands in for one runner: records its calls and leaves the verdict `gates` names.

    "error" raises the runner's error, "none" leaves no usage.json.
    """

    def __init__(self, arm: str, calls: list, gates=None):
        self.arm, self.calls, self.gates = arm, calls, gates or {}
        self.lock = threading.Lock()

    def __call__(self, raw, goal, checks, out, claude):
        with self.lock:
            self.calls.append((self.arm, Path(out), Path(raw), claude))
        dataset, _, trial = Path(out).parts[-3:]
        gate = self.gates.get(f"{dataset}/{trial}", "PASS")
        Path(out).mkdir(parents=True)
        if gate == "error":
            raise ArmError("the run stopped") if self.arm == "graph" else EngineerError("no claude")
        if gate != "none":
            (Path(out) / "usage.json").write_text(
                json.dumps({"gate": gate, "roles": [{"cost_usd": 0.2, "cost_known": True}]}),
                encoding="utf-8",
            )


class RunPairedTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.out = root / "paired"
        self.datasets = {}
        for name in ("s1-200", "s7-1000"):
            folder = root / "data" / name
            folder.mkdir(parents=True)
            for number in range(4):
                (folder / f"table_{number}.csv").write_text("id\n1\n", encoding="utf-8")
            self.datasets[name] = folder
        self.lines = []
        self.calls = []

    def runners(self, default=None, graph=None):
        return {
            "default": FakeRunner("default", self.calls, default),
            "graph": FakeRunner("graph", self.calls, graph),
        }

    def paired(self, runners, **options):
        options.setdefault("trials", 2)
        return run_paired(
            self.datasets,
            GOAL,
            DECLARATION,
            self.out,
            runners=runners,
            progress=self.lines.append,
            claude="my-claude",
            **options,
        )

    def test_runs_both_runners_on_every_pair_in_the_planned_order(self):
        text = self.paired(self.runners(graph={"s7-1000/2": "FAIL"}), parallel=1)
        order = paired_plan(list(self.datasets), 2)
        expected = [
            (t.arm, self.out / RUNS_DIR / t.folder, self.datasets[t.dataset], "my-claude")
            for t in order
        ]
        self.assertEqual(self.calls, expected)
        self.assertIn("4 of 4 pairs have both gate verdicts", text)
        self.assertEqual(summarise_paired(self.out).pairs, Pairs(3, 1, 0, 0))
        self.assertEqual(self.lines[0][:41], "8 runs to do, estimated at US$2.24, at mo")
        self.assertEqual(len(self.lines), 9)

    def test_errors_leave_their_pair_out_and_a_second_start_runs_them_again(self):
        self.paired(self.runners(default={"s1-200/1": "error"}, graph={"s7-1000/2": "none"}))
        summary = summarise_paired(self.out)
        self.assertEqual(summary.left_out, ("s1-200/1", "s7-1000/2"))
        self.assertTrue(any("s1-200/default/1: error: no claude" in line for line in self.lines))
        self.assertTrue(any("s7-1000/graph/2: no gate verdict" in line for line in self.lines))
        self.calls.clear()
        self.paired(self.runners())
        rerun = sorted((arm, out.relative_to(self.out / RUNS_DIR)) for arm, out, _, _ in self.calls)
        self.assertEqual(
            rerun, [("default", Path("s1-200/default/1")), ("graph", Path("s7-1000/graph/2"))]
        )
        self.assertEqual(summarise_paired(self.out).pairs, Pairs(4, 0, 0, 0))

    def test_a_graph_error_is_reported(self):
        self.paired(self.runners(graph={"s1-200/1": "error"}))
        self.assertTrue(
            any("s1-200/graph/1: error: the run stopped" in line for line in self.lines)
        )

    def test_refuses_an_arm_comparison_folder_and_missing_runners(self):
        self.out.mkdir()
        write_plan(self.out, list(self.datasets), ["alone"], 2, 0)
        with self.assertRaisesRegex(ValueError, "another kind"):
            self.paired(self.runners())
        with self.assertRaisesRegex(ValueError, "a runner is needed"):
            self.paired({"default": FakeRunner("default", self.calls)})
        self.assertEqual(self.calls, [])

    def test_an_expensive_comparison_is_refused_before_anything_starts(self):
        # 2 datasets x 5 trials x 2 runners = 20 runs at US$0.28 = US$5.60.
        with self.assertRaises(CostNotApproved) as refusal:
            self.paired(self.runners(), trials=5)
        self.assertTrue(str(refusal.exception).startswith("20 runs to do, estimated at US$5.60,"))
        self.assertEqual(self.calls, [])
        self.assertFalse(self.out.exists())
        self.paired(self.runners(), trials=5, approve_cost=True)
        self.assertEqual(len(self.calls), 20)

    def test_command_line(self):
        argv = ["paired", "--goal", str(GOAL), "--checks", str(DECLARATION)]
        for name, folder in self.datasets.items():
            argv += ["--raw", f"{name}={folder}"]
        argv += ["--out", str(self.out)]
        with (
            mock.patch.dict("tenx.compare.PAIRED_RUNNERS", self.runners()),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()) as errors,
        ):
            self.assertEqual(main(argv), 2)  # 8 trials by default: 32 runs, US$8.96
        self.assertIn("32 runs to do, estimated at US$8.96", errors.getvalue())
        self.assertEqual(self.calls, [])
        with (
            mock.patch.dict("tenx.compare.PAIRED_RUNNERS", self.runners()),
            redirect_stdout(io.StringIO()) as printed,
        ):
            self.assertEqual(main([*argv, "--approve-cost"]), 0)
        self.assertEqual(len(self.calls), 32)
        text = (self.out / REPORT_TEXT).read_text(encoding="utf-8")
        self.assertTrue(printed.getvalue().endswith("\n" + text))
        self.assertIn("parity: inconclusive", text)  # 16 pairs cannot show a 10-point margin
        with redirect_stdout(io.StringIO()) as printed:
            self.assertEqual(main(["report", str(self.out)]), 0)
        self.assertEqual(printed.getvalue(), text)


class RunnersTest(unittest.TestCase):
    def test_the_default_runner_runs_the_cross_source_agent(self):
        with mock.patch("tenx.compare.engineer_run") as engineer:
            run_default(Path("raw"), Path("goal"), Path("checks"), Path("out"), "c")
        engineer.assert_called_once_with(
            Path("raw"), Path("goal"), Path("checks"), Path("out"), claude="c", cross_source=True
        )

    def test_the_graph_runner_runs_the_cross_source_agent(self):
        with mock.patch("tenx.run.start") as start:
            run_graph(Path("raw"), Path("goal"), Path("checks"), Path("out"), "c")
        start.assert_called_once_with(
            Path("raw"), Path("goal"), Path("checks"), Path("out"), cross_source=True, claude="c"
        )

    def test_the_graph_runners_refusal_becomes_an_arm_error(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "something").write_text("x", encoding="utf-8")
            with self.assertRaisesRegex(ArmError, "must be empty"):
                run_graph(ROOT / "demo", GOAL, DECLARATION, Path(folder), "claude")


if __name__ == "__main__":
    unittest.main()
