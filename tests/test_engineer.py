"""Tests for preparing and running the engineer agent's project.

The raw files here are written by hand, so every expected figure is known
without running the code under test.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from tenx.engineer import (
    DEV_DATABASE,
    GOAL_FILE,
    PROJECT_NAME,
    SOURCE_PROFILES_DIR,
    EngineerError,
    prepare_project,
)

# Seven orders, one of them written twice.
ORDERS = """id,mode,amount
1,live,10
2,live,5
3,test,7
4,live,9
5,live,3
5,live,3
6,live,12
"""

GOAL = "Build stg_shop__orders: one row per order, keyed on order_id.\n"


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def dbt(project: Path, *args: str) -> subprocess.CompletedProcess:
    # Run from inside the project, as the agent does, so that dbt finds the
    # project's own profiles.yml.
    environment = {key: value for key, value in os.environ.items() if key != "DBT_PROFILES_DIR"}
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "from dbt.cli.main import cli; cli(prog_name='dbt')",
            *args,
            "--no-use-colors",
            "--no-send-anonymous-usage-stats",
        ],
        cwd=project,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )


class PrepareProjectTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.raw = self.root / "raw"
        write(self.raw / "shop" / "orders.csv", ORDERS)
        self.goal = write(self.root / "goal.md", GOAL)
        self.project = self.root / "run" / "project"

    def test_writes_only_the_goal_profiles_and_an_empty_project(self):
        prepare_project(self.project, self.raw, self.goal)
        self.assertEqual(
            sorted(path.name for path in self.project.iterdir()),
            ["GOAL.md", "dbt_project.yml", "models", "profiles.yml", "source_profiles"],
        )
        self.assertEqual((self.project / GOAL_FILE).read_text(encoding="utf-8"), GOAL)
        self.assertEqual(list((self.project / "models").iterdir()), [])

    def test_profiles_every_raw_table(self):
        profiles = prepare_project(self.project, self.raw, self.goal)
        self.assertEqual([profile["table"] for profile in profiles], ["shop.orders"])
        folder = self.project / SOURCE_PROFILES_DIR
        self.assertEqual(
            sorted(path.name for path in folder.iterdir()),
            ["shop.orders.json", "shop.orders.txt"],
        )
        text = (folder / "shop.orders.txt").read_text(encoding="utf-8")
        self.assertIn("rows: 7", text)
        self.assertIn("1 exact duplicate row", text)

    def test_project_reads_raw_files_from_an_absolute_raw_dir(self):
        relative = Path(os.path.relpath(self.raw))
        prepare_project(self.project, relative, self.goal)
        config = yaml.safe_load((self.project / "dbt_project.yml").read_text(encoding="utf-8"))
        self.assertEqual(config["vars"], {"raw_dir": str(self.raw.resolve())})
        self.assertEqual(config["profile"], PROJECT_NAME)
        profiles = yaml.safe_load((self.project / "profiles.yml").read_text(encoding="utf-8"))
        output = profiles[PROJECT_NAME]["outputs"]["dev"]
        self.assertEqual((output["type"], output["path"]), ("duckdb", DEV_DATABASE))

    def test_dbt_runs_in_the_project_and_reads_the_raw_files(self):
        prepare_project(self.project, self.raw, self.goal)
        query = "select count(*) as n from read_csv('{{ var(\"raw_dir\") }}/shop/orders.csv')"
        shown = dbt(self.project, "show", "--inline", query, "--output", "json")
        self.assertEqual(shown.returncode, 0, shown.stdout)
        self.assertIn('"n": 7', shown.stdout)
        built = dbt(self.project, "build")
        self.assertEqual(built.returncode, 0, built.stdout)
        self.assertTrue((self.project / DEV_DATABASE).is_file())

    def test_an_existing_empty_folder_is_used(self):
        self.project.mkdir(parents=True)
        prepare_project(self.project, self.raw, self.goal)
        self.assertTrue((self.project / "dbt_project.yml").is_file())

    def test_refuses_a_folder_that_is_not_empty(self):
        write(self.project / "models" / "old.sql", "select 1")
        with self.assertRaisesRegex(EngineerError, "must be empty"):
            prepare_project(self.project, self.raw, self.goal)
        self.assertEqual(sorted(path.name for path in self.project.iterdir()), ["models"])

    def test_refuses_a_missing_or_empty_goal(self):
        with self.assertRaisesRegex(EngineerError, "does not exist"):
            prepare_project(self.project, self.raw, self.root / "missing.md")
        with self.assertRaisesRegex(EngineerError, "is empty"):
            prepare_project(self.project, self.raw, write(self.root / "blank.md", " \n"))
        self.assertFalse(self.project.exists())

    def test_refuses_raw_folders_without_data(self):
        with self.assertRaisesRegex(EngineerError, "not a folder"):
            prepare_project(self.project, self.root / "missing", self.goal)
        empty = self.root / "empty"
        empty.mkdir()
        with self.assertRaisesRegex(EngineerError, "no CSV, JSON or Parquet"):
            prepare_project(self.project, empty, self.goal)


if __name__ == "__main__":
    unittest.main()
