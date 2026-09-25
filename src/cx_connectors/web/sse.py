"""The SSE transport: turn a :class:`~cx_connectors.sources.base.LiveSource` into an
``text/event-stream`` a browser ``EventSource`` consumes.

The engine renders and the browser holds no credential: this async generator polls the source
on the server (where the upstream secret lives), formats each tick as an SSE frame, and relays
it to the browser, which feeds it to ``pushData(tick)``. Reconnection is native to
``EventSource`` (we send a ``retry`` hint and an ``id`` per tick so it resumes with
``Last-Event-ID``); a heartbeat comment keeps the connection warm through idle polls and proxies.
Needs the ``web`` extra (starlette, pulled in by FastAPI).
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator, Awaitable, Callable, Optional

from starlette.concurrency import run_in_threadpool

from ..sources.base import LiveSource, format_sse

# Reconnect hint (ms) sent once at stream open; EventSource waits this long before retrying.
_RETRY_MS = 3000


async def sse_event_stream(
    source: LiveSource,
    *,
    is_disconnected: Optional[Callable[[], Awaitable[bool]]] = None,
    sleep: Optional[Callable[[float], Awaitable[None]]] = None,
    max_ticks: Optional[int] = None,
) -> AsyncIterator[str]:
    """Yield SSE frames for a live source until the client disconnects (or ``max_ticks``).

    Each loop: bail if the client is gone, poll the source (off the event loop, in a worker
    thread, so a blocking upstream read never stalls other requests), emit the tick as a
    ``tick`` event (or a heartbeat comment when the poll yields ``None``), then wait
    ``source.interval`` seconds. A ``close()`` on the source, if present, is called on exit.

    :param source: The :class:`LiveSource` to stream (must expose ``interval`` and ``poll``).
    :param is_disconnected: Optional awaitable returning ``True`` once the client has gone
        (``request.is_disconnected`` in the endpoint); the stream stops cleanly when it does.
    :param sleep: Injectable async sleep (defaults to ``asyncio.sleep``); tests pass a no-op.
    :param max_ticks: Optional cap on emitted ticks — the stream ends after this many
        (used by tests; the live endpoint leaves it ``None`` for an open-ended stream).
    :returns: An async iterator of SSE frame strings.
    """
    sleep = sleep or asyncio.sleep
    interval = float(getattr(source, "interval", 1.0))
    yield format_sse(comment="connected", retry=_RETRY_MS)
    count = 0
    try:
        while True:
            if is_disconnected is not None and await is_disconnected():
                break
            tick = await run_in_threadpool(source.poll)
            if tick is not None:
                count += 1
                yield format_sse(tick, event="tick", id=count)
            else:
                yield format_sse(comment="ping")
            if max_ticks is not None and count >= max_ticks:
                break
            await sleep(interval)
    finally:
        close = getattr(source, "close", None)
        if callable(close):
            close()
