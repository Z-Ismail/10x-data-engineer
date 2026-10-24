# 10x Data Engineer

A multi-agent data engineering team that builds data pipelines and checks
its work against the source data.

Today it builds tested dbt projects on DuckDB from raw files. The aim is
wider: other platforms (Snowflake, Databricks, BigQuery), ingestion from
APIs and databases, orchestration, and diagnosing and optimising existing
pipelines. See [Scope](#scope) for what exists today and what is planned.

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
| Cross-source agent: notes on how the tables join, given to the engineer | Available, and runs by default: it raised the engineer's pass rate in a measured comparison |
| Scouts: notes on each raw table, given to the engineer | Available as an option; in a measured comparison they lowered the engineer's pass rate, and added nothing with the cross-source agent |
| Comparison: how often the engineer passes with and without the other agents | Available |
| Usage ledger: tokens, turns, time and cost by role for every run | Available |
| Spec contract: each model's grain, key, source, rows to leave out and column origins, checked against the profiles and the raw files and written out as `SPEC.md` | Available as Python code (`tenx.contract`, `tenx.spec`); the Architect agent that will write it is not built yet |
| Isolation: agents run in an operating-system sandbox that cannot see the gate's expectations | In progress. Until it lands, do not run the agents on sensitive data (see Security) |
| Coordinated crew: six roles in a resumable workflow with approvals and spending limits | In progress, on the `langgraph` branch |
| Snowflake, Databricks and BigQuery | Planned, read-only first |

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
make engineer    # the cross-source agent, then the engineer builds the demo project; then the gate
make engineer-alone    # the same without the cross-source agent
make engineer-scouts   # the same as make engineer, with a scout for each raw table first
make compare     # runs the engineer with and without the other agents, 128 runs
```

### Demo data

`demo/generate.py` writes a billing system, a CRM and a product event log for
the same set of companies. The three only agree on the customer's email
address and each stores it differently; for some companies the CRM holds
another mailbox at the same domain. Eleven kinds of defect are planted at
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
billing.customers.email ~ crm.accounts.billing_email: 119 shared values
  billing.customers.email     119 of 150 values found (79.3%), 125 of 157 rows match (76 exactly), many rows per value, not found: billing@company0030.example, ...
  crm.accounts.billing_email  119 of 180 values found (66.1%), 119 of 180 rows match (73 exactly), one row per value, not found: accounts@company0030.example, ...
```

That pair is the billing system and the CRM sharing the customer's email.
Each side is reported separately, because the two directions differ: most
billing addresses are in the CRM, while a third of CRM accounts have no
billing address (most are prospects). An exact join would match 73 CRM rows;
comparing in lower case without surrounding spaces matches 119. The values
not found show why some of the rest do not match: `billing@company0030.example`
in billing and `accounts@company0030.example` in the CRM are the same
company with different mailboxes.

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
agent is not given this report; the cross-source agent reads it.

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
where the two systems hold the email differently. For such a model the
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
    "query": "SELECT customers.id AS customer_id, accounts.account_id FROM billing.customers AS customers LEFT JOIN crm.accounts AS accounts ON split_part(lower(trim(customers.email)), '@', 2) = split_part(lower(trim(accounts.billing_email)), '@', 2) WHERE customers.livemode = 'true'"
  }
}
```

```
FAIL  expected  int_customers_matched_to_accounts  customer_id, account_id: 147 rows expected and 147 in the model, of which 13 not expected and 13 expected but missing
      not expected: (cus_000030A, null), (cus_000059A, null), (cus_000064A, null)
      missing: (cus_000030A, 001000000000000030), (cus_000059A, 001000000000000059), (cus_000064A, 001000000000000064)
