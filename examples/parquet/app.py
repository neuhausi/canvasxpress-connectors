"""Parquet -> CanvasXpress, the smallest possible example (DuckDB, no server).

Reads a local Parquet file with ParquetSource -- just a path, no hand-written
duckdb:/// URL or read_parquet() SQL -- and serves the CanvasXpress object at
/api/data. A grouped pushdown query (?_q=...) aggregates in DuckDB so only the
result rows travel. ParquetSource IS a SqlSource, so s3:// / https:// URLs and
join/columns all work the same way.

    pip install -e "../..[duckdb,web]"   # or ".[all]" from the repo root
    python seed.py                       # creates example.parquet
    uvicorn app:app --port 8090          # open http://localhost:8090
"""

import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from cx_connectors.pushdown import parse_query, run_pushdown
from cx_connectors.reshape import rows_to_cx
from cx_connectors.sources import ParquetSource, to_cx

HERE = os.path.dirname(os.path.abspath(__file__))
# A local path; swap for "s3://bucket/key.parquet" (s3={...}) or an https:// URL.
PARQUET = os.path.join(HERE, "example.parquet")

app = FastAPI(title="Parquet -> CanvasXpress")


@app.get("/api/data")
def data(request: Request):
    src = ParquetSource(PARQUET)
    raw_q = request.query_params.get("_q")
    if raw_q:
        query = parse_query(raw_q)
        header, rows, truncated = run_pushdown(src.conn_url, src.sql, {}, query, 100_000)
        groups = query.get("groupBy") or []
        if groups:
            header = ["id"] + list(header)
            rows = [[" · ".join(str(v) for v in r[:len(groups)])] + list(r) for r in rows]
        return JSONResponse(rows_to_cx(header, rows) if rows else {"y": {"vars": [], "smps": [], "data": []}},
                            headers={"X-Cx-Rows": str(len(rows))})
    return JSONResponse(to_cx(src))


app.mount("/", StaticFiles(directory=os.path.join(HERE, "static"), html=True), name="static")
