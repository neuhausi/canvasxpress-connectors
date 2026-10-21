"""Posit pins source: direct reads + registration/dispatch through the BYO web app.

A real ``board_folder`` round-trip exercises the ``pins`` package (skipped when it is not
installed); the web tests validate registration and that ``/api/data`` decrypts the stored
blob, builds the source, and reshapes the pin — no network.
"""

import json
import os

import pytest

from cx_connectors.sources.pins_board import PinsBoardSource, _frame_to_rows
from cx_connectors.store import Store, generate_key

pins = pytest.importorskip("pins")
pd = pytest.importorskip("pandas")


def _folder_board(tmp_path):
    board = pins.board_folder(str(tmp_path / "board"))
    board.pin_write(pd.DataFrame({"city": ["A", "B"], "pop": [10, 20]}),
                    "cities", type="csv", force_identical_write=True)
    return board


def test_folder_board_read_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("CXC_PINS_FOLDER_ROOTS", str(tmp_path))
    _folder_board(tmp_path)
    header, rows = PinsBoardSource("folder", "cities",
                                   {"path": str(tmp_path / "board")}).read()
    assert header == ["city", "pop"]
    assert rows == [["A", 10], ["B", 20]]


def test_unknown_board_type_and_missing_params():
    with pytest.raises(ValueError):
        PinsBoardSource("dropbox", "x")
    with pytest.raises(ValueError):
        PinsBoardSource("folder", "x", {})._require_params()
    with pytest.raises(ValueError):
        PinsBoardSource("url", "x", {"url": "http://b"})._require_params()


# ---- credential-escalation policy ----

def test_connect_requires_explicit_server_url_and_key(monkeypatch):
    # No key / no server_url ⇒ rejected at registration AND read, env fallback irrelevant.
    monkeypatch.setenv("CONNECT_SERVER", "https://connect.internal")
    monkeypatch.setenv("CONNECT_API_KEY", "SERVER_OWNER_KEY")
    for cfg in ({"server_url": "https://c.example.com"}, {"api_key": "K"}, {}):
        with pytest.raises(ValueError):
            PinsBoardSource("connect", "owner/pin", cfg)._require_params()
        with pytest.raises(ValueError):
            PinsBoardSource("connect", "owner/pin", cfg).read()


def test_connect_passes_explicit_creds_never_none(monkeypatch):
    monkeypatch.setenv("CONNECT_SERVER", "https://connect.internal")
    monkeypatch.setenv("CONNECT_API_KEY", "SERVER_OWNER_KEY")
    captured = {}

    def fake_board_connect(server_url=None, api_key=None):
        captured["server_url"] = server_url
        captured["api_key"] = api_key
        raise RuntimeError("stop before network")

    monkeypatch.setattr(pins, "board_connect", fake_board_connect)
    src = PinsBoardSource("connect", "owner/pin",
                          {"server_url": "https://c.example.com", "api_key": "USERKEY"})
    with pytest.raises(RuntimeError):
        src.read()
    assert captured == {"server_url": "https://c.example.com", "api_key": "USERKEY"}
    assert captured["server_url"] is not None and captured["api_key"] is not None


def test_folder_disabled_without_allowlist(tmp_path, monkeypatch):
    monkeypatch.delenv("CXC_PINS_FOLDER_ROOTS", raising=False)
    with pytest.raises(ValueError):
        PinsBoardSource("folder", "cities", {"path": str(tmp_path)})._require_params()
    with pytest.raises(ValueError):
        PinsBoardSource("folder", "cities", {"path": str(tmp_path)}).read()


