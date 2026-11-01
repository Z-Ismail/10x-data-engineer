"""The DuckDB adapters: the interfaces they satisfy, what they declare, how they refuse."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from tenx.adapters.duckdb import DbtDuckDB, DuckDBGate, DuckDBProfiler
from tenx.ports import (
    BUILD,
    OVERLAP,
    PROFILE,
    SCAFFOLD,
    VALIDATE,
    PlatformError,
    SourceProfiler,
    TransformEngine,
    Validator,
)


class InterfaceTest(unittest.TestCase):
    def test_each_adapter_satisfies_its_interface_and_declares_its_capabilities(self):
        cases = [
            (DuckDBProfiler(), SourceProfiler, {PROFILE, OVERLAP}),
            (DbtDuckDB(), TransformEngine, {SCAFFOLD, BUILD}),
            (DuckDBGate(), Validator, {VALIDATE}),
        ]
        for adapter, interface, capabilities in cases:
            with self.subTest(adapter=adapter.name):
                self.assertIsInstance(adapter, interface)
                self.assertEqual(adapter.capabilities(), capabilities)


class RefusalTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="tenx-adapters-"))
        self.addCleanup(shutil.rmtree, self.folder)
        self.raw = self.folder / "raw"
        self.raw.mkdir()

    def test_profiling_a_folder_with_no_tables_is_refused(self):
        with self.assertRaisesRegex(PlatformError, "no CSV, JSON or Parquet files"):
            DuckDBProfiler().profile(self.raw, self.folder / "out")

    def test_overlap_of_a_single_table_is_refused(self):
        (self.raw / "one.csv").write_text("id\n1\n", encoding="utf-8")
        with self.assertRaisesRegex(PlatformError, "at least two tables"):
            DuckDBProfiler().overlap(self.raw, self.folder / "out")

    def test_overlap_report_named_like_a_table_is_refused(self):
        (self.raw / "overlap.csv").write_text("id\n1\n", encoding="utf-8")
        (self.raw / "other.csv").write_text("id\n1\n", encoding="utf-8")
        with self.assertRaisesRegex(PlatformError, "table named overlap"):
            DuckDBProfiler().overlap(self.raw, self.folder / "out")

    def test_scaffold_writes_an_empty_project_once(self):
        project = self.folder / "project"
        DbtDuckDB().scaffold(project, self.raw)
        self.assertEqual(
            sorted(path.name for path in project.iterdir()),
            ["dbt_project.yml", "models", "profiles.yml"],
        )
        self.assertEqual(list((project / "models").iterdir()), [])
        self.assertIn(
            json.dumps(str(self.raw.resolve())), (project / "dbt_project.yml").read_text()
        )
        with self.assertRaisesRegex(PlatformError, "already holds a dbt project"):
            DbtDuckDB().scaffold(project, self.raw)

    def test_validating_a_folder_that_is_not_a_project_is_refused(self):
        declaration = Path(__file__).parent / "fixtures" / "demo_gate.json"
        with self.assertRaisesRegex(PlatformError, "is not a dbt project"):
            DuckDBGate().validate(self.folder, self.raw, declaration, self.folder / "gate")

    def test_validating_with_an_unreadable_declaration_is_refused(self):
        declaration = self.folder / "checks.json"
        declaration.write_text("not json", encoding="utf-8")
        with self.assertRaises(PlatformError):
            DuckDBGate().validate(self.folder, self.raw, declaration, self.folder / "gate")

    def test_building_into_a_used_folder_is_refused(self):
        project = self.folder / "project"
        DbtDuckDB().scaffold(project, self.raw)
        workdir = self.folder / "build"
        (workdir / "profiles").mkdir(parents=True)
        with self.assertRaises(PlatformError):
            DbtDuckDB().build(project, self.raw, workdir)


if __name__ == "__main__":
    unittest.main()
