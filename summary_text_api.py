"""
WhatsApp-ready portfolio summary for one client:
  GET /crm/{owner}/clients/{client_id}/summary-text

Returns plain text (Indian digit grouping, no markdown tricks beyond WhatsApp's *bold*)
built from the client's recorded holdings at the latest NAV, plus the client's phone so
the app can open a WhatsApp chat. Nothing is sent from the server.
"""
import os
from datetime import date

from fastapi import FastAPI, HTTPException

import holdings_api

MAX_LINES = 15
DISCLAIMER = "Mutual fund investments are subject to market risks. Read all scheme related documents carefully."


def inr(v: float) -> str:
    neg = v < 0
    s = str(int(round(abs(v))))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:]); head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts) + "," + tail
    return ("-" if neg else "") + "Rs " + s


def short_name(n: str) -> str:
    n = (n or "").split(" - ")[0].strip()
    return n if len(n) <= 42 else n[:41].rstrip() + "…"


def build_summary(client_name: str, val: dict, today: date, advisor: str = "") -> dict:
    hs = [h for h in val["holdings"] if h.get("current_value") is not None]
    hs.sort(key=lambda h: -h["current_value"])
    t = val["totals"]
    inv = sum(h["invested"] for h in hs)
    cur = sum(h["current_value"] for h in hs)
    gain = cur - inv
    gain_pct = (gain / inv * 100) if inv else None
    lines = [f"*Portfolio summary for {client_name}*", f"As on {today.strftime('%d %b %Y')}", ""]
    if not hs:
        lines.append("No holdings with a current NAV are recorded yet.")
    else:
        for h in hs[:MAX_LINES]:
            g = h.get("gain_pct")
            lines.append(f"• {short_name(h['scheme_name'])}")
            lines.append(f"  {inr(h['invested'])} → {inr(h['current_value'])}"
                         + (f" ({'+' if g >= 0 else ''}{g:.1f}%)" if g is not None else ""))
        if len(hs) > MAX_LINES:
            rest = hs[MAX_LINES:]
            lines.append(f"• {len(rest)} more funds worth {inr(sum(h['current_value'] for h in rest))}")
        lines += ["", f"*Total invested:* {inr(inv)}", f"*Current value:* {inr(cur)}"]
        if gain_pct is not None:
            lines.append(f"*Gain:* {inr(gain)} ({'+' if gain_pct >= 0 else ''}{gain_pct:.1f}%)")
        if t["priced"] < t["total"]:
            lines.append(f"({t['total'] - t['priced']} fund(s) left out: no NAV available)")
    lines += ["", DISCLAIMER]
    if advisor:
        lines.append(f"— {advisor}")
    return {"text": "\n".join(lines), "holdings": len(hs)}


def register_summary_text(app: FastAPI, supabase_getter, nav_lookup=None, today_fn=date.today):
    @app.get("/crm/{owner_email}/clients/{client_id}/summary-text")
    def summary_text(owner_email: str, client_id: int):
        owner = owner_email.strip().lower()
        allowed = os.environ.get("ORDERS_OWNER_EMAIL", "").strip().lower()
        if allowed and owner != allowed:
            raise HTTPException(403, "Not authorized")
        sb = supabase_getter()
        if sb is None:
            raise HTTPException(500, "Database not configured on the server")
        try:
            cl = (sb.table("crm_clients").select("*").eq("id", client_id).eq("owner_email", owner).execute().data)
            if not cl:
                raise HTTPException(404, "Client not found")
            rows = (sb.table("crm_manual_holdings").select("*").eq("owner_email", owner)
                    .eq("client_id", client_id).execute().data or [])
            val = holdings_api.valuation(rows, nav_lookup)
            out = build_summary(cl[0].get("full_name") or "Client", val, today_fn(),
                                os.environ.get("ADVISOR_SIGNATURE", "").strip())
            out["phone"] = cl[0].get("phone")
            return out
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, f"Could not build the summary: {e}")
