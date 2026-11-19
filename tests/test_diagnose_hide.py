"""Paths a caller hides from a diagnose run stay out of the agent's sight.

The test's files sit in a temporary folder, which every sandbox hides by
itself; here the sandbox is told not to, so only ``hide`` keeps the
folder of secrets from the agent. The same run without ``hide`` is the
positive control: the agent reads the secret.
"""

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from failing_bed import make, variant
from test_engineer import DECLARATION, DEMO_GOAL

from demo.generate import generate
from tenx import run
from tenx.adapters.duckdb import DuckDBGate
from tenx.sandbox import Bubblewrap, SandboxError, default_sandbox

# The stand-in diagnostics agent notes whether it can read the secret, then
# ends without an answer, so the run ends with no diagnosis.
PROBE = """
import json, os, sys
from pathlib import Path

secret = Path(os.environ["PROBE_SECRET"])
try:
    seen = secret.read_text()
except OSError as error:
    seen = f"refused: {type(error).__name__}"
(Path.cwd() / "probe.txt").write_text(seen)
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "num_turns": 1, "total_cost_usd": 0.01,
                  "modelUsage": {"m": {"costBasis": "list"}}, "permission_denials": []}))
"""


def bubblewrap() -> Bubblewrap:
    try:
        found = default_sandbox()
    except SandboxError:
        if os.environ.get("TENX_REQUIRE_SANDBOX"):
            raise
        raise unittest.SkipTest("bubblewrap is not available here") from None
    return found


class HiddenPathsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name).resolve()
        cls.root = root
        cls.raw = root / "data"
        generate(11, 20, cls.raw, root / "answers")
        claude = root / "bin" / "claude"
        claude.parent.mkdir()
        claude.write_text(f"#!{sys.executable}\n{PROBE}", encoding="utf-8")
        claude.chmod(claude.stat().st_mode | stat.S_IXUSR)
        cls.claude = str(claude)
        cls.bed = root / "bed"
        cls.project = make(variant("refunds_dropped"), cls.bed / "projects" / "project-01")
        cls.secret = cls.bed / "answers.json"
        cls.secret.write_text('{"cause": "row filter"}\n')
        executable = bubblewrap().executable
        # Not told to hide the temporary folders, so the test's files are visible.
        cls.sandbox = Bubblewrap(executable, home=root / "home", scratch=())
        (root / "home").mkdir()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def diagnose(self, out: str, hide: tuple[Path, ...]) -> tuple[dict, str]:
        os.environ["PROBE_SECRET"] = str(self.secret)
        self.addCleanup(os.environ.pop, "PROBE_SECRET", None)
        os.environ["TENX_SANDBOX_ENV"] = "PROBE_SECRET"
        self.addCleanup(os.environ.pop, "TENX_SANDBOX_ENV", None)
        folder = self.root / out
        state = run.diagnose(
            self.project,
            self.raw,
            DEMO_GOAL,
            DECLARATION,
            folder,
            sandbox=self.sandbox,
            claude=self.claude,
            hide=hide,
        )
        probe = folder / "diagnostics" / "attempt-1" / "work" / "probe.txt"
        return state, probe.read_text()

    def test_a_hidden_folder_cannot_be_read_by_the_agent_or_the_gate(self):
        validate = DuckDBGate.validate
        hidden_from_gate = []

        def noting(gate, *args):
            hidden_from_gate.append(tuple(args[-1]))
            return validate(gate, *args)

        with mock.patch.object(DuckDBGate, "validate", noting):
            state, seen = self.diagnose("hidden", (self.bed,))
        self.assertEqual(len(hidden_from_gate), 1)
        self.assertIn(self.bed, hidden_from_gate[0])
        self.assertTrue(seen.startswith("refused: "), seen)
        self.assertEqual(state["status"], "failed")
        claims = [json.dumps(finding) for finding in state["findings"]]
        self.assertTrue(any("no diagnosis: " in claim for claim in claims), claims)

    def test_without_hide_the_agent_reads_it(self):
        # Positive control: the probe can see the secret when nothing hides it.
        _, seen = self.diagnose("shown", ())
        self.assertEqual(seen, '{"cause": "row filter"}\n')


if __name__ == "__main__":
    unittest.main()
