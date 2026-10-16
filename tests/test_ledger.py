"""Tests for the usage ledger.

The reports are written by hand in the shape Claude Code prints with
`claude -p --output-format json`. Expected figures are read off the
handmade reports, not computed by the code under test.
"""

import json
import tempfile
import unittest
from pathlib import Path

from tenx.ledger import Ledger, ModelUsage, RoleUsage, render, role_usage

# Shaped like a real report: the main model plus a small helper request
# that appears in modelUsage but not in usage.
REPORT = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "num_turns": 16,
    "duration_ms": 36008,
    "duration_api_ms": 21156,
    "total_cost_usd": 0.0927878,
    "usage": {
        "input_tokens": 14,
        "cache_creation_input_tokens": 13501,
        "cache_read_input_tokens": 58591,
        "output_tokens": 3277,
    },
    "modelUsage": {
        "claude-haiku-5-5": {
            "inputTokens": 1177,
            "outputTokens": 18,
            "cacheReadInputTokens": 0,
            "cacheCreationInputTokens": 0,
            "costUSD": 0.0001267,
            "costBasis": "list",
        },
        "claude-sonnet-5-5": {
            "inputTokens": 14,
            "outputTokens": 3277,
            "cacheReadInputTokens": 58591,
            "cacheCreationInputTokens": 13501,
            "thinkingTokens": 175,
            "costUSD": 0.0926611,
            "costBasis": "list",
        },
    },
    "permission_denials": [],
}


class RoleUsageTest(unittest.TestCase):
    def test_reads_turns_time_cost_and_tokens_per_model(self):
        usage = role_usage("engineer", REPORT)
        self.assertEqual(
            usage,
            RoleUsage(
                role="engineer",
                outcome="success",
                turns=16,
                duration_ms=36008,
                api_duration_ms=21156,
                cost_usd=0.0927878,
                refused=0,
                models=(
                    ModelUsage("claude-haiku-5-5", 1177, 0, 0, 18, "list"),
                    ModelUsage("claude-sonnet-5-5", 14, 13501, 58591, 3277, "list"),
                ),
            ),
        )
        self.assertTrue(usage.cost_known)

    def test_tokens_come_from_model_usage_not_the_main_loop(self):
        # The helper's 1,177 input tokens are in modelUsage only.
        usage = role_usage("engineer", REPORT)
        self.assertEqual(sum(model.input for model in usage.models), 1191)

    def test_thinking_is_not_added_to_output_again(self):
        usage = role_usage("engineer", REPORT)
        self.assertEqual(sum(model.output for model in usage.models), 3295)

    def test_counts_refused_tool_calls(self):
        report = dict(
            REPORT,
            permission_denials=[
                {"tool_name": "Bash", "tool_input": {"command": "ls /"}},
                {"tool_name": "Read", "tool_input": {"file_path": "/etc/hosts"}},
            ],
        )
        self.assertEqual(role_usage("engineer", report).refused, 2)

    def test_an_error_report_keeps_its_figures_and_names_the_error(self):
        report = dict(REPORT, subtype="error_max_turns", is_error=True, num_turns=61)
        usage = role_usage("engineer", report)
        self.assertEqual((usage.outcome, usage.turns), ("error_max_turns", 61))
        self.assertEqual(usage.cost_usd, 0.0927878)

    def test_a_model_without_a_known_price_makes_the_cost_unknown(self):
        models = dict(REPORT["modelUsage"])
        models["claude-new-model"] = {"inputTokens": 5, "outputTokens": 1, "costBasis": "unknown"}
        usage = role_usage("engineer", dict(REPORT, modelUsage=models))
        self.assertFalse(usage.cost_known)
        self.assertEqual(
            usage.models[-1], ModelUsage("claude-new-model", 5, None, None, 1, "unknown")
        )

    def test_a_missing_report_is_recorded_with_no_figures(self):
        usage = role_usage("engineer", None, missing="timed out")
        self.assertEqual(usage, RoleUsage(role="engineer", outcome="timed out"))
        self.assertFalse(usage.cost_known)
        self.assertEqual(role_usage("engineer", None).outcome, "no report")

    def test_fields_of_the_wrong_kind_are_left_empty_not_guessed(self):
        report = {
            "subtype": "",
            "num_turns": True,
            "duration_ms": "36008",
            "duration_api_ms": -1,
            "total_cost_usd": None,
            "permission_denials": "none",
            "modelUsage": {"claude-sonnet-5-5": {"inputTokens": 3.5}, "broken": [1, 2]},
        }
        self.assertEqual(
            role_usage("engineer", report),
            RoleUsage(
                role="engineer",
                outcome="unknown",
                models=(ModelUsage("claude-sonnet-5-5", None, None, None, None),),
            ),
        )

    def test_to_dict_names_every_figure(self):
        self.assertEqual(
            role_usage("engineer", REPORT).to_dict()["models"][1],
            {
                "model": "claude-sonnet-5-5",
                "input_tokens": 14,
                "cache_write_tokens": 13501,
                "cache_read_tokens": 58591,
                "output_tokens": 3277,
                "cost_basis": "list",
            },
        )
        record = role_usage("engineer", None, missing="timed out").to_dict()
        self.assertEqual(
            record,
            {
                "role": "engineer",
                "outcome": "timed out",
                "turns": None,
                "duration_ms": None,
                "api_duration_ms": None,
                "cost_usd": None,
                "cost_known": False,
                "refused_tool_calls": None,
                "models": [],
            },
        )


