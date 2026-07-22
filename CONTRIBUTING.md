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

Maintainers only. Releases publish to PyPI automatically via **Trusted Publishing**
(OIDC — no API tokens). To cut a release:

1. Bump `version` in `pyproject.toml` (PyPI rejects re-publishing an existing version).
2. Commit, then tag and push:
   ```bash
   git tag vX.Y.Z
   git push origin vX.Y.Z
   ```
3. The `release.yml` workflow builds the sdist + wheel, runs `twine check`, and
   publishes to PyPI. Watch it under the repo's **Actions** tab.

The one-time setup (already done for this project) is a PyPI *pending publisher* bound to
`owner=neuhausi`, `repo=canvasxpress-connectors`, `workflow=release.yml`,
`environment=pypi`. To require manual approval before a publish, add yourself as a
required reviewer on the `pypi` environment (repo **Settings → Environments → pypi**).
