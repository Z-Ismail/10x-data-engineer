"""One diagnosis run read into a record, and the measurement loop, with stand-in runs."""

import json
import tempfile
import unittest
from pathlib import Path

from test_diagnosis_rates import answer, cause

from tenx import diagnosis_rates as rates
from tenx.compare import CostNotApproved
from tenx.domain import AgentFinding, ArtefactRef, CostRecord, Failure, ValidationResult

ANSWERS = {
    "project-01": {
        "variant": "refunds_dropped",
        "cause": "row filter",
        "origin": "planted",
        "model": "stg_billing__invoices",
        "failing": [["rows", "stg_billing__invoices", "fail"]],
    },
    "project-02": {
        "variant": "crosswalk_whole_email",
        "cause": "match rule",
        "origin": "observed",
        "model": "int_customers_matched_to_accounts",
        "failing": [["expected", "int_customers_matched_to_accounts", "fail"]],
    },
}


def write_bed(root: Path, answers=ANSWERS) -> Path:
    bed = root / "bed"
    for name in answers:
        folder = bed / "projects" / name
        folder.mkdir(parents=True)
        (folder / "dbt_project.yml").write_text(f"name: {name.replace('-', '_')}\n")
    (bed / "answers.json").write_text(json.dumps({"projects": answers}))
    return bed


def state(out: Path, failing, diagnosis=None, cost="0.0415", problem=None) -> dict:
    """A final state as the operate graph leaves it."""
    failures = tuple(Failure(check=c, subject=s, status=st) for c, s, st in failing)
    result = ValidationResult(
        validator="stand-in", passed=not failures, checks=24, failures=failures
    )
    found = {"validations": [result.dump()], "costs": [], "artefacts": {}, "findings": []}
    if cost is not None:
        record = CostRecord(
            run_id="r",
            node="diagnostics",
            role="diagnostics",
            attempt=1,
            backend="claude-code",
            outcome="success",
            # Claude Code can give a figure it calls unknown; it is not used.
            estimated_usd="0.04" if cost == "unknown" else cost,
            cost_basis="unknown" if cost == "unknown" else "estimate",
        )
        found["costs"].append(record.dump())
    if diagnosis is not None:
        folder = out / "diagnostics" / "attempt-1"
        folder.mkdir(parents=True)
        (folder / "DIAGNOSIS.md").write_text("# Diagnosis\n")
        (folder / "diagnosis.json").write_text(json.dumps(diagnosis))
        ref = ArtefactRef.of("diagnosis", folder / "DIAGNOSIS.md", attempt=1)
        found["artefacts"]["diagnosis"] = ref.dump()
    if problem is not None:
        finding = AgentFinding(
            agent="diagnostics", claim=f"no diagnosis: {problem}", kind="fact", confidence="high"
        )
        found["findings"].append(finding.dump())
    return found


class Runner:
    """Answers right for project-01; blames the wrong model for project-02."""

    def __init__(self, fail_on=()):
        self.calls = []
        self.fail_on = fail_on

    def __call__(self, out_root):
        def run(project, raw, goal, declaration, out):
            self.calls.append((Path(raw).name, project.name))
            if project.name in self.fail_on:
                raise RuntimeError("the sandbox could not start")
            truth = ANSWERS[project.name]
            out.mkdir(parents=True)
            model = truth["model"] if project.name == "project-01" else "stg_crm__accounts"
            given = answer(cause(truth["cause"], model))
            return state(out, truth["failing"], given)

        return run


class DiagnoseProjectTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def record(self, made: dict) -> dict:
        folder = self.root / "one"
        folder.mkdir(exist_ok=True)
        record = rates.diagnose_project(
            Path("p"),
            Path("raw"),
            Path("goal.md"),
            Path("checks.json"),
            folder,
            lambda *args: made,
        )
        self.assertEqual(json.loads((folder / "diagnosis-record.json").read_text()), record)
        return record

    def test_a_diagnosed_run(self):
        given = answer(cause("row filter", "stg_billing__invoices"))
        failing = [["total", "b", "fail"], ["rows", "a", "fail"]]
        record = self.record(state(self.root / "one" / "run", failing, given))
        self.assertEqual(
            record,
            {
                "gate": "FAIL",
                "failing": [["rows", "a", "fail"], ["total", "b", "fail"]],
                "answer": given,
                "called": True,
                "cost_usd": 0.0415,
                "problem": "",
            },
        )

    def test_a_refused_answer_and_an_unknown_cost(self):
        made = state(self.root, [["rows", "a", "fail"]], cost="unknown", problem="cause 1 names x")
        record = self.record(made)
        self.assertEqual(
            (record["answer"], record["called"], record["cost_usd"], record["problem"]),
            (None, True, None, "cause 1 names x"),
        )

    def test_a_passed_project_makes_no_call(self):
        record = self.record(
            state(self.root, [], cost=None, problem="the project passed every check")
        )
        self.assertEqual(
            (record["gate"], record["called"], record["problem"]),
            ("PASS", False, "the project passed every check"),
        )


class MeasureTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.bed = write_bed(self.root)
        (self.root / "goal.md").write_text("goal\n")
        (self.root / "checks.json").write_text("{}\n")
        self.datasets = {"s1": self.root / "s1", "s7": self.root / "s7"}
        self.out = self.root / "measure"
        self.said = []

    def measure(self, runner, **extra):
        return rates.measure(
            self.bed,
            self.datasets,
            self.root / "goal.md",
            self.root / "checks.json",
            self.out,
            runner,
            seed=3,
            parallel=1,
            progress=self.said.append,
            **extra,
        )

    def test_every_project_on_every_dataset_scored_and_reported(self):
        runner = Runner()
        text = self.measure(runner)
        self.assertEqual(
            sorted(runner.calls),
            [
                ("s1", "project-01"),
                ("s1", "project-02"),
                ("s7", "project-01"),
                ("s7", "project-02"),
            ],
        )
        self.assertEqual(
            [tuple(p) for p in runner.calls], rates.plan(["s1", "s7"], list(ANSWERS), 3)
        )
        self.assertTrue(text.startswith("Diagnosis measurement: unclear\n\nRight (first cause"))
        self.assertIn("Right (first cause names the cause and the model): 2 of 4 (50%,", text)
        self.assertIn("observed in live runs: 0 of 2", text)
        self.assertIn("Otherwise: model only 0, cause only 2, wrong 0", text)
        self.assertEqual(
            self.said[0],
            "4 diagnoses to do, estimated at US$0.32, at most US$1.60 at each call's cap (Claude Code's own estimates)",
        )
        data = json.loads((self.out / "report.json").read_text())
        self.assertEqual(
            (data["right"], data["by_origin"]), ([2, 4], {"observed": [0, 2], "planted": [2, 2]})
        )
        self.assertAlmostEqual(data["cost_usd"], 4 * 0.0415)
        self.assertEqual(rates.report(self.out), text)

    def test_a_failed_run_is_redone_on_the_next_start_and_only_it(self):
        first = Runner(fail_on=("project-02",))
        text = self.measure(first)
        self.assertIn("2 of 4 runs not done yet", text)
        self.assertTrue(any("error: the sandbox could not start" in line for line in self.said))
        second = Runner()
        self.measure(second)
        self.assertEqual(sorted(second.calls), [("s1", "project-02"), ("s7", "project-02")])

    def test_checks_other_than_the_listed_ones_are_noted(self):
        def runner(out_root):
            def run(project, raw, goal, declaration, out):
                out.mkdir(parents=True)
                return state(out, [["repeat", "x", "fail"]], None, None, "a crash")

            return run

        self.measure(runner)
        text = rates.report(self.out)
        self.assertIn(
            "no diagnosis (a crash; the validator failed other checks than the bed lists)", text
        )

    def test_above_the_limit_nothing_is_written_or_run(self):
        answers = {f"project-{n:02d}": {**ANSWERS["project-01"]} for n in range(1, 33)}
        self.bed = write_bed(self.root / "big", answers)
        runner = Runner()
        with self.assertRaisesRegex(CostNotApproved, r"64 diagnoses to do, estimated at US\$5\.12"):
            self.measure(runner)
        self.assertFalse(self.out.exists())
        self.assertEqual(runner.calls, [])

    def test_another_measurement_in_the_folder_is_refused(self):
        self.measure(Runner())
        (self.root / "goal.md").write_text("another goal\n")
        with self.assertRaisesRegex(rates.RatesError, "other projects, data or settings"):
            self.measure(Runner())

    def test_bad_answers_are_refused(self):
        broken = {"project-01": {**ANSWERS["project-01"], "cause": "other"}}
        self.bed = write_bed(self.root / "broken", broken)
        with self.assertRaisesRegex(rates.RatesError, "listed causes"):
            self.measure(Runner())


if __name__ == "__main__":
    unittest.main()
