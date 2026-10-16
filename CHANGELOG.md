# Changelog

Notable changes to this project.

## Unreleased

### Changed

- Gate: the report's check column is as wide as the longest check name.
- Gate: the `total` check compares the values of the `by` column in capitals
  with surrounding spaces removed. A correct model that writes currency codes
  in capitals (`USD`) where the raw file has lower case (`usd`) used to fail.
- Gate: the `total` check reports how many raw amounts were blank.
- `make install` now installs dbt-core and dbt-duckdb as well as DuckDB.

### Added

- Usage ledger: every engineer run prints its tokens per model, turns,
  refused tool calls, time and Claude Code's cost estimate by role, with the
  gate verdict, and saves them as `usage.json` beside the run.
- The engineer agent (`python -m tenx.engineer`, `make engineer`): one agent
  that reads a goal in plain English and the source profiles, builds the
  staging models in a fresh dbt project, and is then checked by the gate.
  The agent may only write files in its project folder and run `dbt`, and
  never sees the gate's declaration.
- A goal for the demo data, `demo/goal.md`.
- Gate: a fifth check, `repeat`. The project is built a second time into
  another empty database, and every model must hold the same rows as in the
  first build. The report names the columns whose values changed.
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
