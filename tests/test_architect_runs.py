"""The architect comparison through the real graph, with the stand-in claude.

The stand-in of tests/equivalence.py answers the same way every time; here
it also answers as the architect, with the hand-written demo contract, so
the run counts that contract's rules in the sandbox and the comparison can
score it against the declaration. Skipped where bubblewrap cannot run
unless TENX_REQUIRE_SANDBOX is set.
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from equivalence import DECLARATION, ROOT, SCENARIOS, STAND_IN, environment, stand_in

from demo.generate import generate
from tenx.compare import AGREEMENT_FILE, RUNS_DIR, run_architect, run_graph, summarise
from tenx.sandbox import SandboxError, default_sandbox

BRIEF = ROOT / "demo" / "brief.md"
CONTRACT = ROOT / "tests" / "fixtures" / "demo_contract.json"

# Started as the architect, the stand-in answers with the contract kept
# beside it, in the folder the sandbox shows it.
ARCHITECT = """
import json, sys
from pathlib import Path
if "tenx:architect" in sys.argv:
    print(json.dumps({
        "type": "result", "subtype": "success", "is_error": False, "num_turns": 10,
        "duration_ms": 61000, "total_cost_usd": 0.0931,
        "modelUsage": {"claude-sonnet-5-5": {
            "inputTokens": 12, "cacheCreationInputTokens": 15000,
            "cacheReadInputTokens": 60000, "outputTokens": 4000, "costBasis": "list"}},
        "permission_denials": [],
        "structured_output": json.loads((Path(sys.argv[0]).parent / "contract.json").read_text()),
    }))
    sys.exit(0)
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
class ArchitectRunsTest(unittest.TestCase):
    def test_both_arms_through_the_graph_in_the_sandbox(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            raw = root / "data"
            generate(11, 20, raw, root / "answers")
            claude = stand_in(root / "stand-in")
            text = claude.read_text(encoding="utf-8")
            claude.write_text(text.replace(STAND_IN, ARCHITECT + STAND_IN), encoding="utf-8")
            shutil.copyfile(CONTRACT, claude.parent / "contract.json")
            out = root / "compare"

            def run(raw, goal, checks, folder, claude_path, **options):
                saved = dict(os.environ)
                os.environ.update(environment(SCENARIOS["pass"], claude, raw, Path(folder)))
                try:
                    run_graph(raw, goal, checks, folder, claude_path, **options)
                finally:
                    os.environ.clear()
                    os.environ.update(saved)

            lines = []
            text = run_architect(
                {"s11-20": raw},
                BRIEF,
                DECLARATION,
                out,
                trials=1,
                parallel=1,
                claude=str(claude),
                runner=run,
                progress=lines.append,
            )
            runs = out / RUNS_DIR / "s11-20"
            for arm, roles in (
                ("graph", ["cross-source", "engineer"]),
                ("architect", ["cross-source", "architect", "engineer"]),
            ):
                with self.subTest(arm=arm):
                    usage = json.loads((runs / arm / "1" / "usage.json").read_text())
                    self.assertEqual(usage["gate"], "PASS", "\n".join(lines))
                    self.assertEqual(usage["sandbox"], "bubblewrap")
                    self.assertEqual([role["role"] for role in usage["roles"]], roles)
            spec = runs / "architect" / "1" / "engineer" / "attempt-1" / "work" / "SPEC.md"
            self.assertTrue(spec.is_file())
            self.assertFalse((runs / "graph" / "1" / "architect").exists())
            # The demo contract says what the declaration says, model by model.
            found = json.loads((runs / "architect" / "1" / AGREEMENT_FILE).read_text())
            self.assertEqual((found["spec"], found["agreeing"], found["declared"]), (True, 5, 5))
            self.assertIn("s11-20/architect/1  PASS  5 of 5 models agree\n", text)
            # 0.1204 + 0.0812 without the architect; 0.0931 more with it.
            arms = {arm.arm: arm for arm in summarise(out).arms}
            self.assertAlmostEqual(arms["graph"].cost_usd, 0.2016)
            self.assertAlmostEqual(arms["architect"].cost_usd, 0.2947)


if __name__ == "__main__":
    unittest.main()
