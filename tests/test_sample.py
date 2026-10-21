"""Tests for the row sample given to the scouts.

Expected rows are read from the files with the csv module or written out by
hand, never taken from the sampler itself.
"""

import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path

import duckdb

from tenx.sample import pick, sample

ROOT = Path(__file__).resolve().parent.parent


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class SmallFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_csv_smaller_than_the_sample_is_given_whole_as_text(self):
        path = write(
            self.root / "people.csv",
            "id,email,joined\n007,a@x.example,03/02/2026\n012, B@X.example ,\n",
        )
        self.assertEqual(
            sample(path, "shop.people"),
            '{"id": "007", "email": "a@x.example", "joined": "03/02/2026"}\n'
            '{"id": "012", "email": " B@X.example ", "joined": null}\n',
        )

    def test_json_lines_are_given_exactly_as_written(self):
        lines = [
            '{"id": 1, "at": "2026-01-01T00:00:00Z", "amount": 1.50, "tags": ["a"]}',
            '{"id":2,"at":"2026-01-02T00:00:00+00:00","amount":2,"tags":[]}',
        ]
        path = write(self.root / "events.jsonl", lines[0] + "\n\n" + lines[1] + "\r\n")
        self.assertEqual(sample(path, "app.events"), lines[0] + "\n" + lines[1] + "\n")

    def test_a_json_array_and_parquet_are_read_by_duckdb(self):
        array = write(
            self.root / "items.json", '[{"id": 1, "name": "x1"}, {"id": 2, "name": " x2"}]'
        )
        self.assertEqual(
            sample(array, "shop.items"),
            '{"id": 1, "name": "x1"}\n{"id": 2, "name": " x2"}\n',
        )
        parquet = self.root / "items.parquet"
        con = duckdb.connect()
        con.execute(
            "COPY (SELECT i AS id, 'Ä' || i AS name FROM range(2) t(i)) "
            f"TO '{parquet}' (FORMAT parquet)"
        )
        con.close()
        self.assertEqual(
            sample(parquet, "shop.items"), '{"id": 0, "name": "Ä0"}\n{"id": 1, "name": "Ä1"}\n'
        )

    def test_size_must_be_positive_and_the_file_supported(self):
        path = write(self.root / "a.csv", "id\n1\n")
        with self.assertRaises(ValueError):
            sample(path, "a", size=0)
        with self.assertRaises(ValueError):
            sample(write(self.root / "a.txt", "id\n1\n"), "a")


class Picking(unittest.TestCase):
    def test_fewer_items_than_the_size_are_all_kept_in_order(self):
        self.assertEqual(pick(["c", "a", "b"], "s", size=5), ["c", "a", "b"])
        self.assertEqual(pick([], "s"), [])

    def test_twenty_of_a_hundred_in_their_original_order(self):
        chosen = pick(range(100), "demo.numbers")
        self.assertEqual(len(chosen), 20)
        self.assertEqual(chosen, sorted(set(chosen)))
        self.assertTrue(set(chosen) <= set(range(100)))

    def test_the_same_seed_gives_the_same_items_and_another_seed_others(self):
        first = pick(range(100), "demo.numbers")
        self.assertEqual(pick(range(100), "demo.numbers"), first)
        self.assertNotEqual(pick(range(100), "demo.other"), first)

    def test_every_item_can_be_picked_and_none_is_favoured(self):
        # Each item should be picked in about 20 of 100 draws of 20 from 100,
        # so about 40 times in 200 seeds (standard deviation about 5.7). The
        # first 50 should take about half of all picks: 2,000 (about 28).
        picked = Counter()
        for seed in range(200):
            picked.update(pick(range(100), f"seed {seed}"))
        self.assertEqual(sum(picked.values()), 200 * 20)
        self.assertEqual(set(picked), set(range(100)))
        self.assertGreater(min(picked.values()), 15)
        self.assertLess(max(picked.values()), 65)
        early = sum(picked[n] for n in range(50))
        self.assertGreater(early, 1900)
        self.assertLess(early, 2100)


class LargerFile(unittest.TestCase):
    """A 100-row CSV with a running number, so every row can be told apart."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        lines = ["n,text"] + [f"{n},row {n:03d}" for n in range(100)]
        cls.path = write(Path(cls.tmp.name) / "numbers.csv", "\n".join(lines) + "\n")
        with cls.path.open(newline="", encoding="utf-8") as handle:
            cls.file_rows = list(csv.DictReader(handle))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_twenty_rows_of_the_file_in_file_order(self):
        rows = [json.loads(line) for line in sample(self.path, "demo.numbers").splitlines()]
        self.assertEqual(len(rows), 20)
        for row in rows:
            self.assertIn(row, self.file_rows)
        numbers = [int(row["n"]) for row in rows]
        self.assertEqual(numbers, sorted(set(numbers)))
        self.assertEqual(numbers, pick(range(100), "demo.numbers"))

    def test_the_choice_does_not_depend_on_the_hash_seed(self):
        script = (
            "import sys; from tenx.sample import sample; "
            "print(sample(sys.argv[1], 'demo.numbers'), end='')"
        )
        outputs = set()
        for hash_seed in ("1", "2"):
            environment = dict(os.environ, PYTHONHASHSEED=hash_seed)
            result = subprocess.run(
                [sys.executable, "-c", script, str(self.path)],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=True,
            )
            outputs.add(result.stdout)
        self.assertEqual(outputs, {sample(self.path, "demo.numbers")})


if __name__ == "__main__":
    unittest.main()
