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
