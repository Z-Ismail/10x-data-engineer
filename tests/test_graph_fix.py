"""The operate workflow's fix step: the engineer fixes a copy of the project handed over."""

import unittest
from decimal import Decimal

from graph_fakes import Fakes
from test_graph_operate import OperateCase, tree

from tenx.graph.build import gate_node, ledger, report_node, steps_for
from tenx.policy import Budget, Policy
from tenx.ports import PREPARE

OPENING = (
    "# What the checks found\n"
    "\n"
    "The project in this folder was handed over failing its checks. It was built\n"
    "from an empty database and checked. 1 check failed, 2 passed.\n"
    "It is not your previous attempt, but treat it as one: find the cause of each\n"
    "failure and fix it in the models, for every row it affects, not only for the\n"
    "rows or values named here, change nothing the failures do not need, and run\n"
    "`dbt build` again before you stop.\n"
)
WITH_DIAGNOSIS = (
    "\n"
    "`DIAGNOSIS.md` in this folder is a diagnosis of these failures, written by\n"
    "another agent from the project, the checks' feedback and the build's log.\n"
    "Its file and line numbers are this project's. Check each cause it names\n"
    "against the project before you act on it; it can be wrong.\n"
)
ROWS = "\n## rows: orders\n\n5 of 6 rows\n"
FIX_DIFF = (
    "# The fix proposed for the project: what the engineer changed in a copy of it.\n"
    "# Your project was not changed. Review this, then apply it in the project's\n"
    "# folder with: git apply FIX.diff (or: patch -p1 < FIX.diff)\n"
    "--- a/models/orders.sql\n"
    "+++ b/models/orders.sql\n"
    "@@ -1 +1 @@\n"
    "-select 1\n"
    "+select * from orders\n"
)


def fixes(workspace):
    (workspace / "models" / "orders.sql").write_text("select * from orders\n")
    # What the engineer's own dbt build leaves, which is no part of the fix.
    (workspace / ".user.yml").write_text("id: 7652be3b\n")
    (workspace / "target").mkdir()
    (workspace / "target" / "run_results.json").write_text("{}\n")
    (workspace / "dev.duckdb").write_text("tables\n")


class FixCase(OperateCase):
    def fakes(self, *failing: int) -> Fakes:
        made = Fakes()
        made.failing = list(failing)
        made.engineer_also = fixes
        return made

    def work(self, attempt: int):
        return self.out / "engineer" / f"attempt-{attempt}" / "work"


class FixPlanTest(FixCase, unittest.TestCase):
    def test_fixes_add_the_engineer_to_the_operate_plan(self):
        self.assertEqual(
            steps_for(self.operate(repairs=1)),
            ("intake", "profile", "overlap", "engineer", "gate", "diagnostics", "report"),
        )

    def test_leaving_the_diagnosis_out_drops_the_diagnostics_step(self):
        self.assertEqual(
            steps_for(self.operate(repairs=1, skip_diagnosis=True)),
            ("intake", "profile", "overlap", "engineer", "gate", "report"),
        )
        self.assertEqual(
            steps_for(self.operate(skip_diagnosis=True)),
            ("intake", "profile", "overlap", "gate", "report"),
        )

    def test_an_engine_that_cannot_prepare_a_project_stops_a_fixing_run_only(self):
        fakes = Fakes(missing={"engine": [PREPARE]})
        state = self.run_graph(fakes, repairs=1)
        self.assertEqual(state["status"], "stopped")
        self.assertEqual(
            state["reason"], "fake engine cannot prepare an existing project for an agent's builds"
        )
        self.assertEqual(fakes.calls, [])
        built = self.invoke(Fakes(missing={"engine": [PREPARE]}).toolkit(self.root), self.request())
        self.assertEqual(built["status"], "passed")


