"""
holdings_import_api.py - bulk import of a client's holdings (e.g. from a CSV).

POST /crm/{owner}/clients/{client_id}/portfolio/import
  body: {"rows": [{"scheme_code": "120503", "scheme_name": "...", "units": 10.5,
                   "avg_nav": 55.2, "buy_date": "2023-04-01", "folio_number": ""}, ...],
         "dry_run": true}
A row needs units, avg_nav and either a numeric scheme_code or a scheme_name that
matches AMFI's list. dry_run=true only validates and shows how each row resolves.
Rows for a fund already held (same code + folio) merge exactly like the single add.
Invalid rows are reported and skipped; valid rows are still saved.
"""
import os
from typing import List, Optional

from fastapi import HTTPException
from pydantic import BaseModel

import holdings_api as h
import nav_service

MAX_ROWS = 300


class ImportRow(BaseModel):
    scheme_code: Optional[str] = None
    scheme_name: Optional[str] = None
    units: Optional[float] = None
    avg_nav: Optional[float] = None
    buy_date: Optional[str] = None
    folio_number: Optional[str] = None


class ImportRequest(BaseModel):
    rows: List[ImportRow]
    dry_run: bool = False


def resolve_row(r: ImportRow, name_lookup=None, code_lookup=None) -> dict:
    """Validate one row. Returns a clean dict or raises ValueError(message)."""
    name_lookup = name_lookup or (lambda n: (nav_service.lookup_by_name(n) or {}).get("code"))
    code_lookup = code_lookup or nav_service.scheme_name
    code = (r.scheme_code or "").strip()
    name = (r.scheme_name or "").strip()
    if code.endswith(".0"):
        code = code[:-2]
    if code:
        if not code.isdigit():
            raise ValueError("Scheme code must be a number")
        official = code_lookup(code)
        name = official or name
        if not name:
            raise ValueError(f"Unknown scheme code {code}")
    elif name:
        code = name_lookup(name) or ""
        if not code:
            raise ValueError("Fund name not found - use the AMFI scheme code")
        name = code_lookup(code) or name
    else:
        raise ValueError("Needs a scheme code or fund name")
    try:
        units = h._check_positive(r.units, "Units", h.MAX_UNITS)
        avg = h._check_positive(r.avg_nav, "Average NAV", h.MAX_NAV)
        buy = h.parse_buy_date(r.buy_date) if (r.buy_date or "").strip() else None
    except HTTPException as e:
        raise ValueError(str(e.detail))
    return {"scheme_code": code, "scheme_name": name, "units": units,
            "avg_nav": avg, "buy": buy, "folio": (r.folio_number or "").strip()}


def register_holdings_import(app, supabase_getter):
    def _owner_ok(owner: str):
        allowed = os.environ.get("ORDERS_OWNER_EMAIL", "").strip().lower()
        if allowed and owner != allowed:
            raise HTTPException(403, "Not authorized")

    @app.post("/crm/{owner_email}/clients/{client_id}/portfolio/import")
    def import_holdings(owner_email: str, client_id: int, req: ImportRequest):
        owner = owner_email.strip().lower()
        _owner_ok(owner)
        if not req.rows:
            raise HTTPException(400, "No rows to import")
        if len(req.rows) > MAX_ROWS:
            raise HTTPException(400, f"At most {MAX_ROWS} rows per import")
        sb = supabase_getter()
        if sb is None:
            raise HTTPException(500, "Database not configured on the server")
        try:
            nav_service.refresh()
        except Exception:
            pass
        try:
            c = (sb.table("crm_clients").select("id")
                 .eq("id", client_id).eq("owner_email", owner).execute())
            if not c.data:
                raise HTTPException(404, "Client not found")
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, f"Database error: {e}")

        preview, errors, added, merged = [], [], 0, 0
        for i, r in enumerate(req.rows, start=1):
            try:
                x = resolve_row(r)
            except ValueError as e:
                errors.append({"row": i, "error": str(e)})
                continue
            preview.append({"row": i, "scheme_code": x["scheme_code"],
                            "scheme_name": x["scheme_name"], "units": x["units"],
                            "avg_nav": x["avg_nav"]})
            if req.dry_run:
                continue
            try:
                ex = (sb.table("crm_manual_holdings").select("*")
                      .eq("owner_email", owner).eq("client_id", client_id)
                      .eq("scheme_code", x["scheme_code"])
                      .eq("folio_number", x["folio"]).execute())
                if ex.data:
                    row = ex.data[0]
                    ou, on = float(row["units"]), float(row["avg_nav"])
                    nu = ou + x["units"]
                    upd = {"units": nu,
                           "avg_nav": round((ou * on + x["units"] * x["avg_nav"]) / nu, 4),
                           "updated_at": h._now_iso()}
                    if x["buy"] is not None:
                        nd = h.merged_buy_date(row.get("buy_date"), ou * on, x["buy"],
                                               x["units"] * x["avg_nav"])
                        if nd is not None:
                            upd["buy_date"] = nd.isoformat()
                    sb.table("crm_manual_holdings").update(upd).eq("id", row["id"]).execute()
                    merged += 1
                else:
                    new = {"owner_email": owner, "client_id": client_id,
                           "scheme_code": x["scheme_code"], "scheme_name": x["scheme_name"],
                           "folio_number": x["folio"], "units": x["units"],
                           "avg_nav": x["avg_nav"]}
                    if x["buy"] is not None:
                        new["buy_date"] = x["buy"].isoformat()
                    sb.table("crm_manual_holdings").insert(new).execute()
                    added += 1
            except Exception as e:
                errors.append({"row": i, "error": f"Could not save: {e}"})
        return {"dry_run": req.dry_run, "added": added, "merged": merged,
                "valid": len(preview), "errors": errors, "preview": preview}
