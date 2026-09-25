import os
import sqlite3

import pytest

from cx_connectors.sources.base import to_cx
from cx_connectors.sources.sql import ReadOnlyViolation, SqlSource
from cx_connectors.sqlstore import SqlStore
from cx_connectors.store import Store, generate_key

# Set CXC_TEST_PG_URL to also run the store assertions against a real Postgres
# (the durable-deployment proof); unset → the pg param is absent.
_PG_URL = os.getenv("CXC_TEST_PG_URL")


# ---------------------------------------------------------------------------
# Backend factories
# ---------------------------------------------------------------------------

def _stdlib_store(tmp_path):
    return Store(str(tmp_path / "app.db"), generate_key())


def _sql_store(tmp_path):
    return SqlStore("sqlite:///" + str(tmp_path / "sql.db"), generate_key())


def _pg_store(tmp_path):
    import sqlalchemy as sa

    engine = sa.create_engine(_PG_URL, future=True)
    with engine.begin() as conn:
        for tbl in ("cxc_sources", "cxc_users"):
            conn.execute(sa.text(f"DROP TABLE IF EXISTS {tbl}"))
    return SqlStore(_PG_URL, generate_key(), engine=engine)


_PARAMS = [(_stdlib_store, "stdlib"), (_sql_store, "sql")]
if _PG_URL:
    _PARAMS.append((_pg_store, "pg"))


@pytest.fixture(params=[p[0] for p in _PARAMS], ids=[p[1] for p in _PARAMS])
def store(request, tmp_path):
    return request.param(tmp_path)


# ---------------------------------------------------------------------------
# Parity tests — all backends
# ---------------------------------------------------------------------------

def test_password_roundtrip_and_wrong_password(store):
    assert store.create_user("alice", "secret1")
    assert store.check_user("alice", "secret1")
    assert not store.check_user("alice", "nope")
    assert not store.create_user("alice", "again")  # duplicate username


def test_set_password_replaces_an_existing_users_password_only(store):
    store.create_user("alice", "secret1")
    store.save_source("alice", "s", "sqlite:///x.db", "SELECT 1")
    assert store.set_password("alice", "rotated1")
    assert store.check_user("alice", "rotated1")
    assert not store.check_user("alice", "secret1")
    assert store.list_sources("alice") == ["s"]  # the user's sources are untouched
    assert not store.set_password("nobody", "x")  # no such user: nothing is created
    assert not store.check_user("nobody", "x")


def test_isolation_between_users(store):
    store.save_source("alice", "s", "sqlite:///a.db", "SELECT 1")
    assert store.list_sources("bob") == []
    assert store.get_source("bob", "s") is None


def test_delete_source_absent_is_noop(store):
    store.delete_source("alice", "ghost")  # must not raise


def test_owner_cannot_access_or_delete_other_users_source(store):
    store.save_source("alice", "s", "sqlite:///a.db", "SELECT 1")
    assert store.get_source("bob", "s") is None
    assert store.list_sources("bob") == []
    store.delete_source("bob", "s")  # no-op
    assert store.get_source("alice", "s") is not None


def test_resave_updates_kind_sql_config_and_updated_at(store):
    import time

    store.save_source("alice", "s1", "sqlite:///v1.db", "SELECT 1")
    t1 = store.list_sources_meta("alice")[0]["updated_at"]
    time.sleep(1.05)
    store.save_source("alice", "s1", "sqlite:///v2.db", "SELECT 2",
                      kind="packed", config={"x": 1})
    rec = store.get_source("alice", "s1")
    t2 = store.list_sources_meta("alice")[0]["updated_at"]
    assert rec["conn_url"] == "sqlite:///v2.db"
    assert rec["sql"] == "SELECT 2"
    assert rec["kind"] == "packed"
    assert rec["config"] == {"x": 1}
    assert t2 > t1
    assert len(store.list_sources("alice")) == 1


def test_conn_url_not_stored_in_plaintext_sql_backend(tmp_path):
    """SQL-backend equivalent of the SQLite-file ciphertext check."""
    import sqlalchemy as sa

    key = generate_key()
    s = SqlStore("sqlite:///" + str(tmp_path / "app.db"), key)
    s.save_source("bob", "s1", "sqlite:///secret_path.db", "SELECT 1")
    with s._engine.connect() as conn:
        row = conn.execute(
            sa.text("SELECT conn_enc FROM cxc_sources WHERE username='bob' AND name='s1'")
        ).first()
    raw = bytes(row[0]) if isinstance(row[0], memoryview) else row[0]
    assert b"secret_path" not in raw
    assert s.get_source("bob", "s1")["conn_url"] == "sqlite:///secret_path.db"


def test_two_instances_see_each_others_writes(tmp_path):
    """Two store instances on the same URL see each other's writes."""
    key = generate_key()
    url = "sqlite:///" + str(tmp_path / "shared.db")
    a = SqlStore(url, key)
    b = SqlStore(url, key)
    a.save_source("alice", "s1", "sqlite:///d.db", "SELECT 1")
    assert b.get_source("alice", "s1") is not None


def test_back_to_back_upsert_leaves_one_row(tmp_path):
    """The same (username, name) saved from two instances ends with one row."""
    key = generate_key()
    url = "sqlite:///" + str(tmp_path / "shared.db")
    a = SqlStore(url, key)
    b = SqlStore(url, key)
    a.save_source("alice", "s1", "sqlite:///v1.db", "SELECT 1")
    b.save_source("alice", "s1", "sqlite:///v2.db", "SELECT 2")
    assert len(a.list_sources("alice")) == 1
    assert a.get_source("alice", "s1")["conn_url"] == "sqlite:///v2.db"


