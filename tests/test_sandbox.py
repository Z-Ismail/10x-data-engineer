"""Tests for the sandbox the agents run in.

The command line bubblewrap is given is checked against paths worked out by
hand. Then a hostile stand-in for the claude command tries, from inside the
real sandbox, every way of reading what an agent must never see: the gate's
declaration, the reference answers, the run's other files and the home
folder's keys, by `cat`, by Python, by `dbt show` with `read_text()` and by
a dbt Python model. It must also be able to read the raw data, write its
project and build the reference models, which the gate then passes.

The sandbox tests are skipped where bubblewrap cannot run, unless
TENX_REQUIRE_SANDBOX is set, as it is in CI; then they fail instead.
"""

import io
import json
import os
import shutil
import stat
import sys
import tempfile
import time
import unittest
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from demo.generate import generate
from tenx.engineer import AGENT_FILE, PLUGIN_DIR, PROJECT_DIR, USAGE_FILE, main, mounts, run_agent
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


def _sandbox_works() -> str:
    """Why the sandbox cannot run here, or an empty string if it can."""
    try:
        default_sandbox()
    except SandboxError as error:
        return str(error)
    return ""


SANDBOX_PROBLEM = _sandbox_works()
REQUIRED = bool(os.environ.get("TENX_REQUIRE_SANDBOX"))


def needs_sandbox(test_class):
    if SANDBOX_PROBLEM and not REQUIRED:
        return unittest.skip(f"no working sandbox: {SANDBOX_PROBLEM}")(test_class)
    return test_class


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


# The hostile stand-in, started as the engineer inside the sandbox. It tries
# to read everything in PROBE_SECRETS, to read the raw data, to write outside
# its project, and to read the declaration through dbt, then copies the
# reference models (kept beside it) into the project. What it saw goes into
# probes.json in the project, the one place a write is kept.
PROBE = r"""
import json, os, subprocess, sys
from pathlib import Path

here = Path.cwd()
secrets = json.loads(os.environ["PROBE_SECRETS"])
found = {"cat": {}, "python": {}, "list": {}}
for secret in secrets:
    done = subprocess.run(["cat", secret], capture_output=True, text=True)
    found["cat"][secret] = done.returncode == 0 and bool(done.stdout)
    try:
        found["python"][secret] = bool(Path(secret).read_text())
    except OSError:
        found["python"][secret] = False
    try:
        found["list"][secret] = sorted(os.listdir(secret)) if Path(secret).is_dir() else None
    except OSError:
        found["list"][secret] = None
found["out_listing"] = sorted(os.listdir(here.parent))
found["raw"] = Path(os.environ["PROBE_RAW"]).read_text()
for name, target in json.loads(os.environ["PROBE_WRITES"]).items():
    try:
        Path(target).write_text("escaped")
        found[f"write {name}"] = Path(target).read_text() == "escaped"
    except OSError:
        found[f"write {name}"] = False

def dbt(*arguments):
    done = subprocess.run(["dbt", *arguments, "--no-use-colors"], capture_output=True, text=True)
    return done.returncode, done.stdout + done.stderr

declaration = os.environ["PROBE_DECLARATION"]
query = f"select contains(content, 'exclusions') as leaked from read_text('{declaration}')"
code, output = dbt("show", "--inline", query, "--output", "json")
found["dbt show"] = {"exit": code, "leaked": '"leaked": true' in output, "tail": output[-600:]}
model = here / "models" / "leak.py"
model.parent.mkdir(exist_ok=True)
model.write_text(
    "def model(dbt, session):\n"
    "    from pathlib import Path\n"
    "    try:\n"
    f"        text = Path({declaration!r}).read_text()\n"
    "    except OSError as error:\n"
    "        text = 'blocked: ' + type(error).__name__\n"
    f"    Path({str(here / 'leak.txt')!r}).write_text(text)\n"
    "    return session.sql('select 1 as x')\n"
)
code, output = dbt("run", "--select", "leak")
found["dbt python model"] = {"exit": code, "read": (here / "leak.txt").read_text()}
model.unlink()
(here / "leak.txt").unlink()
pids = [name for name in os.listdir("/proc") if name.isdigit()]
found["processes"] = len(pids)
import shutil
shutil.copytree(Path(__file__).parent / "models", here / "models", dirs_exist_ok=True)
(here / "probes.json").write_text(json.dumps(found))
print(json.dumps({"type": "result", "subtype": "success", "num_turns": 1}))
"""


