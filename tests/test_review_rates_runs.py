"""Reviewing bed projects with the real gate and a stand-in claude.

The stand-in reviewer answers from what the project's models say, so each
bed variant gets a known answer: the contact name taken from the email is
reported at its line, event times cut to the day crash the session, invoice
times in milliseconds get a finding at a line the file does not have, and
anything else is approved. Its summary says whether it could read the bed's
answers, which the sandbox must hide.
"""

import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from review_bed import DECLARATION, write_bed
from test_gate_sandbox import needs_sandbox

from demo.generate import generate
from tenx import review_rates as rates
from tenx.adapters.duckdb import DuckDBGate, DuckDBProfiler
from tenx.backends.claude_code import ClaudeCode
from tenx.engineer import PLUGIN_COPY_DIR, PLUGIN_DIR, REVIEWER, mounts, stage_plugin
from tenx.sandbox import Bubblewrap, Unisolated, default_sandbox

ROOT = Path(__file__).resolve().parent.parent
GOAL = ROOT / "demo" / "goal.md"

STAND_IN = """
import json, os, sys
from pathlib import Path
calls = os.environ.get("STUB_CALLS")
if calls:
    with open(calls, "a") as log:
        log.write(os.getcwd() + "\\n")
models = {p.name: p.read_text() for p in Path("project/models").rglob("*.sql")}
if any("date_trunc" in text for text in models.values()):
    sys.exit(1)
try:
    Path(os.environ["STUB_SECRET"]).read_text()
    seen = "answers visible"
except OSError:
    seen = "answers hidden"
findings = []
customers = models["stg_billing__customers.sql"].splitlines()
for number, line in enumerate(customers, start=1):
    if "email as contact_name" in line:
        findings.append({
            "severity": "blocking", "model": "stg_billing__customers",
            "column": "contact_name", "file": "models/staging/stg_billing__customers.sql",
            "line": number, "claim": "The contact name is the email.", "evidence": line,
        })
if any("/ 1000" in text for text in models.values()):
    findings.append({
        "severity": "blocking", "model": "stg_billing__invoices", "column": "created_at",
        "file": "models/staging/stg_billing__invoices.sql", "line": 999,
        "claim": "Milliseconds.", "evidence": "/ 1000",
    })
verdict = "request changes" if findings else "approve"
print(json.dumps({
    "type": "result", "subtype": "success", "is_error": False, "num_turns": 15,
    "duration_ms": 40000, "total_cost_usd": 0.0612,
    "modelUsage": {"claude-sonnet-5-5": {
        "inputTokens": 12, "cacheCreationInputTokens": 15000,
        "cacheReadInputTokens": 60000, "outputTokens": 3000, "costBasis": "list"}},
    "permission_denials": [],
    "structured_output": {"verdict": verdict, "summary": seen, "findings": findings},
}))
"""


def stand_in(folder: Path) -> Path:
    folder.mkdir(parents=True)
    claude = folder / "claude"
    claude.write_text(f"#!{sys.executable}\n{STAND_IN}", encoding="utf-8")
    claude.chmod(claude.stat().st_mode | stat.S_IXUSR)
    return claude


