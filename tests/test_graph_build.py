"""The build graph with stand-in ports: steps, stops, budget and resume."""

import json
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from graph_fakes import ANSWER, Fakes, GraphCase, report

from tenx import artefacts
from tenx.cross_source import render as render_notes
from tenx.domain import (
    AgentFinding,
    ArtefactRef,
    CostRecord,
    ExecutionPlan,
    ResourceBudget,
    ValidationResult,
)
from tenx.graph.build import cross_source_node, overlap_node, profile_node, steps_for
from tenx.policy import Budget, Policy


class Dying:
    """A backend whose process dies part way through the agent's call."""

    def __init__(self, backend) -> None:
        self.name = backend.name
        self.capabilities = backend.capabilities

    def run(self, *args, **kwargs):
        raise SystemExit("killed")


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


class CrossSourceTest(GraphCase, unittest.TestCase):
    """The cross-source step, called directly on a state made by the steps before it."""

    def state(self, toolkit, **changes) -> dict:
        state = {"run_id": "run", "request": self.request(**changes).dump(), "costs": []}
        state["artefacts"] = {
            **profile_node(toolkit)(state)["artefacts"],
            **overlap_node(toolkit)(state)["artefacts"],
        }
        return state

    def test_a_checked_answer_becomes_joins_for_the_engineer(self):
        fakes = Fakes()
        toolkit = fakes.toolkit(self.out)
        update = cross_source_node(toolkit)(self.state(toolkit))
        self.assertEqual(fakes.calls, ["profile", "overlap", "cross-source"])
        self.assertEqual(
            fakes.seen["cross-source"],
            [
                "GOAL.md",
                "source_profiles",
                "source_profiles/overlap.json",
                "source_profiles/overlap.txt",
                "source_profiles/shop.orders.json",
                "source_profiles/shop.orders.txt",
            ],
        )
        folder = self.out / "cross-source" / "attempt-1"
        self.assertEqual((folder / "JOINS.md").read_text(), render_notes(ANSWER))
        self.assertEqual(update["artefacts"]["joins"]["path"], str(folder / "JOINS.md"))
        self.assertEqual(update["attempts"], {"cross-source": 1})
        self.assertEqual(artefacts.load(folder).status, "complete")
        [cost] = [CostRecord.load(cost) for cost in update["costs"]]
        self.assertEqual(
            (cost.node, cost.attempt, cost.outcome, cost.turns), ("cross-source", 1, "success", 9)
        )
        self.assertEqual(
            (cost.reserved_usd, cost.charged_usd, cost.overshoot_usd),
            (Decimal("0.40"), Decimal("0.07"), Decimal(0)),
        )
        findings = [AgentFinding.load(finding) for finding in update["findings"]]
        self.assertEqual([finding.kind for finding in findings], ["recommendation", "hypothesis"])
        self.assertEqual(findings[0].assets, ("shop.orders.email", "shop.orders.id"))
        self.assertEqual(findings[1].claim, "Check for shared domains.")
        self.assertEqual(toolkit.budget.spent_usd, Decimal("0.07"))

    def test_an_answer_that_fails_its_check_is_not_passed_on(self):
        fakes = Fakes()
        bad = {**ANSWER, "joins": [{**ANSWER["joins"][0], "right": "shop.orders.nope"}]}
        fakes.reports["cross-source"] = report(0.07, 9, bad)
        toolkit = fakes.toolkit(self.out)
        update = cross_source_node(toolkit)(self.state(toolkit))
        self.assertNotIn("artefacts", update)
        self.assertFalse((self.out / "cross-source" / "attempt-1" / "JOINS.md").exists())
        [finding] = update["findings"]
        self.assertEqual(
            finding["claim"],
            "no notes for the engineer: join 1 names 'shop.orders.nope', "
            "which is not a column in the profiles",
        )
        self.assertEqual(len(update["costs"]), 1)

    def test_a_call_with_no_report_is_charged_its_whole_reservation(self):
        fakes = Fakes()
        fakes.reports["cross-source"] = None
        toolkit = fakes.toolkit(self.out)
        update = cross_source_node(toolkit)(self.state(toolkit))
        [cost] = [CostRecord.load(cost) for cost in update["costs"]]
        self.assertEqual((cost.outcome, cost.charged_usd), ("no report", Decimal("0.40")))
        self.assertEqual(artefacts.load(self.out / "cross-source" / "attempt-1").status, "failed")
        self.assertIn("gave no report", update["findings"][0]["claim"])

    def test_a_call_that_does_not_fit_the_ceiling_is_never_made(self):
        fakes = Fakes()
        budget = Budget()
        budget.restore("engineer", "engineer", "0.75", Decimal("1.70"))
        toolkit = fakes.toolkit(self.out, budget=budget)
        update = cross_source_node(toolkit)(self.state(toolkit))
        self.assertNotIn("cross-source", fakes.calls)
        self.assertEqual(update["status"], "stopped")
        self.assertEqual(
            update["reason"],
            "stopped: budget: cross-source was not started; its cap of US$0.40 with "
            "US$1.70 spent and US$0 reserved would pass the run's ceiling of US$2.00",
        )
        self.assertFalse((self.out / "cross-source").exists())

    def test_a_finished_attempt_is_reused_after_a_crash_and_charged_once(self):
        fakes = Fakes()
        state = self.state(fakes.toolkit(self.out))
        first = cross_source_node(fakes.toolkit(self.out))(state)
        # The process died before the step was saved; a new one runs it again.
        again = Fakes()
        toolkit = again.toolkit(self.out)
        second = cross_source_node(toolkit)(state)
        self.assertEqual(again.calls, [])
        self.assertEqual(second["costs"], first["costs"])
        self.assertEqual(second["artefacts"], first["artefacts"])
        self.assertEqual(toolkit.budget.spent_usd, Decimal("0.07"))

    def test_an_attempt_cut_off_is_charged_in_full_and_a_new_one_made(self):
        fakes = Fakes()
        state = self.state(fakes.toolkit(self.out))
        killed = fakes.toolkit(self.out)
        killed = replace(killed, backend=Dying(killed.backend))
        with self.assertRaises(SystemExit):
            cross_source_node(killed)(state)
        toolkit = fakes.toolkit(self.out)
        update = cross_source_node(toolkit)(state)
        costs = [CostRecord.load(cost) for cost in update["costs"]]
        self.assertEqual(
            [(cost.attempt, cost.outcome, cost.charged_usd) for cost in costs],
            [(1, "interrupted", Decimal("0.40")), (2, "success", Decimal("0.07"))],
        )
        self.assertEqual(update["attempts"], {"cross-source": 2})
        # 0.40 + 0.07
        self.assertEqual(toolkit.budget.spent_usd, Decimal("0.47"))

    def test_an_edited_goal_gives_a_new_attempt(self):
        fakes = Fakes()
        toolkit = fakes.toolkit(self.out)
        state = self.state(toolkit)
        first = cross_source_node(toolkit)(state)
        self.goal.write_text("Build orders and customers.\n")
        state = {**state, "costs": first["costs"]}
        second = cross_source_node(toolkit)(state)
        self.assertEqual(fakes.calls.count("cross-source"), 2)
        self.assertEqual(second["attempts"], {"cross-source": 2})
        self.assertEqual([cost["attempt"] for cost in second["costs"]], [2])


