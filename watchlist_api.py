"""
watchlist_api.py - live-ish prices for a list of stocks (the Markets watchlist).

    GET /market/quotes?symbols=RELIANCE,TCS,INFY      (up to 25 symbols)

Price: Dhan last-traded price while the market is open (real time), otherwise, or if Dhan
fails, Yahoo (delayed). Change is measured against the previous session's close from Yahoo.
Cached 15 seconds. Public route.
"""
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional

from fastapi import HTTPException

log = logging.getLogger("watchlist_api")

IST = timezone(timedelta(hours=5, minutes=30))
SYMBOL_RE = re.compile(r"^[A-Z0-9&\-]{1,20}$")
MAX_SYMBOLS = 25
CACHE_S = 15
_cache = {}


def market_open(now: datetime) -> bool:
    now = now.astimezone(IST)
    if now.weekday() >= 5:
        return False
    mins = now.hour * 60 + now.minute
    return 9 * 60 + 15 <= mins <= 15 * 60 + 30


def parse_symbols(raw: str) -> List[str]:
    out = []
    for s in (raw or "").upper().replace(".NS", "").split(","):
        s = s.strip()
        if not s:
            continue
        if not SYMBOL_RE.match(s):
            raise HTTPException(400, f"'{s}' is not a valid NSE symbol")
        if s not in out:
            out.append(s)
    if not out:
        raise HTTPException(400, "Give at least one symbol, e.g. symbols=RELIANCE,TCS")
    if len(out) > MAX_SYMBOLS:
        raise HTTPException(400, f"At most {MAX_SYMBOLS} symbols at a time")
    return out


def fetch_yahoo(symbols: List[str]) -> Dict[str, list]:
    """symbol -> [(date 'YYYY-MM-DD', close)] oldest first (a few recent sessions)."""
    import yfinance as yf
    tickers = [f"{s}.NS" for s in symbols]
    df = yf.download(tickers, period="5d", interval="1d", group_by="ticker",
                     progress=False, auto_adjust=False, threads=True)
    out = {}
    for s, t in zip(symbols, tickers):
        try:
            col = df[t]["Close"] if len(tickers) > 1 else df["Close"]
            rows = [(ts.strftime("%Y-%m-%d"), float(c)) for ts, c in col.dropna().items()]
            if rows:
                out[s] = rows
        except Exception:
            continue
    return out


def fetch_dhan_ltp(symbol: str) -> Optional[float]:
    try:
        from dhanhq_api import get_dhan_client
        client = get_dhan_client()
        if client is None:
            return None
        sec = client.get_security_id(symbol)
        if not sec:
            return None
        px = client.get_ltp(sec)
        return float(px) if px and float(px) > 0 else None
    except Exception:
        return None


def build_quotes(symbols: List[str], history: Dict[str, list], now: datetime,
                 ltp_fetch: Optional[Callable[[str], Optional[float]]] = None) -> dict:
    is_open = market_open(now)
    today = now.astimezone(IST).strftime("%Y-%m-%d")
    live = {}
    if is_open and ltp_fetch:
        with ThreadPoolExecutor(max_workers=6) as pool:
            for s, px in zip(symbols, pool.map(ltp_fetch, symbols)):
                if px:
                    live[s] = px
    quotes, missing = [], []
    for s in symbols:
        rows = history.get(s) or []
        if not rows and s not in live:
            missing.append(s)
            continue
        if rows:
            last_date, last_close = rows[-1]
            # while open, a bar dated today is the session in progress, so the previous close is the one before it
            if last_date == today and len(rows) >= 2:
                prev = rows[-2][1]
            elif last_date == today:
                prev = None
            elif is_open:
                prev = last_close                       # today's bar not created yet
            else:
                prev = rows[-2][1] if len(rows) >= 2 else None
        else:
            last_close, prev = None, None
        realtime = s in live
        price = live[s] if realtime else last_close
        if price is None:
            missing.append(s)
            continue
        chg = None if not prev else round(price - prev, 2)
        pct = None if not prev else round((price - prev) / prev * 100, 2)
        quotes.append({"symbol": s, "price": round(price, 2), "change": chg, "change_pct": pct,
                       "realtime": realtime})
    return {"as_of": now.astimezone(IST).strftime("%d-%b-%Y %H:%M:%S"), "market_open": is_open,
            "quotes": quotes, "missing": missing}


def get_quotes(raw: str, history_fetch: Optional[Callable] = None, ltp_fetch: Optional[Callable] = None,
               now: Optional[datetime] = None, clock: Optional[float] = None) -> dict:
    symbols = parse_symbols(raw)
    history_fetch = history_fetch or fetch_yahoo
    ltp_fetch = ltp_fetch if ltp_fetch is not None else fetch_dhan_ltp
    t = time.time() if clock is None else clock
    key = ",".join(symbols)
    hit = _cache.get(key)
    if hit and t - hit["at"] < CACHE_S:
        return hit["data"]
    now = now or datetime.now(IST)
    try:
        history = history_fetch(symbols)
    except Exception as e:
        log.warning("quotes history failed: %s", e)
        history = {}
    data = build_quotes(symbols, history, now, ltp_fetch)
    if not data["quotes"]:
        if hit:
            return dict(hit["data"], stale=True)
        raise HTTPException(503, "Prices are unavailable right now")
    _cache[key] = {"at": t, "data": data}
    return data


def register_watchlist(app):
    @app.get("/market/quotes")
    def market_quotes(symbols: str = ""):
        return get_quotes(symbols)
