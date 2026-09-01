"""Reshape prices + an option chain into a CanvasXpress **OptionsWall** object.

``graphType: "OptionsWall"`` renders daily candlesticks flanked by the put/call option chains
for one expiry (see the canvasXpress OptionsWall type). This module turns a price series
``(header, rows)`` plus a list of option-contract dicts into the exact ``{data, config}`` that
type consumes — provider-agnostic (Alpha Vantage and Yahoo field names both handled) — and
offers :func:`fetch_options_wall`, a one-call convenience that wires the connector sources.

    from cx_connectors.options_wall import fetch_options_wall
    obj = fetch_options_wall("IBM", api_key="YOUR_ALPHAVANTAGE_KEY")
    # -> {"data": {...}, "config": {"graphType": "OptionsWall", ...}}  (JSON-ready)
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple


# ---- Black-Scholes implied volatility (to derive IV from real premiums when a feed lacks it) --
def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs_price(spot: float, strike: float, t: float, rate: float, sigma: float, kind: str) -> float:
    """Black-Scholes European option price."""
    if t <= 0 or sigma <= 0:
        return max(0.0, (spot - strike) if kind == "call" else (strike - spot))
    sqrt_t = math.sqrt(t)
    d1 = (math.log(spot / strike) + (rate + 0.5 * sigma * sigma) * t) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    if kind == "call":
        return spot * _norm_cdf(d1) - strike * math.exp(-rate * t) * _norm_cdf(d2)
    return strike * math.exp(-rate * t) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def _implied_vol(
    price: Optional[float],
    spot: Optional[float],
    strike: Optional[float],
    t: float,
    rate: float,
    kind: str,
) -> Optional[float]:
    """Solve Black-Scholes for the implied volatility (bisection); None if price is unusable."""
    if (
        price is None
        or spot is None
        or strike is None
        or price <= 0
        or spot <= 0
        or strike <= 0
        or t <= 0
    ):
        return None
    intrinsic = max(0.0, (spot - strike) if kind == "call" else (strike - spot))
    if price < intrinsic - 1e-6:
        return None  # below intrinsic -> no real IV
    lo, hi = 1e-4, 5.0
    for _ in range(64):
        mid = 0.5 * (lo + hi)
        if _bs_price(spot, strike, t, rate, mid, kind) > price:
            hi = mid
        else:
            lo = mid
    return round(0.5 * (lo + hi), 4)


def _year_fraction(from_date: Optional[str], to_date: Optional[str]) -> float:
    """Fraction of a year between two YYYY-MM-DD dates (0 if unparseable)."""
    try:
        d0 = datetime.strptime(str(from_date)[:10], "%Y-%m-%d")
        d1 = datetime.strptime(str(to_date)[:10], "%Y-%m-%d")
        return max(0.0, (d1 - d0).days / 365.0)
    except (ValueError, TypeError):
        return 0.0


# ---- field normalization (Alpha Vantage / Yahoo / generic) ----------------------------------
def _num(value: Any) -> Optional[float]:
    if value in (None, "", "None"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first(record: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in record and record[key] not in (None, ""):
            return record[key]
    return None


def _normalize_contract(record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Map one provider contract dict to ``{type, strike, iv, premium, volume, expiration}``."""
    kind = _first(record, "type", "option_type", "side")
    kind = str(kind).lower() if kind is not None else None
    if kind not in ("call", "put"):
        return None
    strike = _num(_first(record, "strike", "strikePrice", "strike_price"))
    if strike is None:
        return None
    return {
        "type": kind,
        "strike": strike,
        "iv": _num(_first(record, "implied_volatility", "impliedVolatility", "iv")),
        "premium": _num(_first(record, "mark", "last", "lastPrice", "premium", "ask")),
        "volume": _num(_first(record, "volume", "openInterest", "open_interest")) or 0.0,
        "expiration": _first(record, "expiration", "expirationDate", "expiry"),
    }


