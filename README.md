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
| Specification | Architect agent: a machine-checked spec contract and `SPEC.md` for the engineer | In progress: built into the graph run, off by default (`python -m tenx.run run --architect`, `make engineer-architect`); not shown to raise the pass rate. With `--review-spec` the run waits for a person to approve, edit or reject the spec before the engineer starts. The measured comparison on the brief demo goal (`make compare-architect`, 64 runs, each spec scored against the gate's hidden declaration) was run on 2026-11-11: 5 of 32 passes with the Architect against 2 of 32 without, not enough to say it helps (chance it is better 0.87, against the 0.98 required); in 13 of the 32 runs its spec could not be used, mostly because a rule read a second table; the Architect is now told to keep each rule to its own table's columns, and such a rule is refused before counting (not yet re-measured). It stays off by default |
| Safety | Operating-system sandbox for every agent, failing closed | Available on Linux: every agent, and the gate's builds of its work, run in a bubblewrap sandbox; other systems refuse unless `--unisolated` |
| Portability | Capability interfaces for sources, transform engine, validation, sandbox and agents; DuckDB, dbt and Claude Code as the first implementations | Available: the graph run works only through them |
| Cost control | Spending policy: a cap per agent call, a run ceiling enforced by reserving each call's cap before it starts, and approval for comparisons estimated above US$5 | Available: the per-call caps and the comparison approval apply to every run; the run ceiling applies to graph runs (`python -m tenx.run`) |
| Orchestration | Six-role workflow with checkpoints, resume, approvals and spending limits | In progress: the build workflow runs as a graph that is saved after every step and can be resumed (`python -m tenx.run`), with the run ceiling enforced; with a stand-in for Claude Code it leaves byte-identical projects, notes, reports and usage to the current runner's on eight demo datasets (`make equivalence`). A paired comparison with real agents against the current runner (`make parity`, 64 pairs, a predeclared 10-point margin) was run on 2026-11-06 and found the graph non-inferior: 62 of 64 passes against 59 of 64, at 0.96 times the cost per pass. `make engineer` still uses the current runner. A run can wait for approval of the Architect's spec (`--review-spec`); approval of the handover and the other roles come later |
| Build | Repair loop driven by the gate's report | Available in the graph run, on by default since 2026-11-11 (`python -m tenx.run run` repairs a failing project up to twice; `--repairs 0` turns it off); the comparison that measures it (`make repairs`, the engineer alone, each run against its own first round) was run on 2026-11-11: with up to two repairs 32 of 32 runs passed against 11 of 32 first rounds, +65.6 points (95% interval +47.9 to +79.6), verdict better, at 0.55 times the cost per pass |
| Operations | Diagnostics for failing pipelines; performance and cost tuning | In progress: a test bed of thirteen dbt projects that each fail the gate for one known cause (two of them mistakes engineer agents made in live runs), with the checks each one fails and the model and file where the cause is, for measuring how often a diagnosis finds the cause (`python tests/failing_bed.py --out <folder>`). What the Diagnostics agent will be given of a failing project (its models, every check with what an agent may be told of it, dbt's results and log with the run's folders renamed, never the gate's hidden checks) and the code check of its answer (each fact must quote a file it was given, each cause must name a model, file and line the project has and checks that failed or could not run) are built; the agent itself is not |
| Review | Advisory reviewer, measured on planted defects | In progress: the reviewer agent is built into the graph run, off by default (`python -m tenx.run run --reviewer`, `make engineer-reviewer`): after a gate pass it reads the review packet (the goal, spec, profiles, the project's models and the checks that already ran, never the gate's hidden checks) and advises, with every finding checked in code against a file and line of the project; its advice is saved as `REVIEW.md` and never changes the gate's verdict. A test bed of twelve dbt projects that all pass the gate, six with one planted mistake in a column the gate does not check and six correct but written differently, is used to measure how often it catches mistakes and how often it rejects good work (`make review-rates`, 48 reviews: each of the twelve projects on four demo datasets). On 2026-11-12 it caught 24 of 24 planted mistakes at the right model and column and rejected none of the 24 correct projects, about US$0.05 a review, which meets the rule set before the run; every planted mistake is visible in one line of SQL, so this says nothing yet about subtler ones. It stays off by default until that result is applied |
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
make install    # the package, DuckDB, dbt and LangGraph
make demo-data  # three demo source systems with planted defects
make test
make engineer   # the agents build the demo project, then the gate checks it
make engineer-graph  # the same as a graph run that can be resumed if it stops
make engineer-brief  # the graph run on a brief goal that leaves the spec unwritten
make engineer-architect  # the same, with the Architect writing the spec first
make engineer-reviewer   # the graph run, with the reviewer advising after a gate pass
make equivalence     # the graph against the current runner, with a stand-in for Claude Code
make parity          # the graph against the current runner with real agents (needs approval)
make repairs         # repairs against no repairs with real agents (needs approval)
make compare-architect  # the graph with and without the Architect on the brief goal (needs approval)
make review-rates     # the reviewer on the test bed: how often it catches mistakes and rejects good work
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
