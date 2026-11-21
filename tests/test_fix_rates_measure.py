"""One fixing run read into a record, pairs, and the measurement loop, with stand-in runs."""

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from test_diagnosis_rates import answer, cause
from test_diagnosis_rates_measure import ANSWERS, write_bed

from tenx import fix_rates as rates
from tenx.compare import CostNotApproved
from tenx.diagnosis_rates import RatesError
from tenx.domain import ArtefactRef, CostRecord, Failure, ValidationResult
from tenx.policy import Policy


def checked(failing) -> dict:
    failures = tuple(Failure(check=c, subject=s, status=st) for c, s, st in failing)
    return ValidationResult(
        validator="stand-in", passed=not failures, checks=24, failures=failures
    ).dump()


def cost(node: str, usd: str) -> dict:
    return CostRecord(
        run_id="r",
        node=node,
        role=node,
        attempt=1,
        backend="claude-code",
        outcome="success",
        estimated_usd="0.05" if usd == "unknown" else usd,
        cost_basis="unknown" if usd == "unknown" else "estimate",
    ).dump()


def state(out: Path, failing, after=(), diagnosis=None, diagnosis_usd="0.04", fix_usd="0.07"):
    """A final state as the operate graph leaves it after at most one fix.

    ``after`` is what the check of the fix found, or None when no fix was made.
    """
    found = {"validations": [checked(failing)], "costs": [], "artefacts": {}, "findings": []}
    found["status"] = "passed" if not failing else "failed"
    if diagnosis is not None:
        folder = out / "diagnostics" / "attempt-1"
        folder.mkdir(parents=True)
        (folder / "DIAGNOSIS.md").write_text("# Diagnosis\n")
        (folder / "diagnosis.json").write_text(json.dumps(diagnosis))
        found["artefacts"]["diagnosis"] = ArtefactRef.of(
            "diagnosis", folder / "DIAGNOSIS.md", attempt=1
        ).dump()
        found["costs"].append(cost("diagnostics", diagnosis_usd))
    if failing and after is not None:
        found["costs"].append(cost("engineer", fix_usd))
        found["validations"].append(checked(after))
        found["status"] = "passed" if not after else "failed"
        if after:
            found["reason"] = "the repairs allowed (1) were made"
    elif failing:
        found["reason"] = "no fix made: stopped: budget"
    return found


class Runner:
    """project-01 is fixed in both arms; project-02 only with the diagnosis."""

    def __init__(self, fail_on=()):
        self.calls = []
        self.fail_on = fail_on

    def __call__(self, out_root):
        def run(project, raw, goal, declaration, out, skip):
            self.calls.append((Path(raw).name, project.name, skip))
            if project.name in self.fail_on:
                raise RuntimeError("the sandbox could not start")
            truth = ANSWERS[project.name]
            out.mkdir(parents=True)
            given = None if skip else answer(cause(truth["cause"], truth["model"]))
            fixed = project.name == "project-01" or not skip
            after = () if fixed else truth["failing"]
            return state(out, truth["failing"], after, given)

        return run


class FixProjectTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.skips = []

    def record(self, made: dict, arm="with") -> dict:
        folder = self.root / "one"
        folder.mkdir(exist_ok=True)

        def run(*args):
            self.skips.append(args[-1])
            return made

        record = rates.fix_project(
            Path("p"), Path("raw"), Path("goal.md"), Path("checks.json"), folder, run, arm
        )
        self.assertEqual(json.loads((folder / "fix-record.json").read_text()), record)
        return record

    def test_a_fix_with_the_diagnosis_that_passed(self):
        given = answer(cause("row filter", "stg_billing__invoices"))
        failing = [["total", "b", "fail"], ["rows", "a", "fail"]]
        record = self.record(state(self.root / "one" / "run", failing, (), given))
        self.assertEqual(self.skips, [False])
        self.assertEqual(
            record,
            {
                "arm": "with",
                "first": "FAIL",
                "failing": [["rows", "a", "fail"], ["total", "b", "fail"]],
                "last": "PASS",
                "fix_made": True,
                "fixed": True,
                "answer": given,
                "cost_diagnosis": 0.04,
                "cost_fix": 0.07,
                "cost_usd": 0.04 + 0.07,
                "problem": "",
            },
        )

    def test_a_fix_without_the_diagnosis_that_still_failed(self):
        failing = [["rows", "a", "fail"]]
        record = self.record(state(self.root, failing, failing), arm="without")
        self.assertEqual(self.skips, [True])
        self.assertEqual(
            (record["fixed"], record["last"], record["cost_diagnosis"], record["cost_usd"]),
            (False, "FAIL", 0.0, 0.07),
        )
        self.assertEqual(
            record["problem"], "the fix still failed: the repairs allowed (1) were made"
        )

    def test_no_fix_made_and_a_project_that_passed_as_handed_over(self):
        record = self.record(state(self.root, [["rows", "a", "fail"]], None), arm="without")
        self.assertEqual((record["fixed"], record["fix_made"]), (False, False))
        self.assertEqual(record["problem"], "no fix made: stopped: budget")
        record = self.record(state(self.root, []), arm="without")
        self.assertEqual((record["first"], record["fixed"]), ("PASS", False))
        self.assertEqual(record["problem"], "the project passed as handed over")

    def test_an_unknown_cost_leaves_the_run_cost_unknown(self):
        made = state(self.root / "one" / "run", [["rows", "a", "fail"]], (), answer(), "unknown")
        record = self.record(made)
        self.assertEqual((record["cost_diagnosis"], record["cost_usd"]), (None, None))
        self.assertEqual(record["cost_fix"], 0.07)

    def test_an_unknown_arm_is_refused(self):
        with self.assertRaises(RatesError):
            self.record({}, arm="both")


