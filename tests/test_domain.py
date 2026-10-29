"""Tests for the records a run passes between its steps."""

import hashlib
import shutil
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from tenx.domain import ArtefactRef, Record, RecordError, Usd

HASH = "a" * 64


class Priced(Record):
    usd: Usd


def is_json_only(value) -> bool:
    if value is None or isinstance(value, (str, bool, int)):
        return not isinstance(value, float)
    if isinstance(value, list):
        return all(is_json_only(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and is_json_only(v) for k, v in value.items())
    return False


class AmountTest(unittest.TestCase):
    def test_amounts_follow_the_policy_rule(self):
        self.assertEqual(Priced.load({"usd": "0.10"}).usd, Decimal("0.10"))
        self.assertEqual(Priced.load({"usd": "0.10"}).dump(), {"usd": "0.10"})
        # A float is read through its shortest text, not its binary value.
        self.assertEqual(Priced.load({"usd": 0.1}).usd, Decimal("0.1"))
        self.assertEqual(Priced.load({"usd": 2}).dump(), {"usd": "2"})

    def test_values_that_are_not_json_are_refused(self):
        for bad in [Decimal(1), {1, 2}, float("nan"), Path("x")]:
            with self.subTest(bad=bad), self.assertRaises(RecordError) as caught:
                Priced.load({"usd": bad})
            self.assertIn("not a valid Priced: not JSON", str(caught.exception))

    def test_bad_amounts_are_refused_with_the_field(self):
        for bad in ["1e3", "-1", "NaN", True, "0.12345", "", None, [1]]:
            with self.subTest(bad=bad), self.assertRaises(RecordError) as caught:
                Priced.load({"usd": bad})
            self.assertIn("not a valid Priced: usd:", str(caught.exception))


class ArtefactRefTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder)

    def ref(self, **changes) -> dict:
        value = {"name": "goal", "path": "/x/goal.md", "kind": "file", "sha256": HASH, "attempt": 1}
        value.update(changes)
        return value

    def test_counts_and_hashes_are_checked(self):
        for field, bad in [
            ("attempt", True),
            ("attempt", "1"),
            ("attempt", 1.0),
            ("attempt", -1),
            ("sha256", "A" * 64),
            ("sha256", "a" * 63),
            ("kind", "link"),
            ("path", ""),
        ]:
            with self.subTest(field=field, bad=bad), self.assertRaises(RecordError) as caught:
                ArtefactRef.load(self.ref(**{field: bad}))
            self.assertIn(f"not a valid ArtefactRef: {field}:", str(caught.exception))
        self.assertEqual(ArtefactRef.load(self.ref()).dump(), self.ref())
        self.assertTrue(is_json_only(ArtefactRef.load(self.ref()).dump()))

    def test_a_file_is_referred_to_by_the_hash_of_its_bytes(self):
        path = self.folder / "goal.md"
        path.write_bytes(b"Build the staging models.\n")
        ref = ArtefactRef.of("goal", path, attempt=2)
        expected = hashlib.sha256(b"Build the staging models.\n").hexdigest()
        self.assertEqual(ref.dump(), self.ref(path=str(path), sha256=expected, attempt=2))
        self.assertTrue(ref.unchanged())
        path.write_bytes(b"Build the marts.\n")
        self.assertFalse(ref.unchanged())
        path.unlink()
        self.assertFalse(ref.unchanged())

    def test_a_folder_hash_covers_each_file_and_its_place(self):
        project = self.folder / "project"
        (project / "models").mkdir(parents=True)
        (project / "dbt_project.yml").write_bytes(b"name: demo\n")
        (project / "models" / "stg.sql").write_bytes(b"select 1\n")
        (project / "models" / ".stg.sql.123.tmp").write_bytes(b"half written")
        first = hashlib.sha256(b"name: demo\n").hexdigest()
        second = hashlib.sha256(b"select 1\n").hexdigest()
        listing = f'{{"dbt_project.yml":"{first}","models/stg.sql":"{second}"}}'
        ref = ArtefactRef.of("project", project)
        self.assertEqual(ref.kind, "folder")
        self.assertEqual(ref.attempt, 0)
        self.assertEqual(ref.sha256, hashlib.sha256(listing.encode()).hexdigest())
        self.assertTrue(ref.unchanged())
        # The same bytes in another place are another folder.
        (project / "models" / "stg.sql").rename(project / "stg.sql")
        self.assertFalse(ref.unchanged())

    def test_a_missing_path_is_refused(self):
        with self.assertRaises(FileNotFoundError):
            ArtefactRef.of("goal", self.folder / "missing.md")


if __name__ == "__main__":
    unittest.main()
