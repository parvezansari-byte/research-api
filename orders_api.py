"""
orders_api.py - mutual fund order endpoints for the CRM.

Plugged into the main app with two lines at the end of api.py:

    from orders_api import register_orders
    register_orders(app, _supabase)

Routes (all scoped to the advisor's email, like the other /crm/ routes):
    POST /crm/{owner}/clients/{client_id}/orders   place an order
    GET  /crm/{owner}/clients/{client_id}/orders   list a client's orders
    GET  /crm/{owner}/orders/{order_id}            one order + its audit trail
    POST /crm/{owner}/orders/{order_id}/cancel     cancel an open order

Safety rules enforced here, not just in the app:
  * only ORDERS_OWNER_EMAIL may use these routes (required in live mode)
  * the request must carry confirmed=true (the app sets it only after the
    user taps Confirm on the summary screen)
  * a per-tap idempotency key makes retries return the same order instead of
    placing a second one
  * the client must exist under this advisor and have KYC_VALIDATED or
    KYC_REGISTERED status; live mode also needs their NSE client code
  * amount sanity limits (configurable) and field validation
  * every state change is written to crm_order_events
"""
import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException
from pydantic import BaseModel

import nse_service
from nse_service import (
    NseNotReady,
    STATUS_CANCELLED,
    STATUS_CREATED,
    STATUS_FAILED,
    TERMINAL_STATUSES,
    get_nse_client,
)

logger = logging.getLogger("orders_api")

ORDER_TYPES = {"PURCHASE", "SIP", "REDEEM"}
KYC_OK = {"KYC_VALIDATED", "KYC_REGISTERED"}

# Placeholders: real minimums differ per scheme and will come from NSE later.
MIN_PURCHASE = 100.0
MIN_SIP = 100.0
_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


def _max_amount() -> float:
    try:
        return float(os.environ.get("ORDERS_MAX_AMOUNT", "1000000"))
    except ValueError:
        return 1000000.0


class OrderRequest(BaseModel):
    scheme_code: str
    scheme_name: str
    order_type: str                      # PURCHASE | SIP | REDEEM
    amount: Optional[float] = None
    units: Optional[float] = None        # REDEEM by units instead of amount
    sip_day: Optional[int] = None
    sip_installments: Optional[int] = None
    idempotency_key: str
    confirmed: bool = False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_order(req: OrderRequest) -> dict:
    """Return the cleaned order fields, or raise HTTPException(400)."""
    otype = req.order_type.strip().upper()
    if otype not in ORDER_TYPES:
        raise HTTPException(400, "order_type must be PURCHASE, SIP or REDEEM")
    if not req.confirmed:
        raise HTTPException(400, "Order was not confirmed")
    if not _KEY_RE.match(req.idempotency_key or ""):
        raise HTTPException(400, "Invalid idempotency key")
    scheme_code = req.scheme_code.strip()
    scheme_name = req.scheme_name.strip()
    if not scheme_code or not scheme_name:
        raise HTTPException(400, "Pick a fund first")

    cap = _max_amount()
    amount = req.amount
    units = req.units
    sip_day = None
    installments = None

    if otype in ("PURCHASE", "SIP"):
        if amount is None or amount <= 0:
            raise HTTPException(400, "Enter an amount")
        floor = MIN_PURCHASE if otype == "PURCHASE" else MIN_SIP
        if amount < floor:
            raise HTTPException(400, f"Minimum amount is ₹{floor:,.0f}")
        if amount > cap:
            raise HTTPException(
                400, f"Amount is above the ₹{cap:,.0f} safety limit for one order")
        units = None
        if otype == "SIP":
            sip_day = req.sip_day
            installments = req.sip_installments
            if sip_day is None or not 1 <= sip_day <= 28:
                raise HTTPException(400, "SIP day must be between 1 and 28")
            if installments is None or not 1 <= installments <= 360:
                raise HTTPException(400, "SIP installments must be between 1 and 360")
    else:  # REDEEM: exactly one of amount / units
        has_amount = amount is not None and amount > 0
        has_units = units is not None and units > 0
        if has_amount == has_units:
            raise HTTPException(400, "For a redemption give either an amount or units, not both")
        if has_amount and amount > cap:
            raise HTTPException(
                400, f"Amount is above the ₹{cap:,.0f} safety limit for one order")
        if not has_amount:
            amount = None
        if not has_units:
            units = None

    return {
        "scheme_code": scheme_code,
        "scheme_name": scheme_name,
        "order_type": otype,
        "amount": amount,
        "units": units,
        "sip_day": sip_day,
        "sip_installments": installments,
    }


