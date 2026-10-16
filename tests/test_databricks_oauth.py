"""Databricks "sign in as the viewer" via Posit Connect OAuth.

No Connect or Databricks is available in the test environment, so both are
faked: a fake ``posit.connect.Client`` for the token exchange, and (for the web
tests) ``SqlSource`` / ``run_pushdown`` / ``run_join`` are monkeypatched to
capture the connection URL instead of touching a real database.
"""

from __future__ import annotations

import sys
import types
from urllib.parse import parse_qs, urlsplit

import pytest

from cx_connectors.databricks_oauth import (
    AUDIENCE_ENV,
    OAuthExchangeFailed,
    ViewerSessionRequired,
    exchange_viewer_token,
    is_viewer_oauth_url,
    resolve_conn_url,
)

HOST = "dbc-1234.cloud.databricks.com"
VIEWER_URL = ("databricks://posit-connect@" + HOST
              + "?http_path=/sql/1.0/warehouses/abc123&catalog=main&schema=default")
PAT_URL = ("databricks://token:dapiSECRET@" + HOST
           + "?http_path=/sql/1.0/warehouses/abc123&catalog=main&schema=default")


# ---------------------------------------------------------------------------
# Fake Posit Connect client
# ---------------------------------------------------------------------------

def _fake_factory(token="viewer-token", audience_sink=None, fail=None):
    """A client_factory whose oauth.get_credentials returns ``token`` (or one
    derived from the session token if ``token`` is callable), records the
    audience, or raises ``fail``."""
    class _OAuth:
        def get_credentials(self, session_token, audience=None):
            if audience_sink is not None:
                audience_sink.append(audience)
            if fail is not None:
                raise fail
            value = token(session_token) if callable(token) else token
            return {"access_token": value}

    class _Client:
        oauth = _OAuth()

    return lambda: _Client()


# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------

def test_detects_opted_in_vs_pat_vs_other_vs_saas():
    assert is_viewer_oauth_url(VIEWER_URL) is True
    assert is_viewer_oauth_url("databricks+connector://posit-connect@" + HOST) is True
    assert is_viewer_oauth_url(PAT_URL) is False                      # has a password
    assert is_viewer_oauth_url("postgresql://posit-connect@h/db") is False  # not databricks
    assert is_viewer_oauth_url('{"instance": "acme", "table": "incident"}') is False  # SaaS blob
    assert is_viewer_oauth_url(None) is False


# ---------------------------------------------------------------------------
# URL rewriting
# ---------------------------------------------------------------------------

def test_rewrite_keeps_host_path_catalog_schema_and_encodes_token():
    resolved = resolve_conn_url(VIEWER_URL, {"posit-connect-user-session-token": "sess"},
                                client_factory=_fake_factory(token="a/b c+d"))
    parts = urlsplit(resolved)
    assert parts.hostname == HOST
    assert parts.username == "token"
    # The token is URL-encoded in the userinfo (no raw '/', ' ' or '+').
    assert parts.password == "a%2Fb%20c%2Bd"
    q = parse_qs(parts.query)
    assert q["http_path"] == ["/sql/1.0/warehouses/abc123"]
    assert q["catalog"] == ["main"]
    assert q["schema"] == ["default"]


def test_non_opted_in_urls_are_returned_unchanged():
    assert resolve_conn_url(PAT_URL, {}) == PAT_URL
    assert resolve_conn_url("postgresql://u:p@h/db", {}) == "postgresql://u:p@h/db"


# ---------------------------------------------------------------------------
# Missing viewer session -> 403-worthy error
# ---------------------------------------------------------------------------

def test_no_session_token_raises_viewer_required():
    with pytest.raises(ViewerSessionRequired):
        resolve_conn_url(VIEWER_URL, {}, client_factory=_fake_factory())
    with pytest.raises(ViewerSessionRequired):
        resolve_conn_url(VIEWER_URL, None)


# ---------------------------------------------------------------------------
# Exchange: audience passed, failures wrapped, token never in the message
# ---------------------------------------------------------------------------

def test_audience_is_passed_from_env(monkeypatch):
    monkeypatch.setenv(AUDIENCE_ENV, "guid-xyz")
    sink = []
    resolve_conn_url(VIEWER_URL, {"posit-connect-user-session-token": "sess"},
                     client_factory=_fake_factory(audience_sink=sink))
    assert sink == ["guid-xyz"]


def test_no_audience_when_env_absent(monkeypatch):
    monkeypatch.delenv(AUDIENCE_ENV, raising=False)
    sink = []
    exchange_viewer_token("sess", audience=None, client_factory=_fake_factory(audience_sink=sink))
    assert sink == [None]


def test_exchange_failure_is_wrapped_without_token():
    boom = RuntimeError("connect said no for token sekret-123")
    with pytest.raises(OAuthExchangeFailed) as exc:
        exchange_viewer_token("sess", client_factory=_fake_factory(fail=boom))
    # Our message names the failure class, never the upstream token text.
    assert "sekret-123" not in str(exc.value)
    assert "RuntimeError" in str(exc.value)


