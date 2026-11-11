"""The review packet made from real gate runs: nothing hidden reaches it."""

from __future__ import annotations

import filecmp
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from review_bed import DECLARATION, REFERENCE, write_bed

from demo.generate import generate
from tenx import review
from tenx.adapters.duckdb import DuckDBGate, DuckDBProfiler
from tenx.ports import CheckResult, ValidationReport

ROOT = Path(__file__).resolve().parent.parent
GOAL = ROOT / "demo" / "goal.md"
# Identifiers from the raw data: a billing customer and a CRM account.
CUSTOMER_ID = re.compile(r"cus_\d{6}[A-Z]")
ACCOUNT_ID = re.compile(r"\b001\d{15}\b")


def _files(folder: Path) -> list[Path]:
    return sorted(path for path in folder.rglob("*") if path.is_file())


class PacketFromTheGate(unittest.TestCase):
    """The reference project, gated for real, then packed with links planted to secrets."""

    SEED = 11
    COMPANIES = 60

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.raw = cls.root / "data"
        generate(cls.SEED, cls.COMPANIES, cls.raw, cls.root / "answers")
        cls.profiles = cls.root / "profiles"
        DuckDBProfiler().profile(cls.raw, cls.profiles)
        DuckDBProfiler().overlap(cls.raw, cls.profiles)
        cls.project = cls.root / "engineer" / "work"
        shutil.copytree(REFERENCE, cls.project)
        cls.workdir = cls.root / "gate"
        cls.report = DuckDBGate().validate(cls.project, cls.raw, DECLARATION, cls.workdir)
        assert cls.report.passed, cls.report.text
        # Links an engineer could have left, to the files the gate hides.
        (cls.project / "models" / "declaration.yml").symlink_to(DECLARATION)
        (cls.project / "models" / "rows.sql").symlink_to(cls.workdir / "expected.duckdb")
        (cls.project / "models" / "gate").symlink_to(cls.workdir, target_is_directory=True)
        cls.packet = review.build(
            cls.root / "packet",
            goal=GOAL,
            profiles=cls.profiles,
            project=cls.project,
            report=cls.report,
            secrets=(DECLARATION, cls.workdir),
        )
        cls.text = (cls.packet.folder / review.PACKET_FILE).read_text(encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_the_gates_files_are_where_the_links_point(self):
        self.assertTrue((self.workdir / "expected.duckdb").is_file())

    def test_the_planted_links_are_left_out(self):
        self.assertEqual(
            self.packet.left_out,
            (
                ("models/declaration.yml", review.LINK),
                ("models/gate", review.LINK),
                ("models/rows.sql", review.LINK),
            ),
        )

    def test_no_packet_file_equals_a_hidden_file(self):
        hidden = [DECLARATION, *_files(self.workdir)]
        self.assertGreater(len(hidden), 3)
        for path in _files(self.packet.folder):
            for secret in hidden:
                self.assertFalse(filecmp.cmp(path, secret, shallow=False), (path, secret))

    def test_no_reference_query_or_expected_row_is_carried(self):
        declared = json.loads(DECLARATION.read_text(encoding="utf-8"))
        queries = [m["expected"]["query"] for m in declared["models"] if "expected" in m]
        self.assertTrue(queries)
        carried = [self.packet.folder / review.PACKET_FILE]
        carried += _files(self.packet.folder / "project")
        for path in carried:
            text = path.read_text(encoding="utf-8")
            for query in queries:
                self.assertNotIn(query, text, path)
            self.assertIsNone(CUSTOMER_ID.search(text), path)
            self.assertIsNone(ACCOUNT_ID.search(text), path)

    def test_lists_every_check_the_gate_ran(self):
        ran = [line for line in self.report.text.splitlines() if line.startswith("PASS")]
        listed = [line for line in self.text.splitlines() if re.match(r"- \w+ on ", line)]
        self.assertEqual(len(listed), len(ran))
        self.assertIn(f"passed every\ncheck the validator ran ({len(ran)} checks)", self.text)
        self.assertRegex(
            self.text,
            r"\n- expected on int_customers_matched_to_accounts: customer_id, account_id: "
            r"the \d+ expected rows, and no other\n",
        )

    def test_carries_the_project_and_the_runs_goal_and_profiles(self):
        folder = self.packet.folder
        self.assertTrue(filecmp.cmp(folder / "GOAL.md", GOAL, shallow=False))
        self.assertEqual(
            [p.relative_to(folder / "source_profiles") for p in _files(folder / "source_profiles")],
            [p.relative_to(self.profiles) for p in _files(self.profiles)],
        )
        carried = [p.relative_to(folder / "project") for p in _files(folder / "project")]
        self.assertEqual(carried, [p.relative_to(REFERENCE) for p in _files(REFERENCE)])
        for name in carried:
            self.assertTrue(filecmp.cmp(folder / "project" / name, REFERENCE / name, shallow=False))


class PacketsOfTheTestBed(unittest.TestCase):
    """Each project of the reviewer test bed packs whole, and its answers never do."""

    def test_each_project_packs_byte_for_byte_without_the_answers(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            bed = root / "bed"
            write_bed(bed, seed=3)
            answers = bed / "answers.json"
            profiles = root / "profiles"
            profiles.mkdir()
            (profiles / "billing.customers.txt").write_text("profile\n", encoding="utf-8")
            check = CheckResult(
                "build", "stg_billing__customers", "pass", "built", feedback="built"
            )
            report = ValidationReport((check,), True, "")
            projects = sorted((bed / "projects").iterdir())
            self.assertEqual(len(projects), 12)
            for project in projects:
                with self.subTest(project=project.name):
                    out = root / "packets" / project.name
                    packet = review.build(
                        out,
                        goal=GOAL,
                        profiles=profiles,
                        project=project,
                        report=report,
                        secrets=(answers,),
                    )
                    self.assertEqual(packet.left_out, ())
                    carried = _files(out / "project")
                    self.assertEqual(
                        [p.relative_to(out / "project") for p in carried],
                        [p.relative_to(project) for p in _files(project)],
                    )
                    for path in carried:
                        source = project / path.relative_to(out / "project")
                        self.assertTrue(filecmp.cmp(path, source, shallow=False))
                    for path in _files(out):
                        self.assertFalse(filecmp.cmp(path, answers, shallow=False))
                        self.assertNotIn("answers", path.name)


if __name__ == "__main__":
    unittest.main()
