"""The byte-for-byte comparison of an old-runner run with a graph run.

The map is tested on small handmade run folders, each written as the
runner would lay it out, with one thing changed per test.
"""

import tempfile
import unittest
from pathlib import Path

from tenx.equivalence import compare_runs, latest_attempts

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


if __name__ == "__main__":
    unittest.main()
