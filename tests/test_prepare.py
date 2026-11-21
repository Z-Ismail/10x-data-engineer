"""Preparing a project the run did not make, so that an agent can build it in its folder."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from demo.generate import generate
from tenx.adapters.duckdb import DbtDuckDB
from tenx.gate import dbt_installed
from tenx.ports import PlatformError

REFERENCE = Path(__file__).resolve().parent / "fixtures" / "demo_project"
PROFILE = """demo:
  target: dev
  outputs:
    dev:
      type: duckdb
      path: dev.duckdb
      threads: 1
"""


class PrepareTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="tenx-prepare-"))
        self.addCleanup(shutil.rmtree, self.root)
        self.raw = self.root / "raw"
        self.raw.mkdir()
        self.project = self.root / "project"
        self.project.mkdir()
        self.config = self.project / "dbt_project.yml"

    def test_a_project_without_vars_gains_raw_dir_and_the_run_s_profile(self):
        self.config.write_text("name: shop\nprofile: shop_profile\n# keep me\n")
        changed = DbtDuckDB().prepare(self.project, self.raw)
        self.assertEqual(changed, ["dbt_project.yml", "profiles.yml"])
        self.assertEqual(
            self.config.read_text(),
            "name: shop\nprofile: shop_profile\n# keep me\n\nvars:\n"
            f'  raw_dir: "{self.raw.resolve()}"\n',
        )
        self.assertEqual(
            (self.project / "profiles.yml").read_text(),
            PROFILE.replace("demo:", "shop_profile:"),
        )
        # Done once: a second call changes nothing.
        self.assertEqual(DbtDuckDB().prepare(self.project, self.raw), [])

    def test_a_file_without_a_final_newline_and_without_a_profile_uses_the_name(self):
        self.config.write_text("name: shop")
        DbtDuckDB().prepare(self.project, self.raw)
        self.assertEqual(
            self.config.read_text(), f'name: shop\n\nvars:\n  raw_dir: "{self.raw.resolve()}"\n'
        )
        self.assertTrue((self.project / "profiles.yml").read_text().startswith("shop:\n"))

    def test_a_raw_dir_already_set_is_kept_and_a_person_s_profile_replaced(self):
        text = "name: shop\nvars:\n  raw_dir: /data/raw\n"
        self.config.write_text(text)
        (self.project / "profiles.yml").write_text("shop:\n  target: prod\n")
        self.assertEqual(DbtDuckDB().prepare(self.project, self.raw), ["profiles.yml"])
        self.assertEqual(self.config.read_text(), text)
        self.assertEqual(
            (self.project / "profiles.yml").read_text(), PROFILE.replace("demo:", "shop:")
        )

    def test_projects_the_run_cannot_prepare_are_refused_and_left_as_they_were(self):
        cases = {
            "name: shop\nvars:\n  region: eu\n": "has a vars block without raw_dir",
            "name: shop\nvars: []\n": "has a vars block without raw_dir",
            "- a list\n": "does not hold a project's settings",
            "name: [unclosed\n": "cannot be read",
            'name: shop\nprofile: "a\'; drop"\n': "names no profile the run can write",
            "version: 2\n": "names no profile the run can write",
        }
        for text, reason in cases.items():
            with self.subTest(text=text):
                self.config.write_text(text)
                with self.assertRaisesRegex(PlatformError, reason):
                    DbtDuckDB().prepare(self.project, self.raw)
                self.assertEqual(self.config.read_text(), text)
                self.assertFalse((self.project / "profiles.yml").exists())

    def test_links_are_never_written_through(self):
        outside = self.root / "outside.yml"
        outside.write_text("name: shop\n")
        os.symlink(outside, self.config)
        with self.assertRaisesRegex(PlatformError, "is not a file"):
            DbtDuckDB().prepare(self.project, self.raw)
        self.config.unlink()
        self.config.write_text("name: shop\n")
        os.symlink(outside, self.project / "profiles.yml")
        with self.assertRaisesRegex(PlatformError, "not a file the run can replace"):
            DbtDuckDB().prepare(self.project, self.raw)
        self.assertEqual(outside.read_text(), "name: shop\n")


@unittest.skipUnless(dbt_installed(), "dbt is not installed")
class PreparedBuildTest(unittest.TestCase):
    def test_a_prepared_reference_project_builds_with_the_agent_s_own_command(self):
        root = Path(tempfile.mkdtemp(prefix="tenx-prepare-build-"))
        self.addCleanup(shutil.rmtree, root)
        raw = root / "raw"
        generate(11, 60, raw, root / "answers")
        project = root / "project"
        shutil.copytree(REFERENCE, project)
        # Without the run's preparation, dbt finds no profile here.
        command = [
            sys.executable,
            "-c",
            "from dbt.cli.main import cli; cli(prog_name='dbt')",
            "build",
            "--no-use-colors",
            "--no-send-anonymous-usage-stats",
        ]
        environment = {k: v for k, v in os.environ.items() if not k.startswith("DBT_")}
        environment["HOME"] = str(root)
        before = subprocess.run(
            command, cwd=project, env=environment, capture_output=True, check=False
        )
        self.assertNotEqual(before.returncode, 0)
        DbtDuckDB().prepare(project, raw)
        built = subprocess.run(
            command, cwd=project, env=environment, capture_output=True, text=True, check=False
        )
        self.assertEqual(built.returncode, 0, built.stdout[-2000:])
        results = json.loads((project / "target" / "run_results.json").read_text())
        self.assertTrue(results["results"])
        self.assertTrue(all(r["status"] in ("success", "pass") for r in results["results"]))
        self.assertTrue((project / "dev.duckdb").is_file())


if __name__ == "__main__":
    unittest.main()
