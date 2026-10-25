"""Tests for the sandbox the agents run in.

The command line bubblewrap is given is checked against paths worked out by
hand, and the sandbox is refused where it cannot be set up.
The runner shows each agent what its work needs and hides the rest, and
the command line refuses a run without a sandbox unless told otherwise.
"""

import io
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from demo.generate import generate
from tenx.engineer import PLUGIN_DIR, USAGE_FILE, main, mounts
from tenx.sandbox import (
    BWRAP_INSTALL,
    Bubblewrap,
    Mounts,
    SandboxError,
    Unisolated,
    default_sandbox,
)

FIXTURES = Path(__file__).parent / "fixtures"
REFERENCE_MODELS = FIXTURES / "demo_project" / "models"
DECLARATION = FIXTURES / "demo_gate.json"
DEMO_GOAL = PLUGIN_DIR / "demo" / "goal.md"


def options(arguments: list[str]) -> list[str]:
    """The bubblewrap options before the command, as (flag, path...) groups."""
    return arguments[1 : arguments.index("--")]


def mounted(arguments: list[str]) -> list[tuple[str, ...]]:
    """Every mount, in order: ("--tmpfs", dest) or (flag, source, dest)."""
    items, found, index = options(arguments), [], 0
    while index < len(items):
        flag = items[index]
        if flag in ("--bind", "--ro-bind"):
            found.append((flag, items[index + 1], items[index + 2]))
            index += 3
        elif flag in ("--tmpfs", "--dev", "--proc", "--chdir"):
            found.append((flag, items[index + 1]))
            index += 2
        else:
            found.append((flag,))
            index += 1
    return found


