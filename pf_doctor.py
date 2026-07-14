"""
pf_doctor.py
============
AI Portfolio Doctor — a candid diagnosis of the user's REAL holdings.

The important design idea: we don't just hand a stock list to the AI and hope.
We COMPUTE hard diagnostics first (concentration, sector weights, correlation
between holdings, each position vs NIFTY, drawdowns), then ask the AI to
interpret those specific numbers. That's the difference between a vague
"you should diversify" and "your top 3 are all private banks that move
together — a rate shock hits all of them at once."

Used by pages/analytics.py.
"""

import numpy as np
import pandas as pd
import streamlit as st

from cache_compat import cache_data


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------
@cache_data(ttl=3600)
def _history(symbols: tuple[str, ...], period: str = "1y") -> pd.DataFrame:
    """Batched daily closes for the holdings + NIFTY, in one request."""
    import yfinance as yf

    tickers = [f"{s}.NS" for s in symbols] + ["^NSEI"]
    data = yf.download(tickers, period=period, progress=False,
                       auto_adjust=True, group_by="ticker", threads=True)
    out = {}
    for t in tickers:
        try:
            closes = data[t]["Close"].dropna() if len(tickers) > 1 else data["Close"].dropna()
            if not closes.empty:
                out[t.replace(".NS", "").replace("^NSEI", "NIFTY")] = closes
        except Exception:
            continue
    return pd.DataFrame(out).dropna(how="all")


@cache_data(ttl=3600)
def _sectors(symbols: tuple[str, ...]) -> dict:
    """Sector for each holding (one info call each — cached hard)."""
    import yfinance as yf
    out = {}
    for s in symbols:
        try:
            out[s] = (yf.Ticker(f"{s}.NS").info or {}).get("sector") or "Unknown"
        except Exception:
            out[s] = "Unknown"
    return out