def _pick_expiry(
    contracts: List[Dict[str, Any]],
    last_date: Optional[str],
    target_days: int = 45,
    window: Tuple[int, int] = (15, 90),
) -> Optional[str]:
    """Choose the most *liquid* expiry (a real options wall wants the monthly, not a thin weekly).

    Prefers the expiry with the most contracts among those roughly ``window`` days out (a proxy
    for the liquid 3rd-Friday monthly); ties break toward ``last_date + target_days``. Falls back
    to nearest-to-target across all expiries when none land in the window.
    """
    base = None
    if last_date:
        try:
            base = datetime.strptime(str(last_date)[:10], "%Y-%m-%d")
        except ValueError:
            base = None

    counts: Dict[str, int] = {}
    days: Dict[str, float] = {}
    for c in contracts:
        exp = c.get("expiration")
        if not exp:
            continue
        counts[exp] = counts.get(exp, 0) + 1
        if exp not in days:
            try:
                days[exp] = (
                    (datetime.strptime(str(exp)[:10], "%Y-%m-%d") - base).days if base else 1e9
                )
            except ValueError:
                days[exp] = 1e9
    if not counts:
        return None

    candidates = [e for e in counts if window[0] <= days.get(e, 1e9) <= window[1]]
    if not candidates:
        candidates = list(counts)
    # most contracts first (liquidity), then nearest to the ~target horizon.
    return sorted(candidates, key=lambda e: (-counts[e], abs(days.get(e, 1e9) - target_days)))[0]


def _price_columns(header: Sequence[str]) -> Dict[str, int]:
    """Locate Open/High/Low/Close columns case-insensitively (fallback to positions 1-4)."""
    lut = {str(name).strip().lower(): i for i, name in enumerate(header)}
    return {
        "open": lut.get("open", 1),
        "high": lut.get("high", 2),
        "low": lut.get("low", 3),
        "close": lut.get("close", 4),
    }


