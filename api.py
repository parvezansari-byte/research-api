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


# ===========================================================================
# STOCK UNIVERSE  (multi-source, never hard-fails)
# ===========================================================================
#
# NSE blocks most datacenter IPs, so a single fetch from nseindia.com fails
# from Render even though it works locally. This tries several hosts in turn
# and falls back to a static list, so the search box is always populated even
# when every network source refuses.

_UNIVERSE_SOURCES = {
    "LARGECAP": [
        "https://www.niftyindices.com/IndexConstituent/ind_nifty100list.csv",
        "https://archives.nseindia.com/content/indices/ind_nifty100list.csv",
        "https://nsearchives.nseindia.com/content/indices/ind_nifty100list.csv",
    ],
    "MIDCAP": [
        "https://www.niftyindices.com/IndexConstituent/ind_niftymidcap150list.csv",
        "https://archives.nseindia.com/content/indices/ind_niftymidcap150list.csv",
        "https://nsearchives.nseindia.com/content/indices/ind_niftymidcap150list.csv",
    ],
    "SMALLCAP": [
        "https://www.niftyindices.com/IndexConstituent/ind_niftysmallcap250list.csv",
        "https://archives.nseindia.com/content/indices/ind_niftysmallcap250list.csv",
        "https://nsearchives.nseindia.com/content/indices/ind_niftysmallcap250list.csv",
    ],
}

# Static safety nets. Not the full index — enough that search stays useful
# when every remote source is blocked.
_NIFTY50_FALLBACK = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "HINDUNILVR", "ITC",
    "SBIN", "BHARTIARTL", "KOTAKBANK", "LT", "AXISBANK", "ASIANPAINT",
    "MARUTI", "TITAN", "SUNPHARMA", "ULTRACEMCO", "BAJFINANCE", "NESTLEIND",
    "WIPRO", "HCLTECH", "ONGC", "NTPC", "POWERGRID", "M&M", "TATAMOTORS",
    "TATASTEEL", "JSWSTEEL", "ADANIENT", "ADANIPORTS", "COALINDIA", "GRASIM",
    "HINDALCO", "DRREDDY", "CIPLA", "DIVISLAB", "BRITANNIA", "EICHERMOT",
    "HEROMOTOCO", "BAJAJ-AUTO", "BAJAJFINSV", "INDUSINDBK", "TECHM", "SHREECEM",
    "UPL", "APOLLOHOSP", "TATACONSUM", "SBILIFE", "HDFCLIFE", "BPCL",
]

_MIDCAP_FALLBACK = [
    "ASHOKLEY", "AUBANK", "AUROPHARMA", "BALKRISIND", "BANDHANBNK", "BEL",
    "BHARATFORG", "BHEL", "BIOCON", "CANBK", "CHOLAFIN", "COFORGE",
    "COLPAL", "CONCOR", "CUMMINSIND", "DALBHARAT", "DIXON", "ESCORTS",
    "FEDERALBNK", "GMRINFRA", "GODREJPROP", "GUJGASLTD", "HAL", "HAVELLS",
    "IDFCFIRSTB", "INDHOTEL", "INDIGO", "IRCTC", "JINDALSTEL", "JUBLFOOD",
    "LICHSGFIN", "LTIM", "LUPIN", "MFSL", "MPHASIS", "MRF", "MUTHOOTFIN",
    "NMDC", "OBEROIRLTY", "OFSS", "PAGEIND", "PERSISTENT", "PETRONET",
    "PFC", "PIIND", "PNB", "POLYCAB", "RECLTD", "SAIL", "SRF", "SUNTV",
    "SUPREMEIND", "SYNGENE", "TATACHEM", "TATACOMM", "TATAELXSI", "TATAPOWER",
    "TORNTPHARM", "TRENT", "TVSMOTOR", "UBL", "VOLTAS", "ZEEL", "ZYDUSLIFE",
    "ABCAPITAL", "ALKEM", "APLAPOLLO", "ASTRAL", "BATAINDIA", "CROMPTON",
    "DEEPAKNTR", "GLAND", "GLENMARK", "GODREJCP", "HINDPETRO", "IDEA",
    "IPCALAB", "L&TFH", "M&MFIN", "MANAPPURAM", "MAXHEALTH", "METROPOLIS",
    "NAM-INDIA", "NAVINFLUOR", "OIL", "PEL", "PHOENIXLTD", "RAMCOCEM",
    "SHRIRAMFIN", "SONACOMS", "STARHEALTH", "THERMAX", "UNIONBANK",
]

_SMALLCAP_FALLBACK = [
    "AARTIIND", "AAVAS", "ABFRL", "ACE", "AEGISCHEM", "AFFLE", "AJANTPHARM",
    "ALLCARGO", "AMARAJABAT", "ANGELONE", "APARINDS", "APTUS", "ASAHIINDIA",
    "ASTERDM", "ATUL", "BALAMINES", "BALRAMCHIN", "BASF", "BIRLACORPN",
    "BLUEDART", "BLUESTARCO", "BSOFT", "CAMS", "CANFINHOME", "CAPLIPOINT",
    "CARBORUNIV", "CASTROLIND", "CDSL", "CEATLTD", "CENTRALBK", "CENTURYPLY",
    "CERA", "CHAMBLFERT", "CHOLAHLDNG", "CIEINDIA", "CLEAN", "CSBBANK",
    "CYIENT", "DATAPATTNS", "DCMSHRIRAM", "DELTACORP", "DEVYANI", "DHANI",
    "EIDPARRY", "EIHOTEL", "ELGIEQUIP", "EMAMILTD", "ENDURANCE", "ENGINERSIN",
    "EQUITASBNK", "EXIDEIND", "FDC", "FINCABLES", "FINEORG", "FINPIPE",
    "FORTIS", "FSL", "GALAXYSURF", "GESHIP", "GILLETTE", "GNFC", "GODFRYPHLP",
    "GRANULES", "GRAPHITE", "GRINDWELL", "GSFC", "GSPL", "HAPPSTMNDS",
    "HATSUN", "HEG", "HFCL", "HINDCOPPER", "HOMEFIRST", "HUDCO", "IBULHSGFIN",
    "IEX", "IIFL", "INDIACEM", "INDIAMART", "INTELLECT", "IOB", "IRB",
    "IRCON", "ITI", "JBCHEPHARM", "JKCEMENT", "JKLAKSHMI", "JKPAPER",
    "JMFINANCIL", "JSWENERGY", "JUBLINGREA", "JUSTDIAL", "JYOTHYLAB",
    "KAJARIACER", "KALYANKJIL", "KANSAINER", "KARURVYSYA", "KEC", "KEI",
    "KIRLOSENG", "KNRCON", "KPITTECH", "KRBL", "LATENTVIEW", "LAURUSLABS",
    "LEMONTREE", "LINDEINDIA", "LXCHEM", "MAHABANK", "MAHLIFE", "MAPMYINDIA",
    "MASTEK", "MEDPLUS", "MGL", "MIDHANI", "MINDACORP", "MOTILALOFS",
    "MRPL", "NATCOPHARM", "NATIONALUM", "NBCC", "NCC", "NESCO", "NETWORK18",
    "NH", "NIACL", "NLCINDIA", "NUVOCO", "OLECTRA", "ORIENTELEC", "PGHH",
    "PNBHOUSING", "POLYMED", "POONAWALLA", "PRAJIND", "PRESTIGE", "PRINCEPIPE",
    "PRSMJOHNSN", "PVRINOX", "QUESS", "RADICO", "RAILTEL", "RAIN", "RAJESHEXPO",
    "RALLIS", "RATNAMANI", "RBLBANK", "RCF", "REDINGTON", "RELAXO", "RENUKA",
    "RITES", "ROUTE", "RVNL", "SANOFI", "SAPPHIRE", "SCHAEFFLER", "SFL",
    "SHARDACROP", "SHOPERSTOP", "SHYAMMETL", "SIS", "SJVN", "SOBHA",
    "SOLARINDS", "SONATSOFTW", "SPARC", "STLTECH", "SUMICHEM", "SUNCLAYLTD",
    "SUNDARMFIN", "SUNDRMFAST", "SUPRAJIT", "SUVENPHAR", "SWANENERGY",
    "SYMPHONY", "TANLA", "TATAINVEST", "TCIEXP", "TEAMLEASE", "TEJASNET",
    "TIINDIA", "TIMKEN", "TRIDENT", "TRITURBINE", "TTKPRESTIG", "TV18BRDCST",
    "UJJIVANSFB", "UTIAMC", "VAIBHAVGBL", "VAKRANGEE", "VARROC", "VBL",
    "VGUARD", "VINATIORGA", "VIPIND", "VTL", "WELCORP", "WELSPUNIND",
    "WESTLIFE", "WHIRLPOOL", "ZENSARTECH", "ZFCVINDIA", "ZYDUSWELL",
]