# ---------------------------------------------------------------------------
# THE DIAGNOSTICS — real numbers, computed, no guessing
# ---------------------------------------------------------------------------
def diagnose(df: pd.DataFrame) -> dict:
    """
    df: holdings with columns symbol, invested, current_value, pnl_pct
    Returns a dict of computed findings the AI will interpret.
    """
    d = df.copy()
    d["symbol"] = d["symbol"].astype(str).str.replace(".NS", "", regex=False)
    d["value"] = d["current_value"].fillna(d["invested"])
    total = d["value"].sum()
    if not total:
        return {}

    syms = tuple(d["symbol"].tolist())
    findings: dict = {"n_holdings": len(d), "total_value": round(total, 0)}

    # ---- concentration ----
    d["weight_pct"] = d["value"] / total * 100
    d = d.sort_values("weight_pct", ascending=False)
    top = d.iloc[0]
    findings["top_holding"] = str(top["symbol"])
    findings["top_weight_pct"] = round(float(top["weight_pct"]), 1)
    findings["top3_weight_pct"] = round(float(d["weight_pct"].head(3).sum()), 1)

    # Herfindahl index -> "effective number of stocks" you really own
    hhi = float(((d["weight_pct"] / 100) ** 2).sum())
    findings["effective_holdings"] = round(1 / hhi, 1) if hhi else None

    findings["weights"] = [
        {"symbol": str(r["symbol"]), "weight_pct": round(float(r["weight_pct"]), 1),
         "pnl_pct": (round(float(r["pnl_pct"]), 1)
                     if pd.notna(r.get("pnl_pct")) else None)}
        for _, r in d.iterrows()
    ]

    # ---- sector concentration ----
    sec_map = _sectors(syms)
    d["sector"] = d["symbol"].map(sec_map)
    sec = (d.groupby("sector")["value"].sum() / total * 100).sort_values(ascending=False)
    findings["sectors"] = [{"sector": str(k), "weight_pct": round(float(v), 1)}
                           for k, v in sec.items()]
    findings["top_sector"] = str(sec.index[0])
    findings["top_sector_pct"] = round(float(sec.iloc[0]), 1)

    # ---- performance vs NIFTY, and correlation ----
    hist = _history(syms)
    if not hist.empty and "NIFTY" in hist.columns:
        rets = hist.pct_change().dropna()

        # each holding's 1y return vs NIFTY
        perf = []
        nifty_1y = (hist["NIFTY"].iloc[-1] / hist["NIFTY"].iloc[0] - 1) * 100
        findings["nifty_1y_pct"] = round(float(nifty_1y), 1)
        for s in syms:
            if s in hist.columns:
                r = (hist[s].iloc[-1] / hist[s].iloc[0] - 1) * 100
                perf.append({"symbol": s, "ret_1y_pct": round(float(r), 1),
                             "vs_nifty_pp": round(float(r - nifty_1y), 1)})
        findings["performance"] = perf
        findings["n_beating_nifty"] = sum(1 for p in perf if p["vs_nifty_pp"] > 0)

        # portfolio-weighted daily returns -> volatility & max drawdown (REAL)
        w = d.set_index("symbol")["weight_pct"] / 100
        cols = [s for s in syms if s in rets.columns]
        if cols:
            pw = (rets[cols] * w.reindex(cols).values).sum(axis=1)
            findings["volatility_pct"] = round(float(pw.std() * np.sqrt(252) * 100), 1)
            nifty_vol = float(rets["NIFTY"].std() * np.sqrt(252) * 100)
            findings["nifty_volatility_pct"] = round(nifty_vol, 1)
            curve = (1 + pw).cumprod()
            findings["max_drawdown_pct"] = round(
                float(((curve / curve.cummax()) - 1).min() * 100), 1)

        # correlation: which holdings move together? (the hidden risk)
        if len(cols) >= 2:
            corr = rets[cols].corr()
            pairs = []
            for i in range(len(cols)):
                for j in range(i + 1, len(cols)):
                    c = float(corr.iloc[i, j])
                    if c >= 0.7:      # strongly co-moving
                        pairs.append({"a": cols[i], "b": cols[j],
                                      "corr": round(c, 2)})
            pairs.sort(key=lambda x: -x["corr"])
            findings["high_correlation_pairs"] = pairs[:6]
            findings["avg_correlation"] = round(
                float(corr.values[np.triu_indices(len(cols), 1)].mean()), 2)

    # ---- winners & losers (real) ----
    if "pnl_pct" in d and d["pnl_pct"].notna().any():
        w_df = d.dropna(subset=["pnl_pct"])
        findings["worst"] = [
            {"symbol": str(r["symbol"]), "pnl_pct": round(float(r["pnl_pct"]), 1)}
            for _, r in w_df.nsmallest(3, "pnl_pct").iterrows()]
        findings["best"] = [
            {"symbol": str(r["symbol"]), "pnl_pct": round(float(r["pnl_pct"]), 1)}
            for _, r in w_df.nlargest(3, "pnl_pct").iterrows()]

    return findings


