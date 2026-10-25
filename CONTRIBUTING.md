# Contributing

## Getting started

```
make install
make test
make demo-data
make profile
```

Python 3.10 or later. `make install` installs the package in editable mode
together with DuckDB and dbt. The gate tests run `dbt build` many times and
take a few minutes.

The sandbox tests need bubblewrap (`sudo apt install bubblewrap`) and are
skipped without it, unless `TENX_REQUIRE_SANDBOX=1` is set, as CI does; then
they fail. On Ubuntu 24.04 and later bubblewrap also needs an AppArmor
profile; the CI workflow installs one.

The tests do not run the agents themselves: they replace the `claude`
command with a stand-in that writes a known project, or gives a known answer
for the cross-source agent and the scouts. To run the real agents on the
demo data, install Claude Code, sign in and run `make engineer`,
`make engineer-alone` or `make engineer-scouts`. `make compare` runs
all of them 128 times and takes one to two hours.

## Guidelines

- One logical change per commit, with its tests.
- Run `make test` before opening a pull request. Do not build on a failing
  baseline.
- Changes reach `main` through pull requests. CI must pass first: the tests,
  `ruff check` and `ruff format --check`, and `pip-audit` on the installed
  dependencies.
- Notable changes go in `CHANGELOG.md`.

## Layout

| Path | Contents |
| --- | --- |
| `.claude-plugin/` | Manifest that tells Claude Code what the add-on contains |
| `agents/` | The agents' instructions |
| `demo/` | Generator for the three demo source systems, and their goal |
| `tenx/` | Python package: the source profiler, the overlap report, the gate, the agents' runner and its sandbox, the row sample, the checks of the cross-source agent's and scouts' answers, and the spec contract's check |
| `tests/` | Tests, and the dbt project the gate is tested against |
