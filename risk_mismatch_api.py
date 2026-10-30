"""
Risk mismatch: clients whose actual equity share is far from their saved risk profile.

  GET /crm/{owner}/risk-mismatch?threshold=15

For each client with a risk profile (CONSERVATIVE / MODERATE / AGGRESSIVE) and priced equity,
hybrid or debt funds, the actual mix is compared with the model mix for that profile (10 year
horizon, same model the rebalance screen uses). A client is flagged when the equity share is
`threshold` percentage points or more away from the model. Gold, fund-of-funds and unmatched
funds are left out of the percentages, as in the rebalance check.
"""
import os

from fastapi import FastAPI, HTTPException

import allocation_api as al
import holdings_api
import portfolio_review_api as pr

PROFILE_RISK = {"CONSERVATIVE": "conservative", "MODERATE": "balanced", "AGGRESSIVE": "aggressive"}
DEFAULT_THRESHOLD = 15.0
MAX_HOLDINGS = 1000


def class_of_fund(name: str):
    f, _ = pr.match_fund(name)
    return pr.CLASS_OF.get(pr.asset_class(f))


def build_mismatch(rows: list, clients: list, nav_lookup=None, threshold: float = DEFAULT_THRESHOLD) -> dict:
    threshold = max(1.0, min(float(threshold), 60.0))
    val = holdings_api.valuation(rows[:MAX_HOLDINGS], nav_lookup)
    per = {}
    for h in val["holdings"]:
        if h.get("current_value") is None:
            continue
        c = class_of_fund(h["scheme_name"])
        if not c:
            continue
        m = per.setdefault(h["client_id"], {k: 0.0 for k in al.CLASSES})
        m[c] += float(h["current_value"])
    flagged, ok_count, no_profile, no_holdings = [], 0, 0, 0
    for cl in clients:
        prof = (cl.get("risk_profile") or "").upper()
        risk = PROFILE_RISK.get(prof)
        if not risk:
            no_profile += 1
            continue
        m = per.get(cl["id"])
        tot = sum(m.values()) if m else 0.0
        if tot <= 0:
            no_holdings += 1
            continue
        actual = {k: round(100.0 * m[k] / tot, 1) for k in al.CLASSES}
        target = {k: float(v) for k, v in al.auto_split(risk, 10).items()}
        gap = actual["equity"] - target["equity"]
        if abs(gap) >= threshold:
            flagged.append({"client_id": cl["id"], "client_name": cl.get("full_name") or f'Client {cl["id"]}',
                            "profile": prof, "actual": actual, "target": target,
                            "equity_gap_pp": round(gap, 1), "value": round(tot, 0),
                            "direction": "Too aggressive" if gap > 0 else "Too conservative"})
        else:
            ok_count += 1
    flagged.sort(key=lambda x: -abs(x["equity_gap_pp"]))
    return {"flagged": flagged, "threshold_pp": threshold,
            "counts": {"flagged": len(flagged), "in_line": ok_count,
                       "no_risk_profile": no_profile, "no_holdings": no_holdings},
            "note": "Compared with a model mix for a 10 year horizon. A client may have good reasons to differ; "
                    "treat this as a prompt for a conversation."}


def register_risk_mismatch(app: FastAPI, supabase_getter, nav_lookup=None):
    @app.get("/crm/{owner_email}/risk-mismatch")
    def risk_mismatch(owner_email: str, threshold: float = DEFAULT_THRESHOLD):
        owner = owner_email.strip().lower()
        allowed = os.environ.get("ORDERS_OWNER_EMAIL", "").strip().lower()
        if allowed and owner != allowed:
            raise HTTPException(403, "Not authorized")
        sb = supabase_getter()
        if sb is None:
            raise HTTPException(500, "Database not configured on the server")
        try:
            rows = sb.table("crm_manual_holdings").select("*").eq("owner_email", owner).execute().data or []
            cl = sb.table("crm_clients").select("*").eq("owner_email", owner).execute().data or []
            return build_mismatch(rows, cl, nav_lookup, threshold)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, f"Could not build the check: {e}")
