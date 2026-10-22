"""Saving a database source whose URL still carries the ``********`` an edit form shows."""

import pytest
from fastapi.testclient import TestClient

import cx_connectors.web.byo_app as byo
from cx_connectors.connmask import MASK
from cx_connectors.store import Store, generate_key

REAL = "postgresql+psycopg://app:s3cr3t-pw@db.example.com:5432/sales"
SQL = "SELECT region, revenue FROM sales"


@pytest.fixture
def world(tmp_path):
    store = Store(str(tmp_path / "app.db"), generate_key())
    store.create_user("alice", "secret1")
    store.create_user("bob", "secret2")
    app = byo.create_byo_app(store=store, session_secret="t", encryption_key=generate_key(), serve_static=False)

    def client_for(user, password):
        client = TestClient(app)
        assert client.post("/auth/login", json={"username": user, "password": password}).status_code == 200
        return client

    return store, client_for("alice", "secret1"), client_for("bob", "secret2")


def _register(client, name, conn_url, **extra):
    return client.post("/api/sources", json=dict({"name": name, "conn_url": conn_url, "sql": SQL}, **extra))


def test_saving_an_untouched_masked_url_keeps_the_stored_password(world):
    store, alice, _ = world
    assert _register(alice, "sales", REAL).status_code == 200
    masked = REAL.replace("s3cr3t-pw", MASK)
    assert _register(alice, "sales", masked, ).status_code == 200
    assert store.get_source("alice", "sales")["conn_url"] == REAL


def test_changing_the_query_or_database_keeps_the_stored_password(world):
    store, alice, _ = world
    _register(alice, "sales", REAL)
    edited = REAL.replace("s3cr3t-pw", MASK).replace("/sales", "/reports")
    assert _register(alice, "sales", edited).status_code == 200
    assert store.get_source("alice", "sales")["conn_url"] == REAL.replace("/sales", "/reports")


def test_a_typed_password_replaces_the_stored_one(world):
    store, alice, _ = world
    _register(alice, "sales", REAL)
    typed = REAL.replace("s3cr3t-pw", "a-new-password")
    assert _register(alice, "sales", typed).status_code == 200
    assert store.get_source("alice", "sales")["conn_url"] == typed


def test_the_stored_password_is_never_sent_to_a_different_server(world):
    store, alice, _ = world
    _register(alice, "sales", REAL)
    response = _register(alice, "sales", REAL.replace("s3cr3t-pw", MASK).replace("db.example.com", "attacker.example"))
    assert response.status_code == 400 and "Re-enter the password" in response.json()["detail"]
    assert store.get_source("alice", "sales")["conn_url"] == REAL          # untouched


def test_copying_a_connection_to_a_new_source_uses_secret_from(world):
    store, alice, _ = world
    _register(alice, "sales", REAL)
    copy = REAL.replace("s3cr3t-pw", MASK).replace("/sales", "/hr")
    assert _register(alice, "hr", copy, secret_from="sales").status_code == 200
    assert store.get_source("alice", "hr")["conn_url"] == REAL.replace("/sales", "/hr")


def test_a_user_can_never_take_another_users_stored_password(world):
    store, alice, bob = world
    _register(alice, "sales", REAL)
    masked = REAL.replace("s3cr3t-pw", MASK)
    for extra in ({}, {"secret_from": "sales"}):                       # same name, or pointing at alice's
        response = _register(bob, "sales", masked, **extra)
        assert response.status_code == 400, response.text
    assert store.get_source("bob", "sales") is None


def test_a_masked_url_with_nothing_stored_is_refused(world):
    store, alice, _ = world
    response = _register(alice, "fresh", REAL.replace("s3cr3t-pw", MASK))
    assert response.status_code == 400
    assert store.get_source("alice", "fresh") is None


def test_secret_from_cannot_name_a_saas_source(world):
    store, alice, _ = world
    alice.post("/api/sources", json={"name": "opps", "kind": "salesforce", "soql": "SELECT Name FROM Opportunity",
                                     "auth": {"username": "u", "password": "p"}})
    response = _register(alice, "x", REAL.replace("s3cr3t-pw", MASK), secret_from="opps")
    assert response.status_code == 400
