"""Tests for the gate's dbt builds in the real sandbox.

The gate builds the project an agent wrote, and a dbt Python model in it
runs any Python while it is built. Here a hostile project, the reference
project plus one Python model, tries during both of the gate's builds to
read the declaration, the expected rows, the first build's database, a
key in the home folder and a variable in the caller's environment, and to change the declaration, the gate's databases,
the project, the raw data and the home folder. In the sandbox every attempt
must fail and the reference models must still pass; without it the same
model must succeed at each, so the probes are shown able to see a leak.

Skipped where bubblewrap cannot run, unless TENX_REQUIRE_SANDBOX is set, as
it is in CI; then they fail instead.
"""

import io
import json
import os
import shutil
import socket
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import duckdb

from demo.generate import generate
from tenx.gate import (
    BUILD_ENVIRONMENT,
    DATABASE_FILE,
    EXPECTED_FILE,
    REPEAT_DIR,
    REPEAT_FILE,
    build_project,
    main,
    passed,
    run_gate,
)
from tenx.sandbox import Bubblewrap, SandboxError, Unisolated, default_sandbox

FIXTURES = Path(__file__).parent / "fixtures"
PROJECT = FIXTURES / "demo_project"
DECLARATION = FIXTURES / "demo_gate.json"
TAMPERED = "tenx_tampered"
TOKEN = "gate-token-5678"
# Set only in .env files: one in the project, one above the gate's work folder.
PROJECT_TOKEN = "dotenv-project-1357"
ABOVE_TOKEN = "dotenv-above-2468"
DOTENV_MODEL = (
    'select \'{{ env_var("TENX_DOTENV_PROJECT", "unset") }}\' as project,\n'
    '    \'{{ env_var("TENX_DOTENV_ABOVE", "unset") }}\' as above\n'
)

BLOCKED = ("", "blocked: FileNotFoundError", "blocked: PermissionError")


def _sandbox_problem() -> str:
    try:
        default_sandbox()
    except SandboxError as error:
        return str(error)
    return ""


SANDBOX_PROBLEM = _sandbox_problem()


def needs_sandbox(test_class):
    if SANDBOX_PROBLEM and not os.environ.get("TENX_REQUIRE_SANDBOX"):
        return unittest.skip(f"no working sandbox: {SANDBOX_PROBLEM}")(test_class)
    return test_class


# Paths are filled in per test. The model records what it managed in a
# one-row table, so each build's own database shows what that build did.
HOSTILE_MODEL = """
import json
import os
from pathlib import Path

import duckdb
import socket

PATHS = json.loads({paths!r})


def read(path):
    try:
        return Path(path).read_bytes()[:4000].decode("utf-8", "replace")
    except OSError as error:
        return "blocked: " + type(error).__name__


def tamper(path):
    try:
        con = duckdb.connect(path)
        con.execute("create or replace table {tampered} as select 1 as x")
        con.close()
        return "written"
    except Exception as error:
        return "blocked: " + type(error).__name__


def write(path):
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("escaped")
        return "written"
    except OSError as error:
        return "blocked: " + type(error).__name__


def connect(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=5).close()
        return "connected"
    except OSError as error:
        return "blocked: " + type(error).__name__


def model(dbt, session):
    second = Path(PATHS["repeat"]).is_dir()
    found = {{
        "second build": second,
        "declaration": read(PATHS["declaration"]),
        "expected rows": read(PATHS["expected"]),
        "home key": read(PATHS["key"]),
        "write declaration": write(PATHS["declaration"]),
        "tamper expected rows": tamper(PATHS["expected"]),
        "write project": write(PATHS["project"]),
        "write raw": write(PATHS["raw"]),
        "write home": write(PATHS["home"]),
        "environment token": os.environ.get("TENX_PROBE_TOKEN", "unset"),
        "network": connect(PATHS["port"]),
        "system sockets": sorted(os.listdir("/run")) if os.path.isdir("/run") else [],
    }}
    if second:
        found["first database"] = read(PATHS["first"])
        found["tamper first database"] = tamper(PATHS["first"])
    text = json.dumps(found).replace("'", "''")
    return session.sql(f"select '{{text}}' as found")
"""


class Listener:
    """A TCP listener on the host's loopback that counts who connects."""

    def __init__(self):
        self.server = socket.create_server(("127.0.0.1", 0))
        self.server.settimeout(0.2)
        self.port = self.server.getsockname()[1]
        self.connections = 0
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._serve)
        self.thread.start()

    def _serve(self):
        while not self.stopped.is_set():
            try:
                connection, _ = self.server.accept()
            except TimeoutError:
                continue
            connection.close()
            self.connections += 1

    def stop(self) -> int:
        self.stopped.set()
        self.thread.join()
        self.server.close()
        return self.connections