def probe_run(root: Path, writes: dict, *flags: str, sandbox=None) -> tuple[int, str, dict | None]:
    """A run whose engineer is the hostile stand-in. Returns the exit code,
    what was printed, and what the stand-in saw. ``sandbox`` replaces the
    default one."""
    raw = root / "data"
    generate(11, 20, raw, root / "answers")
    # A copy of the declaration, named to the runner as the checks.
    checks = root / "checks" / "gate.json"
    checks.parent.mkdir()
    shutil.copyfile(DECLARATION, checks)
    home = root / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_test").write_text("PRIVATE KEY", encoding="utf-8")
    bin_dir = root / "bin"
    bin_dir.mkdir()
    claude = bin_dir / "claude"
    claude.write_text(f"#!{sys.executable}\n{PROBE}", encoding="utf-8")
    claude.chmod(claude.stat().st_mode | stat.S_IXUSR)
    shutil.copytree(REFERENCE_MODELS, bin_dir / "models")
    out = root / "run"
    secrets = [
        str(checks),
        str(DECLARATION),
        str(FIXTURES),
        str(home / ".ssh"),
        str(out / (AGENT_FILE + ".partial")),
    ]
    # The answer key sits beside the raw data. The runner is never told
    # where it is, so it is hidden only when it is in a folder hidden anyway.
    if sandbox is None:
        secrets.append(str(root / "answers"))
    environment = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(home),
        "PROBE_SECRETS": json.dumps(secrets),
        "PROBE_RAW": str(raw / "billing" / "customers.csv"),
        "PROBE_DECLARATION": str(checks),
        "PROBE_WRITES": json.dumps(writes),
    }
    arguments = ["--raw", str(raw), "--goal", str(DEMO_GOAL), "--checks", str(checks)]
    arguments += ["--out", str(out), "--no-cross-source", *flags]
    replace = (
        mock.patch("tenx.engineer.default_sandbox", return_value=sandbox(home))
        if sandbox
        else nullcontext()
    )
    with (
        mock.patch.dict(os.environ, environment),
        replace,
        redirect_stdout(io.StringIO()) as printed,
    ):
        code = main(arguments)
    probes = out / PROJECT_DIR / "probes.json"
    return code, printed.getvalue(), json.loads(probes.read_text()) if probes.is_file() else None


class UnisolatedProbeTest(unittest.TestCase):
    """Without the sandbox the same stand-in sees everything, so the probes can see a leak."""

    def test_every_probe_finds_its_secret_without_the_sandbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            writes = {"beside the run": str(root / "outside.txt")}
            code, printed, probes = probe_run(root, writes, "--unisolated")
            self.assertEqual(code, 0, printed)
            for way in ("cat", "python"):
                for secret, seen in probes[way].items():
                    if secret.endswith(".partial") or Path(secret).is_dir():
                        continue
                    with self.subTest(way=way, secret=secret):
                        self.assertTrue(seen)
            self.assertIn("id_test", probes["list"][str(root / "home" / ".ssh")])
            self.assertIn("agent.json.partial", probes["out_listing"])
            self.assertTrue(probes["dbt show"]["leaked"])
            self.assertEqual(
                probes["dbt python model"]["read"], (root / "checks" / "gate.json").read_text()
            )
            self.assertTrue((root / "outside.txt").is_file())


