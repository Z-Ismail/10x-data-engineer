# 10x Data Engineer

A multi-agent data engineering crew for dbt and DuckDB.

It is installed as an add-on to [Claude Code](https://code.claude.com/docs),
Anthropic's coding assistant, and adds a team of specialised agents and the
commands to run them.

## Project status

This project is under active development and is not yet ready for general
use.

| Component | Status |
| --- | --- |
| Demo data generator | Available |
| Source profiler | Available |
| Gate: build, row count, key, money total and repeat build checks | Available |
| Engineer agent: one agent that builds staging models from a goal | Available |
| The rest of the crew | Planned |

## Why

Starting a pipeline on unfamiliar data means the same steps every time: find
out what is in the source tables, decide what one row of each model means,
write the staging layer, build the models, then check the numbers against the
source.

Coding agents can do each of those steps. Left alone, they also produce dbt
projects that build cleanly and are wrong. Typical failures are a join that
duplicates rows, a load that silently drops records, and a test relaxed until
it passes.

This project splits the work across a small crew of agents with separate
jobs and puts a coded gate between them and the result. The gate ignores a
green build and checks the output against the raw data.

## What works today

```
make install     # installs the package, DuckDB and dbt
make demo-data   # writes three source systems to demo/data
make profile     # writes one profile per source table to profiles/
make test
make engineer    # has the engineer agent build the demo project, then gates it
```

### Demo data

`demo/generate.py` writes a billing system, a CRM and a product event log for
the same set of companies. The three only agree on the customer's email
address and each stores it differently. Ten kinds of defect are planted at
known rates (duplicate customers, test-mode rows, yen amounts, refunds, junk
user IDs and others), and an answer key is written to a separate folder.
Output is deterministic for a given seed, and every defect is present
whatever the seed. Details are in
[demo/README.md](demo/README.md).

### Source profiler

`python -m tenx.profile <folder> --out <folder>` reads every CSV, JSON and
Parquet file under a folder and writes two files per table: a short text
profile and the same facts as JSON. The text profile is what an agent reads
instead of the data. For the demo data it is under 5% of the size of the
files it describes.

Every figure is exact and computed in DuckDB. Nothing is sampled. A profile
gives the row count, checked against a separate count of the records in the
file, and for each column the nulls, blanks, distinct values, most common
values and the type it safely converts to. It also reports:

- key candidates: columns with no nulls and no repeated values
- exact duplicate rows
- identifiers that would lose leading zeros if read as numbers
- dates that are not ISO formatted, and whether the day and month are
  ambiguous
- integers that look like Unix timestamps
- values that differ only by case or surrounding spaces
- placeholder values such as `null`, `anonymous` and `-1`

CSV files are read as text, so nothing is changed by type inference before
it is counted. A column that is unique in a small file can be unique by
coincidence, so key candidates are a starting point and not a decision.

```
table: crm.accounts
file: crm/accounts.csv (csv)
rows: 180
records in file: 180
key candidates: account_id, company_name, billing_email

columns:
  account_id     text  nulls 0  distinct 180  unique  length 18 to 18
  ...
  created_date   date  nulls 0  distinct 102  top: 04/04/2026 (5), 19/04/2026 (5), 10/04/2026 (4)

notes:
  - account_id: every value is digits, 180 with leading zeros; kept as text so the zeros are not lost
  - billing_email: 13 values with leading or trailing spaces
  - created_date: dates are written day/month/year
```

### Gate

`python -m tenx.gate <project> --raw <folder> --checks <file>` builds a dbt
project into an empty DuckDB database and checks the result against the raw
files. A successful build is only the first check. There are five checks:

| Check | Fails when |
| --- | --- |
| `build` | `dbt build` does not succeed, or an expected model is missing from the build |
| `rows` | a model does not hold exactly the rows of its source, less the rows declared as excluded |
| `key` | a column of a model's declared key is missing, or the key is null or repeated in any row |
| `total` | a declared money column does not add up to exactly the raw total less the declared exclusions, overall or for each value of a column such as the currency |
| `repeat` | a second build of the project into another empty database does not give the same rows in every model |

What to expect is declared in a JSON file that is kept outside the dbt
project, so the checks cannot be edited by whoever writes the models:

```json
{
  "models": [
    {
      "name": "stg_billing__invoices",
      "key": ["invoice_id"],
      "source": "billing.invoices",
      "exclusions": [
        {"name": "test_mode", "where": "livemode = 'false'"}
      ],
      "totals": [
        {"column": "amount_minor", "by": "currency"}
      ]
    },
    {
      "name": "stg_events__events",
      "key": ["event_id"],
      "source": "events.events",
      "exclusions": [
        {"name": "repeat_deliveries", "duplicate_rows": true}
      ]
    }
  ]
}
```

Rows may only be left out on purpose. An exclusion is either a SQL condition
on the raw file or the removal of exact repeats of a row, and the report
counts what each one removed. A model that filters out anything else comes up
short and fails, and so does a model that keeps rows it was told to exclude.

```
gate: FAIL (17 passed, 2 failed, 0 not run)

PASS  build   project                 dbt build exited 0 after running 12 nodes
PASS  build   stg_billing__invoices   built as "build"."main"."stg_billing__invoices"
...
FAIL  rows    stg_billing__invoices   496 rows in billing.invoices, 20 excluded (test_mode 20), 476 expected, 458 in the model: 18 fewer than expected
PASS  key     stg_billing__invoices   invoice_id: unique and not null over 458 rows
FAIL  total   stg_billing__invoices   amount_minor by currency: 4 of 4 currency values do not tie with the raw data
      EUR: raw 987100, expected 987100, model 917500, 69600 short
      GBP: raw 1351900, expected 1351900, model 1329200, 22700 short
      JPY: raw 2537400, expected 2537400, model 2507700, 29700 short
      USD: raw 24152800, test_mode 19998000, expected 4154800, model 4070700, 84100 short
...
PASS  repeat  stg_billing__invoices   the same 458 rows in both builds
...
```

That report is from a model that quietly drops void invoices.

The gate runs dbt as a separate process with a connection profile of its own,
so the project does not choose the database it is built into, and nothing is
written to the project folder. The project reads the raw files from the folder
it is given in the dbt variable `raw_dir`. Exit status is 0 when every check
passes, 1 when any check fails and 2 when the gate could not run. Pass
`--workdir` to keep the built databases and dbt's artifacts and logs for
both builds; the second build is in the `repeat` folder.

A total is compared exactly, with no tolerance, separately for each value of
the `by` column. The gate adds up the raw column itself and reports what each
exclusion removed, so the figures can be followed by hand:

```
FAIL  total   stg_billing__invoices   amount_minor by currency: 1 of 4 currency values do not tie with the raw data
      JPY: raw 2537400, expected 2537400, model 253740000, 251202600 over
```

That report is from a model that treats yen as if it had cents.

The row check compares counts, so on its own it would not notice one row
swapped for another; the money no longer ties, and the total check fails.
Rules for totals:

- The model column must be in the same unit as the raw column. The gate does
  not convert between units, because a wrong conversion is one of the things
  it is there to catch.
- The model column must be an integer or DECIMAL. Money held as floating
  point fails, because it cannot be added up exactly.
- Raw amounts must be plain decimal numbers such as `-1250` or `12.50`. They
  are added at full precision, so nothing is rounded. A raw value that is not
  a number, such as `n/a` or `12,50`, fails the check unless an exclusion
  removes its row; the report gives how many rows and up to three of the
  values.
- A blank raw amount adds nothing, as it would in SQL's `sum`. The report
  counts blank amounts so that missing money is visible.
- Values of the `by` column are compared in capitals with surrounding spaces
  removed, so `usd` in the raw file and `USD` in the model are the same
  currency, and the report shows `USD`. Nothing else is changed: `US$` is
  not `USD`.
- If `source_column` or `source_by` is given, the raw file's column of that
  name is used; otherwise the raw column has the same name as the model's.

The `repeat` check builds the project a second time, from nothing, into a
separate database, and compares every model the first build produced, not
only the declared ones. A model that stamps the load time into its rows or
generates random keys gives different results on each build, and the
report names the columns whose values changed:

```
FAIL  repeat  stg_billing__invoices   the builds differ: 476 rows in the first and 476 in the second, of which 476 only in the first and 476 only in the second; values differ in loaded_at
```

Rules for the repeat build:

- Rows are compared as a whole, in any order, but every copy counts: a build
  that repeats a row does not match one that holds it once.
- Both builds must have the same columns, with the same types, in the same
  order.
- dbt runs with one thread for both builds, so differences come from the
  models and not from the order work was scheduled in.
- A view is stored as its query, so its two builds run the same query over
  the same files. The check still catches a view whose query gives a
  different answer each time it runs.
- The second build roughly doubles the time the gate takes.

### Engineer agent

`python -m tenx.engineer --raw <folder> --goal <file> --checks <file> --out <folder>`
has one agent build a dbt project from raw files and then runs the gate on
it. The run passes only if the gate passes.

The goal is a short Markdown file in plain English. It names each model,
what one row of it means, its key and which source rows to leave out.
[demo/goal.md](demo/goal.md) is the goal for the demo data.

The runner prepares a folder for the agent with the goal, a profile of every
raw table and a dbt project with no models, set to read the raw files from
the `raw_dir` variable. The agent reads the goal and the profiles, writes the
sources, the staging models and their key tests, and runs `dbt build`. It is
told to count rows with `dbt show` until every model reconciles with its
source before it stops, but only the gate's verdict counts. The gate's
declaration is never put in the agent's folder, so the agent cannot read the
checks or change them.

The agent runs in Claude Code without anyone at the keyboard. It may write
files in its own folder and run `dbt`, and every other action is refused
rather than asked about. It is pinned to the Sonnet model and stops after 60
turns, US$5 of estimated usage or 30 minutes, whichever comes first.

```
agent: success after 15 turns (exit 0)
project: runs/demo/project

gate: PASS (19 passed, 0 failed, 0 not run)

PASS  build   project                 dbt build exited 0 after running 12 nodes
...
PASS  repeat  stg_events__events      the same 1183 rows in both builds
```

Everything from the run is kept under the `--out` folder:

| Path | Contents |
| --- | --- |
| `project/` | The dbt project the agent wrote |
| `agent.json` | What Claude Code reported when the agent stopped |
| `gate/` | The gate's databases, dbt artifacts and logs |
| `plugin/` | The copy of the add-on the agent was loaded from |

Today the agent builds staging models only: one model per source table, with
renames, type conversions and the exclusions the goal asks for, and no joins.
It runs once; if the gate fails, nothing is retried. Running it needs a clone
of this repository and Claude Code installed and signed in.

## Design

This section describes the target design. The status table above shows which
parts exist today.

Input is a goal in plain English and a folder of raw files (CSV, JSON or
Parquet). Output is a dbt project, delivered as pull requests, plus a report
showing how the result ties back to the sources.

| Role | Responsibility |
| --- | --- |
| Lead | Breaks the goal into tasks and decides how many roles the job needs |
| Scouts | Profile the source tables, one table each, in parallel |
| Architect | Writes the spec, including the grain of every model |
| QA | Writes acceptance checks from the spec before anything is built |
| Staging engineers | One per source system: renames, casts, no joins |
| Marts engineer | Matches records across systems and builds the final models |
| Reviewer | Reviews each pull request and has to back findings with a query |
| Analyst | Answers the original question from the finished tables |

A job with one source and a few tables does not use all eight. The Lead runs
a single engineer and the same gate.

The gate fails a run unless:

- the project builds twice from an empty database with identical results
- every model has a declared grain and a passing key test
- no join changes the row count where it should not
- money totals in the final tables match the raw sources

Acceptance checks are stored outside the engineers' write paths, so a check
cannot be edited to make a run pass.

Everything runs locally on DuckDB. The agents run under your own Claude
subscription, so there is no separate API key and no hosted service.

## Requirements

Python 3.10 or later. The demo generator uses the standard library only. The
profiler needs DuckDB 1.1 or later. The gate also needs dbt-core 1.12 and
dbt-duckdb 1.11, which are the `pipeline` dependency group in
`pyproject.toml`. `make install` installs all of them.

The engineer agent also needs [Claude Code](https://code.claude.com/docs),
installed and signed in. It runs on your own Claude subscription or API key.

## Repository layout

| Path | Contents |
| --- | --- |
| `.claude-plugin/` | Manifest that tells Claude Code what the add-on contains |
| `agents/` | The agents' instructions |
| `demo/` | Demo data generator, and the goal for the demo data |
| `tenx/` | Python package: the source profiler, the gate and the engineer agent's runner |
| `tests/` | Tests, and the dbt project the gate is tested against |

## Licence

[MIT](LICENSE)
