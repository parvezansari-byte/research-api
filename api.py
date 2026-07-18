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
    The full searchable universe (~500 names): NIFTY 100 + Midcap 150 +
    Smallcap 250. Just the symbols — full data loads when a stock is opened.
    Cached implicitly by clients; the list changes rarely.
    """
    from analysis_api import get_universe

    names: list[str] = []
    seen = set()
    for uni in ("LARGECAP", "MIDCAP", "SMALLCAP"):
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


# ===========================================================================
# FUND CATEGORIES
# ===========================================================================
#
# mfapi's scheme list carries no category field — category only appears in the
# per-scheme `meta`, so grouping all ~10,000 schemes would need ~10,000 calls.
#
# Instead each category is defined by search terms. We resolve them against
# mfapi's own search endpoint at request time, then keep direct-growth plans.
# That way the scheme codes are never stale or hand-typed, and new funds show
# up on their own.
FUND_CATEGORIES: dict[str, list[str]] = {
    "Large Cap": ["large cap fund", "bluechip fund", "top 100 fund"],
    "Mid Cap": ["midcap fund", "mid cap fund"],
    "Small Cap": ["small cap fund", "smallcap fund"],
    "Flexi Cap": ["flexi cap fund", "flexicap fund"],
    "Large & Mid Cap": ["large and mid cap", "large & midcap", "equity opportunities"],
    "ELSS (Tax Saving)": ["elss tax saver", "tax saver fund", "long term equity fund"],
    "Index / Passive": ["nifty 50 index fund", "sensex index fund", "nifty next 50 index"],
    "Hybrid / Balanced": ["balanced advantage fund", "aggressive hybrid fund", "multi asset allocation"],
    "Debt / Short Duration": ["short duration fund", "short term fund", "corporate bond fund"],
}

# How many schemes to analyse per category. Each is a separate upstream call,
# so this trades completeness for a response that returns in a few seconds.
_FUNDS_PER_CATEGORY = 12


def _is_direct_growth(name: str) -> bool:
    """Prefer direct growth plans — same fund, lower expense ratio."""
    low = name.lower()
    return "direct" in low and "growth" in low and "idcw" not in low \
        and "dividend" not in low


def _cagr(start: float, end: float, years: float):
    """Annualised growth, or None when the inputs can't support it."""
    if start <= 0 or end <= 0 or years <= 0:
        return None
    return round(((end / start) ** (1 / years) - 1) * 100, 2)


def _analyse_nav(data: list) -> dict:
    """
    Turn mfapi's NAV series into headline return figures.

    `data` arrives newest-first as [{"date": "dd-mm-yyyy", "nav": "123.45"}].
    """
    from datetime import datetime

    points = []
    for row in data:
        try:
            d = datetime.strptime(row["date"], "%d-%m-%Y")
            n = float(row["nav"])
            if n > 0:
                points.append((d, n))
        except (KeyError, ValueError, TypeError):
            continue

    if not points:
        return {}

    points.sort(key=lambda p: p[0], reverse=True)
    latest_date, latest_nav = points[0]
    oldest_date, oldest_nav = points[-1]

    def nav_years_ago(years: int):
        try:
            target = latest_date.replace(year=latest_date.year - years)
        except ValueError:            # 29 Feb
            target = latest_date.replace(year=latest_date.year - years, day=28)
        if oldest_date > target:
            return None
        best, best_gap = None, None
        for d, n in points:
            gap = abs((d - target).days)
            if best_gap is None or gap < best_gap:
                best, best_gap = (d, n), gap
        return best if best_gap is not None and best_gap <= 60 else None

    def ret(years: int):
        past = nav_years_ago(years)
        if past is None:
            return None
        actual = (latest_date - past[0]).days / 365.25
        return _cagr(past[1], latest_nav, actual)

    # Volatility and drawdown from month-end NAVs.
    monthly, seen = [], set()
    for d, n in reversed(points):
        key = (d.year, d.month)
        if key not in seen:
            seen.add(key)
            monthly.append(n)

    vol = None
    if len(monthly) >= 13:
        rets = [monthly[i] / monthly[i - 1] - 1 for i in range(1, len(monthly))]
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        vol = round((var ** 0.5) * (12 ** 0.5) * 100, 2)

    peak, max_dd = 0.0, 0.0
    for n in monthly:
        peak = max(peak, n)
        if peak > 0:
            max_dd = min(max_dd, (n - peak) / peak)

    inception_years = (latest_date - oldest_date).days / 365.25

    return {
        "latest_nav": round(latest_nav, 4),
        "nav_date": latest_date.strftime("%d-%m-%Y"),
        "return_1y": ret(1),
        "return_3y": ret(3),
        "return_5y": ret(5),
        "since_inception": _cagr(oldest_nav, latest_nav, inception_years)
        if inception_years >= 1 else None,
        "volatility": vol,
        "max_drawdown": round(max_dd * 100, 2) if max_dd < 0 else None,
        "history_years": round(inception_years, 1),
    }


