"""Tests for the records a run passes between its steps."""

import hashlib
import shutil
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from pydantic import ValidationError

from tenx.domain import (
    AgentFinding,
    ArtefactRef,
    CostRecord,
    EngineeringRequest,
    Evidence,
    ExecutionPlan,
    Record,
    RecordError,
    ResourceBudget,
    Usd,
)
from tenx.ledger import role_usage
from tenx.policy import Budget, Policy

HASH = "a" * 64


class Priced(Record):
    usd: Usd


def is_json_only(value) -> bool:
    if value is None or isinstance(value, (str, bool, int)):
        return not isinstance(value, float)
    if isinstance(value, list):
        return all(is_json_only(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and is_json_only(v) for k, v in value.items())
    return False


def finding(**changes) -> dict:
    value = {
        "agent": "cross-source",
        "claim": "billing customers match CRM accounts on the email domain",
        "kind": "inference",
        "evidence": [{"kind": "file", "ref": "overlap.txt", "detail": "79% of domains found"}],
        "confidence": "high",
        "assets": ["customers.email", "accounts.email"],
        "next_action": "match on the domain after lower case and trimming",
        "risks": ["free mail domains would match unrelated customers"],
        "open_questions": [],
    }
    value.update(changes)
    return value


class AmountTest(unittest.TestCase):
    def test_amounts_follow_the_policy_rule(self):
        self.assertEqual(Priced.load({"usd": "0.10"}).usd, Decimal("0.10"))
        self.assertEqual(Priced.load({"usd": "0.10"}).dump(), {"usd": "0.10"})
        # A float is read through its shortest text, not its binary value.
        self.assertEqual(Priced.load({"usd": 0.1}).usd, Decimal("0.1"))
        self.assertEqual(Priced.load({"usd": 2}).dump(), {"usd": "2"})

    def test_bad_amounts_are_refused_with_the_field(self):
        for bad in ["1e3", "-1", "NaN", True, "0.12345", "", None, [1]]:
            with self.subTest(bad=bad), self.assertRaises(RecordError) as caught:
                Priced.load({"usd": bad})
            self.assertIn("not a valid Priced: usd:", str(caught.exception))


class RecordTest(unittest.TestCase):
    def test_a_finding_reads_back_equal_and_is_json_only(self):
        record = AgentFinding.load(finding())
        dumped = record.dump()
        self.assertTrue(is_json_only(dumped))
        self.assertEqual(dumped, finding())
        self.assertEqual(AgentFinding.load(dumped), record)
        self.assertEqual(
            record.evidence,
            (Evidence(kind="file", ref="overlap.txt", detail="79% of domains found"),),
        )

    def test_defaults_are_written_out(self):
        record = AgentFinding.load(
            {"agent": "qa", "claim": "c", "kind": "fact", "confidence": "low"}
        )
        self.assertEqual(
            record.dump(),
            {
                "agent": "qa",
                "claim": "c",
                "kind": "fact",
                "evidence": [],
                "confidence": "low",
                "assets": [],
                "next_action": "",
                "risks": [],
                "open_questions": [],
            },
        )

    def test_records_cannot_be_changed(self):
        record = AgentFinding.load(finding())
        with self.assertRaises(ValidationError):
            record.claim = "something else"

    def test_wrong_values_are_refused_with_the_field(self):
        cases = {
            "kind: Input should be": finding(kind="guess"),
            "confidence: Input should be": finding(confidence="certain"),
            "claim: String should have at least 1 character": finding(claim=""),
            "agent: Input should be a valid string": finding(agent=7),
            "evidence: Input should be a valid array": finding(evidence={"kind": "file"}),
            "evidence.0.kind: Input should be": finding(evidence=[{"kind": "log", "ref": "x"}]),
            "evidence.0.extra: Extra inputs are not permitted": finding(
                evidence=[{"kind": "file", "ref": "x", "extra": 1}]
            ),
            "surprise: Extra inputs are not permitted": finding(surprise=True),
            "assets.0: Input should be a valid string": finding(assets=[1]),
        }
        for expected, value in cases.items():
            with self.subTest(expected=expected), self.assertRaises(RecordError) as caught:
                AgentFinding.load(value)
            self.assertIn(expected, str(caught.exception))
        missing = finding()
        del missing["claim"]
        with self.assertRaises(RecordError) as caught:
            AgentFinding.load(missing)
        self.assertIn("claim: Field required", str(caught.exception))

    def test_values_that_are_not_json_are_refused(self):
        for bad in [Decimal(1), {1, 2}, float("nan"), Path("x")]:
            with self.subTest(bad=bad), self.assertRaises(RecordError) as caught:
                AgentFinding.load(finding(next_action=bad))
            self.assertIn("not a valid AgentFinding: not JSON", str(caught.exception))


class ArtefactRefTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder)

    def ref(self, **changes) -> dict:
        value = {"name": "goal", "path": "/x/goal.md", "kind": "file", "sha256": HASH, "attempt": 1}
        value.update(changes)
        return value

    def test_counts_and_hashes_are_checked(self):
        for field, bad in [
            ("attempt", True),
            ("attempt", "1"),
            ("attempt", 1.0),
            ("attempt", -1),
            ("sha256", "A" * 64),
            ("sha256", "a" * 63),
            ("kind", "link"),
            ("path", ""),
        ]:
            with self.subTest(field=field, bad=bad), self.assertRaises(RecordError) as caught:
                ArtefactRef.load(self.ref(**{field: bad}))
            self.assertIn(f"not a valid ArtefactRef: {field}:", str(caught.exception))
        self.assertEqual(ArtefactRef.load(self.ref()).dump(), self.ref())
        self.assertTrue(is_json_only(ArtefactRef.load(self.ref()).dump()))

    def test_a_file_is_referred_to_by_the_hash_of_its_bytes(self):
        path = self.folder / "goal.md"
        path.write_bytes(b"Build the staging models.\n")
        ref = ArtefactRef.of("goal", path, attempt=2)
        expected = hashlib.sha256(b"Build the staging models.\n").hexdigest()
        self.assertEqual(ref.dump(), self.ref(path=str(path), sha256=expected, attempt=2))
        self.assertTrue(ref.unchanged())
        path.write_bytes(b"Build the marts.\n")
        self.assertFalse(ref.unchanged())
        path.unlink()
        self.assertFalse(ref.unchanged())

    def test_a_folder_hash_covers_each_file_and_its_place(self):
        project = self.folder / "project"
        (project / "models").mkdir(parents=True)
        (project / "dbt_project.yml").write_bytes(b"name: demo\n")
        (project / "models" / "stg.sql").write_bytes(b"select 1\n")
        (project / "models" / ".stg.sql.123.tmp").write_bytes(b"half written")
        first = hashlib.sha256(b"name: demo\n").hexdigest()
        second = hashlib.sha256(b"select 1\n").hexdigest()
        listing = f'{{"dbt_project.yml":"{first}","models/stg.sql":"{second}"}}'
        ref = ArtefactRef.of("project", project)
        self.assertEqual(ref.kind, "folder")
        self.assertEqual(ref.attempt, 0)
        self.assertEqual(ref.sha256, hashlib.sha256(listing.encode()).hexdigest())
        self.assertTrue(ref.unchanged())
        # The same bytes in another place are another folder.
        (project / "models" / "stg.sql").rename(project / "stg.sql")
        self.assertFalse(ref.unchanged())

    def test_a_missing_path_is_refused(self):
        with self.assertRaises(FileNotFoundError):
            ArtefactRef.of("goal", self.folder / "missing.md")


