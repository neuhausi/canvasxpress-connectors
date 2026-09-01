"""Nasdaq option-chain source (keyless).

Reads the real option chain from Nasdaq's public JSON endpoint
(``https://api.nasdaq.com/api/quote/<symbol>/option-chain``) and returns one contract dict per
call/put via :meth:`read_contracts` (for the OptionsWall reshaper), plus a flat ``(header, rows)``
via :meth:`read`. No API key is required.

The chain is a flat table where an ``expirygroup`` header row (null strike) sets the expiry for
the option rows that follow; each option row carries both the call (``c_*``) and put (``p_*``)
side for one strike. This source flattens that into separate call/put contracts and parses the
``"September 4, 2026"`` group label into ``2026-09-04``. Nasdaq does not return implied
volatility on this endpoint, so contracts carry **premium** (last price) + volume/open-interest —
drive the OptionsWall flanks with ``optionsWallFlankMetric: "premium"``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

_HOST = "https://api.nasdaq.com/api/quote/"


def _num(value: Any) -> Optional[float]:
    if value in (None, "", "--", "N/A"):
        return None
    try:
        return float(str(value).replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return None


def _parse_expiry(label: Optional[str]) -> Optional[str]:
    """'September 4, 2026' -> '2026-09-04' (returns the label unchanged if unparseable)."""
    if not label:
        return None
    try:
        return datetime.strptime(label.strip(), "%B %d, %Y").strftime("%Y-%m-%d")
    except ValueError:
        return label


class NasdaqOptionsSource:
    """A keyless Nasdaq option chain, exposed as OptionsWall-ready contract dicts."""

    _FIELDS = ("type", "strike", "expiration", "premium", "bid", "ask", "volume", "open_interest")

    def __init__(self, symbol: str, asset_class: str = "stocks", limit: int = 0,
                 from_date: str = "all", session=None):
        """
        :param symbol: Underlying ticker (e.g. ``"IBM"``).
        :param asset_class: Nasdaq asset class (``"stocks"`` default; ``"etf"`` / ``"index"``).
        :param limit: ``0`` for the full chain (default), else Nasdaq's row cap.
        :param from_date: Nasdaq ``fromdate`` (``"all"`` default).
        :param session: Inject a prebuilt ``requests.Session`` (used in tests); when ``None`` one
            is built lazily with a browser User-Agent (Nasdaq rejects the default requests UA).
        """
        self.symbol = symbol
        self.asset_class = asset_class
        self.limit = limit
        self.from_date = from_date
        self._session = session

    def _fetch_rows(self) -> List[Dict[str, Any]]:
        params = {"assetclass": self.asset_class, "fromdate": self.from_date}
        if self.limit:
            params["limit"] = str(self.limit)
        session = self._session
        if session is None:
            import requests  # lazy: the core package doesn't require requests

            session = requests.Session()
            session.headers.update({
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                "Accept": "application/json",
            })
        response = session.get(_HOST + self.symbol + "/option-chain", params=params, timeout=60)
        response.raise_for_status()
        payload = response.json() or {}
        data = payload.get("data") or {}
        table = data.get("table") or {}
        return table.get("rows") or []

    def read_contracts(self) -> List[Dict[str, Any]]:
        """Return a flat list of ``{type, strike, expiration, premium, bid, ask, volume, ...}``."""
        rows = self._fetch_rows()
        contracts: List[Dict[str, Any]] = []
        current_expiry: Optional[str] = None
        for row in rows:
            group = row.get("expirygroup")
            if group:
                current_expiry = _parse_expiry(group)
            strike = _num(row.get("strike"))
            if strike is None:
                continue  # an expirygroup header row
            # Prefer the expirygroup header (full "Month D, YYYY") over the row's abbreviated
            # expiryDate (e.g. "Sep 18", which lacks the year).
            expiry = current_expiry or _parse_expiry(row.get("expiryDate"))
            for side, prefix in (("call", "c_"), ("put", "p_")):
                contracts.append({
                    "type": side,
                    "strike": strike,
                    "expiration": expiry,
                    "premium": _num(row.get(prefix + "Last")),
                    "bid": _num(row.get(prefix + "Bid")),
                    "ask": _num(row.get(prefix + "Ask")),
                    "volume": _num(row.get(prefix + "Volume")) or 0.0,
                    "open_interest": _num(row.get(prefix + "Openinterest")) or 0.0,
                })
        return contracts

    def read(self) -> Tuple[Sequence[str], Sequence[Sequence[Any]]]:
        """Return ``(header, rows)`` — one flat row per call/put contract."""
        contracts = self.read_contracts()
        header = list(self._FIELDS)
        rows = [[c.get(name) for name in header] for c in contracts]
        return header, rows
