"""Tests for preparing and running the engineer agent's project.

The handmade raw files have expected figures worked out by hand. The runner
is tested with a stand-in for the claude command that writes known models
into the project: the reference project, which must pass the gate, or a copy
broken in one known way, which must fail it.
"""

import csv
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import yaml

from demo.generate import generate
from tenx.engineer import (
    AGENT_FILE,
    DEV_DATABASE,
    GOAL_FILE,
    PLUGIN_COPY_DIR,
    PLUGIN_DIR,
    PROJECT_DIR,
    PROJECT_NAME,
    SOURCE_PROFILES_DIR,
    EngineerError,
    agent_command,
    main,
    prepare_project,
    run,
)
from tenx.gate import FAIL, GateError, passed

FIXTURES = Path(__file__).parent / "fixtures"
REFERENCE_MODELS = FIXTURES / "demo_project" / "models"
DECLARATION = FIXTURES / "demo_gate.json"
DEMO_GOAL = Path(__file__).resolve().parent.parent / "demo" / "goal.md"

# Stands in for the claude command. It records how it was started and what
# the project held, copies STUB_MODELS into the project and reports like
# `claude -p --output-format json` does.
STUB = """
import json, os, shutil, sys, time
from pathlib import Path

here = Path.cwd()
Path(os.environ["STUB_RECORD"]).write_text(json.dumps({
    "argv": sys.argv[1:],
    "cwd": str(here),
    "files": sorted(str(path.relative_to(here)) for path in here.rglob("*")),
    "dbt_profiles_dir": os.environ.get("DBT_PROFILES_DIR"),
}))
if os.environ.get("STUB_MODELS"):
    shutil.copytree(os.environ["STUB_MODELS"], here / "models", dirs_exist_ok=True)
mode = os.environ.get("STUB_MODE", "success")
if mode == "crash":
    print("Error: not signed in", file=sys.stderr)
    sys.exit(1)
if mode == "hang":
    time.sleep(60)
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "num_turns": 7, "result": "Built four models."}))
"""

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


class AgentCommandTest(unittest.TestCase):
    def test_runs_the_engineer_headless_with_only_edits_and_dbt_allowed(self):
        self.assertEqual(
            agent_command("claude", Path("/runs/1/plugin")),
            [
                "claude",
                "-p",
                "Build what GOAL.md asks for.",
                "--plugin-dir",
                str(Path("/runs/1/plugin")),
                "--agent",
                "tenx:engineer",
                "--permission-mode",
                "dontAsk",
                "--allowedTools",
                "Edit",
                "Write",
                "Bash(dbt *)",
                "--max-turns",
                "60",
                "--max-budget-usd",
                "5",
                "--output-format",
                "json",
                "--no-session-persistence",
            ],
        )

    def test_the_plugin_folder_holds_the_engineer_agent(self):
        self.assertTrue((PLUGIN_DIR / ".claude-plugin" / "plugin.json").is_file())
        self.assertTrue((PLUGIN_DIR / "agents" / "engineer.md").is_file())