def build_options_wall(
    price_header: Sequence[str],
    price_rows: Sequence[Sequence[Any]],
    option_contracts: Sequence[Dict[str, Any]],
    symbol: str = "",
    expiry: Optional[str] = None,
    flank_metric: str = "iv",
    strike_pad: float = 0.15,
    title: Optional[str] = None,
    compute_iv: bool = True,
    risk_free_rate: float = 0.04,
) -> Dict[str, Any]:
    """Build the OptionsWall ``{data, config}`` object.

    :param price_header: Price column names (Date first, then OHLC...).
    :param price_rows: Price rows, ascending by date; ``[date, open, high, low, close, ...]``.
    :param option_contracts: Raw contract dicts from a provider (any of the handled field names).
    :param symbol: Ticker, used only for the default title.
    :param expiry: Force a specific expiry ``YYYY-MM-DD``; auto-picked (~45 days out) when omitted.
    :param flank_metric: ``iv`` (default) or ``premium`` — the flank curve config default.
    :param strike_pad: Keep strikes within ``spot * (1 ± strike_pad)`` (0.15 -> ±15%).
    :param title: Chart title; a sensible default is built when omitted.
    :returns: ``{"data": {"y": {...}}, "config": {"graphType": "OptionsWall", ...}}``.
    :raises ValueError: If there are no price rows.
    """
    if not price_rows:
        raise ValueError("No price rows")

    cols = _price_columns(price_header)
    dates = [str(r[0]) for r in price_rows]
    opens = [_num(r[cols["open"]]) for r in price_rows]
    highs = [_num(r[cols["high"]]) for r in price_rows]
    lows = [_num(r[cols["low"]]) for r in price_rows]
    closes = [_num(r[cols["close"]]) for r in price_rows]
    spot = closes[-1] if closes else None

    normalized = [c for c in (_normalize_contract(x) for x in option_contracts) if c]
    chosen_expiry = expiry or _pick_expiry(normalized, dates[-1] if dates else None)
    in_expiry = (
        [c for c in normalized if str(c["expiration"])[:10] == str(chosen_expiry)[:10]]
        if chosen_expiry
        else normalized
    )

    # Strike window: at least ±strike_pad around spot, but WIDENED to cover the shown price
    # range so the option wall spans the same vertical extent as the candlesticks (otherwise the
    # flanks stop at the strikes while the price history runs past them).
    if spot:
        price_low = min((v for v in lows if v is not None), default=spot)
        price_high = max((v for v in highs if v is not None), default=spot)
        lo = min(spot * (1 - strike_pad), price_low)
        hi = max(spot * (1 + strike_pad), price_high)
        in_expiry = [c for c in in_expiry if lo <= c["strike"] <= hi]

    # Derive IV from the premium (Black-Scholes) when the feed doesn't provide it (e.g. Nasdaq),
    # so the flanks can show the IV smile as well as premium.
    if compute_iv and spot:
        t_years = _year_fraction(dates[-1] if dates else None, chosen_expiry)
        if t_years > 0:
            for c in in_expiry:
                if c.get("iv") is not None:
                    continue
                strike = c.get("strike")
                # IV is only trustworthy from OUT-OF-THE-MONEY options (ITM premium is ~all
                # intrinsic, so the solver returns noise). Calls: K>=spot; puts: K<=spot.
                otm = (c["type"] == "call" and strike is not None and strike >= spot) or (
                    c["type"] == "put" and strike is not None and strike <= spot
                )
                if not otm:
                    continue
                iv = _implied_vol(
                    c.get("premium"), spot, strike, t_years, risk_free_rate, c["type"]
                )
                # drop implausible solutions (deep-OTM pennies / bad quotes)
                c["iv"] = iv if (iv is not None and 0.03 <= iv <= 2.0) else None

    strikes = sorted({c["strike"] for c in in_expiry})
    by_key = {(c["type"], c["strike"]): c for c in in_expiry}

    def side(kind: str) -> Dict[str, List[Any]]:
        premium, iv, volume = [], [], []
        for k in strikes:
            c = by_key.get((kind, k))
            premium.append(c["premium"] if c else None)
            iv.append(c["iv"] if c else None)
            volume.append(c["volume"] if c else 0.0)
        return {"premium": premium, "iv": iv, "volume": volume}

    if title is None:
        title = (symbol + " — options wall").strip(" —") or "Options wall"

    return {
        "data": {
            "y": {
                "vars": ["Open", "High", "Low", "Close"],
                "smps": dates,
                "data": [opens, highs, lows, closes],
            }
        },
        "config": {
            "graphType": "OptionsWall",
            "title": title,
            "optionsWallSpot": round(spot, 2) if spot is not None else False,
            "optionsWallExpiry": chosen_expiry or "",
            "optionsWallFlankMetric": flank_metric,
            "optionsWallChain": {
                "strikes": strikes,
                "expiry": chosen_expiry or "",
                "call": side("call"),
                "put": side("put"),
            },
        },
    }


