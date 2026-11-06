"""The architect step of the build graph, with stand-in ports."""

import unittest

from graph_fakes import Fakes, GraphCase

from tenx.domain import AgentFinding
from tenx.graph.build import steps_for


class ArchitectCase(GraphCase):
    def run_graph(self, fakes, **changes):
        return self.invoke(fakes.toolkit(self.out), self.request(architect=True, **changes))

    def findings(self, state) -> list[str]:
        return [
            AgentFinding.load(item).claim
            for item in state.get("findings") or []
            if item["agent"] == "architect"
        ]


class ArchitectPlanTest(ArchitectCase, unittest.TestCase):
    def test_the_architect_runs_only_when_asked_after_the_cross_source_agent(self):
        self.assertNotIn("architect", steps_for(self.request()))
        self.assertEqual(
            steps_for(self.request(architect=True)),
            ("profile", "overlap", "cross-source", "architect", "engineer", "gate", "report"),
        )
        self.assertEqual(
            steps_for(self.request(architect=True, cross_source=False)),
            ("profile", "overlap", "architect", "engineer", "gate", "report"),
        )

    def test_a_profiler_that_cannot_count_stops_the_run_before_anything_runs(self):
        fakes = Fakes(missing={"profiler": ["count the rows a spec keeps"]})
        state = self.run_graph(fakes)
        self.assertEqual(fakes.calls, [])
        self.assertEqual(state["status"], "stopped")
        self.assertEqual(state["reason"], "fake profiler cannot count the rows a spec keeps")

    def test_without_the_architect_counting_is_not_needed(self):
        fakes = Fakes(missing={"profiler": ["count the rows a spec keeps"]})
        state = self.invoke(fakes.toolkit(self.out), self.request())
        self.assertEqual(state["status"], "passed")
        self.assertNotIn("architect", fakes.calls)


if __name__ == "__main__":
    unittest.main()
