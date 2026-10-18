"""
holdings_api.py - manually recorded mutual fund holdings for CRM clients.

Until the NSE portfolio sync exists, the advisor records what each client
holds (fund, units, average buy NAV). This module stores those records,
values them at the latest NAV, and lets the order code check that a
redemption never exceeds the units a client holds.

Routes (advisor-scoped like the other /crm/ routes):
    GET    /crm/{owner}/clients/{client_id}/portfolio   holdings + live valuation
    POST   /crm/{owner}/clients/{client_id}/portfolio   add units (merges into an existing line)
    PUT    /crm/{owner}/portfolio/{holding_id}          correct units / buy NAV / folio
    DELETE /crm/{owner}/portfolio/{holding_id}          remove a line

Latest NAVs come from AMFI's daily file (nav_service.py), with mfapi.in as a fallback, cached
in memory. It does not touch crm_client_holdings, the table reserved for
the future NSE sync.
"""
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Optional

import requests
from fastapi import HTTPException
from pydantic import BaseModel

import nav_service

logger = logging.getLogger("holdings_api")

_NAV_TTL_SECONDS = 1800
_NAV_CACHE: dict = {}  # scheme_code -> (fetched_at, nav, date_string)

MAX_UNITS = 1e9
MAX_NAV = 1e6


def fetch_nav(scheme_code: str):
    """
    Latest NAV for a scheme: (nav, 'DD-MM-YYYY') or None if unavailable.

    Reads AMFI's daily file first (one download covers every scheme); falls
    back to mfapi.in if AMFI can't be reached or doesn't list the scheme.
    """
    code = str(scheme_code).strip()
    if not code.isdigit():
        return None
    return nav_service.get_nav(code) or fetch_nav_mfapi(code)


def fetch_nav_mfapi(scheme_code: str):
    """Latest NAV from mfapi.in (per-scheme call, cached 30 min), or None."""
    code = str(scheme_code).strip()
    if not code.isdigit():
        return None
    now = time.time()
    hit = _NAV_CACHE.get(code)
    if hit and now - hit[0] < _NAV_TTL_SECONDS:
        return hit[1], hit[2]
    try:
        r = requests.get(f"https://api.mfapi.in/mf/{code}/latest", timeout=8)
        if r.status_code != 200:
            return None
        rows = r.json().get("data") or []
        if not rows:
            return None
        nav = float(rows[0]["nav"])
        date = rows[0].get("date")
        _NAV_CACHE[code] = (now, nav, date)
        return nav, date
    except Exception as e:
        logger.warning("NAV fetch failed for %s: %s", code, e)
        return None


def held_units(sb, owner: str, client_id: int, scheme_code: str) -> Optional[float]:
    """Total units the client holds in a scheme across folios; None if no record."""
    res = (sb.table("crm_manual_holdings").select("units")
           .eq("owner_email", owner).eq("client_id", client_id)
           .eq("scheme_code", str(scheme_code)).execute())
    rows = res.data or []
    if not rows:
        return None
    return sum(float(r["units"]) for r in rows)


class HoldingRequest(BaseModel):
    scheme_code: str
    scheme_name: str
    units: float
    avg_nav: float
    folio_number: Optional[str] = None


class HoldingUpdate(BaseModel):
    units: Optional[float] = None
    avg_nav: Optional[float] = None
    folio_number: Optional[str] = None


def _check_positive(value, label: str, ceiling: float) -> float:
    if value is None or value <= 0:
        raise HTTPException(400, f"{label} must be greater than zero")
    if value > ceiling:
        raise HTTPException(400, f"{label} is unrealistically large")
    return float(value)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def valuation(rows: list, nav_lookup=None) -> dict:
    """Attach live NAV, current value and gain to each holding; add totals."""
    nav_lookup = nav_lookup or fetch_nav
    codes = sorted({r["scheme_code"] for r in rows})
    navs = {}
    if codes:
        with ThreadPoolExecutor(max_workers=min(6, len(codes))) as pool:
            for code, result in zip(codes, pool.map(nav_lookup, codes)):
                navs[code] = result

    out = []
    total_invested = total_value = priced_invested = 0.0
    priced = 0
    for r in rows:
        units = float(r["units"])
        avg_nav = float(r["avg_nav"])
        invested = units * avg_nav
        item = {
            **r,
            "units": units,
            "avg_nav": avg_nav,
            "invested": round(invested, 2),
            "current_nav": None,
            "nav_date": None,
            "current_value": None,
            "gain": None,
            "gain_pct": None,
        }
        nav = navs.get(r["scheme_code"])
        total_invested += invested
        if nav:
            value = units * nav[0]
            item.update({
                "current_nav": nav[0],
                "nav_date": nav[1],
                "current_value": round(value, 2),
                "gain": round(value - invested, 2),
                "gain_pct": round((value - invested) / invested * 100, 2) if invested else None,
            })
            total_value += value
            priced_invested += invested
            priced += 1
        out.append(item)

    gain = total_value - priced_invested
    return {
        "holdings": out,
        "totals": {
            "invested": round(total_invested, 2),
            "current_value": round(total_value, 2),
            "gain": round(gain, 2),
            "gain_pct": round(gain / priced_invested * 100, 2) if priced_invested else None,
            "priced": priced,
            "total": len(out),
        },
    }


