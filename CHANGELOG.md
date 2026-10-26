# Changelog

Notable changes to this project.

## Unreleased

### Added

- Every agent runs in a bubblewrap sandbox on Linux. It sees its own folder,
  the add-on, the programs it runs, Claude Code's settings and, for the
  engineer, the raw files; the gate's expectations, the repository, the run
  folder and the home folder are hidden, and writes outside the agent's
  folder are discarded. Where no sandbox can be set up the run is refused;
  `--unisolated` runs without one, and the report and `usage.json` say so.
- The gate builds each project in the same sandbox, so a dbt Python model
  cannot read the gate's declaration or expected rows, or change its
  databases. Each build sees the project and the raw files read-only and can
  write only its own work folder; the second build cannot see the first
  one's database. `python -m tenx.gate` builds in the sandbox by default and
  refuses where none can be set up; `--unisolated` builds without one and
  says so.
- Run artefacts are written atomically: to a temporary file beside the
  destination, synced, then renamed into place, so a run stopped part way
  never leaves a half-written report, profile, note or ledger. Also new, for
  resuming runs later: input hashes, attempt folders with manifests, and the
  rule for when an earlier attempt can be reused.

### Fixed

- The runner no longer waits forever when a command started by an agent
  outlives it, and stops every process an agent started when the agent
  times out or ends.

### Changed

- README: rewritten around the product's aim, design and principles, with
  a roadmap table showing what is available today. The detailed description
  of each component, with example output, moved to `docs/components.md`,
  and the full security description to `docs/security.md`.
- README: the opening and a new Scope section describe the wider aim
  (other platforms, ingestion, orchestration, diagnosis) and separate what
  exists today from what is planned.
- README: the design now describes six roles in a coordinated workflow, the
  limits per run, the platform plan, and a Security section on the engineer
  agent's file access through dbt.
- CI runs `ruff check`, `ruff format --check` and `pip-audit` alongside the
  tests. `make lint` runs the same checks locally.
- `python -m tenx.engineer` and `make engineer` run the cross-source agent
  before the engineer unless `--no-cross-source` is given, since it raised
  the engineer's pass rate in the comparison. `make engineer-alone` runs the
  engineer without it. With the change, `make engineer` costs about US$0.06
  more per run by Claude Code's estimate, and writes `cross_source/` and
  `cross_source.json` beside the project.
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

- `tenx.contract` and `tenx.spec`: a spec's contract lists, for each model,
  its grain, key, source table, the rows to leave out with the reason and
  the rule, where each column comes from, and the money that must add up to
  the raw data. The contract is checked against the source profiles (every
  table and column it names must exist; a staging model takes columns only
  from its own table; at most three open questions), each rule for leaving
  rows out is run on the raw files and its rows counted, and the contract is
  written out as `SPEC.md` and turned into a gate declaration. Nothing
  writes a contract yet; it is the first part of the Architect role.
- Comparison (`python -m tenx.compare`, `make compare`): runs the engineer
  alone, with scouts, with the cross-source agent and with both, the same
  number of times on every dataset, in shuffled blocks, and reports each
  arm's pass rate with a 95% interval, the chance that it beats the engineer
  alone, and the estimated cost per run and per passing run. A comparison
  that stops part way resumes. On the demo data the cross-source agent
  raised the pass rate from 17 of 32 runs to 31 of 32, and the scouts
  lowered it to 8 of 32.
- Scouts (`python -m tenx.engineer --scouts`, `make engineer-scouts`):
  before the engineer starts, a scout for each raw table, four at a time,
  reads the goal, the table's profile and up to 20 of its rows, and answers
  in a fixed JSON shape what the engineer must handle in that table. Each
  answer is checked, every column it names must be a column of its table,
  and the answers are given to the engineer together as `TABLE_NOTES.md`.
  Each scout's usage has its own row in the ledger.
- `tenx.sample`: picks up to 20 rows of a raw file, as written, the same
  rows every time for the same file.
- Cross-source agent (on by default; `--no-cross-source` and
  `make engineer-alone` turn it off): before the engineer starts, an agent that
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
