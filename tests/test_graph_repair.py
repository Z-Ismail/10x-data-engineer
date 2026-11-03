"""The build graph's repair loop, with stand-in ports."""

import json
import unittest
from dataclasses import replace
from decimal import Decimal

from graph_fakes import Fakes, GraphCase
from test_graph_build import Dying

from tenx.domain import ValidationResult
from tenx.policy import Policy
from tenx.repair import render


def usage_rows(out) -> list[tuple]:
    usage = json.loads((out / "usage.json").read_text())
    return [(role["role"], role["cost_usd"]) for role in usage["roles"]]


class RepairTest(GraphCase, unittest.TestCase):
    def test_a_failed_project_is_repaired_and_passes(self):
        fakes = Fakes()
        fakes.failing = [1, 0]

        def leave_build_files(work):
            (work / "target").mkdir(exist_ok=True)
            (work / "target" / "run_results.json").write_text("{}")
            (work / "dev.duckdb").write_text("db")

        fakes.engineer_also = leave_build_files
        state = self.invoke(fakes.toolkit(self.out), self.request(repairs=2))
        self.assertEqual(
            fakes.calls,
            [
                "profile",
                "overlap",
                "cross-source",
                "scaffold",
                "engineer",
                "gate",
                "engineer",
                "gate",
            ],
        )
        self.assertEqual(state["status"], "passed")
        self.assertNotIn("reason", state)
        self.assertEqual(state["rounds"], [1, 2])
        self.assertEqual(state["attempts"], {"cross-source": 1, "engineer": 2, "gate": 2})
        # The repair starts from the project checked, without what its build left.
        self.assertEqual(
            fakes.seen["engineer"],
            [
                "GATE_REPORT.md",
                "GOAL.md",
                "JOINS.md",
                "dbt_project.yml",
                "models",
                "models/orders.sql",
                "source_profiles",
                "source_profiles/shop.orders.json",
                "source_profiles/shop.orders.txt",
            ],
        )
        first = ValidationResult.load(state["validations"][0])
        repair = self.out / "engineer" / "attempt-2" / "work"
        self.assertEqual((repair / "GATE_REPORT.md").read_text(), render(first))
        self.assertIn("## rows: orders\n\n5 of 6 rows\n", render(first))
        self.assertEqual(state["artefacts"]["project"]["path"], str(repair))
        self.assertEqual(
            usage_rows(self.out),
            [("cross-source", 0.07), ("engineer", 0.11), ("engineer repair 1", 0.11)],
        )
        usage = json.loads((self.out / "usage.json").read_text())
        self.assertEqual(usage["gate"], "PASS")
        spending = json.loads((self.out / "budget.json").read_text())
        self.assertEqual(spending["spent_usd"], "0.29")

    def test_without_repairs_a_failure_ends_the_run_as_before(self):
        fakes = Fakes()
        fakes.failing = [1]
        state = self.invoke(fakes.toolkit(self.out), self.request())
        self.assertEqual(fakes.calls.count("engineer"), 1)
        self.assertEqual(state["status"], "failed")
        self.assertNotIn("reason", state)
        self.assertEqual(usage_rows(self.out), [("cross-source", 0.07), ("engineer", 0.11)])

    def test_a_repair_that_fails_as_many_checks_ends_the_run(self):
        fakes = Fakes()
        fakes.failing = [1, 1]
        state = self.invoke(fakes.toolkit(self.out), self.request(repairs=3))
        self.assertEqual(fakes.calls.count("gate"), 2)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(
            state["reason"],
            "repair 1 left 1 check failing or not run, against 1 before it, "
            "so no further repair was made",
        )

    def test_the_cap_ends_the_run(self):
        fakes = Fakes()
        fakes.failing = [2, 1]
        state = self.invoke(fakes.toolkit(self.out), self.request(repairs=1))
        self.assertEqual(fakes.calls.count("engineer"), 2)
        self.assertEqual(state["reason"], "the repairs allowed (1) were made")
        self.assertEqual(
            [row[0] for row in usage_rows(self.out)],
            ["cross-source", "engineer", "engineer repair 1"],
        )

    def test_a_repair_the_ceiling_cannot_hold_is_not_made(self):
        fakes = Fakes()
        fakes.failing = [1, 0]
        toolkit = fakes.toolkit(self.out, policy=Policy(run_usd=Decimal("0.90")))
        state = self.invoke(toolkit, self.request(repairs=2))
        self.assertEqual(fakes.calls.count("engineer"), 1)
        self.assertEqual(state["status"], "failed")
        # 0.07 + 0.11 spent; 0.18 + 0.75 = 0.93 > 0.90.
        self.assertEqual(
            state["reason"],
            "no repair made: stopped: budget: engineer was not started; its cap of "
            "US$0.75 with US$0.18 spent and US$0 reserved would pass the run's ceiling "
            "of US$0.90",
        )
        self.assertEqual(json.loads((self.out / "usage.json").read_text())["gate"], "FAIL")

    def test_a_crash_inside_a_repair_resumes_into_a_new_attempt(self):
        fakes = Fakes()
        fakes.failing = [1, 0]
        toolkit = fakes.toolkit(self.out)
        calls = {"engineer": 0}
        backend = toolkit.backend
        dying = Dying(backend)

        def run(role, *args, **kwargs):
            if role.name == "engineer":
                calls["engineer"] += 1
                if calls["engineer"] == 2:
                    return dying.run(role, *args, **kwargs)
            return backend.run(role, *args, **kwargs)

        backend_with_crash = type("B", (), {"name": backend.name})()
        backend_with_crash.capabilities = backend.capabilities
        backend_with_crash.run = run
        with self.assertRaises(SystemExit):
            self.invoke(replace(toolkit, backend=backend_with_crash), self.request(repairs=2))
        state = self.invoke(fakes.toolkit(self.out), resume=True)
        self.assertEqual(state["status"], "passed")
        self.assertEqual(fakes.calls.count("gate"), 2)
        self.assertEqual(state["rounds"], [1, 3])
        charged = [
            (c["attempt"], c["charged_usd"]) for c in state["costs"] if c["node"] == "engineer"
        ]
        # The attempt cut off is charged its whole reservation.
        self.assertEqual(charged, [(1, "0.11"), (2, "0.75"), (3, "0.11")])
        self.assertEqual(
            usage_rows(self.out),
            [("cross-source", 0.07), ("engineer", 0.11), ("engineer repair 1", 0.11)],
        )

    def test_a_link_the_agent_made_is_carried_as_a_link_never_followed(self):
        fakes = Fakes()
        fakes.failing = [1, 0]
        secret = self.declaration
        secret.write_text('{"models": "the answer"}\n')

        def plant(work):
            link = work / "models" / "secret.sql"
            if not link.is_symlink():
                link.symlink_to(secret)

        fakes.engineer_also = plant
        self.invoke(fakes.toolkit(self.out), self.request(repairs=1))
        carried = self.out / "engineer" / "attempt-2" / "work" / "models" / "secret.sql"
        self.assertTrue(carried.is_symlink())
        self.assertEqual(carried.readlink(), secret)


if __name__ == "__main__":
    unittest.main()
