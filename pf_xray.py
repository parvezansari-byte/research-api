"""
pf_xray.py
==========
PORTFOLIO X-RAY — see what you ACTUALLY own.

The problem this solves:
    You hold 5 mutual funds and feel diversified. But Parag Parikh Flexi Cap,
    HDFC Flexi Cap and SBI Bluechip may all hold HDFC Bank, Reliance and Infosys
    as top positions. You could be 15% exposed to HDFC Bank without ever having
    bought a single share of it.

    Nothing free in India shows you this. That's the point of the X-ray.

Data source: mfdata.in — free, open, no auth, 14,000+ schemes, stock-level
holdings. We degrade gracefully if a fund's holdings aren't available (coverage
isn't perfect, and pretending otherwise would be exactly the kind of fake data
we've been removing).
"""

from collections import defaultdict

import pandas as pd
import requests
import streamlit as st

from cache_compat import cache_data

BASE = "https://mfdata.in/api/v1"
TIMEOUT = 12


# ---------------------------------------------------------------------------
# API access (cached hard — holdings only update monthly)
# ---------------------------------------------------------------------------
@cache_data(ttl=86400)
def fetch_holdings(scheme_code: str) -> list[dict] | None:
    """
    Stock-level holdings for one scheme.
    Returns [{name, weight_pct, sector}, ...] or None if unavailable.
    """
    code = str(scheme_code).strip()
    for url in (f"{BASE}/schemes/{code}/holdings",
                f"{BASE}/schemes/{code}"):
        try:
            r = requests.get(url, timeout=TIMEOUT)
            if r.status_code != 200:
                continue
            payload = r.json()
            data = payload.get("data", payload)

            # the holdings list can be nested under a few plausible keys
            rows = None
            if isinstance(data, list):
                rows = data
            else:
                for k in ("holdings", "equity_holdings", "equity", "portfolio"):
                    if isinstance(data.get(k), list) and data[k]:
                        rows = data[k]
                        break
            if not rows:
                continue

            out = []
            for h in rows:
                if not isinstance(h, dict):
                    continue
                name = (h.get("name") or h.get("stock") or h.get("company")
                        or h.get("instrument"))
                wt = (h.get("weight") or h.get("weight_pct")
                      or h.get("percentage") or h.get("holding_pct"))
                if not name or wt is None:
                    continue
                try:
                    wt = float(wt)
                except (TypeError, ValueError):
                    continue
                out.append({
                    "name": _clean(str(name)),
                    "weight_pct": wt,
                    "sector": h.get("sector") or "Unknown",
                })
            if out:
                return out
        except Exception:
            continue
    return None


def _clean(name: str) -> str:
    """Normalise company names so 'HDFC Bank Ltd.' == 'HDFC Bank Limited'."""
    n = name.strip()
    for suffix in (" Ltd.", " Ltd", " Limited", " Ltd..", " Corp.", " Corporation"):
        if n.endswith(suffix):
            n = n[: -len(suffix)]
    return n.strip()


