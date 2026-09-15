"""
api.py — FastAPI backend for the Flutter app
============================================
Wraps the Python engine you already built (analysis_api, pf_doctor, pf_xray)
and exposes it over HTTP so the Flutter app can call it.

Nothing here reimplements logic — it reuses your existing modules. That's the
whole point: Flutter handles screens, Python keeps doing the thinking.

RUN LOCALLY
-----------
    pip install fastapi uvicorn
    uvicorn api:app --reload --host 0.0.0.0 --port 8000

Then open http://localhost:8000/docs — FastAPI generates interactive docs where
you can try every endpoint in the browser. Use that to check the API works
BEFORE writing any Flutter code.

DEPLOY (free)
-------------
Render / Railway both work. Start command:
    uvicorn api:app --host 0.0.0.0 --port $PORT
"""

from typing import Optional
import math

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(
    title="My Research Platform API",
    description="Stock & mutual fund research engine",
    version="1.0.0",
)

# The Flutter app calls this from a phone, so allow cross-origin requests.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],       # tighten this once you have a real app domain
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ===========================================================================
# HELPERS
# ===========================================================================
def _clean(obj):
    """
    Recursively replace NaN / Infinity with None.

    yfinance and pandas hand back np.float64('nan') for missing figures, and
    json.dumps refuses to serialize those — which crashes the endpoint with
    "Out of range float values are not JSON compliant". Every response built
    from DataFrame data goes through here first.
    """
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if obj is None or isinstance(obj, (bool, str, bytes)):
        return obj
    if isinstance(obj, (int, float)):
        f = float(obj)
        return None if not math.isfinite(f) else obj
    # numpy scalars and anything else float-like
    try:
        f = float(obj)
    except (TypeError, ValueError):
        return obj
    if not math.isfinite(f):
        return None
    return obj


# ===========================================================================
# HEALTH
# ===========================================================================
@app.get("/")
def root():
    return {"status": "ok", "service": "My Research Platform API"}


# ===========================================================================
# MARKET INDICES  (live)
# ===========================================================================
_INDICES = {
    "NIFTY 50": "^NSEI",
    "SENSEX": "^BSESN",
    "BANK NIFTY": "^NSEBANK",
    "NIFTY IT": "^CNXIT",
    "NIFTY AUTO": "^CNXAUTO",
    "NIFTY PHARMA": "^CNXPHARMA",
    "NIFTY FMCG": "^CNXFMCG",
    "NIFTY METAL": "^CNXMETAL",
}


@app.get("/indices")
def market_indices():
    """Live values + daily change for the major Indian indices."""
    import yfinance as yf

    symbols = list(_INDICES.values())
    try:
        data = yf.download(symbols, period="5d", progress=False,
                           auto_adjust=True, group_by="ticker", threads=True)
    except Exception as e:
        raise HTTPException(502, f"Index data error: {e}")

    out = []
    for name, sym in _INDICES.items():
        try:
            closes = data[sym]["Close"].dropna() if len(symbols) > 1 \
                else data["Close"].dropna()
            if closes.empty:
                continue
            value = float(closes.iloc[-1])
            prev = float(closes.iloc[-2]) if len(closes) > 1 else value
            change = value - prev
            out.append({
                "name": name,
                "value": round(value, 2),
                "change": round(change, 2),
                "change_pct": round((change / prev * 100) if prev else 0, 2),
            })
        except Exception:
            continue

    if not out:
        raise HTTPException(502, "No index data available right now")
    return _clean({"indices": out})


@app.get("/stocks/list")
def stock_list():
    """
    The full searchable universe (~2000 names): NIFTY 100 + Midcap 150 +
    Smallcap 250 + every other NSE-listed equity. Just the symbols — full
    data loads when a stock is opened. Cached implicitly by clients; the
    list changes rarely.
    """
    from analysis_api import get_universe

    names: list[str] = []
    seen = set()
    for uni in ("LARGECAP", "MIDCAP", "SMALLCAP", "ALLEQUITIES"):
        try:
            for sym in get_universe(uni):
                s = sym.replace(".NS", "")
                if s not in seen:
                    seen.add(s)
                    names.append(s)
        except Exception:
            continue

    # fallback so the endpoint never returns empty if NSE blocks the fetch
    if not names:
        names = ["RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "ITC",
                 "SBIN", "BHARTIARTL", "LT", "KOTAKBANK"]

    return {"count": len(names), "symbols": sorted(names)}


@app.get("/stocks/list/detailed")
def stock_list_detailed():
    """
    Same ~2000-name universe as /stocks/list, with sector, company name,
    ISIN, and market-cap category attached to each symbol - lets the app
    filter the search bar by sector/cap, show full names, and look up a
    logo (by ISIN) instead of just matching on symbol text. Uses the same
    get_universe_with_sectors() the web app's Screener already relies on,
    so sector names stay consistent across both platforms. The top ~500
    (LARGECAP/MIDCAP/SMALLCAP) get real sector names from NSE's index
    files; the remaining ~1500 (ALLEQUITIES) come back as "Uncategorized"
    since NSE's full equity list doesn't carry sector data - shown
    honestly rather than guessed.
    """
    from analysis_api import get_universe_with_sectors

    rows: list[dict] = []
    seen = set()
    for cap in ("LARGECAP", "MIDCAP", "SMALLCAP", "ALLEQUITIES"):
        try:
            symbols, sector_map, name_map, isin_map = get_universe_with_sectors(cap)
        except Exception:
            continue
        for sym in symbols:
            s = sym.replace(".NS", "")
            if s in seen:
                continue
            seen.add(s)
            rows.append({
                "symbol": s,
                "name": name_map.get(sym) or s,
                "isin": isin_map.get(sym),
                "sector": sector_map.get(sym) or "Other",
                "cap": cap,
            })

    if not rows:
        rows = [{"symbol": s, "name": s, "isin": None, "sector": "Other",
                  "cap": "LARGECAP"} for s in
                ["RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "ITC",
                 "SBIN", "BHARTIARTL", "LT", "KOTAKBANK"]]


    sectors = sorted(set(r["sector"] for r in rows))
    rows.sort(key=lambda r: r["symbol"])
    return {"count": len(rows), "stocks": rows, "sectors": sectors}



# =============================================================================
# MARKET DATA — sectors, live chart, FII/DII flows, market news
# =============================================================================
# Same situation as Holdings Explorer: this code existed on the previously-
# running Render instance but was never in the GitHub source, so it was lost
# the moment a fresh deploy replaced that stale instance. Rebuilt here,
# reusing proven logic from the Crescent web app (dashboard.py's sector
# section, live_chart.py, and advanced_news.py's multi-source FII/DII and
# RSS news fetchers) so behavior matches across both platforms.
# =============================================================================

import html as _mhtml
import re as _mre
from datetime import datetime as _mdatetime, timezone as _mtimezone
from time import time as _mtime

import pandas as _mpd
import yfinance as _myf

# ---------------------------------------------------------------------------
# Tiny in-memory TTL cache - api.py runs outside Streamlit, so there's no
# st.cache_data here. This is the same idea cache_compat.py already applies
# elsewhere in this codebase, just written directly since these functions
# don't call into any Streamlit-cached code.
# ---------------------------------------------------------------------------
_M_CACHE: dict = {}


def _m_cached(key: str, ttl_seconds: int, fn):
    now = _mtime()
    hit = _M_CACHE.get(key)
    if hit and now - hit[0] < ttl_seconds:
        return hit[1]
    value = fn()
    _M_CACHE[key] = (now, value)
    return value