class Bed:
    """A bed, a dataset and its profiles, a plugin copy and a stand-in, under root."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.raw = root / "data"
        generate(11, 20, self.raw, root / "generated")
        self.bed = root / "bed"
        answers = write_bed(self.bed, seed=3)["projects"]
        self.projects = {entry["variant"]: name for name, entry in answers.items()}
        self.answers = self.bed / "answers.json"
        self.out = root / "out"
        self.profiles = self.out / "profiles"
        DuckDBProfiler().profile(self.raw, self.profiles)
        stage_plugin(self.out / PLUGIN_COPY_DIR)
        self.claude = stand_in(root / "bin")

    def kit(self, sandbox) -> rates.Kit:
        def seen(work: Path):
            shown = mounts(work, self.out, DECLARATION, str(self.claude))
            return replace(shown, hidden=(*shown.hidden, self.bed))

        backend = ClaudeCode(str(self.claude), self.out / PLUGIN_COPY_DIR)
        return rates.Kit(
            validator=DuckDBGate(),
            backend=backend,
            role=REVIEWER,
            sandbox=sandbox,
            mounts=seen,
            hidden=(PLUGIN_DIR, self.bed, self.out),
            secrets=(self.answers,),
        )

    def review(self, variant: str, sandbox, folder: str) -> dict:
        project = self.bed / "projects" / self.projects[variant]
        return rates.review_project(
            project,
            self.raw,
            DECLARATION,
            GOAL,
            self.profiles,
            self.out / folder,
            self.kit(sandbox),
        )


class ReviewProjectTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.bed = Bed(Path(cls.tmp.name).resolve())
        cls.calls = cls.bed.root / "calls.txt"
        os.environ["STUB_CALLS"] = str(cls.calls)
        os.environ["STUB_SECRET"] = str(cls.bed.answers)

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("STUB_CALLS", None)
        os.environ.pop("STUB_SECRET", None)
        cls.tmp.cleanup()

    def review(self, variant: str) -> dict:
        return self.bed.review(variant, Unisolated(), variant)

    def test_a_defect_reported_at_its_line(self):
        record = self.review("contact_name_from_email")
        self.assertEqual(record["gate"], "PASS")
        self.assertEqual(record["answer"]["verdict"], "request changes")
        [finding] = record["answer"]["findings"]
        self.assertEqual(
            (finding["model"], finding["column"]), ("stg_billing__customers", "contact_name")
        )
        self.assertEqual(record["cost_usd"], 0.0612)
        folder = self.bed.out / "contact_name_from_email"
        saved = json.loads((folder / rates.REVIEW_RECORD).read_text())
        self.assertEqual(saved, record)
        packet = sorted(p.relative_to(folder / "packet") for p in (folder / "packet").rglob("*"))
        work = folder / "reviewer" / "work"
        self.assertEqual(sorted(p.relative_to(work) for p in work.rglob("*")), packet)
        self.assertNotIn(Path("answers.json"), packet)

    def test_a_good_project_approved(self):
        record = self.review("reference")
        self.assertEqual((record["answer"]["verdict"], record["problem"]), ("approve", ""))
        # Unisolated, the stand-in can read the answers: the positive control
        # for the sandboxed test below.
        self.assertEqual(record["answer"]["summary"], "answers visible")

    def test_a_crash_leaves_no_review(self):
        record = self.review("event_time_truncated_to_day")
        self.assertEqual(record["gate"], "PASS")
        self.assertIsNone(record["answer"])
        self.assertIsNone(record["cost_usd"])
        self.assertEqual(record["problem"], "the reviewer gave no report")

    def test_an_answer_that_fails_its_check_leaves_no_review(self):
        record = self.review("invoice_time_in_milliseconds")
        self.assertIsNone(record["answer"])
        self.assertIn("999", record["problem"])
        self.assertEqual(record["cost_usd"], 0.0612)

    def test_a_project_the_gate_fails_is_not_reviewed(self):
        # The reference project keeping test-mode invoices fails exactly
        # the rows and the money totals.
        project = self.bed.root / "keeps_test_mode"
        shutil.copytree(self.bed.bed / "projects" / self.bed.projects["reference"], project)
        invoices = project / "models" / "staging" / "stg_billing__invoices.sql"
        invoices.write_text(invoices.read_text().replace("where livemode = 'true'", ""))
        before = self.calls.read_text() if self.calls.exists() else ""
        record = rates.review_project(
            project,
            self.bed.raw,
            DECLARATION,
            GOAL,
            self.bed.profiles,
            self.bed.out / "keeps_test_mode",
            self.bed.kit(Unisolated()),
        )
        self.assertEqual(
            record,
            {
                "gate": "FAIL",
                "answer": None,
                "cost_usd": None,
                "problem": "the validator failed the project: rows, total",
            },
        )
        self.assertFalse((self.bed.out / "keeps_test_mode" / "reviewer").exists())
        after = self.calls.read_text() if self.calls.exists() else ""
        self.assertEqual(before, after)


@needs_sandbox
class SandboxedReviewTest(unittest.TestCase):
    """In bubblewrap the reviewer cannot read the bed's answers.

    The test's files are in the real home folder and the sandbox is told the
    home is a folder inside them, so only the bed's own place in the hidden
    paths keeps the answers out of sight.
    """

    def test_the_answers_are_hidden_from_the_reviewer(self):
        with tempfile.TemporaryDirectory(dir=Path.home()) as tmp:
            root = Path(tmp).resolve()
            bed = Bed(root)
            sandbox = Bubblewrap(default_sandbox().executable, home=root / "home")
            os.environ["STUB_SECRET"] = str(bed.answers)
            self.addCleanup(os.environ.pop, "STUB_SECRET", None)
            record = bed.review("contact_name_from_email", sandbox, "sandboxed")
            self.assertEqual(record["gate"], "PASS")
            self.assertEqual(record["answer"]["summary"], "answers hidden")
            self.assertEqual(record["answer"]["verdict"], "request changes")


if __name__ == "__main__":
    unittest.main()
