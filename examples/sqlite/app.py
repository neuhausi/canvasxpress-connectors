"""SQLite -> CanvasXpress, the smallest possible example.

Reads a local SQLite file with SqlSource and serves the CanvasXpress object at
/api/data. No auth (single, app-owned database) -- this is the plain data-path demo.

    pip install -e "../..[sql,web]"     # or ".[all]" from the repo root
    python seed.py                      # creates example.db
    uvicorn app:app --port 8090         # open http://localhost:8090
"""

import os

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from cx_connectors.sources import SqlSource
from cx_connectors.sources.base import to_cx

HERE = os.path.dirname(os.path.abspath(__file__))
CONN_URL = "sqlite:///" + os.path.join(HERE, "example.db")

# The query is server-side config (never sent by the browser). Text columns become
# annotations; the quoted aliases set their display names.
QUERY = (
    'SELECT sample, GeneA, GeneB, GeneC, GeneD, '
    'category AS "Category", grp AS "Group" '
    'FROM expression ORDER BY sample'
)

app = FastAPI(title="SQLite -> CanvasXpress")


@app.get("/api/data")
def data():
    return JSONResponse(to_cx(SqlSource(CONN_URL, QUERY)))


app.mount("/", StaticFiles(directory=os.path.join(HERE, "static"), html=True), name="static")