# ---------------------------------------------------------------------------
# stdlib-only tests (SQLite file-level checks)
# ---------------------------------------------------------------------------

def test_a_duplicate_create_user_does_not_lock_the_database(tmp_path):
    # Regression: the failed INSERT left its transaction open, so the process kept a
    # write lock and any other process opening the store got "database is locked"
    # (a restarted server could not mount the connectors app).
    path = str(tmp_path / "app.db")
    store = Store(path, generate_key())
    store.create_user("alice", "secret1")
    assert not store.create_user("alice", "again")
    assert not store._conn.in_transaction
    other = sqlite3.connect(path, timeout=0.5)
    other.execute("CREATE TABLE probe (x)")   # a write from another connection
    other.commit()
    other.close()
    assert store.check_user("alice", "secret1")


def test_connection_string_encrypted_at_rest(tmp_path):
    db = str(tmp_path / "app.db")
    s = Store(db, generate_key())
    s.create_user("bob", "secret1")
    s.save_source("bob", "s1", "sqlite:///secret_path.db", "SELECT 1")
    # The raw bytes on disk must NOT contain the plaintext connection string.
    blob = sqlite3.connect(db).execute("SELECT conn_enc FROM sources").fetchone()[0]
    assert b"secret_path" not in blob
    assert s.get_source("bob", "s1")["conn_url"] == "sqlite:///secret_path.db"


# ---------------------------------------------------------------------------
# SQL / source tests (not store-backend-specific)
# ---------------------------------------------------------------------------

def test_sql_read_only_guard():
    with pytest.raises(ReadOnlyViolation):
        SqlSource("sqlite://", "DROP TABLE t")
    with pytest.raises(ReadOnlyViolation):
        SqlSource("sqlite://", "SELECT 1; DELETE FROM t")


def test_sql_source_end_to_end(tmp_path):
    path = str(tmp_path / "data.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (sample TEXT, v INT, grp TEXT)")
    conn.executemany("INSERT INTO t VALUES (?,?,?)",
                     [("s1", 10, "X"), ("s2", 20, "Y")])
    conn.commit()
    conn.close()

    cx = to_cx(SqlSource("sqlite:///" + path, "SELECT sample, v, grp FROM t ORDER BY sample"))
    assert cx["y"]["smps"] == ["s1", "s2"]
    assert cx["y"]["vars"] == ["v"]
    assert cx["x"]["grp"] == ["X", "Y"]


def test_bind_param_names_extracts_declared_binds():
    from cx_connectors.sources.sql import bind_param_names
    sql = ("SELECT sample, v FROM t "
           "WHERE (:region IS NULL OR region = :region) AND grp = :grp")
    assert bind_param_names(sql) == ["region", "grp"]     # deduped, in order
    assert bind_param_names("SELECT 1") == []
    # A Postgres ::cast is not a bind parameter.
    assert bind_param_names("SELECT ts::date FROM t") == []


def test_sql_source_binds_params_end_to_end(tmp_path):
    path = str(tmp_path / "p.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (sample TEXT, v INT, region TEXT)")
    conn.executemany("INSERT INTO t VALUES (?,?,?)",
                     [("s1", 10, "EMEA"), ("s2", 20, "APAC"), ("s3", 30, "EMEA")])
    conn.commit()
    conn.close()
    sql = ("SELECT sample, v FROM t "
           "WHERE (:region IS NULL OR region = :region) ORDER BY sample")
    # Bound value narrows...
    hdr, rows = SqlSource("sqlite:///" + path, sql, {"region": "EMEA"}).read()
    assert [r[0] for r in rows] == ["s1", "s3"]
    # ...and NULL widens (the "All" case).
    hdr, rows = SqlSource("sqlite:///" + path, sql, {"region": None}).read()
    assert [r[0] for r in rows] == ["s1", "s2", "s3"]


def _byo_client(tmp_path, db_path):
    from fastapi.testclient import TestClient

    from cx_connectors.store import Store, generate_key
    from cx_connectors.web.byo_app import create_byo_app
    store = Store(str(tmp_path / "app.db"), generate_key())
    store.create_user("alice", "secret1")
    store.save_source(
        "alice", "sales", "sqlite:///" + db_path,
        "SELECT sample, v FROM t WHERE (:region IS NULL OR region = :region) ORDER BY sample",
    )
    app = create_byo_app(store=store, session_secret="test",
                         encryption_key=generate_key(), serve_static=False)
    client = TestClient(app)
    r = client.post("/auth/login", json={"username": "alice", "password": "secret1"})
    assert r.status_code == 200, r.text
    return client


def test_data_endpoint_forwards_declared_params_as_binds(tmp_path):
    db = str(tmp_path / "d.db")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t (sample TEXT, v INT, region TEXT)")
    conn.executemany("INSERT INTO t VALUES (?,?,?)",
                     [("s1", 10, "EMEA"), ("s2", 20, "APAC")])
    conn.commit()
    conn.close()
    client = _byo_client(tmp_path, db)

    # No param -> widened (NULL branch) -> both samples.
    full = client.get("/api/data", params={"source": "sales"}).json()
    assert full["y"]["smps"] == ["s1", "s2"]

    # Declared param -> bound -> narrowed.
    emea = client.get("/api/data", params={"source": "sales", "region": "EMEA"}).json()
    assert emea["y"]["smps"] == ["s1"]

    # An unknown/injected key is ignored (query is unchanged, still narrowed by region).
    inj = client.get("/api/data", params={"source": "sales", "region": "EMEA",
                                          "v": "0 OR 1=1"}).json()
    assert inj["y"]["smps"] == ["s1"]
