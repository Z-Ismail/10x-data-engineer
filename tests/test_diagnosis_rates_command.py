"""``python -m tenx.diagnosis_rates`` on part of the failing bed, with the real gate.

The stand-in Claude Code blames the model of the first check ``PACKET.md``
lists as failed, always as a row filter, so it is right on a row-filter
variant and right only about the model on the others. The command runs
unisolated; the sandboxed runner has its own test below, where the bed
and the measurement's folder must be out of the agent's sight.
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from failing_bed import write_bed
from test_diagnose_hide import PROBE, bubblewrap
from test_engineer import DECLARATION, DEMO_GOAL

from demo.generate import generate
from tenx import diagnosis_rates as rates
from tenx.graph.build import DIAGNOSTICS_STEP
from tenx.sandbox import Bubblewrap

ROOT = Path(__file__).resolve().parent.parent
KEPT = ("refunds_dropped", "yen_scaled_by_100", "crosswalk_whole_email")

STAND_IN = """
import json, re, sys
from pathlib import Path

here = Path.cwd()
failed = (here / "PACKET.md").read_text().split("## Checks that failed")[1]
check, subject = re.search(r"^- `(\\w+)` on `(\\w+)`:", failed, re.M).groups()
file = next((here / "project").rglob(f"{subject}.sql")).relative_to(here).as_posix()
print(json.dumps({
    "type": "result", "subtype": "success", "is_error": False, "num_turns": 4,
    "total_cost_usd": 0.03, "modelUsage": {"m": {"costBasis": "list"}},
    "permission_denials": [],
    "structured_output": {
        "summary": "Rows are left out.", "facts": [], "missing": [],
        "causes": [{"likelihood": "probable", "cause": "row filter", "model": subject,
                    "file": file, "line": 1, "claim": "c", "evidence": "e",
                    "explains": [{"check": check, "subject": subject}]}]},
}))
"""


def program(folder: Path, body: str) -> str:
    folder.mkdir(parents=True, exist_ok=True)
    claude = folder / "claude"
    claude.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    claude.chmod(claude.stat().st_mode | stat.S_IXUSR)
    return str(claude)


def small_bed(folder: Path) -> Path:
    """The failing bed cut down to three variants (one observed)."""
    write_bed(folder, seed=5)
    found = json.loads((folder / "answers.json").read_text())
    kept = {n: a for n, a in found["projects"].items() if a["variant"] in KEPT}
    for name in set(found["projects"]) - set(kept):
        shutil.rmtree(folder / "projects" / name)
    found["projects"] = kept
    (folder / "answers.json").write_text(json.dumps(found, indent=2))
    return folder


class CommandTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name).resolve()
        generate(11, 60, root / "data", root / "generated")
        small_bed(root / "bed")
        cls.root, cls.claude = root, program(root / "bin", STAND_IN)
        cls.first = cls.tenx("run", *cls.arguments())
        cls.again = cls.tenx("run", *cls.arguments())
        cls.shown = cls.tenx("report", str(root / "out"))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @classmethod
    def arguments(cls) -> list[str]:
        root = cls.root
        return [
            "--bed", str(root / "bed"), "--raw", f"s11-60={root / 'data'}",
            "--goal", str(DEMO_GOAL), "--checks", str(DECLARATION), "--out", str(root / "out"),
            "--claude", cls.claude, "--unisolated",
        ]  # fmt: skip

    @classmethod
    def tenx(cls, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "tenx.diagnosis_rates", *args],
            check=False,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=900,
        )

    def test_every_project_is_diagnosed_and_scored(self):
        self.assertEqual(self.first.returncode, 0, self.first.stderr)
        text = self.first.stdout
        self.assertTrue(text.startswith("3 diagnoses to do, estimated at US$0.24"), text)
        self.assertIn("Right (first cause names the cause and the model): 1 of 3 (33%,", text)
        self.assertIn("observed in live runs: 0 of 1", text)
        self.assertIn("planted: 1 of 2", text)
        self.assertIn(
            "Otherwise: model only 2, cause only 0, wrong 0, no cause 0, no diagnosis 0", text
        )
        self.assertIn("Cost: US$0.0900 for 3 runs", text)
        self.assertNotIn("other checks than the bed lists", text)
        self.assertIn("  refunds_dropped               s11-60      right\n", text)

    def test_a_second_start_runs_nothing_and_report_repeats_it(self):
        self.assertEqual(self.again.returncode, 0, self.again.stderr)
        self.assertTrue(self.again.stdout.startswith("0 diagnoses to do"), self.again.stdout)
        self.assertEqual(self.shown.stdout, self.again.stdout.split("\n", 1)[1])

    def test_each_run_keeps_its_diagnosis_and_the_bed_is_unchanged(self):
        runs = sorted((self.root / "out" / "runs" / "s11-60").iterdir())
        self.assertEqual(len(runs), 3)
        for folder in runs:
            record = json.loads((folder / "diagnosis-record.json").read_text())
            written = folder / "run" / DIAGNOSTICS_STEP / "attempt-1" / "diagnosis.json"
            self.assertEqual(json.loads(written.read_text()), record["answer"])
            self.assertFalse((self.root / "bed" / "projects" / folder.name / "target").exists())

    def test_the_step_name_is_the_graphs(self):
        self.assertEqual(rates.DIAGNOSTICS_STEP, DIAGNOSTICS_STEP)

    def test_a_missing_bed_is_refused(self):
        done = self.tenx(
            "run", *self.arguments()[:1], str(self.root / "none"), *self.arguments()[2:]
        )
        self.assertEqual(done.returncode, 2)
        self.assertIn("cannot read", done.stderr)


class SandboxedRunnerTest(unittest.TestCase):
    """The runner hides the bed and the measurement's folder from the agent."""

    def test_neither_the_bed_nor_other_runs_can_be_read(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        generate(11, 20, root / "data", root / "generated")
        bed = small_bed(root / "bed")
        out = root / "out"
        other = out / "runs" / "s1" / "project-99" / "diagnosis-record.json"
        other.parent.mkdir(parents=True)
        other.write_text("another run's diagnosis\n")
        # The control's bed and folder do not exist, which a run must accept.
        (root / "home").mkdir()
        sandbox = Bubblewrap(bubblewrap().executable, home=root / "home", scratch=())
        claude = program(root / "bin", PROBE)
        project = min((bed / "projects").iterdir())
        seen = {}
        for secret in (bed / "answers.json", other):
            os.environ["PROBE_SECRET"] = str(secret)
            self.addCleanup(os.environ.pop, "PROBE_SECRET", None)
            for hidden, runner in (
                (True, rates.claude_runner(out, bed, claude, sandbox)),
                (False, rates.claude_runner(root / "elsewhere", root / "no-bed", claude, sandbox)),
            ):
                folder = out / "runs" / "s11" / f"{secret.name}-{hidden}"
                runner(project, root / "data", DEMO_GOAL, DECLARATION, folder)
                probe = folder / "diagnostics" / "attempt-1" / "work" / "probe.txt"
                seen[(secret.name, hidden)] = probe.read_text()
        self.assertTrue(seen[("answers.json", True)].startswith("refused: "), seen)
        self.assertTrue(seen[("diagnosis-record.json", True)].startswith("refused: "), seen)
        # Positive control: with other folders hidden, the probe reads both.
        self.assertIn("variant", seen[("answers.json", False)])
        self.assertEqual(seen[("diagnosis-record.json", False)], "another run's diagnosis\n")


if __name__ == "__main__":
    unittest.main()
