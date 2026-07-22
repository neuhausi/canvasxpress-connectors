# Contributing

Thanks for your interest in improving **canvasxpress-connectors**.

## Development setup

```bash
git clone https://github.com/neuhausi/canvasxpress-connectors
cd canvasxpress-connectors
python -m venv .venv && source .venv/bin/activate
pip install -e ".[all,dev]"
```

## Before you open a PR

Run the lint and test suite locally — CI runs the same on Python 3.9–3.13:

```bash
ruff check .        # lint (import order, unused code, style)
pytest -q           # tests
```

`ruff check . --fix` auto-fixes most findings.

## Adding a new data source

A source is one small class that returns `(header, rows)`; `reshape.rows_to_cx` handles
the rest. See `src/cx_connectors/sources/sql.py` for the pattern:

```python
class MySource:
    def read(self):
        return header, rows      # column names + row values
```

Add a matching test in `tests/` (inject a fake client so tests need no network — see
`tests/test_sheets.py` for the approach).

## Guidelines

- Keep the **core** (`reshape`, `store`, `sources.base`) free of heavy dependencies;
  put backend libraries behind the lazy imports + optional extras used elsewhere.
- Encrypt secrets at rest and never log credentials.
- Match the existing style; `ruff` enforces the basics.

## Releases

Maintainers only — see the "Releasing" section in the README. In short: bump the version
in `pyproject.toml`, tag `vX.Y.Z`, and push the tag; Trusted Publishing does the rest.
