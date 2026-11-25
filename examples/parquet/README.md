# Parquet → CanvasXpress (DuckDB)

Read a Parquet file with `ParquetSource` — **just a path**, no hand-written
`duckdb:///` URL or `read_parquet()` SQL — reshape to a CanvasXpress object, chart it.
A grouped pushdown query aggregates **in DuckDB**, so only the result rows travel. No
database server: DuckDB is in-process.

```bash
# from the repo root, install the package (once):
pip install -e ".[duckdb,web]"      # or ".[all]"

cd examples/parquet
python seed.py                      # creates example.parquet (50k rows)
uvicorn app:app --port 8090         # open http://localhost:8090
```

## What it shows

- `ParquetSource("example.parquet")` builds a `(duckdb:/// URL, read_parquet SELECT)`
  pair, so it **is** a `SqlSource`.
- `to_cx(source)` turns the rows into `{y: {vars, smps, data}, x: {...}}`.
- `/api/data?_q={groupBy,measures}` runs `run_pushdown`, so DuckDB does the aggregation
  and the response carries an `X-Cx-Rows` header with the (small) row count.

## Remote files (S3 / HTTPS)

`ParquetSource` supports remote objects too (DuckDB `httpfs`, auto-loaded):

```python
# Public HTTPS — no credentials:
ParquetSource("https://host/path/data.parquet")

# Private S3 — credentials ride the connection URL (so pushdown/join inherit them):
ParquetSource("s3://bucket/orders/*.parquet",
              s3={"region": "us-east-1",
                  "access_key_id": "…", "secret_access_key": "…"})
```

A list of paths (`ParquetSource(["a.parquet", "b.parquet"])`) unions them. The read-only
`SELECT` guard applies unchanged — the Parquet location is server-side config, never a
browser value.
