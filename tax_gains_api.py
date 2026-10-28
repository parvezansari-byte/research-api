"""
Unrealised capital-gains estimate for one client:
  GET /crm/{owner}/clients/{client_id}/tax-gains?slab=30

Rules used (India, FY 2025-26 onward) - an ESTIMATE for conversations, not tax advice:
  * Equity funds: held 12 months or less -> short term, 20%.
    Held more than 12 months -> long term, 12.5% on total LTCG above Rs 1.25 lakh a year.
  * Debt funds: taxed at the investor's slab rate (slab param, default 30%).
  * Hybrid, gold, fund-of-funds and unmatched funds: no estimate (rules depend on the fund).
  * Excludes surcharge and 4% cess. The purchase date on each holding is the AVERAGE
    date, so a line bought in several lots is only approximately classified.
"""
import os
from datetime import date

from fastapi import FastAPI, HTTPException

import holdings_api
import portfolio_review_api as pr

STCG_RATE = 20.0
LTCG_RATE = 12.5
LTCG_EXEMPT = 125000.0
LONG_DAYS = 365          # more than 12 months
SOON_DAYS = 60


def _classify(name: str):
    fund, _ = pr.match_fund(name)
    return pr.asset_class(fund) if fund else "Unclassified"


def build_tax(holdings: list, today: date, slab: float = 30.0) -> dict:
    slab = max(0.0, min(float(slab), 50.0))
    out, soon = [], []
    eq_st = eq_lt = debt_gain = 0.0
    skipped_nodate = skipped_nonav = 0
    for h in holdings:
        if h.get("current_value") is None:
            skipped_nonav += 1
            continue
        gain = float(h["current_value"]) - float(h["invested"])
        cls = _classify(h["scheme_name"])
        bd = None
        if h.get("buy_date"):
            try:
                bd = date.fromisoformat(str(h["buy_date"])[:10])
            except ValueError:
                bd = None
        days = (today - bd).days if bd else None
        term = None
        if days is not None:
            term = "long" if days > LONG_DAYS else "short"
        row = {"scheme_name": h["scheme_name"], "asset_class": cls,
               "invested": round(float(h["invested"]), 2), "current": round(float(h["current_value"]), 2),
               "gain": round(gain, 2), "buy_date": bd.isoformat() if bd else None, "days": days,
               "term": term, "treatment": None}
        if cls == "Equity":
            if term is None:
                skipped_nodate += 1
                row["treatment"] = "Needs a purchase date"
            elif term == "short":
                eq_st += gain
                row["treatment"] = "Short term, 20%"
                left = LONG_DAYS + 1 - days
                if gain > 0 and left <= SOON_DAYS:
                    soon.append({"scheme_name": h["scheme_name"], "gain": row["gain"], "days_left": left})
            else:
                eq_lt += gain
                row["treatment"] = "Long term, 12.5% above the yearly exemption"
        elif cls == "Debt":
            debt_gain += gain
            row["treatment"] = f"Slab rate ({slab:g}%)"
        else:
            row["treatment"] = "Rules depend on the fund; check manually"
        out.append(row)

    stcg_taxable = max(0.0, eq_st)
    ltcg_taxable = max(0.0, eq_lt - LTCG_EXEMPT)
    tax_st = stcg_taxable * STCG_RATE / 100
    tax_lt = ltcg_taxable * LTCG_RATE / 100
    tax_debt = max(0.0, debt_gain) * slab / 100
    out.sort(key=lambda r: -r["gain"])
    soon.sort(key=lambda s: s["days_left"])
    return {
        "holdings": out, "turning_long_term": soon,
        "equity": {"short_term_gain": round(eq_st, 2), "long_term_gain": round(eq_lt, 2),
                   "exemption": LTCG_EXEMPT,
                   "exemption_left": round(max(0.0, LTCG_EXEMPT - max(0.0, eq_lt)), 2),
                   "stcg_tax": round(tax_st, 2), "ltcg_tax": round(tax_lt, 2)},
        "debt": {"gain": round(debt_gain, 2), "tax": round(tax_debt, 2), "slab_pct": slab},
        "total_tax": round(tax_st + tax_lt + tax_debt, 2),
        "skipped": {"no_date": skipped_nodate, "no_nav": skipped_nonav},
        "note": "Estimate if every holding were redeemed today. Excludes surcharge and 4% cess. "
                "Purchase dates are averages, so lots bought on different days are approximate.",
    }


def register_tax_gains(app: FastAPI, supabase_getter, nav_lookup=None, today_fn=date.today):
    @app.get("/crm/{owner_email}/clients/{client_id}/tax-gains")
    def tax_gains(owner_email: str, client_id: int, slab: float = 30.0):
        owner = owner_email.strip().lower()
        allowed = os.environ.get("ORDERS_OWNER_EMAIL", "").strip().lower()
        if allowed and owner != allowed:
            raise HTTPException(403, "Not authorized")
        sb = supabase_getter()
        if sb is None:
            raise HTTPException(500, "Database not configured on the server")
        try:
            cl = (sb.table("crm_clients").select("id").eq("id", client_id)
                  .eq("owner_email", owner).execute().data)
            if not cl:
                raise HTTPException(404, "Client not found")
            rows = (sb.table("crm_manual_holdings").select("*").eq("owner_email", owner)
                    .eq("client_id", client_id).execute().data or [])
            val = holdings_api.valuation(rows, nav_lookup)
            return build_tax(val["holdings"], today_fn(), slab)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, f"Could not build the estimate: {e}")