def write_dotenv(project: Path, above: Path) -> None:
    """A .env in the project and one in a folder above the gate's work folder."""
    (project / ".env").write_text(f"TENX_DOTENV_PROJECT={PROJECT_TOKEN}\n", encoding="utf-8")
    (above / ".env").write_text(f"TENX_DOTENV_ABOVE={ABOVE_TOKEN}\n", encoding="utf-8")


def hostile_gate(root: Path, sandbox) -> dict:
    """Run the gate on the hostile project; return what it saw and did."""
    raw = root / "data"
    generate(11, 20, raw, root / "answers")
    checks = root / "checks" / "gate.json"
    checks.parent.mkdir()
    shutil.copyfile(DECLARATION, checks)
    key = root / "home" / ".ssh" / "id_test"
    key.parent.mkdir(parents=True)
    key.write_text("PRIVATE KEY", encoding="utf-8")
    project = root / "project"
    shutil.copytree(PROJECT, project)
    work = root / "work"
    paths = {
        "declaration": str(checks),
        "expected": str(work / EXPECTED_FILE),
        "first": str(work / DATABASE_FILE),
        "repeat": str(work / REPEAT_DIR),
        "key": str(key),
        "project": str(project / "escape.txt"),
        "raw": str(raw / "escape.txt"),
        "home": str(root / "home" / "escape.txt"),
    }
    listener = Listener()
    paths["port"] = listener.port
    model = HOSTILE_MODEL.format(paths=json.dumps(paths), tampered=TAMPERED)
    (project / "models" / "leak.py").write_text(model, encoding="utf-8")
    (project / "models" / "leak_env.sql").write_text(
        'select \'{{ env_var("TENX_PROBE_TOKEN", "unset") }}\' as token\n', encoding="utf-8"
    )
    (project / "models" / "leak_dotenv.sql").write_text(DOTENV_MODEL, encoding="utf-8")
    write_dotenv(project, root)
    try:
        with mock.patch.dict(os.environ, {"TENX_PROBE_TOKEN": TOKEN}):
            results = run_gate(project, raw, checks, work, sandbox)
    finally:
        connections = listener.stop()

    def found(database: Path) -> dict:
        con = duckdb.connect(str(database), read_only=True)
        try:
            return json.loads(con.execute("select found from leak").fetchone()[0])
        finally:
            con.close()

    def token(database: Path) -> str:
        con = duckdb.connect(str(database), read_only=True)
        try:
            return con.execute("select token from leak_env").fetchone()[0]
        finally:
            con.close()

    def dotenv(database: Path) -> tuple[str, str]:
        con = duckdb.connect(str(database), read_only=True)
        try:
            return con.execute("select project, above from leak_dotenv").fetchone()
        finally:
            con.close()

    def tampered(database: Path) -> bool:
        con = duckdb.connect(str(database), read_only=True)
        try:
            tables = {
                name for (name,) in con.execute("select table_name from duckdb_tables()").fetchall()
            }
        finally:
            con.close()
        return TAMPERED in tables

    return {
        "results": results,
        "first": found(work / DATABASE_FILE),
        "second": found(work / REPEAT_DIR / REPEAT_FILE),
        "env_var": [token(work / DATABASE_FILE), token(work / REPEAT_DIR / REPEAT_FILE)],
        "declaration intact": checks.read_bytes() == DECLARATION.read_bytes(),
        "expected tampered": tampered(work / EXPECTED_FILE),
        "first tampered": tampered(work / DATABASE_FILE),
        "escaped": [name for name in ("project", "raw", "home") if Path(paths[name]).exists()],
        "connections": connections,
        "dotenv": [dotenv(work / DATABASE_FILE), dotenv(work / REPEAT_DIR / REPEAT_FILE)],
    }


