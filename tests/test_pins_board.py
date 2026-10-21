"""Posit pins source: direct reads + registration/dispatch through the BYO web app.

A real ``board_folder`` round-trip exercises the ``pins`` package (skipped when it is not
installed); the web tests validate registration and that ``/api/data`` decrypts the stored
blob, builds the source, and reshapes the pin — no network.
"""

import json

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


def test_folder_board_read_round_trip(tmp_path):
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


def test_pins_data_dispatch_reshapes_pin(tmp_path):
    client, _ = _client(tmp_path)
    client.post("/api/sources", json={
        "name": "cities", "kind": "pins", "board_type": "folder", "pin": "cities",
        "config": {"path": str(tmp_path / "board")}})
    _folder_board(tmp_path)
    r = client.get("/api/data", params={"source": "cities"})
    assert r.status_code == 200, r.text
    cx = r.json()
    assert cx["y"]["vars"] == ["city", "pop"] or "pop" in cx["y"]["vars"]
