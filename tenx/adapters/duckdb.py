"""Raw files read with DuckDB, projects built with dbt on DuckDB, checked by the gate.

Each adapter wraps the module that already does the work and changes
nothing about what it writes or decides; it only gives that work the shape
of an interface in ``tenx.ports`` and turns the module's own errors into
``PlatformError``.
"""

from __future__ import annotations

from pathlib import Path

from tenx import gate
from tenx.engineer import dbt_project_yml, profiles_yml
from tenx.overlap import overlap_folder, write_report
from tenx.ports import (
    BUILD,
    OVERLAP,
    PROFILE,
    SCAFFOLD,
    VALIDATE,
    BuildResult,
    CheckResult,
    PlatformError,
    Sandbox,
    ValidationReport,
)
from tenx.profile import ProfileError, profile_folder


class DuckDBProfiler:
    """Profiles CSV, JSON and Parquet files in a folder, and the values their columns share."""

    name = "DuckDB"

    def capabilities(self) -> frozenset[str]:
        return frozenset({PROFILE, OVERLAP})

    def profile(self, raw: Path, out: Path) -> list[dict]:
        try:
            return profile_folder(Path(raw), Path(out))
        except ProfileError as error:
            raise PlatformError(str(error)) from error

    def overlap(self, raw: Path, out: Path) -> dict:
        try:
            report = overlap_folder(Path(raw), exclude=Path(out))
            write_report(report, Path(out))
        except ProfileError as error:
            raise PlatformError(str(error)) from error
        return report


class DbtDuckDB:
    """dbt projects built into DuckDB files."""

    name = "dbt on DuckDB"

    def capabilities(self) -> frozenset[str]:
        return frozenset({SCAFFOLD, BUILD})

    def scaffold(self, project: Path, raw: Path) -> None:
        project = Path(project)
        if (project / "dbt_project.yml").exists() or (project / "models").exists():
            raise PlatformError(f"{project} already holds a dbt project")
        project.mkdir(parents=True, exist_ok=True)
        (project / "dbt_project.yml").write_text(
            dbt_project_yml(Path(raw).resolve()), encoding="utf-8"
        )
        (project / "profiles.yml").write_text(profiles_yml(), encoding="utf-8")
        (project / "models").mkdir()

    def build(
        self,
        project: Path,
        raw: Path,
        workdir: Path,
        sandbox: Sandbox | None = None,
        hidden: tuple[Path, ...] = (),
    ) -> BuildResult:
        try:
            exit_code, results, output = gate.build_project(
                Path(project).resolve(),
                Path(raw).resolve(),
                Path(workdir).resolve(),
                sandbox=sandbox,
                hidden=hidden,
            )
        except (gate.GateError, FileExistsError) as error:
            raise PlatformError(str(error)) from error
        return BuildResult(exit_code, results, output)


class DuckDBGate:
    """The gate: builds the project twice from nothing and checks it against the raw files."""

    name = "DuckDB gate"

    def capabilities(self) -> frozenset[str]:
        return frozenset({VALIDATE})

    def validate(
        self,
        project: Path,
        raw: Path,
        declaration: Path,
        workdir: Path,
        sandbox: Sandbox | None = None,
        hidden: tuple[Path, ...] = (),
    ) -> ValidationReport:
        try:
            results = gate.run_gate(project, raw, declaration, workdir, sandbox, hidden)
        except gate.GateError as error:
            raise PlatformError(str(error)) from error
        return ValidationReport(
            tuple(
                CheckResult(
                    result.check, result.subject, result.status, result.detail, result.facts
                )
                for result in results
            ),
            gate.passed(results),
            gate.render(results),
        )