def report(cost, basis="list", subtype="success") -> dict:
    return {
        "subtype": subtype,
        "num_turns": 18,
        "duration_ms": 34600,
        "total_cost_usd": cost,
        "permission_denials": [],
        "modelUsage": {
            "claude-sonnet": {
                "inputTokens": 20,
                "cacheCreationInputTokens": 5000,
                "cacheReadInputTokens": 120000,
                "outputTokens": 3000,
                "costBasis": basis,
            },
            "claude-haiku": {
                "inputTokens": 1177,
                "cacheCreationInputTokens": 0,
                "cacheReadInputTokens": 0,
                "outputTokens": 40,
                "costBasis": "list",
            },
        },
    }


def settled(kind, usage):
    budget = Budget()
    reservation = budget.reserve(usage.role, kind)
    return budget.settle(reservation, usage.cost_usd, usage.cost_known)


class CostRecordTest(unittest.TestCase):
    def record(self, usage, settlement=None) -> CostRecord:
        return CostRecord.of("run-1", "engineer", 2, "claude-code", usage, settlement)

    def test_a_measured_call_is_charged_its_cost(self):
        usage = role_usage("engineer", report(0.1116))
        record = self.record(usage, settled("engineer", usage))
        self.assertEqual(
            record.dump(),
            {
                "run_id": "run-1",
                "node": "engineer",
                "role": "engineer",
                "attempt": 2,
                "backend": "claude-code",
                "outcome": "success",
                "turns": 18,
                "duration_ms": 34600,
                "refused_calls": 0,
                "models": [
                    {
                        "model": "claude-sonnet",
                        "input": 20,
                        "cache_write": 5000,
                        "cache_read": 120000,
                        "output": 3000,
                        "cost_basis": "list",
                    },
                    {
                        "model": "claude-haiku",
                        "input": 1177,
                        "cache_write": 0,
                        "cache_read": 0,
                        "output": 40,
                        "cost_basis": "list",
                    },
                ],
                "estimated_usd": "0.1116",
                "cost_basis": "estimate",
                "reserved_usd": "0.75",
                "charged_usd": "0.1116",
                "overshoot_usd": "0",
            },
        )
        self.assertEqual(CostRecord.load(record.dump()), record)

    def test_a_call_past_its_cap_shows_the_overshoot(self):
        # 0.8123 against the engineer's cap of 0.75: 0.0623 over.
        usage = role_usage("engineer", report(0.8123, subtype="error_max_budget_usd"))
        dumped = self.record(usage, settled("engineer", usage)).dump()
        self.assertEqual(
            (dumped["charged_usd"], dumped["overshoot_usd"], dumped["outcome"]),
            ("0.8123", "0.0623", "error_max_budget_usd"),
        )
        # A cost reported with more places than the policy's keeps them all.
        usage = role_usage("scout", report(0.0455485))
        dumped = self.record(usage, settled("scout", usage)).dump()
        self.assertEqual((dumped["charged_usd"], dumped["overshoot_usd"]), ("0.0455485", "0"))

    def test_a_call_with_no_known_cost_is_charged_its_reservation(self):
        for usage in [
            role_usage("engineer", None, "timed out"),
            role_usage("engineer", report(0.2, basis="unknown")),
        ]:
            with self.subTest(outcome=usage.outcome):
                dumped = self.record(usage, settled("engineer", usage)).dump()
                self.assertEqual((dumped["estimated_usd"], dumped["cost_basis"]), (None, "unknown"))
                self.assertEqual((dumped["charged_usd"], dumped["overshoot_usd"]), ("0.75", "0"))
        dumped = self.record(role_usage("engineer", None, "timed out")).dump()
        self.assertEqual((dumped["turns"], dumped["models"]), (None, []))

    def test_a_call_not_reserved_has_no_settlement(self):
        dumped = self.record(role_usage("engineer", report(0.1))).dump()
        self.assertEqual(
            (dumped["reserved_usd"], dumped["charged_usd"], dumped["overshoot_usd"]),
            (None, None, None),
        )

    def test_figures_that_do_not_add_up_are_refused(self):
        usage = role_usage("engineer", report(0.1116))
        good = self.record(usage, settled("engineer", usage)).dump()
        for changes in [
            {"charged_usd": "0.75"},
            {"overshoot_usd": "0.01"},
            {"charged_usd": None},
            {"reserved_usd": "0.10"},
            {"cost_basis": "unknown"},
        ]:
            with self.subTest(changes=changes), self.assertRaises(RecordError):
                CostRecord.load({**good, **changes})
        for field, bad in [("turns", True), ("estimated_usd", "-0.1"), ("cost_basis", "list")]:
            with self.subTest(field=field), self.assertRaises(RecordError) as caught:
                CostRecord.load({**good, field: bad})
            self.assertIn(f"CostRecord: {field}:", str(caught.exception))


