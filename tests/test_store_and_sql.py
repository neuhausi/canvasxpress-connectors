import sqlite3

import pytest

from cx_connectors.sources.base import to_cx
from cx_connectors.sources.sql import ReadOnlyViolation, SqlSource
from cx_connectors.store import Store, generate_key


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "app.db"), generate_key())


def test_password_roundtrip_and_wrong_password(store):
    assert store.create_user("alice", "secret1")
    assert store.check_user("alice", "secret1")
    assert not store.check_user("alice", "nope")
    assert not store.create_user("alice", "again")  # duplicate username


def test_connection_string_encrypted_at_rest(tmp_path):
    db = str(tmp_path / "app.db")
    store = Store(db, generate_key())
    store.create_user("bob", "secret1")
    store.save_source("bob", "s1", "sqlite:///secret_path.db", "SELECT 1")
    # The raw bytes on disk must NOT contain the plaintext connection string.
    blob = sqlite3.connect(db).execute("SELECT conn_enc FROM sources").fetchone()[0]
    assert b"secret_path" not in blob
    assert store.get_source("bob", "s1")["conn_url"] == "sqlite:///secret_path.db"


def test_isolation_between_users(store):
    store.save_source("alice", "s", "sqlite:///a.db", "SELECT 1")
    assert store.list_sources("bob") == []
    assert store.get_source("bob", "s") is None


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
