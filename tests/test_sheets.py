"""Tests for the Google Sheets source + token store + app wiring.

The OAuth *flow* needs Google and can't run in CI, so we test everything around it:
the source's reshape via an injected fake Sheets service, encrypted token storage,
and that /api/sheet-data enforces auth (401) without a connected user.
"""

import sqlite3

import pytest

from cx_connectors.sources.base import to_cx
from cx_connectors.sources.google_sheets import GoogleSheetsSource
from cx_connectors.store import TokenStore, generate_key


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


def test_token_store_encrypts_refresh_token(tmp_path):
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


def test_token_store_isolation_and_delete(tmp_path):
    ts = TokenStore(str(tmp_path / "t.db"), generate_key())
    ts.save("uid-1", "r", "u", "c", "s", ["x"])
    assert ts.load("uid-2") is None                 # other user sees nothing
    ts.delete("uid-1")
    assert ts.load("uid-1") is None


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