# =============================================================================
# SECTORS
# =============================================================================
_M_SECTOR_DIRECT = {"Bank": "^NSEBANK", "IT": "^CNXIT", "Pharma": "^CNXPHARMA"}
_M_SECTOR_BASKETS = {
    "Auto": ["MARUTI.NS", "TATAMOTORS.NS", "M&M.NS", "BAJAJ-AUTO.NS", "EICHERMOT.NS", "HEROMOTOCO.NS"],
    "FMCG": ["HINDUNILVR.NS", "ITC.NS", "NESTLEIND.NS", "BRITANNIA.NS", "DABUR.NS", "GODREJCP.NS"],
    "Metal": ["TATASTEEL.NS", "JSWSTEEL.NS", "HINDALCO.NS", "VEDL.NS", "SAIL.NS", "JINDALSTEL.NS"],
    "Energy": ["RELIANCE.NS", "ONGC.NS", "NTPC.NS", "POWERGRID.NS", "BPCL.NS", "IOC.NS"],
    "Realty": ["DLF.NS", "GODREJPROP.NS", "OBEROIRLTY.NS", "PRESTIGE.NS", "PHOENIXLTD.NS", "BRIGADE.NS"],
    "PSU Bank": ["SBIN.NS", "BANKBARODA.NS", "PNB.NS", "CANBK.NS", "UNIONBANK.NS", "INDIANB.NS"],
    "Infra": ["LT.NS", "ADANIPORTS.NS", "GMRINFRA.NS", "IRB.NS", "NBCC.NS", "NCC.NS"],
}
_M_COMPARE_DAYS = {"1y": 365, "2y": 730, "3y": 1095, "5y": 1825}


def _m_compute_sectors(compare: str):
    period_days = _M_COMPARE_DAYS.get(compare, 365)
    yf_period = f"{max(period_days // 365, 1) + 1}y"

    all_direct = list(_M_SECTOR_DIRECT.values())
    all_basket = [s for basket in _M_SECTOR_BASKETS.values() for s in basket]
    all_symbols = all_direct + all_basket

    data = _myf.download(all_symbols, period=yf_period, progress=False,
                         auto_adjust=True, group_by="ticker", threads=True)

    def _closes(symbol):
        try:
            s = data[symbol]["Close"].dropna()
            return s if len(s) >= 2 else None
        except Exception:
            return None

    def _returns(symbol):
        c = _closes(symbol)
        if c is None:
            return None, None, None, None
        last = float(c.iloc[-1])
        r1d = (last / float(c.iloc[-2]) - 1) * 100 if len(c) >= 2 else None
        r1m = None
        if len(c) > 22:
            r1m = (last / float(c.iloc[-22]) - 1) * 100
        r_cmp = (last / float(c.iloc[0]) - 1) * 100
        return last, r1d, r1m, r_cmp

    sectors = []
    for name, symbol in _M_SECTOR_DIRECT.items():
        last, r1d, r1m, r_cmp = _returns(symbol)
        if last is not None:
            sectors.append({"name": name, "level": round(last, 1),
                           "return_1d": r1d and round(r1d, 2),
                           "return_1m": r1m and round(r1m, 2),
                           "return_compare": r_cmp and round(r_cmp, 2)})

    for name, basket in _M_SECTOR_BASKETS.items():
        results = [_returns(s) for s in basket]
        results = [r for r in results if r[0] is not None]
        if not results:
            continue
        avg_level = sum(r[0] for r in results) / len(results)
        avg_1d = [r[1] for r in results if r[1] is not None]
        avg_1m = [r[2] for r in results if r[2] is not None]
        avg_cmp = [r[3] for r in results if r[3] is not None]
        sectors.append({
            "name": name, "level": round(avg_level, 1),
            "return_1d": round(sum(avg_1d) / len(avg_1d), 2) if avg_1d else None,
            "return_1m": round(sum(avg_1m) / len(avg_1m), 2) if avg_1m else None,
            "return_compare": round(sum(avg_cmp) / len(avg_cmp), 2) if avg_cmp else None,
        })

    sectors.sort(key=lambda s: (s["return_1m"] is None, -(s["return_1m"] or 0)))

    advancing = sum(1 for s in sectors if (s["return_1m"] or 0) > 0)
    declining = sum(1 for s in sectors if (s["return_1m"] or 0) < 0)
    with_1m = [s for s in sectors if s["return_1m"] is not None]
    best = with_1m[0]["name"] if with_1m else None
    worst = with_1m[-1]["name"] if with_1m else None

    return {
        "sectors": sectors,
        "summary": {"advancing": advancing, "declining": declining, "best": best, "worst": worst},
        "compare_period": compare.upper(),
    }


@app.get("/sectors")
def sectors(compare: str = "1y"):
    try:
        return _m_cached(f"sectors:{compare}", 3600, lambda: _m_compute_sectors(compare))
    except Exception as e:
        raise HTTPException(503, f"Could not load sector performance: {e}")


# =============================================================================
# LIVE CHART
# =============================================================================
_M_TIMEFRAME_CONFIG = {
    "1D": {"period": "1d", "interval": "5m", "live": True},
    "1W": {"period": "5d", "interval": "30m", "live": False},
    "1M": {"period": "1mo", "interval": "1d", "live": False},
    "6M": {"period": "6mo", "interval": "1d", "live": False},
    "1Y": {"period": "1y", "interval": "1d", "live": False},
    "5Y": {"period": "5y", "interval": "1wk", "live": False},
    "ALL": {"period": "max", "interval": "1mo", "live": False},
}
_M_DISPLAY_NAMES = {
    "^NSEI": "NIFTY 50", "^NSEBANK": "BANK NIFTY", "^BSESN": "SENSEX",
}


def _m_compute_chart(symbol: str, timeframe: str):
    cfg = _M_TIMEFRAME_CONFIG.get(timeframe.upper(), _M_TIMEFRAME_CONFIG["1D"])
    ticker = _myf.Ticker(symbol)
    df = ticker.history(period=cfg["period"], interval=cfg["interval"])
    if df.empty:
        raise ValueError(f"No chart data for {symbol}")

    prev_close = None
    if cfg["live"]:
        try:
            prev_close = float(ticker.fast_info["previous_close"])
        except Exception:
            try:
                daily = ticker.history(period="5d")["Close"]
                if len(daily) >= 2:
                    prev_close = float(daily.iloc[-2])
            except Exception:
                prev_close = None

    closes = df["Close"].dropna()
    last = float(closes.iloc[-1])
    baseline = prev_close if (cfg["live"] and prev_close) else float(closes.iloc[0])
    baseline_label = "prev close" if cfg["live"] else f"start of {timeframe.upper()}"

    change = last - baseline
    change_pct = (change / baseline * 100) if baseline else 0
    high = float(df["High"].max())
    low = float(df["Low"].min())
    range_pct = ((high - low) / low * 100) if low else None

    fmt = "%H:%M" if cfg["interval"].endswith("m") else "%d-%b"
    series = [{"label": ts.strftime(fmt), "close": float(c)} for ts, c in closes.items()]

    return {
        "display": _M_DISPLAY_NAMES.get(symbol, symbol.replace(".NS", "")),
        "last": round(last, 2), "change": round(change, 2), "change_pct": round(change_pct, 2),
        "high": round(high, 2), "low": round(low, 2),
        "range_pct": round(range_pct, 2) if range_pct is not None else None,
        "updated": _mdatetime.now().strftime("%d-%b-%Y %H:%M"),
        "live": cfg["live"], "interval": cfg["interval"], "points": len(series),
        "baseline": round(baseline, 2), "baseline_label": baseline_label,
        "series": series,
    }