# ---------------------------------------------------------------------------
# AI interpretation
# ---------------------------------------------------------------------------
def _build_prompt(f: dict) -> str:
    import json
    return f"""You are a candid, experienced portfolio analyst talking to a retail investor in India. You explain what the DATA shows. You do NOT tell them to buy or sell any specific security.

Here are REAL computed diagnostics for this person's actual portfolio:

{json.dumps(f, indent=2, default=str)}

Notes on the data so you read it correctly:
- "effective_holdings" is 1/Herfindahl — how many stocks they *effectively* own once weighting is considered. If they hold 8 stocks but effective_holdings is 3.2, their money is really concentrated in ~3 names.
- "high_correlation_pairs" are holdings whose daily returns move together (corr >= 0.7). This is hidden risk: they look diversified but fall together.
- "vs_nifty_pp" is percentage points of out/under-performance versus the NIFTY 50 over one year.
- "max_drawdown_pct" is the worst peak-to-trough fall this portfolio actually experienced.

Write a portfolio diagnosis with these sections:

**The headline** — one or two sentences. What is the single most important thing about this portfolio? Be direct.

**Concentration** — interpret top_weight_pct, top3_weight_pct and effective_holdings. Is their money really spread out, or does it just look that way?

**Hidden correlation** — if high_correlation_pairs exist, explain plainly that these move together and what event would hit them at once (e.g. rate changes for banks, a global selloff for IT exporters). If there are none, say the holdings are reasonably independent.

**Sector tilt** — interpret the sector weights. Name the concentration and what it exposes them to.

**How it's actually doing** — use n_beating_nifty, nifty_1y_pct, volatility vs nifty_volatility_pct, and max_drawdown_pct. Was the extra risk rewarded? Be honest if a portfolio took more risk for less return.

**What to think about** — 3 concrete, honest points. Frame as questions or considerations, NOT instructions to buy/sell. E.g. "if X fell 30% tomorrow, could you sit through it?" rather than "sell X".

Rules:
- Use the ACTUAL numbers from the data. Cite them.
- Be candid. If it's badly concentrated, say so plainly. Do not flatter.
- NEVER tell them to buy, sell, hold, or trim a specific stock.
- Under 400 words. Plain English, no jargon without explaining it.
- Do not add a disclaimer; the app adds one."""


def render_doctor(df: pd.DataFrame, source: str):
    """Render the AI Portfolio Doctor button + diagnosis for real holdings."""
    st.markdown('<h2 class="section-title">🩺 AI PORTFOLIO DOCTOR</h2>',
                unsafe_allow_html=True)
    st.markdown('<div style="color:#94a3b8;font-size:13px;margin-bottom:10px;">'
                'A candid diagnosis of your actual holdings — concentration, '
                'hidden correlation, sector risk, and whether the risk paid off.'
                '</div>', unsafe_allow_html=True)

    if len(df) < 2:
        st.info("Add at least 2 holdings for a meaningful diagnosis.")
        return

    if not st.button("🩺 Diagnose my portfolio", type="primary",
                     use_container_width=True, key=f"doc_{source}"):
        return

    with st.spinner("Computing diagnostics on your real holdings…"):
        try:
            findings = diagnose(df)
        except Exception as e:
            st.error(f"Couldn't compute diagnostics: {e}")
            return
    if not findings:
        st.warning("Not enough data to diagnose this portfolio.")
        return

    # --- show the hard numbers first (these are computed, not AI) ---
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Effective holdings", findings.get("effective_holdings", "—"),
              help="1/Herfindahl — how many stocks you *really* own after weighting")
    c2.metric("Top 3 weight", f"{findings.get('top3_weight_pct', 0):.0f}%")
    c3.metric("Max drawdown", f"{findings.get('max_drawdown_pct', 0):.0f}%",
              help="Worst peak-to-trough fall this portfolio actually had")
    beat = findings.get("n_beating_nifty")
    c4.metric("Beating NIFTY", f"{beat}/{findings['n_holdings']}"
              if beat is not None else "—")

    pairs = findings.get("high_correlation_pairs") or []
    if pairs:
        st.warning("**Holdings that move together** (correlation ≥ 0.7): " +
                   " · ".join(f"{p['a']}–{p['b']} ({p['corr']})" for p in pairs))

    # --- AI interpretation ---
    try:
        from ai_analysis import _cached_generate
        prompt = _build_prompt(findings)
        with st.spinner("The doctor is reading your chart…"):
            text = _cached_generate(prompt)
    except Exception as e:
        msg = str(e).lower()
        if any(w in msg for w in ("quota", "429", "rate")):
            st.warning("Free AI quota is busy — try again in a minute.")
        elif any(w in msg for w in ("503", "unavailable", "overloaded")):
            st.warning("The AI is busy right now — click again in a few seconds.")
        else:
            st.error(f"AI diagnosis failed: {e}")
        return

    if text:
        st.markdown(text)
        st.caption("⚠️ AI interpretation of your real portfolio data, for education "
                   "only. **Not investment advice** — no recommendation to buy or "
                   "sell any security. Consult a SEBI-registered advisor.")
