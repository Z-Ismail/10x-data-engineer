"""The repair comparison through the real graph, with the stand-in claude.

The stand-in of tests/equivalence.py answers the same way every time; here
its engineer copies one set of models on a first attempt and another when
``GATE_REPORT.md`` is in its folder, so a first round can fail and its
repair pass. Runs in the sandbox, skipped where bubblewrap cannot run unless
TENX_REQUIRE_SANDBOX is set.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from equivalence import DECLARATION, GOAL, SCENARIOS, STAND_IN, environment, stand_in

from demo.generate import generate
from tenx import parity
from tenx.compare import (
    RUNS_DIR,
    read_plan,
    rounds,
    run_graph,
    run_repairs,
    summarise_repairs,
)
from tenx.sandbox import SandboxError, default_sandbox

# A repair attempt finds GATE_REPORT.md in its folder and copies the models
# STAND_IN_REPAIR_MODELS names instead.
REPAIRING = """
import os
from pathlib import Path
if Path("GATE_REPORT.md").exists():
    os.environ["STAND_IN_MODELS"] = os.environ["STAND_IN_REPAIR_MODELS"]
"""


def _sandbox_problem() -> str:
    try:
        default_sandbox()
    except SandboxError as error:
        return str(error)
    return ""


SANDBOX_PROBLEM = _sandbox_problem()


@unittest.skipIf(
    SANDBOX_PROBLEM and not os.environ.get("TENX_REQUIRE_SANDBOX"), "no working sandbox"
)
class RepairRunsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.raw = root / "data"
        generate(11, 20, cls.raw, root / "answers")
        cls.claude = stand_in(root / "stand-in")
        text = cls.claude.read_text(encoding="utf-8")
        cls.claude.write_text(text.replace(STAND_IN, REPAIRING + STAND_IN), encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def behaving(self, first: dict):
        """The graph run, its engineer's first attempt copying the models `first` names."""

        def run(raw, goal, checks, folder, claude_path, **options):
            scenario = SCENARIOS["pass"]
            saved = dict(os.environ)
            os.environ.update(environment(scenario, self.claude, raw, Path(folder)))
            models = first.get(Path(folder).name, "reference")
            os.environ["STAND_IN_REPAIR_MODELS"] = os.environ["STAND_IN_MODELS"]
            os.environ["STAND_IN_MODELS"] = str(self.claude.parent / models)
            try:
                run_graph(raw, goal, checks, folder, claude_path, **options)
            finally:
                os.environ.clear()
                os.environ.update(saved)

        return run

    def test_a_repaired_run_counts_for_repairs_only(self):
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder) / "repairs"
            lines = []
            text = run_repairs(
                {"s11-20": self.raw},
                GOAL,
                DECLARATION,
                out,
                trials=2,
                parallel=1,
                claude=str(self.claude),
                runner=self.behaving({"1": "broken"}),
                progress=lines.append,
            )
            summary = summarise_repairs(out)
            self.assertEqual(summary.pairs, parity.Pairs(1, 1, 0, 0), "\n".join(lines))
            trial = next(t for t in read_plan(out)[1] if t.trial == 1)
            first = rounds(out, trial)
            self.assertEqual((first.first, first.final), ("FAIL", "PASS"))
            usage = json.loads(
                (out / RUNS_DIR / "s11-20" / "repair" / "1" / "usage.json").read_text()
            )
            self.assertEqual(
                [r["role"] for r in usage["roles"]],
                ["cross-source", "engineer", "engineer repair 1"],
            )
            self.assertEqual(usage["sandbox"], "bubblewrap")
            # Each run: cross-source 0.1204 and engineer 0.0812 = 0.2016; the
            # repair adds 0.0812. First round: 0.4032 over 1 pass. With
            # repairs: 0.4844 over 2 passes = 0.2422.
            self.assertAlmostEqual(summary.cost_per_pass["first round"], 0.4032)
            self.assertAlmostEqual(summary.cost_per_pass["with repairs"], 0.2422)
            self.assertIn("pass rate: first round 50%, with repairs 100%", text)

    def test_the_first_attempt_does_not_depend_on_the_repairs(self):
        seen = {}
        with tempfile.TemporaryDirectory() as folder:
            for repairs in (0, 2):
                out = Path(folder) / f"run-{repairs}"
                self.behaving({})(
                    self.raw,
                    GOAL,
                    DECLARATION,
                    out,
                    str(self.claude),
                    cross_source=True,
                    repairs=repairs,
                )
                for step in ("cross-source", "engineer"):
                    report = json.loads((out / step / "attempt-1" / "agent.json").read_text())
                    record = json.loads(report["result"])
                    argv = [arg.replace(str(out), "<out>") for arg in record["argv"]]
                    seen[(repairs, step)] = (record["seen"], argv, record["visible"])
                self.assertFalse((out / "engineer" / "attempt-2").exists())
        for step in ("cross-source", "engineer"):
            with self.subTest(step=step):
                self.assertEqual(seen[(0, step)][:2], seen[(2, step)][:2])
                self.assertEqual(
                    list(seen[(0, step)][2].values()), list(seen[(2, step)][2].values())
                )


if __name__ == "__main__":
    unittest.main()