@app.get("/chart/{symbol}")
def chart(symbol: str, timeframe: str = "1D"):
    try:
        return _m_compute_chart(symbol, timeframe)
    except Exception as e:
        raise HTTPException(503, f"No chart data for {symbol}: {e}")


# =============================================================================
# FII / DII FLOWS
# =============================================================================
def _m_num(x):
    try:
        return float(str(x).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _m_fetch_fii_dii():
    import requests
    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

    try:
        s = requests.Session()
        s.headers.update({"User-Agent": UA, "Accept": "application/json, text/plain, */*",
                          "Referer": "https://www.nseindia.com/reports/fii-dii"})
        s.get("https://www.nseindia.com", timeout=8)
        r = s.get("https://www.nseindia.com/api/fiidiiTradeReact", timeout=8)
        r.raise_for_status()
        rows = []
        for d in r.json():
            cat = str(d.get("category", "")).upper()
            rows.append({"who": "FII" if ("FII" in cat or "FPI" in cat) else "DII",
                        "date": d.get("date", ""), "buy": _m_num(d.get("buyValue")),
                        "sell": _m_num(d.get("sellValue")), "net": _m_num(d.get("netValue"))})
        if rows:
            return rows, "NSE"
    except Exception:
        pass

    for url in ("https://api.stockedge.com/Api/FIIDailyDashboardApi/GetLatestFIIDIIActivities?lang=en",
               "https://api.stockedge.com/Api/DailyDashboardApi/GetLatestFIIDIIActivity?lang=en"):
        try:
            r = requests.get(url, headers={"User-Agent": UA, "Accept": "application/json"}, timeout=10)
            r.raise_for_status()
            data = r.json()
            items = data if isinstance(data, list) else [data]
            rows = []
            for d in items:
                if not isinstance(d, dict):
                    continue
                blob = {str(k).lower(): v for k, v in d.items()}
                who_raw = str(blob.get("name") or blob.get("category") or blob.get("clienttype") or "").upper()
                who = "FII" if ("FII" in who_raw or "FPI" in who_raw) else ("DII" if "DII" in who_raw else None)
                if not who:
                    continue
                buy = _m_num(blob.get("buyvalue") or blob.get("grosspurchase") or blob.get("buy"))
                sell = _m_num(blob.get("sellvalue") or blob.get("grosssales") or blob.get("sell"))
                net = _m_num(blob.get("netvalue") or blob.get("net"))
                if net is None and buy is not None and sell is not None:
                    net = buy - sell
                rows.append({"who": who, "date": str(blob.get("date") or blob.get("tradedate") or ""),
                           "buy": buy, "sell": sell, "net": net})
            if rows:
                seen = {}
                for r2 in rows:
                    seen.setdefault(r2["who"], r2)
                return list(seen.values()), "StockEdge"
        except Exception:
            continue

    for url in ("https://groww.in/v1/api/stocks_fo_data/v1/fii_dii/activity?count=2",
               "https://groww.in/v1/api/stocks_fo_data/v1/fii_dii_activity"):
        try:
            r = requests.get(url, headers={"User-Agent": UA, "Accept": "application/json"}, timeout=10)
            r.raise_for_status()
            data = r.json()
            items = data.get("data") or data.get("fiiDiiList") or (data if isinstance(data, list) else [])
            rows = []
            for d in (items if isinstance(items, list) else []):
                blob = {str(k).lower(): v for k, v in d.items()}
                date = str(blob.get("date") or "")
                for who, prefix in (("FII", "fii"), ("DII", "dii")):
                    buy = _m_num(blob.get(f"{prefix}buy") or blob.get(f"{prefix}_buy"))
                    sell = _m_num(blob.get(f"{prefix}sell") or blob.get(f"{prefix}_sell"))
                    net = _m_num(blob.get(f"{prefix}net") or blob.get(f"{prefix}_net"))
                    if net is None and buy is not None and sell is not None:
                        net = buy - sell
                    if net is not None:
                        rows.append({"who": who, "date": date, "buy": buy, "sell": sell, "net": net})
            if rows:
                return rows[:2], "Groww"
        except Exception:
            continue

    return [], None


@app.get("/fii-dii")
def fii_dii():
    rows, source = _m_cached("fii_dii", 1800, _m_fetch_fii_dii)
    if not rows:
        raise HTTPException(503, "FII/DII data is temporarily unavailable — all sources are "
                                 "blocking this server right now.")
    date = rows[0].get("date", "") if rows else ""
    return {"date": date, "source": source, "flows": rows}


@app.get("/fii-dii/history")
def fii_dii_history(days: int = 30):
    def _compute():
        import io as _mio
        import requests
        from datetime import date as _mdate

        base = "https://www.moneycontrol.com/stocks/marketstats/fii_dii_activity/index.php"
        prev = _mdate.today().replace(day=1) - _mpd.Timedelta(days=1)
        urls = [base, f"{base}?mon_year={prev.strftime('%m-%Y')}", f"{base}?mon_year={prev.strftime('%b-%Y')}"]

        s = requests.Session()
        s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                          "Referer": "https://www.moneycontrol.com/"})
        frames = []
        for url in urls:
            try:
                r = s.get(url, timeout=12)
                r.raise_for_status()
                for t in _mpd.read_html(_mio.StringIO(r.text)):
                    if t.shape[1] >= 7:
                        t = t.iloc[:, :7].copy()
                        t.columns = ["date", "fii_buy", "fii_sell", "fii_net", "dii_buy", "dii_sell", "dii_net"]
                        frames.append(t)
                        break
            except Exception:
                continue

        rows, _source = _m_fetch_fii_dii()
        today_row = None
        if rows:
            try:
                fii = next(r for r in rows if r["who"] == "FII")
                dii = next(r for r in rows if r["who"] == "DII")
                today_row = _mpd.DataFrame([{
                    "date": _mpd.to_datetime(fii["date"], dayfirst=True, errors="coerce"),
                    "fii_net": fii["net"], "dii_net": dii["net"],
                }])
            except Exception:
                today_row = None

        parts = frames + ([today_row] if today_row is not None else [])
        if not parts:
            return []
        df = _mpd.concat(parts, ignore_index=True)
        df["date"] = _mpd.to_datetime(df["date"], errors="coerce", dayfirst=True)
        df = df.dropna(subset=["date", "fii_net", "dii_net"]) if "fii_net" in df.columns else df.dropna(subset=["date"])
        df = df.drop_duplicates(subset="date", keep="last").sort_values("date", ascending=False)
        out = []
        for _, r2 in df.head(days).iterrows():
            out.append({
                "date": r2["date"].strftime("%d-%b-%Y"),
                "fii_net": None if _mpd.isna(r2.get("fii_net")) else round(float(r2["fii_net"]), 1),
                "dii_net": None if _mpd.isna(r2.get("dii_net")) else round(float(r2["dii_net"]), 1),
            })
        return out

    history = _m_cached(f"fii_dii_history:{days}", 6 * 3600, _compute)
    if not history:
        raise HTTPException(503, "No FII/DII history available right now — the data source is "
                                 "unreachable from this server.")
    fii_total = sum(h["fii_net"] for h in history if h["fii_net"] is not None)
    dii_total = sum(h["dii_net"] for h in history if h["dii_net"] is not None)
    buying_days = sum(1 for h in history if (h["fii_net"] or 0) > 0)
    return {
        "history": history,
        "summary": {
            "fii_net_total": round(fii_total, 1), "dii_net_total": round(dii_total, 1),
            "combined_net": round(fii_total + dii_total, 1),
            "fii_buying_days": buying_days, "total_days": len(history),
        },
    }


