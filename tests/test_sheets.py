"""Tests for the Google Sheets source + token store + app wiring.

The OAuth *flow* needs Google and can't run in CI, so we test everything around it:
the source's reshape via an injected fake Sheets service, encrypted token storage,
and that /api/sheet-data enforces auth (401) without a connected user.
"""

import os
import sqlite3

import pytest

from cx_connectors.sources.base import to_cx
from cx_connectors.sources.google_sheets import GoogleSheetsSource
from cx_connectors.sqlstore import SqlTokenStore
from cx_connectors.store import TokenStore, generate_key

_PG_URL = os.getenv("CXC_TEST_PG_URL")


class _FakeSheets:
    """Mimics the googleapiclient Sheets service chain: .spreadsheets().values().get().execute()"""
    def __init__(self, values):
        self._values = values

    def spreadsheets(self):
        return self

    def values(self):
        return self

    def get(self, spreadsheetId, range):
        self._req = (spreadsheetId, range)
        return self

    def execute(self):
        return {"values": self._values}


# ---------------------------------------------------------------------------
# Backend factories for token store
# ---------------------------------------------------------------------------

def _stdlib_token_store(tmp_path):
    return TokenStore(str(tmp_path / "tokens.db"), generate_key())


def _sql_token_store(tmp_path):
    return SqlTokenStore("sqlite:///" + str(tmp_path / "sql_tokens.db"), generate_key())


def _pg_token_store(tmp_path):
    import sqlalchemy as sa

    engine = sa.create_engine(_PG_URL, future=True)
    with engine.begin() as conn:
        conn.execute(sa.text("DROP TABLE IF EXISTS cxc_tokens"))
    return SqlTokenStore(_PG_URL, generate_key(), engine=engine)


_PARAMS = [(_stdlib_token_store, "stdlib"), (_sql_token_store, "sql")]
if _PG_URL:
    _PARAMS.append((_pg_token_store, "pg"))


@pytest.fixture(params=[p[0] for p in _PARAMS], ids=[p[1] for p in _PARAMS])
def token_store(request, tmp_path):
    return request.param(tmp_path)


# ---------------------------------------------------------------------------
# Google Sheets source tests (no backend)
# ---------------------------------------------------------------------------

def test_google_sheets_source_reshapes_via_injected_service():
    fake = _FakeSheets([
        ["Sample", "GeneA", "GeneB", "Category"],
        ["S1", "11", "13", "A"],
        ["S2", "25", "16", "B"],
    ])
    cx = to_cx(GoogleSheetsSource(credentials=None, spreadsheet_id="x", service=fake))
    assert cx["y"]["vars"] == ["GeneA", "GeneB"]
    assert cx["y"]["smps"] == ["S1", "S2"]
    assert cx["x"]["Category"] == ["A", "B"]


def test_google_sheets_source_pads_ragged_rows():
    # Sheets omits trailing empty cells; source must pad so indexing is safe.
    fake = _FakeSheets([["Sample", "A", "Note"], ["S1", "5"]])  # missing Note cell
    header, rows = GoogleSheetsSource(None, "x", service=fake).read()
    assert rows == [["S1", "5", ""]]


# ---------------------------------------------------------------------------
# Token store parity tests — all backends
# ---------------------------------------------------------------------------

def test_token_store_encrypts_refresh_token(token_store):
    token_store.save("uid-1", "1//secret-refresh", "https://oauth2.googleapis.com/token",
                     "cid", "csecret", ["scope-a"], email="a@example.com")
    rec = token_store.load("uid-1")
    assert rec["refresh_token"] == "1//secret-refresh"
    assert rec["email"] == "a@example.com"
    assert rec["scopes"] == ["scope-a"]


def test_token_store_isolation_and_delete(token_store):
    token_store.save("uid-1", "r", "u", "c", "s", ["x"])
    assert token_store.load("uid-2") is None                 # other user sees nothing
    token_store.delete("uid-1")
    assert token_store.load("uid-1") is None


def test_token_store_upsert(token_store):
    token_store.save("uid-1", "old", "u", "c", "s", ["x"])
    token_store.save("uid-1", "new", "u2", "c2", "s2", ["y", "z"])
    rec = token_store.load("uid-1")
    assert rec["refresh_token"] == "new"
    assert rec["scopes"] == ["y", "z"]


def test_token_not_stored_in_plaintext_sql(tmp_path):
    """SQL-backend equivalent of the SQLite-file ciphertext check."""
    import sqlalchemy as sa

    ts = SqlTokenStore("sqlite:///" + str(tmp_path / "tok.db"), generate_key())
    ts.save("uid-1", "1//secret-refresh", "u", "c", "s", ["x"])
    with ts._engine.connect() as conn:
        row = conn.execute(
            sa.text("SELECT refresh_enc FROM cxc_tokens WHERE user_id='uid-1'")
        ).first()
    raw = bytes(row[0]) if isinstance(row[0], memoryview) else row[0]
    assert b"secret-refresh" not in raw


# ---------------------------------------------------------------------------
# stdlib-only token store test (file-level check)
# ---------------------------------------------------------------------------

def test_token_store_encrypts_refresh_token_at_rest_stdlib(tmp_path):
    db = str(tmp_path / "tokens.db")
    ts = TokenStore(db, generate_key())
    ts.save("uid-1", "1//secret-refresh", "https://oauth2.googleapis.com/token",
            "cid", "csecret", ["scope-a"], email="a@example.com")
    blob = sqlite3.connect(db).execute("SELECT refresh_enc FROM tokens").fetchone()[0]
    assert b"secret-refresh" not in blob            # not stored in plaintext
    rec = ts.load("uid-1")
    assert rec["refresh_token"] == "1//secret-refresh"
    assert rec["email"] == "a@example.com"
    assert rec["scopes"] == ["scope-a"]


# ---------------------------------------------------------------------------
# App wiring test
# ---------------------------------------------------------------------------

def test_sheets_app_requires_connection():
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from cx_connectors.web import create_sheets_app

    app = create_sheets_app(
        client_id="cid", client_secret="csecret",
        redirect_uri="http://localhost/oauth/callback",
        session_secret="s" * 32, encryption_key=generate_key(),
        db_path=":memory:", serve_static=False,
    )
    client = fastapi_testclient.TestClient(app)
    # Not connected -> status false, and data endpoint is 401.
    assert client.get("/api/status").json() == {"connected": False, "email": None}
    assert client.get("/api/sheet-data?spreadsheetId=x").status_code == 401
