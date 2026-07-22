"""Postgres -> CanvasXpress.

Identical to the SQLite example except the connection URL points at a Postgres server
(read from DATABASE_URL). That is the whole point: `SqlSource` is driver-agnostic, so
swapping the database is a one-line change and nothing else moves.

    pip install -e ".[all]"  &&  pip install "psycopg[binary]"
    export DATABASE_URL=postgresql+psycopg://user:pw@localhost:5432/postgres
    python seed.py
    uvicorn app:app --port 8095        # open http://localhost:8095
"""

import os

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from cx_connectors.sources import SqlSource
from cx_connectors.sources.base import to_cx

HERE = os.path.dirname(os.path.abspath(__file__))
CONN_URL = os.environ["DATABASE_URL"]   # e.g. postgresql+psycopg://user:pw@host:5432/db

QUERY = (
    'SELECT sample, "GeneA", "GeneB", "GeneC", "GeneD", '
    'category AS "Category", grp AS "Group" '
    'FROM expression ORDER BY sample'
)

app = FastAPI(title="Postgres -> CanvasXpress")


@app.get("/api/data")
def data():
    return JSONResponse(to_cx(SqlSource(CONN_URL, QUERY)))


app.mount("/", StaticFiles(directory=os.path.join(HERE, "static"), html=True), name="static")