_STATIC_FALLBACKS = {
    "LARGECAP": _NIFTY50_FALLBACK,
    "MIDCAP": _MIDCAP_FALLBACK,
    "SMALLCAP": _SMALLCAP_FALLBACK,
}

# Rough floors — a truncated or error page shouldn't pass as a real list.
_MIN_EXPECTED = {"LARGECAP": 80, "MIDCAP": 100, "SMALLCAP": 150}

# Populated on first success; avoids re-downloading on every request.
_universe_cache: dict[str, list[str]] = {}


def _fetch_constituents(url: str) -> list[str]:
    """Pull a symbol column out of an NSE-style constituents CSV."""
    import csv
    import io
    import requests

    r = requests.get(
        url,
        headers={
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/126.0 Safari/537.36"),
            "Accept": "text/csv,application/csv,text/plain,*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.niftyindices.com/",
        },
        timeout=15,
    )
    r.raise_for_status()

    text = r.text
    if "<html" in text[:400].lower():
        raise ValueError("got an HTML page, not a CSV")

    reader = csv.DictReader(io.StringIO(text))
    field = None
    for candidate in ("Symbol", "SYMBOL", "symbol"):
        if reader.fieldnames and candidate in reader.fieldnames:
            field = candidate
            break
    if field is None:
        raise ValueError(f"no Symbol column in {reader.fieldnames}")

    out = []
    for row in reader:
        sym = (row.get(field) or "").strip().upper()
        if sym:
            out.append(sym)
    return out


def _universe_symbols(universe: str) -> tuple[list[str], str]:
    """
    Symbols for one universe, plus where they came from.

    Order of preference: in-process cache, then each remote source in turn,
    then the static list. Returning the origin makes it visible in the API
    response whether the data is live or a fallback.
    """
    universe = universe.upper()

    if universe in _universe_cache:
        return _universe_cache[universe], "cache"

    minimum = _MIN_EXPECTED.get(universe, 40)
    for url in _UNIVERSE_SOURCES.get(universe, []):
        try:
            symbols = _fetch_constituents(url)
            if len(symbols) >= minimum:
                _universe_cache[universe] = symbols
                host = url.split("/")[2]
                return symbols, host
        except Exception:
            continue

    # Last resort: the analysis module's own logic, in case it has a
    # locally cached copy from a previous successful run.
    try:
        from analysis_api import get_universe
        symbols = [s.replace(".NS", "") for s in get_universe(universe)]
        if len(symbols) >= minimum:
            _universe_cache[universe] = symbols
            return symbols, "analysis_api"
    except Exception:
        pass

    return list(_STATIC_FALLBACKS.get(universe, [])), "static"


@app.get("/debug/universe")
def debug_universe():
    """Where each universe's symbols are coming from, and how many."""
    out = {}
    for uni in ("LARGECAP", "MIDCAP", "SMALLCAP"):
        symbols, origin = _universe_symbols(uni)
        out[uni] = {
            "count": len(symbols),
            "source": origin,
            "sample": symbols[:5],
        }
    return out


@app.get("/stocks/list")
def stock_list():
    """
    The full searchable universe (~500 names): NIFTY 100 + Midcap 150 +
    Smallcap 250. Just the symbols — full data loads when a stock is opened.
    Cached implicitly by clients; the list changes rarely.
    """
    names: list[str] = []
    seen = set()
    sources: list[str] = []

    for uni in ("LARGECAP", "MIDCAP", "SMALLCAP"):
        symbols, origin = _universe_symbols(uni)
        if symbols:
            sources.append(f"{uni}:{origin}({len(symbols)})")
        for sym in symbols:
            s = sym.replace(".NS", "").strip().upper()
            if s and s not in seen:
                seen.add(s)
                names.append(s)

    # Should never happen — _universe_symbols always has a static fallback —
    # but a search box with zero options is worse than a short list.
    if not names:
        names = list(_NIFTY50_FALLBACK)
        sources.append("emergency")

    return {"count": len(names), "symbols": sorted(names),
            "sources": sources}


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
        # Yahoo limits per IP, and every user of this server shares one, so
        # throttling is routine rather than exceptional. Say so plainly.
        if _yf_rate_limited(e):
            # Fundamentals need Yahoo, but the price doesn't — Dhan can still
            # answer. A partial response beats an error screen.
            try:
                quote = live_quote(symbol)
                return _clean({
                    "symbol": sym,
                    "partial": True,
                    "notice": "Yahoo is rate-limiting this server, so "
                              "fundamentals and technicals are unavailable "
                              "right now. The price below is live from Dhan.",
                    "fundamentals": {"current_price": quote.get("price")},
                    "technicals": {},
                })
            except Exception:
                pass
            raise HTTPException(
                503,
                "Yahoo is rate-limiting this server right now. Try again in "
                "a minute.")
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
            hist = _yf_history(ticker, "6y")["Close"].dropna()
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
# LIVE CHART
# ===========================================================================
#
# Intraday and historical price series for charting. Separate from
# /stock/{symbol}/history, which is daily-only and carries indicators — this
# one is optimised for drawing a line and knowing what changed.

# period/interval pairs Yahoo accepts for each timeframe.
CHART_TIMEFRAMES = {
    "1D":  {"period": "1d",  "interval": "1m",  "live": True},
    "1W":  {"period": "5d",  "interval": "15m", "live": False},
    "1M":  {"period": "1mo", "interval": "60m", "live": False},
    "6M":  {"period": "6mo", "interval": "1d",  "live": False},
    "1Y":  {"period": "1y",  "interval": "1d",  "live": False},
    "5Y":  {"period": "5y",  "interval": "1wk", "live": False},
    "ALL": {"period": "max", "interval": "1mo", "live": False},
}

# Handy names the app shows as chips. Indices keep their ^ prefix.
CHART_QUICK_PICKS = {
    "NIFTY 50": "^NSEI",
    "BANK NIFTY": "^NSEBANK",
    "SENSEX": "^BSESN",
    "RELIANCE": "RELIANCE.NS",
    "HDFC BANK": "HDFCBANK.NS",
    "TCS": "TCS.NS",
    "INFOSYS": "INFY.NS",
    "ICICI BANK": "ICICIBANK.NS",
}


def _normalise_chart_symbol(symbol: str) -> str:
    """Accept 'RELIANCE', 'RELIANCE.NS' or '^NSEI' and return a Yahoo ticker."""
    s = symbol.strip().upper()
    if s.startswith("^"):
        return s
    if s.endswith((".NS", ".BO")):
        return s
    return f"{s}.NS"


@app.get("/chart/quick-picks")
def chart_quick_picks():
    """Preset symbols for the chart's chip row."""
    return {"picks": [{"label": k, "symbol": v}
                      for k, v in CHART_QUICK_PICKS.items()],
            "timeframes": list(CHART_TIMEFRAMES.keys())}