def test_folder_outside_allowlist_refused(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "secret"
    outside.mkdir()
    monkeypatch.setenv("CXC_PINS_FOLDER_ROOTS", str(allowed))
    with pytest.raises(ValueError):
        PinsBoardSource("folder", "x", {"path": str(outside)})._require_params()


def test_folder_dotdot_escape_refused(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    (tmp_path / "secret").mkdir()
    monkeypatch.setenv("CXC_PINS_FOLDER_ROOTS", str(allowed))
    escape = str(allowed / ".." / "secret")
    with pytest.raises(ValueError):
        PinsBoardSource("folder", "x", {"path": escape})._require_params()


def test_folder_symlink_escape_refused(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    secret = tmp_path / "secret"
    secret.mkdir()
    link = allowed / "link"
    os.symlink(str(secret), str(link))
    monkeypatch.setenv("CXC_PINS_FOLDER_ROOTS", str(allowed))
    with pytest.raises(ValueError):
        PinsBoardSource("folder", "x", {"path": str(link)})._require_params()


def test_s3_refused_without_optin_or_config_creds(monkeypatch):
    monkeypatch.delenv("CXC_PINS_S3_SERVER_CREDENTIALS", raising=False)
    with pytest.raises(ValueError):
        PinsBoardSource("s3", "metrics",
                        {"path": "bucket", "region": "us-east-1"})._require_params()


def test_s3_allowed_with_config_creds(monkeypatch):
    monkeypatch.delenv("CXC_PINS_S3_SERVER_CREDENTIALS", raising=False)
    # Config carries its own credentials ⇒ allowed (no server fallback involved).
    PinsBoardSource("s3", "metrics",
                    {"path": "bucket", "key": "AKIA…", "secret": "s"})._require_params()


def test_s3_allowed_with_operator_optin(monkeypatch):
    monkeypatch.setenv("CXC_PINS_S3_SERVER_CREDENTIALS", "on")
    PinsBoardSource("s3", "metrics", {"path": "bucket"})._require_params()


def test_non_tabular_pin_is_rejected():
    with pytest.raises(ValueError):
        _frame_to_rows(object(), "model")


def test_list_of_records_pin():
    header, rows = _frame_to_rows([{"a": 1, "b": 2}, {"a": 3, "b": 4}], "p")
    assert header == ["a", "b"]
    assert rows == [[1, 2], [3, 4]]


# ---- web app registration + dispatch ----

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


def test_register_pins_source_stores_kind_and_config(tmp_path):
    client, store = _client(tmp_path)
    r = client.post("/api/sources", json={
        "name": "sales", "kind": "pins", "board_type": "connect",
        "pin": "hadley/sales-summary",
        "config": {"server_url": "https://connect.example.com", "api_key": "KEY123"},
    })
    assert r.status_code == 200, r.text
    rec = store.get_source("alice", "sales")
    assert rec["kind"] == "pins"
    cfg = json.loads(rec["conn_url"])
    assert cfg["board_type"] == "connect" and cfg["pin"] == "hadley/sales-summary"
    assert cfg["config"]["api_key"] == "KEY123"      # stored encrypted at rest


def test_register_pins_rejects_bad_board_type_and_missing_path(tmp_path):
    client, _ = _client(tmp_path)
    assert client.post("/api/sources", json={
        "name": "x", "kind": "pins", "board_type": "dropbox", "pin": "p"}).status_code == 400
    assert client.post("/api/sources", json={
        "name": "y", "kind": "pins", "board_type": "folder", "pin": "p",
        "config": {}}).status_code == 400


def test_pins_data_dispatch_reshapes_pin(tmp_path, monkeypatch):
    monkeypatch.setenv("CXC_PINS_FOLDER_ROOTS", str(tmp_path))
    client, _ = _client(tmp_path)
    client.post("/api/sources", json={
        "name": "cities", "kind": "pins", "board_type": "folder", "pin": "cities",
        "config": {"path": str(tmp_path / "board")}})
    _folder_board(tmp_path)
    r = client.get("/api/data", params={"source": "cities"})
    assert r.status_code == 200, r.text
    cx = r.json()
    assert cx["y"]["vars"] == ["city", "pop"] or "pop" in cx["y"]["vars"]
