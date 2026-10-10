# Contributing

## Getting started

```
make install
make test
make demo-data
make profile
```

Python 3.10 or later. `make install` installs the package in editable mode
together with DuckDB.

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
| `tenx/` | Python package: the source profiler |
| `tests/` | Tests |