class RunTest(GraphCase, unittest.TestCase):
    """Whole runs with stand-in ports, from the plan to the report."""

    def test_a_run_that_passes(self):
        fakes = Fakes()
        state = self.invoke(fakes.toolkit(self.out), self.request())
        self.assertEqual(
            fakes.calls,
            ["profile", "overlap", "cross-source", "scaffold", "engineer", "gate"],
        )
        self.assertEqual(
            fakes.seen["engineer"],
            [
                "GOAL.md",
                "JOINS.md",
                "dbt_project.yml",
                "models",
                "source_profiles",
                "source_profiles/shop.orders.json",
                "source_profiles/shop.orders.txt",
            ],
        )
        self.assertEqual(state["status"], "passed")
        project = self.out / "engineer" / "attempt-1" / "work"
        self.assertEqual(state["artefacts"]["project"]["path"], str(project))
        self.assertTrue((project / "models" / "orders.sql").is_file())
        [verdict] = [ValidationResult.load(item) for item in state["validations"]]
        self.assertEqual((verdict.passed, verdict.checks, verdict.failures), (True, 2, ()))
        self.assertEqual(Path(verdict.report.path).read_text(), "gate pass\n")
        self.assertEqual(state["attempts"], {"cross-source": 1, "engineer": 1, "gate": 1})
        usage = json.loads((self.out / "usage.json").read_text())
        self.assertEqual(usage["gate"], "PASS")
        self.assertEqual(usage["sandbox"], "none")
        self.assertEqual(
            [(role["role"], role["turns"], role["cost_usd"]) for role in usage["roles"]],
            [("cross-source", 9, 0.07), ("engineer", 18, 0.11)],
        )
        spending = json.loads((self.out / "budget.json").read_text())
        # 0.07 + 0.11, each well under its cap.
        self.assertEqual((spending["spent_usd"], spending["overshoot_usd"]), ("0.18", "0"))
        charged = sum(Decimal(cost["charged_usd"]) for cost in state["costs"])
        self.assertEqual(charged, Decimal("0.18"))

    def test_a_run_that_fails_the_gate(self):
        fakes = Fakes()
        fakes.verdict = False
        state = self.invoke(fakes.toolkit(self.out), self.request())
        self.assertEqual(state["status"], "failed")
        [verdict] = [ValidationResult.load(item) for item in state["validations"]]
        self.assertEqual(
            [(f.check, f.subject, f.status) for f in verdict.failures], [("rows", "orders", "fail")]
        )
        self.assertEqual(json.loads((self.out / "usage.json").read_text())["gate"], "FAIL")

    def test_a_run_without_the_cross_source_agent(self):
        fakes = Fakes()
        state = self.invoke(fakes.toolkit(self.out), self.request(cross_source=False))
        self.assertEqual(fakes.calls, ["profile", "scaffold", "engineer", "gate"])
        self.assertNotIn("JOINS.md", fakes.seen["engineer"])
        self.assertEqual(state["status"], "passed")
        usage = json.loads((self.out / "usage.json").read_text())
        self.assertEqual([role["role"] for role in usage["roles"]], ["engineer"])

    def test_a_ceiling_reached_stops_the_run_before_the_engineer(self):
        fakes = Fakes()
        toolkit = fakes.toolkit(self.out, policy=Policy(run_usd=Decimal("0.80")))
        state = self.invoke(toolkit, self.request())
        self.assertEqual(fakes.calls, ["profile", "overlap", "cross-source"])
        self.assertEqual(state["status"], "stopped")
        # 0.07 charged for the cross-source call; 0.07 + 0.75 = 0.82 > 0.80.
        self.assertEqual(
            state["reason"],
            "stopped: budget: engineer was not started; its cap of US$0.75 with "
            "US$0.07 spent and US$0 reserved would pass the run's ceiling of US$0.80",
        )
        self.assertFalse((self.out / "engineer").exists())
        self.assertEqual(state["validations"], [])
        usage = json.loads((self.out / "usage.json").read_text())
        self.assertEqual(usage["gate"], "not run")
        self.assertEqual([role["role"] for role in usage["roles"]], ["cross-source"])
        spending = json.loads((self.out / "budget.json").read_text())
        self.assertEqual((spending["spent_usd"], spending["reserved_usd"]), ("0.07", "0"))
