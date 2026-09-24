"""Pushdown queries: aggregation, filters, order and limits run in the database,
around the owner's SELECT, with nothing from the request becoming SQL text."""

import json
import sqlite3
import time

import pytest

sa = pytest.importorskip("sqlalchemy")

from cx_connectors.pushdown import (  # noqa: E402
    PushdownError,
    build_statement,
    parse_query,
    run_pushdown,
)

ROWS = [("o1", "EMEA", "won", 100.0), ("o2", "EMEA", "open", 50.0),
        ("o3", "APAC", "won", 70.0), ("o4", "APAC", "lost", 30.0),
        ("o5", "AMER", "won", 200.0), ("o6", "AMER", None, 10.0)]
SQL = "SELECT id, region, status, amount FROM orders WHERE (:minimum IS NULL OR amount >= :minimum)"


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "orders.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE orders (id TEXT, region TEXT, status TEXT, amount REAL)")
    conn.executemany("INSERT INTO orders VALUES (?,?,?,?)", ROWS)
    conn.commit()
    conn.close()
    return "sqlite:///" + path


def run(db, query, params=None, cap=100_000):
    return run_pushdown(db, SQL, dict({"minimum": None}, **(params or {})), query, cap)


def test_group_by_with_measures_order_and_limit(db):
    header, rows, truncated = run(db, {
        "groupBy": ["region"],
        "measures": [{"fn": "sum", "column": "amount"}, {"fn": "count"},
                     {"fn": "count_distinct", "column": "status", "as": "statuses"}],
        "orderBy": [{"column": "sum_amount", "desc": True}], "limit": 2})
    assert header == ["region", "sum_amount", "count", "statuses"]
    assert rows == [["AMER", 210.0, 2, 1], ["EMEA", 150.0, 2, 2]]
    assert truncated is True                       # a third region exists


def test_filters_of_every_kind(db):
    def ids(where):
        return [r[0] for r in run(db, {"columns": ["id"], "where": where,
                                       "orderBy": ["id"]})[1]]
    assert ids([{"column": "region", "op": "=", "value": "EMEA"}]) == ["o1", "o2"]
    assert ids([{"column": "region", "op": "!=", "value": "EMEA"}]) == ["o3", "o4", "o5", "o6"]
    assert ids([{"column": "amount", "op": ">", "value": 70}]) == ["o1", "o5"]
    assert ids([{"column": "amount", "op": "<=", "value": 30}]) == ["o4", "o6"]
    assert ids([{"column": "status", "op": "in", "value": ["won", "lost"]}]) == \
        ["o1", "o3", "o4", "o5"]
    assert ids([{"column": "status", "op": "not_in", "value": ["won"]}]) == ["o2", "o4"]
    assert ids([{"column": "status", "op": "in", "value": []}]) == []
    assert ids([{"column": "amount", "op": "between", "value": [30, 70]}]) == ["o2", "o3", "o4"]
    assert ids([{"column": "status", "op": "is_null"}]) == ["o6"]
    assert ids([{"column": "status", "op": "=", "value": None}]) == ["o6"]
    assert ids([{"column": "status", "op": "not_null"},
                {"column": "region", "op": "=", "value": "AMER"}]) == ["o5"]


def test_totals_without_group_by_and_owner_binds_still_apply(db):
    header, rows, _ = run(db, {"measures": [{"fn": "avg", "column": "amount"},
                                            {"fn": "max", "column": "amount"}]},
                          {"minimum": 50})
    assert header == ["group", "avg_amount", "max_amount"]
    assert rows == [["all", 105.0, 200.0]]         # o1 100, o2 50, o3 70, o5 200


def test_row_cap_marks_truncation(db):
    header, rows, truncated = run(db, {}, cap=4)
    assert len(rows) == 4 and truncated is True
    assert run(db, {"limit": 6})[2] is False


def test_nothing_from_the_request_becomes_sql_text(db):
    # An injected column name is just an unknown column.
    with pytest.raises(PushdownError, match="No column named"):
        run(db, {"groupBy": ["region; DROP TABLE orders"]})
    # A hostile value is a bound parameter: it matches nothing and runs harmlessly.
    evil = "EMEA' OR '1'='1"
    assert run(db, {"where": [{"column": "region", "op": "=", "value": evil}]})[1] == []
    stmt, _ = build_statement(SQL, ["id", "region", "status", "amount"],
                              parse_query({"where": [{"column": "region", "value": evil}]}),
                              100)
    from sqlalchemy.dialects import postgresql
    assert evil not in str(stmt.compile(dialect=postgresql.dialect()))
    assert len(run(db, {})[1]) == 6                # the table is still there


