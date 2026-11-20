"""Tests for the Claude Code config folder a sandboxed agent is given.

A home folder is planted with what a user's Claude Code keeps there: its
settings, another session's transcript, ``~/.claude.json`` and the sign-in
file. A stand-in agent run by the runner in the real sandbox records what it
can see and tries to change, and the test checks the result against the
rule: an empty folder of its own holding only the sign-in file, which writes
reach; nothing else of the user's config readable or changed; the folder gone
when the agent ends.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from shutil import which
from unittest import mock

from test_sandbox import needs_sandbox

from tenx.engineer import run_agent
from tenx.sandbox import Bubblewrap, Mounts, Unisolated, python_folders

SIGN_IN = '{"claudeAiOauth": {"accessToken": "planted-token"}}'
REFRESHED = '{"claudeAiOauth": {"accessToken": "refreshed-token"}}'
HOOK = '{"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "id"}]}]}}'

# Records what the agent sees, then does what Claude Code and a hostile
# command might: write a hook, refresh the sign-in in place, replace it.
STAND_IN = f"""
import errno, json, os, pathlib, time
home = pathlib.Path(os.environ["HOME"])
config = os.environ.get("CLAUDE_CONFIG_DIR", "")
found = {{"config": config}}
pathlib.Path("config.txt").write_text(config)
def read(path):
    try:
        return pathlib.Path(path).read_text()
    except OSError as error:
        return "unreadable: " + errno.errorcode[error.errno]
def write(path, text):
    try:
        pathlib.Path(path).write_text(text)
        return "written"
    except OSError as error:
        return "refused: " + errno.errorcode[error.errno]
found["listing"] = sorted(os.listdir(config)) if config else None
found["sign_in"] = read(pathlib.Path(config) / ".credentials.json")
for name in (".claude/settings.json", ".claude/projects/other/session.jsonl", ".claude.json"):
    found[name] = read(home / name)
found["hook"] = write(home / ".claude" / "settings.json", {HOOK!r})
found["own_settings"] = write(pathlib.Path(config) / "settings.json", {HOOK!r})
pathlib.Path(config, "replacement").write_text("x")
try:
    os.replace(pathlib.Path(config, "replacement"), pathlib.Path(config, ".credentials.json"))
    found["replace"] = "replaced"
except OSError as error:
    found["replace"] = errno.errorcode[error.errno]
with open(pathlib.Path(config) / ".credentials.json", "r+") as sign_in:
    sign_in.truncate(0)
    sign_in.write({REFRESHED!r})
pathlib.Path("found.json").write_text(json.dumps(found))
if os.environ.get("TENX_STAND_IN_SLEEP"):
    time.sleep(60)
"""


class PlantedHome:
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home = self.root / "home"
        self.config = self.home / ".claude"
        (self.config / "projects" / "other").mkdir(parents=True)
        (self.config / "settings.json").write_text('{"model": "opus"}', encoding="utf-8")
        (self.config / "projects" / "other" / "session.jsonl").write_text(
            "secret line", encoding="utf-8"
        )
        (self.home / ".claude.json").write_text('{"oauthAccount": {}}', encoding="utf-8")
        (self.config / ".credentials.json").write_text(SIGN_IN, encoding="utf-8")
        self.workspace = self.root / "work"
        self.workspace.mkdir()

    def run_stand_in(self, sandbox, timeout: float = 60, **variables):
        names = "TENX_STAND_IN_SLEEP" if variables else ""
        planted = {"HOME": str(self.home), "TENX_SANDBOX_ENV": names, **variables}
        with mock.patch.dict(os.environ, planted):
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            mounts = Mounts(workspace=self.workspace, read_only=python_folders())
            return run_agent(
                self.workspace,
                [sys.executable, "-c", STAND_IN],
                self.root / "out" / "agent.json",
                timeout,
                sandbox,
                mounts,
            )

    def found(self) -> dict:
        return json.loads((self.workspace / "found.json").read_text(encoding="utf-8"))


@needs_sandbox
class SandboxedConfigTest(PlantedHome, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.sandbox = Bubblewrap(which("bwrap"), home=self.home)

    def test_the_agent_gets_an_empty_folder_of_its_own_with_only_the_sign_in(self):
        outcome = self.run_stand_in(self.sandbox)
        self.assertEqual(outcome.exit_code, 0, outcome.describe())
        found = self.found()
        config = Path(found["config"])
        self.assertTrue(config.name.startswith("tenx-claude-"))
        self.assertNotEqual(config, self.config)
        self.assertEqual(found["listing"], [".credentials.json"])
        self.assertEqual(found["sign_in"], SIGN_IN)

    def test_the_users_settings_sessions_and_state_cannot_be_read(self):
        self.run_stand_in(self.sandbox)
        found = self.found()
        for name in (".claude/settings.json", ".claude/projects/other/session.jsonl"):
            self.assertEqual(found[name], "unreadable: ENOENT", name)
        self.assertEqual(found[".claude.json"], "unreadable: ENOENT")
        self.assertNotIn("secret line", json.dumps(found))

    def test_a_hook_written_by_the_agent_never_reaches_the_users_settings(self):
        self.run_stand_in(self.sandbox)
        found = self.found()
        # Written into the sandbox's empty home, which vanishes with it.
        self.assertEqual(found["own_settings"], "written")
        self.assertEqual(
            (self.config / "settings.json").read_text(encoding="utf-8"), '{"model": "opus"}'
        )
        self.assertEqual(
            sorted(p.name for p in self.config.iterdir()),
            [".credentials.json", "projects", "settings.json"],
        )

    def test_a_refreshed_sign_in_is_kept_in_the_users_file(self):
        self.run_stand_in(self.sandbox)
        # Replacing the file is refused, as Claude Code's own writer expects,
        # and the write in place reaches the user's file.
        self.assertEqual(self.found()["replace"], "EBUSY")
        self.assertEqual((self.config / ".credentials.json").read_text(encoding="utf-8"), REFRESHED)

    def test_the_folder_is_removed_when_the_agent_ends(self):
        self.run_stand_in(self.sandbox)
        self.assertFalse(Path(self.found()["config"]).exists())

    def test_the_folder_is_removed_when_the_agent_is_stopped(self):
        outcome = self.run_stand_in(self.sandbox, timeout=5, TENX_STAND_IN_SLEEP="1")
        self.assertIsNotNone(outcome.timed_out_after)
        config = Path((self.workspace / "config.txt").read_text(encoding="utf-8"))
        self.assertTrue(config.name.startswith("tenx-claude-"))
        self.assertFalse(config.exists())

    def test_without_a_sign_in_file_the_folder_starts_empty(self):
        (self.config / ".credentials.json").unlink()
        outcome = self.run_stand_in(self.sandbox)
        self.assertEqual(outcome.exit_code, 0, outcome.describe())
        self.assertEqual(self.found()["listing"], [])
        # What the agent wrote there stayed in its own folder.
        self.assertFalse((self.config / ".credentials.json").exists())


class UnisolatedConfigTest(PlantedHome, unittest.TestCase):
    def test_an_unisolated_agent_keeps_the_users_own_config(self):
        outcome = self.run_stand_in(Unisolated())
        self.assertEqual(outcome.exit_code, 0, outcome.describe())
        found = self.found()
        # No folder is made for it: Claude Code uses the user's own.
        self.assertEqual(found["config"], "")
        self.assertEqual(found[".claude/settings.json"], '{"model": "opus"}')


if __name__ == "__main__":
    unittest.main()
