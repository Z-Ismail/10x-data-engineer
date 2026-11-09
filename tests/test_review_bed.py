"""The reviewer test bed: how its variants are made, and that the gate passes them."""

from __future__ import annotations

import filecmp
import tempfile
import unittest
from pathlib import Path

from review_bed import (
    CROSSWALK,
    DECLARATION,
    DEFECT,
    EVENTS,
    GOOD,
    REFERENCE,
    VARIANTS,
    BedError,
    Edit,
    Variant,
    differences,
    make,
    variant,
)

from demo.generate import generate
from tenx.gate import PASS, run_gate


def same_tree(one: Path, other: Path) -> bool:
    files = sorted(path.relative_to(one) for path in one.rglob("*") if path.is_file())
    if files != sorted(path.relative_to(other) for path in other.rglob("*") if path.is_file()):
        return False
    return all(filecmp.cmp(one / name, other / name, shallow=False) for name in files)


class Making(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_names_are_unique_and_kinds_known(self):
        names = [chosen.name for chosen in VARIANTS]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual({chosen.kind for chosen in VARIANTS}, {DEFECT, GOOD})
        self.assertEqual(sum(chosen.kind == DEFECT for chosen in VARIANTS), 6)
        self.assertEqual(sum(chosen.kind == GOOD for chosen in VARIANTS), 6)

    def test_a_defect_names_its_columns_and_a_good_variant_none(self):
        for chosen in VARIANTS:
            with self.subTest(chosen.name):
                if chosen.kind == DEFECT:
                    self.assertTrue(chosen.columns)
                    self.assertTrue(chosen.edits)
                    self.assertEqual(chosen.mapping, ())
                else:
                    self.assertEqual(chosen.columns, ())

    def test_every_edit_changes_exactly_one_place(self):
        for chosen in VARIANTS:
            with self.subTest(chosen.name):
                project = make(chosen, self.root / chosen.name)
                for edit in chosen.edits:
                    original = (REFERENCE / edit.path).read_text(encoding="utf-8")
                    self.assertEqual(original.count(edit.old), 1)
                    text = (project / edit.path).read_text(encoding="utf-8")
                    self.assertEqual(text, original.replace(edit.old, edit.new))
                changed = {edit.path for edit in chosen.edits}
                for path in REFERENCE.rglob("*"):
                    name = str(path.relative_to(REFERENCE))
                    if path.is_file() and name not in changed:
                        self.assertTrue(filecmp.cmp(path, project / name, shallow=False), name)

    def test_the_reference_variant_is_the_reference(self):
        self.assertTrue(same_tree(make(variant("reference"), self.root / "ref"), REFERENCE))

    def test_making_a_variant_twice_gives_the_same_files(self):
        for chosen in VARIANTS:
            with self.subTest(chosen.name):
                one = make(chosen, self.root / f"{chosen.name}-1")
                other = make(chosen, self.root / f"{chosen.name}-2")
                self.assertTrue(same_tree(one, other))

    def test_an_edit_that_does_not_match_once_is_refused(self):
        for old in ("not in the file", "\n"):
            broken = Variant("broken", DEFECT, "x", (Edit("dbt_project.yml", old, "y"),))
            with self.subTest(old=old):
                with self.assertRaisesRegex(ValueError, "not once"):
                    make(broken, self.root / "broken")
                self.assertFalse((self.root / "broken").exists())

    def test_an_unknown_name_is_refused(self):
        with self.assertRaises(KeyError):
            variant("nothing")


def failures(results) -> list[tuple[str, str, str]]:
    return [(r.check, r.subject, r.status) for r in results if r.status != PASS]


class GatePasses(unittest.TestCase):
    """Every variant passes the gate; only a defect's own columns differ from the reference."""

    SEED = 11
    COMPANIES = 60

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.raw = cls.root / "data"
        generate(cls.SEED, cls.COMPANIES, cls.raw, cls.root / "answers")
        cls.reference = cls.root / "reference-build"
        results = run_gate(REFERENCE, cls.raw, DECLARATION, cls.reference)
        assert not failures(results), failures(results)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def build(self, chosen: Variant, name: str | None = None):
        name = name or chosen.name
        project = make(chosen, self.root / f"project-{name}")
        workdir = self.root / f"build-{name}"
        return run_gate(project, self.raw, DECLARATION, workdir), workdir

    def test_each_variant_passes_and_differs_only_where_planted(self):
        for chosen in VARIANTS:
            with self.subTest(chosen.name):
                results, workdir = self.build(chosen)
                self.assertEqual(failures(results), [])
                counts = differences(self.reference, workdir, chosen)
                self.assertEqual(len(counts), 19)
                wrong = {pair for pair, count in counts.items() if count}
                self.assertEqual(wrong, set(chosen.columns))
                for pair in chosen.columns:
                    self.assertGreater(counts[pair], 0)

    def test_a_defect_that_changes_nothing_is_seen(self):
        chosen = variant("contact_name_from_email")
        idle = Variant(chosen.name, DEFECT, chosen.summary, (), chosen.columns)
        results, workdir = self.build(idle, "idle")
        self.assertEqual(failures(results), [])
        self.assertEqual(differences(self.reference, workdir, idle)[chosen.columns[0]], 0)

    def test_an_unintended_change_in_a_good_variant_is_seen(self):
        chosen = variant("columns_renamed")
        careless = Variant(
            chosen.name,
            GOOD,
            chosen.summary,
            (
                *chosen.edits,
                Edit(EVENTS, "event_name,", "upper(event_name) as event_name,"),
            ),
            mapping=chosen.mapping,
        )
        results, workdir = self.build(careless, "careless")
        self.assertEqual(failures(results), [])
        counts = differences(self.reference, workdir, careless)
        self.assertEqual(
            {pair for pair, count in counts.items() if count},
            {("stg_events__events", "event_name")},
        )

    def test_a_variant_the_gate_catches_fails_the_gate(self):
        caught = Variant(
            "inner_crosswalk",
            DEFECT,
            "The crosswalk drops customers with no account.",
            (Edit(CROSSWALK, "left join", "join"),),
            (("int_customers_matched_to_accounts", "account_id"),),
        )
        results, _ = self.build(caught)
        self.assertIn(("rows", "int_customers_matched_to_accounts", "fail"), failures(results))

    def test_a_variant_without_a_reference_column_cannot_be_compared(self):
        chosen = variant("columns_renamed")
        unmapped = Variant(chosen.name, GOOD, chosen.summary, chosen.edits)
        _, workdir = self.build(unmapped, "unmapped")
        with self.assertRaisesRegex(BedError, "no column contact_name"):
            differences(self.reference, workdir, unmapped)


if __name__ == "__main__":
    unittest.main()