class RenderTest(unittest.TestCase):
    def test_one_role_with_two_models(self):
        ledger = Ledger((role_usage("engineer", REPORT),), "PASS")
        self.assertEqual(
            render(ledger),
            "usage by role (cost is Claude Code's own estimate, not a bill)\n"
            "\n"
            "role      outcome    turns  refused    time  cost (US$)\n"
            "engineer  success       16        0  36.0 s      0.0928\n"
            "run       gate PASS     16        0  36.0 s      0.0928\n"
            "\n"
            "role      model              input  cache write  cache read  output\n"
            "engineer  claude-haiku-5-5   1,177            0           0      18\n"
            "engineer  claude-sonnet-5-5     14       13,501      58,591   3,277\n",
        )

    def test_the_run_row_adds_up_every_role(self):
        # 16 + 9 turns, 0 + 2 refused, 36.008 + 4.5 s, 0.0927878 + 0.25 US$.
        reviewer = RoleUsage("reviewer", "success", 9, 4500, 3000, 0.25, 2)
        ledger = Ledger((role_usage("engineer", REPORT), reviewer), "FAIL")
        lines = render(ledger).splitlines()
        self.assertEqual(lines[4], "reviewer  success        9        2   4.5 s      0.2500")
        self.assertEqual(lines[5], "run       gate FAIL     25        2  40.5 s      0.3428")

    def test_a_role_without_a_report_leaves_the_run_figures_unknown(self):
        ledger = Ledger(
            (role_usage("engineer", REPORT), role_usage("reviewer", None, "timed out")), "FAIL"
        )
        lines = render(ledger).splitlines()
        self.assertEqual(lines[4], "reviewer  timed out      -        -       -           -")
        self.assertEqual(lines[5], "run       gate FAIL      -        -       -           -")
        # The engineer's tokens are still listed; the reviewer has none.
        self.assertEqual(len(lines), 10)

    def test_an_unknown_price_is_shown_as_unknown_not_as_a_figure(self):
        models = dict(REPORT["modelUsage"])
        models["claude-new-model"] = {"inputTokens": 5, "outputTokens": 1, "costBasis": "unknown"}
        ledger = Ledger((role_usage("engineer", dict(REPORT, modelUsage=models)),), "PASS")
        lines = render(ledger).splitlines()
        self.assertTrue(lines[3].endswith("  unknown"), lines[3])
        self.assertTrue(lines[4].endswith("  unknown"), lines[4])
        self.assertEqual(
            lines[-1], "engineer  claude-new-model       5            -           -       1"
        )

    def test_no_token_table_when_no_role_reported(self):
        ledger = Ledger((role_usage("engineer", None, "timed out"),), "FAIL")
        self.assertEqual(
            render(ledger),
            "usage by role (cost is Claude Code's own estimate, not a bill)\n"
            "\n"
            "role      outcome    turns  refused  time  cost (US$)\n"
            "engineer  timed out      -        -     -           -\n"
            "run       gate FAIL      -        -     -           -\n",
        )


class LedgerFileTest(unittest.TestCase):
    def test_writes_the_gate_verdict_and_every_role_as_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "usage.json"
            Ledger((role_usage("engineer", REPORT),), "PASS").write(path)
            written = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(written["gate"], "PASS")
        self.assertEqual([role["role"] for role in written["roles"]], ["engineer"])
        role = written["roles"][0]
        self.assertEqual((role["turns"], role["cost_usd"]), (16, 0.0927878))
        self.assertEqual(
            [model["model"] for model in role["models"]],
            ["claude-haiku-5-5", "claude-sonnet-5-5"],
        )


if __name__ == "__main__":
    unittest.main()
