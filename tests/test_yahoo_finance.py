"""Tests for the Yahoo Finance sources (EOD prices + EOD option chains).

Both hit public JSON endpoints, so we test reshape and request-building through an injected
fake requests.Session — the same approach as the other REST sources.
"""

from cx_connectors.sources.base import to_cx
from cx_connectors.sources.yahoo_finance import (
    YahooFinanceSource,
    YahooOptionsSource,
    _epoch_to_date,
)


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeSession:
    """Mimics requests.Session.get(url, ...) -> response."""
    def __init__(self, payload):
        self._payload = payload
        self.headers = {}

    def get(self, url, params=None, timeout=None):
        self.url, self.params = url, params
        return _FakeResponse(self._payload)


# 2024-01-02 and 2024-01-03 (UTC).
_CHART = {
    "chart": {
        "result": [
            {
                "timestamp": [1704153600, 1704240000],
                "indicators": {
                    "quote": [
                        {
                            "open": [10.0, 11.0],
                            "high": [12.0, 13.0],
                            "low": [9.0, 10.5],
                            "close": [11.5, 12.5],
                            "volume": [1000, 2000],
                        }
                    ],
                    "adjclose": [{"adjclose": [11.4, 12.4]}],
                },
            }
        ],
        "error": None,
    }
}


def test_epoch_to_date():
    assert _epoch_to_date(1704153600) == "2024-01-02"
    assert _epoch_to_date(None) == ""


def test_price_history_reshapes_to_cx():
    session = _FakeSession(_CHART)
    cx = to_cx(YahooFinanceSource("AAPL", range="5d", session=session))
    assert cx["y"]["smps"] == ["2024-01-02", "2024-01-03"]        # Date is the sample axis
    assert cx["y"]["vars"] == ["Open", "High", "Low", "Close", "Adj Close", "Volume"]
    assert cx["y"]["data"][3] == [11.5, 12.5]                     # Close row
    assert session.url.endswith("/v8/finance/chart/AAPL")
    assert session.params == {"range": "5d", "interval": "1d"}


def test_price_history_skips_incomplete_candle():
    payload = {
        "chart": {"result": [{
            "timestamp": [1704153600, 1704240000],
            "indicators": {"quote": [{
                "open": [10.0, 11.0], "high": [12.0, 13.0], "low": [9.0, 10.5],
                "close": [11.5, None], "volume": [1000, 2000],
            }]},
        }]}
    }
    header, rows = YahooFinanceSource("AAPL", include_adjclose=False,
                                      session=_FakeSession(payload)).read()
    assert header == ["Date", "Open", "High", "Low", "Close", "Volume"]
    assert len(rows) == 1                                          # null-close day dropped


_OPTIONS = {
    "optionChain": {
        "result": [
            {
                "underlyingSymbol": "AAPL",
                "expirationDates": [1705536000],
                "options": [
                    {
                        "expirationDate": 1705536000,
                        "calls": [
                            {"contractSymbol": "AAPL240118C00190000", "strike": 190.0,
                             "expiration": 1705536000, "lastTradeDate": 1705190400,
                             "lastPrice": 5.1, "bid": 5.0, "ask": 5.2, "change": 0.1,
                             "percentChange": 2.0, "volume": 300, "openInterest": 1200,
                             "impliedVolatility": 0.25},
                        ],
                        "puts": [
                            {"contractSymbol": "AAPL240118P00190000", "strike": 190.0,
                             "expiration": 1705536000, "lastTradeDate": 1705190400,
                             "lastPrice": 4.0, "bid": 3.9, "ask": 4.1, "change": -0.2,
                             "percentChange": -4.5, "volume": 250, "openInterest": 900,
                             "impliedVolatility": 0.27},
                        ],
                    }
                ],
            }
        ],
        "error": None,
    }
}


def test_option_chain_flattens_calls_and_puts():
    session = _FakeSession(_OPTIONS)
    header, rows = YahooOptionsSource("AAPL", expiration=1705536000, session=session).read()
    assert header[0] == "contractSymbol" and "type" in header
    assert len(rows) == 2                                          # one call + one put
    call, put = rows
    assert call[header.index("type")] == "call"
    assert put[header.index("type")] == "put"
    assert call[header.index("expiration")] == "2024-01-18"        # epoch rendered as date
    assert session.url.endswith("/v7/finance/options/AAPL")
    assert session.params == {"date": "1705536000"}


def test_option_chain_reshapes_to_cx():
    cx = to_cx(YahooOptionsSource("AAPL", session=_FakeSession(_OPTIONS)))
    assert cx["y"]["smps"] == ["AAPL240118C00190000", "AAPL240118P00190000"]
    assert "strike" in cx["y"]["vars"]                             # numeric -> variable
    assert cx["x"]["type"] == ["call", "put"]                     # non-numeric -> annotation
