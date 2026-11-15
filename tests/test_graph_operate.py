"""The operate workflow of the graph: check an existing project, and diagnose a failure."""

import hashlib
import json
import os
import unittest
from decimal import Decimal
from pathlib import Path

from graph_fakes import Fakes, GraphCase

from tenx.domain import AgentFinding
from tenx.graph.build import ledger, report_node, steps_for
from tenx.graph.operate import copy_project
from tenx.policy import Budget, Policy

# The stand-in diagnosis, as DIAGNOSIS.md shows it.
DIAGNOSIS_TEXT = """# Diagnosis

Advice from the diagnostician on why the project failed the checks. It changes
no file and no verdict.

The orders model selects a constant, so its rows are wrong.

## Probable causes

1. `project/models/orders.sql:1`, `orders`, other: The model selects the number 1 instead of the orders.
   Evidence: Line 1: select 1.
   Explains: rows on orders.

## Confirmed facts

- The rows check failed on orders.
  Quoted from `PACKET.md`: `rows` on `orders`
"""


def tree(folder: Path) -> dict[str, str]:
    """Every entry under folder: a file's hash, a link's target, or 'folder'."""
    found = {}
    for path in sorted(folder.rglob("*")):
        name = path.relative_to(folder).as_posix()
        if path.is_symlink():
            found[name] = "link to " + os.readlink(path)
        elif path.is_dir():
            found[name] = "folder"
        elif path.is_file():
            found[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            found[name] = "special"
    return found


class OperateCase(GraphCase):
    def setUp(self):
        super().setUp()
        self.project = self.root / "project"
        (self.project / "models").mkdir(parents=True)
        (self.project / "dbt_project.yml").write_text("name: shop\n")
        (self.project / "models" / "orders.sql").write_text("select 1\n")
        # Not the stand-in overlap report's bytes, which a packet would refuse.
        self.declaration.write_text('{"checks": []}\n')

    def operate(self, **changes):
        fields = {"workflow": "operate", "project": str(self.project), "cross_source": False}
        return self.request(**{**fields, **changes})

    def run_graph(self, fakes, toolkit=None, **changes):
        return self.invoke(toolkit or fakes.toolkit(self.out), self.operate(**changes))

    def claims(self, state, agent="diagnostics") -> list[str]:
        return [
            AgentFinding.load(item).claim
            for item in state.get("findings") or []
            if item["agent"] == agent
        ]


class OperatePlanTest(OperateCase, unittest.TestCase):
    def test_the_operate_workflow_checks_the_project_then_diagnoses_it(self):
        self.assertEqual(
            steps_for(self.operate()),
            ("intake", "profile", "overlap", "gate", "diagnostics", "report"),
        )

    def test_the_build_workflow_is_unchanged(self):
        self.assertEqual(
            steps_for(self.request()),
            ("profile", "overlap", "cross-source", "engineer", "gate", "report"),
        )


class IntakeTest(OperateCase, unittest.TestCase):
    def test_the_copy_leaves_out_builds_and_special_files_and_never_follows_links(self):
        (self.project / "target").mkdir()
        (self.project / "target" / "manifest.json").write_text("{}\n")
        (self.project / "logs").mkdir()
        (self.project / "dev.duckdb").write_text("db\n")
        (self.project / "models" / "secret.sql").symlink_to(self.declaration)
        os.mkfifo(self.project / "models" / "pipe")
        destination = self.root / "copy"
        self.assertEqual(copy_project(self.project, destination), ["models/pipe"])
        self.assertEqual(
            tree(destination),
            {
                "dbt_project.yml": tree(self.project)["dbt_project.yml"],
                "models": "folder",
                "models/orders.sql": tree(self.project)["models/orders.sql"],
                "models/secret.sql": f"link to {self.declaration}",
            },
        )

    def test_the_run_checks_a_copy_and_leaves_the_project_as_it_was(self):
        os.mkfifo(self.project / "pipe")
        before = tree(self.project)
        fakes = Fakes()
        checked = []
        validate = fakes.validate
        fakes.validate = lambda project, *rest, **named: (
            checked.append(project) or validate(project, *rest, **named)
        )
        state = self.run_graph(fakes)
        self.assertEqual(tree(self.project), before)
        self.assertEqual(checked, [(self.out / "project").resolve()])
        self.assertEqual(
            sorted(tree(self.out / "project")), ["dbt_project.yml", "models", "models/orders.sql"]
        )
        self.assertEqual(
            self.claims(state, "intake"),
            ["the project's copy leaves out what is not a file, folder or link: pipe"],
        )


class DiagnosisTest(OperateCase, unittest.TestCase):
    def test_a_failed_project_is_diagnosed_from_its_packet(self):
        fakes = Fakes()
        fakes.verdict = False
        fakes.records_builds = True
        state = self.run_graph(fakes)
        self.assertEqual(fakes.calls, ["profile", "overlap", "gate", "diagnostics"])
        self.assertEqual(
            fakes.seen["diagnostics"],
            [
                "GOAL.md",
                "PACKET.md",
                "build",
                "build/dbt.log",
                "packet.json",
                "project",
                "project/dbt_project.yml",
                "project/models",
                "project/models/orders.sql",
                "source_profiles",
                "source_profiles/overlap.json",
                "source_profiles/overlap.txt",
                "source_profiles/shop.orders.json",
                "source_profiles/shop.orders.txt",
            ],
        )
        work = self.out / "diagnostics" / "attempt-1" / "work"
        self.assertEqual((work / "build" / "dbt.log").read_text(), "Running in <project>\nDone.\n")
        written = self.out / "diagnostics" / "attempt-1" / "DIAGNOSIS.md"
        self.assertEqual(written.read_text(), DIAGNOSIS_TEXT)
        self.assertEqual(state["artefacts"]["diagnosis"]["path"], str(written))
        self.assertEqual(state["status"], "failed")
        self.assertEqual(
            self.claims(state),
            [
                "the diagnosis: The orders model selects a constant, so its rows are wrong.",
                "probable cause, other: The model selects the number 1 instead of the orders.",
            ],
        )
        used = ledger(state, fakes.toolkit(self.out).roles, "none")
        self.assertEqual([role.role for role in used.roles], ["diagnostics"])
        self.assertEqual(used.gate, "FAIL")
        packet = json.loads((self.out / "diagnosis-packet" / "packet.json").read_text())
        self.assertIn("build/dbt.log", packet["files"])

    def test_a_validator_without_a_build_record_gives_a_packet_without_one(self):
        fakes = Fakes()
        fakes.verdict = False
        state = self.run_graph(fakes)
        self.assertNotIn("build/dbt.log", fakes.seen["diagnostics"])
        self.assertIn("diagnosis", state["artefacts"])

    def test_a_passed_project_is_not_diagnosed(self):
        fakes = Fakes()
        state = self.run_graph(fakes)
        self.assertEqual(fakes.calls, ["profile", "overlap", "gate"])
        self.assertEqual(state["status"], "passed")
        self.assertEqual(self.claims(state), ["no diagnosis: the project passed every check"])
        self.assertFalse((self.out / "diagnosis-packet").exists())


class NoDiagnosisTest(OperateCase, unittest.TestCase):
    def failing(self) -> Fakes:
        fakes = Fakes()
        fakes.verdict = False
        return fakes

    def assertNoDiagnosis(self, state, reason):
        self.assertEqual(state["status"], "failed")
        self.assertEqual(self.claims(state), [f"no diagnosis: {reason}"])
        self.assertNotIn("diagnosis", state["artefacts"])

    def test_an_answer_that_fails_its_check_gives_no_diagnosis(self):
        fakes = self.failing()
        wrong = json.loads(json.dumps(fakes.reports["diagnostics"]))
        wrong["structured_output"]["causes"][0]["explains"] = [
            {"check": "build", "subject": "project"}
        ]
        fakes.reports["diagnostics"] = wrong
        state = self.run_graph(fakes)
        self.assertNoDiagnosis(
            state, "cause 1 explains build on project, which did not fail or go unrun"
        )
        self.assertEqual([c["node"] for c in state["costs"]], ["diagnostics"])

    def test_a_crashed_diagnostician_is_charged_and_gives_no_diagnosis(self):
        fakes = self.failing()
        fakes.reports["diagnostics"] = None
        state = self.run_graph(fakes)
        self.assertNoDiagnosis(state, "the diagnostician gave no report")
        self.assertEqual([c["charged_usd"] for c in state["costs"]], ["0.40"])

    def test_a_copy_of_a_hidden_file_gives_no_packet_and_no_call(self):
        (self.project / "models" / "copy.sql").write_bytes(self.declaration.read_bytes())
        fakes = self.failing()
        state = self.run_graph(fakes)
        self.assertNotIn("diagnostics", fakes.calls)
        self.assertNoDiagnosis(
            state, "project/models/copy.sql has the same bytes as a file hidden from agents"
        )

    def test_a_copy_of_a_gate_file_gives_no_packet(self):
        # The stand-in gate writes built.duckdb holding these bytes.
        (self.project / "models" / "built.sql").write_text("tables\n")
        fakes = self.failing()
        state = self.run_graph(fakes)
        self.assertNotIn("diagnostics", fakes.calls)
        self.assertNoDiagnosis(
            state, "project/models/built.sql has the same bytes as a file hidden from agents"
        )

    def test_a_diagnosis_the_ceiling_cannot_hold_leaves_the_verdict(self):
        fakes = self.failing()
        # A run whose earlier spending leaves less than the diagnostician's cap.
        budget = Budget(Policy(run_usd=Decimal("0.75")))
        budget.reserve("engineer", "engineer")
        toolkit = fakes.toolkit(self.out, budget=budget)
        state = self.run_graph(fakes, toolkit=toolkit)
        self.assertNotIn("diagnostics", fakes.calls)
        self.assertEqual(state["status"], "failed")
        [claim] = self.claims(state)
        self.assertTrue(claim.startswith("no diagnosis: stopped: budget"), claim)

    def test_a_resumed_run_does_not_ask_again(self):
        fakes = self.failing()
        toolkit = fakes.toolkit(self.out)
        finish = report_node(toolkit)
        stop = {"report": True}

        def dies(state):
            if stop.pop("report", False):
                raise SystemExit("killed")
            return finish(state)

        with self.assertRaises(SystemExit):
            self.invoke(toolkit, self.operate(), nodes={"report": dies})
        state = self.invoke(toolkit, nodes={"report": dies}, resume=True)
        self.assertEqual(fakes.calls.count("diagnostics"), 1)
        self.assertEqual(state["status"], "failed")
        self.assertIn("diagnosis", state["artefacts"])


if __name__ == "__main__":
    unittest.main()
