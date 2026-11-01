"""The DuckDB adapters write and decide exactly what the modules they wrap do.

Each adapter is run beside the code the runner calls today, on generated
demo data, and the two results are compared: files byte for byte, gate
results field by field.
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from demo.generate import generate
from tenx.adapters.duckdb import DbtDuckDB, DuckDBGate, DuckDBProfiler
from tenx.engineer import prepare_project
from tenx.gate import build_project, passed, render, run_gate
from tenx.overlap import overlap_folder, write_report
from tenx.profile import profile_folder
from tenx.sandbox import Mounts

FIXTURES = Path(__file__).parent / "fixtures"
PROJECT = FIXTURES / "demo_project"
DECLARATION = FIXTURES / "demo_gate.json"
DATASETS = ((1, 20), (7, 200), (2026, 45))


def files(folder: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(folder)): path.read_bytes()
        for path in sorted(folder.rglob("*"))
        if path.is_file()
    }


class Recording:
    """A sandbox that runs the command as it is and keeps what it was asked to show."""

    name = "recording"
    isolated = False

    def __init__(self):
        self.mounts: list[Mounts] = []

    def command(self, command: list[str], mounts: Mounts) -> list[str]:
        self.mounts.append(mounts)
        return list(command)

    def describe(self) -> str:
        return "recording"


class Folder(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="tenx-equivalence-"))
        self.addCleanup(shutil.rmtree, self.root)


class ProfilerEquivalenceTest(Folder):
    def test_profiles_and_overlap_match_the_modules_on_demo_data(self):
        for seed, companies in DATASETS:
            with self.subTest(seed=seed, companies=companies):
                case = self.root / f"{seed}-{companies}"
                raw = case / "data"
                generate(seed, companies, raw, case / "answers")

                direct = profile_folder(raw, case / "direct")
                write_report(overlap_folder(raw), case / "direct")
                adapter = DuckDBProfiler()
                through = adapter.profile(raw, case / "adapter")
                report = adapter.overlap(raw, case / "adapter")

                self.assertEqual(through, direct)
                self.assertEqual(report, overlap_folder(raw))
                written = files(case / "adapter")
                self.assertEqual(written, files(case / "direct"))
                self.assertIn("overlap.json", written)
                self.assertGreaterEqual(len(written), 10)


class EngineEquivalenceTest(Folder):
    def test_scaffold_writes_what_the_runner_writes(self):
        raw = self.root / "data"
        generate(42, 20, raw, self.root / "answers")
        goal = self.root / "goal.md"
        goal.write_text("Build the staging models.\n", encoding="utf-8")
        # A relative path, as a user would type it, must be written out in full.
        relative = Path(os.path.relpath(raw))
        prepare_project(self.root / "runner", relative, goal)
        DbtDuckDB().scaffold(self.root / "adapter", relative)
        runner = files(self.root / "runner")
        dbt_files = {name: content for name, content in runner.items() if name.endswith(".yml")}
        self.assertEqual(files(self.root / "adapter"), dbt_files)
        self.assertEqual(sorted(dbt_files), ["dbt_project.yml", "profiles.yml"])
        self.assertTrue((self.root / "adapter" / "models").is_dir())

    def test_build_matches_the_gate_build_and_passes_on_the_sandbox(self):
        raw = self.root / "data"
        generate(42, 20, raw, self.root / "answers")
        secret = self.root / "secret"
        secret.mkdir()
        direct = build_project(PROJECT.resolve(), raw.resolve(), (self.root / "direct").resolve())
        sandbox = Recording()
        built = DbtDuckDB().build(PROJECT, raw, self.root / "adapter", sandbox, hidden=(secret,))

        self.assertEqual(direct[0], 0)
        self.assertTrue(built.ok)
        self.assertEqual(built.exit_code, direct[0])
        statuses = [(r["unique_id"], r["status"]) for r in direct[1]["results"]]
        self.assertEqual(
            [(r["unique_id"], r["status"]) for r in built.results["results"]], statuses
        )
        self.assertEqual(len(sandbox.mounts), 1)
        mounts = sandbox.mounts[0]
        self.assertEqual(mounts.workspace, (self.root / "adapter").resolve())
        self.assertEqual(mounts.hidden, (secret,))
        self.assertIn(PROJECT.resolve(), mounts.read_only)
        self.assertIn(raw.resolve(), mounts.read_only)


class GateEquivalenceTest(Folder):
    def test_results_and_report_match_the_gate_on_a_correct_and_a_broken_project(self):
        raw = self.root / "data"
        generate(7, 200, raw, self.root / "answers")
        broken = self.root / "broken"
        shutil.copytree(PROJECT, broken)
        invoices = broken / "models" / "staging" / "stg_billing__invoices.sql"
        sql = invoices.read_text(encoding="utf-8")
        self.assertIn("where livemode = 'true'", sql)
        invoices.write_text(sql.replace("where livemode = 'true'", ""), encoding="utf-8")

        for name, project, verdict in (("reference", PROJECT, True), ("broken", broken, False)):
            with self.subTest(project=name):
                direct = run_gate(project, raw, DECLARATION, self.root / f"{name}-direct")
                report = DuckDBGate().validate(
                    project, raw, DECLARATION, self.root / f"{name}-adapter"
                )
                self.assertEqual(passed(direct), verdict)
                self.assertEqual(report.passed, verdict)
                self.assertEqual(report.text, render(direct))
                self.assertEqual(
                    [(r.check, r.subject, r.status, r.detail, r.facts) for r in report.results],
                    [(r.check, r.subject, r.status, r.detail, r.facts) for r in direct],
                )
                failed = {(r.check, r.subject) for r in report.results if r.status == "fail"}
                expected = (
                    set()
                    if verdict
                    else {
                        ("rows", "stg_billing__invoices"),
                        ("total", "stg_billing__invoices"),
                    }
                )
                self.assertEqual(failed, expected)


if __name__ == "__main__":
    unittest.main()