def test_parse_query_rejects_bad_shapes():
    for bad in ("not json", [], {"groupBy": "region"}, {"measures": [{"fn": "median",
                                                                       "column": "x"}]},
                {"measures": [{"fn": "sum"}]}, {"where": [{"column": "x", "op": "like",
                                                            "value": "a%"}]},
                {"where": [{"column": "x", "op": "in", "value": "a"}]},
                {"where": [{"column": "x", "op": "between", "value": [1]}]},
                {"where": [{"column": "x", "value": {"nested": 1}}]},
                {"limit": 0}, {"limit": True}, {"limit": 10 ** 9},
                {"columns": ["a"], "groupBy": ["b"]}, {"select": ["a"]}):
        with pytest.raises(PushdownError):
            parse_query(bad)
    assert parse_query('{"measures": [{"fn": "mean", "column": "x"}]}')["measures"] == \
        [{"fn": "avg", "column": "x", "as": "avg_x"}]


def test_order_by_must_be_an_output(db):
    with pytest.raises(PushdownError, match="not an output"):
        run(db, {"groupBy": ["region"], "orderBy": ["amount"]})


def _client(tmp_path, db):
    from fastapi.testclient import TestClient

    from cx_connectors.store import Store, generate_key
    from cx_connectors.web.byo_app import create_byo_app
    store = Store(str(tmp_path / "app.db"), generate_key())
    store.create_user("alice", "secret1")
    store.save_source("alice", "orders", db, SQL)
    store.save_source("alice", "crm", '{"x": 1}', "SELECT Id FROM Account", kind="salesforce")
    client = TestClient(create_byo_app(store=store, session_secret="t",
                                       encryption_key=generate_key(), serve_static=False))
    assert client.post("/auth/login", json={"username": "alice", "password": "secret1"}
                       ).status_code == 200
    return client


def test_data_endpoint_pushdown_and_columns(tmp_path, db):
    client = _client(tmp_path, db)
    q = {"groupBy": ["region"], "measures": [{"fn": "sum", "column": "amount"}],
         "orderBy": ["region"]}
    r = client.get("/api/data", params={"source": "orders", "_q": json.dumps(q),
                                        "minimum": 50})
    assert r.status_code == 200, r.text
    assert r.json() == {"y": {"vars": ["sum_amount"], "smps": ["AMER", "APAC", "EMEA"],
                              "data": [[200.0, 70.0, 150.0]]},
                        "x": {"region": ["AMER", "APAC", "EMEA"]}}
    two = {"groupBy": ["region", "status"], "measures": [{"fn": "count"}],
           "where": [{"column": "status", "op": "not_null"}], "orderBy": ["region", "status"]}
    cx = client.get("/api/data", params={"source": "orders", "_q": json.dumps(two)}).json()
    assert cx["y"]["smps"][:2] == ["AMER · won", "APAC · lost"]
    assert cx["x"]["status"][:2] == ["won", "lost"]
    assert r.headers["X-Cx-Rows"] == "3" and r.headers["X-Cx-Truncated"] == "0"
    # a filter that matches nothing is an empty object, not an error
    none = {"groupBy": ["region"], "measures": [{"fn": "count"}],
            "where": [{"column": "region", "value": "MARS"}]}
    empty = client.get("/api/data", params={"source": "orders", "_q": json.dumps(none)})
    assert empty.json() == {"y": {"vars": ["count"], "smps": [], "data": [[]]}}
    bad = client.get("/api/data", params={"source": "orders", "_q": '{"groupBy": ["nope"]}'})
    assert bad.status_code == 400 and "No column named" in bad.json()["detail"]
    cols = client.get("/api/columns", params={"source": "orders"}).json()["columns"]
    assert cols == [{"name": "id", "type": "text"}, {"name": "region", "type": "text"},
                    {"name": "status", "type": "text"}, {"name": "amount", "type": "number"}]
    assert client.get("/api/columns", params={"source": "crm"}).status_code == 400


def test_duckdb_parquet_pushdown_at_scale(tmp_path):
    duckdb = pytest.importorskip("duckdb")
    pytest.importorskip("duckdb_engine")
    path = str(tmp_path / "events.parquet")
    duckdb.sql("COPY (SELECT i AS id, 'site' || (i % 50) AS site, (i % 997) * 1.0 AS value "
               "FROM range(2000000) t(i)) TO '" + path + "' (FORMAT PARQUET)")
    sql = "SELECT id, site, value FROM read_parquet('" + path + "')"
    start = time.time()
    header, rows, truncated = run_pushdown(
        "duckdb:///:memory:", sql, None,
        {"groupBy": ["site"], "measures": [{"fn": "avg", "column": "value"}, {"fn": "count"}],
         "where": [{"column": "value", "op": ">=", "value": 500}], "orderBy": ["site"]})
    elapsed = time.time() - start
    assert header == ["site", "avg_value", "count"] and len(rows) == 50 and not truncated
    assert sum(r[2] for r in rows) == sum(1 for i in range(2000000) if i % 997 >= 500)
    assert elapsed < 10          # 2M rows scanned in the database; 50 rows travel


