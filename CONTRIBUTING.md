# Contributing

## Getting started

```
make test
make demo-data
```

The demo generator and tests need Python 3.10 or later and nothing else.

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
| `tests/` | Tests |
