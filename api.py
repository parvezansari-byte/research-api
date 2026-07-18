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
    return {"indices": out}


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

    return {"symbol": sym, "fundamentals": fundamentals, "technicals": signals}


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
    return {"symbol": sym, "candles": out.where(out.notna(), None)
            .to_dict(orient="records")}


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

    return {
        "symbol": sym,
        "income": to_rows(stm.get("income")),
        "balance": to_rows(stm.get("balance")),
        "cashflow": to_rows(stm.get("cashflow")),
    }


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

    return {"count": len(df),
            "stocks": df.where(df.notna(), None).to_dict(orient="records")}


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
    return findings


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
    return result


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