class ResourceBudgetTest(unittest.TestCase):
    def test_the_policy_reads_back_equal(self):
        rules = Policy(
            run_usd=Decimal("3.5"),
            call_usd={
                "engineer": Decimal("1.25"),
                "cross-source": Decimal("0.3"),
                "scout": Decimal(0),
            },
            typical_usd={
                "engineer": Decimal("0.21"),
                "cross-source": Decimal("0.07"),
                "scout": Decimal(0),
            },
            approve_above_usd=Decimal(7),
        )
        for policy in [Policy(), rules]:
            with self.subTest(policy=policy):
                budget = ResourceBudget.of(policy)
                self.assertEqual(budget.to_policy(), policy)
                self.assertEqual(ResourceBudget.load(budget.dump()), budget)
        self.assertEqual(ResourceBudget.of(rules).dump()["call_usd"]["engineer"], "1.25")

    def test_a_policy_that_could_not_run_is_refused(self):
        good = ResourceBudget.of(Policy()).dump()
        cases = [
            ({"call_usd": {**good["call_usd"], "engineer": "3.00"}}, "more than the run's ceiling"),
            ({"call_usd": {**good["call_usd"], "architect": "0.40"}}, "unknown kinds"),
            ({"typical_usd": {"engineer": "0.20"}}, "is missing"),
            ({"run_usd": "1e3"}, "run_usd:"),
        ]
        for changes, expected in cases:
            with self.subTest(expected=expected), self.assertRaises(RecordError) as caught:
                ResourceBudget.load({**good, **changes})
            self.assertIn(expected, str(caught.exception))


