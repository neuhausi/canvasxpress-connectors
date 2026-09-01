"""Yahoo Finance data sources — end-of-day prices and end-of-day option chains.

Two read-only ``DataSource`` classes over Yahoo's public JSON endpoints, each returning
``(header, rows)`` for ``reshape.rows_to_cx``:

* :class:`YahooFinanceSource` — daily OHLCV history from the **chart** endpoint
  (``/v8/finance/chart/<symbol>``). First column is the trading date, so it becomes the
  CanvasXpress sample axis; Open/High/Low/Close/Volume become numeric variables.
* :class:`YahooOptionsSource` — an **EOD option chain** from the **options** endpoint
  (``/v7/finance/options/<symbol>``): calls and puts for one expiration, flattened into one
  row per contract (``contractSymbol`` first, so it is the sample axis).

Both issue only ``GET`` and take no credentials — Yahoo Finance has no official public API,
so these endpoints are unofficial, rate-limited, and can change without notice. Some regions
now require a cookie + ``crumb`` on ``/v7`` requests; inject a pre-authorized
``requests.Session`` via ``session=`` if you hit ``401``/``Unauthorized``. For anything
beyond a demo, prefer a keyed provider (Alpha Vantage, Finnhub, Polygon) behind the same seam.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, List, Optional, Sequence, Tuple

# A browser-like UA — Yahoo rejects the default requests UA on some endpoints.
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def _epoch_to_date(epoch: Any) -> str:
    """Convert a UNIX epoch (seconds) to an ISO ``YYYY-MM-DD`` date string.

    :param epoch: Seconds since the epoch, or ``None``.
    :returns: The UTC date as ``YYYY-MM-DD``, or ``""`` when ``epoch`` is falsy.
    """
    if not epoch:
        return ""
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime("%Y-%m-%d")


def _new_session(session):
    """Return the injected session, or lazily build a browser-UA ``requests.Session``.

    :param session: An injected session (used in tests), or ``None``.
    :returns: A session whose ``get`` mirrors ``requests.Session.get``.
    """
    if session is not None:
        return session
    # Lazy import so the core package doesn't require requests.
    import requests

    built = requests.Session()
    built.headers.update({"User-Agent": _USER_AGENT, "Accept": "application/json"})
    return built


class YahooFinanceSource:
    """Daily OHLCV price history for one symbol, reshaped to ``(header, rows)``."""

    _HOST = "https://query1.finance.yahoo.com/v8/finance/chart/"

    def __init__(self, symbol: str, range: str = "1y", interval: str = "1d",
                 include_adjclose: bool = True, session=None):
        """
        :param symbol: Ticker (e.g. ``"AAPL"``, ``"^GSPC"``, ``"BTC-USD"``).
        :param range: Look-back window Yahoo accepts — ``1d``, ``5d``, ``1mo``, ``3mo``,
            ``6mo``, ``1y``, ``2y``, ``5y``, ``10y``, ``ytd`` or ``max``.
        :param interval: Candle size — ``1d`` (default, EOD), ``1wk`` or ``1mo``.
        :param include_adjclose: Add a split/dividend-adjusted ``Adj Close`` column.
        :param session: Inject a prebuilt ``requests.Session`` (used in tests); when ``None``
            one is built lazily with a browser User-Agent.
        """
        self.symbol = symbol
        self.range = range
        self.interval = interval
        self.include_adjclose = include_adjclose
        self._session = session

    def read(self) -> Tuple[Sequence[str], Sequence[Sequence[Any]]]:
        """Fetch the price history and return ``(header, rows)`` — one row per trading day."""
        session = _new_session(self._session)
        params = {"range": self.range, "interval": self.interval}
        response = session.get(self._HOST + self.symbol, params=params, timeout=60)
        response.raise_for_status()

        results = (response.json().get("chart") or {}).get("result") or []
        if not results:
            return [], []
        result = results[0]

        timestamps = result.get("timestamp") or []
        quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
        adjclose = None
        if self.include_adjclose:
            adj = (result.get("indicators") or {}).get("adjclose") or []
            if adj:
                adjclose = adj[0].get("adjclose")

        header: List[str] = ["Date", "Open", "High", "Low", "Close"]
        if adjclose is not None:
            header.append("Adj Close")
        header.append("Volume")

        opens = quote.get("open") or []
        highs = quote.get("high") or []
        lows = quote.get("low") or []
        closes = quote.get("close") or []
        volumes = quote.get("volume") or []

        rows: List[List[Any]] = []
        for i, ts in enumerate(timestamps):
            # Yahoo pads incomplete candles with nulls — skip a day with no close.
            if i >= len(closes) or closes[i] is None:
                continue
            row: List[Any] = [_epoch_to_date(ts),
                              opens[i] if i < len(opens) else None,
                              highs[i] if i < len(highs) else None,
                              lows[i] if i < len(lows) else None,
                              closes[i]]
            if adjclose is not None:
                row.append(adjclose[i] if i < len(adjclose) else None)
            row.append(volumes[i] if i < len(volumes) else None)
            rows.append(row)
        return header, rows


class YahooOptionsSource:
    """An EOD option chain (calls + puts) for one expiration, reshaped to ``(header, rows)``."""

    _HOST = "https://query1.finance.yahoo.com/v7/finance/options/"

    # Contract fields pulled from each call/put record, in output order. ``contractSymbol``
    # is first so it becomes the CanvasXpress sample axis; ``type`` and the dates are
    # non-numeric annotations, the rest are numeric variables.
    _FIELDS = ("contractSymbol", "type", "strike", "expiration", "lastTradeDate",
               "lastPrice", "bid", "ask", "change", "percentChange",
               "volume", "openInterest", "impliedVolatility")
    _DATE_FIELDS = ("expiration", "lastTradeDate")

    def __init__(self, symbol: str, expiration: Optional[int] = None,
                 kinds: Sequence[str] = ("calls", "puts"), session=None):
        """
        :param symbol: Underlying ticker (e.g. ``"AAPL"``).
        :param expiration: Expiration as a UNIX epoch (seconds); ``None`` returns Yahoo's
            nearest expiration. Valid epochs are listed as ``expirationDates`` on any response.
        :param kinds: Which sides to include — any of ``"calls"`` and ``"puts"``.
        :param session: Inject a prebuilt ``requests.Session`` (used in tests); when ``None``
            one is built lazily with a browser User-Agent.
        """
        self.symbol = symbol
        self.expiration = expiration
        self.kinds = tuple(kinds)
        self._session = session

    def read(self) -> Tuple[Sequence[str], Sequence[Sequence[Any]]]:
        """Fetch the option chain and return ``(header, rows)`` — one row per contract."""
        session = _new_session(self._session)
        params = {}
        if self.expiration is not None:
            params["date"] = str(self.expiration)
        response = session.get(self._HOST + self.symbol, params=params, timeout=60)
        response.raise_for_status()

        results = (response.json().get("optionChain") or {}).get("result") or []
        if not results:
            return [], []
        chains = results[0].get("options") or []
        if not chains:
            return list(self._FIELDS), []
        chain = chains[0]

        header = list(self._FIELDS)
        rows: List[List[Any]] = []
        for kind in self.kinds:
            for contract in chain.get(kind, []):
                rows.append(self._contract_row(contract, kind))
        return header, rows

    def _contract_row(self, contract: dict, kind: str) -> List[Any]:
        """Flatten one option contract into a row aligned with ``_FIELDS``.

        :param contract: A single call/put record from the chain.
        :param kind: ``"calls"`` or ``"puts"`` — recorded as a singular ``type`` cell.
        :returns: One row of cell values in ``_FIELDS`` order (epochs rendered as dates).
        """
        row: List[Any] = []
        for field in self._FIELDS:
            if field == "type":
                row.append("call" if kind == "calls" else "put")
            elif field in self._DATE_FIELDS:
                row.append(_epoch_to_date(contract.get(field)))
            else:
                row.append(contract.get(field))
        return row
