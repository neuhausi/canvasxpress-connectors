"""Tests for the OptionsWall data path: Stooq + Alpha Vantage sources and the reshaper.

All network access is mocked with an injected fake requests.Session (the same approach the
other REST sources use), so these run offline / on IP-blocked hosts.
"""

from cx_connectors.sources.stooq import StooqSource
from cx_connectors.sources.alphavantage import AlphaVantageSource, AlphaVantageOptionsSource
from cx_connectors.options_wall import build_options_wall, fetch_options_wall


class _CsvResponse:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


class _JsonResponse:
    def __init__(self, payload):
        self._payload = payload
        self.text = "{}"

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeSession:
    """A get() that returns queued responses; records the params of each call."""
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []
        self.headers = {}

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params})
        return self._responses.pop(0)


# ---- Stooq --------------------------------------------------------------------------------
_STOOQ_CSV = "Date,Open,High,Low,Close,Volume\n2026-06-01,227.5,228.3,226.1,227.6,3100000\n2026-06-02,227.6,229.0,227.0,228.8,2800000\n"


def test_stooq_parses_csv_and_adds_us_suffix():
    s = StooqSource("IBM", session=_FakeSession([_CsvResponse(_STOOQ_CSV)]))
    header, rows = s.read()
    assert s.symbol == "ibm.us"
    assert header == ["Date", "Open", "High", "Low", "Close", "Volume"]
    assert rows[0][0] == "2026-06-01" and rows[0][4] == 227.6
    assert rows[1][4] == 228.8


def test_stooq_raises_on_antibot_html():
    import pytest
    s = StooqSource("IBM", session=_FakeSession([_CsvResponse("<!DOCTYPE html><html>challenge</html>")]))
    with pytest.raises(ValueError):
        s.read()


# ---- Alpha Vantage -------------------------------------------------------------------------
def test_alphavantage_prices_sorted_ascending():
    payload = {"Time Series (Daily)": {
        "2026-06-02": {"1. open": "227.6", "2. high": "229", "3. low": "227", "4. close": "228.8", "5. volume": "2800000"},
        "2026-06-01": {"1. open": "227.5", "2. high": "228.3", "3. low": "226.1", "4. close": "227.6", "5. volume": "3100000"},
    }}
    header, rows = AlphaVantageSource("IBM", "KEY", session=_FakeSession([_JsonResponse(payload)])).read()
    assert [r[0] for r in rows] == ["2026-06-01", "2026-06-02"]   # ascending
    assert rows[-1][4] == 228.8


def test_alphavantage_rate_limit_raises():
    import pytest
    s = AlphaVantageSource("IBM", "KEY", session=_FakeSession([_JsonResponse({"Note": "rate limit"})]))
    with pytest.raises(ValueError):
        s.read()


def test_alphavantage_options_contracts():
    payload = {"data": [
        {"type": "call", "strike": "240", "expiration": "2026-10-17", "mark": "6.5", "implied_volatility": "0.22", "volume": "3000"},
        {"type": "put", "strike": "240", "expiration": "2026-10-17", "mark": "6.3", "implied_volatility": "0.24", "volume": "2500"},
    ]}
    contracts = AlphaVantageOptionsSource("IBM", "KEY", session=_FakeSession([_JsonResponse(payload)])).read_contracts()
    assert len(contracts) == 2 and contracts[0]["type"] == "call"


# ---- reshaper -----------------------------------------------------------------------------
_PRICE_HEADER = ["Date", "Open", "High", "Low", "Close"]
_PRICE_ROWS = [["2026-06-01", 227.5, 228.3, 226.1, 240.0], ["2026-06-02", 240.0, 241, 239, 243.0]]
_CONTRACTS = [
    {"type": "call", "strike": 235, "expiration": "2026-10-17", "mark": 12.0, "implied_volatility": 0.25, "volume": 1000},
    {"type": "put", "strike": 235, "expiration": "2026-10-17", "mark": 3.0, "implied_volatility": 0.27, "volume": 800},
    {"type": "call", "strike": 250, "expiration": "2026-10-17", "mark": 4.0, "implied_volatility": 0.30, "volume": 600},
    {"type": "put", "strike": 250, "expiration": "2026-10-17", "mark": 9.0, "implied_volatility": 0.31, "volume": 500},
    # a far-out strike that should be dropped by the ±15% window around spot 243
    {"type": "call", "strike": 400, "expiration": "2026-10-17", "mark": 0.1, "implied_volatility": 0.9, "volume": 1},
    # a different expiry, should be excluded once 2026-10-17 is chosen
    {"type": "call", "strike": 240, "expiration": "2027-01-15", "mark": 20, "implied_volatility": 0.2, "volume": 9},
]