@app.get("/funds/categories")
def fund_categories():
    """The category names available to browse."""
    return {"categories": list(FUND_CATEGORIES.keys())}


@app.get("/funds/category/{category}")
def funds_by_category(category: str, sort: str = "return_3y"):
    """
    Live returns for the leading funds in one category, ranked.

    Scheme codes are resolved from mfapi's search endpoint rather than stored,
    so the list stays current without maintenance.
    """
    import requests
    from concurrent.futures import ThreadPoolExecutor

    matched = None
    for name in FUND_CATEGORIES:
        if name.lower() == category.lower():
            matched = name
            break
    if matched is None:
        raise HTTPException(
            404, f"Unknown category '{category}'. "
                 f"Options: {', '.join(FUND_CATEGORIES)}")

    # --- resolve scheme codes from search terms ---
    seen_codes: dict[str, str] = {}
    for term in FUND_CATEGORIES[matched]:
        try:
            r = requests.get("https://api.mfapi.in/mf/search",
                             params={"q": term}, timeout=12)
            if r.status_code != 200:
                continue
            for row in r.json():
                name = str(row.get("schemeName", ""))
                code = str(row.get("schemeCode", ""))
                if code and _is_direct_growth(name) and code not in seen_codes:
                    seen_codes[code] = name
        except Exception:
            continue
        if len(seen_codes) >= _FUNDS_PER_CATEGORY * 2:
            break

    if not seen_codes:
        raise HTTPException(
            502, f"Could not resolve any schemes for '{matched}'")

    codes = list(seen_codes)[:_FUNDS_PER_CATEGORY]

    def fetch(code: str):
        try:
            r = requests.get(f"https://api.mfapi.in/mf/{code}", timeout=12)
            if r.status_code != 200:
                return None
            payload = r.json()
            meta = payload.get("meta") or {}
            stats = _analyse_nav(payload.get("data") or [])
            if not stats:
                return None
            return {
                "scheme_code": code,
                "name": meta.get("scheme_name", seen_codes.get(code, "Unknown")),
                "fund_house": meta.get("fund_house", "—"),
                "category": meta.get("scheme_category", matched),
                **stats,
            }
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = [f for f in pool.map(fetch, codes) if f]

    key = sort if sort in {"return_1y", "return_3y", "return_5y",
                           "since_inception", "volatility"} else "return_3y"
    # Funds missing the sort window sink to the bottom rather than erroring.
    # Volatility sorts ascending — less risk ranks better.
    if key == "volatility":
        results.sort(key=lambda f: (f.get(key) is None, f.get(key) or 1e9))
    else:
        results.sort(key=lambda f: (f.get(key) is None, -(f.get(key) or 0)))

    return _clean({"category": matched, "count": len(results),
                   "funds": results})


