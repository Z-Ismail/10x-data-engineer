"""Tests for the gate's dbt builds in the real sandbox.

The gate builds the project an agent wrote, and a dbt Python model in it
runs any Python while it is built. Here a hostile project, the reference
project plus one Python model, tries during both of the gate's builds to
read the declaration, the expected rows, the first build's database and a
key in the home folder, and to change the declaration, the gate's databases,
the project, the raw data and the home folder. In the sandbox every attempt
must fail and the reference models must still pass; without it the same
model must succeed at each, so the probes are shown able to see a leak.

Skipped where bubblewrap cannot run, unless TENX_REQUIRE_SANDBOX is set, as
it is in CI; then they fail instead.
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import duckdb

from demo.generate import generate
from tenx.gate import DATABASE_FILE, EXPECTED_FILE, REPEAT_DIR, REPEAT_FILE, passed, run_gate
from tenx.sandbox import SandboxError, Unisolated, default_sandbox

FIXTURES = Path(__file__).parent / "fixtures"
PROJECT = FIXTURES / "demo_project"
DECLARATION = FIXTURES / "demo_gate.json"
TAMPERED = "tenx_tampered"

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
from pathlib import Path

import duckdb

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
    }}
    if second:
        found["first database"] = read(PATHS["first"])
        found["tamper first database"] = tamper(PATHS["first"])
    text = json.dumps(found).replace("'", "''")
    return session.sql(f"select '{{text}}' as found")
"""


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
    model = HOSTILE_MODEL.format(paths=json.dumps(paths), tampered=TAMPERED)
    (project / "models" / "leak.py").write_text(model, encoding="utf-8")
    results = run_gate(project, raw, checks, work, sandbox)

    def found(database: Path) -> dict:
        con = duckdb.connect(str(database), read_only=True)
        try:
            return json.loads(con.execute("select found from leak").fetchone()[0])
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
        "declaration intact": checks.read_bytes() == DECLARATION.read_bytes(),
        "expected tampered": tampered(work / EXPECTED_FILE),
        "first tampered": tampered(work / DATABASE_FILE),
        "escaped": [name for name in ("project", "raw", "home") if Path(paths[name]).exists()],
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