class UnisolatedGateProbeTest(unittest.TestCase):
    """Without the sandbox the hostile model reads and changes everything it tries."""

    def test_every_probe_succeeds_without_the_sandbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            seen = hostile_gate(Path(tmp).resolve(), Unisolated())
        first, second = seen["first"], seen["second"]
        self.assertEqual((first["second build"], second["second build"]), (False, True))
        for build in (first, second):
            self.assertIn("exclusions", build["declaration"])
            self.assertNotIn("blocked", build["expected rows"])
            self.assertEqual(build["home key"], "PRIVATE KEY")
            for probe in ("write declaration", "tamper expected rows", "write project"):
                self.assertEqual(build[probe], "written", probe)
        self.assertNotIn("blocked", second["first database"])
        self.assertEqual(second["tamper first database"], "written")
        self.assertFalse(seen["declaration intact"])
        self.assertTrue(seen["expected tampered"])
        self.assertTrue(seen["first tampered"])
        self.assertEqual(seen["escaped"], ["project", "raw", "home"])
        self.assertEqual(seen["env_var"], [TOKEN, TOKEN])
        self.assertEqual([first["environment token"], second["environment token"]], [TOKEN, TOKEN])
        self.assertEqual([first["network"], second["network"]], ["connected", "connected"])
        self.assertEqual(seen["connections"], 2)
        if os.path.isdir("/run") and os.listdir("/run"):
            self.assertEqual(first["system sockets"], sorted(os.listdir("/run")))
        # Not a sandbox matter: the builds never load .env, isolated or not.
        self.assertEqual(seen["dotenv"], [("unset", "unset"), ("unset", "unset")])


@needs_sandbox
class SandboxedGateTest(unittest.TestCase):
    """The gate's builds in the real sandbox: nothing hidden is read or changed."""

    # Where to keep the test's files (by default, a temporary folder), and
    # the sandbox to use, made from the test's home folder.
    location = None

    @staticmethod
    def sandbox(home: Path) -> Bubblewrap:
        return default_sandbox()

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(dir=cls.location)
        cls.root = Path(cls.tmp.name).resolve()
        cls.seen = hostile_gate(cls.root, cls.sandbox(cls.root / "home"))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_both_builds_ran_the_hostile_model(self):
        self.assertFalse(self.seen["first"]["second build"])
        self.assertTrue(self.seen["second"]["second build"])

    def test_nothing_hidden_can_be_read(self):
        for name in ("first", "second"):
            build = self.seen[name]
            for probe in ("declaration", "expected rows", "home key"):
                with self.subTest(build=name, probe=probe):
                    self.assertIn(build[probe], BLOCKED)
        self.assertIn(self.seen["second"]["first database"], BLOCKED)

    def test_a_secret_in_the_environment_reaches_neither_build(self):
        self.assertEqual(self.seen["env_var"], ["unset", "unset"])
        for name in ("first", "second"):
            self.assertEqual(self.seen[name]["environment token"], "unset")

    def test_neither_build_reaches_the_network_or_the_system_sockets(self):
        for name in ("first", "second"):
            build = self.seen[name]
            with self.subTest(build=name):
                self.assertTrue(build["network"].startswith("blocked"), build["network"])
                # /run/user is emptied for every sandbox.
                self.assertLessEqual(set(build["system sockets"]), {"user"})
        self.assertEqual(self.seen["connections"], 0)

    def test_neither_build_loads_a_dotenv_file(self):
        self.assertEqual(self.seen["dotenv"], [("unset", "unset"), ("unset", "unset")])

    def test_nothing_outside_the_build_folder_is_changed(self):
        self.assertTrue(self.seen["declaration intact"])
        self.assertFalse(self.seen["expected tampered"])
        self.assertFalse(self.seen["first tampered"])
        self.assertEqual(self.seen["escaped"], [])

    def test_the_reference_models_still_pass(self):
        failed = {
            (result.check, result.subject)
            for result in self.seen["results"]
            if result.status != "pass"
        }
        # The hostile model's own row differs between the builds.
        self.assertLessEqual(failed, {("repeat", "leak")})
        declared = [r for r in self.seen["results"] if r.subject != "leak"]
        self.assertTrue(passed(declared))


class OutsideTemporaryFoldersGateTest(SandboxedGateTest):
    """The same, with the test's files in the real home folder.

    In a temporary folder everything of the test is hidden whatever else the
    sandbox does. Here the sandbox is told the home folder is the test's own,
    so the declaration, the expected rows and the first database must each
    be hidden in their own right.
    """

    location = Path.home()

    @staticmethod
    def sandbox(home: Path) -> Bubblewrap:
        return Bubblewrap(default_sandbox().executable, home=home)


