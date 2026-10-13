# Changelog

Notable changes to this project.

## Unreleased

### Changed

- Gate: the `total` check compares the values of the `by` column in capitals
  with surrounding spaces removed. A correct model that writes currency codes
  in capitals (`USD`) where the raw file has lower case (`usd`) used to fail.
- Gate: the `total` check reports how many raw amounts were blank.
- `make install` now installs dbt-core and dbt-duckdb as well as DuckDB.

### Added

- Gate: a fourth check, `total`. A declared money column must add up to
  exactly the raw total, less what the declared exclusions removed, for each
  currency. Raw amounts are added without rounding, and money held as
  floating point fails.
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
