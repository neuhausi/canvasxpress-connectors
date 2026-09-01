"""Alpha Vantage data sources — daily prices and (historical) option chains.

Two read-only ``DataSource`` classes over the Alpha Vantage JSON API (a free API key is
required — https://www.alphavantage.co/support/#api-key), the most complete **free** source
that carries an options chain:

* :class:`AlphaVantageSource` — ``TIME_SERIES_DAILY`` OHLCV history, returned as ``(header, rows)``
  for ``reshape.rows_to_cx`` (Date first, so it is the sample axis).
* :class:`AlphaVantageOptionsSource` — ``HISTORICAL_OPTIONS`` full option chain for a symbol
  (optionally a specific ``date``), returned as a list of contract dicts (``read_contracts``)
  for the OptionsWall reshaper, plus a flat ``(header, rows)`` via ``read`` for generic use.

Both issue only ``GET`` and take no credentials beyond the API key.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

_HOST = "https://www.alphavantage.co/query"


def _session(session):
    if session is not None:
        return session
    import requests  # lazy: the core package doesn't require requests

    built = requests.Session()
    built.headers.update({"Accept": "application/json"})
    return built


def _check_av_error(payload: Dict[str, Any]) -> None:
    """Raise a clear error for Alpha Vantage's soft-error envelopes (rate limit / bad key)."""
    for key in ("Error Message", "Note", "Information"):
        if key in payload:
            raise ValueError("Alpha Vantage: " + str(payload[key]))


class AlphaVantageSource:
    """Daily OHLCV history for one symbol via ``TIME_SERIES_DAILY``."""

    def __init__(self, symbol: str, api_key: str, output_size: str = "compact", session=None):
        """
        :param symbol: Ticker (e.g. ``"IBM"``).
        :param api_key: Alpha Vantage API key.
        :param output_size: ``compact`` (last 100 rows, default) or ``full`` (20+ years).
        :param session: Inject a prebuilt ``requests.Session`` (used in tests).
        """
        self.symbol = symbol
        self.api_key = api_key
        self.output_size = output_size
        self._session = session

    def read(self) -> Tuple[Sequence[str], Sequence[Sequence[Any]]]:
        """Return ``(header, rows)`` — one row per trading day, ascending by date."""
        params = {"function": "TIME_SERIES_DAILY", "symbol": self.symbol,
                  "outputsize": self.output_size, "apikey": self.api_key}
        response = _session(self._session).get(_HOST, params=params, timeout=60)
        response.raise_for_status()
        payload = response.json()
        _check_av_error(payload)
        series = payload.get("Time Series (Daily)") or {}

        header = ["Date", "Open", "High", "Low", "Close", "Volume"]
        rows: List[List[Any]] = []
        for date in sorted(series.keys()):
            bar = series[date]
            rows.append([
                date,
                float(bar.get("1. open", 0)),
                float(bar.get("2. high", 0)),
                float(bar.get("3. low", 0)),
                float(bar.get("4. close", 0)),
                float(bar.get("5. volume", 0)),
            ])
        return header, rows


class AlphaVantageOptionsSource:
    """A full option chain via ``HISTORICAL_OPTIONS`` (all expirations for a symbol/date)."""

    _FIELDS = ("contractID", "type", "strike", "expiration",
               "last", "mark", "bid", "ask", "volume", "open_interest", "implied_volatility")

    def __init__(self, symbol: str, api_key: str, date: Optional[str] = None, session=None):
        """
        :param symbol: Underlying ticker (e.g. ``"IBM"``).
        :param api_key: Alpha Vantage API key.
        :param date: Optional trading date ``YYYY-MM-DD`` for the chain snapshot; the latest
            available session is used when omitted.
        :param session: Inject a prebuilt ``requests.Session`` (used in tests).
        """
        self.symbol = symbol
        self.api_key = api_key
        self.date = date
        self._session = session

    def _fetch(self) -> List[Dict[str, Any]]:
        params = {"function": "HISTORICAL_OPTIONS", "symbol": self.symbol, "apikey": self.api_key}
        if self.date:
            params["date"] = self.date
        response = _session(self._session).get(_HOST, params=params, timeout=60)
        response.raise_for_status()
        payload = response.json()
        _check_av_error(payload)
        return payload.get("data", []) or []

    def read_contracts(self) -> List[Dict[str, Any]]:
        """Return the raw list of contract dicts (used by the OptionsWall reshaper)."""
        return self._fetch()

    def read(self) -> Tuple[Sequence[str], Sequence[Sequence[Any]]]:
        """Return ``(header, rows)`` — one flat row per contract, ``contractID`` first."""
        records = self._fetch()
        header = list(self._FIELDS)
        rows = [[record.get(name, "") for name in header] for record in records]
        return header, rows
