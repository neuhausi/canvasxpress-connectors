"""Databricks -> CanvasXpress.

Identical to the SQLite/Postgres examples except the connection URL points at a Databricks
SQL Warehouse. That is the whole point: `SqlSource` is driver-agnostic, so swapping the
database is a one-line change (the URL) and nothing else moves — Databricks is reached
through its `databricks-sql-connector` SQLAlchemy dialect.

    pip install -e ".[databricks]"          # or ".[all]" + databricks-sql-connector[sqlalchemy]
    export DATABRICKS_HOST=dbc-xxxx.cloud.databricks.com
    export DATABRICKS_HTTP_PATH=/sql/1.0/warehouses/abc123
    export DATABRICKS_TOKEN=dapi...
    export DATABRICKS_CATALOG=main          # optional, default 'main'
    export DATABRICKS_SCHEMA=default        # optional, default 'default'
    python seed.py                          # create + fill the demo table
    uvicorn app:app --port 8096             # open http://localhost:8096
"""

import os

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from cx_connectors.sources import SqlSource
from cx_connectors.sources.base import to_cx

HERE = os.path.dirname(os.path.abspath(__file__))


def conn_url():
    """Build the Databricks SQLAlchemy URL from environment variables.

    The token is the password of a `token:` user; the warehouse is selected by the
    `http_path` query arg. Catalog/schema default sensibly but are overridable.
    """
    catalog = os.environ.get("DATABRICKS_CATALOG", "main")
    schema = os.environ.get("DATABRICKS_SCHEMA", "default")
    return (
        "databricks://token:" + os.environ["DATABRICKS_TOKEN"] + "@"
        + os.environ["DATABRICKS_HOST"]
        + "?http_path=" + os.environ["DATABRICKS_HTTP_PATH"]
        + "&catalog=" + catalog + "&schema=" + schema
    )


QUERY = (
    "SELECT sample, geneA AS `GeneA`, geneB AS `GeneB`, geneC AS `GeneC`, "
    "geneD AS `GeneD`, category AS `Category`, grp AS `Group` "
    "FROM expression ORDER BY sample"
)

app = FastAPI(title="Databricks -> CanvasXpress")


@app.get("/api/data")
def data():
    return JSONResponse(to_cx(SqlSource(conn_url(), QUERY)))


app.mount("/", StaticFiles(directory=os.path.join(HERE, "static"), html=True), name="static")
