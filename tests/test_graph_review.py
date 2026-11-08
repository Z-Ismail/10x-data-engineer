"""The spec review step of the build graph: wait, then approve, refuse or reject."""

import json
import unittest
from pathlib import Path

from graph_fakes import Fakes, GraphCase
from langgraph.types import Command
from test_graph_architect import SPEC

from tenx import artefacts
from tenx.graph.build import CHECKPOINT_FILE, build_graph, config, steps_for
from tenx.graph.checkpoints import open_checkpoints

SPEC_FILE = Path("architect") / "attempt-1" / "SPEC.md"
BEFORE = ["profile", "overlap", "cross-source", "architect", "count"]


class ReviewCase(GraphCase):
    def setUp(self):
        super().setUp()
        self.fakes = Fakes()
        self.given = {}

        def note(work):
            spec = work / "SPEC.md"
            self.given["spec"] = spec.read_text() if spec.exists() else None

        self.fakes.engineer_also = note

    def start(self, **changes):
        request = self.request(architect=True, review_spec=True, **changes)
        return self.invoke(self.fakes.toolkit(self.out), request)

    def answer(self, value):
        """Resume the waiting run with a person's answer; return the state it ends in."""
        with open_checkpoints(self.out / CHECKPOINT_FILE) as saver:
            app = build_graph(self.fakes.toolkit(self.out)).compile(checkpointer=saver)
            return app.invoke(Command(resume=value), config("run"), durability="sync")

    def waiting(self, state) -> dict:
        self.assertIn("__interrupt__", state)
        (pause,) = state["__interrupt__"]
        return pause.value

    def sha(self) -> str:
        return artefacts.file_hash(self.out / SPEC_FILE)

    def decisions(self, state) -> list[tuple[str, str]]:
        return [(item["decision"], item["reason"]) for item in state.get("approvals") or []]


class WaitTest(ReviewCase, unittest.TestCase):
    def test_the_review_comes_after_the_architect_only_when_asked(self):
        self.assertNotIn("review", steps_for(self.request(architect=True)))
        self.assertEqual(
            steps_for(self.request(architect=True, review_spec=True)),
            ("profile", "overlap", "cross-source", "architect", "review", "engineer", "gate")
            + ("report",),
        )

    def test_the_run_waits_after_the_spec_with_nothing_of_the_engineer_started(self):
        state = self.start()
        asked = self.waiting(state)
        self.assertEqual(
            asked,
            {"subject": "spec", "spec": str(self.out / SPEC_FILE), "sha256": self.sha()},
        )
        self.assertEqual(self.fakes.calls, BEFORE)
        self.assertFalse((self.out / "engineer").exists())
        self.assertEqual(state["status"], "running")
        # Starting it again without an answer waits on the same thing and calls nothing.
        with open_checkpoints(self.out / CHECKPOINT_FILE) as saver:
            app = build_graph(self.fakes.toolkit(self.out)).compile(checkpointer=saver)
            again = app.invoke(None, config("run"), durability="sync")
            self.assertEqual(app.get_state(config("run")).next, ("review",))
        self.assertEqual(self.waiting(again), asked)
        self.assertEqual(self.fakes.calls, BEFORE)


class ApproveTest(ReviewCase, unittest.TestCase):
    def test_an_approved_spec_is_the_one_the_engineer_is_given(self):
        self.start()
        sha = self.sha()
        state = self.answer({"decision": "approve", "sha256": sha})
        self.assertEqual(state["status"], "passed")
        self.assertEqual(self.given["spec"], SPEC)
        self.assertEqual(self.decisions(state), [("approved", "")])
        self.assertEqual(state["approvals"][0]["sha256"], sha)
        self.assertEqual(state["artefacts"]["approved spec"]["sha256"], sha)
        self.assertEqual(self.fakes.calls, BEFORE + ["scaffold", "engineer", "gate"])
        # The wait charged nothing: three calls, as in a run without a review.
        charged = [cost["charged_usd"] for cost in state["costs"]]
        self.assertEqual(charged, ["0.07", "0.09", "0.11"])

    def test_an_approval_for_another_spec_is_refused_and_the_run_waits(self):
        self.start()
        state = self.answer({"decision": "approve", "sha256": "0" * 64})
        asked = self.waiting(state)
        expected = f"the approval is for spec {'0' * 64}, but the run is waiting on {self.sha()}"
        self.assertEqual(asked["refused"], expected)
        self.assertEqual(self.decisions(state), [("refused", expected)])
        self.assertNotIn("engineer", self.fakes.calls)
        state = self.answer({"decision": "approve", "sha256": self.sha()})
        self.assertEqual(state["status"], "passed")
        self.assertEqual([d for d, _ in self.decisions(state)], ["refused", "approved"])

    def test_a_spec_changed_after_it_was_written_is_refused(self):
        self.start()
        sha = self.sha()
        spec = self.out / SPEC_FILE
        spec.write_text(SPEC.replace("Rows to keep: 5", "Rows to keep: 6"))
        state = self.answer({"decision": "approve", "sha256": sha})
        expected = (
            f"{spec} was changed after it was written; to use a changed spec, "
            "approve an edited contract"
        )
        self.assertEqual(self.waiting(state)["refused"], expected)
        self.assertNotIn("engineer", self.fakes.calls)
        self.assertNotIn("approved spec", state["artefacts"])

    def test_an_answer_the_run_does_not_take_is_refused(self):
        self.start()
        for value in ("yes", {"decision": "ship it"}, ["approve"]):
            with self.subTest(value=value):
                state = self.answer(value)
                self.assertTrue(self.waiting(state)["refused"].startswith("not an answer"))
        self.assertNotIn("engineer", self.fakes.calls)


class NoSpecTest(ReviewCase, unittest.TestCase):
    def test_without_a_usable_spec_the_person_is_told_why_and_may_go_on(self):
        self.fakes.reports["architect"] = None
        asked = self.waiting(self.start())
        self.assertEqual(
            asked,
            {
                "subject": "spec",
                "spec": None,
                "sha256": None,
                "missing": "no spec for the engineer: the architect gave no report",
            },
        )
        state = self.answer({"decision": "approve", "sha256": None})
        self.assertEqual(state["status"], "passed")
        self.assertIsNone(self.given["spec"])
        self.assertEqual(
            self.decisions(state),
            [("approved", "no spec; the engineer works from the goal alone")],
        )


class RejectTest(ReviewCase, unittest.TestCase):
    def test_a_rejected_spec_stops_the_run_before_the_engineer(self):
        self.start()
        state = self.answer({"decision": "reject", "reason": "the grain is wrong"})
        self.assertEqual(state["status"], "stopped")
        self.assertEqual(state["reason"], "spec rejected: the grain is wrong")
        self.assertEqual(self.decisions(state), [("rejected", "the grain is wrong")])
        self.assertEqual(self.fakes.calls, BEFORE)
        usage = json.loads((self.out / "usage.json").read_text())
        self.assertEqual([row["role"] for row in usage["roles"]], ["cross-source", "architect"])
        self.assertTrue((self.out / "budget.json").is_file())

    def test_a_rejection_needs_no_reason(self):
        self.start()
        state = self.answer({"decision": "reject"})
        self.assertEqual(state["reason"], "spec rejected")


if __name__ == "__main__":
    unittest.main()
