"""Tests for the two demo goals: the full goal and the brief one.

Both must name what the gate's hidden declaration checks by name (models,
keys and the columns it reads), or no project could pass however well it was
designed. The brief must leave out everything else the full goal spells out,
since it is the test bed for writing a spec from a brief goal: each phrase
below is checked to be in the full goal, so a phrase that drifts out of it
cannot pass by being absent from both.
"""

import re
import unittest
from pathlib import Path

from tenx.gate import load_declaration

ROOT = Path(__file__).resolve().parent.parent
GOAL = ROOT / "demo" / "goal.md"
BRIEF = ROOT / "demo" / "brief.md"
DECLARATION = ROOT / "tests" / "fixtures" / "demo_gate.json"

# What the full goal says that the brief must not: how many rows each model
# holds, which rows to leave out, the unit of money and how to match.
SPELLED_OUT = (
    "one row per",
    "test mode",
    "leave",
    "exact copy",
    "keep every",
    "smallest unit",
    "email",
    "in common",
    "more than one",
    "no crm account",
)


def plain(path: Path) -> str:
    """The file's text in lower case, with hyphens read as spaces."""
    return path.read_text(encoding="utf-8").lower().replace("-", " ")


def names_in_code(text: str) -> set[str]:
    """Every name the text writes as code, between backticks."""
    return set(re.findall(r"`([^`]+)`", text))


def checked_names() -> set[str]:
    """Every model and column name the declaration reads from a project."""
    names = set()
    for model in load_declaration(DECLARATION):
        names.add(model.name)
        names.update(model.key)
        for total in model.totals:
            names.add(total.column)
            if total.by:
                names.add(total.by)
        if model.expected:
            names.update(model.expected.columns)
    return names


class DemoGoalsTest(unittest.TestCase):
    def test_both_goals_name_every_checked_model_and_column(self):
        expected = checked_names()
        self.assertIn("amount_minor", expected)
        self.assertIn("account_id", expected)
        for goal in (GOAL, BRIEF):
            with self.subTest(goal=goal.name):
                missing = expected - names_in_code(goal.read_text(encoding="utf-8"))
                self.assertEqual(missing, set())

    def test_the_full_goal_spells_out_each_rule(self):
        text = plain(GOAL)
        for phrase in SPELLED_OUT:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)

    def test_the_brief_leaves_the_rules_out(self):
        text = plain(BRIEF)
        for phrase in SPELLED_OUT:
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase, text)

    def test_the_brief_names_only_what_the_gate_reads(self):
        text = BRIEF.read_text(encoding="utf-8")
        self.assertEqual(names_in_code(text), checked_names())


if __name__ == "__main__":
    unittest.main()
