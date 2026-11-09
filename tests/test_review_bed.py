"""The reviewer test bed: how its variants are made."""

from __future__ import annotations

import filecmp
import tempfile
import unittest
from pathlib import Path

from review_bed import DEFECT, GOOD, REFERENCE, VARIANTS, Edit, Variant, make, variant


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


if __name__ == "__main__":
    unittest.main()
