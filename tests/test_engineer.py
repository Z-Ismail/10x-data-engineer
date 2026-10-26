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
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import yaml

from demo.generate import generate
from tenx.cross_source import render as render_notes
from tenx.engineer import (
    AGENT_FILE,
    CROSS_SOURCE,
    CROSS_SOURCE_DIR,
    CROSS_SOURCE_FILE,
    DEV_DATABASE,
    GATE_DIR,
    GOAL_FILE,
    PLUGIN_COPY_DIR,
    PLUGIN_DIR,
    PROJECT_DIR,
    PROJECT_NAME,
    SCOUT,
    SCOUTS_DIR,
    SOURCE_PROFILES_DIR,
    USAGE_FILE,
    EngineerError,
    Role,
    agent_command,
    main,
    prepare_project,
    prepare_scout,
    run,
    run_agent,
)
from tenx.gate import FAIL, GateError, load_declaration, passed
from tenx.sandbox import Unisolated
from tenx.scouts import render as render_table_notes

FIXTURES = Path(__file__).parent / "fixtures"
REFERENCE_MODELS = FIXTURES / "demo_project" / "models"
DECLARATION = FIXTURES / "demo_gate.json"
DEMO_GOAL = Path(__file__).resolve().parent.parent / "demo" / "goal.md"
# The stand-in records what it saw outside its folder and copies models from
# the fixtures, so these runs are unisolated; tests/test_sandbox.py runs
# agents in the sandbox.
UNISOLATED = Unisolated()