@app.get("/chart/{symbol}")
def chart_data(symbol: str, timeframe: str = "1D"):
    """
    Price series for one symbol over one timeframe.

    On the 1D view the baseline is the previous close, so the change shown
    matches what every broker quotes. On longer views the baseline is the
    first point in the window, so the change describes that window.
    """
    from datetime import datetime, timezone, timedelta

    tf = timeframe.upper()
    if tf not in CHART_TIMEFRAMES:
        raise HTTPException(
            400, f"timeframe must be one of: {', '.join(CHART_TIMEFRAMES)}")

    cfg = CHART_TIMEFRAMES[tf]
    sym = _normalise_chart_symbol(symbol)

    try:
        import yfinance as yf
    except ImportError:
        raise HTTPException(503, "yfinance is not installed on the server")

    try:
        ticker = yf.Ticker(sym)
        used_interval = cfg["interval"]
        df = _price_history(sym, cfg["period"], used_interval)

        # 1-minute NSE data is patchy; 5-minute is a reliable stand-in.
        if df.empty and cfg["interval"] == "1m":
            used_interval = "5m"
            df = _yf_history(sym, cfg["period"], used_interval)

        # A fresh session before the open returns nothing — show the last day.
        if df.empty and tf == "1D":
            used_interval = "15m"
            df = _yf_history(sym, "5d", used_interval)
    except Exception as e:
        if _yf_rate_limited(e):
            raise HTTPException(
                503, "Yahoo is rate-limiting this server. Try again shortly.")
        raise HTTPException(502, f"Chart data error: {e}")

    if df.empty or "Close" not in df.columns:
        raise HTTPException(
            404,
            f"No chart data for '{symbol}'. NSE stocks need no suffix "
            "(RELIANCE), indices start with ^ (^NSEI).")

    closes = df["Close"].dropna()
    if closes.empty:
        raise HTTPException(404, f"No usable prices for '{symbol}'")

    # Previous close matters only on the intraday view.
    prev_close = None
    if cfg["live"]:
        try:
            prev_close = float(ticker.fast_info["previous_close"])
        except Exception:
            try:
                daily = ticker.history(period="5d")["Close"].dropna()
                if len(daily) >= 2:
                    prev_close = float(daily.iloc[-2])
            except Exception:
                pass

    # Timestamps to IST so the app doesn't have to guess the market's clock.
    ist = timezone(timedelta(hours=5, minutes=30))
    points = []
    for stamp, value in closes.items():
        try:
            when = stamp.to_pydatetime()
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            when = when.astimezone(ist)
        except Exception:
            continue
        points.append({
            "t": when.isoformat(),
            "label": when.strftime("%H:%M" if cfg["live"] else "%d %b %y"),
            "close": round(float(value), 2),
        })

    if not points:
        raise HTTPException(404, f"No usable prices for '{symbol}'")

    last = points[-1]["close"]
    first = points[0]["close"]
    baseline = prev_close if (cfg["live"] and prev_close) else first
    change = last - baseline
    change_pct = (change / baseline * 100) if baseline else 0.0

    values = [p["close"] for p in points]
    high, low = max(values), min(values)

    # Meta name is nicer than the raw ticker where Yahoo provides it.
    display = sym.replace(".NS", "").replace(".BO", "")
    try:
        info_name = ticker.fast_info.get("shortName")
        if info_name:
            display = str(info_name)
    except Exception:
        pass

    return _clean({
        "symbol": sym,
        "display": display,
        "timeframe": tf,
        "interval": used_interval,
        "live": cfg["live"],
        "last": round(last, 2),
        "baseline": round(baseline, 2) if baseline else None,
        "baseline_label": "prev close" if (cfg["live"] and prev_close)
                          else f"start of {tf}",
        "change": round(change, 2),
        "change_pct": round(change_pct, 2),
        "high": round(high, 2),
        "low": round(low, 2),
        "range_pct": round((high - low) / low * 100, 2) if low else None,
        "points": len(points),
        "series": points,
        "updated": datetime.now(ist).strftime("%H:%M:%S"),
    })


@app.get("/chart")
def chart_data_query(symbol: str, timeframe: str = "1D"):
    """
    Same as /chart/{symbol}, but the symbol arrives as a query parameter.

    Index tickers start with '^', which some clients mangle when it sits in a
    path segment. Passing it as a query value sidesteps that entirely.
    """
    return chart_data(symbol, timeframe)


# ===========================================================================
# OPTION CHAIN  (Dhan Data API)
# ===========================================================================
#
# Talks to Dhan's REST endpoints directly rather than through the SDK, which
# has had breaking changes between versions. Needs an active Data APIs
# subscription on the Dhan account — a plain trading login returns 403.

OPTION_INDICES = {
    "NIFTY 50":     {"security_id": 13,  "segment": "IDX_I", "step": 50},
    "BANK NIFTY":   {"security_id": 25,  "segment": "IDX_I", "step": 100},
    "FIN NIFTY":    {"security_id": 27,  "segment": "IDX_I", "step": 50},
    "MIDCAP NIFTY": {"security_id": 442, "segment": "IDX_I", "step": 25},
}


def _dhan_option_headers():
    """Auth headers for Dhan's Data API, or None when unconfigured."""
    import os
    token = (os.environ.get("DHAN_ACCESS_TOKEN")
             or os.environ.get("DHANHQ_ACCESS_TOKEN"))
    client = (os.environ.get("DHAN_CLIENT_ID")
              or os.environ.get("DHANHQ_CLIENT_ID"))
    if not token:
        return None
    headers = {
        "access-token": token,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if client:
        headers["client-id"] = client
    return headers


def _dhan_error(status: int, body: str) -> str:
    """Turn Dhan's HTTP codes into something a user can act on."""
    if status == 401:
        return ("Dhan token is invalid or expired. Generate a fresh one at "
                "web.dhan.co and update DHAN_ACCESS_TOKEN.")
    if status == 403:
        return ("Dhan denied access to the Data API. Check that the Data APIs "
                "subscription is active on this account.")
    if status == 429:
        return "Dhan rate limit hit. Wait a few seconds and try again."
    return f"Dhan returned HTTP {status}: {body[:200]}"


@app.get("/options/indices")
def option_indices():
    """Indices that can be charted, with their strike steps."""
    return {"indices": [{"name": k, **v} for k, v in OPTION_INDICES.items()]}


@app.get("/options/expiries")
def option_expiries(index: str = "NIFTY 50"):
    """Available expiry dates for one index, nearest first."""
    import requests

    cfg = OPTION_INDICES.get(index.upper()) or OPTION_INDICES.get(index)
    if cfg is None:
        raise HTTPException(
            404, f"Unknown index '{index}'. "
                 f"Options: {', '.join(OPTION_INDICES)}")

    headers = _dhan_option_headers()
    if headers is None:
        raise HTTPException(
            503, "DHAN_ACCESS_TOKEN is not set on the server")

    try:
        r = requests.post(
            "https://api.dhan.co/v2/optionchain/expirylist",
            headers=headers,
            json={"UnderlyingScrip": cfg["security_id"],
                  "UnderlyingSeg": cfg["segment"]},
            timeout=12)
    except Exception as e:
        raise HTTPException(502, f"Network error reaching Dhan: {e}")

    if r.status_code != 200:
        raise HTTPException(502, _dhan_error(r.status_code, r.text))

    expiries = r.json().get("data") or []
    if not expiries:
        raise HTTPException(404, f"Dhan returned no expiries for {index}")

    return _clean({"index": index, "count": len(expiries),
                   "expiries": expiries})


@app.get("/options/chain")
def option_chain(index: str = "NIFTY 50", expiry: str = "",
                 strikes: int = 10):
    """
    Full option chain for one index and expiry, plus the derived metrics.

    [strikes] limits the returned rows to that many either side of ATM, which
    is what actually matters — the far wings carry almost no open interest.
    """
    import requests

    cfg = OPTION_INDICES.get(index.upper()) or OPTION_INDICES.get(index)
    if cfg is None:
        raise HTTPException(
            404, f"Unknown index '{index}'. "
                 f"Options: {', '.join(OPTION_INDICES)}")

    headers = _dhan_option_headers()
    if headers is None:
        raise HTTPException(
            503, "DHAN_ACCESS_TOKEN is not set on the server")

    # Default to the nearest expiry when none is given.
    if not expiry:
        try:
            found = option_expiries(index)
            expiry = (found.get("expiries") or [""])[0]
        except HTTPException:
            raise
        if not expiry:
            raise HTTPException(404, "Could not determine an expiry")

    try:
        r = requests.post(
            "https://api.dhan.co/v2/optionchain",
            headers=headers,
            json={"UnderlyingScrip": cfg["security_id"],
                  "UnderlyingSeg": cfg["segment"],
                  "Expiry": expiry},
            timeout=15)
    except Exception as e:
        raise HTTPException(502, f"Network error reaching Dhan: {e}")

    if r.status_code != 200:
        raise HTTPException(502, _dhan_error(r.status_code, r.text))

    data = r.json().get("data") or {}
    oc = data.get("oc") or {}
    spot = float(data.get("last_price") or 0)

    rows = []
    for strike_str, legs in oc.items():
        try:
            strike = float(strike_str)
        except (TypeError, ValueError):
            continue
        ce = legs.get("ce") or {}
        pe = legs.get("pe") or {}
        rows.append({
            "strike": strike,
            "ce_oi": float(ce.get("oi") or 0),
            "ce_ltp": float(ce.get("last_price") or 0),
            "ce_iv": float(ce.get("implied_volatility") or 0),
            "pe_oi": float(pe.get("oi") or 0),
            "pe_ltp": float(pe.get("last_price") or 0),
            "pe_iv": float(pe.get("implied_volatility") or 0),
        })

    if not rows:
        raise HTTPException(
            404, f"Dhan returned an empty chain for {index} {expiry}")

    rows.sort(key=lambda x: x["strike"])

    # --- metrics over the whole chain ---
    total_ce_oi = sum(r_["ce_oi"] for r_ in rows)
    total_pe_oi = sum(r_["pe_oi"] for r_ in rows)
    pcr = (total_pe_oi / total_ce_oi) if total_ce_oi > 0 else 0.0

    # Max pain: the strike where option writers pay out least in total.
    max_pain, best_pain = None, None
    for candidate in (r_["strike"] for r_ in rows):
        pain = 0.0
        for r_ in rows:
            pain += r_["ce_oi"] * max(0.0, candidate - r_["strike"])
            pain += r_["pe_oi"] * max(0.0, r_["strike"] - candidate)
        if best_pain is None or pain < best_pain:
            best_pain, max_pain = pain, candidate

    atm = min((r_["strike"] for r_ in rows),
              key=lambda s: abs(s - spot)) if spot else None

    if pcr > 1.3:
        signal = "Strongly bullish — heavy put writing"
    elif pcr > 1.0:
        signal = "Bullish"
    elif pcr > 0.7:
        signal = "Neutral to mildly bearish"
    else:
        signal = "Bearish — heavy call writing"

    # --- window around ATM ---
    window = rows
    if atm is not None and strikes > 0:
        span = strikes * cfg["step"]
        window = [r_ for r_ in rows
                  if atm - span <= r_["strike"] <= atm + span]
        if not window:
            window = rows

    highest_pe = max(window, key=lambda r_: r_["pe_oi"], default=None)
    highest_ce = max(window, key=lambda r_: r_["ce_oi"], default=None)

    return _clean({
        "index": index,
        "expiry": expiry,
        "spot": round(spot, 2),
        "atm": atm,
        "step": cfg["step"],
        "pcr": round(pcr, 2),
        "signal": signal,
        "total_ce_oi": total_ce_oi,
        "total_pe_oi": total_pe_oi,
        "max_pain": max_pain,
        "support": highest_pe["strike"] if highest_pe else None,
        "resistance": highest_ce["strike"] if highest_ce else None,
        "count": len(window),
        "rows": window,
    })


# ===========================================================================
# YAHOO RATE-LIMIT HANDLING
# ===========================================================================
#
# Every request from this server shares one IP, so Yahoo's per-IP limit is
# reached far sooner than it would be for a single user. Two defences:
#
#   1. Cache responses, so repeat views cost nothing upstream.
#   2. Retry with backoff, then serve stale cache rather than failing — a
#      price a few minutes old beats an error page.

import threading as _yf_threading

_YF_LOCK = _yf_threading.Lock()
_yf_cache: dict[str, tuple[float, object]] = {}   # key -> (stored_at, value)

# History moves slowly; quotes need to be fresher.
_YF_TTL = {
    "history": 900,      # 15 min
    "quote": 120,        # 2 min
    "info": 3600,        # 1 hr — fundamentals barely change intraday
}


def _yf_cached(key: str, kind: str, producer, allow_stale: bool = True):
    """
    Run [producer] at most once per TTL for [key].

    On failure, a stale cached value is returned when one exists — being a
    little out of date is almost always better than showing nothing.
    """
    import time

    ttl = _YF_TTL.get(kind, 300)
    now = time.time()

    with _YF_LOCK:
        hit = _yf_cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]

    try:
        value = producer()
        with _YF_LOCK:
            _yf_cache[key] = (now, value)
            # Keep the cache from growing without bound on a long-lived process.
            if len(_yf_cache) > 500:
                oldest = sorted(_yf_cache.items(), key=lambda kv: kv[1][0])
                for k, _ in oldest[:100]:
                    _yf_cache.pop(k, None)
        return value
    except Exception:
        if allow_stale and hit:
            return hit[1]
        raise


