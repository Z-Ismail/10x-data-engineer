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

The tests do not run the engineer agent itself: they replace the `claude`
command with a stand-in that writes a known project. To run the real agent
on the demo data, install Claude Code, sign in and run `make engineer`.

## Guidelines

- One logical change per commit, with its tests.
- Run `make test` before opening a pull request. Do not build on a failing
  baseline.
- Notable changes go in `CHANGELOG.md`.

## Layout

| Path | Contents |
| --- | --- |
| `.claude-plugin/` | Manifest that tells Claude Code what the add-on contains |
| `agents/` | The agents' instructions |
| `demo/` | Generator for the three demo source systems, and their goal |
| `tenx/` | Python package: the source profiler, the gate and the engineer agent's runner |
| `tests/` | Tests, and the dbt project the gate is tested against |
