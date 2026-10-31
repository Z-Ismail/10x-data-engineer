"""The byte-for-byte comparison of an old-runner run with a graph run.

The map is tested on small handmade run folders, each written as the
runner would lay it out, with one thing changed per test. Then both runners
run on a small demo dataset with the stand-in claude of tests/equivalence.py,
in the sandbox, skipped where bubblewrap cannot run unless
TENX_REQUIRE_SANDBOX is set.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from equivalence import DECLARATION, ROOT, SCENARIOS, run_pair

from demo.generate import generate
from tenx.equivalence import compare_runs, latest_attempts
from tenx.sandbox import SandboxError, default_sandbox

REPORT = "gate: PASS (2 passed, 0 failed, 0 not run)\n"

OLD = {
    "plugin/agents/engineer.md": "engineer prompt\n",
    "project/GOAL.md": "Build it.\n",
    "project/JOINS.md": "joins\n",
    "project/source_profiles/a.txt": "profile a\n",
    "project/models/a.sql": "select 1\n",
    "cross_source/GOAL.md": "Build it.\n",
    "cross_source/source_profiles/a.txt": "profile a\n",
    "cross_source/source_profiles/overlap.txt": "overlap\n",
    "cross_source/source_profiles/overlap.json": "{}\n",
    "agent.json": '{"num_turns": 7}\n',
    "cross_source.json": '{"num_turns": 9}\n',
    "usage.json": '{"gate": "PASS"}\n',
    "gate/build.duckdb": "old database",
    "gate/target/run_results.json": "old results",
}

GRAPH = {
    "plugin/agents/engineer.md": "engineer prompt\n",
    "engineer/attempt-1/work/GOAL.md": "Build it.\n",
    "engineer/attempt-1/work/JOINS.md": "joins\n",
    "engineer/attempt-1/work/source_profiles/a.txt": "profile a\n",
    "engineer/attempt-1/work/models/a.sql": "select 1\n",
    "engineer/attempt-1/agent.json": '{"num_turns": 7}\n',
    "engineer/attempt-1/manifest.json": "{}",
    "cross-source/attempt-1/work/GOAL.md": "Build it.\n",
    "cross-source/attempt-1/work/source_profiles/a.txt": "profile a\n",
    "cross-source/attempt-1/work/source_profiles/overlap.txt": "overlap\n",
    "cross-source/attempt-1/work/source_profiles/overlap.json": "{}\n",
    "cross-source/attempt-1/JOINS.md": "joins\n",
    "cross-source/attempt-1/agent.json": '{"num_turns": 9}\n',
    "cross-source/attempt-1/manifest.json": "{}",
    "profile/a.txt": "profile a\n",
    "overlap/overlap.txt": "overlap\n",
    "overlap/overlap.json": "{}\n",
    "gate/attempt-1/report.txt": REPORT,
    "gate/attempt-1/manifest.json": "{}",
    # Databases and dbt artifacts differ between any two builds.
    "gate/attempt-1/work/build.duckdb": "graph database",
    "gate/attempt-1/work/target/run_results.json": "graph results",
    "usage.json": '{"gate": "PASS"}\n',
    "budget.json": "{}",
    "checkpoints.sqlite": "state",
}


def write(root: Path, files: dict) -> Path:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


class CompareRunsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def compare(self, old=None, graph=None, report=REPORT):
        # Each comparison gets folders of its own.
        self.calls = getattr(self, "calls", 0) + 1
        self.graph = self.root / f"graph-{self.calls}"
        old = write(self.root / f"old-{self.calls}", OLD if old is None else old)
        graph = write(self.graph, GRAPH if graph is None else graph)
        return compare_runs(old, graph, report)

    def test_identical_runs(self):
        found = self.compare()
        self.assertTrue(found.identical, found.differences)
        # 4 project, 4 cross-source, 1 add-on, 1 profile, 2 overlap and 5 single files.
        self.assertEqual(len(found.compared), 18)
        self.assertIn("gate/attempt-1/work/ (file names)", found.compared)
        self.assertEqual(
            found.graph_only,
            (
                "budget.json",
                "checkpoints.sqlite",
                "cross-source/attempt-1/manifest.json",
                "engineer/attempt-1/manifest.json",
                "gate/attempt-1/manifest.json",
            ),
        )
        self.assertEqual(found.render(), "identical: 18 files compared, 5 graph only\n")

    def test_a_changed_byte_names_the_line(self):
        graph = {**GRAPH, "engineer/attempt-1/work/models/a.sql": "select 2\n"}
        found = self.compare(graph=graph)
        self.assertEqual(
            found.differences,
            (
                (
                    "differs: project/models/a.sql and engineer/attempt-1/work/models/a.sql, "
                    "line 1: 'select 1' against 'select 2'"
                ),
            ),
        )
        self.assertEqual(
            found.render().splitlines()[0], "1 difference: 18 files compared, 5 graph only"
        )

    def test_extra_lines_and_line_endings(self):
        graph = {
            **GRAPH,
            "usage.json": '{"gate": "PASS"}\n\n',
            "cross-source/attempt-1/JOINS.md": "joins\r\n",
        }
        self.assertEqual(
            self.compare(graph=graph).differences,
            (
                (
                    "differs: project/JOINS.md and cross-source/attempt-1/JOINS.md, "
                    "line endings differ"
                ),
                "differs: usage.json and usage.json, 1 lines against 2",
            ),
        )

    def test_a_file_in_one_arm_only(self):
        old = {**OLD, "project/models/b.sql": "select 3\n"}
        found = self.compare(old=old)
        self.assertEqual(
            found.differences,
            (
                "only in the old run: project/models/b.sql (no engineer/attempt-1/work/models/b.sql)",
            ),
        )
        graph = {key: value for key, value in GRAPH.items() if key != "usage.json"}
        self.assertEqual(
            self.compare(graph=graph).differences,
            ("only in the old run: usage.json (no usage.json)",),
        )

    def test_unmapped_files_are_differences(self):
        old = {**OLD, "notes.txt": "left behind\n"}
        graph = {**GRAPH, "engineer/attempt-1/agent.json.partial": "{"}
        self.assertEqual(
            self.compare(old=old, graph=graph).differences,
            (
                "only in the old run, not in the map: notes.txt",
                "only in the graph run, not in the map: engineer/attempt-1/agent.json.partial",
            ),
        )

    def test_the_gate_report_and_working_files(self):
        found = self.compare(report="gate: FAIL (1 passed, 1 failed, 0 not run)\n")
        self.assertEqual(
            found.differences,
            (
                (
                    "differs: (gate report as printed) and gate/attempt-1/report.txt, line 1: "
                    "'gate: FAIL (1 passed, 1 failed, 0 not run)' against "
                    "'gate: PASS (2 passed, 0 failed, 0 not run)'"
                ),
            ),
        )
        old = {**OLD, "gate/repeat/repeat.duckdb": "second build"}
        self.assertEqual(
            self.compare(old=old).differences, ("only in the old run: gate/repeat/repeat.duckdb",)
        )
        graph = {key: value for key, value in GRAPH.items() if key != "gate/attempt-1/report.txt"}
        self.assertEqual(
            self.compare(graph=graph).differences,
            ("only in the old run: (gate report as printed) (no gate/attempt-1/report.txt)",),
        )

    def test_a_resumed_run_is_compared_on_its_last_attempt(self):
        graph = {
            key.replace("engineer/attempt-1", "engineer/attempt-2"): value
            for key, value in GRAPH.items()
        }
        graph["engineer/attempt-1/manifest.json"] = "{}"
        graph["engineer/attempt-1/work/GOAL.md"] = "Build it.\n"
        found = self.compare(graph=graph)
        self.assertTrue(found.identical, found.differences)
        self.assertIn("engineer/attempt-1/work/GOAL.md", found.graph_only)
        self.assertIn("engineer/attempt-2/manifest.json", found.graph_only)
        self.assertEqual(
            latest_attempts(self.graph),
            {"cross-source": "attempt-1", "engineer": "attempt-2", "gate": "attempt-1"},
        )

    def test_a_run_without_the_cross_source_agent(self):
        old = {k: v for k, v in OLD.items() if "cross_source" not in k and "JOINS" not in k}
        graph = {
            k: v
            for k, v in GRAPH.items()
            if not k.startswith(("cross-source", "overlap")) and "JOINS" not in k
        }
        found = self.compare(old=old, graph=graph)
        self.assertTrue(found.identical, found.differences)
        # The cross-source agent's folder present in one arm only is a difference.
        graph["cross-source/attempt-1/work/GOAL.md"] = "Build it.\n"
        self.assertEqual(
            self.compare(old=old, graph=graph).differences,
            (
                (
                    "only in the graph run: cross-source/attempt-1/work/GOAL.md "
                    "(no cross_source/GOAL.md)"
                ),
            ),
        )


def _sandbox_problem() -> str:
    try:
        default_sandbox()
    except SandboxError as error:
        return str(error)
    return ""


SANDBOX_PROBLEM = _sandbox_problem()


@unittest.skipIf(
    SANDBOX_PROBLEM and not os.environ.get("TENX_REQUIRE_SANDBOX"), "no working sandbox"
)
class ScenarioTest(unittest.TestCase):
    """Both runners on one small demo dataset, in the sandbox, with the stand-in."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.raw = cls.root / "data"
        generate(11, 20, cls.raw, cls.root / "answers")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def pair(self, name: str, unisolated: bool = False):
        folder = self.root / f"{name.replace(' ', '-')}-{'unisolated' if unisolated else 'sandbox'}"
        found = run_pair(self.raw, folder, SCENARIOS[name], unisolated)
        self.assertTrue(found.comparison.identical, found.comparison.render())
        return found

    def record(self, report: Path) -> dict:
        return json.loads(json.loads(report.read_text())["result"])

    def usage(self, found) -> dict:
        return json.loads((found.graph / "usage.json").read_text())

    def test_a_passing_run(self):
        found = self.pair("pass")
        self.assertEqual(found.graph_exits, [0])
        # Project: goal, notes, 2 dbt files, 8 profile files, 8 model files (20).
        # Cross-source folder: goal, 8 profile files, 2 overlap files (11). profile/ (8),
        # overlap/ (2), the add-on (4), 5 single files and the gate's file names (1).
        self.assertEqual(len(found.comparison.compared), 20 + 11 + 8 + 2 + 4 + 5 + 1)
        usage = self.usage(found)
        self.assertEqual(usage["gate"], "PASS")
        self.assertEqual([role["role"] for role in usage["roles"]], ["cross-source", "engineer"])
        self.assertEqual(usage["sandbox"], "bubblewrap")
        engineer = self.record(found.old / "agent.json")
        cross_source = self.record(found.old / "cross_source.json")
        raw = str(self.raw.resolve())
        # The sandbox shows the raw files to the engineer only, and the add-on to both.
        for record, sees_raw in ((engineer, True), (cross_source, False)):
            visible = record["visible"]
            self.assertEqual(visible[raw], sees_raw)
            self.assertFalse(visible[str(DECLARATION)])
            self.assertFalse(visible[str(Path(ROOT) / "README.md")])
            plugin = [path for path in visible if path.endswith("agents/engineer.md")]
            self.assertEqual([visible[path] for path in plugin], [True])
            self.assertEqual(record["environment"]["CLAUDE_CODE_TOOL_MEMORY_LIMIT"], "none")
            self.assertIsNone(record["environment"]["DBT_PROFILES_DIR"])
        self.assertIn("JOINS.md", engineer["seen"])
        self.assertNotIn("models/staging/stg_billing__invoices.sql", engineer["seen"])

    def test_the_probe_sees_what_an_unisolated_run_shows(self):
        # Positive control: without the sandbox the declaration is visible.
        found = self.pair("pass", unisolated=True)
        record = self.record(found.old / "cross_source.json")
        self.assertTrue(record["visible"][str(DECLARATION)])
        self.assertTrue(record["visible"][str(self.raw.resolve())])
        self.assertEqual(self.usage(found)["sandbox"], "none")

    def test_a_run_that_fails_the_gate(self):
        found = self.pair("fail")
        self.assertEqual(found.graph_exits, [1])
        self.assertEqual(self.usage(found)["gate"], "FAIL")
        report = (found.graph / "gate" / "attempt-1" / "report.txt").read_text()
        failed = [line.split()[1:3] for line in report.splitlines() if line.startswith("FAIL")]
        self.assertEqual(
            failed, [["rows", "stg_billing__invoices"], ["total", "stg_billing__invoices"]]
        )

    def test_an_answer_that_fails_its_check(self):
        found = self.pair("no notes")
        self.assertEqual(found.graph_exits, [0])
        self.assertFalse((found.old / "project" / "JOINS.md").exists())
        self.assertNotIn("JOINS.md", self.record(found.old / "agent.json")["seen"])

    def test_an_engineer_with_no_report(self):
        found = self.pair("no report")
        self.assertEqual(found.graph_exits, [1])
        usage = self.usage(found)
        self.assertEqual(usage["gate"], "FAIL")
        self.assertEqual(
            [(role["role"], role["outcome"]) for role in usage["roles"]],
            [("cross-source", "success"), ("engineer", "no report")],
        )

    def test_the_engineer_alone(self):
        found = self.pair("engineer alone")
        self.assertEqual(found.graph_exits, [0])
        self.assertEqual([role["role"] for role in self.usage(found)["roles"]], ["engineer"])
        self.assertFalse((found.old / "cross_source").exists())
        self.assertFalse((found.graph / "cross-source").exists())

    def test_a_graph_killed_in_the_engineer_and_resumed(self):
        # Against an old run that was never interrupted.
        found = self.pair("resumed")
        self.assertEqual(found.graph_exits, [-9, 0])
        earlier = [name for name in found.comparison.graph_only if "engineer/attempt-1/" in name]
        self.assertIn("engineer/attempt-1/work/HANGING", earlier)
        spending = json.loads((found.graph / "budget.json").read_text())
        # 0.1204 measured, the killed call's whole 0.75, then 0.0812 measured.
        self.assertEqual(spending["spent_usd"], "0.9516")


if __name__ == "__main__":
    unittest.main()
