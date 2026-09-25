"""The DataSource seam: everything a connector needs to produce a CanvasXpress object.

A source's only job is to return ``(header, rows)``. ``reshape.rows_to_cx`` does the
rest, so adding a new backend (BigQuery, a REST API, a CSV endpoint) means writing one
small class — nothing else in the stack changes.
"""

from __future__ import annotations

import json
from typing import Any, Optional, Protocol, Sequence, Tuple, runtime_checkable


@runtime_checkable
class DataSource(Protocol):
    def read(self) -> Tuple[Sequence[str], Sequence[Sequence[Any]]]:
        """Return ``(header, rows)`` — column names and row values."""
        ...


def to_cx(source: DataSource):
    """Convenience: read a source and reshape it in one call."""
    from ..reshape import rows_to_cx

    header, rows = source.read()
    return rows_to_cx(header, rows)


# A streaming increment: a CanvasXpress-shaped payload describing the NEW samples
# only, exactly the shape the engine's ``instance.pushData(tick)`` consumes::
#
#     {"y": {"vars": ["A", "B"],           # optional; aligns rows by name when given
#            "smps": ["t5"],               # the new sample name(s)
#            "data": [[a5], [b5]]},        # rows = variables, cols = the new samples
#      "x": {"time": ["2026-09-25T12:00:00Z"]}}   # optional per-new-sample annotations
Tick = dict


@runtime_checkable
class LiveSource(Protocol):
    """A push/streaming data source: the counterpart of :class:`DataSource` for real-time feeds.

    Where a ``DataSource`` answers one request with a full ``(header, rows)`` snapshot, a
    ``LiveSource`` is polled repeatedly and yields *increments* — one tick of new samples per
    :meth:`poll`, shaped so the SSE transport can relay each straight to the browser's
    ``pushData(tick)`` (the engine rolling-window seam). The server holds any upstream
    credential and relays; the browser subscribes to our origin, never upstream — the same
    isolation model as the request/response connectors.
    """

    #: Seconds the transport waits between polls (the tick cadence). "Dashboard-live", not HFT.
    interval: float

    def poll(self) -> Optional[Tick]:
        """Return the next :data:`Tick` (new samples), or ``None`` to emit only a heartbeat."""
        ...


def format_sse(
    data: Any = None,
    *,
    event: Optional[str] = None,
    id: Optional[Any] = None,
    comment: Optional[str] = None,
    retry: Optional[int] = None,
) -> str:
    """Serialize one Server-Sent-Events frame (the wire format the SSE transport emits).

    Pure and dependency-free so it can be unit-tested without a server. A non-string ``data``
    is JSON-encoded; multi-line payloads are split into one ``data:`` line each per the SSE
    spec. ``comment`` becomes a ``:``-prefixed line (used for heartbeats, which keep the
    connection warm without delivering a message); ``retry`` sets the client's reconnect delay
    in milliseconds (native ``EventSource`` reconnection honors it).

    :param data: The message payload; ``dict``/list is JSON-encoded, ``str`` sent verbatim,
        ``None`` omits the data field (e.g. a heartbeat-only frame).
    :param event: Optional ``event:`` type (the browser listens per type).
    :param id: Optional ``id:`` — the client echoes it as ``Last-Event-ID`` on reconnect.
    :param comment: Optional ``:`` comment line (heartbeat / keep-alive).
    :param retry: Optional reconnect delay hint in milliseconds.
    :returns: The frame text, terminated by the blank line that ends an SSE event.
    """
    lines = []
    if comment is not None:
        lines.append(": " + comment)
    if event is not None:
        lines.append("event: " + str(event))
    if id is not None:
        lines.append("id: " + str(id))
    if retry is not None:
        lines.append("retry: " + str(int(retry)))
    if data is not None:
        payload = data if isinstance(data, str) else json.dumps(data, default=str)
        for line in payload.split("\n"):
            lines.append("data: " + line)
    return "\n".join(lines) + "\n\n"