@app.get("/funds/{scheme_code}/analysis")
def fund_analysis(scheme_code: str):
    """Full stats for one scheme, including a downsampled NAV series."""
    import requests
    try:
        r = requests.get(f"https://api.mfapi.in/mf/{scheme_code}", timeout=15)
        if r.status_code != 200:
            raise HTTPException(404, "Scheme not found")
        payload = r.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Fund fetch failed: {e}")

    data = payload.get("data") or []
    stats = _analyse_nav(data)
    if not stats:
        raise HTTPException(422, "No usable NAV history for this scheme")

    # Roughly 120 points is enough to draw a smooth chart.
    step = max(len(data) // 120, 1)
    chart = [{"date": row["date"], "nav": float(row["nav"])}
             for row in data[::step][:120]
             if row.get("nav") not in (None, "")]

    return _clean({
        "meta": payload.get("meta", {}),
        **stats,
        "chart": list(reversed(chart)),
    })


@app.get("/funds/{scheme_code}/sip-backtest")
def fund_sip_backtest(scheme_code: str, monthly: float = 10000,
                      years: int = 5):
    """
    What a monthly SIP into this scheme would actually have been worth.

    Uses real historical NAVs: each month buys units at that month's NAV, and
    the final value is total units times the latest NAV.
    """
    import requests
    from datetime import datetime

    try:
        r = requests.get(f"https://api.mfapi.in/mf/{scheme_code}", timeout=15)
        if r.status_code != 200:
            raise HTTPException(404, "Scheme not found")
        payload = r.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Fund fetch failed: {e}")

    points = []
    for row in payload.get("data") or []:
        try:
            points.append((datetime.strptime(row["date"], "%d-%m-%Y"),
                           float(row["nav"])))
        except (KeyError, ValueError, TypeError):
            continue
    if not points:
        raise HTTPException(422, "No usable NAV history")

    points.sort(key=lambda p: p[0])
    latest_date, latest_nav = points[-1]

    try:
        start = latest_date.replace(year=latest_date.year - years)
    except ValueError:
        start = latest_date.replace(year=latest_date.year - years, day=28)
    if points[0][0] > start:
        raise HTTPException(
            422, f"This scheme has less than {years} years of history")

    # First NAV on or after each monthly instalment date.
    units, invested, rows = 0.0, 0.0, []
    cursor = start
    while cursor <= latest_date:
        buy = next((p for p in points if p[0] >= cursor), None)
        if buy is None:
            break
        units += monthly / buy[1]
        invested += monthly
        rows.append({
            "date": buy[0].strftime("%d-%m-%Y"),
            "nav": round(buy[1], 4),
            "invested": round(invested, 2),
            "value": round(units * buy[1], 2),
        })
        month = cursor.month + 1
        year = cursor.year + (1 if month > 12 else 0)
        month = 1 if month > 12 else month
        day = min(cursor.day, 28)
        cursor = cursor.replace(year=year, month=month, day=day)

    if not rows:
        raise HTTPException(422, "Could not build a SIP schedule")

    final_value = units * latest_nav
    gain = final_value - invested

    # Money-weighted return: solve for the rate that grows the instalments to
    # the observed value, rather than quoting a simple gain percentage.
    def sip_fv(rate: float) -> float:
        rr = rate / 12
        n = len(rows)
        if rr <= 0:
            return monthly * n
        return monthly * (((1 + rr) ** n - 1) / rr)

    lo, hi = -0.9, 1.0
    for _ in range(100):
        mid = (lo + hi) / 2
        if sip_fv(mid) < final_value:
            lo = mid
        else:
            hi = mid
    xirr = round(hi * 100, 2)

    return _clean({
        "scheme_code": scheme_code,
        "name": (payload.get("meta") or {}).get("scheme_name", "Unknown"),
        "monthly": monthly,
        "years": years,
        "instalments": len(rows),
        "invested": round(invested, 2),
        "final_value": round(final_value, 2),
        "gain": round(gain, 2),
        "gain_pct": round(gain / invested * 100, 2) if invested else None,
        "xirr_pct": xirr,
        "latest_nav": round(latest_nav, 4),
        "schedule": rows[::max(len(rows) // 60, 1)],
    })


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

    # ~2000 daily observations covers roughly eight years, enough to compute
    # 3Y and 5Y CAGR client-side. 400 only reached about 18 months, which made
    # the longer return windows permanently unavailable.
    return {"meta": payload.get("meta", {}),
            "nav_history": payload.get("data", [])[:2000]}


# ===========================================================================
# MARKET NEWS  (free RSS, no API key)
# ===========================================================================
NEWS_FEEDS = {
    "Economic Times Markets":
        "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "ET Stocks":
        "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms",
    "Moneycontrol Markets":
        "https://www.moneycontrol.com/rss/marketreports.xml",
    "Moneycontrol Business":
        "https://www.moneycontrol.com/rss/business.xml",
    "Livemint Markets":
        "https://www.livemint.com/rss/markets",
    "Business Standard Markets":
        "https://www.business-standard.com/rss/markets-106.rss",
}

_POSITIVE = ("surge", "rally", "gain", "jump", "rise", "record high", "profit",
             "beats", "upgrade", "bullish", "soar", "growth", "strong", "boost",
             "buy", "outperform", "up ", "hits high", "best")
_NEGATIVE = ("fall", "drop", "crash", "plunge", "loss", "decline", "slump",
             "downgrade", "bearish", "weak", "cuts", "misses", "sell-off",
             "selloff", "tumble", "down ", "fears", "worst", "fraud", "probe")
_HIGH_IMPACT = ("rbi", "sebi", "fed", "budget", "gdp", "inflation", "rate cut",
                "rate hike", "repo", "election", "tariff", "crash", "record",
                "nifty", "sensex", "results", "earnings", "ipo", "merger",
                "acquisition", "crude", "rupee")


def _sentiment(text: str) -> str:
    t = text.lower()
    pos = sum(w in t for w in _POSITIVE)
    neg = sum(w in t for w in _NEGATIVE)
    if pos > neg:
        return "Positive"
    if neg > pos:
        return "Negative"
    return "Neutral"


def _impact(text: str) -> str:
    hits = sum(w in text.lower() for w in _HIGH_IMPACT)
    return "High" if hits >= 2 else ("Medium" if hits == 1 else "Low")


def _strip_html(s: str) -> str:
    import html as html_lib
    import re
    s = re.sub(r"<[^>]+>", " ", s or "")
    return html_lib.unescape(re.sub(r"\s+", " ", s)).strip()


def _entry_image(entry, raw_summary: str) -> str:
    """Article image from RSS media tags, enclosures, or an inline <img>."""
    import re
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
            if "image" in enc.get("type", "") and \
                    enc.get("href", "").startswith("http"):
                return enc["href"]
        m = re.search(r'<img[^>]+src=["\'](http[^"\']+)["\']', raw_summary or "")
        if m:
            return m.group(1)
    except Exception:
        pass
    return ""


@app.get("/news")
def market_news(limit: int = 60, sentiment: str = "", impact: str = ""):
    """
    Live market news aggregated from Indian financial RSS feeds.

    Each item is tagged with a keyword-based sentiment and impact score. That
    is a crude heuristic, not real NLP — it is there to help scanning, not to
    be traded on.
    """
    from datetime import datetime, timezone
    from concurrent.futures import ThreadPoolExecutor

    try:
        import feedparser
    except ImportError:
        raise HTTPException(
            503, "feedparser is not installed on the server "
                 "(add 'feedparser' to requirements.txt)")

    def age(dt):
        if dt is None:
            return ""
        mins = int((datetime.now(timezone.utc) - dt).total_seconds() // 60)
        if mins < 60:
            return f"{mins}m ago"
        if mins < 1440:
            return f"{mins // 60}h ago"
        return f"{mins // 1440}d ago"

    def pull(item):
        source, url = item
        out = []
        try:
            feed = feedparser.parse(url)
            for e in feed.entries[:15]:
                title = _strip_html(getattr(e, "title", ""))
                if not title:
                    continue
                raw = getattr(e, "summary", "")
                summary = _strip_html(raw)[:400]
                published = None
                for attr in ("published_parsed", "updated_parsed"):
                    tp = getattr(e, attr, None)
                    if tp:
                        published = datetime(*tp[:6], tzinfo=timezone.utc)
                        break
                text = f"{title} {summary}"
                out.append({
                    "title": title,
                    "summary": summary,
                    "link": getattr(e, "link", ""),
                    "source": source,
                    "published": published.isoformat() if published else None,
                    "age": age(published),
                    "image": _entry_image(e, raw),
                    "sentiment": _sentiment(text),
                    "impact": _impact(text),
                    "_ts": published.timestamp() if published else 0,
                })
        except Exception:
            pass
        return out

    with ThreadPoolExecutor(max_workers=6) as pool:
        batches = list(pool.map(pull, NEWS_FEEDS.items()))

    articles, seen = [], set()
    for batch in batches:
        for a in batch:
            key = a["title"].lower()[:80]
            if key not in seen:
                seen.add(key)
                articles.append(a)

    if sentiment:
        articles = [a for a in articles
                    if a["sentiment"].lower() == sentiment.lower()]
    if impact:
        articles = [a for a in articles
                    if a["impact"].lower() == impact.lower()]

    articles.sort(key=lambda a: a["_ts"], reverse=True)
    for a in articles:
        a.pop("_ts", None)

    counts = {"positive": 0, "negative": 0, "neutral": 0, "high_impact": 0}
    for a in articles:
        counts[a["sentiment"].lower()] += 1
        if a["impact"] == "High":
            counts["high_impact"] += 1

    return _clean({
        "count": len(articles[:limit]),
        "total_fetched": len(articles),
        "sources": list(NEWS_FEEDS),
        "summary": counts,
        "articles": articles[:limit],
    })


# ===========================================================================
# FII / DII INSTITUTIONAL FLOWS
# ===========================================================================
@app.get("/fii-dii")
def fii_dii():
    """
    Daily FII/DII cash-market activity in ₹ crore.

    NSE blocks most datacenter IPs, so this tries several sources in order and
    reports which one answered. `diagnostics` is included so a failure is
    debuggable rather than silent.
    """
    import requests

    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
    diag = []

    def num(x):
        try:
            return float(str(x).replace(",", "").replace("₹", "").strip())
        except (TypeError, ValueError):
            return None

    # ---- source 1: NSE official ----
    try:
        s = requests.Session()
        s.headers.update({
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.nseindia.com/reports/fii-dii",
        })
        warm = s.get("https://www.nseindia.com", timeout=8)
        r = s.get("https://www.nseindia.com/api/fiidiiTradeReact", timeout=8)
        diag.append(f"NSE: warmup {warm.status_code}, api {r.status_code}")
        r.raise_for_status()
        rows = []
        for d in r.json():
            cat = str(d.get("category", "")).upper()
            rows.append({
                "who": "FII" if ("FII" in cat or "FPI" in cat) else "DII",
                "date": d.get("date", ""),
                "buy": num(d.get("buyValue")),
                "sell": num(d.get("sellValue")),
                "net": num(d.get("netValue")),
            })
        if rows:
            return _clean({"source": "NSE", "flows": rows,
                           "diagnostics": diag})
        diag.append("NSE: 200 but empty payload")
    except Exception as e:
        diag.append(f"NSE: {type(e).__name__}: {str(e)[:120]}")

    # ---- source 2: StockEdge public API ----
    for url in (
        "https://api.stockedge.com/Api/FIIDailyDashboardApi/"
        "GetLatestFIIDIIActivities?lang=en",
        "https://api.stockedge.com/Api/DailyDashboardApi/"
        "GetLatestFIIDIIActivity?lang=en",
    ):
        try:
            r = requests.get(url, headers={"User-Agent": UA,
                                           "Accept": "application/json"},
                             timeout=10)
            diag.append(f"StockEdge {url.split('/')[-1][:26]}: {r.status_code}")
            r.raise_for_status()
            data = r.json()
            items = data if isinstance(data, list) else [data]
            rows = []
            for d in items:
                if not isinstance(d, dict):
                    continue
                blob = {str(k).lower(): v for k, v in d.items()}
                who_raw = str(blob.get("name") or blob.get("category")
                              or blob.get("clienttype") or "").upper()
                who = "FII" if ("FII" in who_raw or "FPI" in who_raw) else (
                      "DII" if "DII" in who_raw else None)
                if not who:
                    continue
                buy = num(blob.get("buyvalue") or blob.get("grosspurchase")
                          or blob.get("buy"))
                sell = num(blob.get("sellvalue") or blob.get("grosssales")
                           or blob.get("sell"))
                net = num(blob.get("netvalue") or blob.get("net"))
                if net is None and buy is not None and sell is not None:
                    net = buy - sell
                rows.append({
                    "who": who,
                    "date": str(blob.get("date") or blob.get("tradedate") or ""),
                    "buy": buy, "sell": sell, "net": net,
                })
            rows = [r_ for r_ in rows if r_["net"] is not None]
            if rows:
                # Keep the most recent row per participant.
                seen, unique = set(), []
                for r_ in rows:
                    if r_["who"] not in seen:
                        seen.add(r_["who"])
                        unique.append(r_)
                return _clean({"source": "StockEdge", "flows": unique,
                               "diagnostics": diag})
        except Exception as e:
            diag.append(f"StockEdge: {type(e).__name__}: {str(e)[:100]}")

    # ---- source 3: Moneycontrol HTML table ----
    try:
        r = requests.get(
            "https://www.moneycontrol.com/stocks/marketstats/fii_dii_activity/",
            headers={"User-Agent": UA}, timeout=12)
        diag.append(f"Moneycontrol: {r.status_code}")
        r.raise_for_status()
        import pandas as pd
        from io import StringIO
        tables = pd.read_html(StringIO(r.text))
        for t in tables:
            cols = [str(c).lower() for c in t.columns]
            if any("gross purchase" in c or "buy" in c for c in cols) and len(t) >= 2:
                rows = []
                for _, row in t.head(2).iterrows():
                    vals = [num(v) for v in row.tolist()[1:4]]
                    label = str(row.tolist()[0]).upper()
                    who = "FII" if ("FII" in label or "FPI" in label) else "DII"
                    if len(vals) >= 3:
                        rows.append({"who": who, "date": "",
                                     "buy": vals[0], "sell": vals[1],
                                     "net": vals[2]})
                if rows:
                    return _clean({"source": "Moneycontrol", "flows": rows,
                                   "diagnostics": diag})
        diag.append("Moneycontrol: no matching table")
    except Exception as e:
        diag.append(f"Moneycontrol: {type(e).__name__}: {str(e)[:100]}")

    raise HTTPException(
        503,
        "FII/DII data is unavailable right now — every upstream source "
        f"refused. Diagnostics: {' | '.join(diag)}")


# ===========================================================================
# SECTOR PERFORMANCE
# ===========================================================================
#
# NSE sector indices via Yahoo. Returns are computed from close prices rather
# than quoted, so every window uses the same basis and stays consistent.
SECTOR_INDICES = {
    "Nifty 50": "^NSEI",
    "Bank": "^NSEBANK",
    "IT": "^CNXIT",
    "Auto": "^CNXAUTO",
    "Pharma": "^CNXPHARMA",
    "FMCG": "^CNXFMCG",
    "Metal": "^CNXMETAL",
    "Realty": "^CNXREALTY",
    "Energy": "^CNXENERGY",
    "Infra": "^CNXINFRA",
    "PSU Bank": "^CNXPSUBANK",
    "Media": "^CNXMEDIA",
    "Financial Services": "NIFTY_FIN_SERVICE.NS",
    "Private Bank": "NIFTY_PVT_BANK.NS",
    "Consumer Durables": "^CNXCONSUM",
    "Commodities": "^CNXCMDT",
}

# Trading-day lookbacks. Approximate because markets close on weekends and
# holidays — we take the nearest available close at or before each offset.
_PERIOD_DAYS = {
    "1d": 1,
    "1m": 22,
    "1y": 252,
    "2y": 504,
    "3y": 756,
    "5y": 1260,
}


@app.get("/sectors")
def sector_performance(compare: str = "1y"):
    """
    Sector index levels with 1-day, 1-month and one longer-window return.

    [compare] is one of 1y, 2y, 3y, 5y. Sectors are ranked by 1-month return,
    strongest first, so the leaders sit at the top.
    """
    from concurrent.futures import ThreadPoolExecutor

    compare = compare.lower()
    if compare not in {"1y", "2y", "3y", "5y"}:
        raise HTTPException(
            400, "compare must be one of: 1y, 2y, 3y, 5y")

    try:
        import yfinance as yf
    except ImportError:
        raise HTTPException(503, "yfinance is not installed on the server")

    def pull(item):
        name, ticker = item
        try:
            # 6y of daily closes covers the longest window with room to spare.
            hist = yf.Ticker(ticker).history(period="6y")["Close"].dropna()
            if hist.empty or len(hist) < 2:
                return None

            closes = hist.tolist()
            latest = float(closes[-1])

            def ret(days: int):
                """Return over the last `days` sessions, or None if too short."""
                if len(closes) <= days:
                    return None
                past = float(closes[-(days + 1)])
                if past <= 0:
                    return None
                return round((latest / past - 1) * 100, 2)

            return {
                "name": name,
                "ticker": ticker,
                "level": round(latest, 2),
                "return_1d": ret(_PERIOD_DAYS["1d"]),
                "return_1m": ret(_PERIOD_DAYS["1m"]),
                "return_compare": ret(_PERIOD_DAYS[compare]),
                "compare_period": compare.upper(),
                "sessions": len(closes),
            }
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = [s for s in pool.map(pull, SECTOR_INDICES.items()) if s]

    if not results:
        raise HTTPException(
            502, "Could not fetch any sector data right now")

    # Rank by 1-month return; sectors missing it sink to the bottom.
    results.sort(key=lambda s: (s["return_1m"] is None,
                                -(s["return_1m"] or 0)))

    gainers = [s for s in results if (s["return_1m"] or 0) > 0]
    losers = [s for s in results if (s["return_1m"] or 0) < 0]

    return _clean({
        "count": len(results),
        "compare_period": compare.upper(),
        "available_periods": ["1Y", "2Y", "3Y", "5Y"],
        "summary": {
            "advancing": len(gainers),
            "declining": len(losers),
            "best": results[0]["name"] if results else None,
            "worst": results[-1]["name"] if results else None,
        },
        "sectors": results,
    })


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


# ===========================================================================
# FII / DII HISTORY  (30-day daily flows)
# ===========================================================================
_HIST_COLS = ["date", "fii_buy", "fii_sell", "fii_net",
              "dii_buy", "dii_sell", "dii_net"]


def _hist_load_cache() -> list:
    """Stored history from Supabase. Survives redeploys; empty if unconfigured."""
    sb = _supabase()
    if sb is None:
        return []
    try:
        res = (sb.table("fii_dii_history").select("*")
                 .order("date", desc=True).limit(120).execute())
        return res.data or []
    except Exception:
        return []


def _hist_save_cache(rows: list) -> None:
    """Upsert merged history so tomorrow's request starts from today's data."""
    sb = _supabase()
    if sb is None or not rows:
        return
    try:
        payload = []
        for r in rows[:120]:
            payload.append({k: r.get(k) for k in _HIST_COLS})
        sb.table("fii_dii_history").upsert(
            payload, on_conflict="date").execute()
    except Exception:
        pass


def _hist_scrape_moneycontrol() -> list:
    """
    Moneycontrol's FII/DII activity table — current month plus the previous one.

    Returns [] rather than raising: this is one of several sources, and a
    blocked scrape shouldn't take the endpoint down.
    """
    import io as _io
    from datetime import date, timedelta

    try:
        import pandas as pd
        import requests
    except ImportError:
        return []

    base = ("https://www.moneycontrol.com/stocks/marketstats/"
            "fii_dii_activity/index.php")
    prev = date.today().replace(day=1) - timedelta(days=1)
    urls = [base,
            f"{base}?mon_year={prev.strftime('%m-%Y')}",
            f"{base}?mon_year={prev.strftime('%b-%Y')}"]

    s = requests.Session()
    s.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/126.0 Safari/537.36"),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.moneycontrol.com/",
    })

    frames = []
    for url in urls:
        try:
            r = s.get(url, timeout=12)
            r.raise_for_status()
            for t in pd.read_html(_io.StringIO(r.text)):
                if t.shape[1] >= 7:
                    t = t.iloc[:, :7].copy()
                    t.columns = _HIST_COLS
                    frames.append(t)
                    break
        except Exception:
            continue

    if not frames:
        return []

    df = pd.concat(frames, ignore_index=True)
    df["date"] = pd.to_datetime(df["date"], errors="coerce", dayfirst=True)
    df = df.dropna(subset=["date"])
    for c in _HIST_COLS[1:]:
        df[c] = pd.to_numeric(
            df[c].astype(str).str.replace(",", ""), errors="coerce")
    df = df.dropna(subset=["fii_net", "dii_net"])

    out = []
    for _, row in df.iterrows():
        out.append({
            "date": row["date"].strftime("%Y-%m-%d"),
            **{c: (None if row[c] != row[c] else float(row[c]))
               for c in _HIST_COLS[1:]},
        })
    return out


def _hist_today_row() -> list:
    """Pivot today's live FII/DII snapshot into a single history row."""
    from datetime import datetime
    try:
        snap = fii_dii()
    except Exception:
        return []

    flows = snap.get("flows") or []
    fii = next((f for f in flows if str(f.get("who")).upper() == "FII"), None)
    dii = next((f for f in flows if str(f.get("who")).upper() == "DII"), None)
    if not fii or not dii:
        return []

    raw = str(fii.get("date") or "").strip()
    stamp = None
    for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d", "%d %b %Y"):
        try:
            stamp = datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
            break
        except ValueError:
            continue
    if stamp is None:
        stamp = datetime.now().strftime("%Y-%m-%d")

    return [{
        "date": stamp,
        "fii_buy": fii.get("buy"), "fii_sell": fii.get("sell"),
        "fii_net": fii.get("net"),
        "dii_buy": dii.get("buy"), "dii_sell": dii.get("sell"),
        "dii_net": dii.get("net"),
    }]


@app.get("/fii-dii/history")
def fii_dii_history(days: int = 30):
    """
    Daily FII/DII net flows for the last [days] trading sessions.

    Merges three sources — stored history, a Moneycontrol scrape, and today's
    live snapshot — then persists the result. That way history keeps building
    day by day even when the scrape source blocks this server, which it often
    does from a datacenter IP.
    """
    cached = _hist_load_cache()
    scraped = _hist_scrape_moneycontrol()
    today = _hist_today_row()

    merged: dict[str, dict] = {}
    for source in (cached, scraped, today):   # later sources win
        for row in source:
            stamp = str(row.get("date", ""))[:10]
            if not stamp:
                continue
            clean = {"date": stamp}
            for c in _HIST_COLS[1:]:
                try:
                    v = row.get(c)
                    clean[c] = None if v is None else float(v)
                except (TypeError, ValueError):
                    clean[c] = None
            if clean.get("fii_net") is None and clean.get("dii_net") is None:
                continue
            merged[stamp] = clean

    rows = sorted(merged.values(), key=lambda r: r["date"], reverse=True)
    if rows:
        _hist_save_cache(rows)

    window = rows[:days]
    fii_total = sum(r["fii_net"] or 0 for r in window)
    dii_total = sum(r["dii_net"] or 0 for r in window)
    fii_buy_days = sum(1 for r in window if (r["fii_net"] or 0) > 0)

    sources = []
    if cached:
        sources.append("cache")
    if scraped:
        sources.append("moneycontrol")
    if today:
        sources.append("live")

    return _clean({
        "count": len(window),
        "requested_days": days,
        "sources": sources,
        "summary": {
            "fii_net_total": round(fii_total, 2),
            "dii_net_total": round(dii_total, 2),
            "combined_net": round(fii_total + dii_total, 2),
            "fii_buying_days": fii_buy_days,
            "total_days": len(window),
        },
        # Oldest first, so a chart can plot straight through.
        "history": list(reversed(window)),
    })