class FixTest(FixCase, unittest.TestCase):
    def test_a_diagnosed_project_is_fixed_in_a_copy_and_checked_again(self):
        before = tree(self.project)
        fakes = self.fakes(1, 0)
        state = self.run_graph(fakes, repairs=1)
        self.assertEqual(
            fakes.calls,
            ["profile", "overlap", "gate", "diagnostics", "prepare", "prepare", "engineer", "gate"],
        )
        self.assertEqual(
            fakes.seen["engineer"],
            [
                "DIAGNOSIS.md",
                "GATE_REPORT.md",
                "GOAL.md",
                "dbt_project.yml",
                "models",
                "models/orders.sql",
                "profiles.yml",
                "source_profiles",
                "source_profiles/shop.orders.json",
                "source_profiles/shop.orders.txt",
            ],
        )
        work = self.work(1)
        self.assertEqual((work / "GATE_REPORT.md").read_text(), OPENING + WITH_DIAGNOSIS + ROWS)
        written = self.out / "diagnostics" / "attempt-1" / "DIAGNOSIS.md"
        self.assertEqual((work / "DIAGNOSIS.md").read_text(), written.read_text())
        self.assertEqual((work / "profiles.yml").read_text(), "fake profile for raw\n")
        diff = self.out / "engineer" / "attempt-1" / "FIX.diff"
        self.assertEqual(diff.read_text(), FIX_DIFF)
        self.assertEqual(state["artefacts"]["fix"]["path"], str(diff))
        self.assertEqual(state["artefacts"]["project"]["path"], str(work))
        self.assertEqual(tree(self.project), before)
        self.assertEqual(state["status"], "passed")
        used = ledger(state, fakes.toolkit(self.out).roles, "none")
        self.assertEqual([role.role for role in used.roles], ["diagnostics", "engineer"])
        self.assertEqual(used.gate, "PASS")

    def test_only_the_first_fix_gets_the_diagnosis_and_fixes_stop_without_headway(self):
        fakes = self.fakes(2, 1, 1)
        state = self.run_graph(fakes, repairs=3)
        self.assertEqual(fakes.calls.count("engineer"), 2)
        self.assertTrue((self.work(1) / "DIAGNOSIS.md").is_file())
        second = self.work(2)
        self.assertFalse((second / "DIAGNOSIS.md").exists())
        self.assertTrue(
            (second / "GATE_REPORT.md")
            .read_text()
            .startswith("# What the checks found\n\nYour previous attempt was built")
        )
        self.assertEqual(state["status"], "failed")
        self.assertEqual(
            state["reason"],
            "repair 2 left 1 check failing or not run, against 1 before it, "
            "so no further repair was made",
        )
        self.assertEqual(state["rounds"], [1, 2])

    def test_fixes_stop_at_the_number_asked_for(self):
        fakes = self.fakes(2, 1, 0)
        state = self.run_graph(fakes, repairs=1)
        self.assertEqual(fakes.calls.count("engineer"), 1)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["reason"], "the repairs allowed (1) were made")

    def test_without_a_diagnosis_the_fix_gets_the_checks_alone(self):
        fakes = self.fakes(1, 0)
        fakes.reports["diagnostics"] = None
        state = self.run_graph(fakes, repairs=1)
        self.assertNotIn("DIAGNOSIS.md", fakes.seen["engineer"])
        self.assertEqual((self.work(1) / "GATE_REPORT.md").read_text(), OPENING + ROWS)
        self.assertEqual(state["status"], "passed")

    def test_a_passed_project_is_not_fixed(self):
        fakes = self.fakes(0)
        state = self.run_graph(fakes, repairs=2)
        self.assertEqual(fakes.calls, ["profile", "overlap", "gate"])
        self.assertEqual(state["status"], "passed")
        self.assertNotIn("fix", state["artefacts"])


