"""Live streaming: the LiveSource seam, the SimulatedLiveSource demo feed, the SSE
frame format, the async SSE transport generator, and the /api/stream/demo endpoint wiring.

No network and no real waiting: the transport generator is driven directly with a no-op sleep
and a stub disconnect, and the demo source is seeded for a deterministic sequence.
"""

import asyncio
import json

from cx_connectors.sources.base import LiveSource, format_sse
from cx_connectors.sources.simulated import SimulatedLiveSource
from cx_connectors.store import Store, generate_key
from cx_connectors.web.sse import sse_event_stream


# ---------------------------------------------------------------------------- format_sse
def test_format_sse_encodes_dict_as_json_data_line():
    frame = format_sse({"a": 1}, event="tick", id=7)
    assert frame.endswith("\n\n")
    assert "event: tick" in frame
    assert "id: 7" in frame
    assert 'data: {"a": 1}' in frame


def test_format_sse_comment_and_retry_heartbeat():
    frame = format_sse(comment="ping")
    assert frame == ": ping\n\n"
    assert "retry: 3000" in format_sse(comment="connected", retry=3000)


def test_format_sse_multiline_payload_splits_data_lines():
    frame = format_sse("line1\nline2")
    assert "data: line1" in frame and "data: line2" in frame


# --------------------------------------------------------------------- SimulatedLiveSource
def test_simulated_source_is_a_live_source():
    assert isinstance(SimulatedLiveSource(), LiveSource)


def test_simulated_source_tick_shape_and_pushdata_alignment():
    src = SimulatedLiveSource(variables=["A", "B"], seed=1)
    tick = src.poll()
    assert tick["y"]["vars"] == ["A", "B"]
    assert tick["y"]["smps"] == ["t1"]
    assert len(tick["y"]["data"]) == 2  # one row per variable
    assert all(len(row) == 1 for row in tick["y"]["data"])  # one new sample per row
    assert len(tick["x"]["time"]) == 1


def test_simulated_source_is_deterministic_with_seed():
    src_a = SimulatedLiveSource(seed=42)
    src_b = SimulatedLiveSource(seed=42)
    a = [src_a.poll() for _ in range(3)]
    b = [src_b.poll() for _ in range(3)]
    assert [t["y"]["data"] for t in a] == [t["y"]["data"] for t in b]
    assert [t["y"]["smps"] for t in a] == [["t1"], ["t2"], ["t3"]]  # sample counter increments


def test_simulated_source_respects_bounds():
    src = SimulatedLiveSource(seed=3, start=99.0, step=10.0, low=0.0, high=100.0)
    for _ in range(200):
        value = src.poll()["y"]["data"][0][0]
        assert 0.0 <= value <= 100.0


# ------------------------------------------------------------------------ sse_event_stream
def _drain(agen):
    async def go():
        out = []
        async for frame in agen:
            out.append(frame)
        return out
    return asyncio.run(go())


async def _noop_sleep(_seconds):
    return None


def test_sse_stream_emits_connected_then_ticks_then_stops_at_max():
    src = SimulatedLiveSource(seed=5)
    frames = _drain(sse_event_stream(src, sleep=_noop_sleep, max_ticks=3))
    assert frames[0].startswith(": connected")
    ticks = [f for f in frames if "event: tick" in f]
    assert len(ticks) == 3
    # each tick's data payload is a valid pushData-shaped increment
    payload = ticks[0].split("data: ", 1)[1].strip()
    assert "smps" in json.loads(payload)["y"]


def test_sse_stream_emits_heartbeat_when_poll_returns_none():
    class Idle:
        interval = 0.0
        def poll(self):
            return None  # nothing new this tick -> transport should emit a heartbeat

    calls = {"n": 0}

    async def disconnected():
        calls["n"] += 1
        return calls["n"] > 2  # let two idle polls happen, then stop

    frames = _drain(sse_event_stream(Idle(), sleep=_noop_sleep, is_disconnected=disconnected))
    assert frames[0].startswith(": connected")
    assert sum(f == ": ping\n\n" for f in frames) == 2  # a heartbeat per idle poll
    assert not any("event: tick" in f for f in frames)