def _yf_history(symbol: str, period: str = "1y", interval: str = "1d"):
    """
    Price history with caching and backoff.

    Yahoo answers a rate-limited request with an exception rather than a
    status code, so any failure is retried briefly before giving up.
    """
    import time

    def fetch():
        import yfinance as yf
        last_error = None
        for attempt in range(3):
            try:
                df = yf.Ticker(symbol).history(period=period,
                                               interval=interval)
                if not df.empty:
                    return df
                last_error = ValueError("empty response")
            except Exception as e:
                last_error = e
            # 0.5s, then 1.5s — enough to clear a short burst limit without
            # holding the request open for long.
            if attempt < 2:
                time.sleep(0.5 + attempt)
        raise last_error or ValueError("no data")

    return _yf_cached(f"hist:{symbol}:{period}:{interval}", "history", fetch)


def _yf_rate_limited(error: Exception) -> bool:
    """Whether an exception looks like Yahoo throttling rather than a bad symbol."""
    text = str(error).lower()
    return any(s in text for s in
               ("too many requests", "rate limit", "429", "try after"))


# ===========================================================================
# PRICE SOURCE  (Dhan first, Yahoo as fallback)
# ===========================================================================
#
# Yahoo limits by IP and every user of this server shares one, so it is the
# wrong primary source for anything called often. Dhan is a paid API keyed to
# this account, has no such shared-IP ceiling, and already powers live quotes
# here — so price history now goes to Dhan first.
#
# Yahoo remains the fallback, and stays the only source for fundamentals:
# Dhan serves prices, not P/E or ROE.

def _dhan_history(symbol: str, days: int = 400):
    """
    Daily OHLC from Dhan as a DataFrame indexed by date.

    Returns None rather than raising when Dhan can't serve this symbol, so
    callers can fall through to Yahoo without special-casing every failure.
    """
    from datetime import datetime, timedelta

    sym = symbol.strip().upper().replace(".NS", "").replace(".BO", "")
    if sym.startswith("^"):
        return None          # indices aren't equities in the scrip master

    try:
        from dhanhq_api import get_dhan_client
        client = get_dhan_client()
        if client is None:
            return None

        sec_id = client.get_security_id(sym)
        if not sec_id:
            return None

        to_date = datetime.now()
        from_date = to_date - timedelta(days=days)
        df = client.get_historical_daily(
            sec_id,
            from_date.strftime("%Y-%m-%d"),
            to_date.strftime("%Y-%m-%d"),
        )
        if df is None or df.empty:
            return None

        # Normalise column names to match what the Yahoo path returns, so
        # downstream code doesn't need to know which source answered.
        rename = {}
        for col in df.columns:
            low = str(col).lower()
            if low in ("open", "high", "low", "close", "volume"):
                rename[col] = low.capitalize()
        df = df.rename(columns=rename)

        needed = {"Open", "High", "Low", "Close"}
        if not needed.issubset(df.columns):
            return None

        return df
    except Exception:
        return None


def _price_history(symbol: str, period: str = "1y", interval: str = "1d",
                   prefer_dhan: bool = True):
    """
    Daily price history from whichever source can serve it.

    Dhan is tried first for daily equity data. Intraday intervals and indices
    fall straight through to Yahoo, which handles both.
    """
    period_days = {
        "1d": 5, "5d": 10, "1mo": 45, "3mo": 120, "6mo": 220,
        "1y": 400, "2y": 760, "5y": 1850, "10y": 3700, "6y": 2200,
        "max": 3700,
    }

    is_daily = interval in ("1d", "1wk", "1mo")
    is_index = symbol.strip().startswith("^")

    if prefer_dhan and is_daily and not is_index:
        df = _dhan_history(symbol, period_days.get(period, 400))
        if df is not None and len(df) > 5:
            # Weekly and monthly bars are resampled locally; Dhan returns
            # daily candles only.
            if interval == "1wk":
                df = df.resample("W").agg({"Open": "first", "High": "max",
                                           "Low": "min", "Close": "last"}).dropna()
            elif interval == "1mo":
                df = df.resample("ME").agg({"Open": "first", "High": "max",
                                            "Low": "min", "Close": "last"}).dropna()
            return df

    return _yf_history(symbol, period, interval)


# ===========================================================================
# FUND DATABASE  (bundled snapshot + live NAV)
# ===========================================================================
#
# Metrics come from a monthly research snapshot shipped alongside this file:
# AUM, expense ratio, risk ratios, portfolio composition and manager — none of
# which AMFI's public NAV feed carries. NAV itself is refreshed daily from
# mfapi so prices don't go stale between snapshots.
#
# These are REGULAR plan figures. Direct plans of the same schemes carry a
# lower expense ratio and correspondingly higher returns, so the numbers here
# should not be compared against a Direct-plan quote.

