"""The interfaces, and the pipeline code built on them, reach no platform directly.

``tenx/ports.py`` and everything under ``tenx/graph/`` may not import DuckDB,
dbt, the gate, the profiler, the overlap report, the old runner (which
imports the gate) or the adapters themselves. Two checks: the source is read
for every way of writing an import, and a fresh interpreter that imports the
interfaces is asked which modules it loaded, which also catches imports made
indirectly.
"""

import ast
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN = (
    "duckdb",
    "dbt",
    "tenx.gate",
    "tenx.profile",
    "tenx.overlap",
    "tenx.engineer",
    "tenx.adapters",
    "tenx.backends",
)
IMPORTERS = {"import_module", "__import__"}


def _blocked(name: str) -> bool:
    return any(name == banned or name.startswith(banned + ".") for banned in FORBIDDEN)


def _absolute(module: str | None, level: int, package: str) -> str:
    if level == 0:
        return module or ""
    parts = package.split(".")
    base = parts[: len(parts) - level + 1]
    return ".".join([*base, module] if module else base)


def forbidden_imports(source: str, package: str) -> list[str]:
    """Every forbidden module the source imports, written in any form.

    ``package`` is the package the source lives in, for relative imports.
    """
    found = []
    for node in ast.walk(ast.parse(source)):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            module = _absolute(node.module, node.level, package)
            names = [module, *(f"{module}.{alias.name}" for alias in node.names)]
        elif isinstance(node, ast.Call):
            func = node.func
            called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            first = node.args[0] if node.args else None
            literal = isinstance(first, ast.Constant) and isinstance(first.value, str)
            if called in IMPORTERS and literal:
                names = [first.value]
        found += [name for name in names if _blocked(name)]
    return found


def guarded_files() -> list[Path]:
    return [ROOT / "tenx" / "ports.py", *sorted((ROOT / "tenx" / "graph").rglob("*.py"))]


def _package(path: Path) -> str:
    parts = path.relative_to(ROOT).with_suffix("").parts
    return ".".join(parts[:-1])


LOADED = (
    "import sys, {module}; "
    "print('\\n'.join(sorted(name for name in sys.modules if name.split('.')[0] "
    "in ('duckdb', 'dbt', 'tenx'))))"
)


def loaded_by(module: str) -> set[str]:
    """The DuckDB, dbt and tenx modules a fresh interpreter holds after importing ``module``."""
    finished = subprocess.run(
        [sys.executable, "-c", LOADED.format(module=module)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return set(finished.stdout.split())


class ForbiddenImportsTest(unittest.TestCase):
    def test_every_form_of_a_forbidden_import_is_found(self):
        cases = {
            "import duckdb": ["duckdb"],
            "import duckdb as db": ["duckdb"],
            "import dbt.cli.main": ["dbt.cli.main"],
            "from dbt.cli.main import cli": ["dbt.cli.main", "dbt.cli.main.cli"],
            "from tenx import gate": ["tenx.gate"],
            "from tenx.gate import run_gate as g": ["tenx.gate", "tenx.gate.run_gate"],
            "import tenx.profile": ["tenx.profile"],
            "from .. import overlap": ["tenx.overlap"],
            "from ..engineer import run": ["tenx.engineer", "tenx.engineer.run"],
            "from ..adapters.duckdb import DuckDBGate": [
                "tenx.adapters.duckdb",
                "tenx.adapters.duckdb.DuckDBGate",
            ],
            "def f():\n    import duckdb": ["duckdb"],
            "import importlib\nimportlib.import_module('duckdb')": ["duckdb"],
            "from importlib import import_module\nimport_module('tenx.gate')": ["tenx.gate"],
            "__import__('dbt')": ["dbt"],
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(forbidden_imports(source, "tenx.graph"), expected)

    def test_allowed_imports_pass(self):
        source = (
            "import json\n"
            "import duckdbx\n"
            "from tenx import ports, artefacts\n"
            "from tenx.ports import Validator\n"
            "from . import state\n"
            "from ..ledger import Ledger\n"
            "from tenx.gateway import x\n"
            "import_module(name)\n"
        )
        self.assertEqual(forbidden_imports(source, "tenx.graph"), [])


class GuardedCodeTest(unittest.TestCase):
    def test_the_guarded_files_import_nothing_forbidden(self):
        files = guarded_files()
        self.assertTrue(files[0].is_file())
        for path in files:
            with self.subTest(path=str(path.relative_to(ROOT))):
                source = path.read_text(encoding="utf-8")
                self.assertEqual(forbidden_imports(source, _package(path)), [])

    def test_a_copy_of_the_interfaces_with_a_forbidden_import_fails(self):
        source = (ROOT / "tenx" / "ports.py").read_text(encoding="utf-8")
        self.assertEqual(forbidden_imports(source + "\nimport duckdb\n", "tenx"), ["duckdb"])

    def test_importing_the_guarded_code_loads_no_platform_module(self):
        for path in guarded_files():
            module = ".".join(path.relative_to(ROOT).with_suffix("").parts)
            module = module.removesuffix(".__init__")
            with self.subTest(module=module):
                loaded = loaded_by(module)
                self.assertIn(module, loaded)
                self.assertEqual(sorted(name for name in loaded if _blocked(name)), [])

    def test_the_load_check_sees_an_indirect_import(self):
        # Positive control: the runner imports the gate, which imports DuckDB.
        loaded = loaded_by("tenx.engineer")
        self.assertIn("duckdb", loaded)
        self.assertIn("tenx.gate", loaded)


if __name__ == "__main__":
    unittest.main()