# ---------------------------------------------------------------------------
# The X-ray itself
# ---------------------------------------------------------------------------
def xray(funds: list[dict]) -> dict:
    """
    funds: [{scheme_code, name, value}, ...]  — the user's fund holdings
    Returns look-through exposure and overlap findings.
    """
    total = sum(f["value"] for f in funds) or 1

    exposure = defaultdict(float)          # stock -> ₹ across ALL funds
    per_fund_stocks: dict[str, set] = {}   # fund  -> set of stocks it holds
    sectors = defaultdict(float)
    covered, missing = [], []

    for f in funds:
        holds = fetch_holdings(f["scheme_code"])
        if not holds:
            missing.append(f["name"])
            continue
        covered.append(f["name"])
        per_fund_stocks[f["name"]] = {h["name"] for h in holds}
        for h in holds:
            # this fund's ₹ value × the stock's weight in that fund
            rupees = f["value"] * (h["weight_pct"] / 100)
            exposure[h["name"]] += rupees
            sectors[h["sector"]] += rupees

    if not exposure:
        return {"covered": [], "missing": missing, "stocks": []}

    covered_value = sum(f["value"] for f in funds if f["name"] in covered) or 1

    stocks = [
        {
            "stock": s,
            "value": round(v, 0),
            "pct_of_portfolio": round(v / covered_value * 100, 2),
            "held_by": sorted([fn for fn, ss in per_fund_stocks.items() if s in ss]),
        }
        for s, v in exposure.items()
    ]
    stocks.sort(key=lambda x: -x["value"])

    # the headline finding: stocks held by MORE THAN ONE of your funds
    overlaps = [s for s in stocks if len(s["held_by"]) > 1]

    sector_rows = sorted(
        [{"sector": k, "pct": round(v / covered_value * 100, 1)}
         for k, v in sectors.items()],
        key=lambda x: -x["pct"],
    )

    return {
        "covered": covered,
        "missing": missing,
        "stocks": stocks,
        "overlaps": overlaps,
        "sectors": sector_rows,
        "n_unique_stocks": len(stocks),
        "top_stock": stocks[0] if stocks else None,
        "top10_pct": round(sum(s["pct_of_portfolio"] for s in stocks[:10]), 1),
    }


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
def render_xray(funds: list[dict]):
    """
    funds: [{scheme_code, name, value}, ...]
    Renders the X-ray. Call from the analytics or mutual fund page.
    """
    st.markdown("### 🔬 Portfolio X-ray")
    st.caption("What you *actually* own, looking through your funds to the "
               "underlying stocks.")

    if len(funds) < 1:
        st.info("Add mutual funds to your portfolio to X-ray them.")
        return

    if not st.button("🔬 X-ray my funds", type="primary",
                     use_container_width=True, key="xray_btn"):
        return

    with st.spinner("Looking through your funds to the underlying stocks…"):
        res = xray(funds)

    if not res.get("stocks"):
        st.warning(
            "Couldn't get holdings data for these funds. Coverage isn't complete "
            "across all 14,000+ schemes — and rather than guess, we show nothing."
        )
        if res.get("missing"):
            st.caption("No holdings data for: " + ", ".join(res["missing"]))
        return

    if res["missing"]:
        st.info(f"ℹ️ X-rayed {len(res['covered'])} of "
                f"{len(res['covered']) + len(res['missing'])} funds. "
                f"No holdings data for: {', '.join(res['missing'])} — "
                "these are excluded from the numbers below.")

    # ---- the headline ----
    top = res["top_stock"]
    c1, c2, c3 = st.columns(3)
    c1.metric("Stocks you really own", res["n_unique_stocks"],
              help="Unique companies across all your funds")
    c2.metric("Biggest hidden position", top["stock"],
              f"{top['pct_of_portfolio']:.1f}%")
    c3.metric("Top 10 concentration", f"{res['top10_pct']:.0f}%",
              help="How much of your money sits in just 10 companies")

    # ---- THE KEY FINDING: overlap ----
    overlaps = res.get("overlaps") or []
    if overlaps:
        st.markdown("#### ⚠️ You own these stocks through *multiple* funds")
        st.caption("This is the diversification illusion — different funds, "
                   "same companies underneath.")
        odf = pd.DataFrame([
            {"Stock": o["stock"],
             "% of your money": o["pct_of_portfolio"],
             "Held by": f"{len(o['held_by'])} funds",
             "Which funds": ", ".join(o["held_by"])}
            for o in overlaps[:15]
        ])
        st.dataframe(
            odf, use_container_width=True, hide_index=True,
            column_config={"% of your money":
                           st.column_config.NumberColumn(format="%.2f%%")},
        )
    else:
        st.success("✅ No stock is held by more than one of your funds — "
                   "your funds genuinely don't overlap.")

    # ---- full look-through ----
    with st.expander(f"📋 All {res['n_unique_stocks']} stocks you own (look-through)"):
        sdf = pd.DataFrame([
            {"Stock": s["stock"], "Value ₹": s["value"],
             "% of portfolio": s["pct_of_portfolio"],
             "Funds holding it": len(s["held_by"])}
            for s in res["stocks"]
        ])
        st.dataframe(
            sdf, use_container_width=True, hide_index=True, height=420,
            column_config={
                "Value ₹": st.column_config.NumberColumn(format="%.0f"),
                "% of portfolio": st.column_config.NumberColumn(format="%.2f%%"),
            },
        )

    # ---- sector look-through ----
    if res.get("sectors"):
        st.markdown("#### 🏭 Your real sector exposure")
        st.caption("Aggregated across every stock inside every fund you hold.")
        sec = pd.DataFrame(res["sectors"][:12])
        st.dataframe(
            sec.rename(columns={"sector": "Sector", "pct": "% of portfolio"}),
            use_container_width=True, hide_index=True,
            column_config={"% of portfolio":
                           st.column_config.NumberColumn(format="%.1f%%")},
        )

    st.caption("Holdings data via mfdata.in (AMFI monthly disclosures — funds "
               "report holdings monthly, so this reflects their last disclosure, "
               "not today). For information only, not investment advice.")
