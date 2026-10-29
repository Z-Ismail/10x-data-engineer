"""The graph state in a real graph: parallel writes, and a crash then a resume."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from langgraph.errors import InvalidUpdateError
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from tenx.domain import AgentFinding, ArtefactRef, CostRecord
from tenx.graph.checkpoints import open_checkpoints
from tenx.graph.state import GraphState, StateConflict, plain

ROOT = Path(__file__).resolve().parent.parent

# A run in three steps: one profile, three findings at once, then the engineer,
# which can be made to crash. Each step notes in a log that it ran. With
# "leak", the findings go into the state as objects rather than as JSON.
GRAPH = textwrap.dedent(
    """
    import json, os, sys
    from pathlib import Path
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import Send
    from tenx.domain import AgentFinding, ArtefactRef, CostRecord
    from tenx.graph.checkpoints import open_checkpoints
    from tenx.graph.state import GraphState, plain

    database, phase, log, work = sys.argv[1:5]
    leak = sys.argv[5:] == ["leak"]

    def note(step):
        with open(log, "a") as handle:
            handle.write(step + "\\n")

    def profile(state):
        note("profile")
        path = Path(work) / "profiles.txt"
        path.write_text("rows 200\\n")
        ref = ArtefactRef.of("profiles", path)
        return plain({"artefacts": {"profiles": ref.dump()}, "status": "running"})

    def fan_out(state):
        return [Send("find", {"number": number}) for number in range(3)]

    def find(task):
        note(f"find {task['number']}")
        finding = AgentFinding(
            agent="cross-source",
            claim=f"join {task['number']}",
            kind="inference",
            confidence="high",
        )
        return {"findings": [finding]} if leak else plain({"findings": [finding.dump()]})

    def engineer(state):
        note("engineer")
        if phase == "crash":
            os._exit(9)
        cost = CostRecord(
            run_id="run-1", node="engineer", role="engineer", attempt=1,
            backend="claude-code", outcome="success", cost_basis="estimate",
            estimated_usd="0.1116", reserved_usd="0.75", charged_usd="0.1116",
            overshoot_usd="0",
        )
        return plain(
            {"costs": [cost.dump()], "attempts": {"engineer": 1}, "status": "passed"}
        )

    graph = StateGraph(GraphState)
    graph.add_node("profile", profile)
    graph.add_node("find", find)
    graph.add_node("engineer", engineer)
    graph.add_edge(START, "profile")
    graph.add_conditional_edges("profile", fan_out, ["find"])
    graph.add_edge("find", "engineer")
    graph.add_edge("engineer", END)

    with open_checkpoints(Path(database)) as saver:
        app = graph.compile(checkpointer=saver)
        config = {"configurable": {"thread_id": "run-1"}}
        start = None if phase == "resume" else {"run_id": "run-1"}
        state = app.invoke(start, config, durability="sync")
    types = [type(item).__name__ for item in state["findings"]]
    print(json.dumps({"types": types, "state": None if leak else state}))
    """
)


class ParallelWritesTest(unittest.TestCase):
    def run_graph(self, write):
        graph = StateGraph(GraphState)
        graph.add_node("write", write)
        graph.add_conditional_edges(
            START, lambda state: [Send("write", {"n": n}) for n in range(3)], ["write"]
        )
        graph.add_edge("write", END)
        return graph.compile().invoke({"run_id": "run-1"})

    def test_findings_and_artefacts_from_parallel_steps_are_joined(self):
        def write(task):
            n = task["n"]
            ref = ArtefactRef(
                name=f"notes-{n}", path=f"/r/{n}", kind="file", sha256=str(n) * 64, attempt=1
            )
            finding = AgentFinding(agent=f"scout-{n}", claim="c", kind="fact", confidence="low")
            return plain({"artefacts": {ref.name: ref.dump()}, "findings": [finding.dump()]})

        state = self.run_graph(write)
        self.assertEqual(sorted(state["artefacts"]), ["notes-0", "notes-1", "notes-2"])
        agents = sorted(AgentFinding.load(item).agent for item in state["findings"])
        self.assertEqual(agents, ["scout-0", "scout-1", "scout-2"])

    def test_parallel_steps_giving_one_artefact_different_files_fail(self):
        def write(task):
            ref = ArtefactRef(
                name="project", path="/r/p", kind="folder", sha256=str(task["n"]) * 64, attempt=1
            )
            return {"artefacts": {"project": ref.dump()}}

        with self.assertRaises(StateConflict):
            self.run_graph(write)

    def test_parallel_steps_writing_the_status_fail(self):
        with self.assertRaises(InvalidUpdateError):
            self.run_graph(lambda task: {"status": "running"})


class ResumeTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.script = self.folder / "graph.py"
        self.script.write_text(GRAPH)
        (self.folder / "work").mkdir()
        self.addCleanup(shutil.rmtree, self.folder)

    def run_graph(self, name: str, phase: str, *extra: str) -> subprocess.CompletedProcess:
        env = {**os.environ, "PYTHONPATH": str(ROOT)}
        env.pop("LANGGRAPH_STRICT_MSGPACK", None)
        return subprocess.run(
            [
                sys.executable,
                str(self.script),
                str(self.folder / f"{name}.sqlite"),
                phase,
                str(self.folder / f"{name}.log"),
                str(self.folder / "work"),
                *extra,
            ],
            capture_output=True,
            text=True,
            env=env,
            check=False,
            timeout=120,
        )

    def log_lines(self, name: str) -> list[str]:
        return (self.folder / f"{name}.log").read_text().splitlines()

    def finished(self, done: subprocess.CompletedProcess) -> dict:
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_a_crashed_run_resumes_where_it_stopped_with_the_same_state(self):
        whole = self.finished(self.run_graph("whole", "fresh"))
        crashed = self.run_graph("crashed", "crash")
        self.assertEqual(crashed.returncode, 9, crashed.stderr)
        before = self.log_lines("crashed")
        resumed = self.finished(self.run_graph("crashed", "resume"))
        # The profile and the three findings ran once; only the engineer ran again.
        self.assertEqual(sorted(before), sorted(self.log_lines("whole")))
        self.assertEqual(self.log_lines("crashed")[len(before) :], ["engineer"])

        state = resumed["state"]
        for run in (whole["state"], state):
            run["findings"].sort(key=lambda item: item["claim"])
        self.assertEqual(state, whole["state"])
        self.assertIs(plain(state), state)
        self.assertEqual(state["status"], "passed")
        self.assertEqual(state["attempts"], {"engineer": 1})
        self.assertTrue(ArtefactRef.load(state["artefacts"]["profiles"]).unchanged())
        claims = [AgentFinding.load(item).claim for item in state["findings"]]
        self.assertEqual(claims, ["join 0", "join 1", "join 2"])
        self.assertEqual(CostRecord.load(state["costs"][0]).dump(), state["costs"][0])

        # A third process reads the same state from the file.
        with open_checkpoints(self.folder / "crashed.sqlite") as saver:
            saved = saver.get({"configurable": {"thread_id": "run-1"}})
        values = saved["channel_values"]
        self.assertEqual(values["status"], "passed")
        self.assertEqual(
            sorted(item["claim"] for item in values["findings"]), ["join 0", "join 1", "join 2"]
        )

    def test_objects_left_in_the_state_change_form_on_resume(self):
        # Why the state holds only JSON: objects come back from a checkpoint
        # as plain dictionaries, so a step reading them works only until a resume.
        fresh = self.finished(self.run_graph("fresh", "fresh", "leak"))
        self.assertEqual(fresh["types"], ["AgentFinding"] * 3)
        self.assertEqual(self.run_graph("leaked", "crash", "leak").returncode, 9)
        resumed = self.run_graph("leaked", "resume", "leak")
        self.assertEqual(self.finished(resumed)["types"], ["dict"] * 3)
        self.assertIn("Blocked deserialization", resumed.stderr)
        # plain() refuses the object before it is written.
        finding = AgentFinding(agent="a", claim="c", kind="fact", confidence="low")
        with self.assertRaises(TypeError):
            plain({"findings": [finding]})


if __name__ == "__main__":
    unittest.main()