def test_build_options_wall_shape_and_filtering():
    obj = build_options_wall(_PRICE_HEADER, _PRICE_ROWS, _CONTRACTS, symbol="IBM", expiry="2026-10-17")
    cfg = obj["config"]
    assert cfg["graphType"] == "OptionsWall"
    assert obj["data"]["y"]["vars"] == ["Open", "High", "Low", "Close"]
    assert obj["data"]["y"]["smps"] == ["2026-06-01", "2026-06-02"]
    assert cfg["optionsWallSpot"] == 243.0                       # last close
    assert cfg["optionsWallExpiry"] == "2026-10-17"
    chain = cfg["optionsWallChain"]
    assert chain["strikes"] == [235, 250]                        # 400 dropped (window), 240@other-expiry excluded
    assert chain["call"]["premium"] == [12.0, 4.0]
    assert chain["put"]["iv"] == [0.27, 0.31]
    assert chain["call"]["volume"] == [1000, 600]


def test_fetch_options_wall_alphavantage_wires_both_sources():
    prices = {"Time Series (Daily)": {
        "2026-06-01": {"1. open": "227.5", "2. high": "228.3", "3. low": "226.1", "4. close": "240.0", "5. volume": "1"},
        "2026-06-02": {"1. open": "240", "2. high": "241", "3. low": "239", "4. close": "243.0", "5. volume": "1"},
    }}
    options = {"data": _CONTRACTS}
    session = _FakeSession([_JsonResponse(prices), _JsonResponse(options)])
    obj = fetch_options_wall("IBM", api_key="KEY", provider="alphavantage", session=session, expiry="2026-10-17")
    assert obj["config"]["optionsWallChain"]["strikes"] == [235, 250]
    assert session.calls[0]["params"]["function"] == "TIME_SERIES_DAILY"
    assert session.calls[1]["params"]["function"] == "HISTORICAL_OPTIONS"


def test_fetch_stooq_requires_option_contracts():
    import pytest
    with pytest.raises(ValueError):
        fetch_options_wall("IBM", provider="stooq", session=_FakeSession([_CsvResponse(_STOOQ_CSV)]))


# ---- Nasdaq option chain -------------------------------------------------------------------
def test_nasdaq_flattens_calls_and_puts_and_parses_expiry():
    from cx_connectors.sources.nasdaq import NasdaqOptionsSource
    payload = {"data": {"table": {"rows": [
        {"expirygroup": "September 18, 2026", "strike": None},
        {"strike": "230", "c_Last": "8.5", "c_Bid": "8.4", "c_Ask": "8.6", "c_Volume": "10", "c_Openinterest": "5354",
         "p_Last": "4.7", "p_Bid": "4.6", "p_Ask": "4.8", "p_Volume": "8", "p_Openinterest": "3788"},
    ]}}}

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return payload

    class _Sess:
        headers = {}
        def get(self, url, params=None, timeout=None): return _Resp()

    contracts = NasdaqOptionsSource("IBM", session=_Sess()).read_contracts()
    assert len(contracts) == 2
    call = next(c for c in contracts if c["type"] == "call")
    assert call["strike"] == 230.0 and call["expiration"] == "2026-09-18"
    assert call["premium"] == 8.5 and call["open_interest"] == 5354.0
    put = next(c for c in contracts if c["type"] == "put")
    assert put["premium"] == 4.7 and put["expiration"] == "2026-09-18"


