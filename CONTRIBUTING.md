# Contributing

## Getting started

```
make install
make test
make demo-data
make profile
```

Python 3.10 or later. `make install` installs the package in editable mode
together with DuckDB and dbt. The gate tests run `dbt build` several times and
take most of a minute.

## Guidelines

- One logical change per commit, with its tests.
- Run `make test` before opening a pull request. Do not build on a failing
  baseline.
- Notable changes go in `CHANGELOG.md`.

## Layout

| Path | Contents |
| --- | --- |
| `.claude-plugin/` | Manifest that tells Claude Code what the add-on contains |
| `demo/` | Generator for the three demo source systems |
| `tenx/` | Python package: the source profiler and the gate |
| `tests/` | Tests, and the dbt project the gate is tested against |
