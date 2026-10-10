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
| Demo data generator and tests | Available |
| Source profiler | Next |
| Automated checks (the gate) | Planned |
| Agents | Planned |

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
make demo-data   # writes three source systems to demo/data
make test
```

`demo/generate.py` writes a billing system, a CRM and a product event log for
the same set of companies. The three only agree on the customer's email
address and each stores it differently. Ten kinds of defect are planted at
known rates (duplicate customers, test-mode rows, yen amounts, refunds, junk
user IDs and others), and an answer key is written to a separate folder.
Output is deterministic for a given seed, and every defect is present
whatever the seed. Details are in
[demo/README.md](demo/README.md).

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

Python 3.10 or later. The demo generator and tests use the standard library
only. dbt-core 1.12 and dbt-duckdb 1.11 are listed as an optional dependency
group in `pyproject.toml` for the stages that follow.

## Repository layout

| Path | Contents |
| --- | --- |
| `.claude-plugin/` | Manifest that tells Claude Code what the add-on contains |
| `demo/` | Demo data generator |
| `tests/` | Tests |

## Licence

[MIT](LICENSE)
