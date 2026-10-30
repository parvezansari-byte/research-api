"""
Look-through: what a client REALLY owns once you open up their funds.

  GET  /crm/{owner}/clients/{client_id}/lookthrough
  POST /portfolio/lookthrough   {"holdings": [{"name": "...", "current": 12345}, ...]}

Uses the disclosed stock holdings snapshot (holdings_data.json). For every fund we can
match, rupee exposure to each stock and sector is worked out and added up across funds.
Also lists pairs of funds that largely hold the same stocks. Funds not in the snapshot
(debt funds, gold, new funds) are reported as "not analysed" - nothing is invented for them.
"""
import difflib
import json
import os
from collections import defaultdict
from typing import List

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import holdings_api
import portfolio_review_api as pr

TOP_STOCKS = 15
TOP_SECTORS = 10
PAIR_MIN_PCT = 30.0
STOCK_FLAG_PCT = 5.0
MAX_FUNDS = 60

_DATA = None
_IDX = None


def _data():
    global _DATA
    if _DATA is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "holdings_data.json")
        with open(path, encoding="utf-8") as f:
            _DATA = json.load(f)
    return _DATA


def _index():
    global _IDX
    if _IDX is None:
        idx = {}
        for f in _data():
            k = pr.name_key(f.get("fund", ""))
            if k and k not in idx:
                idx[k] = f
        _IDX = idx
    return _IDX


def match_holdings_fund(name: str, idx=None):
    """Snapshot record for a fund name, or None. Same strict rules as the portfolio review."""
    idx = idx if idx is not None else _index()
    k = pr.name_key(name)
    if not k:
        return None
    if k in idx:
        return idx[k]
    utoks = set(k.split())
    unums = {t for t in utoks if t.isdigit()}
    best, best_score = None, 0.0
    for key, f in idx.items():
        ctoks = set(key.split())
        if {t for t in ctoks if t.isdigit()} != unums or key.split()[0] != k.split()[0]:
            continue
        if utoks == ctoks:
            score = 0.98
        else:
            sm = difflib.SequenceMatcher(None, k, key)
            score = sm.ratio() if sm.real_quick_ratio() >= 0.78 and sm.quick_ratio() >= 0.78 else 0.0
        if score >= 0.88 and score > best_score:
            best, best_score = f, score
    return best


def _weights(fund: dict) -> dict:
    out = {}
    for h in fund.get("holdings", []):
        if h.get("pct_nav") is None:
            continue
        out[h.get("isin") or h["instrument"]] = h
    return out


def build_lookthrough(items: list, idx=None) -> dict:
    """items: [(fund name, current value in rupees)]."""
    items = [(n, float(v)) for n, v in items if v and float(v) > 0][:MAX_FUNDS]
    total = sum(v for _, v in items)
    analysed, skipped = [], []
    merged = {}
    for name, val in items:
        f = match_holdings_fund(name, idx)
        if not f:
            skipped.append({"name": name, "value": round(val, 0)})
            continue
        key = f["key"]
        if key in merged:                      # same fund held twice (two folios, regular + direct)
            merged[key]["value"] += val
        else:
            merged[key] = {"key": key, "name": name, "fund": f["fund"], "value": val, "rec": f}
    analysed = list(merged.values())

    stocks = defaultdict(lambda: {"name": "", "sector": None, "rupees": 0.0, "funds": set()})
    sectors = defaultdict(float)
    for a in analysed:
        for h in a["rec"].get("holdings", []):
            if h.get("pct_nav") is None:
                continue
            r = a["value"] * h["pct_nav"] / 100
            s = stocks[h.get("isin") or h["instrument"]]
            s["name"] = h["instrument"]; s["sector"] = h.get("sector")
            s["rupees"] += r; s["funds"].add(a["key"])
            sectors[h.get("sector") or "Unknown"] += r

    ana_val = sum(a["value"] for a in analysed)
    stocks_out = sorted(({"name": s["name"], "sector": s["sector"], "value": round(s["rupees"], 0),
                          "pct_of_portfolio": round(s["rupees"] / total * 100, 2) if total else None,
                          "funds": len(s["funds"]),
                          "flag": bool(total and s["rupees"] / total * 100 >= STOCK_FLAG_PCT)}
                         for s in stocks.values()), key=lambda x: -x["value"])
    sectors_out = sorted(({"sector": k, "value": round(v, 0),
                           "pct_of_portfolio": round(v / total * 100, 2) if total else None}
                          for k, v in sectors.items()), key=lambda x: -x["value"])

    pairs = []
    for i in range(len(analysed)):
        wi = _weights(analysed[i]["rec"])
        for j in range(i + 1, len(analysed)):
            wj = _weights(analysed[j]["rec"])
            ov = sum(min(wi[k]["pct_nav"], wj[k]["pct_nav"]) for k in wi.keys() & wj.keys())
            if ov >= PAIR_MIN_PCT:
                pairs.append({"a": analysed[i]["name"], "b": analysed[j]["name"], "overlap_pct": round(ov, 1)})
    pairs.sort(key=lambda p: -p["overlap_pct"])

    return {
        "total_value": round(total, 0),
        "analysed_value": round(ana_val, 0),
        "coverage_pct": round(ana_val / total * 100, 1) if total else 0.0,
        "funds_analysed": len(analysed),
        "not_analysed": sorted(skipped, key=lambda s: -s["value"]),
        "top_stocks": stocks_out[:TOP_STOCKS],
        "stocks_total": len(stocks_out),
        "sectors": sectors_out[:TOP_SECTORS],
        "overlapping_pairs": pairs[:8],
        "note": "Based on each fund's last disclosed holdings, so it is a snapshot, not live. "
                "Percentages are of the whole portfolio, including funds that could not be analysed.",
    }


class _Item(BaseModel):
    name: str
    current: float


class _Req(BaseModel):
    holdings: List[_Item]


def register_lookthrough(app: FastAPI, supabase_getter, nav_lookup=None):
    @app.post("/portfolio/lookthrough")
    def lookthrough_manual(req: _Req):
        if not req.holdings:
            raise HTTPException(400, "Add at least one holding")
        return build_lookthrough([(h.name, h.current) for h in req.holdings])

    @app.get("/crm/{owner_email}/clients/{client_id}/lookthrough")
    def lookthrough_client(owner_email: str, client_id: int):
        owner = owner_email.strip().lower()
        allowed = os.environ.get("ORDERS_OWNER_EMAIL", "").strip().lower()
        if allowed and owner != allowed:
            raise HTTPException(403, "Not authorized")
        sb = supabase_getter()
        if sb is None:
            raise HTTPException(500, "Database not configured on the server")
        try:
            cl = sb.table("crm_clients").select("id").eq("id", client_id).eq("owner_email", owner).execute().data
            if not cl:
                raise HTTPException(404, "Client not found")
            rows = (sb.table("crm_manual_holdings").select("*").eq("owner_email", owner)
                    .eq("client_id", client_id).execute().data or [])
            val = holdings_api.valuation(rows, nav_lookup)
            items = [(h["scheme_name"], h["current_value"]) for h in val["holdings"]
                     if h.get("current_value") is not None]
            return build_lookthrough(items)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, f"Could not build the look-through: {e}")
