"""The DataSource seam: everything a connector needs to produce a CanvasXpress object.

A source's only job is to return ``(header, rows)``. ``reshape.rows_to_cx`` does the
rest, so adding a new backend (BigQuery, a REST API, a CSV endpoint) means writing one
small class — nothing else in the stack changes.
"""

from __future__ import annotations

from typing import Any, List, Protocol, Sequence, Tuple, runtime_checkable


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
