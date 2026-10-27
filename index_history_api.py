"""
index_history_api.py - price history for an index, for the chart that opens when you tap an index.

    GET /indices/history?name=NIFTY 50&period=1M      period: 1W 1M 3M 6M 1Y 5Y

Source: Yahoo Finance via yfinance (the same source /indices uses). Cached 10 minutes per
(index, period); on failure the last good answer (up to 6 hours old) is served, else 503.
"""
import logging
import time
from typing import Callable, Optional

from fastapi import HTTPException

log = logging.getLogger("index_history_api")

# same names and symbols as /indices in api.py
INDICES = {
    "NIFTY 50": "^NSEI", "SENSEX": "^BSESN", "BANK NIFTY": "^NSEBANK", "NIFTY IT": "^CNXIT",
    "NIFTY AUTO": "^CNXAUTO", "NIFTY PHARMA": "^CNXPHARMA", "NIFTY FMCG": "^CNXFMCG",
    "NIFTY METAL": "^CNXMETAL",
}
PERIODS = {            # label -> (yfinance period, interval)
    "1W": ("5d", "1d"), "1M": ("1mo", "1d"), "3M": ("3mo", "1d"),
    "6M": ("6mo", "1d"), "1Y": ("1y", "1d"), "5Y": ("5y", "1wk"),
}
FRESH_S = 600
STALE_S = 6 * 3600
_cache = {}


def fetch_yahoo(symbol: str, period: str, interval: str):
    """Returns a list of (date 'YYYY-MM-DD', close) oldest first."""
    import yfinance as yf
    df = yf.Ticker(symbol).history(period=period, interval=interval, auto_adjust=False)
    if df is None or df.empty or "Close" not in df:
        return []
    out = []
    for ts, close in df["Close"].dropna().items():
        try:
            out.append((ts.strftime("%Y-%m-%d"), float(close)))
        except (AttributeError, ValueError, TypeError):
            continue
    return out


def build(name: str, period: str, series: list) -> dict:
    pts = [(d, c) for d, c in series if c is not None and c == c and c > 0]
    if len(pts) < 2:
        raise ValueError("not enough price history")
    closes = [c for _, c in pts]
    first, last = closes[0], closes[-1]
    return {
        "name": name, "period": period,
        "points": [{"d": d, "c": round(c, 2)} for d, c in pts],
        "first": round(first, 2), "last": round(last, 2),
        "change": round(last - first, 2), "change_pct": round((last - first) / first * 100, 2),
        "high": round(max(closes), 2), "low": round(min(closes), 2),
    }


def get_history(name: str, period: str = "1M", fetcher: Optional[Callable] = None,
                now: Optional[float] = None) -> dict:
    key_name = (name or "").strip().upper()
    period = (period or "1M").strip().upper()
    if key_name not in INDICES:
        raise HTTPException(400, "name must be one of: " + ", ".join(INDICES))
    if period not in PERIODS:
        raise HTTPException(400, "period must be one of: " + ", ".join(PERIODS))
    fetcher = fetcher or fetch_yahoo
    t = time.time() if now is None else now
    key = (key_name, period)
    hit = _cache.get(key)
    if hit and t - hit["at"] < FRESH_S:
        return dict(hit["data"], stale=False)
    try:
        yp, yi = PERIODS[period]
        data = build(key_name, period, fetcher(INDICES[key_name], yp, yi))
    except Exception as e:
        log.warning("index history %s %s failed: %s", key_name, period, e)
        if hit and t - hit["at"] < STALE_S:
            return dict(hit["data"], stale=True)
        raise HTTPException(503, "Price history is unavailable right now")
    _cache[key] = {"at": t, "data": data}
    return dict(data, stale=False)


def register_index_history(app):
    @app.get("/indices/history")
    def indices_history(name: str = "NIFTY 50", period: str = "1M"):
        return get_history(name, period)
