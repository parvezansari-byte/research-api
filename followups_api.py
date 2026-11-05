"""
Client follow-ups: a "next review" date and note per client.

  PUT    /crm/{owner}/clients/{client_id}/followup   {"due_date": "YYYY-MM-DD", "note": "..."}
  DELETE /crm/{owner}/clients/{client_id}/followup
  GET    /crm/{owner}/followups?days=14              overdue + due within N days, with client names
  POST   /clients/followups/tick                     daily cron (X-Tick-Secret): push for what is due today / overdue

One follow-up per client (setting it again replaces it). Needs followups_tables.sql.
"""
import hmac
import os
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

IST = timezone(timedelta(hours=5, minutes=30))
MAX_NOTE = 300
STATE_KEY = "followups_sent"


class FollowupReq(BaseModel):
    due_date: str
    note: Optional[str] = ""


def parse_due(s: str) -> date:
    try:
        d = date.fromisoformat(str(s).strip()[:10])
    except ValueError:
        raise HTTPException(400, "Due date must look like 2026-12-31")
    return d


def build_due(rows: list, names: dict, today: date, days: int = 14) -> dict:
    days = max(0, min(int(days), 365))
    horizon = today + timedelta(days=days)
    out = []
    for r in rows:
        try:
            d = date.fromisoformat(str(r["due_date"])[:10])
        except (ValueError, KeyError):
            continue
        if d > horizon:
            continue
        out.append({"client_id": r["client_id"], "client_name": names.get(r["client_id"]) or f'Client {r["client_id"]}',
                    "due_date": d.isoformat(), "note": r.get("note") or "",
                    "days_until": (d - today).days, "overdue": d < today})
    out.sort(key=lambda x: (x["days_until"], x["client_name"]))
    return {"followups": out,
            "counts": {"total": len(out), "overdue": sum(1 for x in out if x["overdue"]),
                       "today": sum(1 for x in out if x["days_until"] == 0)}}


def register_followups(app: FastAPI, supabase_getter, now_fn=None):
    now_fn = now_fn or (lambda: datetime.now(IST))

    def _db():
        sb = supabase_getter()
        if sb is None:
            raise HTTPException(500, "Database not configured on the server")
        return sb

    def _owner(owner_email: str) -> str:
        owner = owner_email.strip().lower()
        allowed = os.environ.get("ORDERS_OWNER_EMAIL", "").strip().lower()
        if allowed and owner != allowed:
            raise HTTPException(403, "Not authorized")
        return owner

    def _client_ok(sb, owner, client_id):
        if not sb.table("crm_clients").select("id").eq("id", client_id).eq("owner_email", owner).execute().data:
            raise HTTPException(404, "Client not found")

    def _load(sb, owner):
        rows = sb.table("crm_followups").select("*").eq("owner_email", owner).execute().data or []
        cl = sb.table("crm_clients").select("*").eq("owner_email", owner).execute().data or []
        names = {c["id"]: c.get("full_name") for c in cl}
        return [r for r in rows if r.get("client_id") in names], names

    @app.put("/crm/{owner_email}/clients/{client_id}/followup")
    def set_followup(owner_email: str, client_id: int, req: FollowupReq):
        owner = _owner(owner_email)
        d = parse_due(req.due_date)
        sb = _db()
        try:
            _client_ok(sb, owner, client_id)
            row = {"id": f"{owner}|{client_id}", "owner_email": owner, "client_id": client_id,
                   "due_date": d.isoformat(), "note": (req.note or "").strip()[:MAX_NOTE]}
            sb.table("crm_followups").upsert(row, on_conflict="id").execute()
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, f"Could not save the follow-up (is the crm_followups table created?): {e}")
        return {"saved": True, "due_date": row["due_date"], "note": row["note"]}

    @app.get("/crm/{owner_email}/clients/{client_id}/followup")
    def get_followup(owner_email: str, client_id: int):
        owner = _owner(owner_email)
        sb = _db()
        try:
            rows = (sb.table("crm_followups").select("*").eq("id", f"{owner}|{client_id}").execute().data or [])
        except Exception as e:
            raise HTTPException(502, f"Could not load the follow-up: {e}")
        if not rows:
            return {"followup": None}
        r = rows[0]
        return {"followup": {"due_date": str(r["due_date"])[:10], "note": r.get("note") or ""}}

    @app.delete("/crm/{owner_email}/clients/{client_id}/followup")
    def clear_followup(owner_email: str, client_id: int):
        owner = _owner(owner_email)
        sb = _db()
        try:
            sb.table("crm_followups").delete().eq("id", f"{owner}|{client_id}").execute()
        except Exception as e:
            raise HTTPException(502, f"Could not clear the follow-up: {e}")
        return {"cleared": True}

    @app.get("/crm/{owner_email}/followups")
    def list_followups(owner_email: str, days: int = 14):
        owner = _owner(owner_email)
        try:
            rows, names = _load(_db(), owner)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, f"Could not load follow-ups (is the crm_followups table created?): {e}")
        return build_due(rows, names, now_fn().date(), days)

    @app.post("/clients/followups/tick")
    def followups_tick(force: bool = False, x_tick_secret: str = Header(default="")):
        import push_api
        secret = os.environ.get("PUSH_TICK_SECRET", "")
        if not secret:
            raise HTTPException(503, "PUSH_TICK_SECRET is not set on the server")
        if not hmac.compare_digest(x_tick_secret.encode(), secret.encode()):
            raise HTTPException(403, "Bad tick secret")
        sb = _db()
        today = now_fn().date()
        if not force and push_api.state_get(sb, STATE_KEY) == today.isoformat():
            return {"due": 0, "push": "already sent today"}
        rows, names = _load(sb, push_api.OWNER_EMAIL)
        res = build_due(rows, names, today, 0)          # overdue + today
        items = res["followups"]
        sent = None
        if items:
            n = len(items)
            title = "1 client follow-up due" if n == 1 else f"{n} client follow-ups due"
            first = items[0]
            body = first["client_name"] + (f': {first["note"]}' if first["note"] else "")
            if n > 1:
                body += f" and {n - 1} more"
            sent = push_api.send_to_email(sb, push_api.OWNER_EMAIL, title, body[:180],
                                          {"type": "followup"}, kind="followup")
        push_api.state_set(sb, STATE_KEY, today.isoformat())
        return {"due": len(items), "push": sent}