class PlanTest(unittest.TestCase):
    def test_every_pair_once_in_both_arms_together(self):
        order = rates.plan(["s1", "s7"], ["project-02", "project-01"], seed=3)
        self.assertEqual(len(order), 8)
        pairs = [order[i][:2] for i in range(0, 8, 2)]
        self.assertEqual([order[i + 1][:2] for i in range(0, 8, 2)], pairs)
        self.assertEqual(
            sorted(pairs),
            [
                ("s1", "project-01"),
                ("s1", "project-02"),
                ("s7", "project-01"),
                ("s7", "project-02"),
            ],
        )
        for i in range(0, 8, 2):
            self.assertEqual({order[i][2], order[i + 1][2]}, {"with", "without"})
        self.assertEqual(order, rates.plan(["s1", "s7"], ["project-01", "project-02"], seed=3))
        self.assertNotEqual(order, rates.plan(["s1", "s7"], ["project-01", "project-02"], seed=4))

    def test_the_estimate_pays_for_the_diagnosis_only_with_it(self):
        typical, most = rates.estimate([("s1", "p", "with"), ("s1", "p", "without")], Policy())
        self.assertEqual(typical, Decimal("0.48"))
        self.assertEqual(most, Decimal("1.90"))


class MeasureTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.bed = write_bed(self.root)
        self.data = {}
        for name in ("s1", "s7"):
            (self.root / name).mkdir()
            self.data[name] = self.root / name
        (self.root / "goal.md").write_text("goal\n")
        (self.root / "checks.json").write_text("{}\n")
        self.out = self.root / "measure"

    def measure(self, runner, **options):
        return rates.measure(
            self.bed,
            self.data,
            self.root / "goal.md",
            self.root / "checks.json",
            self.out,
            runner,
            parallel=1,
            progress=lambda line: None,
            **options,
        )

    def test_every_pair_is_run_in_both_arms_and_reported(self):
        runner = Runner()
        text = self.measure(runner)
        self.assertEqual(len(runner.calls), 8)
        self.assertEqual(
            sorted(runner.calls),
            sorted(
                (d, p, skip)
                for d in ("s1", "s7")
                for p in ("project-01", "project-02")
                for skip in (False, True)
            ),
        )
        self.assertTrue(text.startswith("Fix measurement: unclear\n"))
        self.assertIn(
            "  both fixed 2, only with the diagnosis 2, only without 0, neither 0\n", text
        )
        self.assertIn("crosswalk_whole_email         observed  with 2 of 2, without 0 of 2\n", text)
        self.assertIn("US$ per passing fix (the diagnosis included): with US$0.1100", text)
        self.assertIn("without US$0.1400", text)
        self.assertEqual((self.out / "report.txt").read_text(), text)
        data = json.loads((self.out / "report.json").read_text())
        self.assertEqual(
            data["pairs"], {"both": 2, "with_only": 2, "without_only": 0, "neither": 0}
        )
        self.assertEqual({p["diagnosis"] for p in data["pairs_found"]}, {"right"})

    def test_a_resume_runs_only_what_did_not_finish(self):
        self.measure(Runner(fail_on=("project-02",)))
        text = rates.report(self.out)
        self.assertIn("2 of 4 pairs not done yet", text)
        again = Runner()
        self.measure(again)
        self.assertEqual(sorted({call[1] for call in again.calls}), ["project-02"])
        self.assertEqual(len(again.calls), 4)
        self.assertNotIn("not done yet", rates.report(self.out))

    def test_other_settings_are_refused(self):
        self.measure(Runner())
        with self.assertRaises(RatesError):
            self.measure(Runner(), seed=9)

    def test_a_measurement_over_the_limit_needs_approval_and_writes_nothing(self):
        many = {f"d{i}": self.data["s1"] for i in range(6)}
        with self.assertRaises(CostNotApproved) as caught:
            rates.measure(
                self.bed,
                many,
                self.root / "goal.md",
                self.root / "checks.json",
                self.out,
                Runner(),
                progress=lambda line: None,
            )
        self.assertIn("24 fixing runs to do, estimated at US$5.76", str(caught.exception))
        self.assertFalse(self.out.exists())

    def test_each_pair_is_read_from_its_own_dataset(self):
        self.measure(Runner())
        for dataset in ("s1", "s7"):
            path = self.out / "runs" / dataset / "project-01" / "with" / "fix-record.json"
            record = json.loads(path.read_text())
            record["fixed"] = dataset == "s7"
            path.write_text(json.dumps(record))
        text = rates.report(self.out)
        self.assertIn(
            "refunds_dropped               s1          with not fixed, without fixed", text
        )
        self.assertIn("refunds_dropped               s7          with fixed, without fixed", text)

    def test_a_pair_the_validator_found_differently_is_flagged(self):
        self.measure(Runner())
        record_path = self.out / "runs" / "s1" / "project-01" / "without" / "fix-record.json"
        record = json.loads(record_path.read_text())
        record["failing"] = [["rows", "other", "fail"]]
        record_path.write_text(json.dumps(record))
        text = rates.report(self.out)
        self.assertIn("(the validator found the project differently in the two runs)", text)


if __name__ == "__main__":
    unittest.main()
