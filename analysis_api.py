"""
analysis_api.py
===============
Fundamental + technical analysis engine for NIFTY 50 and NIFTY NEXT 50.

Data source: Yahoo Finance (yfinance) — statements, ratios, price history.
Constituent lists: downloaded from NSE's official index CSVs, with a
built-in fallback list if NSE blocks the request.

Main entry points
-----------------
    get_universe("NIFTY50" | "NIFTYNEXT50" | "BOTH") -> list[str]  (.NS symbols)
    scan_universe(symbols)      -> DataFrame  (fast: one row per stock)
    get_fundamentals(symbol)    -> dict of ratios + info
    get_statements(symbol)      -> dict of income/balance/cashflow DataFrames
    get_technicals(symbol)      -> (price DataFrame with indicators, signals dict)
"""

import io
import logging

import numpy as np
import pandas as pd
import requests
import yfinance as yf

logger = logging.getLogger("analysis_api")

NSE_LISTS = {
    "NIFTY50": "https://archives.nseindia.com/content/indices/ind_nifty50list.csv",
    "NIFTYNEXT50": "https://archives.nseindia.com/content/indices/ind_niftynext50list.csv",
    "LARGECAP": "https://archives.nseindia.com/content/indices/ind_nifty100list.csv",
    "MIDCAP": "https://archives.nseindia.com/content/indices/ind_niftymidcap150list.csv",
    "SMALLCAP": "https://archives.nseindia.com/content/indices/ind_niftysmallcap250list.csv",
}

# Expected minimum constituent counts (sanity check on downloads)
_MIN_COUNT = {"NIFTY50": 45, "NIFTYNEXT50": 45, "LARGECAP": 90,
              "MIDCAP": 140, "SMALLCAP": 230}

# Fallback lists (approximate — refresh from NSE for the exact current set)
_FALLBACK = {
    "NIFTY50": [
        "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK",
        "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV", "BEL", "BHARTIARTL",
        "CIPLA", "COALINDIA", "DRREDDY", "EICHERMOT", "ETERNAL",
        "GRASIM", "HCLTECH", "HDFCBANK", "HDFCLIFE", "HEROMOTOCO",
        "HINDALCO", "HINDUNILVR", "ICICIBANK", "INDUSINDBK", "INFY",
        "ITC", "JIOFIN", "JSWSTEEL", "KOTAKBANK", "LT",
        "M&M", "MARUTI", "NESTLEIND", "NTPC", "ONGC",
        "POWERGRID", "RELIANCE", "SBILIFE", "SBIN", "SHRIRAMFIN",
        "SUNPHARMA", "TATACONSUM", "TATAMOTORS", "TATASTEEL", "TCS",
        "TECHM", "TITAN", "TRENT", "ULTRACEMCO", "WIPRO",
    ],
    "NIFTYNEXT50": [
        "ABB", "ADANIENSOL", "ADANIGREEN", "ADANIPOWER", "AMBUJACEM",
        "BAJAJHLDNG", "BAJAJHFL", "BANKBARODA", "BHEL", "BOSCHLTD",
        "BRITANNIA", "CANBK", "CGPOWER", "CHOLAFIN", "DABUR",
        "DIVISLAB", "DLF", "DMART", "GAIL", "GODREJCP",
        "HAVELLS", "HAL", "ICICIGI", "ICICIPRULI", "INDHOTEL",
        "INDIGO", "IOC", "IRFC", "JINDALSTEL", "JSWENERGY",
        "LICI", "LODHA", "LTIM", "MOTHERSON", "NAUKRI",
        "PFC", "PIDILITIND", "PNB", "RECLTD", "SHREECEM",
        "SIEMENS", "SWIGGY", "TATAPOWER", "TORNTPHARM", "TVSMOTOR",
        "UNITDSPR", "VBL", "VEDL", "ZYDUSLIFE", "HYUNDAI",
    ],
}