import json as _json
import os as _os
import re as _re
import threading as _threading
from datetime import datetime as _datetime, timedelta as _timedelta

_FUNDS_FILE = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                            "funds_data.json")

_funds_cache: list | None = None
_nav_cache: dict[str, dict] = {}      # normalised key -> {nav, date, code}
_nav_refreshed_at: _datetime | None = None
_nav_lock = _threading.Lock()


def _load_funds() -> list:
    """The bundled snapshot, loaded once per process."""
    global _funds_cache
    if _funds_cache is None:
        try:
            with open(_FUNDS_FILE, "r", encoding="utf-8") as fh:
                _funds_cache = _json.load(fh)
        except Exception:
            _funds_cache = []
    return _funds_cache


def _norm_scheme_name(name: str) -> str:
    """
    Reduce a scheme name to a comparable key.

    The snapshot writes 'ICICI Pru Large Cap Fund(G)' where AMFI writes
    'ICICI Prudential Large Cap Fund - Growth', so plan suffixes are stripped
    and the snapshot's abbreviations expanded before comparing.
    """
    s = str(name)
    # Punctuation first, so plan words are left as whole words to remove.
    s = s.replace("&", " and ")
    s = _re.sub(r"[^A-Za-z0-9 ]", " ", s)
    s = _re.sub(r"\s+", " ", s).strip().lower()

    # Whole words only — a partial strip would turn "regular" into "ular".
    s = _re.sub(r"\b(regular|reg|direct|growth|gr|g|idcw|dividend|div|"
                r"payout|reinvestment|reinvest|option|opt|plan|scheme)\b",
                " ", s)
    s = _re.sub(r"\s+", " ", s).strip()

    for short, full in (
        ("pru", "prudential"), ("sl", "sun life"), ("rob", "robeco"),
        ("intl", "international"), ("opp", "opportunities"),
        ("oppo", "opportunities"), ("mfg", "manufacturing"),
        ("mgmt", "management"), ("bal", "balanced"),
        ("sec", "securities"), ("corp", "corporate"),
        ("govt", "government"), ("insti", "institutional"),
        ("ltd", ""), ("fund", ""), ("funds", ""),
    ):
        s = _re.sub(rf"\b{short}\b", full, s)

    # Spacing variants AMCs write inconsistently across sources.
    s = _re.sub(r"\bblue\s+chip\b", "bluechip", s)
    s = _re.sub(r"\bmid\s+cap\b", "midcap", s)
    s = _re.sub(r"\bsmall\s+cap\b", "smallcap", s)
    s = _re.sub(r"\blarge\s+cap\b", "largecap", s)
    s = _re.sub(r"\bflexi\s+cap\b", "flexicap", s)
    s = _re.sub(r"\bmulti\s+cap\b", "multicap", s)

    return _re.sub(r"\s+", " ", s).strip()


def _is_regular_growth(name: str) -> bool:
    """
    Keep Regular Growth plans only, to match the snapshot.

    AMFI lists Direct and Regular variants of the same scheme with nearly
    identical names; picking the wrong one would attach a Direct NAV to
    Regular-plan metrics.
    """
    low = name.lower()
    if "direct" in low:
        return False
    if any(w in low for w in ("idcw", "dividend", "payout", "reinvest",
                              "bonus", "quarterly", "monthly", "daily",
                              "weekly", "fortnightly")):
        return False
    return "growth" in low or "(g)" in low


def _refresh_navs(force: bool = False) -> dict:
    """
    Rebuild the name -> latest NAV map from AMFI's daily feed.

    AMFI publishes every scheme's NAV in one semicolon-delimited file, so this
    is a single request rather than one per fund. Refreshed at most every six
    hours; NAVs are only published once a day anyway.
    """
    global _nav_refreshed_at

    with _nav_lock:
        fresh = (_nav_refreshed_at is not None
                 and _datetime.now() - _nav_refreshed_at < _timedelta(hours=6))
        if fresh and not force and _nav_cache:
            return {"status": "cached", "schemes": len(_nav_cache)}

        import requests
        try:
            r = requests.get("https://www.amfiindia.com/spages/NAVAll.txt",
                             timeout=30,
                             headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
        except Exception as e:
            return {"status": "failed", "error": f"{type(e).__name__}: {e}",
                    "schemes": len(_nav_cache)}

        parsed = 0
        for line in r.text.splitlines():
            if line.count(";") < 5:
                continue
            parts = line.split(";")
            code, name, nav, date = parts[0], parts[3], parts[4], parts[5]
            if not code.strip().isdigit():
                continue
            if not _is_regular_growth(name):
                continue
            try:
                value = float(nav)
            except (TypeError, ValueError):
                continue
            if value <= 0:
                continue
            key = _norm_scheme_name(name)
            # First match wins; AMFI lists the primary plan first.
            if key not in _nav_cache or _nav_cache[key].get("stale"):
                _nav_cache[key] = {"nav": round(value, 4),
                                   "date": date.strip(),
                                   "code": code.strip()}
                parsed += 1

        _nav_refreshed_at = _datetime.now()
        return {"status": "refreshed", "schemes": len(_nav_cache),
                "parsed": parsed}


def _with_live_nav(fund: dict) -> dict:
    """Overlay today's NAV where AMFI has a match for this scheme."""
    out = dict(fund)
    live = _nav_cache.get(fund.get("key", ""))
    if live:
        snapshot_nav = fund.get("nav")
        out["nav"] = live["nav"]
        out["nav_date"] = live["date"]
        out["nav_live"] = True
        out["scheme_code"] = live["code"]
        # Movement since the snapshot is worth surfacing on its own.
        if snapshot_nav:
            out["nav_snapshot"] = snapshot_nav
            try:
                out["nav_change_pct"] = round(
                    (live["nav"] / snapshot_nav - 1) * 100, 2)
            except ZeroDivisionError:
                pass
    else:
        out["nav_live"] = False
    return out


@app.get("/funds/db/categories")
def fund_db_categories():
    """Every category in the snapshot, grouped by asset class."""
    funds = _load_funds()
    if not funds:
        raise HTTPException(503, "Fund database is not available on the server")

    groups: dict[str, dict] = {}
    for f in funds:
        cls = f.get("classification")
        if not cls:
            continue
        group = cls.split(" : ")[0].strip()
        groups.setdefault(group, {})
        groups[group][cls] = groups[group].get(cls, 0) + 1

    out = []
    for group in sorted(groups):
        cats = [{"name": k, "count": v}
                for k, v in sorted(groups[group].items(),
                                   key=lambda kv: -kv[1])]
        out.append({"group": group, "categories": cats,
                    "total": sum(c["count"] for c in cats)})

    return _clean({"groups": out, "total_funds": len(funds)})


@app.get("/funds/db/list")
def fund_db_list(category: str = "", sort: str = "aum",
                 limit: int = 60, q: str = ""):
    """
    Funds in one category, ranked. NAVs are refreshed from AMFI where matched.

    [sort] is one of aum, expense_ratio, r_1y, r_3y, r_5y, sharpe, alpha.
    """
    funds = _load_funds()
    if not funds:
        raise HTTPException(503, "Fund database is not available on the server")

    _refresh_navs()

    rows = funds
    if category:
        wanted = category.strip().lower()
        rows = [f for f in rows
                if str(f.get("classification", "")).lower() == wanted]
        if not rows:
            raise HTTPException(404, f"No funds in category '{category}'")

    if q:
        needle = q.strip().lower()
        rows = [f for f in rows if needle in str(f.get("name", "")).lower()]

    allowed = {"aum", "expense_ratio", "r_1m", "r_3m", "r_6m", "r_1y",
               "r_2y", "r_3y", "r_5y", "r_10y", "sharpe", "alpha", "sortino"}
    key = sort if sort in allowed else "aum"
    # Lower expense ratio ranks better; everything else is higher-is-better.
    ascending = key == "expense_ratio"
    rows = sorted(
        rows,
        key=lambda f: (f.get(key) is None,
                       (f.get(key) or 0) if ascending else -(f.get(key) or 0)),
    )

    live = [_with_live_nav(f) for f in rows[:limit]]
    matched = sum(1 for f in live if f.get("nav_live"))

    return _clean({
        "category": category or "All",
        "sort": key,
        "count": len(live),
        "total_in_category": len(rows),
        "nav_matched": matched,
        "nav_refreshed": _nav_refreshed_at.isoformat()
        if _nav_refreshed_at else None,
        "plan": "Regular",
        "funds": live,
    })


@app.get("/funds/db/search")
def fund_db_search(q: str, limit: int = 40):
    """Search the snapshot by fund name."""
    if len(q.strip()) < 2:
        raise HTTPException(400, "Search needs at least 2 characters")

    funds = _load_funds()
    if not funds:
        raise HTTPException(503, "Fund database is not available on the server")

    _refresh_navs()
    needle = q.strip().lower()

    scored = []
    for f in funds:
        name = str(f.get("name", "")).lower()
        if needle in name:
            # Prefix matches are almost always what was meant.
            scored.append((0 if name.startswith(needle) else 1,
                           -(f.get("aum") or 0), f))
    scored.sort(key=lambda t: (t[0], t[1]))

    live = [_with_live_nav(f) for _, _, f in scored[:limit]]
    return _clean({"query": q, "count": len(live), "funds": live})


@app.get("/funds/db/fund")
def fund_db_detail(name: str):
    """Everything the snapshot holds on one fund, with a live NAV if matched."""
    funds = _load_funds()
    if not funds:
        raise HTTPException(503, "Fund database is not available on the server")

    _refresh_navs()
    target = name.strip().lower()

    match = next((f for f in funds
                  if str(f.get("name", "")).lower() == target), None)
    if match is None:
        match = next((f for f in funds
                      if target in str(f.get("name", "")).lower()), None)
    if match is None:
        raise HTTPException(404, f"No fund named '{name}'")

    fund = _with_live_nav(match)

    # Peers in the same category, for context on the metrics above.
    peers = [f for f in funds
             if f.get("classification") == match.get("classification")
             and f.get("name") != match.get("name")]
    peers.sort(key=lambda f: -(f.get("aum") or 0))

    def rank_of(field: str, higher_better: bool = True):
        same = [f for f in funds
                if f.get("classification") == match.get("classification")
                and f.get(field) is not None]
        if match.get(field) is None or not same:
            return None
        same.sort(key=lambda f: -f[field] if higher_better else f[field])
        for i, f in enumerate(same, 1):
            if f.get("name") == match.get("name"):
                return {"rank": i, "of": len(same)}
        return None

    return _clean({
        "fund": fund,
        "ranks": {
            "r_1y": rank_of("r_1y"),
            "r_3y": rank_of("r_3y"),
            "r_5y": rank_of("r_5y"),
            "sharpe": rank_of("sharpe"),
            "expense_ratio": rank_of("expense_ratio", higher_better=False),
            "aum": rank_of("aum"),
        },
        "peers": [_with_live_nav(p) for p in peers[:8]],
    })


@app.get("/funds/db/refresh-nav")
def fund_db_refresh_nav():
    """Force a NAV refresh and report how many schemes matched."""
    result = _refresh_navs(force=True)
    funds = _load_funds()
    matched = sum(1 for f in funds if f.get("key") in _nav_cache)
    return _clean({
        **result,
        "funds_in_db": len(funds),
        "funds_matched": matched,
        "match_rate": round(matched / len(funds) * 100, 1) if funds else 0,
    })


# ===========================================================================
# HOLDINGS EXPLORER
# ===========================================================================
#
# Real portfolio disclosures from AMC monthly factsheets: which stocks each
# fund actually owns, at what weight. This is the one thing AMFI's public
# feed never carries, so it ships as a bundled snapshot.
#
# Coverage is partial — only the AMCs that publish machine-readable monthly
# disclosures are included, and every response says which those are so the
# absence of a fund is never mistaken for the absence of a holding.

_HOLDINGS_FILE = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                               "holdings_data.json")

