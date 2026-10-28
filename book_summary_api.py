"""
Client book overview: GET /crm/{owner}/book-summary

Adds up every client's recorded holdings (crm_manual_holdings) at the latest
NAV: total value, split by client, by fund and by asset class, plus simple
concentration figures (largest client / fund as a share of the book).
"""
import os

from fastapi import FastAPI, HTTPException

import holdings_api
import portfolio_review_api as pr

MAX_HOLDINGS = 1000
TOP_N = 10


def _pct(part: float, whole: float):
    return round(part / whole * 100, 1) if whole else None


def build_book(rows: list, clients: dict, nav_lookup=None) -> dict:
    rows = rows[:MAX_HOLDINGS]
    val = holdings_api.valuation(rows, nav_lookup)
    by_client, by_fund, by_class = {}, {}, {}
    unpriced = 0
    for h in val["holdings"]:
        if h.get("current_value") is None:
            unpriced += 1
            continue
        cur, inv = float(h["current_value"]), float(h["invested"])
        c = by_client.setdefault(h["client_id"], {"client_id": h["client_id"],
                                                  "client_name": clients.get(h["client_id"]) or f'Client {h["client_id"]}',
                                                  "invested": 0.0, "value": 0.0, "funds": set()})
        c["invested"] += inv; c["value"] += cur; c["funds"].add(h["scheme_code"])
        f = by_fund.setdefault(h["scheme_code"], {"scheme_code": h["scheme_code"], "scheme_name": h["scheme_name"],
                                                  "value": 0.0, "clients": set()})
        f["value"] += cur; f["clients"].add(h["client_id"])
        fund, _ = pr.match_fund(h["scheme_name"])
        ac = pr.asset_class(fund)
        by_class[ac] = by_class.get(ac, 0.0) + cur

    total = sum(c["value"] for c in by_client.values())
    invested = sum(c["invested"] for c in by_client.values())
    clients_out = sorted(({"client_id": c["client_id"], "client_name": c["client_name"],
                           "invested": round(c["invested"], 2), "value": round(c["value"], 2),
                           "gain_pct": _pct(c["value"] - c["invested"], c["invested"]),
                           "funds": len(c["funds"]), "share_pct": _pct(c["value"], total)}
                          for c in by_client.values()), key=lambda x: -x["value"])
    funds_out = sorted(({"scheme_code": f["scheme_code"], "scheme_name": f["scheme_name"],
                         "value": round(f["value"], 2), "clients": len(f["clients"]),
                         "share_pct": _pct(f["value"], total)}
                        for f in by_fund.values()), key=lambda x: -x["value"])
    classes_out = sorted(({"asset_class": k, "value": round(v, 2), "share_pct": _pct(v, total)}
                          for k, v in by_class.items()), key=lambda x: -x["value"])
    return {
        "totals": {"value": round(total, 2), "invested": round(invested, 2),
                   "gain": round(total - invested, 2), "gain_pct": _pct(total - invested, invested),
                   "clients": len(clients_out), "clients_total": len(clients),
                   "funds": len(funds_out), "holdings": len(val["holdings"]), "unpriced": unpriced},
        "concentration": {"top_client_pct": clients_out[0]["share_pct"] if clients_out else None,
                          "top5_clients_pct": _pct(sum(c["value"] for c in clients_out[:5]), total) if clients_out else None,
                          "top_fund_pct": funds_out[0]["share_pct"] if funds_out else None},
        "clients": clients_out[:TOP_N], "funds": funds_out[:TOP_N], "asset_classes": classes_out,
    }


def register_book_summary(app: FastAPI, supabase_getter, nav_lookup=None):
    @app.get("/crm/{owner_email}/book-summary")
    def book_summary(owner_email: str):
        owner = owner_email.strip().lower()
        allowed = os.environ.get("ORDERS_OWNER_EMAIL", "").strip().lower()
        if allowed and owner != allowed:
            raise HTTPException(403, "Not authorized")
        sb = supabase_getter()
        if sb is None:
            raise HTTPException(500, "Database not configured on the server")
        try:
            rows = (sb.table("crm_manual_holdings").select("*").eq("owner_email", owner).execute().data or [])
            cl = (sb.table("crm_clients").select("*").eq("owner_email", owner).execute().data or [])
            return build_book(rows, {c["id"]: c.get("full_name") for c in cl}, nav_lookup)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, f"Could not build the overview: {e}")
