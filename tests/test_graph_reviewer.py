"""The reviewer step of the build graph, with stand-in ports."""

import json
import unittest
from decimal import Decimal
from pathlib import Path

from graph_fakes import Fakes, GraphCase, report

from tenx.domain import AgentFinding
from tenx.graph.build import ledger, steps_for
from tenx.policy import Policy

# The stand-in reviewer's answer, as REVIEW.md shows it.
REVIEW_TEXT = """# Review

Advice from the reviewer on a project that passed every check. It does not
change the checks' verdict.

Verdict: request changes

The orders model holds a constant.

## Blocking

- `models/orders.sql:1`, `orders`: The model selects the number 1 instead of the orders.
  Evidence: Line 1: select 1.
"""

# What the stand-in gate lets an agent be told: nothing beyond each check's name.
PACKET_CHECKS = (
    "- build on project: No more is said about this check.\n"
    "- rows on orders: No more is said about this check.\n"
)


class ReviewerCase(GraphCase):
    def run_graph(self, fakes, toolkit=None, **changes):
        changes.setdefault("cross_source", False)
        return self.invoke(
            toolkit or fakes.toolkit(self.out), self.request(reviewer=True, **changes)
        )

    def claims(self, state) -> list[str]:
        return [
            AgentFinding.load(item).claim
            for item in state.get("findings") or []
            if item["agent"] == "reviewer"
        ]


class ReviewerPlanTest(ReviewerCase, unittest.TestCase):
    def test_the_reviewer_comes_after_the_gate_only_when_asked(self):
        self.assertNotIn("reviewer", steps_for(self.request()))
        self.assertEqual(
            steps_for(self.request(reviewer=True)),
            ("profile", "overlap", "cross-source", "engineer", "gate", "reviewer", "report"),
        )

    def test_a_backend_without_structured_answers_stops_the_run_first(self):
        fakes = Fakes(missing={"backend": ["give a structured answer"]})
        state = self.run_graph(fakes)
        self.assertEqual(fakes.calls, [])
        self.assertEqual(state["status"], "stopped")


class ReviewerStepTest(ReviewerCase, unittest.TestCase):
    def test_a_passed_project_is_reviewed_from_its_packet(self):
        fakes = Fakes()
        state = self.run_graph(fakes)
        self.assertEqual(fakes.calls, ["profile", "scaffold", "engineer", "gate", "reviewer"])
        self.assertEqual(
            fakes.seen["reviewer"],
            [
                "GOAL.md",
                "PACKET.md",
                "packet.json",
                "project",
                "project/dbt_project.yml",
                "project/models",
                "project/models/orders.sql",
                "source_profiles",
                "source_profiles/shop.orders.json",
                "source_profiles/shop.orders.txt",
            ],
        )
        packet = (self.out / "reviewer" / "attempt-1" / "work" / "PACKET.md").read_text()
        self.assertIn(PACKET_CHECKS, packet)
        written = self.out / "reviewer" / "attempt-1" / "REVIEW.md"
        self.assertEqual(written.read_text(), REVIEW_TEXT)
        self.assertEqual(state["artefacts"]["review"]["path"], str(written))
        self.assertEqual(
            self.claims(state),
            [
                "the reviewer's verdict: request changes. The orders model holds a constant.",
                "blocking: The model selects the number 1 instead of the orders.",
            ],
        )

    def test_a_request_for_changes_leaves_the_gates_verdict(self):
        state = self.run_graph(Fakes())
        self.assertEqual(state["status"], "passed")
        self.assertNotIn("reason", state)
        usage = json.loads((self.out / "usage.json").read_text())
        self.assertEqual(usage["gate"], "PASS")

    def test_the_ledger_has_a_row_for_the_reviewer(self):
        fakes = Fakes()
        state = self.run_graph(fakes)
        rows = ledger(state, fakes.toolkit(self.out).roles, "none")
        self.assertEqual([row.role for row in rows.roles], ["engineer", "reviewer"])
        self.assertEqual(rows.roles[1].cost_usd, 0.06)

    def test_a_failed_project_is_not_reviewed(self):
        fakes = Fakes()
        fakes.verdict = False
        state = self.run_graph(fakes)
        self.assertEqual(state["status"], "failed")
        self.assertNotIn("reviewer", fakes.calls)
        self.assertEqual(self.claims(state), [])
        self.assertFalse((self.out / "reviewer").exists())
        self.assertFalse((self.out / "review-packet").exists())

    def test_a_repaired_project_is_reviewed_as_repaired(self):
        fakes = Fakes()
        fakes.failing = [1, 0]

        def mark(work: Path) -> None:
            (work / "models" / "orders.sql").write_text("select 1\n-- repaired\n")

        fakes.engineer_also = mark
        state = self.run_graph(fakes, repairs=1)
        self.assertEqual(state["status"], "passed")
        self.assertEqual(fakes.calls[-3:], ["engineer", "gate", "reviewer"])
        model = self.out / "reviewer" / "attempt-1" / "work" / "project" / "models" / "orders.sql"
        self.assertEqual(model.read_text(), "select 1\n-- repaired\n")

    def test_without_the_reviewer_nothing_of_it_is_made(self):
        fakes = Fakes()
        state = self.invoke(fakes.toolkit(self.out), self.request(cross_source=False))
        self.assertEqual(state["status"], "passed")
        self.assertNotIn("reviewer", fakes.calls)
        for name in ("reviewer", "review-packet", "gate/attempt-1/checks.json"):
            self.assertFalse((self.out / name).exists(), name)


