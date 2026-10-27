"""
breadth_api.py - market breadth (advances / declines) for the Markets screen.

    GET /market/breadth

Source: NSE's allIndices feed, which carries advances, declines and unchanged for every index.
Public route (no sign-in). Cached for 5 minutes; if NSE fails, the last good answer (up to
1 hour old) is served, otherwise 503 with the reason.
"""
import logging
import time
from typing import Callable, Optional

import requests
from fastapi import HTTPException

log = logging.getLogger("breadth_api")

NSE_HOME = "https://www.nseindia.com"
NSE_URL = NSE_HOME + "/api/allIndices"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": NSE_HOME + "/market-data/live-market-indices",
}
WANTED = ["NIFTY 50", "NIFTY NEXT 50", "NIFTY MIDCAP 100", "NIFTY SMALLCAP 100", "NIFTY 500"]
FRESH_S = 300
STALE_S = 3600
_cache = {"at": 0.0, "data": None}


class BreadthError(Exception):
    pass


def _int(v) -> Optional[int]:
    try:
        return int(float(str(v).replace(",", "")))
    except (TypeError, ValueError):
        return None


def parse_breadth(payload) -> dict:
    """allIndices JSON -> {as_of, indices:[{name, advances, declines, unchanged, adv_pct}]}."""
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise BreadthError("unexpected response from NSE")
    by_name = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        name = str(r.get("index") or r.get("indexSymbol") or "").strip().upper()
        by_name[name] = r
    out = []
    for name in WANTED:
        r = by_name.get(name)
        if not r:
            continue
        a, d, u = _int(r.get("advances")), _int(r.get("declines")), _int(r.get("unchanged"))
        if a is None or d is None:
            continue
        u = u or 0
        total = a + d + u
        out.append({"name": name.title().replace("Nifty", "Nifty"), "advances": a, "declines": d,
                    "unchanged": u, "adv_pct": round(100.0 * a / total, 1) if total else None})
    if not out:
        raise BreadthError("no breadth figures in the NSE response")
    return {"as_of": str(payload.get("timestamp") or ""), "source": "NSE", "indices": out}


def fetch_nse() -> dict:
    s = requests.Session()
    s.headers.update(HEADERS)
    try:
        s.get(NSE_HOME, timeout=10)           # NSE wants the cookies from the home page
        r = s.get(NSE_URL, timeout=15)
    except requests.RequestException as e:
        raise BreadthError(f"could not reach NSE: {e.__class__.__name__}")
    if r.status_code != 200:
        raise BreadthError(f"NSE answered {r.status_code}")
    try:
        return r.json()
    except ValueError:
        raise BreadthError("NSE did not return JSON")


def get_breadth(fetcher: Callable[[], dict] = fetch_nse, now: Optional[float] = None) -> dict:
    t = time.time() if now is None else now
    if _cache["data"] and t - _cache["at"] < FRESH_S:
        return dict(_cache["data"], stale=False)
    try:
        data = parse_breadth(fetcher())
    except BreadthError as e:
        if _cache["data"] and t - _cache["at"] < STALE_S:
            return dict(_cache["data"], stale=True)
        raise HTTPException(503, f"Market breadth is unavailable: {e}")
    except Exception as e:                      # never let a parser surprise become a 500
        log.warning("breadth failed: %s", e)
        if _cache["data"] and t - _cache["at"] < STALE_S:
            return dict(_cache["data"], stale=True)
        raise HTTPException(503, "Market breadth is unavailable")
    _cache["at"], _cache["data"] = t, data
    return dict(data, stale=False)


def register_breadth(app):
    @app.get("/market/breadth")
    def market_breadth():
        return get_breadth()
