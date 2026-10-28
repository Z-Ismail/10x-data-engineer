# 10x Data Engineer

**An AI data engineering team that designs, builds, tests and maintains data
pipelines, and proves every result against the source data.**

10x Data Engineer is an open-source, multi-agent system for data
engineering. Give it a goal in plain English and access to your sources; a
crew of specialist agents profiles the data, writes the specification,
builds the pipeline, and diagnoses and repairs it when something breaks.
Nothing it builds is accepted on the agents' word: an independent, coded
gate checks every result against the raw data before it is called done.

It is designed to be platform-agnostic: SQL, Python, PySpark and dbt; file,
API and database ingestion; batch, incremental, change data capture and
streaming; orchestration with Airflow, Dagster and others; and warehouses
including Snowflake, Databricks and BigQuery. It runs as an add-on to
[Claude Code](https://code.claude.com/docs), on your own Claude
subscription or API key, with no hosted service in between.

> **Status: early development.** The verification gate, the profiling tools
> and the first agents work today on dbt and DuckDB. The full crew, other
> platforms and operating features are being built in stages. See
> [Roadmap](#roadmap) for what is available now.

## Why

Most of the cost of a data pipeline is not in writing it. It is in finding
out what the sources really contain, deciding what each table should mean,
and proving that the numbers are right.

Coding agents are quick at the writing and unreliable at the proving. Left
alone, they produce pipelines that build cleanly and are wrong: a join that
duplicates rows, a load that silently drops records, a test relaxed until it
passes. A green build is not evidence.

10x Data Engineer treats verification as the product. Agents do the work;
independent code, whose expectations are kept from the agents, decides
whether the work is right.

## How it works

A run moves through fixed stages, each owned by one role. This is the
target workflow; the [roadmap](#roadmap) shows which stages exist today.

1. **Discover.** Profile every source, compare columns across tables and
   work out how the systems join.
2. **Specify.** Write the specification for each model: what one row means,
   its key, its source, which records to leave out and where each column
   comes from. You approve it before anything is built.
3. **Build.** Write the models and build them.
4. **Verify.** The gate rebuilds the project from nothing and checks it
   against the raw data.
5. **Repair.** If the gate fails, the engineer gets the exact failures and
   tries again, within set limits.
6. **Review and report.** An advisory review against the specification, and
   a report that separates what was verified, what was inferred and what
   needs your decision.

### The crew

| Role | Responsibility |
| --- | --- |
| Supervisor | Turns a request into a plan, chooses the roles a job needs, enforces spending limits and asks for approval |
| Discovery | Profiles the sources, compares columns across tables and works out how they join |
| Architect | Writes the specification: grain, keys, sources, exclusions and column lineage |
| Engineer | Builds the pipeline, and repairs it when the gate reports a failure |
| Diagnostics | Finds why an existing pipeline fails, separating confirmed facts from hypotheses |
| Reviewer | Reviews the result against the specification; advisory, and never overrides the gate |

New capabilities arrive as tools, specialist instructions and platform
adapters used by these roles, not as more agents.

### The gate

The gate is the only judge of a result. It is ordinary code, independent of
the agents, and its expectations are never shown to them. A run passes only
if:

- the project builds twice from an empty database with identical results;
- every model's key is unique and not null;
- each model holds exactly the rows of its source, less the declared
  exclusions, with every excluded row accounted for;
- money totals match the raw sources to the unit, by currency;
- models that match records across systems match the right ones.

## Principles

- **Verified, not trusted.** A result counts only when an independent check
  against the source data passes.
- **Measured, not assumed.** Every role is compared with the run without it,
  on the same data, and stays in the default run only if it raises the pass
  rate for its cost. In the first such comparison, adding a cross-source
  agent took the engineer from 17 to 31 passes in 32 runs, and lowered the
  cost per passing run; scouts lowered the pass rate and were left out.
- **Safe by default.** The design isolates every agent in an
  operating-system sandbox that fails closed, pauses for approval before
  consequential steps, and keeps each run within spending limits set before
  it starts.
- **Platform-agnostic.** Agents work through capability interfaces (source,
  transform engine, validator, orchestrator, platform). dbt and DuckDB are
  the first implementations, not the centre.
- **Your data, your plan.** It runs on your machine with your own Claude
  subscription or API key; there is no third-party service in between.

## Roadmap

| Area | Capability | Status |
| --- | --- | --- |
| Verification | Coded gate: build, rows, keys, money totals, expected rows, repeat build | Available |
| Discovery | Source profiler and cross-table overlap report | Available |
| Discovery | Cross-source agent: how the tables join, given to the engineer | Available, on by default |
| Build | Engineer agent: dbt models on DuckDB from a plain-English goal | Available |
| Measurement | Usage ledger per role, and measured comparisons of crew configurations | Available |
| Specification | Machine-checked spec contract and `SPEC.md` | Available as code; the Architect agent is next |
| Safety | Operating-system sandbox for every agent, failing closed | Available on Linux: every agent, and the gate's builds of its work, run in a bubblewrap sandbox; other systems refuse unless `--unisolated` |
| Portability | Capability interfaces for sources, transform engine, validation, sandbox and agents; DuckDB, dbt and Claude Code as the first implementations | Available as code; the workflow that uses them is in progress |
| Cost control | Spending policy: a cap per agent call, a run ceiling enforced by reserving each call's cap before it starts, and approval for comparisons estimated above US$5 | Available as code: the per-call caps and the comparison approval apply today; the run ceiling comes into use with the workflow |
| Orchestration | Six-role workflow with checkpoints, resume, approvals and spending limits | In progress |
| Build | Repair loop driven by the gate's report | Planned |
| Operations | Diagnostics for failing pipelines; performance and cost tuning | Planned |
| Review | Advisory reviewer, measured on planted defects | Planned |
| Platforms | Snowflake first, then Databricks and BigQuery, read-only first | Planned |
| Transformation | Marts and incremental models; plain SQL, Python and PySpark | Planned |
| Ingestion | REST and GraphQL APIs, database extraction, change data capture, streaming | Planned |
| Orchestration | Airflow, Dagster and Databricks Jobs | Planned |

## Getting started

Requirements: Python 3.10 or later;
[Claude Code](https://code.claude.com/docs) installed and signed in for the
agents; and on Linux, bubblewrap (`sudo apt install bubblewrap` on Debian
and Ubuntu, `sudo dnf install bubblewrap` on Fedora) for the sandbox.

```
make install    # the package, DuckDB and dbt
make demo-data  # three demo source systems with planted defects
make test
make engineer   # the agents build the demo project, then the gate checks it
```

The demo data is a billing system, a CRM and a product event log for the
same companies, with eleven kinds of defect planted at known rates and an
answer key kept apart. Every command, with example output, is described in
the [component reference](docs/components.md).

## Security

dbt can read files through DuckDB and can run Python models, so limiting an
agent's tools does not limit what it can read. Isolation therefore comes
from the operating system. On Linux every agent runs in a
[bubblewrap](https://github.com/containers/bubblewrap) sandbox that shows it
only its own folder, the programs it runs and the data its work needs; the
gate's expectations, the repository and your home folder are hidden. Where
no sandbox can be set up, the runner refuses to start the agents unless you
pass `--unisolated`, and the run is then marked as unisolated.

The gate builds the agent's project in the same sandbox, so a dbt Python
model the agent wrote cannot read the gate's expectations or change its
databases either. The sandbox does not yet limit network access or
environment variables. Until it does, run the agents only on data you would
be comfortable sharing with them. Details, and how the
isolation is tested, are in [docs/security.md](docs/security.md).

## Costs

There is no hosted service: agents use your own Claude subscription or API
key. Each agent call has a turn limit, a spending limit (US$0.75 for the
engineer, US$0.40 for the cross-source agent) and a time limit, and every
run records its tokens, turns, time and estimated cost by role. Claude Code
checks a spending limit after each model response, so a call can pass it by
up to one response. A measured comparison estimated above US$5 does not
start without `--approve-cost`. Dollar figures are Claude Code's estimates;
on a subscription, the limit that applies is your plan's usage. Details are
in the [component reference](docs/components.md#spending-policy).

## Documentation

- [Component reference](docs/components.md): every tool and agent that
  exists today, how to run it, and example output
- [Security](docs/security.md): the sandbox, what it hides and what it does not cover yet
- [Demo data](demo/README.md): the source systems and their planted defects
- [Contributing](CONTRIBUTING.md)
- [Changelog](CHANGELOG.md)

## Licence

[MIT](LICENSE)