@needs_sandbox
class SandboxedRunTest(unittest.TestCase):
    """A full run in the real sandbox, with a stand-in that tries to see what it must not."""

    # The sandbox to use instead of the default one, made from the home
    # folder, and where to keep the test's files (by default, a temporary
    # folder).
    sandbox = None
    location = None

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(dir=cls.location)
        cls.root = Path(cls.tmp.name).resolve()
        cls.out = cls.root / "run"
        cls.home = cls.root / "home"
        cls.raw = cls.root / "data"
        cls.outside = cls.root / "outside.txt"
        cls.escapes = [cls.outside, cls.home / "escape.txt", PLUGIN_DIR / "escape.txt"]
        cls.escapes.append(Path("/etc/tenx-escape.txt"))
        writes = dict(
            zip(["beside the run", "home", "repository", "system"], map(str, cls.escapes))
        )
        cls.code, cls.printed, cls.probes = probe_run(cls.root, writes, sandbox=cls.sandbox)

    @classmethod
    def tearDownClass(cls):
        for path in cls.escapes[2:]:
            path.unlink(missing_ok=True)
        cls.tmp.cleanup()

    def setUp(self):
        self.assertIsNotNone(self.probes, f"the stand-in left no probes.json:\n{self.printed}")

    def test_nothing_hidden_can_be_read_by_cat_or_python(self):
        for way in ("cat", "python", "list"):
            for secret, seen in self.probes[way].items():
                with self.subTest(way=way, secret=secret):
                    self.assertFalse(seen)

    def test_the_run_folder_shows_only_the_project_and_the_add_on(self):
        self.assertEqual(self.probes["out_listing"], ["plugin", "project"])

    def test_dbt_cannot_read_the_declaration(self):
        # The declaration is either not there or covered by a device that
        # cannot be opened, so DuckDB gives no rows or an error, and Python an
        # error; either way nothing of it comes back.
        shown = self.probes["dbt show"]
        self.assertFalse(shown["leaked"], shown["tail"])
        model = self.probes["dbt python model"]
        self.assertEqual(model["exit"], 0)
        self.assertIn(model["read"], ("blocked: FileNotFoundError", "blocked: PermissionError"))

    def test_raw_data_is_readable(self):
        self.assertEqual(
            self.probes["raw"], (self.raw / "billing" / "customers.csv").read_text("utf-8")
        )

    def test_writes_outside_the_project_do_not_last(self):
        self.assertFalse(self.probes["write system"])
        for path in self.escapes:
            with self.subTest(path=path):
                self.assertFalse(path.exists())

    def test_the_agent_has_its_own_process_ids(self):
        # bubblewrap's init, the stand-in and at most the commands it is running.
        self.assertLess(self.probes["processes"], 6)

    def test_the_reference_models_built_in_the_sandbox_pass_the_gate(self):
        self.assertEqual(self.code, 0, self.printed)
        self.assertIn("gate: PASS (24 passed, 0 failed, 0 not run)", self.printed)
        self.assertIn("sandbox: bubblewrap: the agents see only their own folder", self.printed)
        ledger = json.loads((self.out / USAGE_FILE).read_text(encoding="utf-8"))
        self.assertEqual((ledger["gate"], ledger["sandbox"]), ("PASS", "bubblewrap"))


class OutsideTemporaryFoldersRunTest(SandboxedRunTest):
    """The same run with the test's files outside the temporary folders.

    In a temporary folder, the test's files are hidden whatever else the
    sandbox does. Here they are in the real home folder, which the sandbox
    leaves visible because it is told the home folder is the test's own, so
    the run folder, the declaration, the home folder and the repository
    must each be hidden in their own right.
    """

    location = Path.home()

    @staticmethod
    def sandbox(home: Path) -> Bubblewrap:
        return Bubblewrap(default_sandbox().executable, home=home)


# Started in the sandbox: a child that keeps the output open and writes a
# heartbeat into the workspace, then the agent hangs.
HANGS_WITH_A_CHILD = """
import subprocess, sys, time
subprocess.Popen([sys.executable, "-c", '''
import time
from pathlib import Path
beat = Path("heartbeat")
while True:
    beat.write_text(str(time.time()))
    time.sleep(0.1)
'''])
time.sleep(120)
"""


@needs_sandbox
class SandboxedTimeoutTest(unittest.TestCase):
    def test_a_timed_out_agent_and_its_child_stop_in_the_sandbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            workspace = root / "work"
            workspace.mkdir()
            agent = root / "tools" / "agent.py"
            agent.parent.mkdir()
            agent.write_text(HANGS_WITH_A_CHILD, encoding="utf-8")
            mounts = Mounts(workspace=workspace, read_only=(agent.parent,))
            with mock.patch("tenx.engineer.STOP_GRACE_SECONDS", 2):
                started = time.monotonic()
                outcome = run_agent(
                    workspace,
                    [sys.executable, str(agent)],
                    root / "out" / "agent.json",
                    3,
                    default_sandbox(),
                    mounts,
                )
                elapsed = time.monotonic() - started
            self.assertEqual(outcome.timed_out_after, 3)
            self.assertLess(elapsed, 3 + 2 + 10)
            beat = workspace / "heartbeat"
            self.assertTrue(beat.is_file(), "the child never ran")
            last = beat.read_text()
            time.sleep(1)
            self.assertEqual(beat.read_text(), last, "the agent's child is still running")


if __name__ == "__main__":
    unittest.main()