class BubblewrapCommandTest(unittest.TestCase):
    """The command line, from paths made for each test."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home = self.root / "home"
        self.repo = self.home / "repo"
        self.out = self.repo / "runs" / "demo"
        self.project = self.out / "project"
        self.plugin = self.out / "plugin"
        self.raw = self.repo / "demo" / "data"
        self.checks = self.repo / "tests" / "fixtures" / "gate.json"
        for folder in (self.project, self.plugin, self.raw, self.checks.parent):
            folder.mkdir(parents=True)
        self.checks.write_text("{}", encoding="utf-8")
        self.sandbox = Bubblewrap("bwrap", home=self.home)

    def mounts(self, **changes) -> Mounts:
        values = {
            "workspace": self.project,
            "read_only": (self.raw, self.plugin),
            "hidden": (self.repo, self.out, self.checks),
        }
        values.update(changes)
        return Mounts(**values)

    def test_hides_home_scratch_and_hidden_folders_then_puts_back_what_is_needed(self):
        arguments = self.sandbox.command(["claude", "-p", "x"], self.mounts())
        self.assertEqual(arguments[0], "bwrap")
        self.assertEqual(arguments[arguments.index("--") + 1 :], ["claude", "-p", "x"])
        scratch = [("--tmpfs", path) for path in ("/tmp", "/var/tmp", "/run/user")]
        scratch = [mount for mount in scratch if Path(mount[1]).is_dir()]
        # Shallowest first, so that a deeper folder is hidden on top of its parent;
        # then the folders put back, also shallowest first.
        self.assertEqual(
            mounted(arguments),
            [
                ("--ro-bind", "/", "/"),
                ("--dev", "/dev"),
                ("--proc", "/proc"),
                *sorted(
                    [("--tmpfs", str(self.home))] + scratch,
                    key=lambda m: (len(Path(m[1]).parts), m[1]),
                ),
                ("--tmpfs", str(self.repo)),
                ("--tmpfs", str(self.out)),
                ("--ro-bind", str(self.raw), str(self.raw)),
                ("--ro-bind", str(self.plugin), str(self.plugin)),
                ("--bind", str(self.project), str(self.project)),
                ("--unshare-pid",),
                ("--die-with-parent",),
                ("--chdir", str(self.project)),
            ],
        )

    def test_a_hidden_file_already_under_a_hidden_folder_needs_no_mount_of_its_own(self):
        arguments = self.sandbox.command(["true"], self.mounts())
        self.assertNotIn(str(self.checks), options(arguments))

    def test_a_hidden_file_inside_a_folder_put_back_is_covered_last(self):
        inside = self.raw / "gate.json"
        inside.write_text("{}", encoding="utf-8")
        arguments = self.sandbox.command(["true"], self.mounts(hidden=(self.repo, inside)))
        found = mounted(arguments)
        cover = ("--ro-bind", os.devnull, str(inside))
        self.assertIn(cover, found)
        self.assertGreater(found.index(cover), found.index(("--ro-bind", *[str(self.raw)] * 2)))

    def test_a_hidden_folder_inside_a_folder_put_back_is_emptied_last(self):
        answers = self.raw / "answers"
        answers.mkdir()
        arguments = self.sandbox.command(["true"], self.mounts(hidden=(self.repo, answers)))
        found = mounted(arguments)
        self.assertEqual(found[-4], ("--tmpfs", str(answers)))

    def test_a_hidden_path_outside_every_hidden_folder_is_still_hidden(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        secret = elsewhere / "gate.json"
        secret.write_text("{}", encoding="utf-8")
        arguments = self.sandbox.command(
            ["true"], self.mounts(hidden=(self.repo, self.out, elsewhere, secret))
        )
        found = mounted(arguments)
        self.assertIn(("--tmpfs", str(elsewhere)), found)
        # The file is gone with its folder; it needs no mount of its own.
        self.assertNotIn(str(secret), options(arguments))

    def test_a_hidden_file_under_no_hidden_folder_is_covered(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        secret = elsewhere / "gate.json"
        secret.write_text("{}", encoding="utf-8")
        sandbox = Bubblewrap("bwrap", home=self.home, scratch=())
        arguments = sandbox.command(["true"], self.mounts(hidden=(self.repo, self.out, secret)))
        self.assertEqual(mounted(arguments)[-4], ("--ro-bind", os.devnull, str(secret)))

    def test_refuses_to_show_a_folder_that_holds_the_home_or_temporary_folders(self):
        for shown in (self.home, self.root):
            with (
                self.subTest(shown=shown),
                self.assertRaisesRegex(SandboxError, "which the agent must not see"),
            ):
                self.sandbox.command(["true"], self.mounts(read_only=(shown,)))
        with self.assertRaisesRegex(SandboxError, "which the agent must not see"):
            self.sandbox.command(["true"], self.mounts(workspace=self.home))

    def test_refuses_to_hide_a_folder_inside_a_shown_one_that_holds_another_shown_one(self):
        answers = self.raw / "answers"
        (answers / "notes").mkdir(parents=True)
        mounts = self.mounts(read_only=(self.raw, answers / "notes"), hidden=(self.repo, answers))
        with self.assertRaisesRegex(SandboxError, "cannot be hidden: a folder shown"):
            self.sandbox.command(["true"], mounts)

    def test_refuses_a_path_that_does_not_exist(self):
        with self.assertRaisesRegex(SandboxError, "does not exist"):
            self.sandbox.command(["true"], self.mounts(read_only=(self.root / "missing",)))

    def test_paths_outside_every_hidden_folder_are_not_mounted_again(self):
        tools = Path("/usr")
        arguments = self.sandbox.command(["true"], self.mounts(read_only=(tools, self.plugin)))
        self.assertNotIn(str(tools), options(arguments))

    def test_writable_paths_are_bound_read_write(self):
        config = self.home / ".claude"
        config.mkdir()
        arguments = self.sandbox.command(["true"], self.mounts(writable=(config,)))
        self.assertIn(("--bind", str(config), str(config)), mounted(arguments))

    def test_unisolated_leaves_the_command_as_it_is(self):
        self.assertEqual(Unisolated().command(["claude", "-p"], self.mounts()), ["claude", "-p"])


class RunnerMountsTest(unittest.TestCase):
    """What the runner shows each agent, and what it hides."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home = self.root / "home"
        (self.home / ".claude").mkdir(parents=True)
        (self.home / ".claude.json").write_text("{}", encoding="utf-8")
        self.out = self.root / "run"
        (self.out / "plugin").mkdir(parents=True)
        self.checks = self.root / "gate.json"
        self.raw = self.root / "data"
        self.claude = shutil.which("python3")

    def test_the_engineer_sees_the_raw_data_and_the_other_agents_do_not(self):
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}):
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            engineer = mounts(self.out / "project", self.out, self.checks, self.claude, self.raw)
            other = mounts(self.out / "cross_source", self.out, self.checks, self.claude)
        self.assertEqual(engineer.workspace, self.out / "project")
        self.assertEqual(engineer.read_only[:2], (self.raw, self.out / "plugin"))
        self.assertEqual(other.read_only[0], self.out / "plugin")
        self.assertNotIn(self.raw, other.read_only)
        for seen in (engineer, other):
            self.assertEqual(seen.hidden, (PLUGIN_DIR, self.out, self.checks))
            self.assertEqual(seen.writable, (self.home / ".claude", self.home / ".claude.json"))
        # Claude Code, and the Python that runs dbt, are shown.
        self.assertIn(Path(self.claude).resolve().parent, engineer.read_only)
        self.assertIn(Path(sys.prefix).resolve(), engineer.read_only)

    def test_claude_code_settings_follow_its_config_folder(self):
        config = self.root / "config"
        config.mkdir()
        with mock.patch.dict(
            os.environ, {"HOME": str(self.home), "CLAUDE_CONFIG_DIR": str(config)}
        ):
            seen = mounts(self.out / "project", self.out, self.checks, self.claude)
        self.assertEqual(seen.writable, (config, self.home / ".claude.json"))


