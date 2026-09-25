"""Tests for SqlStore, SqlTokenStore, open_store, and open_token_store."""

import os

import pytest

from cx_connectors.sqlstore import (
    SqlStore,
    SqlTokenStore,
    normalize_sql_url,
    open_store,
    open_token_store,
)
from cx_connectors.store import Store, TokenStore, generate_key

_PG_URL = os.getenv("CXC_TEST_PG_URL")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sql_store(tmp_path, key=None):
    return SqlStore("sqlite:///" + str(tmp_path / "test.db"), key or generate_key())


def _pg_store(tmp_path, key=None):
    import sqlalchemy as sa

    engine = sa.create_engine(_PG_URL, future=True)
    with engine.begin() as conn:
        for tbl in ("cxc_sources", "cxc_users"):
            conn.execute(sa.text(f"DROP TABLE IF EXISTS {tbl}"))
    return SqlStore(_PG_URL, key or generate_key(), engine=engine)


def _sql_token_store(tmp_path, key=None):
    return SqlTokenStore("sqlite:///" + str(tmp_path / "tokens.db"), key or generate_key())


def _pg_token_store(tmp_path, key=None):
    import sqlalchemy as sa

    engine = sa.create_engine(_PG_URL, future=True)
    with engine.begin() as conn:
        conn.execute(sa.text("DROP TABLE IF EXISTS cxc_tokens"))
    return SqlTokenStore(_PG_URL, key or generate_key(), engine=engine)


_STORE_PARAMS = [(_sql_store, "sql")]
if _PG_URL:
    _STORE_PARAMS.append((_pg_store, "pg"))

_TOKEN_PARAMS = [(_sql_token_store, "sql")]
if _PG_URL:
    _TOKEN_PARAMS.append((_pg_token_store, "pg"))


@pytest.fixture(params=[p[0] for p in _STORE_PARAMS], ids=[p[1] for p in _STORE_PARAMS])
def store(request, tmp_path):
    return request.param(tmp_path)


@pytest.fixture(params=[p[0] for p in _TOKEN_PARAMS], ids=[p[1] for p in _TOKEN_PARAMS])
def token_store(request, tmp_path):
    return request.param(tmp_path)


# ---------------------------------------------------------------------------
# normalize_sql_url
# ---------------------------------------------------------------------------

def test_normalize_sql_url_rewrites_postgres_prefix():
    assert normalize_sql_url("postgres://u:p@h/db") == "postgresql://u:p@h/db"


def test_normalize_sql_url_leaves_postgresql_unchanged():
    assert normalize_sql_url("postgresql://u:p@h/db") == "postgresql://u:p@h/db"


def test_normalize_sql_url_leaves_sqlite_unchanged():
    assert normalize_sql_url("sqlite:///local.db") == "sqlite:///local.db"


# ---------------------------------------------------------------------------
# SqlStore — users
# ---------------------------------------------------------------------------

def test_create_and_check_user(store):
    assert store.create_user("alice", "secret1")
    assert store.check_user("alice", "secret1")
    assert not store.check_user("alice", "wrong")


def test_create_user_duplicate_returns_false(store):
    assert store.create_user("alice", "secret1")
    assert not store.create_user("alice", "other")


def test_check_user_nonexistent_returns_false(store):
    assert not store.check_user("nobody", "pw")


# ---------------------------------------------------------------------------
# SqlStore — sources
# ---------------------------------------------------------------------------

def test_save_and_get_source(store):
    store.create_user("alice", "pw")
    store.save_source("alice", "s1", "sqlite:///data.db", "SELECT 1")
    rec = store.get_source("alice", "s1")
    assert rec["conn_url"] == "sqlite:///data.db"
    assert rec["sql"] == "SELECT 1"
    assert rec["kind"] == "sql"
    assert rec["config"] is None


def test_get_source_absent_returns_none(store):
    assert store.get_source("alice", "missing") is None


def test_delete_source(store):
    store.save_source("alice", "s1", "sqlite:///d.db", "SELECT 1")
    store.delete_source("alice", "s1")
    assert store.get_source("alice", "s1") is None


def test_delete_source_absent_is_noop(store):
    store.delete_source("alice", "ghost")  # must not raise


def test_list_sources(store):
    store.save_source("alice", "b_src", "sqlite:///d.db", "SELECT 1")
    store.save_source("alice", "a_src", "sqlite:///d.db", "SELECT 2")
    # ORDER BY name ascending
    names = store.list_sources("alice")
    assert sorted(names) == names
    assert set(names) == {"a_src", "b_src"}


def test_list_sources_meta(store):
    store.save_source("alice", "s1", "sqlite:///d.db", "SELECT 1")
    meta = store.list_sources_meta("alice")
    assert len(meta) == 1
    assert meta[0]["name"] == "s1"
    assert meta[0]["updated_at"]


def test_list_sources_empty_for_unknown_user(store):
    assert store.list_sources("nobody") == []


def test_owner_isolation(store):
    store.save_source("alice", "s", "sqlite:///a.db", "SELECT 1")
    assert store.list_sources("bob") == []
    assert store.get_source("bob", "s") is None


def test_owner_cannot_delete_other_users_source(store):
    store.save_source("alice", "s", "sqlite:///a.db", "SELECT 1")
    store.delete_source("bob", "s")  # must be a no-op
    assert store.get_source("alice", "s") is not None


def test_save_source_upserts_on_conflict(store):
    store.save_source("alice", "s1", "sqlite:///v1.db", "SELECT 1")
    store.save_source("alice", "s1", "sqlite:///v2.db", "SELECT 2", kind="packed",
                      config={"x": 1})
    rec = store.get_source("alice", "s1")
    assert rec["conn_url"] == "sqlite:///v2.db"
    assert rec["sql"] == "SELECT 2"
    assert rec["kind"] == "packed"
    assert rec["config"] == {"x": 1}
    assert len(store.list_sources("alice")) == 1