def register_orders(app, supabase_getter):
    """Attach the order routes to the FastAPI app."""

    def _db():
        sb = supabase_getter()
        if sb is None:
            raise HTTPException(500, "Database not configured on the server")
        return sb

    def _require_owner(owner_email: str):
        allowed = os.environ.get("ORDERS_OWNER_EMAIL", "").strip().lower()
        live = os.environ.get("NSE_MODE", "stub").strip().lower() == "live"
        if not allowed:
            if live:
                raise HTTPException(
                    500, "ORDERS_OWNER_EMAIL must be set on the server before live orders")
            return  # stub mode: open, nothing real can happen
        if owner_email.strip().lower() != allowed:
            raise HTTPException(403, "Not authorized to place orders")

    def _event(sb, order_id: int, owner: str, event: str, detail: Optional[dict] = None):
        try:
            sb.table("crm_order_events").insert({
                "order_id": order_id,
                "owner_email": owner,
                "event": event,
                "detail": detail or {},
            }).execute()
        except Exception as e:  # never let audit-trail trouble break an order
            logger.warning("audit event %s for order %s not saved: %s", event, order_id, e)

    def _owned_client(sb, owner: str, client_id: int) -> dict:
        res = (sb.table("crm_clients").select("*")
               .eq("id", client_id).eq("owner_email", owner).execute())
        if not res.data:
            raise HTTPException(404, "Client not found")
        return res.data[0]

    def _get_order(sb, owner: str, order_id: int) -> dict:
        res = (sb.table("crm_orders").select("*")
               .eq("id", order_id).eq("owner_email", owner).execute())
        if not res.data:
            raise HTTPException(404, "Order not found")
        return res.data[0]

    def _update(sb, order_id: int, fields: dict) -> dict:
        fields = dict(fields, updated_at=_now_iso())
        res = sb.table("crm_orders").update(fields).eq("id", order_id).execute()
        return res.data[0] if res.data else fields

    def _refresh(sb, order: dict, nse) -> dict:
        """Pull the latest status for an open order and save any change."""
        if order["status"] in TERMINAL_STATUSES:
            return order
        try:
            latest = nse.status(order)
        except NseNotReady:
            return order
        except Exception as e:
            logger.warning("status check failed for order %s: %s", order["id"], e)
            return order
        new_status = latest.get("status")
        if new_status and new_status != order["status"]:
            fields = {"status": new_status}
            if latest.get("reason"):
                fields["failure_reason"] = latest["reason"]
            before = order["status"]
            order = {**order, **_update(sb, order["id"], fields)}
            _event(sb, order["id"], order["owner_email"], "STATUS_CHANGED",
                   {"from": before, "to": new_status})
        return order

    @app.post("/crm/{owner_email}/clients/{client_id}/orders")
    def place_order(owner_email: str, client_id: int, req: OrderRequest):
        owner = owner_email.strip().lower()
        _require_owner(owner)
        fields = validate_order(req)
        sb = _db()
        nse = get_nse_client()

        try:
            # A retry of the same tap returns the order already placed.
            dup = (sb.table("crm_orders").select("*")
                   .eq("owner_email", owner)
                   .eq("idempotency_key", req.idempotency_key).execute())
            if dup.data:
                return {**dup.data[0], "duplicate": True}

            if not nse.ready:
                raise HTTPException(
                    501,
                    "Live NSE ordering isn't set up yet. Use NSE_MODE=stub for testing.")

            client = _owned_client(sb, owner, client_id)
            if client.get("kyc_status") not in KYC_OK:
                raise HTTPException(
                    409,
                    "This client's KYC isn't validated yet "
                    f"(status: {client.get('kyc_status') or 'PENDING'}). "
                    "Orders can only be placed once KYC is validated or registered.")
            if nse.name == "LIVE" and not client.get("nse_client_code"):
                raise HTTPException(409, "This client has no NSE client code yet")

            row = {
                "owner_email": owner,
                "client_id": client_id,
                **fields,
                "status": STATUS_CREATED,
                "mode": nse.name,
                "idempotency_key": req.idempotency_key,
            }
            created = sb.table("crm_orders").insert(row).execute()
            if not created.data:
                raise HTTPException(502, "Could not save the order")
            order = created.data[0]
            _event(sb, order["id"], owner, "CREATED", {"mode": nse.name, **fields})

            try:
                result = nse.submit(order)
            except Exception as e:
                order = {**order, **_update(sb, order["id"], {
                    "status": STATUS_FAILED, "failure_reason": f"Submit error: {e}"[:300]})}
                _event(sb, order["id"], owner, "SUBMIT_ERROR", {"error": str(e)[:300]})
                raise HTTPException(502, f"The order could not be submitted: {e}")

            update = {"status": result["status"]}
            if result.get("nse_order_ref"):
                update["nse_order_ref"] = result["nse_order_ref"]
            if result.get("reason"):
                update["failure_reason"] = result["reason"]
            order = {**order, **_update(sb, order["id"], update)}
            _event(sb, order["id"], owner, "SUBMITTED", {
                "status": result["status"], "nse_order_ref": result.get("nse_order_ref")})
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, f"Database error: {e}")
        return {**order, "duplicate": False}

    @app.get("/crm/{owner_email}/clients/{client_id}/orders")
    def list_orders(owner_email: str, client_id: int):
        owner = owner_email.strip().lower()
        _require_owner(owner)
        sb = _db()
        nse = get_nse_client()
        try:
            _owned_client(sb, owner, client_id)
            res = (sb.table("crm_orders").select("*")
                   .eq("owner_email", owner).eq("client_id", client_id)
                   .order("created_at", desc=True).execute())
            orders = [_refresh(sb, o, nse) for o in (res.data or [])]
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, f"Database error: {e}")
        return {"orders": orders, "mode": nse.name}

    @app.get("/crm/{owner_email}/orders/{order_id}")
    def get_order(owner_email: str, order_id: int):
        owner = owner_email.strip().lower()
        _require_owner(owner)
        sb = _db()
        nse = get_nse_client()
        try:
            order = _refresh(sb, _get_order(sb, owner, order_id), nse)
            events = (sb.table("crm_order_events").select("*")
                      .eq("order_id", order_id).eq("owner_email", owner)
                      .order("created_at", desc=False).execute())
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, f"Database error: {e}")
        return {"order": order, "events": events.data or []}

    @app.post("/crm/{owner_email}/orders/{order_id}/cancel")
    def cancel_order(owner_email: str, order_id: int):
        owner = owner_email.strip().lower()
        _require_owner(owner)
        sb = _db()
        nse = get_nse_client()
        try:
            order = _refresh(sb, _get_order(sb, owner, order_id), nse)
            if order["status"] in TERMINAL_STATUSES:
                raise HTTPException(
                    409, f"This order is already {order['status'].lower()} and can't be cancelled")
            try:
                result = nse.cancel(order)
            except NseNotReady as e:
                raise HTTPException(501, str(e))
            except Exception as e:
                raise HTTPException(502, f"The cancellation failed: {e}")
            order = {**order, **_update(sb, order_id, {
                "status": result.get("status", STATUS_CANCELLED),
                "failure_reason": result.get("reason")})}
            _event(sb, order_id, owner, "CANCELLED", {})
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, f"Database error: {e}")
        return order
