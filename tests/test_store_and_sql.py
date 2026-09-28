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


def _drop_pg_tables():
    # The SQL store uses fixed table names, so drop them first for per-test
    # isolation on the shared server DB; create_all rebuilds.
    import sqlalchemy as sa

    engine = sa.create_engine(_PG_URL, future=True)
    with engine.begin() as conn:
        for tbl in ("cxc_sources", "cxc_users"):
            conn.execute(sa.text(f"DROP TABLE IF EXISTS {tbl}"))
    return engine


def _pg_store(tmp_path):
    return SqlStore(_PG_URL, generate_key(), engine=_drop_pg_tables())


_PARAMS = [(_stdlib_store, "stdlib"), (_sql_store, "sql")]
if _PG_URL:
    _PARAMS.append((_pg_store, "pg"))


@pytest.fixture(params=[p[0] for p in _PARAMS], ids=[p[1] for p in _PARAMS])
def store(request, tmp_path):
    return request.param(tmp_path)


# Openers return a zero-arg factory for fresh, independent store instances on one
# shared location + key: each SqlStore builds its own engine, so two instances stand
# in for two processes sharing the database.
def _stdlib_opener(tmp_path):
    path, key = str(tmp_path / "shared.db"), generate_key()
    return lambda: Store(path, key)


def _sql_opener(tmp_path):
    url, key = "sqlite:///" + str(tmp_path / "shared.db"), generate_key()
    return lambda: SqlStore(url, key)


def _pg_opener(tmp_path):
    _drop_pg_tables().dispose()
    key = generate_key()
    return lambda: SqlStore(_PG_URL, key)


_OPENERS = [(_stdlib_opener, "stdlib"), (_sql_opener, "sql")]
if _PG_URL:
    _OPENERS.append((_pg_opener, "pg"))


@pytest.fixture(params=[p[0] for p in _OPENERS], ids=[p[1] for p in _OPENERS])
def open_shared(request, tmp_path):
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


def test_two_instances_see_each_others_writes(open_shared):
    """Two store instances on the same location see each other's writes."""
    a, b = open_shared(), open_shared()
    a.create_user("alice", "secret1")
    a.save_source("alice", "s1", "sqlite:///d.db", "SELECT 1")
    assert b.check_user("alice", "secret1")
    assert b.list_sources("alice") == ["s1"]
    assert b.get_source("alice", "s1")["conn_url"] == "sqlite:///d.db"


def test_back_to_back_upsert_leaves_one_row(open_shared):
    """The same (username, name) saved from two instances ends with one row."""
    a, b = open_shared(), open_shared()
    a.save_source("alice", "s1", "sqlite:///v1.db", "SELECT 1")
    b.save_source("alice", "s1", "sqlite:///v2.db", "SELECT 2")
    assert a.list_sources("alice") == ["s1"]
    assert a.get_source("alice", "s1")["conn_url"] == "sqlite:///v2.db"
    assert a.get_source("alice", "s1")["sql"] == "SELECT 2"


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


# ---------------------------------------------------------------------------
# Web app with APP_DB_PATH as a database URL
# ---------------------------------------------------------------------------

def _sqlite_url(tmp_path):
    return "sqlite:///" + str(tmp_path / "app.db")


def _pg_url(tmp_path):
    _drop_pg_tables().dispose()
    return _PG_URL


_URLS = [(_sqlite_url, "sql")]
if _PG_URL:
    _URLS.append((_pg_url, "pg"))


@pytest.mark.parametrize("make_url", [u[0] for u in _URLS], ids=[u[1] for u in _URLS])
def test_byo_app_db_path_url(tmp_path, make_url):
    """A database URL in db_path (APP_DB_PATH) backs the app with the SQL store: a
    signup + source registered through the API survive into a second app instance."""
    from fastapi.testclient import TestClient

    from cx_connectors.web.byo_app import create_byo_app

    data_db = str(tmp_path / "data.db")
    conn = sqlite3.connect(data_db)
    conn.execute("CREATE TABLE t (sample TEXT, v INT)")
    conn.executemany("INSERT INTO t VALUES (?,?)", [("s1", 10), ("s2", 20)])
    conn.commit()
    conn.close()

    url, key = make_url(tmp_path), generate_key()

    def new_app():
        return create_byo_app(db_path=url, session_secret="test", encryption_key=key,
                              allow_signup=True, serve_static=False)

    first = TestClient(new_app())
    r = first.post("/auth/signup", json={"username": "alice", "password": "secret1"})
    assert r.status_code == 200, r.text
    r = first.post("/api/sources", json={"name": "ds", "conn_url": "sqlite:///" + data_db,
                                         "sql": "SELECT sample, v FROM t ORDER BY sample"})
    assert r.status_code == 200, r.text

    # A second app on the same URL (another process, or the same one after a restart).
    second = TestClient(new_app())
    r = second.post("/auth/login", json={"username": "alice", "password": "secret1"})
    assert r.status_code == 200, r.text
    cx = second.get("/api/data", params={"source": "ds"}).json()
    assert cx["y"]["smps"] == ["s1", "s2"]
    assert cx["y"]["vars"] == ["v"]
    # And the rows live in the SQL store's cxc_* tables, not in a stray SQLite file.
    assert SqlStore(url, key).list_sources("alice") == ["ds"]