# Stands in for the claude command. It records how it was started and what
# its folder held, and reports like `claude -p --output-format json` does.
# Started as the engineer, it copies STUB_MODELS into the project. Started as
# the cross-source agent, it records to STUB_RECORD with ".cross-source"
# added, and answers with STUB_ANSWER, or with no answer if that is empty.
# Started as a scout, it records to STUB_RECORD with ".scout." and its folder's
# name added, waits a second so that scouts started together overlap, and
# answers with its table's entry in STUB_SCOUT_ANSWERS: no answer if there is
# none, and no report at all if the entry is "crash".
STUB = """
import json, os, shutil, sys, time
from pathlib import Path

here = Path.cwd()
agent = sys.argv[sys.argv.index("--agent") + 1]
record = os.environ["STUB_RECORD"]
if agent == "tenx:scout":
    started = time.time()
    time.sleep(1)
    Path(f"{record}.scout.{here.name}").write_text(json.dumps({
        "argv": sys.argv[1:],
        "cwd": str(here),
        "files": sorted(str(path.relative_to(here)) for path in here.rglob("*")),
        "started": started,
        "ended": time.time(),
    }))
    answer = json.loads(os.environ.get("STUB_SCOUT_ANSWERS") or "{}").get(here.name)
    if answer == "crash":
        sys.exit(1)
    report = {
        "type": "result", "subtype": "success", "is_error": False, "num_turns": 5,
        "duration_ms": 20000, "duration_api_ms": 19000, "total_cost_usd": 0.003,
        "modelUsage": {"claude-haiku-5-5": {
            "inputTokens": 10, "cacheCreationInputTokens": 3000,
            "cacheReadInputTokens": 9000, "outputTokens": 800, "costBasis": "list"}},
        "permission_denials": [],
    }
    if answer is not None:
        report["structured_output"] = answer
    print(json.dumps(report))
    sys.exit(0)
if agent == "tenx:cross-source":
    Path(record + ".cross-source").write_text(json.dumps({
        "argv": sys.argv[1:],
        "cwd": str(here),
        "files": sorted(str(path.relative_to(here)) for path in here.rglob("*")),
    }))
    if os.environ.get("STUB_PASS_MODE") == "crash":
        sys.exit(1)
    report = {
        "type": "result", "subtype": "success", "is_error": False, "num_turns": 9,
        "duration_ms": 52000, "duration_api_ms": 50000, "total_cost_usd": 0.1204,
        "modelUsage": {"claude-sonnet-5-5": {
            "inputTokens": 12, "cacheCreationInputTokens": 15000,
            "cacheReadInputTokens": 60000, "outputTokens": 3000, "costBasis": "list"}},
        "permission_denials": [],
    }
    if os.environ.get("STUB_ANSWER"):
        report["structured_output"] = json.loads(os.environ["STUB_ANSWER"])
    print(json.dumps(report))
    sys.exit(0)
Path(record).write_text(json.dumps({
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
print(json.dumps({
    "type": "result", "subtype": "success", "is_error": False, "num_turns": 7,
    "duration_ms": 41300, "duration_api_ms": 30000, "total_cost_usd": 0.0812,
    "result": "Built four models.",
    "modelUsage": {"claude-sonnet-5-5": {
        "inputTokens": 20, "cacheCreationInputTokens": 12000,
        "cacheReadInputTokens": 50000, "outputTokens": 2500, "costBasis": "list"}},
    "permission_denials": [{"tool_name": "Bash", "tool_input": {"command": "ls /"}}],
}))
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

# A cross-source answer that names real columns of the demo data.
ANSWER = {
    "joins": [
        {
            "left": "billing.customers.email",
            "right": "crm.accounts.billing_email",
            "prepare": "Trim and lower-case both sides, then compare the domain.",
            "cardinality": "many-to-one",
            "unmatched": "Test-mode customers.",
            "evidence": "overlap.txt.",
        }
    ],
    "cautions": ["Check that no two accounts share a domain."],
}

# Scout answers that name real columns of their demo tables.
CUSTOMERS_NOTES = {
    "findings": [
        {
            "columns": ["livemode"],
            "issue": "Test-mode customers.",
            "evidence": "PROFILE.txt: livemode false.",
            "handling": "Leave them out.",
        }
    ],
    "questions": [],
}
EVENTS_NOTES = {
    "findings": [
        {
            "columns": [],
            "issue": "Exact duplicate rows.",
            "evidence": "PROFILE.txt.",
            "handling": "Keep one copy.",
        },
        {
            "columns": ["user_email"],
            "issue": "Placeholders.",
            "evidence": "PROFILE.txt.",
            "handling": "Treat them as missing.",
        },
    ],
    "questions": ["Are later timestamps real?"],
}

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


class PrepareScoutTest(unittest.TestCase):
    def test_each_scout_gets_the_goal_its_profile_and_rows_of_its_table(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        raw = root / "data"
        generate(11, 20, raw, root / "answers")
        project = root / "project"
        profiles = prepare_project(project, raw, DEMO_GOAL)
        files = {
            "billing.customers": "billing/customers.csv",
            "billing.invoices": "billing/invoices.csv",
            "crm.accounts": "crm/accounts.csv",
            "events.events": "events/events.jsonl",
        }
        self.assertEqual([profile["table"] for profile in profiles], list(files))
        for profile in profiles:
            table = profile["table"]
            with self.subTest(table):
                folder = root / "scouts" / table
                prepare_scout(folder, project, raw, profile)
                self.assertEqual(
                    sorted(path.name for path in folder.iterdir()),
                    [GOAL_FILE, "PROFILE.txt", "SAMPLE.jsonl"],
                )
                self.assertEqual((folder / GOAL_FILE).read_bytes(), DEMO_GOAL.read_bytes())
                self.assertEqual(
                    (folder / "PROFILE.txt").read_bytes(),
                    (project / SOURCE_PROFILES_DIR / f"{table}.txt").read_bytes(),
                )
                # Every sampled row is a row of the raw file, as written, and
                # a file of fewer than 20 rows is given whole.
                lines = (folder / "SAMPLE.jsonl").read_text(encoding="utf-8").splitlines()
                raw_text = (raw / files[table]).read_text(encoding="utf-8")
                if files[table].endswith(".jsonl"):
                    raw_rows = raw_text.splitlines()
                    sampled = lines
                else:
                    raw_rows = list(csv.DictReader(io.StringIO(raw_text)))
                    sampled = [json.loads(line) for line in lines]
                self.assertEqual(len(sampled), min(20, len(raw_rows)))
                for row in sampled:
                    self.assertIn(row, raw_rows)


class DemoGoalTest(unittest.TestCase):
    def test_names_every_model_the_demo_declaration_checks(self):
        goal = DEMO_GOAL.read_text(encoding="utf-8")
        for model in load_declaration(DECLARATION):
            with self.subTest(model.name):
                self.assertIn(f"`{model.name}`", goal)
                for column in model.key + (model.expected.columns if model.expected else ()):
                    self.assertIn(f"`{column}`", goal)


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

    def test_a_role_with_no_allowed_tools_is_given_none(self):
        role = Role("reader", "tenx:reader", "Read it.", (), 3)
        command = agent_command("claude", Path("/p"), role)
        self.assertNotIn("--allowedTools", command)
        self.assertEqual(
            command[1:9],
            [
                "-p",
                "Read it.",
                "--plugin-dir",
                str(Path("/p")),
                "--agent",
                "tenx:reader",
                "--permission-mode",
                "dontAsk",
            ],
        )
        self.assertEqual(command[9:11], ["--max-turns", "3"])

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

    def stub(
        self,
        models: Path | None = None,
        mode: str = "success",
        answer: dict | None = None,
        pass_mode: str = "success",
        scout_answers: dict | None = None,
    ):
        environment = {
            "STUB_RECORD": str(self.record),
            "STUB_MODELS": str(models) if models else "",
            "STUB_MODE": mode,
            "STUB_ANSWER": json.dumps(answer) if answer is not None else "",
            "STUB_PASS_MODE": pass_mode,
            "STUB_SCOUT_ANSWERS": json.dumps(scout_answers or {}),
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
                    "--unisolated",
                    "--no-cross-source",
                ]
            )
        self.assertEqual(code, 0, printed.getvalue())
        report = printed.getvalue()
        self.assertTrue(report.startswith("agent: success after 7 turns (exit 0)\n"), report)
        self.assertIn("gate: PASS (24 passed, 0 failed, 0 not run)", report)

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
        # With --no-cross-source only the engineer runs.
        self.assertFalse((self.out / CROSS_SOURCE_DIR).exists())
        self.assertFalse(Path(f"{self.record}.cross-source").exists())
        self.assertFalse((self.out / SCOUTS_DIR).exists())
        self.assertEqual(list(self.root.glob("record.json.scout.*")), [])

        # The stand-in reported 7 turns, 1 refused call, 41.3 s and US$0.0812.
        self.assertTrue(
            report.endswith(
                "usage by role (cost is Claude Code's own estimate, not a bill)\n"
                "\n"
                "role      outcome    turns  refused    time  cost (US$)\n"
                "engineer  success        7        1  41.3 s      0.0812\n"
                "run       gate PASS      7        1  41.3 s      0.0812\n"
                "\n"
                "role      model              input  cache write  cache read  output\n"
                "engineer  claude-sonnet-5-5     20       12,000      50,000   2,500\n"
            ),
            report,
        )
        ledger = json.loads((self.out / USAGE_FILE).read_text(encoding="utf-8"))
        self.assertEqual(ledger["gate"], "PASS")
        self.assertEqual(
            [(r["role"], r["outcome"], r["turns"]) for r in ledger["roles"]],
            [("engineer", "success", 7)],
        )

    def test_the_gate_builds_in_the_run_sandbox_and_sees_nothing_else_of_the_run(self):
        class Recorder:
            name, isolated = "recorder", False

            def __init__(self):
                self.seen = []

            def command(self, command, mounts):
                self.seen.append(mounts)
                return list(command)

            def describe(self):
                return "recorder"

        sandbox = Recorder()
        with self.stub(REFERENCE_MODELS):
            outcome = run(self.raw, DEMO_GOAL, DECLARATION, self.out, sandbox=sandbox)
        self.assertTrue(passed(outcome.results))
        out = self.out.resolve()
        engineer, first, second = sandbox.seen
        self.assertEqual(engineer.workspace, out / PROJECT_DIR)
        self.assertEqual(
            (first.workspace, second.workspace), (out / GATE_DIR, out / GATE_DIR / "repeat")
        )
        for build in (first, second):
            self.assertEqual(build.read_only[:2], (out / PROJECT_DIR, self.raw.resolve()))
            self.assertEqual(build.hidden[:3], (DECLARATION.resolve(), PLUGIN_DIR, out))

    def run_with_cross_source(
        self, answer: dict | None, pass_mode: str = "success", *flags: str
    ) -> str:
        with (
            self.stub(REFERENCE_MODELS, answer=answer, pass_mode=pass_mode),
            redirect_stdout(io.StringIO()) as printed,
        ):
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
                    "--unisolated",
                    *flags,
                ]
            )
        self.assertEqual(code, 0, printed.getvalue())
        return printed.getvalue()

    def test_cross_source_notes_reach_the_engineer(self):
        # The cross-source agent runs unless it is turned off.
        report = self.run_with_cross_source(ANSWER)
        self.assertTrue(
            report.startswith(
                "cross-source: success after 9 turns (exit 0); JOINS.md: 1 join, 1 caution\n"
                "agent: success after 7 turns (exit 0)\n"
            ),
            report,
        )
        passed_to = json.loads(Path(f"{self.record}.cross-source").read_text(encoding="utf-8"))
        folder = (self.out / CROSS_SOURCE_DIR).resolve()
        self.assertEqual(Path(passed_to["cwd"]), folder)
        plugin = (self.out / PLUGIN_COPY_DIR).resolve()
        self.assertEqual(passed_to["argv"], agent_command("claude", plugin, CROSS_SOURCE)[1:])
        self.assertNotIn("--allowedTools", passed_to["argv"])
        self.assertIn("--json-schema", passed_to["argv"])
        tables = ["billing.customers", "billing.invoices", "crm.accounts", "events.events"]
        profiles = [f"{SOURCE_PROFILES_DIR}/{t}.{kind}" for t in tables for kind in ("json", "txt")]
        overlap = [f"{SOURCE_PROFILES_DIR}/overlap.{kind}" for kind in ("json", "txt")]
        self.assertEqual(
            passed_to["files"], sorted([GOAL_FILE, SOURCE_PROFILES_DIR] + profiles + overlap)
        )
        self.assertEqual((folder / GOAL_FILE).read_bytes(), DEMO_GOAL.read_bytes())
        overlap_text = (folder / SOURCE_PROFILES_DIR / "overlap.txt").read_text(encoding="utf-8")
        self.assertTrue(overlap_text.startswith("overlap: 4 tables, 24 columns\n"), overlap_text)
        self.assertEqual(
            json.loads((self.out / CROSS_SOURCE_FILE).read_text(encoding="utf-8"))[
                "structured_output"
            ],
            ANSWER,
        )

        # The engineer finds the notes in its project, and nothing else new.
        engineer = self.recorded()
        self.assertIn("JOINS.md", engineer["files"])
        self.assertNotIn(f"{SOURCE_PROFILES_DIR}/overlap.txt", engineer["files"])
        notes = (self.out / PROJECT_DIR / "JOINS.md").read_text(encoding="utf-8")
        self.assertEqual(notes, render_notes(ANSWER))

        # 9 + 7 turns, 0 + 1 refused, 52.0 + 41.3 s, 0.1204 + 0.0812 US$.
        self.assertTrue(
            report.endswith(
                "role          outcome    turns  refused    time  cost (US$)\n"
                "cross-source  success        9        0  52.0 s      0.1204\n"
                "engineer      success        7        1  41.3 s      0.0812\n"
                "run           gate PASS     16        1  93.3 s      0.2016\n"
                "\n"
                "role          model              input  cache write  cache read  output\n"
                "cross-source  claude-sonnet-5-5     12       15,000      60,000   3,000\n"
                "engineer      claude-sonnet-5-5     20       12,000      50,000   2,500\n"
            ),
            report,
        )
        ledger = json.loads((self.out / USAGE_FILE).read_text(encoding="utf-8"))
        self.assertEqual(
            [(r["role"], r["turns"]) for r in ledger["roles"]],
            [("cross-source", 9), ("engineer", 7)],
        )

    def test_the_cross_source_flag_gives_the_default_run(self):
        default = self.run_with_cross_source(ANSWER)
        self.out = self.root / "flagged"
        flagged = self.run_with_cross_source(ANSWER, "success", "--cross-source")
        self.assertIn("cross-source: success after 9 turns", flagged)
        self.assertEqual(
            flagged.replace(str(self.out), "<out>"),
            default.replace(str(self.root / "run"), "<out>"),
        )

    def run_with_scouts(self, scout_answers: dict, *flags: str) -> str:
        with (
            self.stub(REFERENCE_MODELS, answer=ANSWER, scout_answers=scout_answers),
            redirect_stdout(io.StringIO()) as printed,
        ):
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
                    "--unisolated",
                    "--scouts",
                    *flags,
                ]
            )
        self.assertEqual(code, 0, printed.getvalue())
        return printed.getvalue()

    def scout_record(self, table: str) -> dict:
        return json.loads(Path(f"{self.record}.scout.{table}").read_text(encoding="utf-8"))

    def test_scout_notes_reach_the_engineer(self):
        crm = {
            "findings": [
                {
                    "columns": ["account_id", "billing_email"],
                    "issue": "Leading zeros and padded addresses.",
                    "evidence": "PROFILE.txt and SAMPLE.jsonl.",
                    "handling": "Keep the ID as text; trim the address.",
                }
            ],
            "questions": ["Are prospects accounts?", "Is the date day first?"],
        }
        empty = {"findings": [], "questions": []}
        report = self.run_with_scouts(
            {
                "billing.customers": CUSTOMERS_NOTES,
                "billing.invoices": empty,
                "crm.accounts": crm,
                "events.events": EVENTS_NOTES,
            },
            "--no-cross-source",
        )
        self.assertTrue(
            report.startswith(
                "scout billing.customers: success after 5 turns (exit 0); "
                "1 finding, 0 questions\n"
                "scout billing.invoices: success after 5 turns (exit 0); "
                "0 findings, 0 questions\n"
                "scout crm.accounts: success after 5 turns (exit 0); "
                "1 finding, 2 questions\n"
                "scout events.events: success after 5 turns (exit 0); "
                "2 findings, 1 question\n"
                "agent: success after 7 turns (exit 0)\n"
            ),
            report,
        )
        self.assertIn("gate: PASS", report)

        plugin = (self.out / PLUGIN_COPY_DIR).resolve()
        tables = ["billing.customers", "billing.invoices", "crm.accounts", "events.events"]
        records = {table: self.scout_record(table) for table in tables}
        for table in tables:
            with self.subTest(table):
                record = records[table]
                folder = (self.out / SCOUTS_DIR / table).resolve()
                self.assertEqual(Path(record["cwd"]), folder)
                self.assertEqual(record["argv"], agent_command("claude", plugin, SCOUT)[1:])
                argv = record["argv"]
                self.assertEqual(argv[argv.index("--agent") + 1], "tenx:scout")
                self.assertEqual(argv[argv.index("--max-turns") + 1], "8")
                self.assertNotIn("--allowedTools", record["argv"])
                self.assertIn("--json-schema", record["argv"])
                self.assertEqual(record["files"], [GOAL_FILE, "PROFILE.txt", "SAMPLE.jsonl"])
                reported = json.loads(
                    (self.out / SCOUTS_DIR / f"{table}.json").read_text(encoding="utf-8")
                )
                self.assertEqual(reported["num_turns"], 5)
        # Up to four scouts run at once, so all four overlap.
        self.assertLess(
            max(record["started"] for record in records.values()),
            min(record["ended"] for record in records.values()),
        )

        engineer = self.recorded()
        self.assertIn("TABLE_NOTES.md", engineer["files"])
        self.assertNotIn("JOINS.md", engineer["files"])
        self.assertEqual(
            (self.out / PROJECT_DIR / "TABLE_NOTES.md").read_text(encoding="utf-8"),
            render_table_notes(
                [
                    ("billing.customers", CUSTOMERS_NOTES),
                    ("billing.invoices", empty),
                    ("crm.accounts", crm),
                    ("events.events", EVENTS_NOTES),
                ]
            ),
        )

        # 4 x 5 + 7 = 27 turns, 0 + 1 refused, 4 x 20.0 + 41.3 = 121.3 s and
        # 4 x 0.0030 + 0.0812 = 0.0932 US$.
        self.assertTrue(
            report.endswith(
                "role                     outcome    turns  refused     time  cost (US$)\n"
                "scout billing.customers  success        5        0   20.0 s      0.0030\n"
                "scout billing.invoices   success        5        0   20.0 s      0.0030\n"
                "scout crm.accounts       success        5        0   20.0 s      0.0030\n"
                "scout events.events      success        5        0   20.0 s      0.0030\n"
                "engineer                 success        7        1   41.3 s      0.0812\n"
                "run                      gate PASS     27        1  121.3 s      0.0932\n"
                "\n"
                "role                     model              input  cache write  cache read  output\n"
                "scout billing.customers  claude-haiku-5-5      10        3,000       9,000     800\n"
                "scout billing.invoices   claude-haiku-5-5      10        3,000       9,000     800\n"
                "scout crm.accounts       claude-haiku-5-5      10        3,000       9,000     800\n"
                "scout events.events      claude-haiku-5-5      10        3,000       9,000     800\n"
                "engineer                 claude-sonnet-5-5     20       12,000      50,000   2,500\n"
            ),
            report,
        )
        ledger = json.loads((self.out / USAGE_FILE).read_text(encoding="utf-8"))
        self.assertEqual(
            [r["role"] for r in ledger["roles"]],
            [f"scout {table}" for table in tables] + ["engineer"],
        )

    def test_scout_answers_that_fail_their_check_are_left_out(self):
        wrong = json.loads(json.dumps(CUSTOMERS_NOTES))
        wrong["findings"][0]["columns"] = ["email", "status"]
        report = self.run_with_scouts(
            {"billing.invoices": "crash", "crm.accounts": wrong, "events.events": EVENTS_NOTES},
            "--no-cross-source",
        )
        self.assertTrue(
            report.startswith(
                "scout billing.customers: success after 5 turns (exit 0); no notes for the "
                "engineer: the scout ended success without an answer\n"
                "scout billing.invoices: exited 1 without a report; no notes for the engineer: "
                "the scout gave no report\n"
                "scout crm.accounts: success after 5 turns (exit 0); no notes for the engineer: "
                "finding 1 names 'email', which is not a column of the table\n"
                "scout events.events: success after 5 turns (exit 0); 2 findings, 1 question\n"
            ),
            report,
        )
        self.assertEqual(
            (self.out / PROJECT_DIR / "TABLE_NOTES.md").read_text(encoding="utf-8"),
            render_table_notes(
                [
                    ("billing.customers", None),
                    ("billing.invoices", None),
                    ("crm.accounts", None),
                    ("events.events", EVENTS_NOTES),
                ]
            ),
        )
        # The run row is not added up when a role reported nothing.
        self.assertIn(
            "scout billing.invoices   no report      -        -       -           -\n", report
        )
        self.assertIn(
            "run                      gate PASS      -        -       -           -\n", report
        )

    def test_no_notes_file_when_no_scout_answer_passes(self):
        report = self.run_with_scouts({"events.events": "crash"}, "--no-cross-source")
        self.assertIn("scout events.events: exited 1 without a report", report)
        self.assertNotIn("TABLE_NOTES.md", self.recorded()["files"])
        self.assertFalse((self.out / PROJECT_DIR / "TABLE_NOTES.md").exists())

    def test_scouts_run_before_the_cross_source_agent(self):
        report = self.run_with_scouts({"events.events": EVENTS_NOTES}, "--cross-source")
        self.assertIn(
            "scout events.events: success after 5 turns (exit 0); 2 findings, 1 question\n"
            "cross-source: success after 9 turns (exit 0); JOINS.md: 1 join, 1 caution\n"
            "agent: success after 7 turns (exit 0)\n",
            report,
        )
        engineer = self.recorded()
        self.assertIn("TABLE_NOTES.md", engineer["files"])
        self.assertIn("JOINS.md", engineer["files"])
        # The cross-source agent is not given the scouts' notes.
        passed_to = json.loads(Path(f"{self.record}.cross-source").read_text(encoding="utf-8"))
        self.assertNotIn("TABLE_NOTES.md", passed_to["files"])
        self.assertLess(
            self.scout_record("events.events")["ended"],
            (self.out / CROSS_SOURCE_FILE).stat().st_mtime,
        )
        ledger = json.loads((self.out / USAGE_FILE).read_text(encoding="utf-8"))
        self.assertEqual(
            [r["role"] for r in ledger["roles"]],
            [
                "scout billing.customers",
                "scout billing.invoices",
                "scout crm.accounts",
                "scout events.events",
                "cross-source",
                "engineer",
            ],
        )

    def test_an_answer_naming_an_unknown_column_is_not_passed_on(self):
        wrong = json.loads(json.dumps(ANSWER))
        wrong["joins"][0]["right"] = "crm.accounts.email"
        report = self.run_with_cross_source(wrong)
        self.assertTrue(
            report.startswith(
                "cross-source: success after 9 turns (exit 0); no notes for the engineer: "
                "join 1 names 'crm.accounts.email', which is not a column in the profiles\n"
            ),
            report,
        )
        self.assertNotIn("JOINS.md", self.recorded()["files"])
        self.assertFalse((self.out / PROJECT_DIR / "JOINS.md").exists())
        self.assertIn("gate: PASS", report)

    def test_a_pass_with_no_answer_or_no_report_is_not_passed_on(self):
        report = self.run_with_cross_source(None)
        self.assertIn(
            "cross-source: success after 9 turns (exit 0); no notes for the engineer: "
            "the cross-source pass ended success without an answer\n",
            report,
        )
        self.assertNotIn("JOINS.md", self.recorded()["files"])
        shutil.rmtree(self.out)
        report = self.run_with_cross_source(ANSWER, pass_mode="crash")
        self.assertIn(
            "cross-source: exited 1 without a report; no notes for the engineer: "
            "the cross-source pass gave no report\n",
            report,
        )
        self.assertNotIn("JOINS.md", self.recorded()["files"])
        # The run row is not added up when a role reported nothing.
        self.assertIn("run           gate PASS      -        -       -           -\n", report)

    def test_models_that_keep_test_mode_invoices_fail_on_those_invoices(self):
        with (self.raw / "billing" / "invoices.csv").open(newline="", encoding="utf-8") as handle:
            test_mode = [row for row in csv.DictReader(handle) if row["livemode"] == "false"]
        self.assertGreater(len(test_mode), 0)
        with self.stub(self.broken):
            outcome = run(
                self.raw, DEMO_GOAL, DECLARATION, self.out, timeout=60, sandbox=UNISOLATED
            )
        agent, results = outcome.engineer, outcome.results
        self.assertEqual(agent.report["subtype"], "success")
        self.assertFalse(passed(results))
        failed = {(result.check, result.subject) for result in results if result.status == FAIL}
        self.assertEqual(
            failed, {("rows", "stg_billing__invoices"), ("total", "stg_billing__invoices")}
        )
        rows = next(r for r in results if r.status == FAIL and r.check == "rows")
        self.assertTrue(rows.detail.endswith(f": {len(test_mode)} more than expected"), rows.detail)
        # The agent itself succeeded; the run's verdict is the gate's.
        ledger = json.loads((self.out / USAGE_FILE).read_text(encoding="utf-8"))
        self.assertEqual((ledger["gate"], ledger["roles"][0]["outcome"]), ("FAIL", "success"))

    def test_an_agent_that_fails_to_run_leaves_nothing_to_pass(self):
        with self.stub(mode="crash"):
            outcome = run(
                self.raw, DEMO_GOAL, DECLARATION, self.out, timeout=60, sandbox=UNISOLATED
            )
        agent, results = outcome.engineer, outcome.results
        self.assertEqual(agent.describe(), "exited 1 without a report: Error: not signed in")
        self.assertFalse(passed(results))
        ledger = json.loads((self.out / USAGE_FILE).read_text(encoding="utf-8"))
        self.assertEqual(ledger["gate"], "FAIL")
        self.assertEqual(
            (ledger["roles"][0]["outcome"], ledger["roles"][0]["turns"]), ("no report", None)
        )
        missing = [r.subject for r in results if r.check == "build" and r.status == FAIL]
        self.assertIn("stg_billing__invoices", missing)

    def test_an_agent_that_does_not_finish_is_stopped(self):
        with self.stub(REFERENCE_MODELS, mode="hang"):
            outcome = run(self.raw, DEMO_GOAL, DECLARATION, self.out, timeout=5, sandbox=UNISOLATED)
        agent, results = outcome.engineer, outcome.results
        self.assertEqual(agent.describe(), "stopped: did not finish within 5 seconds")
        self.assertFalse((self.out / AGENT_FILE).exists())
        # What the agent wrote before it was stopped is still checked.
        self.assertTrue(passed(results))
        ledger = json.loads((self.out / USAGE_FILE).read_text(encoding="utf-8"))
        self.assertEqual(ledger["gate"], "PASS")
        self.assertEqual(
            (ledger["roles"][0]["outcome"], ledger["roles"][0]["cost_usd"]), ("timed out", None)
        )

    def test_a_missing_claude_command_is_reported(self):
        with (
            mock.patch.dict(os.environ, {"PATH": str(self.root)}),
            self.assertRaisesRegex(EngineerError, "claude was not found"),
        ):
            run(self.raw, DEMO_GOAL, DECLARATION, self.out, sandbox=UNISOLATED)

    def test_refuses_an_output_folder_that_is_not_empty(self):
        write(self.out / "project" / "models" / "old.sql", "select 1")
        with self.assertRaisesRegex(EngineerError, "must be empty"):
            run(self.raw, DEMO_GOAL, DECLARATION, self.out, sandbox=UNISOLATED)

    def test_an_unusable_declaration_stops_the_run_before_anything_is_written(self):
        bad = write(self.root / "bad.json", '{"models": [{"name": "m", "colour": "red"}]}')
        with self.assertRaises(GateError):
            run(self.raw, DEMO_GOAL, bad, self.out, sandbox=UNISOLATED)
        self.assertFalse(self.out.exists())


# A stand-in agent that starts a child process sharing its output, as a dbt
# build started by the agent would, and records the child's process id.
LEAVES_A_CHILD = """
import json, os, subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
open(os.environ["CHILD_PID_FILE"], "w").write(str(child.pid))
if os.environ["AGENT_MODE"] == "hang":
    time.sleep(120)