class DotenvTest(unittest.TestCase):
    """dbt loads .env unless told not to; the gate's builds tell it.

    The switch is taken away in two tests, to show that the .env files are
    read without it: the project's when built unisolated (dbt starts in the
    project), and the one above the work folder in the sandbox (dbt starts
    in the work folder, and the test's folder is not hidden).
    """

    def build(self, sandbox_for, switch: bool) -> tuple[str, str]:
        tmp = tempfile.TemporaryDirectory(dir=Path.home())
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        project, raw, work = root / "project", root / "data", root / "work"
        (project / "models").mkdir(parents=True)
        raw.mkdir()
        shutil.copyfile(PROJECT / "dbt_project.yml", project / "dbt_project.yml")
        (project / "models" / "leak_dotenv.sql").write_text(DOTENV_MODEL, encoding="utf-8")
        write_dotenv(project, root)
        settings = BUILD_ENVIRONMENT if switch else {}
        with mock.patch("tenx.gate.BUILD_ENVIRONMENT", settings):
            code, _, printed = build_project(project, raw, work, sandbox=sandbox_for(root))
        self.assertEqual(code, 0, printed)
        con = duckdb.connect(str(work / DATABASE_FILE), read_only=True)
        try:
            return con.execute("select project, above from leak_dotenv").fetchone()
        finally:
            con.close()

    def test_the_switch_is_set(self):
        self.assertEqual(BUILD_ENVIRONMENT, {"PYTHON_DOTENV_DISABLED": "1"})

    def test_unisolated_the_projects_dotenv_is_read_only_without_the_switch(self):
        self.assertEqual(self.build(lambda root: Unisolated(), False), (PROJECT_TOKEN, "unset"))
        self.assertEqual(self.build(lambda root: Unisolated(), True), ("unset", "unset"))

    @unittest.skipIf(
        SANDBOX_PROBLEM and not os.environ.get("TENX_REQUIRE_SANDBOX"), "no working sandbox"
    )
    def test_sandboxed_a_dotenv_above_is_read_only_without_the_switch(self):
        def sandbox(root: Path) -> Bubblewrap:
            return Bubblewrap(default_sandbox().executable, home=root / "home")

        self.assertEqual(self.build(sandbox, False), ("unset", ABOVE_TOKEN))
        self.assertEqual(self.build(sandbox, True), ("unset", "unset"))


@needs_sandbox
class SandboxedReferenceTest(unittest.TestCase):
    def test_the_reference_project_passes_in_the_sandbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            generate(11, 20, root / "data", root / "answers")
            results = run_gate(
                PROJECT, root / "data", DECLARATION, root / "work", default_sandbox()
            )
        self.assertTrue(passed(results))
        self.assertEqual(len(results), 24)


class CommandLineTest(unittest.TestCase):
    """The gate's command builds in the sandbox, refuses without one, or says it has none."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        generate(11, 20, self.root / "data", self.root / "answers")
        self.work = self.root / "work"

    def main(self, *flags: str) -> tuple[int, str, str]:
        arguments = [str(PROJECT), "--raw", str(self.root / "data"), "--checks", str(DECLARATION)]
        with (
            redirect_stdout(io.StringIO()) as printed,
            redirect_stderr(io.StringIO()) as errors,
        ):
            code = main([*arguments, "--workdir", str(self.work), *flags])
        return code, printed.getvalue(), errors.getvalue()

    def test_without_bubblewrap_nothing_is_built(self):
        with mock.patch("tenx.sandbox.which", return_value=None):
            code, printed, errors = self.main()
        self.assertEqual((code, printed), (2, ""))
        self.assertIn("error: bubblewrap (bwrap) is needed", errors)
        self.assertFalse(self.work.exists())

    def test_an_unisolated_build_goes_ahead_and_says_so(self):
        with mock.patch("tenx.sandbox.which", return_value=None):
            code, printed, errors = self.main("--unisolated")
        self.assertEqual(code, 0, printed + errors)
        self.assertEqual(errors, "warning: built without a sandbox (--unisolated)\n")
        self.assertTrue(printed.startswith("gate: PASS (24 passed"))

    @unittest.skipIf(
        SANDBOX_PROBLEM and not os.environ.get("TENX_REQUIRE_SANDBOX"), "no working sandbox"
    )
    def test_by_default_the_builds_run_in_bubblewrap(self):
        with mock.patch("tenx.gate.run_gate", wraps=run_gate) as gate:
            code, printed, errors = self.main()
        self.assertEqual((code, errors), (0, ""), printed)
        self.assertTrue(printed.startswith("gate: PASS (24 passed"))
        self.assertIsInstance(gate.call_args.args[4], Bubblewrap)