# =============================================================================
# MARKET NEWS
# =============================================================================
_M_FEEDS = {
    "Economic Times Markets": "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "ET Stocks": "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms",
    "Moneycontrol Markets": "https://www.moneycontrol.com/rss/marketreports.xml",
    "Moneycontrol Business": "https://www.moneycontrol.com/rss/business.xml",
    "Livemint Markets": "https://www.livemint.com/rss/markets",
    "Business Standard Markets": "https://www.business-standard.com/rss/markets-106.rss",
}
_M_POSITIVE_WORDS = ("surge", "rally", "gain", "jump", "rise", "record high", "profit", "beats",
                    "upgrade", "bullish", "soar", "growth", "strong", "boost", "buy", "outperform",
                    "up ", "hits high", "best")
_M_NEGATIVE_WORDS = ("fall", "drop", "crash", "plunge", "loss", "decline", "slump", "downgrade",
                    "bearish", "weak", "cuts", "misses", "sell-off", "selloff", "tumble", "down ",
                    "fears", "worst", "fraud", "probe")
_M_HIGH_IMPACT_WORDS = ("rbi", "sebi", "fed", "budget", "gdp", "inflation", "rate cut", "rate hike",
                        "war", "sanctions", "crisis")


def _m_classify_sentiment(text: str) -> str:
    t = text.lower()
    pos = sum(w in t for w in _M_POSITIVE_WORDS)
    neg = sum(w in t for w in _M_NEGATIVE_WORDS)
    if pos > neg:
        return "Positive"
    if neg > pos:
        return "Negative"
    return "Neutral"


def _m_classify_impact(text: str) -> str:
    hits = sum(w in text.lower() for w in _M_HIGH_IMPACT_WORDS)
    return "High" if hits >= 2 else ("Medium" if hits == 1 else "Low")


def _m_strip_html(s: str) -> str:
    s = _mre.sub(r"<[^>]+>", " ", s or "")
    return _mhtml.unescape(_mre.sub(r"\s+", " ", s)).strip()


