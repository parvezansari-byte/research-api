"""
movers_api.py - top gainers and losers for the Markets screen.

    GET /market/movers?index=NIFTY 500&n=10
    GET /market/index-stocks?index=NIFTY 50       every stock in the index, best first

Source: NSE's equity-stockIndices feed (live price and % change for each index member).
Public route. Cached per index for 3 minutes; if NSE fails the last good answer (up to
1 hour old) is served, otherwise 503 with the reason.
"""
import csv
import logging
import os
import time
from urllib.parse import quote
from typing import Callable, Optional

import requests
from fastapi import HTTPException

log = logging.getLogger("movers_api")

HERE = os.path.dirname(os.path.abspath(__file__))
NSE_HOME = "https://www.nseindia.com"
NSE_URL = NSE_HOME + "/api/equity-stockIndices"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": NSE_HOME + "/market-data/live-equity-market",
}
INDICES = ["NIFTY 50", "NIFTY NEXT 50", "NIFTY MIDCAP 100", "NIFTY SMALLCAP 100", "NIFTY 500"]
DEFAULT_INDEX = "NIFTY 500"
MAX_N = 25
FRESH_S = 180
STALE_S = 3600
_cache = {}          # index -> {"at": float, "data": dict}
_names = None


class MoversError(Exception):
    pass


def _num(v) -> Optional[float]:
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def company_names() -> dict:
    global _names
    if _names is None:
        _names = {}
        try:
            with open(os.path.join(HERE, "nse_allequities.csv"), encoding="utf-8-sig") as fh:
                for row in csv.DictReader(fh):
                    sym = (row.get("SYMBOL") or "").strip().upper()
                    if sym:
                        _names[sym] = (row.get("NAME OF COMPANY") or "").strip()
        except OSError:
            pass
    return _names


def parse_movers(payload, index: str, n: int = 10) -> dict:
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise MoversError("unexpected response from NSE")
    names = company_names()
    stocks = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        sym = str(r.get("symbol") or "").strip().upper()
        if not sym or sym == index.upper() or r.get("priority") in (1, "1"):
            continue                                   # the first row is the index itself
        price, pct, chg = _num(r.get("lastPrice")), _num(r.get("pChange")), _num(r.get("change"))
        if price is None or pct is None or price <= 0:
            continue
        stocks.append({"symbol": sym, "name": names.get(sym, sym), "price": round(price, 2),
                       "change": None if chg is None else round(chg, 2), "change_pct": round(pct, 2)})
    if not stocks:
        raise MoversError("no stock prices in the NSE response")
    ranked = sorted(stocks, key=lambda s: s["change_pct"], reverse=True)
    gainers = [s for s in ranked if s["change_pct"] > 0][:n]
    losers = [s for s in reversed(ranked) if s["change_pct"] < 0][:n]
    return {"index": index, "as_of": str(payload.get("timestamp") or ""), "source": "NSE",
            "count": len(stocks), "gainers": gainers, "losers": losers, "stocks": ranked}


def fetch_nse(index: str) -> dict:
    s = requests.Session()
    s.headers.update(HEADERS)
    try:
        s.get(NSE_HOME, timeout=10)
        r = s.get(NSE_URL + "?index=" + quote(index, safe=""), timeout=15)
    except requests.RequestException as e:
        raise MoversError(f"could not reach NSE: {e.__class__.__name__}")
    if r.status_code != 200:
        raise MoversError(f"NSE answered {r.status_code}")
    try:
        return r.json()
    except ValueError:
        raise MoversError("NSE did not return JSON")


def _load(index: str, fetcher, now) -> tuple:
    """(full data for the index, stale?) from cache or NSE."""
    index = (index or DEFAULT_INDEX).strip().upper()
    if index not in INDICES:
        raise HTTPException(400, "index must be one of: " + ", ".join(INDICES))
    fetcher = fetcher or fetch_nse
    t = time.time() if now is None else now
    hit = _cache.get(index)
    if hit and t - hit["at"] < FRESH_S:
        return hit["data"], False
    try:
        data = parse_movers(fetcher(index), index, MAX_N)
    except MoversError as e:
        if hit and t - hit["at"] < STALE_S:
            return hit["data"], True
        raise HTTPException(503, f"Top movers are unavailable: {e}")
    except Exception as e:
        log.warning("movers failed: %s", e)
        if hit and t - hit["at"] < STALE_S:
            return hit["data"], True
        raise HTTPException(503, "Top movers are unavailable")
    _cache[index] = {"at": t, "data": data}
    return data, False


def get_movers(index: str = DEFAULT_INDEX, n: int = 10,
               fetcher: Optional[Callable[[str], dict]] = None, now: Optional[float] = None) -> dict:
    if not 1 <= n <= MAX_N:
        raise HTTPException(400, f"n must be between 1 and {MAX_N}")
    data, stale = _load(index, fetcher, now)
    out = {k: v for k, v in data.items() if k != "stocks"}
    return dict(out, gainers=data["gainers"][:n], losers=data["losers"][:n], stale=stale)


def get_index_stocks(index: str = DEFAULT_INDEX, fetcher: Optional[Callable[[str], dict]] = None,
                     now: Optional[float] = None) -> dict:
    """Every stock in the index with price and day change, best performer first."""
    data, stale = _load(index, fetcher, now)
    rising = sum(1 for s in data["stocks"] if s["change_pct"] > 0)
    falling = sum(1 for s in data["stocks"] if s["change_pct"] < 0)
    return {"index": data["index"], "as_of": data["as_of"], "count": data["count"], "rising": rising,
            "falling": falling, "unchanged": data["count"] - rising - falling,
            "stocks": data["stocks"], "stale": stale}


def register_movers(app):
    @app.get("/market/movers")
    def market_movers(index: str = DEFAULT_INDEX, n: int = 10):
        return get_movers(index, n)

    @app.get("/market/index-stocks")
    def market_index_stocks(index: str = DEFAULT_INDEX):
        return get_index_stocks(index)
