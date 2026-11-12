"""The measurement loop with stand-in ports: order, resume, pricing and the report."""

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from tenx import review_rates as rates
from tenx.compare import CostNotApproved
from tenx.ports import AgentRun, CheckResult, PlatformError, Role, ValidationReport

ROLE = Role("reviewer", "tenx:reviewer", "Review.", (), 20, kind="cross-source")
SQL = "select\n    email as contact_name\nfrom x\n"
FINDING = {
    "severity": "blocking",
    "model": "stg_a",
    "column": "contact_name",
    "file": "models/stg_a.sql",
    "line": 2,
    "claim": "c",
    "evidence": "e",
}


class Validator:
    name = "stand-in"

    def capabilities(self):
        return frozenset()

    def validate(self, project, raw, declaration, workdir, sandbox=None, hidden=()):
        Path(workdir).mkdir(parents=True)
        return ValidationReport((CheckResult("rows", "stg_a", "pass", "", {}, "ok"),), True, "")


class Backend:
    """Asks for changes when the model says ``email as``; fails on ``broken``."""

    name = "stand-in"

    def __init__(self):
        self.calls = []

    def capabilities(self):
        return frozenset()

    def run(self, role, workspace, report_file, timeout, sandbox=None, mounts=None):
        text = (Path(workspace) / "project" / "models" / "stg_a.sql").read_text()
        self.calls.append(text)
        if "broken" in text:
            raise PlatformError("claude stopped")
        found = [FINDING] if "email as" in text else []
        answer = {"verdict": "request changes" if found else "approve", "summary": "s"}
        report = {
            "type": "result",
            "subtype": "success",
            "num_turns": 3,
            "total_cost_usd": 0.05,
            "modelUsage": {"m": {"costBasis": "list"}},
            "structured_output": {**answer, "findings": found},
        }
        Path(report_file).parent.mkdir(parents=True, exist_ok=True)
        Path(report_file).write_text(json.dumps(report))
        return AgentRun(0, report)


class Profiler:
    def __init__(self):
        self.profiled = []

    def profile(self, raw, out):
        self.profiled.append(Path(raw).name)
        Path(out).mkdir(parents=True)
        (Path(out) / "a.txt").write_text("profile\n")
        return []


def bed(root: Path, projects: dict[str, tuple[str, str]]) -> Path:
    """projects: name -> (kind, SQL of stg_a)."""
    answers = {}
    for name, (kind, sql) in projects.items():
        models = root / "bed" / "projects" / name / "models"
        models.mkdir(parents=True)
        (models / "stg_a.sql").write_text(sql)
        (models.parent / "dbt_project.yml").write_text("name: p\n")
        columns = [["stg_a", "contact_name"]] if kind == "defect" else []
        answers[name] = {"variant": f"v-{name}", "kind": kind, "columns": columns}
    (root / "bed" / "answers.json").write_text(json.dumps({"projects": answers}))
    return root / "bed"


class MeasureTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.bed = bed(
            self.root,
            {"p1": ("defect", SQL), "p2": ("good", "select\n    name\nfrom x\n")},
        )
        self.datasets = {}
        for name in ("s1", "s2"):
            (self.root / name).mkdir()
            self.datasets[name] = self.root / name
        self.goal = self.root / "GOAL.md"
        self.goal.write_text("goal\n")
        self.declaration = self.root / "checks.json"
        self.declaration.write_text("{}\n")
        self.out = self.root / "out"
        self.backend, self.profiler, self.lines = Backend(), Profiler(), []

    def kit(self, out):
        return rates.Kit(Validator(), self.backend, ROLE, None, lambda work: None)

    def measure(self, **options):
        return rates.measure(
            self.bed,
            self.datasets,
            self.goal,
            self.declaration,
            self.out,
            self.kit,
            self.profiler,
            parallel=1,
            progress=self.lines.append,
            **options,
        )

    def test_every_project_on_every_dataset_scored(self):
        text = self.measure()
        self.assertEqual(len(self.backend.calls), 4)
        self.assertEqual(sorted(self.profiler.profiled), ["s1", "s2"])
        self.assertIn("Caught (blocking, right model and column): 2 of 2", text)
        self.assertIn("Good projects rejected: 0 of 2", text)
        self.assertEqual(
            self.lines[0],
            (
                "4 reviews to do, estimated at US$0.32, at most US$1.60 at each call's cap "
                "(Claude Code's own estimates)"
            ),
        )
        self.assertEqual(len(self.lines), 5)
        self.assertEqual((self.out / rates.REPORT_TEXT).read_text(), text)
        saved = json.loads((self.out / rates.REPORT_JSON).read_text())
        self.assertEqual((saved["catch"], saved["rejected"]), ([2, 2], [0, 2]))
        self.assertAlmostEqual(saved["cost_usd"], 0.20)
        self.assertEqual(rates.report(self.out), text)

    def test_order_is_shuffled_by_the_seed_and_repeatable(self):
        first = rates.plan(["s1", "s2"], ["p1", "p2", "p3"], seed=0)
        self.assertEqual(first, rates.plan(["s1", "s2"], ["p3", "p2", "p1"], seed=0))
        self.assertEqual(sorted(first), sorted(rates.plan(["s1", "s2"], ["p1", "p2", "p3"], 1)))
        self.assertNotEqual(first, rates.plan(["s1", "s2"], ["p1", "p2", "p3"], seed=1))

    def test_resume_reviews_only_what_has_no_record(self):
        broken = self.bed / "projects" / "p2" / "models" / "stg_a.sql"
        good = broken.read_text()
        broken.write_text("broken\n")
        text = self.measure()
        self.assertEqual(len(self.backend.calls), 4)
        self.assertIn("2 of 4 reviews not done yet", text)
        errors = [line for line in self.lines if "error: claude stopped" in line]
        self.assertEqual(len(errors), 2)
        broken.write_text(good)
        self.backend.calls.clear()
        self.lines.clear()
        text = self.measure()
        self.assertEqual(self.backend.calls, [good, good])
        self.assertTrue(self.lines[0].startswith("2 reviews to do"))
        self.assertNotIn("not done yet", text)
        self.assertEqual(sorted(self.profiler.profiled), ["s1", "s2"])

    def test_other_settings_are_refused_on_resume(self):
        self.measure()
        self.goal.write_text("another goal\n")
        with self.assertRaises(rates.RatesError) as raised:
            self.measure()
        self.assertIn("other projects, data or settings", str(raised.exception))

    def test_above_the_limit_nothing_starts_without_approval(self):
        # 63 reviews at US$0.08 is US$5.04, above the US$5 limit.
        self.assertEqual(rates.estimate(63, rates.Policy(), ROLE)[0], Decimal("5.04"))
        self.datasets = {f"s{n}": self.root / "s1" for n in range(32)}
        projects = {f"p{n}": ("good", "select\n    name\nfrom x\n") for n in range(2)}
        self.bed = bed(self.root / "big", projects)
        with self.assertRaises(CostNotApproved) as raised:
            self.measure()
        self.assertIn("64 reviews to do, estimated at US$5.12", str(raised.exception))
        self.assertFalse(self.out.exists())
        self.assertEqual(self.backend.calls, [])
        self.measure(approve_cost=True)
        self.assertEqual(len(self.backend.calls), 64)

    def test_bad_answers_files_are_refused(self):
        (self.bed / "answers.json").write_text(
            json.dumps({"projects": {"p9": {"variant": "v", "kind": "good"}}})
        )
        with self.assertRaises(rates.RatesError) as raised:
            self.measure()
        self.assertIn("no project p9", str(raised.exception))
        (self.bed / "answers.json").write_text("not json")
        with self.assertRaises(rates.RatesError):
            self.measure()


if __name__ == "__main__":
    unittest.main()
