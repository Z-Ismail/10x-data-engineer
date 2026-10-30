"""The build graph with stand-in ports: steps, stops, budget and resume."""

import json
import unittest
from pathlib import Path

from graph_fakes import Fakes, GraphCase

from tenx.domain import ArtefactRef, ExecutionPlan, ResourceBudget
from tenx.graph.build import steps_for
from tenx.policy import Policy


def _noted(fakes: Fakes, name: str):
    def node(state):
        fakes.calls.append(name)
        return {}

    return node


class PlanTest(GraphCase, unittest.TestCase):
    def stubs(self, fakes):
        return {step: _noted(fakes, step) for step in ("cross-source", "engineer", "gate")}

    def test_the_default_run_and_one_without_the_cross_source_agent(self):
        self.assertEqual(
            steps_for(self.request()),
            ("profile", "overlap", "cross-source", "engineer", "gate", "report"),
        )
        self.assertEqual(
            steps_for(self.request(cross_source=False)),
            ("profile", "engineer", "gate", "report"),
        )

    def test_steps_run_in_the_plan_order_and_record_what_they_made(self):
        fakes = Fakes()
        state = self.invoke(fakes.toolkit(self.out), self.request(), self.stubs(fakes))
        self.assertEqual(fakes.calls, ["profile", "overlap", "cross-source", "engineer", "gate"])
        self.assertEqual(ExecutionPlan.load(state["plan"]).steps, steps_for(self.request()))
        self.assertEqual(ResourceBudget.load(state["budget"]).to_policy(), Policy())
        profiles = ArtefactRef.load(state["artefacts"]["profiles"])
        self.assertEqual(Path(profiles.path), self.out / "profile")
        self.assertTrue(profiles.unchanged())
        self.assertEqual(Path(state["artefacts"]["overlap"]["path"]), self.out / "overlap")
        # No verdict was given, so the run did not pass.
        self.assertEqual(state["status"], "failed")

    def test_without_the_cross_source_agent_there_is_no_overlap_step(self):
        fakes = Fakes()
        state = self.invoke(
            fakes.toolkit(self.out), self.request(cross_source=False), self.stubs(fakes)
        )
        self.assertEqual(fakes.calls, ["profile", "engineer", "gate"])
        self.assertNotIn("overlap", state["artefacts"])

    def test_a_missing_capability_stops_the_run_before_anything_runs(self):
        cases = [
            ("profiler", "compare columns across tables", "fake profiler"),
            ("backend", "give a structured answer", "fake backend"),
            ("engine", "scaffold a project", "fake engine"),
            ("validator", "validate a project", "fake gate"),
        ]
        for port, capability, name in cases:
            with self.subTest(port=port):
                self.setUp()
                fakes = Fakes(missing={port: [capability]})
                state = self.invoke(fakes.toolkit(self.out), self.request(), self.stubs(fakes))
                self.assertEqual(fakes.calls, [])
                self.assertEqual(state["status"], "stopped")
                self.assertEqual(state["reason"], f"{name} cannot {capability}")
                self.assertEqual(state["artefacts"], {})

    def test_a_structured_answer_is_not_needed_without_the_cross_source_agent(self):
        fakes = Fakes(missing={"backend": ["give a structured answer"]})
        state = self.invoke(
            fakes.toolkit(self.out), self.request(cross_source=False), self.stubs(fakes)
        )
        self.assertEqual(fakes.calls, ["profile", "engineer", "gate"])
        self.assertEqual(state["status"], "failed")

    def test_scouts_are_refused(self):
        fakes = Fakes()
        state = self.invoke(fakes.toolkit(self.out), self.request(scouts=True), self.stubs(fakes))
        self.assertEqual(fakes.calls, [])
        self.assertEqual(state["status"], "stopped")
        self.assertIn("scouts are not part of the graph run", state["reason"])

    def test_the_state_is_json_only(self):
        fakes = Fakes()
        state = self.invoke(fakes.toolkit(self.out), self.request(), self.stubs(fakes))
        self.assertEqual(json.loads(json.dumps(state)), state)

    def test_a_profile_left_part_way_is_made_again_from_nothing(self):
        stale = self.out / "profile" / "stale.json"
        stale.parent.mkdir()
        stale.write_text("{}")
        fakes = Fakes()
        self.invoke(fakes.toolkit(self.out), self.request(), self.stubs(fakes))
        self.assertFalse(stale.exists())