```

That report is from a model that matches whole email addresses after lower
case and trimming. The declared query matches on the email's domain, since
every demo company has a domain of its own, so the 13 customers whose CRM
account holds another mailbox are left without the account they belong to.

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
has the engineer agent build a dbt project from raw files and then runs the
gate on it. The run passes only if the gate passes. By default the
cross-source agent (below) runs first and gives the engineer notes on how
the tables join; with `--no-cross-source` (`make engineer-alone`) the
engineer works alone.

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

An earlier run of the engineer alone, before the cross-source agent ran by
default:

```
agent: success after 20 turns (exit 0)
project: runs/demo/project

gate: FAIL (23 passed, 1 failed, 0 not run)

PASS  build     project                            dbt build exited 0 after running 15 nodes
...
PASS  key       int_customers_matched_to_accounts  customer_id: unique and not null over 147 rows
FAIL  expected  int_customers_matched_to_accounts  customer_id, account_id: 147 rows expected and 147 in the model, of which 13 not expected and 13 expected but missing
      not expected: (cus_000030A, null), (cus_000059A, null), (cus_000064A, null)
      missing: (cus_000030A, 001000000000000030), (cus_000059A, 001000000000000059), (cus_000064A, 001000000000000064)
...
PASS  repeat    int_customers_matched_to_accounts  the same 147 rows in both builds

