"""The failing bed: projects that fail the gate for one known reason."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import failing_bed
from failing_bed import (
    CAUSES,
    CROSSWALK,
    CROSSWALK_COMMENT,
    OBSERVED,
    REFERENCE,
    VARIANTS,
    BedError,
    Edit,
    Variant,
    make,
    repair,
    write_bed,
)

from demo.generate import generate
from tenx.gate import run_gate

DECLARATION = Path(__file__).resolve().parent / "fixtures" / "demo_gate.json"

CHECKS = {"build", "rows", "key", "total", "expected", "repeat"}
MODELS = {
    "stg_billing__customers",
    "stg_billing__invoices",
    "stg_crm__accounts",
    "stg_events__events",
    "int_customers_matched_to_accounts",
}


def files(folder: Path) -> dict[str, str]:
    return {
        str(path.relative_to(folder)): path.read_text(encoding="utf-8")
        for path in sorted(folder.rglob("*"))
        if path.is_file()
    }


class VariantsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_names_are_unique_and_every_variant_is_described(self):
        names = [chosen.name for chosen in VARIANTS]
        self.assertEqual(len(names), len(set(names)))
        for chosen in VARIANTS:
            with self.subTest(chosen.name):
                self.assertIn(chosen.cause, CAUSES)
                self.assertIn(chosen.origin, (OBSERVED, failing_bed.PLANTED))
                self.assertIn(chosen.model, MODELS)
                self.assertTrue(chosen.summary.endswith("."))
                self.assertTrue(chosen.edits)
                self.assertEqual({edit.path for edit in chosen.edits}, {chosen.path})
                self.assertTrue(chosen.failing)
                for check, subject, status in chosen.failing:
                    self.assertIn(check, CHECKS)
                    self.assertIn(subject, MODELS | {"project"})
                    self.assertIn(status, ("fail", "skip"))

    def test_the_observed_variants_are_the_mistakes_seen_in_live_runs(self):
        observed = {chosen.name for chosen in VARIANTS if chosen.origin == OBSERVED}
        self.assertEqual(observed, {"crosswalk_whole_email", "customers_one_per_email"})

    def test_each_variant_is_the_reference_without_the_comment_plus_its_edits(self):
        for chosen in VARIANTS:
            with self.subTest(chosen.name):
                project = make(chosen, self.root / chosen.name)
                expected = files(REFERENCE)
                expected[CROSSWALK] = expected[CROSSWALK].replace(CROSSWALK_COMMENT, "")
                for edit in chosen.edits:
                    self.assertEqual(expected[edit.path].count(edit.old), 1)
                    expected[edit.path] = expected[edit.path].replace(edit.old, edit.new)
                self.assertEqual(files(project), expected)
                self.assertNotEqual(expected[chosen.path], files(REFERENCE)[chosen.path])

    def test_no_project_keeps_the_comment_naming_the_match(self):
        for chosen in VARIANTS:
            with self.subTest(chosen.name):
                text = (make(chosen, self.root / chosen.name) / CROSSWALK).read_text()
                self.assertNotIn("domain", text)
                self.assertNotIn("--", text)

    def test_repair_writes_the_edited_files_back_as_the_bed_has_them(self):
        clean = make(Variant("x", "type", OBSERVED, "", "", CROSSWALK, (), ()), self.root / "ok")
        for chosen in VARIANTS:
            with self.subTest(chosen.name):
                project = make(chosen, self.root / chosen.name)
                repair(chosen, project)
                self.assertEqual(files(project), files(clean))

    def test_an_edit_that_does_not_apply_once_is_refused_and_leaves_nothing(self):
        for old in ("no such text", "    "):
            with self.subTest(old=old):
                broken = Variant(
                    "broken", "type", OBSERVED, "", "", CROSSWALK, (Edit(CROSSWALK, old, "x"),), ()
                )
                destination = self.root / "broken"
                with self.assertRaisesRegex(ValueError, "not once"):
                    make(broken, destination)
                self.assertFalse(destination.exists())

    def test_the_comment_must_be_in_the_reference(self):
        reference = self.root / "reference"
        shutil.copytree(REFERENCE, reference)
        crosswalk = reference / CROSSWALK
        crosswalk.write_text(crosswalk.read_text().replace(CROSSWALK_COMMENT, ""))
        with self.assertRaisesRegex(ValueError, "not once"):
            make(VARIANTS[0], self.root / "p", reference)


class WriteBedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_projects_have_neutral_names_and_the_answers_say_which_is_which(self):
        answers = write_bed(self.root / "bed", seed=3)
        projects = sorted(path.name for path in (self.root / "bed" / "projects").iterdir())
        self.assertEqual(projects, [f"project-{n:02d}" for n in range(1, len(VARIANTS) + 1)])
        self.assertEqual(json.loads((self.root / "bed" / "answers.json").read_text()), answers)
        self.assertEqual(answers["causes"], CAUSES)
        by_variant = {
            entry["variant"]: (name, entry) for name, entry in answers["projects"].items()
        }
        self.assertEqual(set(by_variant), {chosen.name for chosen in VARIANTS})
        for chosen in VARIANTS:
            with self.subTest(chosen.name):
                name, entry = by_variant[chosen.name]
                self.assertEqual(
                    entry,
                    {
                        "variant": chosen.name,
                        "cause": chosen.cause,
                        "origin": chosen.origin,
                        "model": chosen.model,
                        "file": chosen.path,
                        "summary": chosen.summary,
                        "failing": sorted(list(result) for result in chosen.failing),
                    },
                )
                expected = make(chosen, self.root / "single" / chosen.name)
                self.assertEqual(files(self.root / "bed" / "projects" / name), files(expected))

    def test_nothing_inside_a_project_says_what_it_is(self):
        write_bed(self.root / "bed")
        texts = "\n".join(
            "\n".join(files(folder).values())
            for folder in (self.root / "bed" / "projects").iterdir()
        )
        for chosen in VARIANTS:
            with self.subTest(chosen.name):
                self.assertNotIn(chosen.name, texts)
                self.assertNotIn(chosen.summary, texts)
        for word in ("answers", "observed", "planted", "cause"):
            self.assertNotIn(word, texts)

    def test_the_seed_fixes_the_order(self):
        one = write_bed(self.root / "one", seed=5)
        again = write_bed(self.root / "again", seed=5)
        other = write_bed(self.root / "other", seed=6)
        self.assertEqual(
            (self.root / "one" / "answers.json").read_bytes(),
            (self.root / "again" / "answers.json").read_bytes(),
        )
        self.assertEqual(one, again)
        order = [entry["variant"] for entry in one["projects"].values()]
        self.assertNotEqual(order, [entry["variant"] for entry in other["projects"].values()])
        self.assertNotEqual(order, [chosen.name for chosen in VARIANTS])

    def test_a_folder_with_files_in_it_is_refused(self):
        (self.root / "bed").mkdir()
        (self.root / "bed" / "keep.txt").write_text("mine")
        with self.assertRaisesRegex(BedError, "is not empty"):
            write_bed(self.root / "bed")
        self.assertEqual(sorted(p.name for p in (self.root / "bed").iterdir()), ["keep.txt"])

    def test_the_command_writes_the_bed_and_refuses_a_full_folder(self):
        script = Path(failing_bed.__file__)
        done = subprocess.run(
            [sys.executable, str(script), "--out", str(self.root / "bed"), "--seed", "2"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(
            done.stdout.splitlines()[0],
            f"{len(VARIANTS)} failing projects (2 observed in live runs)"
            f" in {self.root / 'bed' / 'projects'}",
        )
        self.assertEqual(json.loads((self.root / "bed" / "answers.json").read_text())["seed"], 2)
        again = subprocess.run(
            [sys.executable, str(script), "--out", str(self.root / "bed")],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(again.returncode, 2)
        self.assertIn("is not empty", again.stderr)


class GateTest(unittest.TestCase):
    """Each variant through the real gate on one small dataset."""

    SEED = 11
    COMPANIES = 60

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.raw = root / "data"
        generate(cls.SEED, cls.COMPANIES, cls.raw, root / "answers")
        clean = Variant("clean", "type", OBSERVED, "", "", CROSSWALK, (), ())
        chosen = [clean, *VARIANTS]

        def gate(variant: Variant) -> tuple[str, list]:
            project = make(variant, root / variant.name)
            results = run_gate(project, cls.raw, DECLARATION)
            return variant.name, results

        with ThreadPoolExecutor(max(2, os.cpu_count() or 1)) as pool:
            cls.results = dict(pool.map(gate, chosen))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def not_passed(self, name: str) -> set:
        return {
            (result.check, result.subject, result.status)
            for result in self.results[name]
            if result.status != "pass"
        }

    def test_the_bed_reference_passes(self):
        self.assertEqual(self.not_passed("clean"), set())
        self.assertEqual(len(self.results["clean"]), 24)

    def test_each_variant_fails_exactly_its_listed_checks(self):
        for chosen in VARIANTS:
            with self.subTest(chosen.name):
                self.assertEqual(self.not_passed(chosen.name), set(chosen.failing))
                self.assertEqual(len(chosen.failing), len(set(chosen.failing)))

    def test_one_cause_shows_only_in_a_model_built_from_its_own(self):
        # Read as numbers, account identifiers still pass every check on the
        # accounts model; only the crosswalk built from it fails.
        hidden = {
            chosen.name
            for chosen in VARIANTS
            if not {subject for _, subject, _ in chosen.failing} & {chosen.model, "project"}
        }
        self.assertEqual(hidden, {"account_id_as_number"})


if __name__ == "__main__":
    unittest.main()
