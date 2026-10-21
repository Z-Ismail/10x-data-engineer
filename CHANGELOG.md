# Changelog

Notable changes to this project.

## Unreleased

### Changed

- The engineer agent reads `TABLE_NOTES.md` before writing the staging
  models, when a run provides it.
- The engineer agent reads `JOINS.md` before writing a model that combines
  sources, when a run provides it.
- Demo data: some CRM accounts of paying companies hold another mailbox at
  the company's domain (`accounts@company0030.example` where billing has
  `billing@company0030.example`), so matching billing customers to CRM
  accounts needs the email domain. Every other value is unchanged for a
  given seed and size. The answer key, the gate's demo declaration and the
  reference crosswalk now match companies on the email domain.
- The demo goal asks for a crosswalk, `int_customers_matched_to_accounts`,
  that matches each live billing customer to its company's CRM account, as
  well as the staging models. The gate's demo declaration checks it.
- The engineer agent builds models that combine sources in
  `models/intermediate/`, from the staging models, and builds every model as
  a table or a view.
- The engineer agent is asked to run one `dbt` command per shell call, since
  a command piped into another program is refused.
- Gate: the report's check column is as wide as the longest check name.
- Gate: the `total` check compares the values of the `by` column in capitals
  with surrounding spaces removed. A correct model that writes currency codes
  in capitals (`USD`) where the raw file has lower case (`usd`) used to fail.
- Gate: the `total` check reports how many raw amounts were blank.
- `make install` now installs dbt-core and dbt-duckdb as well as DuckDB.

### Added

- Scouts (`python -m tenx.engineer --scouts`, `make engineer-scouts`):
  before the engineer starts, a scout for each raw table, four at a time,
  reads the goal, the table's profile and up to 20 of its rows, and answers
  in a fixed JSON shape what the engineer must handle in that table. Each
  answer is checked, every column it names must be a column of its table,
  and the answers are given to the engineer together as `TABLE_NOTES.md`.
  Each scout's usage has its own row in the ledger.
- `tenx.sample`: picks up to 20 rows of a raw file, as written, the same
  rows every time for the same file.
- Cross-source agent (`python -m tenx.engineer --cross-source`,
  `make engineer-cross-source`): before the engineer starts, an agent that
  may only read files works out from the goal, the profiles and the overlap
  report how the tables join, and answers in a fixed JSON shape. The answer
  is checked, every column it names must exist, and it is given to the
  engineer as `JOINS.md`. Its usage has its own row in the ledger.
- Gate: a sixth check, `expected`. A model can declare the rows it must hold
  in named columns, as a query on the raw files, and the gate compares the
  model with them, showing rows the model should not hold and expected rows
  it lacks.
- Overlap report (`python -m tenx.overlap`, `make overlap`): every pair of
  columns in different tables is compared on its values, in lower case with
  surrounding spaces removed, and the share found each way, the rows matched
  with and without that normalisation, and examples not found are written
  to `overlap.txt` and `overlap.json`.
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
