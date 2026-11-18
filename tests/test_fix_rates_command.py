"""``python -m tenx.fix_rates`` on part of the failing bed, with the real gate.

The stand-in diagnostician blames the model of the first check ``PACKET.md``
lists as failed, as a row filter. The stand-in engineer copies the reference
models, which repair every variant, but only when ``DIAGNOSIS.md`` is in its
folder: so a project is fixed in the run with the diagnosis and not in the
run without it, which shows the diagnosis reaches the fix in one arm only.
The command runs unisolated; the sandboxed runner has its own test below.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_diagnose_hide import PROBE, bubblewrap
from test_diagnosis_rates_command import STAND_IN, program, small_bed
from test_engineer import DECLARATION, DEMO_GOAL, REFERENCE_MODELS

from demo.generate import generate
from tenx import fix_rates as rates
from tenx.sandbox import Bubblewrap

ROOT = Path(__file__).resolve().parent.parent

ENGINEER = """
import json, os, shutil, sys
from pathlib import Path

if "tenx:engineer" in sys.argv:
    here = Path.cwd()
    if (here / "DIAGNOSIS.md").exists():
        shutil.copytree(os.environ["STUB_MODELS"], here / "models", dirs_exist_ok=True)
    print(json.dumps({
        "type": "result", "subtype": "success", "is_error": False, "num_turns": 6,
        "total_cost_usd": 0.05, "modelUsage": {"m": {"costBasis": "list"}},
        "permission_denials": [],
    }))
    sys.exit(0)
"""


def one_project(bed: Path) -> Path:
    """The small bed cut to its refunds variant: each project here costs two
    full gate runs, and the paths are the same for every variant."""
    found = json.loads((bed / "answers.json").read_text())
    kept = {n: a for n, a in found["projects"].items() if a["variant"] == "refunds_dropped"}
    for name in set(found["projects"]) - set(kept):
        shutil.rmtree(bed / "projects" / name)
    found["projects"] = kept
    (bed / "answers.json").write_text(json.dumps(found, indent=2))
    return bed


class CommandTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name).resolve()
        generate(11, 60, root / "data", root / "generated")
        one_project(small_bed(root / "bed"))
        cls.root, cls.claude = root, program(root / "bin", ENGINEER + STAND_IN)
        os.environ["STUB_MODELS"] = str(REFERENCE_MODELS)
        cls.first = cls.tenx("run", *cls.arguments())
        cls.again = cls.tenx("run", *cls.arguments())
        cls.shown = cls.tenx("report", str(root / "out"))

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("STUB_MODELS", None)
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
            [sys.executable, "-m", "tenx.fix_rates", *args],
            check=False,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=1200,
        )

    def test_every_project_is_fixed_in_both_arms_and_paired(self):
        self.assertEqual(self.first.returncode, 0, self.first.stderr)
        text = self.first.stdout
        self.assertTrue(text.startswith("2 fixing runs to do, estimated at US$0.48"), text)
        self.assertIn("\nFix measurement: unclear\n", text)
        self.assertIn(
            "  both fixed 0, only with the diagnosis 1, only without 0, neither 0\n", text
        )
        self.assertIn("Fixed: with the diagnosis 1 of 1 (100%), without 0 of 1 (0%)\n", text)
        # Each run with the diagnosis: US$0.03 for it and US$0.05 for the fix.
        self.assertIn("with US$0.0800, without -\n", text)
        self.assertNotIn("found the project differently", text)
        self.assertIn(
            "  refunds_dropped               s11-60      with fixed, without not fixed; "
            "diagnosis right (without: the fix still failed: ",
            text,
        )

    def test_a_second_start_runs_nothing_and_report_repeats_it(self):
        self.assertEqual(self.again.returncode, 0, self.again.stderr)
        self.assertTrue(self.again.stdout.startswith("0 fixing runs to do"), self.again.stdout)
        self.assertEqual(self.shown.stdout, self.again.stdout.split("\n", 1)[1])

    def test_only_the_runs_with_the_diagnosis_had_one_and_the_bed_is_unchanged(self):
        projects = sorted((self.root / "out" / "runs" / "s11-60").iterdir())
        self.assertEqual(len(projects), 1)
        for folder in projects:
            work = {
                arm: folder / arm / "run" / "engineer" / "attempt-1" / "work" for arm in rates.ARMS
            }
            self.assertTrue((work["with"] / "DIAGNOSIS.md").is_file())
            self.assertFalse((work["without"] / "DIAGNOSIS.md").exists())
            self.assertFalse((folder / "without" / "run" / "diagnostics").exists())
            self.assertTrue((work["without"] / "GATE_REPORT.md").is_file())
            usage = json.loads((folder / "without" / "run" / "usage.json").read_text())
            self.assertEqual([role["role"] for role in usage["roles"]], ["engineer"])
            self.assertFalse((self.root / "bed" / "projects" / folder.name / "target").exists())


class SandboxedRunnerTest(unittest.TestCase):
    """The runner hides the bed and the measurement's folder from the engineer."""

    def test_neither_the_bed_nor_other_runs_can_be_read(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        generate(11, 20, root / "data", root / "generated")
        bed = small_bed(root / "bed")
        out = root / "out"
        other = out / "runs" / "s1" / "project-99" / "with" / "fix-record.json"
        other.parent.mkdir(parents=True)
        other.write_text("another run's fix\n")
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
                # Without the diagnosis, the engineer is the first agent called.
                runner(project, root / "data", DEMO_GOAL, DECLARATION, folder, True)
                probe = folder / "engineer" / "attempt-1" / "work" / "probe.txt"
                seen[(secret.name, hidden)] = probe.read_text()
        self.assertTrue(seen[("answers.json", True)].startswith("refused: "), seen)
        self.assertTrue(seen[("fix-record.json", True)].startswith("refused: "), seen)
        # Positive control: with other folders hidden, the probe reads both.
        self.assertIn("variant", seen[("answers.json", False)])
        self.assertEqual(seen[("fix-record.json", False)], "another run's fix\n")


if __name__ == "__main__":
    unittest.main()