_holdings_cache: dict | None = None


def _load_holdings() -> dict:
    """The bundled disclosure snapshot, loaded once per process."""
    global _holdings_cache
    if _holdings_cache is None:
        try:
            with open(_HOLDINGS_FILE, "r", encoding="utf-8") as fh:
                _holdings_cache = _json.load(fh)
        except Exception:
            _holdings_cache = {"as_on": None, "securities": {}, "funds": {}}
    return _holdings_cache


def _fund_rows(key: str) -> list:
    """Holdings of one fund as dicts, weight-sorted."""
    data = _load_holdings()
    secs = data.get("securities", {})
    out = []
    for isin, pct, value, qty in data.get("funds", {}).get(key, []):
        meta = secs.get(isin, [isin, "Other"])
        out.append({
            "isin": isin,
            "instrument": meta[0],
            "industry": meta[1],
            "pct_nav": pct,
            "value_lakh": value,
            "quantity": qty,
        })
    return out


@app.get("/holdings/summary")
def holdings_summary():
    """Coverage of the disclosure snapshot."""
    data = _load_holdings()
    funds = data.get("funds", {})
    if not funds:
        raise HTTPException(
            503, "Holdings data is not available on the server")

    amcs = sorted({k.split("|", 1)[0] for k in funds})
    total_rows = sum(len(v) for v in funds.values())

    return _clean({
        "as_on": data.get("as_on"),
        "funds": len(funds),
        "amcs": len(amcs),
        "amc_names": amcs,
        "holdings": total_rows,
        "securities": len(data.get("securities", {})),
    })


@app.get("/holdings/funds")
def holdings_funds(amc: str = ""):
    """Every fund in the snapshot, optionally filtered to one AMC."""
    data = _load_holdings()
    funds = data.get("funds", {})
    if not funds:
        raise HTTPException(
            503, "Holdings data is not available on the server")

    out = []
    for key in sorted(funds):
        fund_amc, fund_name = key.split("|", 1)
        if amc and fund_amc.lower() != amc.strip().lower():
            continue
        out.append({
            "key": key,
            "amc": fund_amc,
            "fund": fund_name,
            "holdings": len(funds[key]),
        })

    return _clean({"count": len(out), "funds": out})


@app.get("/holdings/stock")
def holdings_stock(q: str):
    """
    Which funds hold a given stock, ranked by weight.

    Matching is on the instrument name, so a partial query like "hdfc bank"
    returns every security whose name contains it — the caller picks which.
    """
    if len(q.strip()) < 3:
        raise HTTPException(400, "Search needs at least 3 characters")

    data = _load_holdings()
    secs = data.get("securities", {})
    if not secs:
        raise HTTPException(
            503, "Holdings data is not available on the server")

    needle = q.strip().lower()
    matching = {isin: meta for isin, meta in secs.items()
                if needle in meta[0].lower()}
    if not matching:
        raise HTTPException(404, f"No security matching '{q}'")

    # Group by security, since one query can hit several listings.
    by_isin: dict[str, list] = {isin: [] for isin in matching}
    for key, rows in data.get("funds", {}).items():
        amc, fund = key.split("|", 1)
        for isin, pct, value, qty in rows:
            if isin in by_isin:
                by_isin[isin].append({
                    "amc": amc, "fund": fund,
                    "pct_nav": pct, "value_lakh": value, "quantity": qty,
                })

    results = []
    for isin, holders in by_isin.items():
        if not holders:
            continue
        holders.sort(key=lambda h: -(h["pct_nav"] or 0))
        total_value = sum(h["value_lakh"] or 0 for h in holders)
        results.append({
            "isin": isin,
            "instrument": matching[isin][0],
            "industry": matching[isin][1],
            "fund_count": len(holders),
            "total_value_cr": round(total_value / 100, 2),
            "max_weight": holders[0]["pct_nav"],
            "holders": holders[:40],
        })

    results.sort(key=lambda r: -r["fund_count"])
    return _clean({
        "query": q,
        "as_on": data.get("as_on"),
        "matches": len(results),
        "securities": results[:12],
    })


@app.get("/holdings/fund")
def holdings_fund(key: str):
    """One fund's portfolio, with sector allocation and concentration."""
    data = _load_holdings()
    if key not in data.get("funds", {}):
        raise HTTPException(404, f"No holdings for '{key}'")

    rows = _fund_rows(key)
    amc, fund = key.split("|", 1)

    sectors: dict[str, float] = {}
    for r in rows:
        sectors[r["industry"]] = sectors.get(r["industry"], 0) + (r["pct_nav"] or 0)
    sector_list = [{"industry": k, "pct_nav": round(v, 2)}
                   for k, v in sorted(sectors.items(), key=lambda kv: -kv[1])]

    disclosed = sum(r["pct_nav"] or 0 for r in rows)
    top10 = sum(r["pct_nav"] or 0 for r in rows[:10])

    return _clean({
        "as_on": data.get("as_on"),
        "amc": amc,
        "fund": fund,
        "count": len(rows),
        "disclosed_pct": round(disclosed, 2),
        "top10_pct": round(top10, 2),
        "sectors": sector_list,
        "holdings": rows,
    })