# ---- implied volatility (computed from premium) --------------------------------------------
def test_iv_computed_from_premium_otm_only():
    # spot 100; a call/put at several strikes with NO iv in the feed -> IV computed for OTM only.
    rows = [["2026-06-01", 99, 101, 98, 100.0]]
    contracts = [
        {"type": "call", "strike": 90, "expiration": "2026-07-17", "mark": 11.0},   # ITM call -> no IV
        {"type": "call", "strike": 110, "expiration": "2026-07-17", "mark": 2.0},    # OTM call -> IV
        {"type": "put", "strike": 110, "expiration": "2026-07-17", "mark": 11.5},    # ITM put -> no IV
        {"type": "put", "strike": 90, "expiration": "2026-07-17", "mark": 1.5},      # OTM put -> IV
    ]
    obj = build_options_wall(["Date", "Open", "High", "Low", "Close"], rows, contracts,
                             symbol="X", expiry="2026-07-17", strike_pad=0.3)
    chain = obj["config"]["optionsWallChain"]
    ci = dict(zip(chain["strikes"], chain["call"]["iv"]))
    pi = dict(zip(chain["strikes"], chain["put"]["iv"]))
    assert ci[90] is None and ci[110] is not None and 0.05 < ci[110] < 1.0   # OTM call IV only
    assert pi[110] is None and pi[90] is not None and 0.05 < pi[90] < 1.0     # OTM put IV only
    # premium is always present regardless
    assert chain["call"]["premium"][chain["strikes"].index(110)] == 2.0


# ---- multi-expiry (dashboard slider) --------------------------------------------------------
def test_build_options_wall_multi_groups_by_expiry():
    from cx_connectors.options_wall import build_options_wall_multi
    rows = [["2026-06-01", 99, 101, 98, 100.0]]
    contracts = []
    for exp in ("2026-07-17", "2026-08-21"):
        for k in (95, 100, 105):
            contracts.append({"type": "call", "strike": k, "expiration": exp, "mark": max(0.5, 100 - k + 3)})
            contracts.append({"type": "put", "strike": k, "expiration": exp, "mark": max(0.5, k - 100 + 3)})
    multi = build_options_wall_multi(["Date", "Open", "High", "Low", "Close"], rows, contracts,
                                     symbol="X", min_contracts=2, max_expiries=5)
    assert multi["expiries"] == ["2026-07-17", "2026-08-21"]        # ascending
    assert set(multi["chains"].keys()) == set(multi["expiries"])
    assert multi["spot"] == 100.0
    assert multi["data"]["y"]["smps"] == ["2026-06-01"]            # shared price panel
    assert multi["chains"]["2026-07-17"]["strikes"]                # each expiry has its own chain


def test_build_options_wall_multi_calendar_keeps_weeklies():
    from cx_connectors.options_wall import build_options_wall_multi
    rows = [["2026-08-28", 99, 101, 98, 100.0]]
    # weekly Fridays for ~10 weeks, each liquid enough
    weeks = ["2026-09-04","2026-09-11","2026-09-18","2026-09-25","2026-10-02","2026-10-09",
             "2026-10-16","2026-11-20","2026-12-18","2027-03-19"]
    contracts = []
    for exp in weeks:
        for k in (95, 100, 105):
            contracts.append({"type":"call","strike":k,"expiration":exp,"mark":3.0})
            contracts.append({"type":"put","strike":k,"expiration":exp,"mark":3.0})
    liquid = build_options_wall_multi(hdr_ohlc(), rows, contracts, symbol="X",
                                      select="liquid", min_contracts=2, max_expiries=6)
    cal = build_options_wall_multi(hdr_ohlc(), rows, contracts, symbol="X",
                                   select="calendar", near_days=56, far_gap_days=25,
                                   min_contracts=2, max_expiries=14)
    # calendar keeps the near-term weeklies within 56 days of 2026-08-28
    assert "2026-09-11" in cal["expiries"] and "2026-09-25" in cal["expiries"]
    # far tail thinned to >= 25-day spacing (11-20 kept, but not two dates < 25d apart in the tail)
    assert cal["expiries"] == sorted(cal["expiries"])
    assert len(cal["expiries"]) >= 8


def hdr_ohlc():
    return ["Date", "Open", "High", "Low", "Close"]
