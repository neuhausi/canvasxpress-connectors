"""ParquetSource — the convenience class that builds a (duckdb:/// url, read_parquet SELECT)
pair from just a path/URL, so it IS a SqlSource (plan P0 design A).

Proves: to_cx over a ParquetSource returns a CanvasXpress object with the file's columns;
a columns= projection narrows them; the built SQL passes the read-only guard and an
injection attempt raises; pushdown works through the built (conn_url, sql) — inheriting the
DuckDB config from the URL; and importing the package does not pull duckdb eagerly.
"""

import subprocess
import sys

import pytest

from cx_connectors.pushdown import run_pushdown
from cx_connectors.sources import ParquetSource, to_cx
from cx_connectors.sources.parquet import (
    build_duckdb_url,
    build_read_parquet_sql,
)
from cx_connectors.sources.sql import assert_read_only

duckdb = pytest.importorskip("duckdb")
pytest.importorskip("duckdb_engine")


@pytest.fixture(scope="module")
def parquet(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("pq") / "orders.parquet")
    duckdb.sql(
        "COPY (SELECT 'r' || (i % 3) AS region, 'p' || (i % 4) AS product, "
        "(i % 7) * 1.0 AS amount FROM range(300) t(i)) "
        "TO '" + path + "' (FORMAT PARQUET)"
    )
    return path


def test_to_cx_returns_object_with_columns(parquet):
    cx = to_cx(ParquetSource(parquet))
    assert "y" in cx and "vars" in cx["y"]
    # amount is the only numeric measure column; region/product become sample annotations.
    assert "amount" in cx["y"]["vars"]


def test_columns_projection(parquet):
    src = ParquetSource(parquet, columns=["region", "amount"])
    header, rows = src.read()
    assert header == ["region", "amount"]


def test_built_sql_is_read_only_and_injection_rejected(parquet):
    src = ParquetSource(parquet)
    assert_read_only(src.sql)
    with pytest.raises(ValueError):
        ParquetSource("a') ; DROP TABLE t --")
    with pytest.raises(ValueError):
        ParquetSource(parquet, where="1=1; DROP TABLE t")
    with pytest.raises(ValueError):
        ParquetSource(parquet, columns=["a; DROP"])


def test_pushdown_through_built_pair(parquet):
    src = ParquetSource(parquet)
    header, rows, trunc = run_pushdown(
        src.conn_url, src.sql, {},
        {"groupBy": ["region"], "measures": [{"fn": "count"}]}, 1000,
    )
    assert header == ["region", "count"]
    assert {r[0] for r in rows} == {"r0", "r1", "r2"}
    assert sum(r[1] for r in rows) == 300
    assert trunc is False


def test_s3_config_rides_the_url():
    url = build_duckdb_url(s3={"region": "us-east-1", "secret_access_key": "SECR&T/1"})
    assert url.startswith("duckdb:///:memory:?")
    assert "s3_region=us-east-1" in url
    # the secret is URL-encoded (not raw)
    assert "SECR&T/1" not in url
    assert "s3_secret_access_key=" in url


def test_list_source_globs_union():
    sql = build_read_parquet_sql(["a.parquet", "b.parquet"])
    assert sql == "SELECT * FROM read_parquet(['a.parquet', 'b.parquet'])"


def test_package_import_does_not_pull_duckdb():
    # importing cx_connectors must not import duckdb at package-import time.
    code = (
        "import sys, cx_connectors; "
        "assert 'duckdb' not in sys.modules, 'duckdb imported eagerly'; "
        "print('ok')"
    )
    out = subprocess.run([sys.executable, "-I", "-c", code],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert "ok" in out.stdout