class SkippedDiagnosisTest(FixCase, unittest.TestCase):
    def test_a_fix_without_the_diagnostics_step_gets_the_checks_alone(self):
        before = tree(self.project)
        fakes = self.fakes(1, 0)
        state = self.run_graph(fakes, repairs=1, skip_diagnosis=True)
        self.assertEqual(
            fakes.calls, ["profile", "overlap", "gate", "prepare", "prepare", "engineer", "gate"]
        )
        self.assertNotIn("DIAGNOSIS.md", fakes.seen["engineer"])
        self.assertIn("GATE_REPORT.md", fakes.seen["engineer"])
        self.assertEqual((self.work(1) / "GATE_REPORT.md").read_text(), OPENING + ROWS)
        self.assertFalse((self.out / "diagnostics").exists())
        self.assertNotIn("diagnosis", state["artefacts"])
        self.assertEqual(self.claims(state), [])
        self.assertEqual((self.out / "engineer" / "attempt-1" / "FIX.diff").read_text(), FIX_DIFF)
        self.assertEqual(tree(self.project), before)
        self.assertEqual(state["status"], "passed")
        used = ledger(state, fakes.toolkit(self.out).roles, "none")
        self.assertEqual([role.role for role in used.roles], ["engineer"])

    def test_without_fixes_a_skipped_diagnosis_leaves_the_gate_verdict(self):
        fakes = self.fakes(1)
        state = self.run_graph(fakes, skip_diagnosis=True)
        self.assertEqual(fakes.calls, ["profile", "overlap", "gate"])
        self.assertEqual(state["status"], "failed")

    def test_a_run_resumed_after_its_first_check_keeps_the_diagnosis_out(self):
        # The killed check is made again on resume, so the stand-in fails twice.
        fakes = self.fakes(1, 1, 0)
        toolkit = fakes.toolkit(self.out)
        check = gate_node(toolkit)
        stop = {"gate": True}

        def dies(state):
            # Killed once the first check is made and before its route is taken.
            update = check(state)
            if stop.pop("gate", False):
                raise SystemExit("killed")
            return update

        with self.assertRaises(SystemExit):
            self.invoke(toolkit, self.operate(repairs=2, skip_diagnosis=True), nodes={"gate": dies})
        state = self.invoke(toolkit, nodes={"gate": dies}, resume=True)
        self.assertNotIn("diagnostics", fakes.calls)
        self.assertEqual(fakes.calls.count("engineer"), 1)
        self.assertEqual(state["status"], "passed")


class NoFixTest(FixCase, unittest.TestCase):
    def test_a_project_the_engine_cannot_prepare_is_not_fixed(self):
        (self.project / "dbt_project.yml").write_text("name: unpreparable\n")
        fakes = self.fakes(1)
        state = self.run_graph(fakes, repairs=1)
        self.assertNotIn("engineer", fakes.calls)
        self.assertEqual(state["status"], "failed")
        self.assertTrue(state["reason"].startswith("no fix: "), state["reason"])
        self.assertTrue(state["reason"].endswith("has a vars block without raw_dir"))
        self.assertIn("diagnosis", state["artefacts"])

    def test_a_fix_the_ceiling_cannot_hold_leaves_the_run_failed_with_its_diagnosis(self):
        fakes = self.fakes(1)
        toolkit = fakes.toolkit(self.out, budget=Budget(Policy(run_usd=Decimal("0.75"))))
        state = self.run_graph(fakes, toolkit=toolkit, repairs=1)
        self.assertNotIn("engineer", fakes.calls)
        self.assertEqual(state["status"], "failed")
        self.assertTrue(state["reason"].startswith("no fix made: stopped: budget"), state["reason"])
        self.assertIn("diagnosis", state["artefacts"])

    def test_a_resumed_run_fixes_without_diagnosing_again(self):
        fakes = self.fakes(1, 0)
        toolkit = fakes.toolkit(self.out)
        finish = report_node(toolkit)
        stop = {"report": True}

        def dies(state):
            if stop.pop("report", False):
                raise SystemExit("killed")
            return finish(state)

        with self.assertRaises(SystemExit):
            self.invoke(toolkit, self.operate(repairs=1), nodes={"report": dies})
        state = self.invoke(toolkit, nodes={"report": dies}, resume=True)
        self.assertEqual(fakes.calls.count("diagnostics"), 1)
        self.assertEqual(fakes.calls.count("engineer"), 1)
        self.assertEqual(state["status"], "passed")


if __name__ == "__main__":
    unittest.main()