class NoReviewTest(ReviewerCase, unittest.TestCase):
    def assertNoReview(self, state, reason: str) -> None:
        self.assertEqual(state["status"], "passed")
        self.assertEqual(self.claims(state), [f"no review: {reason}"])
        self.assertNotIn("review", state["artefacts"])

    def test_an_answer_that_fails_its_check_gives_no_review(self):
        fakes = Fakes()
        wrong = json.loads(json.dumps(fakes.reports["reviewer"]))
        wrong["structured_output"]["findings"][0]["line"] = 2
        fakes.reports["reviewer"] = wrong
        state = self.run_graph(fakes)
        self.assertNoReview(state, "finding 1 cites line 2 of models/orders.sql, which has 1 line")

    def test_a_crashed_reviewer_is_charged_and_gives_no_review(self):
        fakes = Fakes()
        fakes.reports["reviewer"] = None
        state = self.run_graph(fakes)
        self.assertNoReview(state, "the reviewer gave no report")
        reviewer = [cost for cost in state["costs"] if cost["node"] == "reviewer"]
        self.assertEqual([cost["charged_usd"] for cost in reviewer], ["0.40"])

    def test_a_copy_of_a_hidden_file_gives_no_packet_and_no_call(self):
        fakes = Fakes()
        hidden = self.declaration.read_bytes()
        fakes.engineer_also = lambda work: (work / "models" / "copy.sql").write_bytes(hidden)
        state = self.run_graph(fakes)
        self.assertNotIn("reviewer", fakes.calls)
        self.assertNoReview(
            state, "project/models/copy.sql has the same bytes as a file hidden from agents"
        )

    def test_a_copy_of_a_gate_file_gives_no_packet(self):
        fakes = Fakes()
        fakes.engineer_also = lambda work: (work / "models" / "built.sql").write_text("tables\n")
        state = self.run_graph(fakes)
        self.assertNotIn("reviewer", fakes.calls)
        self.assertEqual(
            self.claims(state),
            ["no review: project/models/built.sql has the same bytes as a file hidden from agents"],
        )

    def test_a_review_the_ceiling_cannot_hold_leaves_the_verdict(self):
        fakes = Fakes()
        fakes.reports["engineer"] = report(0.50, 18)
        toolkit = fakes.toolkit(self.out, policy=Policy(run_usd=Decimal("0.80")))
        state = self.run_graph(fakes, toolkit=toolkit)
        self.assertNotIn("reviewer", fakes.calls)
        self.assertEqual(state["status"], "passed")
        [claim] = self.claims(state)
        self.assertTrue(claim.startswith("no review: stopped: budget"), claim)


if __name__ == "__main__":
    unittest.main()
