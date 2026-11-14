"""The diagnosis packet made from real gate runs of failing projects."""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from failing_bed import make, variant, write_bed
from test_gate_sandbox import needs_sandbox

from demo.generate import generate
from tenx import artefacts, diagnosis, diagnostics
from tenx.adapters.duckdb import DuckDBGate, DuckDBProfiler
from tenx.sandbox import default_sandbox

ROOT = Path(__file__).resolve().parent.parent
GOAL = ROOT / "demo" / "goal.md"
DECLARATION = ROOT / "tests" / "fixtures" / "demo_gate.json"
CUSTOMER_ID = re.compile(r"cus_\d{6}[A-Z]")


def packet_files(packet) -> dict[str, str]:
    return {name: (packet.folder / name).read_text(encoding="utf-8") for name in packet.files}


def line_of(text: str, words: str) -> int:
    (number,) = [n for n, line in enumerate(text.splitlines(), start=1) if words in line]
    return number


@needs_sandbox
class PacketFromFailingProjects(unittest.TestCase):
    """Two bed projects gated in the sandbox on seed 11/60, then packed."""

    VARIANTS = ("account_column_misspelt", "crosswalk_whole_email")

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        # A folder name long enough that the database's error cuts it short.
        cls.root = Path(cls.tmp.name) / ("a-folder-with-a-long-name-" * 4)
        cls.raw = cls.root / "data"
        generate(11, 60, cls.raw, cls.root / "answers")
        cls.profiles = cls.root / "profiles"
        DuckDBProfiler().profile(cls.raw, cls.profiles)
        DuckDBProfiler().overlap(cls.raw, cls.profiles)
        # The bed's own answers sit beside the run, as they would in a measurement.
        cls.bed = cls.root / "bed"
        write_bed(cls.bed)
        cls.runs = {}
        for name in cls.VARIANTS:
            project = make(variant(name), cls.root / name / "project")
            workdir = cls.root / name / "gate"
            report = DuckDBGate().validate(
                project, cls.raw, DECLARATION, workdir, default_sandbox()
            )
            packet = diagnosis.build(
                cls.root / name / "packet",
                goal=GOAL,
                profiles=cls.profiles,
                project=project,
                report=report,
                raw=cls.raw,
                build_folder=workdir,
                build_files={
                    "run_results.json": workdir / "target" / "run_results.json",
                    "dbt.log": workdir / "logs" / "dbt.log",
                },
                secrets=(DECLARATION, workdir / "expected.duckdb", cls.bed / "answers.json"),
            )
            cls.runs[name] = (report, packet, workdir)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_the_gate_fails_each_as_the_bed_says(self):
        for name, (report, _, _) in self.runs.items():
            with self.subTest(name=name):
                failing = {(r.check, r.subject, r.status) for r in report.results}
                failing = {item for item in failing if item[2] != "pass"}
                self.assertEqual(failing, set(variant(name).failing))

    def test_the_packet_holds_the_builds_error(self):
        _, packet, _ = self.runs["account_column_misspelt"]
        files = packet_files(packet)
        self.assertEqual(packet.left_out, ())
        error = 'Referenced column "segment_name" not found'
        self.assertIn(error, files["build/dbt.log"])
        results = json.loads(files["build/run_results.json"])
        (node,) = [n for n in results["results"] if n["unique_id"].endswith("stg_crm__accounts")]
        self.assertEqual(node["status"], "error")
        self.assertIn(error, node["message"])
        self.assertIn("## Checks that failed\n\n- build on project:", files["PACKET.md"])

    def test_no_folder_of_the_run_is_named_in_any_file(self):
        for name, (_, packet, _) in self.runs.items():
            for file, text in packet_files(packet).items():
                with self.subTest(name=name, file=file):
                    # The temporary folder, which a path cut short still names.
                    self.assertNotIn(self.tmp.name, text)
                    self.assertNotIn(str(Path(self.tmp.name).resolve()), text)
            files = packet_files(packet)
            self.assertIn("<build>/target", files["build/dbt.log"])
            self.assertIn("<raw>", files["build/run_results.json"])
            self.assertIn("<project>", files["build/dbt.log"])
        log = packet_files(self.runs["account_column_misspelt"][1])["build/dbt.log"]
        self.assertIn("from read_csv('<folder>...", log)

    def test_nothing_hidden_reaches_the_packet(self):
        hidden = {artefacts.file_hash(DECLARATION)}
        for name, (report, packet, workdir) in self.runs.items():
            hidden.add(artefacts.file_hash(workdir / "expected.duckdb"))
            with self.subTest(name=name):
                self.assertTrue(hidden.isdisjoint(packet.files.values()))
                for text in packet_files(packet).values():
                    self.assertNotIn("crosswalk_whole_email", text)
                    self.assertNotIn("account_column_misspelt", text)
        report, packet, _ = self.runs["crosswalk_whole_email"]
        (expected,) = [r for r in report.results if r.check == "expected"]
        self.assertTrue(CUSTOMER_ID.search(expected.detail))
        self.assertIsNone(CUSTOMER_ID.search(packet_files(packet)["PACKET.md"]))

    def test_a_correct_diagnosis_passes_the_check(self):
        quotes = {
            "account_column_misspelt": (
                "build/dbt.log",
                'Referenced column "segment_name" not found',
                "segment_name",
            ),
            "crosswalk_whole_email": (
                "PACKET.md",
                "expected on int_customers_matched_to_accounts",
                "customers.email = accounts.billing_email",
            ),
        }
        for name, (report, packet, _) in self.runs.items():
            chosen = variant(name)
            files = packet_files(packet)
            source, quote, words = quotes[name]
            path = f"project/{chosen.path}"
            answer = {
                "summary": chosen.summary,
                "facts": [{"claim": "What the packet says.", "file": source, "quote": quote}],
                "causes": [
                    {
                        "likelihood": "probable",
                        "cause": chosen.cause,
                        "model": chosen.model,
                        "file": path,
                        "line": line_of(files[path], words),
                        "claim": chosen.summary,
                        "evidence": quote,
                        "explains": [
                            {"check": check, "subject": subject}
                            for check, subject, _ in chosen.failing
                        ],
                    }
                ],
                "missing": [],
            }
            with self.subTest(name=name):
                self.assertIs(diagnostics.check_answer(answer, files, report), answer)
                answer["causes"][0]["line"] = len(files[path].splitlines()) + 1
                with self.assertRaises(diagnostics.DiagnosisError):
                    diagnostics.check_answer(answer, files, report)


if __name__ == "__main__":
    unittest.main()