print(json.dumps({"type": "result", "subtype": "success", "num_turns": 1}))
"""


def _running(pid: int) -> bool:
    """Whether a process is alive (a zombie awaiting its parent is not)."""
    try:
        state = Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0]
    except FileNotFoundError:
        return False
    return state != "Z"


@unittest.skipUnless(Path("/proc").is_dir(), "needs /proc to see processes")
class RunAgentCleanupTest(unittest.TestCase):
    """The runner never waits on, or leaves behind, a process the agent started."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.agent = self.root / "agent.py"
        self.agent.write_text(LEAVES_A_CHILD, encoding="utf-8")
        self.child_pid_file = self.root / "child.pid"
        self.report = self.root / "out" / "agent.json"

    def run_stand_in(self, mode: str, timeout: float):
        environment = {"CHILD_PID_FILE": str(self.child_pid_file), "AGENT_MODE": mode}
        with (
            mock.patch.dict(os.environ, environment),
            mock.patch("tenx.engineer.STOP_GRACE_SECONDS", 2),
        ):
            started = time.monotonic()
            agent = run_agent(self.root, [sys.executable, str(self.agent)], self.report, timeout)
            elapsed = time.monotonic() - started
        child = int(self.child_pid_file.read_text())
        self.addCleanup(lambda: _running(child) and os.kill(child, 9))
        return agent, elapsed, child

    def test_an_agent_that_finishes_is_not_held_by_its_child(self):
        agent, elapsed, child = self.run_stand_in("finish", timeout=60)
        self.assertLess(elapsed, 30)
        self.assertEqual(agent.report["subtype"], "success")
        self.assertIsNone(agent.timed_out_after)
        self.assertEqual(json.loads(self.report.read_text())["num_turns"], 1)
        time.sleep(0.5)
        self.assertFalse(_running(child), "the child the agent left running was not stopped")

    def test_a_timed_out_agent_and_its_child_are_stopped(self):
        agent, elapsed, child = self.run_stand_in("hang", timeout=3)
        # The timeout plus the grace period, with room for a slow machine.
        self.assertLess(elapsed, 3 + 2 + 10)
        self.assertEqual(agent.timed_out_after, 3)
        self.assertIsNone(agent.report)
        time.sleep(0.5)
        self.assertFalse(_running(child), "the agent's child is still running")

    def test_a_stopped_agent_leaves_no_report_or_scratch_files(self):
        self.run_stand_in("hang", timeout=3)
        self.assertEqual(sorted(path.name for path in self.report.parent.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
