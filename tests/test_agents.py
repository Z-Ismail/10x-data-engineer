"""Tests for the agent definitions shipped with the add-on.

Claude Code loads a plugin agent with whatever frontmatter parses and
silently ignores fields a plugin agent may not set, so a mistake here would
not show up until a run behaved differently. The supported and ignored
fields are those listed in the Claude Code plugin documentation.
"""

import unittest
from pathlib import Path

import yaml

AGENTS = Path(__file__).resolve().parent.parent / "agents"

SUPPORTED = {
    "name",
    "description",
    "model",
    "effort",
    "maxTurns",
    "tools",
    "disallowedTools",
    "skills",
    "memory",
    "background",
    "omitClaudeMd",
    "isolation",
    "color",
    "experimental",
}


def read_agent(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    opening, frontmatter, body = text.split("---\n", 2)
    if opening:
        raise ValueError(f"{path.name} does not start with frontmatter")
    return yaml.safe_load(frontmatter), body


class EngineerAgentTest(unittest.TestCase):
    def setUp(self):
        self.fields, self.body = read_agent(AGENTS / "engineer.md")

    def test_sets_only_fields_a_plugin_agent_may_set(self):
        self.assertLessEqual(set(self.fields), SUPPORTED)
        self.assertEqual(self.fields["name"], "engineer")
        self.assertTrue(self.fields["description"].strip())

    def test_pins_the_model(self):
        self.assertEqual(self.fields["model"], "sonnet")

    def test_can_edit_files_and_run_commands_but_not_start_other_agents(self):
        tools = {tool.strip() for tool in self.fields["tools"].split(",")}
        self.assertEqual(tools, {"Read", "Glob", "Grep", "Edit", "Write", "Bash"})

    def test_starts_without_claude_md_files(self):
        self.assertIs(self.fields["omitClaudeMd"], True)

    def test_asks_for_one_dbt_command_per_shell_call(self):
        self.assertIn("Run one `dbt` command per shell call", self.body)

    def test_builds_models_that_combine_sources_from_staging_as_tables_or_views(self):
        self.assertIn("in `models/intermediate/`, from the\n   staging models", self.body)
        self.assertIn("never as ephemeral", self.body)

    def test_reads_raw_files_through_raw_dir(self):
        self.assertIn("{{ var('raw_dir') }}", self.body)
        self.assertIn("all_varchar=true", self.body)


class CrossSourceAgentTest(unittest.TestCase):
    def setUp(self):
        self.fields, self.body = read_agent(AGENTS / "cross-source.md")

    def test_sets_only_fields_a_plugin_agent_may_set(self):
        self.assertLessEqual(set(self.fields), SUPPORTED)
        self.assertEqual(self.fields["name"], "cross-source")
        self.assertTrue(self.fields["description"].strip())

    def test_pins_the_model(self):
        self.assertEqual(self.fields["model"], "sonnet")

    def test_can_only_read_files_and_give_its_answer(self):
        # Run with --agent, Claude Code offers only the tools listed here, so
        # the answer tool for --json-schema must be listed as well.
        tools = {tool.strip() for tool in self.fields["tools"].split(",")}
        self.assertEqual(tools, {"Read", "Glob", "Grep", "StructuredOutput"})

    def test_starts_without_claude_md_files(self):
        self.assertIs(self.fields["omitClaudeMd"], True)

    def test_asks_for_every_field_of_the_answer(self):
        for field in (
            "left",
            "right",
            "prepare",
            "cardinality",
            "unmatched",
            "evidence",
            "cautions",
        ):
            with self.subTest(field):
                self.assertIn(f"`{field}`", self.body)


if __name__ == "__main__":
    unittest.main()