class RunTest(unittest.TestCase):
    SEED = 11
    COMPANIES = 20

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.raw = root / "data"
        generate(cls.SEED, cls.COMPANIES, cls.raw, root / "answers")
        bin_dir = root / "bin"
        bin_dir.mkdir()
        cls.claude = bin_dir / "claude"
        cls.claude.write_text(f"#!{sys.executable}\n{STUB}", encoding="utf-8")
        cls.claude.chmod(cls.claude.stat().st_mode | stat.S_IXUSR)
        cls.broken = root / "broken_models"
        shutil.copytree(REFERENCE_MODELS, cls.broken)
        invoices = cls.broken / "staging" / "stg_billing__invoices.sql"
        sql = invoices.read_text(encoding="utf-8")
        assert "where livemode = 'true'" in sql
        invoices.write_text(sql.replace("where livemode = 'true'", ""), encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.out = self.root / "run"
        self.record = self.root / "record.json"

    def stub(self, models: Path | None = None, mode: str = "success"):
        environment = {
            "STUB_RECORD": str(self.record),
            "STUB_MODELS": str(models) if models else "",
            "STUB_MODE": mode,
            "DBT_PROFILES_DIR": str(self.root / "elsewhere"),
            "PATH": f"{self.claude.parent}{os.pathsep}{os.environ['PATH']}",
        }
        return mock.patch.dict(os.environ, environment)

    def recorded(self) -> dict:
        return json.loads(self.record.read_text(encoding="utf-8"))

    def test_reference_models_pass_and_the_agent_never_sees_the_checks(self):
        with self.stub(REFERENCE_MODELS), redirect_stdout(io.StringIO()) as printed:
            code = main(
                [
                    "--raw",
                    str(self.raw),
                    "--goal",
                    str(DEMO_GOAL),
                    "--checks",
                    str(DECLARATION),
                    "--out",
                    str(self.out),
                ]
            )
        self.assertEqual(code, 0, printed.getvalue())
        report = printed.getvalue()
        self.assertTrue(report.startswith("agent: success after 7 turns (exit 0)\n"), report)
        self.assertIn("gate: PASS (19 passed, 0 failed, 0 not run)", report)

        record = self.recorded()
        project = self.out / PROJECT_DIR
        self.assertEqual(Path(record["cwd"]), project.resolve())
        plugin = (self.out / PLUGIN_COPY_DIR).resolve()
        self.assertEqual(record["argv"], agent_command("claude", plugin)[1:])
        # The plugin is loaded from a copy beside the project, never from a
        # folder that holds it, because that folder could not be written.
        self.assertNotIn(plugin, project.resolve().parents)
        for name in (".claude-plugin/plugin.json", "agents/engineer.md"):
            self.assertEqual((plugin / name).read_bytes(), (PLUGIN_DIR / name).read_bytes(), name)
        self.assertIsNone(record["dbt_profiles_dir"])
        tables = ["billing.customers", "billing.invoices", "crm.accounts", "events.events"]
        profiles = [f"{SOURCE_PROFILES_DIR}/{t}.{kind}" for t in tables for kind in ("json", "txt")]
        self.assertEqual(
            record["files"],
            sorted(
                [GOAL_FILE, "dbt_project.yml", "models", "profiles.yml", SOURCE_PROFILES_DIR]
                + profiles
            ),
        )
        reported = json.loads((self.out / AGENT_FILE).read_text(encoding="utf-8"))
        self.assertEqual(reported["result"], "Built four models.")

    def test_models_that_keep_test_mode_invoices_fail_on_those_invoices(self):
        with (self.raw / "billing" / "invoices.csv").open(newline="", encoding="utf-8") as handle:
            test_mode = [row for row in csv.DictReader(handle) if row["livemode"] == "false"]
        self.assertGreater(len(test_mode), 0)
        with self.stub(self.broken):
            agent, results = run(self.raw, DEMO_GOAL, DECLARATION, self.out, timeout=60)
        self.assertEqual(agent.report["subtype"], "success")
        self.assertFalse(passed(results))
        failed = {(result.check, result.subject) for result in results if result.status == FAIL}
        self.assertEqual(
            failed, {("rows", "stg_billing__invoices"), ("total", "stg_billing__invoices")}
        )
        rows = next(r for r in results if r.status == FAIL and r.check == "rows")
        self.assertTrue(rows.detail.endswith(f": {len(test_mode)} more than expected"), rows.detail)

    def test_an_agent_that_fails_to_run_leaves_nothing_to_pass(self):
        with self.stub(mode="crash"):
            agent, results = run(self.raw, DEMO_GOAL, DECLARATION, self.out, timeout=60)
        self.assertEqual(agent.describe(), "exited 1 without a report: Error: not signed in")
        self.assertFalse(passed(results))
        missing = [r.subject for r in results if r.check == "build" and r.status == FAIL]
        self.assertIn("stg_billing__invoices", missing)

    def test_an_agent_that_does_not_finish_is_stopped(self):
        with self.stub(REFERENCE_MODELS, mode="hang"):
            agent, results = run(self.raw, DEMO_GOAL, DECLARATION, self.out, timeout=5)
        self.assertEqual(agent.describe(), "stopped: did not finish within 5 seconds")
        self.assertFalse((self.out / AGENT_FILE).exists())
        # What the agent wrote before it was stopped is still checked.
        self.assertTrue(passed(results))

    def test_a_missing_claude_command_is_reported(self):
        with (
            mock.patch.dict(os.environ, {"PATH": str(self.root)}),
            self.assertRaisesRegex(EngineerError, "claude was not found"),
        ):
            run(self.raw, DEMO_GOAL, DECLARATION, self.out)

    def test_refuses_an_output_folder_that_is_not_empty(self):
        write(self.out / "project" / "models" / "old.sql", "select 1")
        with self.assertRaisesRegex(EngineerError, "must be empty"):
            run(self.raw, DEMO_GOAL, DECLARATION, self.out)

    def test_an_unusable_declaration_stops_the_run_before_anything_is_written(self):
        bad = write(self.root / "bad.json", '{"models": [{"name": "m", "colour": "red"}]}')
        with self.assertRaises(GateError):
            run(self.raw, DEMO_GOAL, bad, self.out)
        self.assertFalse(self.out.exists())


if __name__ == "__main__":
    unittest.main()