# ---------------------------------------------------------------------- #
# Universe / constituents
# ---------------------------------------------------------------------- #
def _fetch_nse_list(index: str) -> list[str] | None:
    """
    NSE's official constituent CSV, cached to disk after first success
    (file: nse_<index>.csv) so later runs work even if NSE blocks us.
    Returns None if neither download nor cache is available.
    """
    import os
    cache_file = f"nse_{index.lower()}.csv"
    try:
        r = requests.get(
            NSE_LISTS[index], timeout=15,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        )
        r.raise_for_status()
        df = pd.read_csv(io.StringIO(r.text))
        col = "Symbol" if "Symbol" in df.columns else df.columns[2]
        symbols = df[col].astype(str).str.strip().tolist()
        if len(symbols) >= _MIN_COUNT.get(index, 45):
            pd.DataFrame({"Symbol": symbols}).to_csv(cache_file, index=False)
            return symbols
    except Exception as e:
        logger.warning("NSE list fetch failed for %s: %s", index, e)
    # fall back to a previously cached download
    if os.path.exists(cache_file):
        logger.info("Using cached constituent list: %s", cache_file)
        return pd.read_csv(cache_file)["Symbol"].astype(str).tolist()
    return None


def get_universe(which: str = "BOTH") -> list[str]:
    """
    Return .NS-suffixed Yahoo symbols for the chosen universe:
      NIFTY50, NIFTYNEXT50, BOTH (top 100 via the two 50s),
      LARGECAP (NIFTY 100), MIDCAP (NIFTY Midcap 150, ranks 101-250),
      SMALLCAP (NIFTY Smallcap 250, ranks 251-500).
    """
    which = which.upper().replace(" ", "")
    if which == "BOTH":
        indices = ["NIFTY50", "NIFTYNEXT50"]
    else:
        indices = [which]

    out: list[str] = []
    for idx in indices:
        symbols = _fetch_nse_list(idx)
        if symbols is None:
            if idx in _FALLBACK:
                symbols = _FALLBACK[idx]
            elif idx == "LARGECAP":
                symbols = _FALLBACK["NIFTY50"] + _FALLBACK["NIFTYNEXT50"]
            else:
                raise RuntimeError(
                    f"Could not download the {idx} constituent list from NSE and "
                    "no cached copy exists yet. Check your internet connection "
                    "and try again."
                )
        out += symbols
    # de-duplicate while keeping order
    seen = set()
    out = [s for s in out if not (s in seen or seen.add(s))]
    return [f"{s}.NS" for s in out]


