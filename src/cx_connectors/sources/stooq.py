"""Stooq end-of-day price source.

Reads daily OHLCV history from Stooq's **keyless CSV** endpoint
(``https://stooq.com/q/d/l/?s=<symbol>&i=d``) and returns ``(header, rows)`` for
``reshape.rows_to_cx`` — the same seam every other source uses. US tickers use the ``.us``
suffix (``ibm`` -> ``ibm.us``); the class adds it when the symbol has no exchange suffix.

Stooq needs no API key, which makes it the natural default for the OptionsWall price panel.
Note that Stooq sometimes serves a JavaScript anti-bot / proof-of-work challenge to datacenter
IPs; when that happens the body is HTML, not CSV, and :meth:`read` raises ``ValueError`` — run
it from a normal network or inject a pre-authorized ``requests.Session``.
"""

from __future__ import annotations

from typing import Any, List, Optional, Sequence, Tuple


class StooqSource:
    """A keyless Stooq daily OHLCV query, reshaped to ``(header, rows)``."""

    _HOST = "https://stooq.com/q/d/l/"

    def __init__(
        self,
        symbol: str,
        interval: str = "d",
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        session=None,
    ):
        """
        :param symbol: Ticker; a plain US symbol gets the ``.us`` suffix (``"IBM"`` -> ``ibm.us``).
        :param interval: ``d`` (daily, default), ``w`` (weekly) or ``m`` (monthly).
        :param date_from: Optional start date ``YYYYMMDD`` (Stooq ``d1``).
        :param date_to: Optional end date ``YYYYMMDD`` (Stooq ``d2``).
        :param session: Inject a prebuilt ``requests.Session`` (used in tests); when ``None`` one
            is built lazily with a browser User-Agent.
        """
        self.symbol = symbol if "." in symbol else symbol.lower() + ".us"
        self.interval = interval
        self.date_from = date_from
        self.date_to = date_to
        self._session = session

    def read(self) -> Tuple[Sequence[str], Sequence[Sequence[Any]]]:
        """Fetch the CSV and return ``(header, rows)`` — one row per trading day, ascending."""
        params = {"s": self.symbol, "i": self.interval}
        if self.date_from:
            params["d1"] = self.date_from
        if self.date_to:
            params["d2"] = self.date_to

        session = self._session
        if session is None:
            import requests  # lazy: the core package doesn't require requests

            session = requests.Session()
            session.headers.update(
                {
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                    "Accept": "text/csv,*/*",
                }
            )

        response = session.get(self._HOST, params=params, timeout=60)
        response.raise_for_status()
        text = response.text or ""
        if not text.lstrip().lower().startswith("date"):
            raise ValueError(
                "Stooq did not return CSV (likely an anti-bot challenge for this IP); "
                "run from a normal network or inject an authorized session."
            )

        lines = [ln for ln in text.splitlines() if ln.strip()]
        header = [c.strip() for c in lines[0].split(",")]
        rows: List[List[Any]] = []
        for line in lines[1:]:
            cells = line.split(",")
            row: List[Any] = []
            for i, cell in enumerate(cells):
                cell = cell.strip()
                if i == 0:
                    row.append(cell)  # Date (sample axis)
                else:
                    try:
                        row.append(float(cell))
                    except ValueError:
                        row.append(cell)
            rows.append(row)
        return header, rows