def test_missing_posit_sdk_is_wrapped():
    def _factory():
        raise ImportError("no module named posit")
    with pytest.raises(OAuthExchangeFailed):
        exchange_viewer_token("sess", client_factory=lambda: (_ for _ in ()).throw(ImportError()))


def test_empty_access_token_raises():
    with pytest.raises(OAuthExchangeFailed):
        exchange_viewer_token("sess", client_factory=_fake_factory(token=""))


# ---------------------------------------------------------------------------
# Join consistency: one memo -> both sides get the SAME token
# ---------------------------------------------------------------------------

def test_memo_keeps_two_sides_of_a_join_identical():
    # token derived from a counter so a second exchange would differ
    calls = {"n": 0}

    def _tok(_session):
        calls["n"] += 1
        return "tok-%d" % calls["n"]

    memo = {}
    headers = {"posit-connect-user-session-token": "sess"}
    left = resolve_conn_url(VIEWER_URL, headers, memo, client_factory=_fake_factory(token=_tok))
    right = resolve_conn_url(VIEWER_URL, headers, memo, client_factory=_fake_factory(token=_tok))
    assert left == right          # run_join compares the two URLs for equality
    assert calls["n"] == 1        # exchanged once, reused for the second side


# ---------------------------------------------------------------------------
# Through the web app
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_posit(monkeypatch):
    """Install a fake ``posit.connect`` so the default client factory works, and
    record every exchange as ``(session_token, audience)``."""
    exchanges = []

    class _OAuth:
        def get_credentials(self, session_token, audience=None):
            exchanges.append((session_token, audience))
            return {"access_token": "tok-" + session_token}

    class _Client:
        oauth = _OAuth()

    posit = types.ModuleType("posit")
    connect = types.ModuleType("posit.connect")
    connect.Client = _Client
    posit.connect = connect
    monkeypatch.setitem(sys.modules, "posit", posit)
    monkeypatch.setitem(sys.modules, "posit.connect", connect)
    return exchanges


def _byo_client(tmp_path, monkeypatch, captured):
    """A TestClient for create_byo_app with a logged-in user who owns one
    viewer-OAuth Databricks source; SqlSource is faked to capture the conn_url."""
    from fastapi.testclient import TestClient

    from cx_connectors.store import Store, generate_key
    from cx_connectors.web import byo_app

    class _FakeSqlSource:
        def __init__(self, conn_url, sql, params=None):
            # Note: add_source also constructs this to validate read-only (no
            # read()), so capture at read() — the actual connect — not __init__.
            self.conn_url = conn_url
            self.truncated = False

        def read(self, row_cap=None):
            captured.append(self.conn_url)
            return ["a", "b"], [[1, 2]]

    monkeypatch.setattr(byo_app, "SqlSource", _FakeSqlSource)

    store = Store(str(tmp_path / "app.db"), generate_key())
    app = byo_app.create_byo_app(store=store, session_secret="s" * 16,
                                 encryption_key=generate_key(), serve_static=False)
    client = TestClient(app)
    client.post("/auth/signup", json={"username": "alice", "password": "password"})
    client.post("/api/sources", json={"name": "dbx", "conn_url": VIEWER_URL,
                                       "sql": "SELECT a, b FROM t"})
    return client, store


def test_web_data_exchanges_fresh_token_each_request(tmp_path, monkeypatch, fake_posit):
    captured = []
    client, store = _byo_client(tmp_path, monkeypatch, captured)

    headers = {"Posit-Connect-User-Session-Token": "viewer-1"}
    r1 = client.get("/api/data?source=dbx", headers=headers)
    r2 = client.get("/api/data?source=dbx", headers=headers)
    assert r1.status_code == 200 and r2.status_code == 200

    # Two requests -> two exchanges (nothing cached across requests).
    assert len(fake_posit) == 2
    # The connection URL carried the exchanged token, not 'posit-connect'.
    assert all("token:tok-viewer-1@" in url for url in captured)
    # The STORED source still contains no token.
    assert store.get_source("alice", "dbx")["conn_url"] == VIEWER_URL


def test_web_two_viewers_get_different_tokens(tmp_path, monkeypatch, fake_posit):
    captured = []
    client, _ = _byo_client(tmp_path, monkeypatch, captured)

    client.get("/api/data?source=dbx", headers={"Posit-Connect-User-Session-Token": "viewer-A"})
    client.get("/api/data?source=dbx", headers={"Posit-Connect-User-Session-Token": "viewer-B"})
    assert "token:tok-viewer-A@" in captured[0]
    assert "token:tok-viewer-B@" in captured[1]


def test_web_no_header_is_403(tmp_path, monkeypatch, fake_posit):
    captured = []
    client, _ = _byo_client(tmp_path, monkeypatch, captured)
    r = client.get("/api/data?source=dbx")   # no viewer session token
    assert r.status_code == 403
    assert "signed-in viewer" in r.json()["detail"]
    assert captured == []                     # never tried to connect