# ---------------------------------------------------------------------- #
# Technical indicators (computed with pandas — no extra deps)
# ---------------------------------------------------------------------- #
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add SMA/EMA, RSI, MACD, Bollinger, ATR, Stochastic to an OHLCV frame."""
    c, h, l = df["Close"], df["High"], df["Low"]

    for n in (20, 50, 200):
        df[f"SMA{n}"] = c.rolling(n).mean()
    df["EMA12"] = c.ewm(span=12, adjust=False).mean()
    df["EMA26"] = c.ewm(span=26, adjust=False).mean()

    # RSI (14, Wilder)
    delta = c.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    df["RSI"] = 100 - 100 / (1 + gain / loss)

    # MACD
    df["MACD"] = df["EMA12"] - df["EMA26"]
    df["MACD_signal"] = df["MACD"].ewm(span=9, adjust=False).mean()
    df["MACD_hist"] = df["MACD"] - df["MACD_signal"]

    # Bollinger (20, 2)
    std20 = c.rolling(20).std()
    df["BB_upper"] = df["SMA20"] + 2 * std20
    df["BB_lower"] = df["SMA20"] - 2 * std20

    # ATR (14)
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    df["ATR"] = tr.ewm(alpha=1 / 14, adjust=False).mean()

    # Stochastic %K/%D (14, 3)
    low14, high14 = l.rolling(14).min(), h.rolling(14).max()
    df["STOCH_K"] = 100 * (c - low14) / (high14 - low14)
    df["STOCH_D"] = df["STOCH_K"].rolling(3).mean()
    return df


def technical_signals(df: pd.DataFrame) -> dict:
    """Summarise the latest values into human-readable signals."""
    last = df.iloc[-1]
    c = last["Close"]
    sig = {
        "close": round(c, 2),
        "rsi": round(last["RSI"], 1),
        "macd_hist": round(last["MACD_hist"], 2),
        "atr": round(last["ATR"], 2),
        "stoch_k": round(last["STOCH_K"], 1),
        "52w_high": round(df["High"].tail(252).max(), 2),
        "52w_low": round(df["Low"].tail(252).min(), 2),
        "ret_1m_pct": round((c / df["Close"].iloc[-22] - 1) * 100, 2) if len(df) > 22 else None,
        "ret_1y_pct": round((c / df["Close"].iloc[-252] - 1) * 100, 2) if len(df) > 252 else None,
        "volatility_pct": round(df["Close"].pct_change().tail(252).std() * np.sqrt(252) * 100, 1),
    }
    sig["rsi_signal"] = ("Overbought" if sig["rsi"] > 70 else
                         "Oversold" if sig["rsi"] < 30 else "Neutral")
    sig["macd_signal"] = "Bullish" if last["MACD"] > last["MACD_signal"] else "Bearish"
    sig["trend"] = ("Uptrend" if c > last["SMA200"] else "Downtrend") if pd.notna(last["SMA200"]) else "—"
    sig["ma_signal"] = ("Golden (50>200)" if last["SMA50"] > last["SMA200"] else "Death (50<200)") \
        if pd.notna(last["SMA200"]) else "—"
    sig["pct_from_52w_high"] = round((c / sig["52w_high"] - 1) * 100, 1)
    return sig


def get_technicals(symbol: str, period: str = "2y"):
    """Price history with indicators + a signal summary for one stock."""
    df = yf.Ticker(symbol).history(period=period)
    if df.empty:
        return pd.DataFrame(), {}
    df = add_indicators(df)
    return df, technical_signals(df)


# ---------------------------------------------------------------------- #
# Fundamentals
# ---------------------------------------------------------------------- #
def _safe(d: dict, key, scale=1.0, nd=2):
    v = d.get(key)
    try:
        return round(float(v) * scale, nd)
    except (TypeError, ValueError):
        return None


def get_fundamentals(symbol: str) -> dict:
    """Key fundamental ratios for one stock (from Yahoo's info + statements)."""
    t = yf.Ticker(symbol)
    info = t.info or {}

    f = {
        "name": info.get("longName", symbol),
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "market_cap_cr": _safe(info, "marketCap", 1e-7, 0),   # ₹ crore
        # Valuation
        "pe": _safe(info, "trailingPE"),
        "forward_pe": _safe(info, "forwardPE"),
        "pb": _safe(info, "priceToBook"),
        "ps": _safe(info, "priceToSalesTrailing12Months"),
        "ev_ebitda": _safe(info, "enterpriseToEbitda"),
        "peg": _safe(info, "pegRatio"),
        "eps": _safe(info, "trailingEps"),
        "book_value": _safe(info, "bookValue"),
        "dividend_yield_pct": _safe(info, "dividendYield", 100),
        # Profitability
        "roe_pct": _safe(info, "returnOnEquity", 100),
        "roa_pct": _safe(info, "returnOnAssets", 100),
        "gross_margin_pct": _safe(info, "grossMargins", 100),
        "operating_margin_pct": _safe(info, "operatingMargins", 100),
        "net_margin_pct": _safe(info, "profitMargins", 100),
        # Health / leverage
        "debt_to_equity": _safe(info, "debtToEquity", 0.01),  # yahoo gives %
        "current_ratio": _safe(info, "currentRatio"),
        "quick_ratio": _safe(info, "quickRatio"),
        "interest_coverage": None,
        # Growth
        "revenue_growth_pct": _safe(info, "revenueGrowth", 100),
        "earnings_growth_pct": _safe(info, "earningsGrowth", 100),
        # Cash
        "free_cashflow_cr": _safe(info, "freeCashflow", 1e-7, 0),
        "operating_cashflow_cr": _safe(info, "operatingCashflow", 1e-7, 0),
    }

    # Interest coverage from the income statement if available
    try:
        fin = t.financials
        ebit = fin.loc["EBIT"].iloc[0] if "EBIT" in fin.index else None
        interest = fin.loc["Interest Expense"].iloc[0] if "Interest Expense" in fin.index else None
        if ebit and interest:
            f["interest_coverage"] = round(abs(float(ebit) / float(interest)), 2)
    except Exception:
        pass
    return f


def get_statements(symbol: str) -> dict:
    """Annual + quarterly income statement, balance sheet, cash flow (₹ crore)."""
    t = yf.Ticker(symbol)

    def cr(df: pd.DataFrame) -> pd.DataFrame:
        if df is None or df.empty:
            return pd.DataFrame()
        out = (df / 1e7).round(1)          # ₹ -> ₹ crore
        out.columns = [pd.Timestamp(col).strftime("%b-%Y") for col in out.columns]
        return out

    return {
        "income": cr(t.financials),
        "income_q": cr(t.quarterly_financials),
        "balance": cr(t.balance_sheet),
        "balance_q": cr(t.quarterly_balance_sheet),
        "cashflow": cr(t.cashflow),
        "cashflow_q": cr(t.quarterly_cashflow),
    }


# ---------------------------------------------------------------------- #
# Universe scan (fast table across all stocks)
# ---------------------------------------------------------------------- #
def scan_universe(symbols: list[str], progress_cb=None) -> pd.DataFrame:
    """
    One row per stock: key technicals (from a single batch price download)
    + key fundamentals (per-stock info calls — the slower part).
    progress_cb(i, n, symbol) is called as each stock's info is fetched.
    """
    prices = yf.download(symbols, period="1y", group_by="ticker",
                         auto_adjust=True, progress=False, threads=True)

    rows = []
    n = len(symbols)
    for i, sym in enumerate(symbols):
        if progress_cb:
            progress_cb(i, n, sym)
        row = {"symbol": sym.replace(".NS", "")}
        # --- technicals from batch download ---
        try:
            px = prices[sym].dropna(subset=["Close"]) if isinstance(prices.columns, pd.MultiIndex) else prices
            c = px["Close"]
            row["price"] = round(c.iloc[-1], 2)
            row["ret_1m_%"] = round((c.iloc[-1] / c.iloc[-22] - 1) * 100, 1) if len(c) > 22 else None
            row["ret_1y_%"] = round((c.iloc[-1] / c.iloc[0] - 1) * 100, 1)
            delta = c.diff()
            gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
            loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
            row["RSI"] = round((100 - 100 / (1 + gain / loss)).iloc[-1], 1)
            sma50 = c.rolling(50).mean().iloc[-1]
            sma200 = c.rolling(200).mean().iloc[-1]
            row["trend"] = ("Up" if c.iloc[-1] > sma200 else "Down") if pd.notna(sma200) else "—"
            row["50>200"] = "Yes" if (pd.notna(sma200) and sma50 > sma200) else "No"
            row["from_52wH_%"] = round((c.iloc[-1] / px["High"].max() - 1) * 100, 1)
        except Exception:
            pass
        # --- fundamentals (fast subset of info) ---
        try:
            info = yf.Ticker(sym).info or {}
            row["mcap_cr"] = _safe(info, "marketCap", 1e-7, 0)
            row["PE"] = _safe(info, "trailingPE", nd=1)
            row["PB"] = _safe(info, "priceToBook", nd=1)
            row["ROE_%"] = _safe(info, "returnOnEquity", 100, 1)
            row["D/E"] = _safe(info, "debtToEquity", 0.01)
            row["div_yield_%"] = _safe(info, "dividendYield", 100, 2)
            row["net_margin_%"] = _safe(info, "profitMargins", 100, 1)
            row["rev_growth_%"] = _safe(info, "revenueGrowth", 100, 1)
            row["sector"] = info.get("sector")
        except Exception:
            pass
        rows.append(row)

    return pd.DataFrame(rows)