def request(**changes) -> dict:
    value = {
        "run_id": "run-1",
        "raw": "demo/data",
        "goal": "demo/goal.md",
        "declaration": "tests/fixtures/demo_gate.json",
        "out": "runs/demo",
        "cross_source": True,
        "scouts": False,
        "isolation": "bubblewrap",
    }
    value.update(changes)
    return value


class RequestAndPlanTest(unittest.TestCase):
    def test_a_request_reads_back_equal(self):
        record = EngineeringRequest.load(request())
        self.assertEqual(record.dump(), request())
        short = {
            k: v for k, v in request().items() if k not in ("cross_source", "scouts", "isolation")
        }
        self.assertEqual(EngineeringRequest.load(short), record)

    def test_a_wrong_request_is_refused(self):
        missing = request()
        del missing["declaration"]
        for value, expected in [
            (request(cross_source="yes"), "cross_source:"),
            (request(isolation="none"), "isolation:"),
            (request(raw=""), "raw:"),
            (missing, "declaration: Field required"),
        ]:
            with self.subTest(expected=expected), self.assertRaises(RecordError) as caught:
                EngineeringRequest.load(value)
            self.assertIn(expected, str(caught.exception))

    def test_a_plan_takes_its_steps_once_in_order(self):
        steps = ["profile", "overlap", "cross-source", "engineer", "gate", "report"]
        plan = ExecutionPlan.load({"workflow": "build", "steps": steps})
        self.assertEqual(plan.steps, tuple(steps))
        self.assertEqual(plan.dump(), {"workflow": "build", "steps": steps})
        for steps, expected in [
            (["engineer", "profile"], "got engineer, profile"),
            (["profile", "profile"], "got profile, profile"),
            (["profile", "deploy"], "steps.1:"),
            ([], "steps:"),
        ]:
            with self.subTest(steps=steps), self.assertRaises(RecordError) as caught:
                ExecutionPlan.load({"workflow": "build", "steps": steps})
            self.assertIn(expected, str(caught.exception))
        with self.assertRaises(RecordError):
            ExecutionPlan.load({"workflow": "operate", "steps": ["profile"]})


if __name__ == "__main__":
    unittest.main()