def test_resave_updates_updated_at(store):
    store.save_source("alice", "s1", "sqlite:///d.db", "SELECT 1")
    t1 = store.list_sources_meta("alice")[0]["updated_at"]
    import time
    time.sleep(1.05)
    store.save_source("alice", "s1", "sqlite:///d.db", "SELECT 1")
    t2 = store.list_sources_meta("alice")[0]["updated_at"]
    assert t2 > t1


def test_conn_url_not_stored_in_plaintext(store):
    import sqlalchemy as sa

    store.save_source("alice", "s1", "sqlite:///secret_path.db", "SELECT 1")
    with store._engine.connect() as conn:
        row = conn.execute(
            sa.text("SELECT conn_enc FROM cxc_sources WHERE username='alice' AND name='s1'")
        ).first()
    raw = bytes(row[0]) if isinstance(row[0], memoryview) else row[0]
    assert b"secret_path" not in raw
    assert store.get_source("alice", "s1")["conn_url"] == "sqlite:///secret_path.db"


def test_two_instances_see_each_others_writes(tmp_path):
    key = generate_key()
    url = "sqlite:///" + str(tmp_path / "shared.db")
    store_a = SqlStore(url, key)
    store_b = SqlStore(url, key)
    store_a.save_source("alice", "s1", "sqlite:///d.db", "SELECT 1")
    assert store_b.get_source("alice", "s1") is not None


def test_back_to_back_upsert_from_two_instances_leaves_one_row(tmp_path):
    key = generate_key()
    url = "sqlite:///" + str(tmp_path / "shared.db")
    store_a = SqlStore(url, key)
    store_b = SqlStore(url, key)
    store_a.save_source("alice", "s1", "sqlite:///v1.db", "SELECT 1")
    store_b.save_source("alice", "s1", "sqlite:///v2.db", "SELECT 2")
    assert len(store_a.list_sources("alice")) == 1
    assert store_a.get_source("alice", "s1")["conn_url"] == "sqlite:///v2.db"


# ---------------------------------------------------------------------------
# SqlTokenStore
# ---------------------------------------------------------------------------

def test_token_save_and_load(token_store):
    token_store.save("uid-1", "refresh-secret", "https://t.example/token",
                     "cid", "csec", ["scope-a", "scope-b"], email="u@example.com")
    rec = token_store.load("uid-1")
    assert rec["refresh_token"] == "refresh-secret"
    assert rec["token_uri"] == "https://t.example/token"
    assert rec["client_id"] == "cid"
    assert rec["scopes"] == ["scope-a", "scope-b"]
    assert rec["email"] == "u@example.com"


def test_token_load_absent_returns_none(token_store):
    assert token_store.load("nobody") is None


def test_token_delete(token_store):
    token_store.save("uid-1", "r", "u", "c", "s", ["x"])
    token_store.delete("uid-1")
    assert token_store.load("uid-1") is None


def test_token_isolation(token_store):
    token_store.save("uid-1", "r", "u", "c", "s", ["x"])
    assert token_store.load("uid-2") is None


def test_token_upsert(token_store):
    token_store.save("uid-1", "old-refresh", "u", "c", "s", ["x"])
    token_store.save("uid-1", "new-refresh", "u2", "c2", "s2", ["y"])
    rec = token_store.load("uid-1")
    assert rec["refresh_token"] == "new-refresh"
    assert rec["scopes"] == ["y"]


def test_token_not_stored_in_plaintext(token_store):
    import sqlalchemy as sa

    token_store.save("uid-1", "1//secret-refresh", "u", "c", "s", ["x"])
    with token_store._engine.connect() as conn:
        row = conn.execute(
            sa.text("SELECT refresh_enc FROM cxc_tokens WHERE user_id='uid-1'")
        ).first()
    raw = bytes(row[0]) if isinstance(row[0], memoryview) else row[0]
    assert b"secret-refresh" not in raw


# ---------------------------------------------------------------------------
# open_store / open_token_store — scheme dispatch
# ---------------------------------------------------------------------------

def test_open_store_bare_path_returns_stdlib_store(tmp_path):
    s = open_store(str(tmp_path / "app.db"), generate_key())
    assert isinstance(s, Store)


def test_open_store_sqlite_url_returns_sql_store(tmp_path):
    s = open_store("sqlite:///" + str(tmp_path / "app.db"), generate_key())
    assert isinstance(s, SqlStore)


def test_open_store_postgresql_url_returns_sql_store(tmp_path):
    s = open_store("sqlite:///" + str(tmp_path / "pg.db"), generate_key())
    assert isinstance(s, SqlStore)


def test_open_token_store_bare_path_returns_stdlib_store(tmp_path):
    s = open_token_store(str(tmp_path / "tok.db"), generate_key())
    assert isinstance(s, TokenStore)


def test_open_token_store_sqlite_url_returns_sql_token_store(tmp_path):
    s = open_token_store("sqlite:///" + str(tmp_path / "tok.db"), generate_key())
    assert isinstance(s, SqlTokenStore)


@pytest.mark.skipif(not _PG_URL, reason="CXC_TEST_PG_URL not set")
def test_open_store_postgres_url_returns_sql_store():
    s = open_store(_PG_URL, generate_key())
    assert isinstance(s, SqlStore)


@pytest.mark.skipif(not _PG_URL, reason="CXC_TEST_PG_URL not set")
def test_open_token_store_postgres_url_returns_sql_token_store():
    s = open_token_store(_PG_URL, generate_key())
    assert isinstance(s, SqlTokenStore)
