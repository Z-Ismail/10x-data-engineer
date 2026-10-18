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
| Overlap report: values shared between columns of different tables | Available |
| Gate: build, row count, key, money total, expected rows and repeat build checks | Available |
| Engineer agent: one agent that builds the models a goal asks for | Available |
| Usage ledger: tokens, turns, time and cost by role for every run | Available |
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
make overlap     # writes a report of columns that share values to profiles/
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

### Overlap report

`python -m tenx.overlap <folder> --out <folder>` compares every column with
every column of the other tables and reports which pairs hold the same
values. It is the starting point for working out how tables join, within one
source system and across systems, and it writes `overlap.txt` and
`overlap.json` beside the profiles. On the demo data:

```
billing.customers.email ~ crm.accounts.billing_email: 132 shared values
  billing.customers.email     132 of 150 values found (88.0%), 138 of 157 rows match (89 exactly), many rows per value, not found: billing@company0082.example, ...
  crm.accounts.billing_email  132 of 180 values found (73.3%), 132 of 180 rows match (86 exactly), one row per value, not found: Billing@company0003.example, ...
```

That pair is the billing system and the CRM sharing the customer's email.
Each side is reported separately, because the two directions differ: most
billing addresses are in the CRM, while a quarter of CRM accounts have no
billing record (they are prospects). An exact join would match 86 CRM rows;
comparing in lower case without surrounding spaces matches 132.

Rules:

- Values are compared as text, in lower case, with surrounding spaces
  removed. Nothing else is changed.
- Nulls, blanks and placeholder values such as `null`, `anonymous` and `-1`
  are left out before comparing, and the report counts them per column.
- A pair is listed when at least half of one column's distinct values are
  found in the other. Pairs that share fewer values are counted in the text
  and kept in the JSON.
- Up to three values that were not found are shown for each side, as they
  are written in the file, so the reason for unmatched rows can be seen.
- Columns of the same table are not compared with each other, and nested
  JSON columns (lists and objects) are skipped.
- Every figure is exact. Nothing is sampled.

The report gives facts, not joins. Two columns of currency codes, or two
timestamp columns, can share values without being related, and the distinct
counts are shown so that such pairs are easy to recognise. The engineer
agent is not given this report yet.

### Gate

`python -m tenx.gate <project> --raw <folder> --checks <file>` builds a dbt
project into an empty DuckDB database and checks the result against the raw
files. A successful build is only the first check. There are six checks:

| Check | Fails when |
| --- | --- |
| `build` | `dbt build` does not succeed, or an expected model is missing from the build |
| `rows` | a model does not hold exactly the rows of its source, less the rows declared as excluded |
| `key` | a column of a model's declared key is missing, or the key is null or repeated in any row |
| `total` | a declared money column does not add up to exactly the raw total less the declared exclusions, overall or for each value of a column such as the currency |
| `expected` | a model's named columns do not hold exactly the rows that a declared query gives from the raw files |
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
gate: FAIL (22 passed, 2 failed, 0 not run)

PASS  build     project                            dbt build exited 0 after running 15 nodes
PASS  build     stg_billing__invoices              built as "build"."main"."stg_billing__invoices"
...
FAIL  rows      stg_billing__invoices              496 rows in billing.invoices, 20 excluded (test_mode 20), 476 expected, 458 in the model: 18 fewer than expected
PASS  key       stg_billing__invoices              invoice_id: unique and not null over 458 rows
FAIL  total     stg_billing__invoices              amount_minor by currency: 4 of 4 currency values do not tie with the raw data
      EUR: raw 987100, expected 987100, model 917500, 69600 short
      GBP: raw 1351900, expected 1351900, model 1329200, 22700 short
      JPY: raw 2537400, expected 2537400, model 2507700, 29700 short
      USD: raw 24152800, test_mode 19998000, expected 4154800, model 4070700, 84100 short
...
PASS  repeat    stg_billing__invoices              the same 458 rows in both builds
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
FAIL  total     stg_billing__invoices              amount_minor by currency: 1 of 4 currency values do not tie with the raw data
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
FAIL  repeat    stg_billing__invoices              the builds differ: 476 rows in the first and 476 in the second, of which 476 only in the first and 476 only in the second; values differ in loaded_at
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

Some models are right or wrong in a way no count or total shows. A model
that matches billing customers to CRM accounts on email can have one row per
customer, a unique key and nothing to add up, and still miss every match
where the two systems wrote the email differently. For such a model the
declaration gives the right answer as a query on the raw files, and the
`expected` check compares the model with it:

```json
{
  "name": "int_customers_matched_to_accounts",
  "key": ["customer_id"],
  "source": "billing.customers",
  "exclusions": [{"name": "test_mode", "where": "livemode = 'false'"}],
  "expected": {
    "columns": ["customer_id", "account_id"],
    "query": "SELECT customers.id AS customer_id, accounts.account_id FROM billing.customers AS customers LEFT JOIN crm.accounts AS accounts ON lower(trim(customers.email)) = lower(trim(accounts.billing_email)) WHERE customers.livemode = 'true'"
  }
}
```