class DefaultSandboxTest(unittest.TestCase):
    def test_unisolated_only_when_asked(self):
        self.assertIsInstance(default_sandbox(unisolated=True), Unisolated)
        self.assertFalse(default_sandbox(unisolated=True).isolated)

    def test_refuses_where_there_is_no_tested_sandbox(self):
        with self.assertRaisesRegex(SandboxError, "no tested sandbox on darwin.*--unisolated"):
            default_sandbox(platform="darwin")

    def test_refuses_without_bubblewrap_and_says_how_to_install_it(self):
        with (
            mock.patch("tenx.sandbox.which", return_value=None),
            self.assertRaises(SandboxError) as caught,
        ):
            default_sandbox(platform="linux")
        self.assertIn("bubblewrap (bwrap) is needed", str(caught.exception))
        self.assertIn(BWRAP_INSTALL, str(caught.exception))

    def test_refuses_a_bubblewrap_that_cannot_set_up_a_sandbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "bwrap"
            fake.write_text(
                "#!/bin/sh\necho 'bwrap: setting up uid map: Permission denied' >&2\nexit 1\n",
                encoding="utf-8",
            )
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            with (
                mock.patch("tenx.sandbox.which", return_value=str(fake)),
                self.assertRaisesRegex(
                    SandboxError, r"cannot set up a sandbox here \(bwrap: setting up uid map"
                ),
            ):
                default_sandbox(platform="linux")


class CommandLineSandboxTest(unittest.TestCase):
    """Refused without a sandbox; run, and marked, with --unisolated."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.raw = self.root / "data"
        generate(11, 20, self.raw, self.root / "answers")
        self.out = self.root / "run"
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        claude = bin_dir / "claude"
        claude.write_text(
            f"#!{sys.executable}\nimport json\n"
            'print(json.dumps({"type": "result", "subtype": "success", "num_turns": 1}))\n',
            encoding="utf-8",
        )
        claude.chmod(claude.stat().st_mode | stat.S_IXUSR)
        self.path = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"

    def main(self, *flags: str) -> tuple[int, str, str]:
        arguments = ["--raw", str(self.raw), "--goal", str(DEMO_GOAL)]
        arguments += ["--checks", str(DECLARATION), "--out", str(self.out), "--no-cross-source"]
        with (
            mock.patch.dict(os.environ, {"PATH": self.path}),
            redirect_stdout(io.StringIO()) as printed,
            redirect_stderr(io.StringIO()) as errors,
        ):
            code = main(arguments + list(flags))
        return code, printed.getvalue(), errors.getvalue()

    def test_without_bubblewrap_the_run_is_refused_before_anything_is_written(self):
        with mock.patch("tenx.sandbox.which", return_value=None):
            code, printed, errors = self.main()
        self.assertEqual(code, 2)
        self.assertEqual(printed, "")
        self.assertIn("error: bubblewrap (bwrap) is needed", errors)
        self.assertFalse(self.out.exists())

    def test_an_unisolated_run_goes_ahead_and_says_so(self):
        with mock.patch("tenx.sandbox.which", return_value=None):
            code, printed, _ = self.main("--unisolated")
        self.assertEqual(code, 1, printed)  # the stand-in writes no models
        self.assertIn(
            "sandbox: none (--unisolated): the agents could read and change any file you can\n",
            printed,
        )
        ledger = json.loads((self.out / USAGE_FILE).read_text(encoding="utf-8"))
        self.assertEqual(ledger["sandbox"], "none")