def _m_age_str(dt) -> str:
    if dt is None:
        return ""
    delta = _mdatetime.now(_mtimezone.utc) - dt
    mins = int(delta.total_seconds() // 60)
    if mins < 60:
        return f"{mins} mins ago"
    hrs = mins // 60
    if hrs < 24:
        return f"{hrs} hrs ago"
    return f"{hrs // 24} days ago"


def _m_extract_image(entry, raw_summary: str) -> str:
    try:
        for m in getattr(entry, "media_content", []) or []:
            url = m.get("url", "")
            if url.startswith("http"):
                return url
        for m in getattr(entry, "media_thumbnail", []) or []:
            url = m.get("url", "")
            if url.startswith("http"):
                return url
        for enc in getattr(entry, "enclosures", []) or []:
            if "image" in enc.get("type", "") and enc.get("href", "").startswith("http"):
                return enc["href"]
        m = _mre.search(r'<img[^>]+src=["\'](http[^"\']+)["\']', raw_summary or "")
        if m:
            return m.group(1)
    except Exception:
        pass
    return ""


def _m_fetch_news():
    import feedparser
    rows = []
    for source, url in _M_FEEDS.items():
        try:
            feed = feedparser.parse(url)
            for e in feed.entries[:15]:
                title = _m_strip_html(getattr(e, "title", ""))
                raw_summary = getattr(e, "summary", "")
                summary = _m_strip_html(raw_summary)[:400]
                if not title:
                    continue
                image = _m_extract_image(e, raw_summary)
                published = None
                for attr in ("published_parsed", "updated_parsed"):
                    tp = getattr(e, attr, None)
                    if tp:
                        published = _mdatetime(*tp[:6], tzinfo=_mtimezone.utc)
                        break
                text = f"{title} {summary}"
                rows.append({
                    "title": title, "summary": summary, "link": getattr(e, "link", ""),
                    "source": source, "_published": published, "image": image,
                    "sentiment": _m_classify_sentiment(text), "impact": _m_classify_impact(text),
                })
        except Exception:
            continue
    seen_titles = set()
    unique = []
    for r in rows:
        if r["title"] in seen_titles:
            continue
        seen_titles.add(r["title"])
        unique.append(r)
    unique.sort(key=lambda r: r["_published"] or _mdatetime.min.replace(tzinfo=_mtimezone.utc), reverse=True)
    return unique


@app.get("/news")
def news(limit: int = 60, sentiment: str = "", impact: str = ""):
    rows = _m_cached("news", 600, _m_fetch_news)
    filtered = rows
    if sentiment:
        filtered = [r for r in filtered if r["sentiment"].lower() == sentiment.lower()]
    if impact:
        filtered = [r for r in filtered if r["impact"].lower() == impact.lower()]
    filtered = filtered[:limit]

    articles = [{
        "title": r["title"], "summary": r["summary"], "source": r["source"],
        "link": r["link"], "image": r["image"], "sentiment": r["sentiment"],
        "impact": r["impact"], "age": _m_age_str(r["_published"]),
    } for r in filtered]

    positive = sum(1 for r in rows if r["sentiment"] == "Positive")
    negative = sum(1 for r in rows if r["sentiment"] == "Negative")
    high_impact = sum(1 for r in rows if r["impact"] == "High")

    return {
        "count": len(articles), "articles": articles,
        "summary": {"positive": positive, "negative": negative, "high_impact": high_impact},
        "sources": sorted(set(r["source"] for r in rows)),
    }

@app.get("/quote/{symbol}")
def live_quote(symbol: str):
    """
    Real-time last-traded price via Dhan (official, no delay).
    Falls back to Yahoo (delayed) if Dhan is unavailable — so the app always
    gets *a* price, and knows which source it came from.
    """
    sym = symbol.strip().upper().replace(".NS", "")
    dhan_error = None

    # --- try Dhan first (real-time) ---
    try:
        from dhanhq_api import get_dhan_client
        client = get_dhan_client()
        if client is None:
            dhan_error = "get_dhan_client() returned None — credentials missing?"
        else:
            sec_id = client.get_security_id(sym)
            if not sec_id:
                dhan_error = f"no security_id found for '{sym}'"
            else:
                price = client.get_ltp(sec_id)
                if price and float(price) > 0:
                    return {"symbol": sym, "price": round(float(price), 2),
                            "source": "dhan", "realtime": True}
                dhan_error = f"get_ltp({sec_id}) returned {price!r}"
    except Exception as e:
        dhan_error = f"{type(e).__name__}: {e}"

    # --- fallback: Yahoo (delayed) ---
    try:
        import yfinance as yf
        h = yf.Ticker(f"{sym}.NS").history(period="1d")["Close"].dropna()
        if not h.empty:
            return {"symbol": sym, "price": round(float(h.iloc[-1]), 2),
                    "source": "yahoo", "realtime": False,
                    "dhan_error": dhan_error}
    except Exception:
        pass

    raise HTTPException(502, f"No live price available for '{symbol}' "
                             f"(dhan: {dhan_error})")


@app.get("/debug/dhan")
def debug_dhan(symbol: str = "RELIANCE"):
    """
    Diagnostics for the Dhan connection. Reports which env vars are present
    (never their values) and exactly where the chain breaks.
    """
    import os

    report = {
        "env": {k: bool(os.environ.get(k)) for k in
                ("DHAN_CLIENT_ID", "DHAN_ACCESS_TOKEN",
                 "DHAN_CLIENT_CODE", "DHAN_TOKEN")},
        "import_ok": False,
        "client_ok": False,
        "security_id": None,
        "ltp": None,
        "error": None,
    }

    try:
        from dhanhq_api import get_dhan_client
        report["import_ok"] = True
        client = get_dhan_client()
        report["client_ok"] = client is not None
        if client is not None:
            sec_id = client.get_security_id(symbol.strip().upper())
            report["security_id"] = sec_id
            if sec_id:
                report["ltp"] = client.get_ltp(sec_id)
    except Exception as e:
        report["error"] = f"{type(e).__name__}: {e}"

    return report


# ===========================================================================
# STOCKS
# ===========================================================================
@app.get("/stock/{symbol}")
def stock_detail(symbol: str):
    """Fundamentals + technical signals for one NSE stock (e.g. RELIANCE)."""
    from analysis_api import get_fundamentals, get_technicals

    sym = symbol.upper().replace(".NS", "") + ".NS"
    try:
        fundamentals = get_fundamentals(sym)
        _, signals = get_technicals(sym)
    except Exception as e:
        raise HTTPException(502, f"Data source error: {e}")

    if not signals:
        raise HTTPException(404, f"No price data for '{symbol}'")

    # get_fundamentals() already detects Yahoo returning a near-empty
    # response without raising (rate-limiting) and flags it via
    # _fetch_failed - previously this endpoint ignored that flag and
    # returned the empty data as if it were real, which is exactly why
    # Fundamental Ratios showed blank instead of an error the app could
    # retry on.
    if fundamentals.get("_fetch_failed"):
        raise HTTPException(
            503, f"Fundamental data for '{symbol}' is temporarily unavailable "
                 "— the data source is rate-limiting this server. Try again shortly."
        )

    # Fix dividend yield units: Yahoo now returns this already as a percent
    # (e.g. 0.46 = 0.46%), but the engine multiplies by 100 → 46%. Undo that.
    dy = fundamentals.get("dividend_yield_pct")
    if isinstance(dy, (int, float)) and dy > 20:
        fundamentals["dividend_yield_pct"] = round(dy / 100, 2)

    return _clean({"symbol": sym, "fundamentals": fundamentals,
                   "technicals": signals})


@app.get("/stock/{symbol}/history")
def stock_history(symbol: str, period: str = "1y"):
    """Daily OHLC history with indicators, for charting in Flutter."""
    from analysis_api import get_technicals

    sym = symbol.upper().replace(".NS", "") + ".NS"
    try:
        df, _ = get_technicals(sym, period=period)
    except Exception as e:
        raise HTTPException(502, f"Data source error: {e}")

    if df.empty:
        raise HTTPException(404, f"No history for '{symbol}'")

    df = df.reset_index()
    cols = [c for c in ["Date", "Open", "High", "Low", "Close", "Volume",
                        "SMA20", "SMA50", "SMA200", "RSI"] if c in df.columns]
    out = df[cols].tail(400)
    out["Date"] = out["Date"].astype(str)
    return _clean({"symbol": sym,
                   "candles": out.where(out.notna(), None)
                                 .to_dict(orient="records")})


@app.get("/stock/{symbol}/statements")
def stock_statements(symbol: str):
    """
    Annual financial statements (income, balance sheet, cash flow) in ₹ crore.
    Each statement is a list of {item, values: {period: number}} for the app
    to render as a table.
    """
    from analysis_api import get_statements

    sym = symbol.upper().replace(".NS", "") + ".NS"
    try:
        stm = get_statements(sym)
    except Exception as e:
        raise HTTPException(502, f"Statements error: {e}")

    def to_rows(df):
        """DataFrame (items x periods) -> list of {item, values}."""
        if df is None or getattr(df, "empty", True):
            return {"periods": [], "rows": []}
        periods = [str(c) for c in df.columns]
        rows = []
        for item, series in df.iterrows():
            vals = {}
            for p, v in zip(periods, series.tolist()):
                try:
                    vals[p] = None if v != v else round(float(v), 1)  # NaN check
                except (TypeError, ValueError):
                    vals[p] = None
            rows.append({"item": str(item), "values": vals})
        return {"periods": periods, "rows": rows}

    return _clean({
        "symbol": sym,
        "income": to_rows(stm.get("income")),
        "balance": to_rows(stm.get("balance")),
        "cashflow": to_rows(stm.get("cashflow")),
    })


# ===========================================================================
# SCREENER
# ===========================================================================
@app.get("/screener")
def screener(universe: str = "LARGECAP", limit: int = 100):
    """Live screen of the NIFTY 100 (or another universe)."""
    from analysis_api import get_universe, scan_universe

    try:
        symbols = get_universe(universe)[:limit]
        df = scan_universe(symbols)
    except Exception as e:
        raise HTTPException(502, f"Scan failed: {e}")

    return _clean({"count": len(df),
                   "stocks": df.where(df.notna(), None)
                               .to_dict(orient="records")})


# ===========================================================================
# MUTUAL FUNDS
# ===========================================================================

# =============================================================================
# FUNDS DATABASE — AMFI daily NAV + fund metrics (funds_data.json)
# =============================================================================
# Same situation as Holdings Explorer and the market-data routes: this
# functionality existed on the previously-running Render instance but was
# never in the GitHub source. Rebuilt from funds_data.json (2085 funds,
# already present in the repo) with every field name matched directly
# against funds_screen.dart's actual usage.
# =============================================================================

import json as _fj
import os as _fo

_FUNDS_DATA: list | None = None

_FUND_SHEET_LABELS = {"equity": "Equity", "debt": "Debt", "hybrid": "Hybrid", "fof": "FoFs"}


def _load_funds_data() -> list:
    global _FUNDS_DATA
    if _FUNDS_DATA is None:
        path = _fo.path.join(_fo.path.dirname(__file__), "funds_data.json")
        with open(path, encoding="utf-8") as f:
            _FUNDS_DATA = _fj.load(f)
    return _FUNDS_DATA


def _fund_category_name(fund: dict) -> str:
    """'Equity : Flexi Cap' -> 'Flexi Cap' (strip the sheet-level prefix
    funds_screen.dart doesn't need, since it groups by sheet separately)."""
    classification = fund.get("classification") or ""
    if " : " in classification:
        return classification.split(" : ", 1)[1].strip()
    return classification.strip() or "Other"


@app.get("/funds/db/categories")
def funds_db_categories():
    """Every fund grouped by sheet (Equity/Debt/Hybrid/FoFs), then by
    category name within each - matches the {group, categories:[{name,
    count}]} shape funds_screen.dart reads directly."""
    data = _load_funds_data()
    by_sheet: dict = {}
    for fund in data:
        sheet = fund.get("sheet") or "equity"
        label = _FUND_SHEET_LABELS.get(sheet, sheet.title())
        cat_name = _fund_category_name(fund)
        by_sheet.setdefault(label, {}).setdefault(cat_name, 0)
        by_sheet[label][cat_name] += 1

    # Equity first (funds_screen.dart opens on it by default), then a
    # sensible fixed order for the rest, alphabetical for anything new.
    sheet_order = ["Equity", "Debt", "Hybrid", "FoFs"]
    groups = []
    for label in sheet_order + sorted(set(by_sheet) - set(sheet_order)):
        if label not in by_sheet:
            continue
        cats = [{"name": name, "count": count} for name, count in by_sheet[label].items()]
        cats.sort(key=lambda c: -c["count"])
        groups.append({"group": label, "categories": cats})

    return {"groups": groups}


# AMC (fund house) name prefix -> official domain, used to fetch a real
# logo via Google's public favicon service (keyless, no signup - Clearbit's
# free logo API shut down Dec 2025 and its replacement requires an API key).
# Deliberately only covers AMCs whose domain we're confident about; longer/
# more specific prefixes are matched first so e.g. "Aditya Birla SL" matches
# before a shorter, wrong prefix would. Anything not listed here simply gets
# no logo and the app falls back to a colored initial - safer than risking
# a wrong domain's favicon.
_AMC_DOMAINS = [
    ("Aditya Birla SL", "adityabirlacapital.com"),
    ("Aditya Birla Sun Life", "adityabirlacapital.com"),
    ("Axis", "axismf.com"),
    ("Bajaj Finserv", "bajajamc.com"),
    ("Bandhan", "bandhanmutual.com"),
    ("Baroda BNP Paribas", "barodabnpparibasmf.in"),
    ("Canara Rob", "canararobeco.com"),
    ("DSP", "dspim.com"),
    ("Edelweiss", "edelweissmf.com"),
    ("Franklin", "franklintempletonindia.com"),
    ("Groww", "groww.in"),
    ("HDFC", "hdfcfund.com"),
    ("HSBC", "assetmanagement.hsbc.co.in"),
    ("ICICI Pru", "icicipruamc.com"),
    ("Invesco", "invescomutualfund.com"),
    ("ITI", "itimf.com"),
    ("JM", "jmfinancialmf.com"),
    ("Kotak", "kotakmf.com"),
    ("LIC MF", "licmf.com"),
    ("Mahindra Manulife", "mahindramanulife.com"),
    ("Mirae Asset", "miraeassetmf.co.in"),
    ("Motilal Oswal", "motilaloswalmf.com"),
    ("Navi", "navi.com"),
    ("Nippon India", "mf.nipponindiaim.com"),
    ("NJ", "njmutualfund.com"),
    ("Parag Parikh", "ppfas.com"),
    ("PGIM India", "pgimindiamf.com"),
    ("Quantum", "quantumamc.com"),
    ("Quant", "quantmutual.com"),
    ("SBI", "sbimf.com"),
    ("Sundaram", "sundarammutual.com"),
    ("Tata", "tatamutualfund.com"),
    ("Taurus", "taurusmutualfund.com"),
    ("TRUSTMF", "trustmf.com"),
    ("UTI", "utimf.com"),
    ("WOC", "whiteoakamc.com"),
    ("360 ONE", "360one.com"),
    ("Angel One", "angelone.in"),
]


def _amc_domain(fund_name: str) -> str | None:
    """Longest-prefix match against the AMC map above."""
    for prefix, domain in sorted(_AMC_DOMAINS, key=lambda p: -len(p[0])):
        if fund_name.startswith(prefix):
            return domain
    return None


def _fund_public_fields(fund: dict) -> dict:
    """Every field funds_screen.dart reads for a fund card/detail row,
    passed straight through from the JSON. nav_live/nav_date/
    nav_change_pct are honestly None - this snapshot doesn't carry a
    live intraday NAV feed, unlike the rest of the static metrics."""
    return {
        "name": fund.get("name"), "classification": fund.get("classification"),
        "manager": fund.get("manager"), "amc": fund.get("amc"),
        "nav": fund.get("nav"), "nav_live": False, "nav_date": None, "nav_change_pct": None,
        "nav_52w_high": fund.get("nav_52w_high"), "nav_52w_low": fund.get("nav_52w_low"),
        "r_1m": fund.get("r_1m"), "r_3m": fund.get("r_3m"), "r_6m": fund.get("r_6m"),
        "r_1y": fund.get("r_1y"), "r_2y": fund.get("r_2y"), "r_3y": fund.get("r_3y"),
        "r_5y": fund.get("r_5y"), "r_10y": fund.get("r_10y"),
        "aum": fund.get("aum"), "expense_ratio": fund.get("expense_ratio"),
        "benchmark": fund.get("benchmark"), "inception": fund.get("inception"),
        "fund_type": fund.get("fund_type"), "exit_load": fund.get("exit_load"),
        "sharpe": fund.get("sharpe"), "sortino": fund.get("sortino"),
        "alpha": fund.get("alpha"), "beta": fund.get("beta"), "std_dev": fund.get("std_dev"),
        "pe": fund.get("pe"), "turnover": fund.get("turnover"), "avg_mcap": fund.get("avg_mcap"),
        "pct_large": fund.get("pct_large"), "pct_mid": fund.get("pct_mid"), "pct_small": fund.get("pct_small"),
        "top_sector": fund.get("top_sector"), "avg_maturity": fund.get("avg_maturity"),
        "mod_duration": fund.get("mod_duration"), "ytm": fund.get("ytm"),
        "key": fund.get("key"),
        "amc_domain": _amc_domain(fund.get("name") or ""),
    }


@app.get("/funds/db/list")
def funds_db_list(category: str, sort: str = "aum", limit: int = 60):
    data = _load_funds_data()
    matches = [f for f in data if _fund_category_name(f) == category]
    total_in_category = len(matches)

    reverse = sort not in ("expense_ratio",)  # lower expense ratio ranks first
    matches.sort(key=lambda f: (f.get(sort) is None, f.get(sort) or 0), reverse=reverse)

    shown = matches[:limit]
    nav_matched = sum(1 for f in shown if f.get("nav") is not None)

    return {
        "funds": [_fund_public_fields(f) for f in shown],
        "count": len(shown), "nav_matched": nav_matched, "total_in_category": total_in_category,
    }


@app.get("/funds/db/search")
def funds_db_search(q: str):
    data = _load_funds_data()
    query = q.strip().lower()
    if len(query) < 2:
        raise HTTPException(400, "Query must be at least 2 characters")
    matches = [f for f in data if query in (f.get("name") or "").lower()][:40]
    return {"funds": [_fund_public_fields(f) for f in matches]}


@app.get("/funds/db/fund")
def funds_db_fund(name: str):
    data = _load_funds_data()
    target_key = name.strip().lower()
    match = next((f for f in data if (f.get("key") or "").lower() == target_key
                 or (f.get("name") or "").lower() == target_key), None)
    if not match:
        raise HTTPException(404, f"Fund '{name}' not found")

    peers = [f for f in data if _fund_category_name(f) == _fund_category_name(match)]

    def _rank_of(field: str, higher_is_better: bool):
        vals = [(f.get("key"), f.get(field)) for f in peers if f.get(field) is not None]
        if not vals or match.get(field) is None:
            return None
        vals.sort(key=lambda v: v[1], reverse=higher_is_better)
        for i, (k, _) in enumerate(vals):
            if k == match.get("key"):
                return {"rank": i + 1, "of": len(vals)}
        return None

    result = _fund_public_fields(match)
    result["ranks"] = {
        "aum": _rank_of("aum", higher_is_better=True),
        "expense_ratio": _rank_of("expense_ratio", higher_is_better=False),
        "sharpe": _rank_of("sharpe", higher_is_better=True),
    }
    return result

@app.get("/funds/search")
def fund_search(q: str):
    """Search all Indian mutual funds by name."""
    import requests
    if len(q) < 3:
        raise HTTPException(400, "Query must be at least 3 characters")
    try:
        r = requests.get(f"https://api.mfapi.in/mf/search?q={q}", timeout=10)
        return {"results": r.json()[:25] if r.status_code == 200 else []}
    except Exception as e:
        raise HTTPException(502, f"Fund search failed: {e}")


@app.get("/funds/{scheme_code}")
def fund_detail(scheme_code: str):
    """NAV history + returns for one scheme."""
    import requests
    try:
        r = requests.get(f"https://api.mfapi.in/mf/{scheme_code}", timeout=10)
        if r.status_code != 200:
            raise HTTPException(404, "Scheme not found")
        payload = r.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Fund fetch failed: {e}")

    return {"meta": payload.get("meta", {}),
            "nav_history": payload.get("data", [])[:400]}


# ===========================================================================
# PORTFOLIO DOCTOR  (your unique feature)
# ===========================================================================
class Holding(BaseModel):
    symbol: str
    invested: float
    current_value: Optional[float] = None
    pnl_pct: Optional[float] = None


class DoctorRequest(BaseModel):
    holdings: list[Holding]


@app.post("/doctor")
def portfolio_doctor(req: DoctorRequest):
    """
    Real diagnostics on a portfolio: concentration (effective holdings),
    hidden correlation between stocks, sector tilt, vs-NIFTY performance.
    """
    import pandas as pd
    from pf_doctor import diagnose

    if len(req.holdings) < 2:
        raise HTTPException(400, "Need at least 2 holdings to diagnose")

    df = pd.DataFrame([h.model_dump() for h in req.holdings])
    try:
        findings = diagnose(df)
    except Exception as e:
        raise HTTPException(502, f"Diagnosis failed: {e}")

    if not findings:
        raise HTTPException(422, "Not enough data to diagnose")
    return _clean(findings)


# ===========================================================================
# PORTFOLIO X-RAY  (your other unique feature)
# ===========================================================================
class FundHolding(BaseModel):
    scheme_code: str
    name: str
    value: float


class XrayRequest(BaseModel):
    funds: list[FundHolding]


@app.post("/xray")
def portfolio_xray(req: XrayRequest):
    """
    Look through mutual funds to the underlying stocks — reveals that your
    'diversified' funds may all hold the same companies.
    """
    from pf_xray import xray

    if not req.funds:
        raise HTTPException(400, "Add at least one fund")

    try:
        result = xray([f.model_dump() for f in req.funds])
    except Exception as e:
        raise HTTPException(502, f"X-ray failed: {e}")

    if not result.get("stocks"):
        raise HTTPException(422, "No holdings data available for these funds")
    return _clean(result)


# ===========================================================================
# AUTH  (same Supabase users as the Streamlit app)
# ===========================================================================
import os


def _supabase():
    """Supabase client, or None if not configured."""
    try:
        from supabase import create_client
        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_KEY")
        if not url or not key:
            return None
        return create_client(url, key)
    except Exception:
        return None


class AuthRequest(BaseModel):
    email: str
    password: str
    name: Optional[str] = None


@app.post("/auth/login")
def login(req: AuthRequest):
    """Verify credentials against the same bcrypt hashes the web app uses."""
    import bcrypt

    sb = _supabase()
    if sb is None:
        raise HTTPException(500, "Database not configured on the server")

    email = req.email.strip().lower()
    try:
        res = sb.table("users").select("*").eq("email", email).execute()
        rec = res.data[0] if res.data else None
    except Exception as e:
        raise HTTPException(502, f"Database error: {e}")

    if not rec:
        raise HTTPException(401, "Email or password doesn't match")

    stored = rec.get("password_hash", "")
    try:
        ok = bcrypt.checkpw(req.password.encode(), stored.encode())
    except (ValueError, TypeError):
        ok = False   # old SHA-256 record, or malformed hash

    if not ok:
        raise HTTPException(401, "Email or password doesn't match")

    return {"email": email, "name": rec.get("name") or email.split("@")[0]}


@app.post("/auth/signup")
def signup(req: AuthRequest):
    """Create an account. Password is hashed with bcrypt, never stored raw."""
    import bcrypt

    sb = _supabase()
    if sb is None:
        raise HTTPException(500, "Database not configured on the server")

    email = req.email.strip().lower()
    if "@" not in email or len(req.password) < 6:
        raise HTTPException(400,
                            "Enter a valid email and a password of 6+ characters")

    try:
        existing = sb.table("users").select("email").eq("email", email).execute()
        if existing.data:
            raise HTTPException(409, "An account with this email already exists")

        pw_hash = bcrypt.hashpw(req.password.encode(), bcrypt.gensalt()).decode()
        sb.table("users").insert({
            "email": email,
            "password_hash": pw_hash,
            "name": req.name or email.split("@")[0].title(),
        }).execute()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Could not create account: {e}")

    return {"email": email, "name": req.name or email.split("@")[0].title()}


# ===========================================================================
# HOLDINGS  (persisted per user — same table as the web app)
# ===========================================================================
class HoldingIn(BaseModel):
    symbol: str
    qty: float
    avg_price: float



# =============================================================================
# HOLDINGS EXPLORER — AMC portfolio disclosures
# =============================================================================
# Rebuilt from scratch after the previously-deployed version (source
# unknown/lost - not present in api.py, api_full.py, or analysis_api.py,
# only confirmed live via /docs) turned out to only expose 12 AMCs. This
# version loads from holdings_data.json (converted from the same
# 32-AMC/516-fund disclosure CSV used by the Crescent web app's Holdings
# Explorer, for consistency across both platforms).
#
# Every field name below was reverse-engineered directly from the Flutter
# app's actual usage (holdings_screen.dart), not assumed - each endpoint's
# response shape matches exactly what the app already reads.
# =============================================================================

import json as _hj
import os as _ho
from collections import defaultdict as _hdefaultdict

_HOLDINGS_DATA: list | None = None


def _load_holdings_data() -> list:
    global _HOLDINGS_DATA
    if _HOLDINGS_DATA is None:
        path = _ho.path.join(_ho.path.dirname(__file__), "holdings_data.json")
        with open(path, encoding="utf-8") as f:
            _HOLDINGS_DATA = _hj.load(f)
    return _HOLDINGS_DATA


@app.get("/holdings/summary")
def holdings_summary():
    """Coverage of the disclosure snapshot - funds, AMCs, and as-on date."""
    data = _load_holdings_data()
    amc_names = sorted(set(f["amc"] for f in data))
    as_on = (data[0]["as_on_date"] if data else "") or "31-Jul-2026"
    return {
        "funds": len(data),
        "amcs": len(amc_names),
        "amc_names": amc_names,
        "as_on": as_on,
    }


@app.get("/holdings/funds")
def holdings_funds(amc: str = ""):
    """Funds in the snapshot, optionally filtered to one AMC."""
    data = _load_holdings_data()
    out = [
        {"key": f["key"], "fund": f["fund"], "amc": f["amc"]}
        for f in data
        if not amc or f["amc"] == amc
    ]
    return {"funds": out}


@app.get("/holdings/fund")
def holdings_fund(key: str):
    """One fund's full disclosed portfolio - holdings, sector split, stats."""
    data = _load_holdings_data()
    match = next((f for f in data if f["key"] == key), None)
    if not match:
        raise HTTPException(404, "Fund not found")

    holdings = sorted(
        [h for h in match["holdings"] if h.get("pct_nav") is not None],
        key=lambda h: -h["pct_nav"],
    )
    disclosed_pct = sum(h["pct_nav"] for h in holdings)
    top10_pct = sum(h["pct_nav"] for h in holdings[:10])

    sector_totals: dict = _hdefaultdict(float)
    for h in holdings:
        sector_totals[h.get("sector") or "Unknown"] += h["pct_nav"]
    sectors = sorted(
        [{"industry": k, "pct_nav": round(v, 2)} for k, v in sector_totals.items()],
        key=lambda s: -s["pct_nav"],
    )

    return {
        "fund": match["fund"],
        "amc": match["amc"],
        "holdings": holdings,
        "sectors": sectors,
        "count": len(holdings),
        "disclosed_pct": round(disclosed_pct, 2),
        "top10_pct": round(top10_pct, 2),
    }


@app.get("/holdings/overlap")
def holdings_overlap(a: str, b: str):
    """Portfolio overlap between two funds - sum of the smaller weight of
    each holding they share, matched by ISIN (falls back to instrument
    name if ISIN is missing for either side)."""
    data = _load_holdings_data()
    fund_a = next((f for f in data if f["key"] == a), None)
    fund_b = next((f for f in data if f["key"] == b), None)
    if not fund_a or not fund_b:
        raise HTTPException(404, "Fund not found")

    def _index(fund):
        idx = {}
        for h in fund["holdings"]:
            if h.get("pct_nav") is None:
                continue
            k = h.get("isin") or h["instrument"]
            idx[k] = h
        return idx

    holdings_a = _index(fund_a)
    holdings_b = _index(fund_b)

    common_keys = set(holdings_a.keys()) & set(holdings_b.keys())
    common = []
    overlap_pct = 0.0
    for k in common_keys:
        ha, hb = holdings_a[k], holdings_b[k]
        pct_a, pct_b = ha["pct_nav"], hb["pct_nav"]
        min_w = min(pct_a, pct_b)
        overlap_pct += min_w
        common.append({
            "instrument": ha["instrument"],
            "pct_a": round(pct_a, 2),
            "pct_b": round(pct_b, 2),
        })
    common.sort(key=lambda c: -min(c["pct_a"], c["pct_b"]))

    if overlap_pct > 60:
        band, verdict = "very_high", "Very high overlap — these funds are largely redundant"
    elif overlap_pct > 40:
        band, verdict = "high", "High overlap — significant redundancy"
    elif overlap_pct > 20:
        band, verdict = "moderate", "Moderate overlap"
    else:
        band, verdict = "low", "Low overlap — good diversification"

    return {
        "overlap_pct": round(overlap_pct, 2),
        "band": band,
        "verdict": verdict,
        "common_count": len(common),
        "common": common,
        "fund_a": {"fund": fund_a["fund"], "holdings": len(holdings_a)},
        "fund_b": {"fund": fund_b["fund"], "holdings": len(holdings_b)},
    }


@app.get("/holdings/stock")
def holdings_stock(q: str):
    """Which funds hold a given stock, and how much of each fund's NAV
    it represents. Matches on instrument name containing the query
    (case-insensitive) - the same approach as the web app's version."""
    data = _load_holdings_data()
    query = q.strip().lower()
    if len(query) < 3:
        raise HTTPException(400, "Query must be at least 3 characters")

    by_instrument: dict = _hdefaultdict(list)
    sectors_by_instrument: dict = {}
    for fund in data:
        for h in fund["holdings"]:
            name = h.get("instrument") or ""
            if query not in name.lower():
                continue
            if h.get("pct_nav") is None:
                continue
            by_instrument[name].append({
                "fund": fund["fund"],
                "pct_nav": h["pct_nav"],
                "value_lakh": h.get("value_lakh"),
            })
            sectors_by_instrument.setdefault(name, h.get("sector") or "Unknown")

    if not by_instrument:
        raise HTTPException(404, "No matching securities found")

    securities = []
    for name, holders in by_instrument.items():
        holders.sort(key=lambda h: -h["pct_nav"])
        total_value_cr = sum((h.get("value_lakh") or 0) for h in holders) / 100
        securities.append({
            "instrument": name,
            "industry": sectors_by_instrument.get(name, "Unknown"),
            "fund_count": len(holders),
            "total_value_cr": round(total_value_cr, 1),
            "max_weight": round(holders[0]["pct_nav"], 2) if holders else 0,
            "holders": holders,
        })
    securities.sort(key=lambda s: -s["fund_count"])

    return {"securities": securities}

@app.get("/holdings/{email}")
def get_holdings(email: str):
    """This user's saved holdings."""
    sb = _supabase()
    if sb is None:
        raise HTTPException(500, "Database not configured")
    try:
        res = (sb.table("holdings")
                 .select("symbol,qty,avg_price")
                 .eq("user_email", email.strip().lower())
                 .order("symbol").execute())
        return {"holdings": res.data or []}
    except Exception as e:
        raise HTTPException(502, f"Could not load holdings: {e}")


@app.post("/holdings/{email}")
def save_holding(email: str, h: HoldingIn):
    """
    Add a holding, MERGING into an existing position with a weighted-average
    price rather than creating a duplicate row.
    """
    sb = _supabase()
    if sb is None:
        raise HTTPException(500, "Database not configured")

    email = email.strip().lower()
    symbol = h.symbol.strip().upper()

    try:
        res = (sb.table("holdings").select("qty,avg_price")
                 .eq("user_email", email).eq("symbol", symbol).execute())
        existing = res.data[0] if res.data else None

        if existing:
            old_q = float(existing["qty"])
            old_a = float(existing["avg_price"])
            new_q = old_q + h.qty
            new_a = round((old_q * old_a + h.qty * h.avg_price) / new_q, 2)
            action = "merged"
        else:
            new_q, new_a, action = h.qty, h.avg_price, "added"

        sb.table("holdings").upsert({
            "user_email": email, "symbol": symbol,
            "qty": new_q, "avg_price": new_a,
        }, on_conflict="user_email,symbol").execute()
    except Exception as e:
        raise HTTPException(502, f"Could not save holding: {e}")

    return {"action": action, "symbol": symbol, "qty": new_q, "avg_price": new_a}


@app.delete("/holdings/{email}/{symbol}")
def delete_holding(email: str, symbol: str):
    sb = _supabase()
    if sb is None:
        raise HTTPException(500, "Database not configured")
    try:
        (sb.table("holdings").delete()
           .eq("user_email", email.strip().lower())
           .eq("symbol", symbol.strip().upper()).execute())
    except Exception as e:
        raise HTTPException(502, f"Could not delete: {e}")
    return {"deleted": symbol.upper()}


# ===========================================================================
# AI ANALYSIS
# ===========================================================================
class AIRequest(BaseModel):
    facts: dict
    kind: str = "stock"          # "stock" or "fund"


@app.post("/ai/analyse")
def ai_analyse(req: AIRequest):
    """Plain-English AI analysis of a stock or fund, from real data."""
    import os
    try:
        from google import genai
    except ImportError:
        raise HTTPException(500, "google-genai not installed")

    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise HTTPException(500, "GEMINI_API_KEY not set on the server")

    thing = "mutual fund" if req.kind == "fund" else "stock"
    facts = "\n".join(f"- {k}: {v}" for k, v in req.facts.items()
                      if v not in (None, "", 0))
    prompt = f"""You are a financial research assistant for a retail investor in India. You are NOT giving buy/sell advice.

Factual data for this {thing}:
{facts}

Write a plain-English analysis with these sections:
**Overview** — what this {thing} is, in 1-2 sentences.
**Recent performance** — what the figures above suggest.
**Strengths** — 2-3 positives supported by the data.
**Risks & things to watch** — 2-3 honest risks; be balanced.

Rules: use ONLY the data above; invent no numbers or news. Never say whether to buy, sell, or hold. Under 280 words."""

    try:
        client = genai.Client(api_key=key)
        resp = client.models.generate_content(
            model="gemini-flash-latest", contents=prompt)
        return {"analysis": (resp.text or "").strip()}
    except Exception as e:
        raise HTTPException(502, f"AI request failed: {e}")
