"""Tests for the environment a sandboxed process is given.

The expected environments below are written out by hand from the rule: in a
sandbox, only the names every process needs, Claude Code's names for an
agent, and the names a user lists in TENX_SANDBOX_ENV; without one, all of
it. Then a stand-in agent run by the runner in the real sandbox records the
environment it was given, with secrets planted in the runner's own.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_sandbox import needs_sandbox

from tenx.engineer import run_agent
from tenx.sandbox import Bubblewrap, Mounts, Unisolated, default_sandbox, environment

CALLER = {
    "PATH": "/usr/bin",
    "HOME": "/home/ann",
    "USER": "ann",
    "LOGNAME": "ann",
    "SHELL": "/bin/bash",
    "LANG": "en_GB.UTF-8",
    "LANGUAGE": "en_GB",
    "TZ": "Europe/London",
    "LC_ALL": "en_GB.UTF-8",
    "LC_TIME": "C",
    "TMPDIR": "/home/ann/tmp",
    "GITHUB_TOKEN": "ghp-secret",
    "AWS_SECRET_ACCESS_KEY": "aws-secret",
    "DATABASE_URL": "postgres://ann:pw@db/prod",
    "DBT_ENV_SECRET_TOKEN": "dbt-secret",
    "ANTHROPIC_API_KEY": "sk-ant-key",
    "CLAUDE_CONFIG_DIR": "/home/ann/.claude",
    "CLAUDE_CODE_USE_BEDROCK": "1",
    "HTTPS_PROXY": "http://proxy:8080",
    "no_proxy": "localhost",
    "NODE_EXTRA_CA_CERTS": "/etc/corp.pem",
    "DO_NOT_TRACK": "1",
    "ANTHROPICX": "not a prefix match",
    "XCLAUDE_CODE": "not a prefix match",
    "lc_all": "case counts",
}
EVERY_PROCESS = {
    "PATH": "/usr/bin",
    "HOME": "/home/ann",
    "USER": "ann",
    "LOGNAME": "ann",
    "SHELL": "/bin/bash",
    "LANG": "en_GB.UTF-8",
    "LANGUAGE": "en_GB",
    "TZ": "Europe/London",
    "LC_ALL": "en_GB.UTF-8",
    "LC_TIME": "C",
}
AGENT = {
    **EVERY_PROCESS,
    "ANTHROPIC_API_KEY": "sk-ant-key",
    "CLAUDE_CONFIG_DIR": "/home/ann/.claude",
    "CLAUDE_CODE_USE_BEDROCK": "1",
    "HTTPS_PROXY": "http://proxy:8080",
    "no_proxy": "localhost",
    "NODE_EXTRA_CA_CERTS": "/etc/corp.pem",
    "DO_NOT_TRACK": "1",
}


class NoIsolationMark:
    """A sandbox that does not say whether it isolates."""

    def command(self, command, mounts):
        return command


class EnvironmentTest(unittest.TestCase):
    def test_a_build_in_a_sandbox_gets_only_what_every_process_needs(self):
        self.assertEqual(environment(Bubblewrap(), CALLER), EVERY_PROCESS)

    def test_an_agent_in_a_sandbox_also_gets_what_claude_code_needs(self):
        self.assertEqual(environment(Bubblewrap(), CALLER, agent=True), AGENT)

    def test_without_isolation_everything_is_passed(self):
        self.assertEqual(environment(Unisolated(), CALLER), CALLER)
        self.assertEqual(environment(Unisolated(), CALLER, agent=True), CALLER)

    def test_a_sandbox_that_does_not_say_is_treated_as_isolated(self):
        self.assertEqual(environment(NoIsolationMark(), CALLER), EVERY_PROCESS)

    def test_names_the_user_lists_are_passed_as_well(self):
        caller = {**CALLER, "TENX_SANDBOX_ENV": " DATABASE_URL, ,MISSING,GITHUB"}
        self.assertEqual(
            environment(Bubblewrap(), caller),
            {**EVERY_PROCESS, "DATABASE_URL": "postgres://ann:pw@db/prod"},
        )

    def test_the_callers_environment_is_the_default_and_is_not_changed(self):
        with mock.patch.dict(os.environ, CALLER, clear=True):
            self.assertEqual(environment(Bubblewrap(), agent=True), AGENT)
            self.assertEqual(dict(os.environ), CALLER)

    def test_the_result_is_a_copy(self):
        given = environment(Unisolated(), CALLER)
        given["PATH"] = "changed"
        self.assertEqual(CALLER["PATH"], "/usr/bin")


RECORDS_ENVIRONMENT = (
    "import json, os, pathlib; "
    "pathlib.Path('environment.json').write_text(json.dumps(dict(os.environ)))"
)
PLANTED = {
    "GITHUB_TOKEN": "planted-github",
    "AWS_SECRET_ACCESS_KEY": "planted-aws",
    "TENX_PLANTED_SECRET": "planted-other",
    "DBT_PROFILES_DIR": "/elsewhere",
    "ANTHROPIC_API_KEY": "planted-but-needed",
    "TENX_PASSED_ON_PURPOSE": "asked-for",
    "TENX_SANDBOX_ENV": "TENX_PASSED_ON_PURPOSE",
}


class AgentEnvironmentRuns:
    def recorded(self, sandbox, mounts=None) -> dict:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, PLANTED):
            workspace = Path(tmp).resolve() / "work"
            workspace.mkdir()
            outcome = run_agent(
                workspace,
                [sys.executable, "-c", RECORDS_ENVIRONMENT],
                workspace.parent / "out" / "agent.json",
                30,
                sandbox,
                mounts(workspace) if mounts else None,
            )
            self.assertEqual(outcome.exit_code, 0, outcome.describe())
            return json.loads((workspace / "environment.json").read_text(encoding="utf-8"))


class UnisolatedAgentEnvironmentTest(AgentEnvironmentRuns, unittest.TestCase):
    def test_an_unisolated_agent_gets_the_whole_environment_but_dbts_profiles(self):
        found = self.recorded(Unisolated())
        for name in ("GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY", "TENX_PLANTED_SECRET"):
            self.assertEqual(found[name], PLANTED[name])
        self.assertNotIn("DBT_PROFILES_DIR", found)


@needs_sandbox
class SandboxedAgentEnvironmentTest(AgentEnvironmentRuns, unittest.TestCase):
    def setUp(self):
        python = (Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve())
        self.found = self.recorded(
            default_sandbox(), lambda workspace: Mounts(workspace=workspace, read_only=python)
        )

    def test_secrets_in_the_runners_environment_do_not_reach_the_agent(self):
        for name in ("GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY", "TENX_PLANTED_SECRET"):
            self.assertNotIn(name, self.found)
        self.assertNotIn("planted-github", json.dumps(self.found))

    def test_the_agent_gets_its_sign_in_path_and_the_names_asked_for(self):
        self.assertEqual(self.found["ANTHROPIC_API_KEY"], "planted-but-needed")
        self.assertEqual(self.found["TENX_PASSED_ON_PURPOSE"], "asked-for")
        self.assertEqual(self.found["PATH"], os.environ["PATH"])
        self.assertEqual(self.found["CLAUDE_CODE_TOOL_MEMORY_LIMIT"], "none")
        self.assertNotIn("DBT_PROFILES_DIR", self.found)


if __name__ == "__main__":
    unittest.main()