def build_options_wall_multi(
    price_header: Sequence[str],
    price_rows: Sequence[Sequence[Any]],
    option_contracts: Sequence[Dict[str, Any]],
    symbol: str = "",
    flank_metric: str = "iv",
    strike_pad: float = 0.15,
    max_expiries: int = 10,
    min_contracts: int = 20,
    compute_iv: bool = True,
    risk_free_rate: float = 0.04,
    select: str = "liquid",
    near_days: int = 56,
    far_gap_days: int = 25,
) -> Dict[str, Any]:
    """Build a MULTI-EXPIRY OptionsWall payload for an expiry-slider dashboard.

    Returns the shared price series plus one option chain per expiry, so a UI can slide
    across expiries and swap ``optionsWallChain``/``optionsWallExpiry`` without refetching:

        {
          "symbol", "spot", "flankMetric",
          "data":     {"y": {...OHLC...}},          # shared candlestick panel
          "expiries": ["2026-09-18", "2026-10-16", ...],   # ascending
          "chains":   {"2026-09-18": {strikes, call, put}, ...}
        }

    ``select`` chooses which expiries to include (both keep only expiries with >= ``min_contracts``
    and cap at ``max_expiries``):

    * ``"liquid"`` (default) — rank by liquidity (contract count), keep the top ``max_expiries``.
      Tends to pick the heavily-traded monthlies/LEAPS and drop the thinner weeklies.
    * ``"calendar"`` — a trader's ladder: keep EVERY expiry within ``near_days`` of the last price
      date (so the near-term WEEKLIES appear), then thin the far tail to roughly monthly by keeping
      an expiry only if it is >= ``far_gap_days`` after the previous kept one. Presented ascending.

    :param price_header: Price column names (Date first, then OHLC...).
    :param price_rows: Price rows, ascending by date.
    :param option_contracts: Raw provider contract dicts (any handled field names).
    :param symbol: Ticker (for labels).
    :param flank_metric: ``iv`` | ``premium`` — the flank curve default.
    :param strike_pad / compute_iv / risk_free_rate: Passed through to :func:`build_options_wall`.
    :param max_expiries: Cap on how many expiries to include.
    :param min_contracts: Drop expiries thinner than this.
    :param select: ``"liquid"`` | ``"calendar"`` — expiry selection strategy (see above).
    :param near_days: (calendar) keep all expiries within this many days out (the weekly zone).
    :param far_gap_days: (calendar) minimum spacing between kept expiries beyond the near zone.
    :returns: The multi-expiry payload described above.
    """
    normalized = [c for c in (_normalize_contract(x) for x in option_contracts) if c]
    counts: Dict[str, int] = {}
    for c in normalized:
        exp = c.get("expiration")
        if exp:
            counts[exp] = counts.get(exp, 0) + 1
    eligible = [e for e in counts if counts[e] >= min_contracts]

    if select == "calendar":
        # Date-first ladder: all near-term expiries (weeklies), then ~monthly spacing further out.
        try:
            base = (
                datetime.strptime(str(price_rows[-1][0])[:10], "%Y-%m-%d") if price_rows else None
            )
        except (ValueError, IndexError):
            base = None
        chosen = []
        last_kept = None
        for e in sorted(eligible):
            try:
                d = datetime.strptime(str(e)[:10], "%Y-%m-%d")
            except ValueError:
                continue
            days_out = (d - base).days if base else 0
            if days_out <= near_days or last_kept is None or (d - last_kept).days >= far_gap_days:
                chosen.append(e)
                last_kept = d
            if len(chosen) >= max_expiries:
                break
    else:
        # liquid expiries first, keep the top max_expiries, then present ascending by date.
        eligible.sort(key=lambda e: -counts[e])
        chosen = sorted(eligible[:max_expiries])

    data = None
    spot = None
    chains: Dict[str, Any] = {}
    for exp in chosen:
        obj = build_options_wall(
            price_header,
            price_rows,
            option_contracts,
            symbol=symbol,
            expiry=exp,
            flank_metric=flank_metric,
            strike_pad=strike_pad,
            compute_iv=compute_iv,
            risk_free_rate=risk_free_rate,
        )
        if data is None:
            data = obj["data"]
            spot = obj["config"]["optionsWallSpot"]
        chains[exp] = obj["config"]["optionsWallChain"]

    return {
        "symbol": symbol,
        "spot": spot,
        "flankMetric": flank_metric,
        "data": data
        or {"y": {"vars": ["Open", "High", "Low", "Close"], "smps": [], "data": [[], [], [], []]}},
        "expiries": chosen,
        "chains": chains,
    }


