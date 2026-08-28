# Databricks → CanvasXpress

Same as the SQLite/Postgres examples, but against a **Databricks SQL Warehouse** — showing
that `SqlSource` is driver-agnostic: only the connection URL changes. Databricks is reached
through the `databricks-sql-connector` SQLAlchemy dialect, so **no new connector code** is
needed.

## 1. A Databricks warehouse to talk to

You need a running **SQL Warehouse** (Databricks SQL → Warehouses) and:

- the workspace **hostname** — `dbc-xxxx.cloud.databricks.com`
- the warehouse **HTTP path** — `/sql/1.0/warehouses/<id>` (warehouse → Connection details)
- a **personal access token** — User Settings → Developer → Access tokens (or an OAuth M2M token)

## 2. Install + seed + run

```bash
# from the repo root, once:
pip install -e ".[databricks]"          # SQLAlchemy + databricks-sql-connector[sqlalchemy]

cd examples/databricks
export DATABRICKS_HOST=dbc-xxxx.cloud.databricks.com
export DATABRICKS_HTTP_PATH=/sql/1.0/warehouses/abc123
export DATABRICKS_TOKEN=dapi...
export DATABRICKS_CATALOG=main          # optional, default 'main'
export DATABRICKS_SCHEMA=default        # optional, default 'default'
python seed.py                          # create + fill the demo table (needs write access)
uvicorn app:app --port 8096             # open http://localhost:8096
```

Already have a table to chart? Skip `seed.py` and point `QUERY` in `app.py` at it.

## What's different from SQLite / Postgres

Only the connection string:

```python
# sqlite:     sqlite:///example.db
# postgres:   postgresql+psycopg://user:pw@localhost:5432/demo
# databricks: databricks://token:<PAT>@<host>?http_path=/sql/1.0/warehouses/<id>&catalog=main&schema=default
```

`SqlSource`, `to_cx`, `/api/data`, and the front end are unchanged. The read-only `SELECT`
guard and `:name` bind-parameter forwarding work exactly as with any other SQL backend.

## Notes

- **Least privilege:** the app only reads — give it a token/user with read-only grants on the
  warehouse. `seed.py` is the only step that writes; run it with a separate write-capable token.
- **Identifier quoting:** Databricks uses backticks for quoted identifiers (`` `GeneA` ``),
  which is what `app.py`'s `QUERY` uses to preserve the CanvasXpress column casing.
- **Cold starts:** a stopped SQL Warehouse takes ~10–30s to resume on the first query; the
  page will show "Loading…" until it does.