def test_sse_stream_stops_on_disconnect():
    calls = {"n": 0}

    async def disconnected():
        calls["n"] += 1
        return calls["n"] > 2

    frames = _drain(sse_event_stream(SimulatedLiveSource(seed=9), sleep=_noop_sleep,
                                     is_disconnected=disconnected))
    # connected + 2 ticks, then the 3rd disconnect check breaks the loop.
    assert sum("event: tick" in f for f in frames) == 2


def test_sse_stream_calls_close_on_exit():
    closed = {"v": False}

    class Closeable:
        interval = 0.0
        def poll(self):
            return {"y": {"vars": ["m"], "smps": ["t"], "data": [[1]]}}
        def close(self):
            closed["v"] = True

    _drain(sse_event_stream(Closeable(), sleep=_noop_sleep, max_ticks=1))
    assert closed["v"] is True


# ------------------------------------------------------------------------------ endpoint
def _client(tmp_path):
    from fastapi.testclient import TestClient
    from cx_connectors.web.byo_app import create_byo_app

    store = Store(str(tmp_path / "app.db"), generate_key())
    store.create_user("alice", "secret1")
    app = create_byo_app(store=store, session_secret="test",
                         encryption_key=generate_key(), serve_static=False)
    return TestClient(app), store


def test_stream_demo_requires_login(tmp_path):
    client, _ = _client(tmp_path)
    assert client.get("/api/stream/demo").status_code == 401


def test_stream_demo_streams_ticks(tmp_path):
    client, _ = _client(tmp_path)
    assert client.post("/auth/login",
                       json={"username": "alice", "password": "secret1"}).status_code == 200
    # A bounded stream (max=3) so the client-side test terminates deterministically; the
    # open-ended form (no max) is what a real dashboard panel opens.
    resp = client.get("/api/stream/demo?interval=0.1&vars=cpu,mem&max=3")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    body = resp.text
    assert ": connected" in body
    ticks = [ln for ln in body.splitlines() if ln.startswith("event: tick")]
    assert len(ticks) == 3
    # every tick carries a pushData-shaped increment for two series
    payloads = [json.loads(ln.split("data: ", 1)[1]) for ln in body.splitlines()
                if ln.startswith("data: ")]
    assert payloads and payloads[0]["y"]["vars"] == ["cpu", "mem"]


# ----------------------------------------------------------------- stream registry
def test_streams_listing_offers_the_demo(tmp_path):
    client, _ = _client(tmp_path)
    assert client.get("/api/streams").status_code == 401  # listing needs a session too
    client.post("/auth/login", json={"username": "alice", "password": "secret1"})
    streams = client.get("/api/streams").json()["streams"]
    demo = [s for s in streams if s["name"] == "demo"][0]
    assert demo["url"] == "/api/stream/demo?vars=cpu,mem"
    assert demo["variables"] == ["cpu", "mem"]


def test_unknown_stream_is_404(tmp_path):
    client, _ = _client(tmp_path)
    client.post("/auth/login", json={"username": "alice", "password": "secret1"})
    assert client.get("/api/stream/nope?max=1").status_code == 404


def test_host_registered_stream_opens_per_user(tmp_path):
    from fastapi.testclient import TestClient
    from cx_connectors.web.byo_app import create_byo_app

    opened = []

    def open_quotes(query, interval, user):
        opened.append((user, interval))
        return SimulatedLiveSource(variables=["IBM"], interval=interval, seed=1)

    store = Store(str(tmp_path / "app.db"), generate_key())
    store.create_user("alice", "secret1")
    app = create_byo_app(store=store, session_secret="test", encryption_key=generate_key(),
                         serve_static=False,
                         live_streams={"quotes": {"title": "Quotes", "variables": ["IBM"],
                                                  "open": open_quotes}})
    client = TestClient(app)
    client.post("/auth/login", json={"username": "alice", "password": "secret1"})
    names = [s["name"] for s in client.get("/api/streams").json()["streams"]]
    assert names == ["demo", "quotes"]  # the default stays, the host's is added
    body = client.get("/api/stream/quotes?interval=0.1&max=2").text
    assert body.count("event: tick") == 2
    assert opened == [("alice", 0.1)]  # built server-side for the signed-in viewer