def fetch_options_wall(
    symbol: str,
    api_key: Optional[str] = None,
    provider: str = "alphavantage",
    expiry: Optional[str] = None,
    flank_metric: str = "iv",
    output_size: str = "compact",
    session=None,
    **kwargs,
) -> Dict[str, Any]:
    """Fetch prices + an option chain and build the OptionsWall object in one call.

    ``provider`` selects the data source:

    * ``"alphavantage"`` (default) — prices + options both from Alpha Vantage (needs ``api_key``).
    * ``"stooq"`` — keyless Stooq prices; options must be supplied via ``option_contracts=``.
    * ``"yahoo"`` — Yahoo prices + options (no key, but Yahoo rate-limits/ blocks server IPs).

    :param symbol: Ticker (e.g. ``"IBM"``).
    :param api_key: API key (required for ``alphavantage``).
    :param provider: ``alphavantage`` | ``stooq`` | ``yahoo``.
    :param expiry: Force a specific expiry; auto-picked when omitted.
    :param flank_metric: ``iv`` | ``premium`` config default.
    :param output_size: Alpha Vantage price window (``compact`` | ``full``).
    :param session: Inject a ``requests.Session`` (used in tests).
    :param kwargs: ``option_contracts=`` may be supplied directly (e.g. with ``stooq`` prices).
    :returns: The OptionsWall ``{data, config}`` object.
    """
    option_contracts = kwargs.get("option_contracts")

    if provider == "alphavantage":
        from .sources.alphavantage import AlphaVantageOptionsSource, AlphaVantageSource

        if not api_key:
            raise ValueError("alphavantage provider requires api_key")
        header, rows = AlphaVantageSource(
            symbol, api_key, output_size=output_size, session=session
        ).read()
        if option_contracts is None:
            option_contracts = AlphaVantageOptionsSource(
                symbol, api_key, date=None, session=session
            ).read_contracts()
    elif provider == "nasdaq":
        # The working "real" path: Alpha Vantage daily prices (free key) + Nasdaq option chain
        # (keyless; premium + open-interest, no IV — pair with flank_metric="premium").
        from .sources.alphavantage import AlphaVantageSource
        from .sources.nasdaq import NasdaqOptionsSource

        if not api_key:
            raise ValueError("nasdaq provider uses Alpha Vantage for prices; pass api_key")
        header, rows = AlphaVantageSource(
            symbol, api_key, output_size=output_size, session=session
        ).read()
        if option_contracts is None:
            option_contracts = NasdaqOptionsSource(symbol, session=session).read_contracts()
    elif provider == "stooq":
        from .sources.stooq import StooqSource

        header, rows = StooqSource(symbol, session=session).read()
        if option_contracts is None:
            raise ValueError("stooq provides prices only; pass option_contracts= for the chain")
    elif provider == "yahoo":
        from .sources.yahoo_finance import YahooFinanceSource, YahooOptionsSource

        header, rows = YahooFinanceSource(symbol, session=session).read()
        if option_contracts is None:
            oh, orows = YahooOptionsSource(symbol, session=session).read()
            option_contracts = [dict(zip(oh, r)) for r in orows]
    else:
        raise ValueError("unknown provider: " + str(provider))

    return build_options_wall(
        header,
        rows,
        option_contracts or [],
        symbol=symbol,
        expiry=expiry,
        flank_metric=flank_metric,
    )


def _main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI: write an OptionsWall JSON for a symbol.

    Example:
        python -m cx_connectors.options_wall --symbol IBM \\
            --provider alphavantage --api-key YOUR_KEY --out ibm-optionswall.json
    """
    import argparse
    import json
    import os
    import sys

    # Optional convenience: load a local .env (searched upward from the CWD) so
    # ALPHAVANTAGE_API_KEY is picked up automatically when run from the repo.
    try:
        from dotenv import find_dotenv, load_dotenv

        load_dotenv(find_dotenv(usecwd=True))
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="Build a CanvasXpress OptionsWall JSON.")
    parser.add_argument("--symbol", required=True, help="Ticker, e.g. IBM")
    parser.add_argument(
        "--provider", default="nasdaq", choices=["nasdaq", "alphavantage", "stooq", "yahoo"]
    )
    parser.add_argument(
        "--api-key",
        dest="api_key",
        default=None,
        help="Provider API key (falls back to $ALPHAVANTAGE_API_KEY / .env)",
    )
    parser.add_argument("--expiry", default=None, help="Force an expiry YYYY-MM-DD")
    parser.add_argument(
        "--flank-metric", dest="flank_metric", default="iv", choices=["iv", "premium"]
    )
    parser.add_argument(
        "--output-size", dest="output_size", default="compact", choices=["compact", "full"]
    )
    parser.add_argument("--out", default=None, help="Output file (default: stdout)")
    args = parser.parse_args(argv)

    api_key = args.api_key or os.environ.get("ALPHAVANTAGE_API_KEY")
    obj = fetch_options_wall(
        args.symbol,
        api_key=api_key,
        provider=args.provider,
        expiry=args.expiry,
        flank_metric=args.flank_metric,
        output_size=args.output_size,
    )
    text = json.dumps(obj, indent=2)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text + "\n")
        print("wrote " + args.out, file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
