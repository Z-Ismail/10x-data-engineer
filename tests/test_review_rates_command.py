"""``python -m tenx.review_rates`` on the whole bed with the real gate and a stand-in claude.

Unisolated, as the stand-in reads files outside its folder; the sandboxed
path has its own test in test_review_rates_runs.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from review_bed import DECLARATION, write_bed
from test_review_rates_runs import GOAL, stand_in

from demo.generate import generate

ROOT = Path(__file__).resolve().parent.parent


class CommandTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name).resolve()
        generate(11, 20, root / "data", root / "generated")
        write_bed(root / "bed", seed=5)
        cls.root, cls.claude = root, stand_in(root / "bin")
        cls.environment = {**os.environ, "STUB_SECRET": str(root / "bed" / "answers.json")}
        cls.environment.pop("STUB_CALLS", None)
        cls.first = cls.tenx("run", *cls.arguments())
        cls.again = cls.tenx("run", *cls.arguments())
        cls.shown = cls.tenx("report", str(root / "out"))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @classmethod
    def arguments(cls) -> list[str]:
        root = cls.root
        return [
            "--bed", str(root / "bed"), "--raw", f"s11-20={root / 'data'}",
            "--goal", str(GOAL), "--checks", str(DECLARATION), "--out", str(root / "out"),
            "--claude", str(cls.claude), "--unisolated",
        ]  # fmt: skip

    @classmethod
    def tenx(cls, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "tenx.review_rates", *args],
            check=False,
            cwd=ROOT,
            env=cls.environment,
            capture_output=True,
            text=True,
            timeout=600,
        )

    def test_the_whole_bed_is_reviewed_and_scored(self):
        self.assertEqual(self.first.returncode, 0, self.first.stderr)
        text = self.first.stdout
        self.assertTrue(
            text.startswith("12 reviews to do, estimated at US$0.96, at most US$4.80"), text
        )
        report = text[text.index("Reviewer measurement:") :]
        # The stand-in catches the contact name, crashes on the times cut to
        # the day, gives a line the file does not have for milliseconds,
        # and approves everything else.
        self.assertIn("Reviewer measurement: unclear\n", report)
        self.assertIn("Caught (blocking, right model and column): 1 of 4 (25%", report)
        self.assertIn("Good projects rejected: 0 of 6 (0%", report)
        self.assertIn("Cost: US$0.6120 for 10 reviews, US$0.0612 a review", report)
        lines = {line.split()[1]: line.split(None, 3)[3] for line in report.splitlines()[8:]}
        self.assertEqual(lines["contact_name_from_email"], "caught")
        self.assertEqual(
            lines["event_time_truncated_to_day"], "no review (the reviewer gave no report)"
        )
        self.assertTrue(lines["invoice_time_in_milliseconds"].startswith("no review ("))
        for name in (
            "account_date_month_first",
            "customer_currency_constant",
            "event_columns_swapped",
        ):
            self.assertEqual(lines[name], "missed")
        self.assertEqual(sum(value == "approved" for value in lines.values()), 6)

    def test_a_second_start_has_nothing_to_do(self):
        self.assertEqual(self.again.returncode, 0, self.again.stderr)
        self.assertTrue(self.again.stdout.startswith("0 reviews to do, estimated at US$0.00"))
        first = self.first.stdout[self.first.stdout.index("Reviewer measurement:") :]
        self.assertEqual(
            self.again.stdout[self.again.stdout.index("Reviewer measurement:") :], first
        )

    def test_report_prints_the_same_report(self):
        self.assertEqual(self.shown.returncode, 0, self.shown.stderr)
        first = self.first.stdout[self.first.stdout.index("Reviewer measurement:") :]
        self.assertEqual(self.shown.stdout, first)

    def test_a_folder_that_is_not_a_bed_is_refused(self):
        arguments = self.arguments()
        arguments[arguments.index("--bed") + 1] = str(self.root / "data")
        done = self.tenx("run", *arguments)
        self.assertEqual(done.returncode, 2)
        self.assertIn("error: cannot read", done.stderr)

    def test_a_missing_claude_is_refused_before_anything_is_written(self):
        arguments = self.arguments()
        arguments[arguments.index("--claude") + 1] = str(self.root / "no-such-claude")
        arguments[arguments.index("--out") + 1] = str(self.root / "elsewhere")
        done = self.tenx("run", *arguments)
        self.assertEqual(done.returncode, 2)
        self.assertIn("no-such-claude was not found", done.stderr)
        self.assertFalse((self.root / "elsewhere").exists())


if __name__ == "__main__":
    unittest.main()
