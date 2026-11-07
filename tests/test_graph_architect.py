"""The architect step of the build graph, with stand-in ports."""

import copy
import json
import unittest
from pathlib import Path

from graph_fakes import CONTRACT, Fakes, GraphCase, report

from tenx.domain import AgentFinding, ArtefactRef
from tenx.graph.build import ledger, steps_for
from tenx.ledger import render as render_usage

# The contract's SPEC.md with the stand-in's counts: six rows, one left out.
SPEC = """# Spec

The models to build, one section each. Row counts are from the raw files
as they were when the spec was written.

## `stg_shop__orders` (staging)

One row per order.

- Source: `shop.orders`, 6 rows
- Key: `order_id`
- Leave out:
  - `tests`, 1 row: Not real. Rule: `id = '0'`
- Rows to keep: 5

| Column | From | Meaning |
| --- | --- | --- |
| `order_id` | `shop.orders.id` | The order. |

## Assumptions

- Orders with ID 0 are tests.

## Questions for the requester

None.
"""


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


class ArchitectStepTest(ArchitectCase, unittest.TestCase):
    def test_a_good_contract_is_counted_and_written_as_the_spec(self):
        fakes = Fakes()
        state = self.run_graph(fakes)
        self.assertEqual(
            fakes.calls,
            ["profile", "overlap", "cross-source", "architect", "count", "scaffold", "engineer"]
            + ["gate"],
        )
        self.assertEqual(
            fakes.seen["architect"],
            [
                "GOAL.md",
                "JOINS.md",
                "source_profiles",
                "source_profiles/overlap.json",
                "source_profiles/overlap.txt",
                "source_profiles/shop.orders.json",
                "source_profiles/shop.orders.txt",
            ],
        )
        attempt = self.out / "architect" / "attempt-1"
        self.assertEqual((attempt / "SPEC.md").read_text(), SPEC)
        self.assertEqual(json.loads((attempt / "contract.json").read_text()), CONTRACT)
        # The stand-in's counts, as SPEC.md states them: six rows, one left out.
        self.assertEqual(
            (attempt / "counts.json").read_text(),
            '{\n  "stg_shop__orders": {\n    "source_rows": 6,\n'
            '    "excluded": {\n      "tests": 1\n    },\n    "kept": 5\n  }\n}\n',
        )
        spec = ArtefactRef.load(state["artefacts"]["spec"])
        self.assertEqual((Path(spec.path), spec.attempt), (attempt / "SPEC.md", 1))
        self.assertTrue(spec.unchanged())
        self.assertEqual(self.findings(state), [])

    def test_the_ledger_has_a_row_for_the_architect(self):
        fakes = Fakes()
        state = self.run_graph(fakes)
        text = render_usage(ledger(state, fakes.toolkit(self.out).roles, "none"))
        # 9 + 10 + 18 turns, a second each; 0.07 + 0.09 + 0.11 dollars.
        self.assertEqual(
            text,
            "usage by role (cost is Claude Code's own estimate, not a bill)\n\n"
            "role          outcome    turns  refused    time  cost (US$)\n"
            "cross-source  success        9        0   9.0 s      0.0700\n"
            "architect     success       10        0  10.0 s      0.0900\n"
            "engineer      success       18        0  18.0 s      0.1100\n"
            "run           gate PASS     37        0  37.0 s      0.2700\n\n"
            "role          model              input  cache write  cache read  output\n"
            "cross-source  claude-sonnet-5-5     10          100       1,000      50\n"
            "architect     claude-sonnet-5-5     10          100       1,000      50\n"
            "engineer      claude-sonnet-5-5     10          100       1,000      50\n",
        )

    def unusable(self, fakes, claim: str):
        state = self.run_graph(fakes)
        self.assertEqual(self.findings(state), [claim])
        self.assertNotIn("spec", state["artefacts"])
        for name in ("SPEC.md", "counts.json"):
            self.assertFalse((self.out / "architect" / "attempt-1" / name).exists())
        self.assertNotIn("SPEC.md", fakes.seen["engineer"])
        self.assertEqual(state["status"], "passed")

    def test_a_contract_naming_a_column_not_in_the_profiles_is_not_used(self):
        fakes = Fakes()
        wrong = copy.deepcopy(CONTRACT)
        wrong["models"][0]["columns"][0]["from"] = ["shop.orders.order_number"]
        fakes.reports["architect"] = report(0.09, 10, wrong)
        self.unusable(
            fakes,
            "no spec for the engineer: model stg_shop__orders column order_id comes from "
            "shop.orders.order_number, which is not a profiled column",
        )
        self.assertNotIn("count", fakes.calls)

    def test_a_rule_that_does_not_run_is_not_used(self):
        fakes = Fakes()
        broken = copy.deepcopy(CONTRACT)
        broken["models"][0]["exclusions"][0]["where"] = "nope = 1"
        fakes.reports["architect"] = report(0.09, 10, broken)
        self.unusable(fakes, "no spec for the engineer: model stg_shop__orders: no column nope")

    def test_an_answer_missing_is_not_used(self):
        fakes = Fakes()
        fakes.reports["architect"] = {**report(0.4, 15), "subtype": "error_max_turns"}
        self.unusable(
            fakes, "no spec for the engineer: the architect ended error_max_turns without an answer"
        )

    def test_a_crashed_architect_leaves_the_engineer_without_a_spec(self):
        fakes = Fakes()
        fakes.reports["architect"] = None
        self.unusable(fakes, "no spec for the engineer: the architect gave no report")


