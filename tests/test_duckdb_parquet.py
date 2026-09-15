"""DuckDB over a Parquet file through the unchanged SqlSource path (the [duckdb] extra).

Proves the three things the README claims: the read-only guard accepts read_parquet(), declared
:name binds are forwarded (and NULL widens), and the result reshapes to a CanvasXpress object.
"""

import pytest

duckdb = pytest.importorskip("duckdb")
pytest.importorskip("duckdb_engine")

from cx_connectors.reshape import rows_to_cx
from cx_connectors.sources.sql import SqlSource, assert_read_only, bind_param_names

SQL = """SELECT sample, avg(expr) AS mean_expr, avg(logfc) AS mean_logfc
           FROM read_parquet(:path)
          WHERE gene = :gene AND (:tissue IS NULL OR tissue = :tissue)
          GROUP BY sample ORDER BY sample"""


@pytest.fixture(scope="module")
def parquet(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("pq") / "expr.parquet")
    duckdb.sql(
        "COPY (SELECT 'g' || (i % 20) AS gene, 's' || ((i // 20) % 5) AS sample, "
        "CASE WHEN i % 2 = 0 THEN 'tumor' ELSE 'normal' END AS tissue, "
        "(i % 7) * 1.0 AS expr, (i % 3) * 0.5 AS logfc FROM range(2000) t(i)) "
        "TO '" + path + "' (FORMAT PARQUET)"
    )
    return path


def test_read_only_guard_accepts_read_parquet():
    assert_read_only(SQL)
    assert bind_param_names(SQL) == ["path", "gene", "tissue"]


def test_parquet_query_binds_and_reshapes(parquet):
    header, rows = SqlSource("duckdb:///:memory:", SQL, {"path": parquet, "gene": "g3", "tissue": None}).read()
    assert header == ["sample", "mean_expr", "mean_logfc"]
    assert [r[0] for r in rows] == ["s0", "s1", "s2", "s3", "s4"]
    cx = rows_to_cx(header, rows)
    assert cx["y"]["vars"] == ["mean_expr", "mean_logfc"]
    assert cx["y"]["smps"] == ["s0", "s1", "s2", "s3", "s4"]
    assert len(cx["y"]["data"][0]) == 5


def test_null_bind_widens_and_value_narrows(parquet):
    wide = SqlSource("duckdb:///:memory:", SQL, {"path": parquet, "gene": "g3", "tissue": None}).read()[1]
    narrow = SqlSource("duckdb:///:memory:", SQL, {"path": parquet, "gene": "g3", "tissue": "tumor"}).read()[1]
    assert len(narrow) <= len(wide)
    # gene g3 = i % 20 == 3 -> i odd -> every row is 'normal', so 'tumor' selects nothing
    assert narrow == []
