"""kind:"parquet" registration through the BYO web app.

A Parquet path/URL registered as kind:"parquet" is stored as a plain ``sql`` source
(a duckdb:/// conn_url + a read_parquet SELECT), so /api/data, /api/columns and the
pushdown _q path all serve it with no dispatch changes. Needs the [duckdb] extra.
"""

import json

import pytest

from cx_connectors.store import Store, generate_key

pytest.importorskip("duckdb")
pytest.importorskip("duckdb_engine")
pytest.importorskip("fastapi")


@pytest.fixture(scope="module")
def parquet(tmp_path_factory):
    import duckdb

    path = str(tmp_path_factory.mktemp("pq") / "orders.parquet")
    duckdb.sql(
        "COPY (SELECT 'r' || (i % 3) AS region, 'p' || (i % 4) AS product, "
        "(i % 7) * 1.0 AS amount FROM range(300) t(i)) "
        "TO '" + path + "' (FORMAT PARQUET)"
    )
    return path


def _client(tmp_path):
    from fastapi.testclient import TestClient

    from cx_connectors.web.byo_app import create_byo_app

    store = Store(str(tmp_path / "app.db"), generate_key())
    store.create_user("alice", "secret1")
    app = create_byo_app(store=store, session_secret="test",
                         encryption_key=generate_key(), serve_static=False)
    client = TestClient(app)
    assert client.post("/auth/login",
                       json={"username": "alice", "password": "secret1"}).status_code == 200
    return client, store


def test_register_parquet_stores_as_sql(tmp_path, parquet):
    client, store = _client(tmp_path)
    r = client.post("/api/sources",
                    json={"name": "orders", "kind": "parquet", "source": parquet})
    assert r.status_code == 200, r.text
    assert "orders" in r.json()["sources"]
    rec = store.get_source("alice", "orders")
    assert rec["kind"] == "sql"                       # stored as a plain SQL source
    assert rec["conn_url"].startswith("duckdb:///")
    assert "read_parquet(" in rec["sql"]


def test_register_parquet_requires_source(tmp_path):
    client, _ = _client(tmp_path)
    r = client.post("/api/sources", json={"name": "bad", "kind": "parquet", "source": ""})
    assert r.status_code == 400


def test_register_parquet_rejects_injection(tmp_path):
    client, _ = _client(tmp_path)
    r = client.post("/api/sources",
                    json={"name": "bad", "kind": "parquet", "source": "x') ; DROP TABLE t --"})
    assert r.status_code == 400


def test_parquet_data_and_pushdown_and_columns(tmp_path, parquet):
    client, _ = _client(tmp_path)
    client.post("/api/sources", json={"name": "orders", "kind": "parquet", "source": parquet})

    # (1) /api/data returns a CanvasXpress object.
    r = client.get("/api/data", params={"source": "orders"})
    assert r.status_code == 200, r.text
    assert "y" in r.json()

    # (2) pushdown through the stored source: aggregated rows + X-Cx-Rows header.
    r = client.get("/api/data", params={
        "source": "orders",
        "_q": json.dumps({"groupBy": ["region"], "measures": [{"fn": "count"}]}),
    })
    assert r.status_code == 200, r.text
    assert "X-Cx-Rows" in r.headers
    smps = r.json()["y"]["smps"]
    assert set(smps) == {"r0", "r1", "r2"}            # 3 groups, not 300 rows

    # (3) /api/columns returns the file's columns.
    r = client.get("/api/columns", params={"source": "orders"})
    assert r.status_code == 200, r.text
    cols = r.json()
    cols = cols.get("columns", cols) if isinstance(cols, dict) else cols
    assert "region" in json.dumps(cols)


def test_s3_secret_never_appears_in_listing(tmp_path):
    client, store = _client(tmp_path)
    r = client.post("/api/sources", json={
        "name": "s3src", "kind": "parquet", "source": "s3://bucket/o.parquet",
        "s3": {"region": "us-east-1", "secret_access_key": "TOPSECRET123"},
    })
    assert r.status_code == 200, r.text
    # /api/sources returns names only — no conn_url, so no secret.
    assert json.dumps(r.json()["sources"]) .find("TOPSECRET123") == -1
    # And when the stored conn_url is masked for display, the secret is hidden.
    from cx_connectors.connmask import mask_conn_url
    rec = store.get_source("alice", "s3src")
    assert "TOPSECRET123" not in mask_conn_url(rec["conn_url"])
    assert "********" in mask_conn_url(rec["conn_url"])