usage by role (cost is Claude Code's own estimate, not a bill)

role      outcome    turns  refused    time  cost (US$)
engineer  success       20        0  60.4 s      0.1156
run       gate FAIL     20        0  60.4 s      0.1156

role      model              input  cache write  cache read  output
engineer  claude-haiku-5-5   1,177            0           0      18
engineer  claude-sonnet-5-5     20       16,056     121,958   3,900
```

In that run the agent built every staging model correctly and reported
success, but matched whole email addresses in the crosswalk, so the 13
customers whose CRM account holds another mailbox were left unmatched. The
gate caught it. In other runs the agent notices the mailboxes and matches on
the domain, and the gate passes. Working alone, it passed in 17 of 32 runs
in the comparison below; with the cross-source agent's notes, in 31 of 32.
That is why the cross-source agent runs by default.

Everything from the run is kept under the `--out` folder:

| Path | Contents |
| --- | --- |
| `project/` | The dbt project the agent wrote |
| `agent.json` | What Claude Code reported when the agent stopped |
| `usage.json` | The usage ledger: each role's figures and the gate verdict |
| `gate/` | The gate's databases, dbt artifacts and logs |
| `plugin/` | The copy of the add-on the agent was loaded from |
| `cross_source/`, `cross_source.json` | Unless `--no-cross-source` is given: the cross-source agent's folder, and what Claude Code reported when it stopped |
| `scouts/<table>/`, `scouts/<table>.json` | With `--scouts`: each scout's folder, and what Claude Code reported when it stopped |

Today the agent builds what the demo goal asks for: one staging model per
source table, with renames, type conversions and the exclusions the goal
asks for, and models that combine sources from the staging models, such as
the crosswalk, though not always correctly, as the example shows. It has not
been tried on other goals. It runs once; if the gate fails, nothing is
retried. Running it needs a clone
of this repository and Claude Code installed and signed in.

### Cross-source agent

Unless `--no-cross-source` is given, a second agent runs before the
engineer and works out how the tables the goal needs join. It reads the
goal, the profiles and the overlap report in a folder of its own, and
changes nothing: it may only read files, and it answers in a fixed JSON
shape that Claude Code checks.
For each join it gives the two columns, what to do to the values so that the
ones that belong together are equal, how many rows on one side match a row
on the other, which rows will find no match and why, and the figures it
relied on, with cautions for the engineer at the end.

The runner checks the answer again before using it. Every column it names
must exist in the profiles. A checked answer is written as `JOINS.md` in the
engineer's folder, and the engineer is told to read it before writing a
model that combines sources. An answer that fails the check is not passed
on, the engineer works without it, and the run says why. The agent is
pinned to the Sonnet model and stops after 20 turns.

```
cross-source: success after 9 turns (exit 0); JOINS.md: 2 joins, 8 cautions
agent: success after 18 turns (exit 0)
project: runs/demo/project

gate: PASS (24 passed, 0 failed, 0 not run)
...
role          outcome    turns  refused    time  cost (US$)
cross-source  success        9        0  26.7 s      0.0729
engineer      success       18        0  33.7 s      0.0829
run           gate PASS     27        0  60.4 s      0.1558
```

In that run the notes said that trimming and lower case leave some
customers unmatched, quoted `billing@company0030.example` in billing against
`accounts@company0030.example` in the CRM, and proposed matching on the part
after the `@`, with a caution to check that no two CRM accounts share a
domain. The engineer matched on the domain and the gate passed.

In the comparison below, the engineer with these notes passed the gate in
31 of 32 runs, against 17 of 32 for the engineer alone on the same data.
The notes can still be wrong. On two 20-company demo datasets, in earlier
runs, the agent advised matching whole addresses, the engineer followed it,
and the gate failed. On one, the three values not found on each side did not
show the other mailbox. On the other they did (`billing@company0009.example`
against `admin@company0009.example`), but the agent took the goal's sentence
that the two systems share only the email address to rule out the domain.
In the comparison the agent added US$0.06 of estimated usage to an average
run, and lowered the estimated cost per passing run.

### Scouts

With `--scouts`, a scout runs for each raw table before the engineer, four
at a time. A scout looks at one table: it reads the goal, the table's
profile and up to 20 of its rows in a folder of its own, and changes
nothing. It may only read files, and it answers in a fixed JSON shape that
Claude Code checks: what the engineer must handle in that table, with the
columns concerned, the figures and values that show it and what to do about
it, and questions the files cannot settle. It is told not to advise how its
table joins to others, since it sees only one.

The profile gives exact figures for the whole table, but not what the
values look like side by side; the rows are there for that, such as an
address written in capitals or a timestamp written two ways. They are
picked at random from the file, and the same file always gives the same
rows. Values are shown as they are written in the file: CSV values as text
(a blank one as `null`), and JSON lines exactly as they are.

The runner checks each answer before using it. Every column it names must
be a column of that table. The answers that pass are written together as
`TABLE_NOTES.md` in the engineer's folder, and the engineer is told to read
it before writing the staging models. An answer that fails its check is
left out, and the run says why. Scouts are pinned to the Haiku model and
stop after 8 turns. The cross-source agent still runs unless
`--no-cross-source` is given; the scouts run first, and neither sees the
other's notes.

```
scout billing.customers: success after 5 turns (exit 0); 7 findings, 4 questions
scout billing.invoices: success after 5 turns (exit 0); 6 findings, 4 questions
scout crm.accounts: success after 5 turns (exit 0); 5 findings, 3 questions
scout events.events: success after 5 turns (exit 0); 6 findings, 4 questions
cross-source: success after 9 turns (exit 0); JOINS.md: 2 joins, 8 cautions
agent: success after 18 turns (exit 0)
project: runs/demo-scouts/project

gate: PASS (24 passed, 0 failed, 0 not run)
...
role                     outcome    turns  refused     time  cost (US$)
scout billing.customers  success        5        0   16.8 s      0.0035
scout billing.invoices   success        5        0   24.0 s      0.0044
scout crm.accounts       success        5        0   17.2 s      0.0037
scout events.events      success        5        0   20.4 s      0.0040
cross-source             success        9        0   26.0 s      0.0557
engineer                 success       18        0   33.4 s      0.1166
run                      gate PASS     47        0  137.7 s      0.1879
```

The scouts run at the same time, so the run took less than the `run` row's
time, which adds up each session's time.

In the comparison below, the engineer with scouts passed the gate in 8 of 32
runs, against 17 of 32 for the engineer alone on the same data: the notes
made it worse. Every failure was a crosswalk that matched whole addresses.
The CRM scout sees the email address but not the billing table, and in at
least 9 of those 32 runs it advised matching the whole address despite being
told to leave joins alone, often quoting the goal's sentence that the email
is the only field the two systems share. The scouts add 21 to 38 seconds to
each run, the time the slowest scout takes, and US$0.011 to 0.016 of
estimated usage.

With the cross-source agent as well, the engineer passed in 30 of 32 runs,
against 31 of 32 with the cross-source agent alone: the scouts added nothing
measurable there, and about US$0.04 of estimated usage per run. They remain
an option, off by default.

### Comparison

`python -m tenx.compare run` measures whether an agent helps. It runs the
engineer in four ways, called arms: alone, with scouts, with the
cross-source agent, and with both. Every arm runs the same number of times
on every dataset, and the gate decides each run. `make compare` does this
on eight demo datasets (seeds 1, 7, 42 and 2026, each with 200 and 1,000
companies), four times each: 128 runs, three at a time.

The runs are taken in blocks of one dataset and one trial number, and the
four arms of a block start in a random order, so a change in the service
during the run affects every arm alike. The order is saved in `plan.json`
before the first run. If the comparison stops part way, running the same
command again keeps every run that has a gate verdict and repeats the
others.

A pass rate from 32 runs is uncertain, so the report gives each arm's rate
with a 95% interval, and the chance that its true rate is higher than the
engineer alone's. Both come from a Beta distribution over the rate, worked
out exactly from the counts, so the same runs always give the same report.
An arm is said to help when that chance is at least 0.98, and to hurt when
the chance that it is lower is at least 0.98; otherwise the report says the
difference is not shown. With three arms compared with one baseline, the
0.98 threshold keeps the chance of wrongly calling any of them helpful near
5%. Cost per pass is all the estimated cost of an arm's runs divided by its
passes.

A comparison on the demo data, all 128 runs on one day with Claude Code
2.1.308:

```
comparison: 8 datasets, 4 trials each, 128 of 128 runs have a gate verdict
an arm helps (or hurts) when the chance that its pass rate is higher (or lower) than alone's is at least 0.98

arm           verdict   runs  passes  rate  95% interval  P(> alone)  US$ per run  US$ per pass
alone         baseline    32      17   53%    36% to 69%           -       0.1055        0.1985
scouts        hurts       32       8   25%    13% to 42%       0.011       0.1375        0.5499
cross-source  helps       32      31   97%    84% to 99%       1.000       0.1665        0.1719
both          helps       32      30   94%    80% to 98%       1.000       0.2074        0.2212

passes by dataset

dataset     alone  scouts  cross-source  both
s1-200        3/4     0/4           4/4   4/4
s1-1000       3/4     1/4           4/4   4/4
s7-200        3/4     2/4           4/4   4/4
s7-1000       2/4     0/4           3/4   3/4
s42-200       2/4     2/4           4/4   4/4
s42-1000      0/4     0/4           4/4   4/4
s2026-200     2/4     1/4           4/4   4/4
s2026-1000    2/4     2/4           4/4   3/4

cost is Claude Code's own estimate, not a bill
```

All 42 failures failed the gate on the crosswalk alone. These figures hold
for the demo goal and data, the Claude Code version and the day they were
measured; agent pass rates change from one day to the next, so a comparison
is only fair when every arm runs on the same day, as here. The report is
written to `comparison.txt` and `comparison.json`, and
`python -m tenx.compare report <folder>` prints it again.

Following this comparison, the cross-source agent runs by default. The
comparison's arms are unchanged: `alone` still runs the engineer without it.

### Usage ledger

Every run ends with its usage by role, printed and saved as `usage.json`.
Each role runs as its own Claude Code session, and the figures are read from
the report Claude Code prints when that session ends. The engineer is always
a role; with `--scouts` each scout has a row of its own, named after its
table, and unless `--no-cross-source` is given so does the cross-source
agent, in the order they ran.

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

## Scope

| Area | Today | Planned |
| --- | --- | --- |
| Transformation | dbt on DuckDB: staging models and a cross-source match | Marts and incremental models; plain SQL, Python and PySpark |
| Ingestion | Local CSV, JSON and Parquet files | REST and GraphQL APIs, database extraction, change data capture, streaming |
| Platforms | DuckDB | Snowflake first, then Databricks and BigQuery |
| Orchestration | None | Airflow, Dagster, Databricks Jobs |
| Checking results | The gate: build, rows, keys, money, expected rows, repeat build | Contracts, incremental and idempotency checks |
| Fixing and tuning | None | Diagnosing failed pipelines; performance and cost |

New areas are added as tools, specialist instructions and platform adapters
used by the same roles, rather than as more agents. dbt and DuckDB are the
first of each, not fixed choices.

## Design

This section describes the target design. The status table above shows which
parts exist today.

Input is a goal in plain English and a folder of raw files (CSV, JSON or
Parquet). Output is a dbt project that has passed the gate, plus a report
that separates what was verified, what was inferred, what is uncertain and
what needs approval.

The crew has six roles. The coordinator is ordinary code; the other roles are
agents, and each is used only when the job needs it.

| Role | Responsibility | Today |
| --- | --- | --- |
| Supervisor | Turns a request into a plan, picks the roles, enforces spending limits and asks for approval | The runner in `tenx.engineer`, without approvals or limits per run |
| Discovery | Profiles the sources, compares columns across tables, works out how they join | Profiler, overlap report and cross-source agent |
| Architect | Writes the spec: grain, key, source, rows to leave out and column origins for each model | Spec contract checks exist; the agent does not |
| Engineer | Builds the dbt models, and fixes them when the gate reports a failure | Builds the models; no second attempt yet |
| Diagnostics | Finds why an existing project fails, separating confirmed facts from guesses | Planned |
| Reviewer | Reviews the result against the spec; advisory, never overrides the gate | Planned |

The workflow is being built with [LangGraph](https://docs.langchain.com/oss/python/langgraph/overview)
on the `langgraph` branch. Each run will be saved after every step, so it can
be resumed after a stop, and will pause for approval before the spec is used and
before anything leaves the run folder. Every role is measured against the
version without it, and a role stays in the default run only if it improves
the pass rate for its cost.

The gate decides every result. It fails a run unless the project builds twice
from an empty database with identical results, every model's key is unique
and not null, each model holds the rows of its source less the declared
exclusions, and money totals match the raw sources by currency. Its
expectations are kept outside the agents' working folder; the sandbox (in
progress) will make them unreadable to the agents.

### Costs and limits

Agents run on your own Claude subscription or API key; there is no hosted
service. Each agent call has a turn limit, a spending limit and a time limit.
The coordinated workflow will add a limit per run: before each agent starts,
its full allowance will be set aside, and the agent will start only if that
fits in what is left. Claude Code checks its own spending limit after each
response, so a single call can pass its limit by up to one response; reports
will show this.
Dollar figures are Claude Code's estimates. On a subscription they are not
what you are charged; the limit that applies is your plan's usage.

### Platforms

DuckDB is the only platform today. The coordinated workflow will reach the
database through small interfaces (profile, validate, build), so that other
platforms can be added without changing the agents. Snowflake, Databricks and BigQuery are planned,
read-only first.

## Security

The engineer agent may run only `dbt`, but dbt can read files through DuckDB
and can run Python models, so tool permissions alone do not stop it reading
files outside its project. Until the operating-system sandbox lands, run the
agents only on data you would be comfortable sharing with them, and keep
credentials out of the machine's readable files. On Linux the sandbox will use
[bubblewrap](https://github.com/containers/bubblewrap); on other systems the
runner will refuse to run unsandboxed unless you say so.

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
| `tenx/` | Python package: the source profiler, the overlap report, the gate, the agents' runner, the row sample, the checks of the cross-source agent's and scouts' answers, and the comparison |
| `tests/` | Tests, and the dbt project the gate is tested against |

## Licence

[MIT](LICENSE)