@app.get("/holdings/overlap")
def holdings_overlap(a: str, b: str):
    """
    Portfolio overlap between two funds.

    Overlap is the sum of the smaller weight of each shared holding — the
    standard measure. Two funds each holding 5% of the same stock overlap by
    5% there; if one holds 5% and the other 2%, they overlap by 2%.
    """
    data = _load_holdings()
    funds = data.get("funds", {})
    for key in (a, b):
        if key not in funds:
            raise HTTPException(404, f"No holdings for '{key}'")
    if a == b:
        raise HTTPException(400, "Pick two different funds")

    rows_a = {r["isin"]: r for r in _fund_rows(a)}
    rows_b = {r["isin"]: r for r in _fund_rows(b)}

    common = []
    overlap_pct = 0.0
    for isin, ra in rows_a.items():
        rb = rows_b.get(isin)
        if rb is None:
            continue
        wa = ra["pct_nav"] or 0
        wb = rb["pct_nav"] or 0
        shared = min(wa, wb)
        overlap_pct += shared
        common.append({
            "instrument": ra["instrument"],
            "industry": ra["industry"],
            "pct_a": wa,
            "pct_b": wb,
            "shared": round(shared, 3),
        })

    common.sort(key=lambda c: -c["shared"])

    if overlap_pct > 60:
        verdict = "Very high — these are close to the same portfolio"
        band = "very_high"
    elif overlap_pct > 40:
        verdict = "High — holding both adds little diversification"
        band = "high"
    elif overlap_pct > 20:
        verdict = "Moderate — some shared exposure"
        band = "moderate"
    else:
        verdict = "Low — genuinely different portfolios"
        band = "low"

    amc_a, fund_a = a.split("|", 1)
    amc_b, fund_b = b.split("|", 1)

    return _clean({
        "as_on": data.get("as_on"),
        "fund_a": {"amc": amc_a, "fund": fund_a, "holdings": len(rows_a)},
        "fund_b": {"amc": amc_b, "fund": fund_b, "holdings": len(rows_b)},
        "common_count": len(common),
        "overlap_pct": round(overlap_pct, 2),
        "verdict": verdict,
        "band": band,
        "common": common[:60],
    })


# ===========================================================================
# STRATEGY BACKTEST
# ===========================================================================
#
# Event-driven simulation on real historical prices. The signal is decided on
# day t's close and executed at day t+1's open, so no trade ever uses a price
# it couldn't have known — the commonest way a backtest flatters itself.
#
# Stop-loss and take-profit are checked against the day's low and high while
# in position. One position at a time, fully invested, no leverage. Brokerage
# and slippage are not modelled, so real results would be slightly worse.

BACKTEST_STRATEGIES = {
    "SMA Crossover": {
        "description": "Hold while the fast moving average is above the slow one.",
        "params": [
            {"key": "fast", "label": "Fast SMA (days)", "min": 5, "max": 50,
             "default": 20},
            {"key": "slow", "label": "Slow SMA (days)", "min": 20, "max": 200,
             "default": 50},
        ],
    },
    "RSI Mean Reversion": {
        "description": "Buy when oversold, exit once it recovers.",
        "params": [
            {"key": "rsi_window", "label": "RSI window", "min": 7, "max": 21,
             "default": 14},
            {"key": "rsi_buy", "label": "Buy below RSI", "min": 15, "max": 40,
             "default": 30},
            {"key": "rsi_sell", "label": "Exit above RSI", "min": 50, "max": 80,
             "default": 60},
        ],
    },
    "Momentum (200-day trend)": {
        "description": "Hold only while price is above its 200-day average.",
        "params": [],
    },
    "Buy & Hold": {
        "description": "Buy on day one and never sell — the benchmark.",
        "params": [],
    },
}

BACKTEST_PERIODS = {"1 Year": "1y", "2 Years": "2y",
                    "5 Years": "5y", "10 Years": "10y"}


