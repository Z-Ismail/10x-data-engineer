# Changelog

Notable changes to this project.

## Unreleased

### Added

- A test bed for measuring a reviewer: `python tests/review_bed.py --out
  <folder>` writes twelve variants of the reference dbt project under
  neutral names (`project-01` to `project-12`, in an order shuffled by
  `--seed`) and, beside them, `answers.json` saying which is which. Six
  carry one planted mistake in a column the gate does not check (invoice
  times read as milliseconds, CRM dates read month first, two event
  columns swapped, a constant currency, a contact name taken from the
  email, event times cut to the day); six are correct but written
  differently. The tests check that the gate passes all twelve, that a
  mistake changes only its own columns, and that the correct ones equal
  the reference.
- Spec review in the graph run: with `--architect --review-spec`, the run
  waits after the Architect writes its spec (exit status 4) and carries on
  only when a person decides. `python -m tenx.run approve --out <run>`
  gives the engineer that spec; `approve --contract <file>` gives it a
  contract of the person's own, checked against the profiles and counted
  in the sandbox as the Architect's is; `reject --out <run> --reason
  <text>` stops the run before the engineer starts. An approval is refused
  if `SPEC.md` was changed after the run wrote it, and the run waits
  again. Every decision is kept with the run, and `status` says what a
  waiting run is waiting on.
- An Architect comparison: `python -m tenx.compare architect` (and `make
  compare-architect`) runs the graph with and without the Architect, both
  with the cross-source agent, on the same datasets in shuffled blocks, and
  judges them as the arm comparison does, the run without the Architect as
  the baseline. Each Architect run's spec is scored against the gate's
  declaration (keys, sources, rows to keep, money totals) and shown beside
  its verdict. It resumes and is refused above US$5 without
  `--approve-cost`; the 64 runs of `make compare-architect` are estimated
  at US$20.48.
- The Architect's step keeps what each model keeps of its source as
  `counts.json` beside `contract.json`.
- The Architect agent, in the graph run when asked (`python -m tenx.run run
  --architect`, `make engineer-architect`). After the cross-source agent it
  writes a spec for each model the goal names: what one row is, its key,
  its source, the rows to leave out and why, where each column comes from
  and which money must add up. The answer is checked against the source
  profiles, each rule for leaving rows out is counted on the raw files, and
  the result goes to the engineer as `SPEC.md`; the run notes it if the
  engineer changes it. A spec that fails its check or its count is not
  used, and the run says why. Its spend is capped as a cross-source call.
- A spec's rules are counted in a separate process in the sandbox, with
  DuckDB limited to the raw folder, one `SELECT` per count and a time limit,
  since an agent wrote them.
- A brief goal for the demo data, `demo/brief.md`, and `make engineer-brief`:
  the same models, keys and money column as `demo/goal.md`, without the
  rows to leave out, the grain or how to match billing to the CRM. It is the
  test bed for writing a spec before the build.
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
- Capability interfaces (`tenx/ports.py`) for what a run needs from a
  platform: profiling sources, scaffolding and building a project,
  validating it, and running an agent. DuckDB and dbt (`tenx/adapters/`)
  and Claude Code (`tenx/backends/`) are the first implementations and
  behave exactly as the code they wrap. An implementation asked for
  something it cannot do refuses with `UnsupportedCapability`, and its
  platform's errors are raised as `PlatformError`. A test keeps the
  interfaces, and the workflow code that will use them, free of DuckDB, dbt
  and gate imports.
- Spending policy (`tenx/policy.py`): a ceiling per run and a cap per agent
  call, and a budget that reserves each call's whole cap before the call
  starts, stops with `stopped: budget` when the reservation would pass the
  ceiling, and settles each call with its reported cost (or its whole
  reservation when it reported none). Amounts are decimals, so sums are
  exact. The workflow being built will use it for every agent call.
- `python -m tenx.compare run` prices the runs it still has to do before
  starting, and refuses a comparison estimated above US$5 unless
  `--approve-cost` is given (`make compare COMPARE_FLAGS=--approve-cost`).
- Run records (`tenx/domain.py`) and the workflow's state (`tenx/graph/`):
  the request, plan, artefacts, agent findings, validation verdicts, cost
  records and spending policy a run carries between steps, kept as JSON and
  read back strictly; rules for joining writes from steps that run at the
  same time; and checkpoints in SQLite that are read back without
  constructing objects.
- A graph run, `python -m tenx.run run | resume | status`
  (`make engineer-graph`): the same steps as `tenx.engineer` (profile,
  overlap, the cross-source agent, the engineer, the gate) as a LangGraph
  graph saved to `<out>/checkpoints.sqlite` after every step. A run stopped
  part way resumes at the step it was in without running finished steps
  again; an agent call that completed from the same inputs is used rather
  than made again, and a call cut off is charged its whole reservation.
  Every agent call reserves its cap first, and the run stops with
  `stopped: budget` before a call that would pass the ceiling (US$2.00, or
  `--policy`). A platform that cannot do a step stops the run before
  anything runs. Agent calls and gate runs each work in an attempt folder of
  their own. `make engineer` still uses the earlier runner.
- An equivalence check, `make equivalence`: the graph run and the earlier
  runner on the same inputs, with a stand-in for Claude Code that answers
  the same way every time, must leave byte-identical projects, notes, agent
  reports, usage and gate reports (`tenx/equivalence.py` maps one folder
  layout onto the other; any file outside the map is a difference). It runs
  on the eight comparison datasets in CI, and on six behaviours of the
  stand-in, a resumed graph run among them, in the tests.
- A paired comparison of the graph run with the earlier runner,
  `python -m tenx.compare paired` and `make parity`: both run with the
  cross-source agent on the same datasets and trials, the two runs of a pair
  next to each other. The difference in pass rates gets Tango's 95% score
  interval for paired proportions against a 10-point margin fixed in
  advance, with three verdicts (non-inferior, inferior, inconclusive), and
  the graph's cost per pass is compared with a bootstrap interval against a
  10% limit (`tenx/parity.py`). It resumes, and is priced and refused above
  US$5 without `--approve-cost`; the 64 pairs of `make parity` are estimated
  at US$35.84.
- Repairs in the graph run, off by default: `python -m tenx.run run
  --repairs N` (0 to 3) gives the engineer up to N more attempts when the
  gate fails its project. Each repair starts from the project just checked,
  in a new attempt folder, with `GATE_REPORT.md` saying which checks failed,
  on which model and by how much. The rows of an `expected` check are not
  shown, only their counts, because they come from the gate's hidden
  reference query. Repairs stop when one fails no fewer checks than the
  attempt before it, when the run ceiling cannot hold another, or at N.
  Every round is charged and has its own row in `usage.json`.
- A repair comparison: `python -m tenx.compare repairs` (and `make
  repairs`) runs the graph with repairs and pairs each run with its own
  first round, which is a run without repairs. Repairs are better when
  Tango's 95% interval on the difference in pass rates lies wholly above 0;
  the cost per pass with and without repairs is reported. It resumes, and
  is priced on every call a run may make and refused above US$5 without
  `--approve-cost`; the 32 runs of `make repairs` are estimated at US$19.20.

### Fixed

- A built wheel now contains `tenx.adapters` and `tenx.backends`; only
  editable installs had them.
- Agents in the sandbox can run shell commands when the runner is started
  from Claude Code's own shell. Claude Code moved each command into the
  memory cgroup it was already in, which the sandbox's read-only `/sys`
  does not allow, so every command failed; the sandbox now turns that
  cgroup off with `CLAUDE_CODE_TOOL_MEMORY_LIMIT=none`.
- The runner no longer waits forever when a command started by an agent
  outlives it, and stops every process an agent started when the agent
  times out or ends.

### Changed

- A spec's row counts come from `tenx.counting.count_kept` (through the
  source profiler's "count the rows a spec keeps") instead of
  `tenx.spec.count_rows`, which ran the rules in the caller's own process;
  `tenx.spec` now only writes `SPEC.md`.
- CI runs the tests one module per process, four at a time
  (`make test-parallel`, `tests/parallel.py`): about 5 minutes instead of 13
  to 19. A module passes only if it runs exactly the tests counted for it,
  and one that hangs is stopped after 15 minutes and named. `make test` is
  unchanged.
- `make install` also installs the new `graph` extra (LangGraph, its SQLite
  checkpointer and Pydantic), pinned to one LangGraph minor version. The
  core package still depends only on DuckDB.
- Each agent call's spending limit comes from the spending policy: US$0.75
  for the engineer, US$0.40 for the cross-source agent and US$0.10 for each
  scout, in place of US$5 for every agent. Measured runs cost well under
  these.
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