```
FAIL  expected  int_customers_matched_to_accounts  customer_id, account_id: 147 rows expected and 147 in the model, of which 49 not expected and 49 expected but missing
      not expected: (cus_000008A, null), (cus_000021A, null), (cus_000023A, null)
      missing: (cus_000008A, 001000000000000008), (cus_000021A, 001000000000000021), (cus_000023A, 001000000000000023)
```

That report is from a model that matches the emails exactly: 49 customers
are left without the account they belong to.

Rules for expected rows:

- The query reads each raw file by its table name, such as
  `billing.customers`, with CSV columns as text. Only files at the top of
  the raw folder or one folder down can be named.
- The query runs before the project is built, and its rows are kept in the
  gate's own working folder, never in the project folder.
- Only the named columns are compared; the model may hold others. Values
  are compared as text, so the model's choice of types does not matter, but
  an identifier that lost its leading zeros does not match.
- Rows are compared in any order, every copy counts and nulls are equal.
- Up to three rows are shown each way: rows the model should not hold, and
  expected rows it lacks.

### Engineer agent

`python -m tenx.engineer --raw <folder> --goal <file> --checks <file> --out <folder>`
has one agent build a dbt project from raw files and then runs the gate on
it. The run passes only if the gate passes.

The goal is a short Markdown file in plain English. It names each model,
what one row of it means, its key and which source rows to leave out.
[demo/goal.md](demo/goal.md) is the goal for the demo data: a staging model
for each source table, and a crosswalk that matches each billing customer to
its company's CRM account, for which the goal says only that the two systems
share the customer's email address.

The runner prepares a folder for the agent with the goal, a profile of every
raw table and a dbt project with no models, set to read the raw files from
the `raw_dir` variable. The agent reads the goal and the profiles, writes the
sources, the staging models, any model that combines sources (under
`models/intermediate/`) and their key tests, and runs `dbt build`. It is
told to count rows with `dbt show` until every model reconciles with its
source before it stops, but only the gate's verdict counts. The gate's
declaration is never put in the agent's folder, so the agent cannot read the
checks or change them.

The agent runs in Claude Code without anyone at the keyboard. It may write
files in its own folder and run `dbt`, and every other action is refused
rather than asked about. It is pinned to the Sonnet model and stops after 60
turns, US$5 of estimated usage or 30 minutes, whichever comes first.

```
agent: success after 19 turns (exit 0)
project: runs/demo/project

gate: PASS (24 passed, 0 failed, 0 not run)

PASS  build     project                            dbt build exited 0 after running 15 nodes
...
PASS  expected  int_customers_matched_to_accounts  customer_id, account_id: the 147 expected rows, and no other
...
PASS  repeat    int_customers_matched_to_accounts  the same 147 rows in both builds

usage by role (cost is Claude Code's own estimate, not a bill)

role      outcome    turns  refused    time  cost (US$)
engineer  success       19        0  43.9 s      0.1170
run       gate PASS     19        0  43.9 s      0.1170

role      model              input  cache write  cache read  output
engineer  claude-haiku-5-5   1,177            0           0      18
engineer  claude-sonnet-5-5     16       16,897      75,772   4,165
```

Everything from the run is kept under the `--out` folder:

| Path | Contents |
| --- | --- |
| `project/` | The dbt project the agent wrote |
| `agent.json` | What Claude Code reported when the agent stopped |
| `usage.json` | The usage ledger: each role's figures and the gate verdict |
| `gate/` | The gate's databases, dbt artifacts and logs |
| `plugin/` | The copy of the add-on the agent was loaded from |

Today the agent builds what the demo goal asks for: one staging model per
source table, with renames, type conversions and the exclusions the goal
asks for, and models that combine sources from the staging models, such as
the crosswalk. It has not been tried on other goals. It runs once; if the
gate fails, nothing is retried. Running it needs a clone
of this repository and Claude Code installed and signed in.

### Usage ledger

Every run ends with its usage by role, printed and saved as `usage.json`.
Each role runs as its own Claude Code session, and the figures are read from
the report Claude Code prints when that session ends. Today the engineer is
the only role.

| Column | Meaning |
| --- | --- |
| `outcome` | How the session ended, as Claude Code reports it: `success`, or an error such as `error_max_turns` or `error_max_budget_usd`. `timed out` or `no report` when the session printed nothing. The `run` row shows the gate verdict instead |
| `turns` | Turns the agent took |
| `refused` | Tool calls refused because the agent is not allowed to make them |
| `time` | How long the session ran; the gate's own time is not included |
| `cost (US$)` | Claude Code's own estimate of the session's cost. It is not a bill, and on a subscription it is not what you are charged. Shown as `unknown` when Claude Code does not know a model's price |
| `input`, `cache write`, `cache read`, `output` | Tokens per model, as Claude Code reports them |

The tokens are listed per model because a session can use more than one: in
the example above, Claude Code made a small request of its own on a second
model. A figure that was not reported is shown as `-`, and the `run` row is
only added up when every role reported.

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
| `tenx/` | Python package: the source profiler, the overlap report, the gate and the engineer agent's runner |
| `tests/` | Tests, and the dbt project the gate is tested against |

## Licence

[MIT](LICENSE)
