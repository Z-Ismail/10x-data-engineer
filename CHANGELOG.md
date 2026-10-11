# Changelog

Notable changes to this project.

## Unreleased

### Changed

- `make install` now installs dbt-core and dbt-duckdb as well as DuckDB.

### Added

- Gate (`python -m tenx.gate`): builds a dbt project into an empty database
  and checks it against the raw files. Three checks so far: the build
  succeeds and produces every expected model, each model holds exactly the
  rows of its source less the declared exclusions, and each declared key is
  unique and not null.
- Demo data generator for three source systems (billing, CRM and product
  events) with planted defects and an answer key.
- Tests covering generator determinism and every planted defect.
- Source profiler (`python -m tenx.profile`): exact per-table profiles of CSV,
  JSON and Parquet files, written as compact text and as JSON.
- Project scaffold: licence, project config, plugin manifest and CI.