def register_holdings(app, supabase_getter):
    """Attach the holdings routes (and the NAV routes) to the FastAPI app."""
    nav_service.register_nav(app)

    def _db():
        sb = supabase_getter()
        if sb is None:
            raise HTTPException(500, "Database not configured on the server")
        return sb

    def _require_owner(owner_email: str):
        allowed = os.environ.get("ORDERS_OWNER_EMAIL", "").strip().lower()
        if allowed and owner_email.strip().lower() != allowed:
            raise HTTPException(403, "Not authorized")

    def _owned_client(sb, owner: str, client_id: int):
        res = (sb.table("crm_clients").select("id")
               .eq("id", client_id).eq("owner_email", owner).execute())
        if not res.data:
            raise HTTPException(404, "Client not found")

    def _owned_holding(sb, owner: str, holding_id: int) -> dict:
        res = (sb.table("crm_manual_holdings").select("*")
               .eq("id", holding_id).eq("owner_email", owner).execute())
        if not res.data:
            raise HTTPException(404, "Holding not found")
        return res.data[0]

    @app.get("/crm/{owner_email}/clients/{client_id}/portfolio")
    def get_portfolio(owner_email: str, client_id: int):
        owner = owner_email.strip().lower()
        _require_owner(owner)
        sb = _db()
        try:
            _owned_client(sb, owner, client_id)
            res = (sb.table("crm_manual_holdings").select("*")
                   .eq("owner_email", owner).eq("client_id", client_id)
                   .order("scheme_name", desc=False).execute())
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, f"Database error: {e}")
        return valuation(res.data or [])

    @app.post("/crm/{owner_email}/clients/{client_id}/portfolio")
    def add_holding(owner_email: str, client_id: int, req: HoldingRequest):
        owner = owner_email.strip().lower()
        _require_owner(owner)
        code = req.scheme_code.strip()
        name = req.scheme_name.strip()
        if not code.isdigit() or not name:
            raise HTTPException(400, "Pick a fund first")
        units = _check_positive(req.units, "Units", MAX_UNITS)
        avg_nav = _check_positive(req.avg_nav, "Average NAV", MAX_NAV)
        folio = (req.folio_number or "").strip()
        sb = _db()
        try:
            _owned_client(sb, owner, client_id)
            existing = (sb.table("crm_manual_holdings").select("*")
                        .eq("owner_email", owner).eq("client_id", client_id)
                        .eq("scheme_code", code).eq("folio_number", folio).execute())
            if existing.data:
                row = existing.data[0]
                old_units, old_nav = float(row["units"]), float(row["avg_nav"])
                new_units = old_units + units
                new_avg = (old_units * old_nav + units * avg_nav) / new_units
                res = (sb.table("crm_manual_holdings")
                       .update({"units": new_units, "avg_nav": round(new_avg, 4),
                                "updated_at": _now_iso()})
                       .eq("id", row["id"]).execute())
            else:
                res = sb.table("crm_manual_holdings").insert({
                    "owner_email": owner, "client_id": client_id,
                    "scheme_code": code, "scheme_name": name,
                    "folio_number": folio, "units": units, "avg_nav": avg_nav,
                }).execute()
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, f"Could not save the holding: {e}")
        return res.data[0] if res.data else {"ok": True}

    @app.put("/crm/{owner_email}/portfolio/{holding_id}")
    def update_holding(owner_email: str, holding_id: int, req: HoldingUpdate):
        owner = owner_email.strip().lower()
        _require_owner(owner)
        fields: dict = {}
        if req.units is not None:
            fields["units"] = _check_positive(req.units, "Units", MAX_UNITS)
        if req.avg_nav is not None:
            fields["avg_nav"] = _check_positive(req.avg_nav, "Average NAV", MAX_NAV)
        if req.folio_number is not None:
            fields["folio_number"] = req.folio_number.strip()
        if not fields:
            raise HTTPException(400, "Nothing to change")
        fields["updated_at"] = _now_iso()
        sb = _db()
        try:
            _owned_holding(sb, owner, holding_id)
            res = (sb.table("crm_manual_holdings").update(fields)
                   .eq("id", holding_id).execute())
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, f"Could not update the holding: {e}")
        return res.data[0] if res.data else {"ok": True}

    @app.delete("/crm/{owner_email}/portfolio/{holding_id}")
    def delete_holding(owner_email: str, holding_id: int):
        owner = owner_email.strip().lower()
        _require_owner(owner)
        sb = _db()
        try:
            _owned_holding(sb, owner, holding_id)
            sb.table("crm_manual_holdings").delete().eq("id", holding_id).execute()
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, f"Could not delete the holding: {e}")
        return {"ok": True}