def _bt_rsi(closes: list, window: int) -> list:
    """Wilder-style RSI on a simple moving average of gains and losses."""
    out = [None] * len(closes)
    if len(closes) <= window:
        return out
    gains, losses = [], []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    for i in range(window, len(gains) + 1):
        avg_gain = sum(gains[i - window:i]) / window
        avg_loss = sum(losses[i - window:i]) / window
        if avg_loss == 0:
            out[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[i] = 100 - 100 / (1 + rs)
    return out


def _bt_sma(values: list, window: int) -> list:
    out = [None] * len(values)
    if window <= 0 or len(values) < window:
        return out
    running = sum(values[:window])
    out[window - 1] = running / window
    for i in range(window, len(values)):
        running += values[i] - values[i - window]
        out[i] = running / window
    return out


def _bt_signals(closes: list, strategy: str, params: dict) -> list:
    """Desired position per day: 1 = long, 0 = flat."""
    n = len(closes)

    if strategy == "Buy & Hold":
        return [1] * n

    if strategy == "SMA Crossover":
        fast = _bt_sma(closes, int(params.get("fast", 20)))
        slow = _bt_sma(closes, int(params.get("slow", 50)))
        return [1 if (fast[i] is not None and slow[i] is not None
                      and fast[i] > slow[i]) else 0 for i in range(n)]

    if strategy == "Momentum (200-day trend)":
        sma = _bt_sma(closes, 200)
        return [1 if (sma[i] is not None and closes[i] > sma[i]) else 0
                for i in range(n)]

    if strategy == "RSI Mean Reversion":
        window = int(params.get("rsi_window", 14))
        buy = float(params.get("rsi_buy", 30))
        sell = float(params.get("rsi_sell", 60))
        r = _bt_rsi(closes, window)
        # Position persists between thresholds, so carry the last state.
        out, state = [], 0
        for value in r:
            if value is not None:
                if value < buy:
                    state = 1
                elif value > sell:
                    state = 0
            out.append(state)
        return out

    return [0] * n


def _bt_simulate(candles: list, desired: list, capital: float,
                 stop_loss: float, take_profit: float, use_sl_tp: bool):
    """
    Walk the price series day by day.

    Returns (equity curve, trades). Signals are lagged one day so a decision
    made on today's close is acted on at tomorrow's open.
    """
    cash, shares = capital, 0.0
    entry_price = None
    equity, trades = [], []

    lagged = [0] + desired[:-1]

    for i, c in enumerate(candles):
        want = lagged[i]

        # Entry and signal exits happen at the open.
        if shares == 0 and want == 1 and c["open"] > 0:
            entry_price = c["open"]
            shares = cash / entry_price
            cash = 0.0
            trades.append({"entry_date": c["date"], "entry": entry_price,
                           "exit_date": None, "exit": None, "reason": None})
        elif shares > 0 and want == 0:
            cash = shares * c["open"]
            trades[-1].update({"exit_date": c["date"], "exit": c["open"],
                               "reason": "Signal"})
            shares, entry_price = 0.0, None

        # Stop-loss and take-profit are intraday, so use the day's range.
        if shares > 0 and use_sl_tp and entry_price:
            sl_price = entry_price * (1 - stop_loss / 100)
            tp_price = entry_price * (1 + take_profit / 100)
            if c["low"] <= sl_price:
                cash = shares * sl_price
                trades[-1].update({"exit_date": c["date"], "exit": sl_price,
                                   "reason": "Stop Loss"})
                shares, entry_price = 0.0, None
            elif c["high"] >= tp_price:
                cash = shares * tp_price
                trades[-1].update({"exit_date": c["date"], "exit": tp_price,
                                   "reason": "Take Profit"})
                shares, entry_price = 0.0, None

        equity.append(cash + shares * c["close"])

    # An open position is marked to the last close, not sold.
    if trades and trades[-1]["exit"] is None:
        trades[-1].update({"exit_date": candles[-1]["date"],
                           "exit": candles[-1]["close"],
                           "reason": "Open (marked)"})

    return equity, trades


def _bt_metrics(equity: list, capital: float, days: int) -> dict:
    if not equity or capital <= 0:
        return {}

    final = equity[-1]
    total = (final / capital - 1) * 100
    years = days / 365.25

    cagr = None
    if years > 0.25 and final > 0:
        cagr = ((final / capital) ** (1 / years) - 1) * 100

    # Daily returns for Sharpe, annualised at 252 trading days.
    rets = []
    for i in range(1, len(equity)):
        if equity[i - 1] > 0:
            rets.append(equity[i] / equity[i - 1] - 1)
    sharpe = 0.0
    if len(rets) > 1:
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        sd = var ** 0.5
        if sd > 0:
            sharpe = mean / sd * (252 ** 0.5)

    peak, max_dd = equity[0], 0.0
    for v in equity:
        peak = max(peak, v)
        if peak > 0:
            max_dd = min(max_dd, v / peak - 1)

    return {
        "final": round(final, 2),
        "total": round(total, 2),
        "cagr": round(cagr, 2) if cagr is not None else None,
        "sharpe": round(sharpe, 2),
        "max_drawdown": round(max_dd * 100, 2),
    }


@app.get("/backtest/strategies")
def backtest_strategies():
    """Strategies, their tunable parameters, and the periods on offer."""
    return {
        "strategies": [
            {"name": k, "description": v["description"], "params": v["params"]}
            for k, v in BACKTEST_STRATEGIES.items()
        ],
        "periods": list(BACKTEST_PERIODS.keys()),
    }


@app.get("/backtest/run")
def backtest_run(symbol: str, strategy: str = "SMA Crossover",
                 period: str = "5 Years", capital: float = 100000,
                 fast: int = 20, slow: int = 50,
                 rsi_window: int = 14, rsi_buy: float = 30,
                 rsi_sell: float = 60,
                 stop_loss: float = 10, take_profit: float = 20,
                 use_sl_tp: bool = False):
    """
    Backtest one strategy on one symbol, against Buy & Hold on the same data.
    """
    if strategy not in BACKTEST_STRATEGIES:
        raise HTTPException(
            400, f"Unknown strategy. Options: {', '.join(BACKTEST_STRATEGIES)}")
    if period not in BACKTEST_PERIODS:
        raise HTTPException(
            400, f"Unknown period. Options: {', '.join(BACKTEST_PERIODS)}")
    if strategy == "SMA Crossover" and fast >= slow:
        raise HTTPException(
            400, "Fast SMA must be shorter than slow SMA")
    if capital <= 0:
        raise HTTPException(400, "Capital must be positive")

    try:
        import yfinance as yf
    except ImportError:
        raise HTTPException(503, "yfinance is not installed on the server")

    sym = symbol.strip().upper()
    if not sym.startswith("^") and not sym.endswith((".NS", ".BO")):
        sym = f"{sym}.NS"

    try:
        df = _price_history(sym, BACKTEST_PERIODS[period])
    except Exception as e:
        if _yf_rate_limited(e):
            raise HTTPException(
                503, "Yahoo is rate-limiting this server. Try again shortly.")
        raise HTTPException(502, f"Price download failed: {e}")

    if df.empty or len(df) < 60:
        raise HTTPException(
            404, f"Not enough price history for '{symbol}'. NSE symbols need "
                 "no suffix (RELIANCE).")

    if strategy == "Momentum (200-day trend)" and len(df) < 220:
        raise HTTPException(
            400, "The 200-day trend strategy needs about a year of data. "
                 "Choose a longer period.")

    candles = []
    for stamp, row in df.iterrows():
        try:
            o, h, l, c = (float(row["Open"]), float(row["High"]),
                          float(row["Low"]), float(row["Close"]))
        except (KeyError, TypeError, ValueError):
            continue
        if any(v != v for v in (o, h, l, c)):   # NaN check
            continue
        candles.append({"date": stamp.strftime("%Y-%m-%d"),
                        "open": o, "high": h, "low": l, "close": c})

    if len(candles) < 60:
        raise HTTPException(404, f"Not enough usable prices for '{symbol}'")

    closes = [c["close"] for c in candles]
    params = {"fast": fast, "slow": slow, "rsi_window": rsi_window,
              "rsi_buy": rsi_buy, "rsi_sell": rsi_sell}

    signals = _bt_signals(closes, strategy, params)
    equity, trades = _bt_simulate(candles, signals, capital,
                                  stop_loss, take_profit, use_sl_tp)

    # Benchmark on identical data, so the comparison is like for like.
    bh_equity, _ = _bt_simulate(candles, [1] * len(candles), capital,
                                0, 0, False)

    from datetime import datetime as _dt
    span = (_dt.strptime(candles[-1]["date"], "%Y-%m-%d")
            - _dt.strptime(candles[0]["date"], "%Y-%m-%d")).days or 1

    metrics = _bt_metrics(equity, capital, span)
    benchmark = _bt_metrics(bh_equity, capital, span)

    # Trade statistics from closed positions only.
    closed = [t for t in trades if t["exit"] is not None]
    returns = [(t["exit"] / t["entry"] - 1) * 100
               for t in closed if t["entry"]]
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r <= 0]
    loss_sum = abs(sum(losses))

    trade_rows = []
    for t in closed[-40:]:
        pct = (t["exit"] / t["entry"] - 1) * 100 if t["entry"] else None
        trade_rows.append({
            "entry_date": t["entry_date"],
            "entry": round(t["entry"], 2),
            "exit_date": t["exit_date"],
            "exit": round(t["exit"], 2),
            "reason": t["reason"],
            "return_pct": round(pct, 2) if pct is not None else None,
        })

    # Downsample the curves; a 10-year daily series is far more than a phone
    # chart can show.
    step = max(len(equity) // 180, 1)
    curve = [{"date": candles[i]["date"],
              "strategy": round(equity[i], 2),
              "buy_hold": round(bh_equity[i], 2)}
             for i in range(0, len(equity), step)]
    if curve and curve[-1]["date"] != candles[-1]["date"]:
        curve.append({"date": candles[-1]["date"],
                      "strategy": round(equity[-1], 2),
                      "buy_hold": round(bh_equity[-1], 2)})

    edge = metrics["total"] - benchmark["total"]

    return _clean({
        "symbol": sym.replace(".NS", "").replace(".BO", ""),
        "strategy": strategy,
        "period": period,
        "capital": capital,
        "from": candles[0]["date"],
        "to": candles[-1]["date"],
        "sessions": len(candles),
        "metrics": metrics,
        "benchmark": benchmark,
        "edge_pp": round(edge, 2),
        "beat_benchmark": edge > 0,
        "trades": {
            "total": len(closed),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(returns) * 100, 1)
            if returns else None,
            "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
            "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
            "best": round(max(returns), 2) if returns else None,
            "worst": round(min(returns), 2) if returns else None,
            "profit_factor": round(sum(wins) / loss_sum, 2)
            if loss_sum > 0 else None,
            "recent": trade_rows,
        },
        "curve": curve,
    })


@app.get("/debug/price-source")
def debug_price_source(symbol: str = "RELIANCE"):
    """
    Which source can currently serve prices for a symbol.

    Useful when data looks stale or missing: it separates "Dhan token expired"
    from "Yahoo is throttling" without reading server logs.
    """
    out = {"symbol": symbol}

    # --- Dhan ---
    try:
        df = _dhan_history(symbol, 30)
        if df is not None and not df.empty:
            out["dhan"] = {
                "ok": True,
                "rows": len(df),
                "last_close": round(float(df["Close"].iloc[-1]), 2),
                "last_date": str(df.index[-1])[:10],
            }
        else:
            out["dhan"] = {"ok": False,
                           "reason": "no data returned (symbol or token?)"}
    except Exception as e:
        out["dhan"] = {"ok": False, "reason": f"{type(e).__name__}: {e}"}

    # --- Yahoo ---
    try:
        sym = symbol if symbol.startswith("^") else f"{symbol}.NS"
        df = _yf_history(sym, "1mo")
        if df is not None and not df.empty:
            out["yahoo"] = {
                "ok": True,
                "rows": len(df),
                "last_close": round(float(df["Close"].iloc[-1]), 2),
            }
        else:
            out["yahoo"] = {"ok": False, "reason": "empty response"}
    except Exception as e:
        out["yahoo"] = {
            "ok": False,
            "rate_limited": _yf_rate_limited(e),
            "reason": f"{type(e).__name__}: {str(e)[:140]}",
        }

    out["cache_entries"] = len(_yf_cache)
    return _clean(out)


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
