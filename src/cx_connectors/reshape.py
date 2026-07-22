"""Reshape tabular results into a CanvasXpress native data object.

A query result — from any source — is always "column names + rows". This turns
that into CanvasXpress' ``{y: {vars, smps, data}, x: {...}}`` shape:

* first column        -> sample ids (``y.smps``)
* numeric columns     -> variables (``y.vars`` + ``y.data``)
* remaining columns   -> per-sample annotations (``x``)
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence


def _is_number(value: Any) -> bool:
    if value is None or value == "":
        return False
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


def rows_to_cx(header: Sequence[str], rows: Sequence[Sequence[Any]]) -> Dict[str, Any]:
    """Convert ``header`` + ``rows`` into a CanvasXpress data object.

    Raises ``ValueError`` if there are no rows.
    """
    if not rows:
        raise ValueError("Query returned no rows")

    ncols = len(header)
    numeric_col = [all(_is_number(r[c]) for r in rows) for c in range(ncols)]

    smps: List[str] = [str(r[0]) for r in rows]
    variables = [(header[c], c) for c in range(1, ncols) if numeric_col[c]]
    annotations = {
        header[c]: [r[c] for r in rows]
        for c in range(1, ncols) if not numeric_col[c]
    }
    data = [[float(r[c]) for r in rows] for (_, c) in variables]

    out: Dict[str, Any] = {
        "y": {"vars": [name for (name, _) in variables], "smps": smps, "data": data}
    }
    if annotations:
        out["x"] = annotations
    return out