# ---- joins in the database -------------------------------------------------------
from cx_connectors.pushdown import run_join  # noqa: E402

LEFT_SQL = "SELECT id, cust, amount FROM orders2"
RIGHT_SQL = "SELECT cust, segment, amount FROM customers"


@pytest.fixture
def jdb(tmp_path):
    path = str(tmp_path / "j.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE orders2 (id TEXT, cust TEXT, amount REAL)")
    conn.executemany("INSERT INTO orders2 VALUES (?,?,?)",
                     [("o1", "c1", 10), ("o2", "c1", 20), ("o3", "c2", 5), ("o4", "c9", 1)])
    conn.execute("CREATE TABLE customers (cust TEXT, segment TEXT, amount REAL)")
    conn.executemany("INSERT INTO customers VALUES (?,?,?)",
                     [("c1", "retail", 1000), ("c2", "pharma", 2000), ("c3", "retail", 3000)])
    conn.commit()
    conn.close()
    return "sqlite:///" + path


def test_join_names_columns_like_the_dashboards_join(jdb):
    header, rows, _ = run_join((jdb, LEFT_SQL, None), (jdb, RIGHT_SQL, None), "cust",
                               "inner", "customers", {"orderBy": ["id"]})
    assert header == ["id", "cust", "amount", "segment", "amount.customers"]
    assert rows == [["o1", "c1", 10.0, "retail", 1000.0], ["o2", "c1", 20.0, "retail", 1000.0],
                    ["o3", "c2", 5.0, "pharma", 2000.0]]
    left = run_join((jdb, LEFT_SQL, None), (jdb, RIGHT_SQL, None),
                    [{"left": "cust", "right": "cust"}], "left", "c", {"orderBy": ["id"]})[1]
    assert left[-1] == ["o4", "c9", 1.0, None, None]            # unmatched left row kept


def test_outer_join_keeps_right_only_keys_and_smps_keys(jdb):
    rows = run_join((jdb, LEFT_SQL, None), (jdb, RIGHT_SQL, None), "cust", "outer", "c",
                    {"orderBy": ["cust"]})[1]
    assert [r[1] for r in rows] == ["c1", "c1", "c2", "c3", "c9"]
    assert [r for r in rows if r[1] == "c3"][0][0] == "c3"       # id falls back to the right's
    # smps = each side's first column (id vs cust: nothing matches)
    assert run_join((jdb, LEFT_SQL, None), (jdb, RIGHT_SQL, None), "smps", "inner")[1] == []


def test_aggregate_over_a_join_in_the_database(jdb):
    header, rows, _ = run_join(
        (jdb, LEFT_SQL, None), (jdb, RIGHT_SQL, None), "cust", "inner", "customers",
        {"groupBy": ["segment"], "measures": [{"fn": "sum", "column": "amount"},
                                             {"fn": "count"}], "orderBy": ["segment"]})
    assert header == ["segment", "sum_amount", "count"]
    assert rows == [["pharma", 5.0, 1], ["retail", 30.0, 2]]


def test_join_refusals(jdb, tmp_path):
    other = "sqlite:///" + str(tmp_path / "other.db")
    with pytest.raises(PushdownError, match="different databases"):
        run_join((jdb, LEFT_SQL, None), (other, RIGHT_SQL, None), "cust")
    with pytest.raises(PushdownError, match="no column"):
        run_join((jdb, LEFT_SQL, None), (jdb, RIGHT_SQL, None), "nope")
    with pytest.raises(PushdownError, match="how must be"):
        run_join((jdb, LEFT_SQL, None), (jdb, RIGHT_SQL, None), "cust", "cross")


def test_join_endpoint(tmp_path, jdb):
    from fastapi.testclient import TestClient

    from cx_connectors.store import Store, generate_key
    from cx_connectors.web.byo_app import create_byo_app
    store = Store(str(tmp_path / "app.db"), generate_key())
    store.create_user("alice", "secret1")
    store.save_source("alice", "orders", jdb, LEFT_SQL)
    store.save_source("alice", "customers", jdb, RIGHT_SQL)
    client = TestClient(create_byo_app(store=store, session_secret="t",
                                       encryption_key=generate_key(), serve_static=False))
    client.post("/auth/login", json={"username": "alice", "password": "secret1"})
    r = client.get("/api/join", params={
        "left": "orders", "right": "customers", "on": '"cust"',
        "_q": json.dumps({"groupBy": ["segment"], "measures": [{"fn": "sum", "column": "amount"}],
                          "orderBy": ["segment"]})})
    assert r.status_code == 200, r.text
    assert r.json()["y"] == {"vars": ["sum_amount"], "smps": ["pharma", "retail"],
                             "data": [[5.0, 30.0]]}
    assert client.get("/api/join", params={"left": "orders", "right": "nope", "on": "cust"}
                      ).status_code == 404
