"""A paired comparison through both real runners, with the stand-in claude.

The stand-in of tests/equivalence.py answers the same way every time, so the
verdicts and costs are known in advance. Runs in the sandbox, skipped where
bubblewrap cannot run unless TENX_REQUIRE_SANDBOX is set.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from equivalence import DECLARATION, GOAL, SCENARIOS, environment, stand_in

from demo.generate import generate
from tenx import parity
from tenx.compare import RUNS_DIR, run_default, run_graph, run_paired, summarise_paired
from tenx.sandbox import SandboxError, default_sandbox


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
class PairedRunnersTest(unittest.TestCase):
    def test_both_runners_in_the_sandbox(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            raw = root / "data"
            generate(11, 20, raw, root / "answers")
            claude = stand_in(root / "stand-in")
            out = root / "paired"

            def behaving(runner, name):
                # The graph's second trial copies models without the invoices
                # test-mode filter, so that pair is discordant.
                def run(raw, goal, checks, folder, claude_path):
                    trial = Path(folder).name
                    scenario = SCENARIOS["fail" if name == "graph" and trial == "2" else "pass"]
                    saved = dict(os.environ)
                    os.environ.update(environment(scenario, claude, raw, Path(folder)))
                    try:
                        runner(raw, goal, checks, folder, claude_path)
                    finally:
                        os.environ.clear()
                        os.environ.update(saved)

                return run

            lines = []
            text = run_paired(
                {"s11-20": raw},
                GOAL,
                DECLARATION,
                out,
                trials=2,
                parallel=1,
                claude=str(claude),
                runners={
                    "default": behaving(run_default, "default"),
                    "graph": behaving(run_graph, "graph"),
                },
                progress=lines.append,
            )
            summary = summarise_paired(out)
            self.assertEqual(summary.pairs, parity.Pairs(1, 1, 0, 0), "\n".join(lines))
            # Every run costs 0.1204 + 0.0812 = 0.2016: default 0.4032 over 2
            # passes, graph 0.4032 over 1.
            self.assertAlmostEqual(summary.cost_per_pass["default"], 0.2016)
            self.assertAlmostEqual(summary.cost_per_pass["graph"], 0.4032)
            self.assertIn("pass rate verdict: inconclusive", text)
            for arm in ("default", "graph"):
                for trial in ("1", "2"):
                    usage = json.loads(
                        (out / RUNS_DIR / "s11-20" / arm / trial / "usage.json").read_text()
                    )
                    self.assertEqual(usage["sandbox"], "bubblewrap")
                    self.assertEqual(
                        [r["role"] for r in usage["roles"]], ["cross-source", "engineer"]
                    )
            self.assertTrue(
                (out / RUNS_DIR / "s11-20" / "graph" / "1" / "checkpoints.sqlite").is_file()
            )
            self.assertFalse(
                (out / RUNS_DIR / "s11-20" / "default" / "1" / "checkpoints.sqlite").exists()
            )


if __name__ == "__main__":
    unittest.main()