class SpecToEngineerTest(ArchitectCase, unittest.TestCase):
    def test_the_engineer_is_given_the_spec(self):
        fakes = Fakes()
        given = {}
        fakes.engineer_also = lambda work: given.update(text=(work / "SPEC.md").read_text())
        state = self.run_graph(fakes)
        self.assertEqual(given["text"], SPEC)
        self.assertEqual([item for item in state["findings"] if item["agent"] == "engineer"], [])

    def test_an_engineer_that_changes_the_spec_is_recorded(self):
        for change in ("edit", "remove"):
            with self.subTest(change):
                self.setUp()
                fakes = Fakes()

                def tamper(work, change=change):
                    if change == "edit":
                        (work / "SPEC.md").write_text(
                            SPEC.replace("Rows to keep: 5", "Rows to keep: 6")
                        )
                    else:
                        (work / "SPEC.md").unlink()

                fakes.engineer_also = tamper
                state = self.run_graph(fakes)
                claims = [
                    item["claim"] for item in state["findings"] if item["agent"] == "engineer"
                ]
                self.assertEqual(claims, ["the engineer changed or removed SPEC.md in attempt 1"])

    def test_a_repair_is_given_the_spec_afresh(self):
        fakes = Fakes()
        fakes.failing = [1, 0]
        rounds = []

        def note(work):
            rounds.append((work / "SPEC.md").read_text())
            (work / "SPEC.md").write_text("changed\n")

        fakes.engineer_also = note
        state = self.invoke(fakes.toolkit(self.out), self.request(architect=True, repairs=1))
        self.assertEqual(rounds, [SPEC, SPEC])
        self.assertEqual(state["status"], "passed")

    def test_a_resumed_run_uses_the_finished_architect_attempt(self):
        fakes = Fakes()
        stop = {"engineer": True}

        def dies(work):
            if stop.pop("engineer", False):
                raise SystemExit("killed")

        fakes.engineer_also = dies
        with self.assertRaises(SystemExit):
            self.run_graph(fakes)
        fakes.engineer_also = None
        state = self.invoke(fakes.toolkit(self.out), resume=True)
        self.assertEqual(fakes.calls.count("architect"), 1)
        self.assertEqual(state["attempts"]["architect"], 1)
        self.assertEqual(state["status"], "passed")


if __name__ == "__main__":
    unittest.main()
