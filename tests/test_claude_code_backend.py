"""The Claude Code backend starts each agent exactly as the runner does."""

import json
import shutil
import stat
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from tenx.backends.claude_code import ClaudeCode
from tenx.engineer import CROSS_SOURCE, ENGINEER, SCOUT, agent_command, run_agent, scout_role
from tenx.policy import Policy
from tenx.ports import RUN_AGENT, AgentBackend, PlatformError, UnsupportedCapability

STAND_IN = f"""#!{sys.executable}
import json, os, sys
mode = open(os.path.join(os.getcwd(), "MODE")).read().strip()
if mode == "crash":
    print("boom", file=sys.stderr)
    sys.exit(3)
if mode == "hang":
    import time
    time.sleep(60)
open("seen.txt", "w").write(sys.argv[2])
print(json.dumps({{"type": "result", "subtype": "success", "num_turns": 4}}))
"""


class NoStructuredAnswers(ClaudeCode):
    """A backend that can run agents but cannot hold them to a schema."""

    name = "plain agents"

    def capabilities(self) -> frozenset[str]:
        return frozenset({RUN_AGENT})


class CommandTest(unittest.TestCase):
    def test_the_command_is_the_runners_for_every_role(self):
        backend = ClaudeCode("/usr/bin/claude", Path("/run/plugin"))
        self.assertIsInstance(backend, AgentBackend)
        for role in (ENGINEER, CROSS_SOURCE, SCOUT, scout_role("billing.customers")):
            with self.subTest(role=role.name):
                self.assertEqual(
                    backend.command(role),
                    agent_command("/usr/bin/claude", Path("/run/plugin"), role),
                )

    def test_the_command_carries_the_backends_policy(self):
        caps = {"engineer": "0.5", "cross-source": "0.25", "scout": "0.05"}
        rules = Policy(
            run_usd=Decimal(1), call_usd={kind: Decimal(cap) for kind, cap in caps.items()}
        )
        backend = ClaudeCode("/usr/bin/claude", Path("/run/plugin"), rules)
        for role in (ENGINEER, CROSS_SOURCE, scout_role("crm.accounts")):
            with self.subTest(role=role.name):
                command = backend.command(role)
                self.assertEqual(
                    command, agent_command("/usr/bin/claude", Path("/run/plugin"), role, rules)
                )
                cap = command[command.index("--max-budget-usd") + 1]
                self.assertEqual(cap, caps[role.budget_kind])


class RunTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="tenx-backend-"))
        self.addCleanup(shutil.rmtree, self.root)
        self.claude = self.root / "claude"
        self.claude.write_text(STAND_IN, encoding="utf-8")
        self.claude.chmod(self.claude.stat().st_mode | stat.S_IXUSR)
        self.backend = ClaudeCode(str(self.claude), self.root / "plugin")

    def workspace(self, name: str, mode: str) -> Path:
        folder = self.root / name
        folder.mkdir()
        (folder / "MODE").write_text(mode, encoding="utf-8")
        return folder

    def test_a_finished_session_gives_what_the_runner_gives(self):
        for mode in ("finish", "crash"):
            with self.subTest(mode=mode):
                through = self.workspace(f"backend-{mode}", mode)
                direct = self.workspace(f"runner-{mode}", mode)
                got = self.backend.run(ENGINEER, through, self.root / f"{mode}-b.json", 20)
                command = agent_command(str(self.claude), self.root / "plugin", ENGINEER)
                expected = run_agent(direct, command, self.root / f"{mode}-r.json", 20)
                self.assertEqual(got, expected)
                self.assertEqual(
                    (self.root / f"{mode}-b.json").read_bytes(),
                    (self.root / f"{mode}-r.json").read_bytes(),
                )
        finished = self.backend.run(
            ENGINEER, self.workspace("again", "finish"), self.root / "a.json", 20
        )
        self.assertEqual(finished.report, {"type": "result", "subtype": "success", "num_turns": 4})
        self.assertEqual((self.root / "again" / "seen.txt").read_text(), ENGINEER.prompt)
        crashed = self.backend.run(
            ENGINEER, self.workspace("down", "crash"), self.root / "d.json", 20
        )
        self.assertEqual((crashed.exit_code, crashed.report, crashed.message), (3, None, "boom"))

    def test_a_session_past_its_time_is_stopped(self):
        hung = self.backend.run(ENGINEER, self.workspace("hang", "hang"), self.root / "h.json", 1)
        self.assertEqual(hung.timed_out_after, 1)
        self.assertIsNone(hung.report)
        self.assertFalse((self.root / "h.json").exists())

    def test_a_missing_claude_is_a_platform_error(self):
        backend = ClaudeCode(str(self.root / "not-here"), self.root / "plugin")
        with self.assertRaisesRegex(PlatformError, "was not found"):
            backend.run(ENGINEER, self.workspace("none", "finish"), self.root / "n.json", 20)

    def test_a_schema_role_is_refused_by_a_backend_without_structured_answers(self):
        backend = NoStructuredAnswers(str(self.claude), self.root / "plugin")
        folder = self.workspace("plain", "finish")
        with self.assertRaises(UnsupportedCapability) as caught:
            backend.run(CROSS_SOURCE, folder, self.root / "p.json", 20)
        self.assertEqual(str(caught.exception), "plain agents cannot give a structured answer")
        self.assertFalse((folder / "seen.txt").exists())
        self.assertFalse((self.root / "p.json").exists())
        # A role without a schema still runs on it.
        self.assertEqual(backend.run(ENGINEER, folder, self.root / "e.json", 20).exit_code, 0)
        self.assertEqual(json.loads((self.root / "e.json").read_text())["num_turns"], 4)


if __name__ == "__main__":
    unittest.main()
