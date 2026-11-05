"""
AUM history: a daily snapshot of the whole client book, and the trend.

  POST /crm/aum/snapshot             daily cron (X-Tick-Secret); stores today's totals
  GET  /crm/{owner}/aum-history?days=180   the saved series + what changed over the period

Needs the table in aum_history_tables.sql. One row per owner per day; running the snapshot
twice in a day just overwrites that day. The value change over a period includes money added
or withdrawn, so the response also splits it into new money and market movement.
"""
import hmac
import os
from datetime import date, datetime, timedelta, timezone

from fastapi import FastAPI, Header, HTTPException

import book_summary_api as bk

IST = timezone(timedelta(hours=5, minutes=30))
MAX_DAYS = 800


def snapshot_owner(sb, owner: str, day: date, nav_lookup=None) -> dict:
    rows = sb.table("crm_manual_holdings").select("*").eq("owner_email", owner).execute().data or []
    cl = sb.table("crm_clients").select("*").eq("owner_email", owner).execute().data or []
    book = bk.build_book(rows, {c["id"]: c.get("full_name") for c in cl}, nav_lookup)
    t = book["totals"]
    if not t["value"] or t["unpriced"] == t["holdings"]:
        return {"saved": False, "reason": "No priced holdings to record"}
    row = {"id": f"{owner}|{day.isoformat()}", "owner_email": owner, "day": day.isoformat(),
           "value": t["value"], "invested": t["invested"], "clients": t["clients"],
           "holdings": t["holdings"], "unpriced": t["unpriced"]}
    sb.table("crm_aum_history").upsert(row, on_conflict="id").execute()
    return {"saved": True, "day": row["day"], "value": row["value"], "invested": row["invested"]}


def build_series(rows: list, days: int, today: date) -> dict:
    days = max(1, min(int(days), MAX_DAYS))
    cutoff = (today - timedelta(days=days)).isoformat()
    pts = sorted(({"day": str(r["day"])[:10], "value": float(r["value"]), "invested": float(r["invested"]),
                   "clients": r.get("clients")} for r in rows if str(r["day"])[:10] >= cutoff),
                 key=lambda p: p["day"])
    out = {"points": pts, "days": days, "change": None}
    if len(pts) >= 2:
        a, b = pts[0], pts[-1]
        dv, di = b["value"] - a["value"], b["invested"] - a["invested"]
        out["change"] = {
            "from": a["day"], "to": b["day"],
            "value_change": round(dv, 2),
            "value_change_pct": round(dv / a["value"] * 100, 2) if a["value"] else None,
            "new_money": round(di, 2),
            "market_movement": round(dv - di, 2),
        }
    return out


def register_aum_history(app: FastAPI, supabase_getter, nav_lookup=None, now_fn=None):
    now_fn = now_fn or (lambda: datetime.now(IST))

    def _db():
        sb = supabase_getter()
        if sb is None:
            raise HTTPException(500, "Database not configured on the server")
        return sb

    @app.post("/crm/aum/snapshot")
    def aum_snapshot(x_tick_secret: str = Header(default="")):
        import push_api
        secret = os.environ.get("PUSH_TICK_SECRET", "")
        if not secret:
            raise HTTPException(503, "PUSH_TICK_SECRET is not set on the server")
        if not hmac.compare_digest(x_tick_secret.encode(), secret.encode()):
            raise HTTPException(403, "Bad tick secret")
        try:
            return snapshot_owner(_db(), push_api.OWNER_EMAIL, now_fn().date(), nav_lookup)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, f"Could not save the snapshot: {e}")

    @app.get("/crm/{owner_email}/aum-history")
    def aum_history(owner_email: str, days: int = 180):
        owner = owner_email.strip().lower()
        allowed = os.environ.get("ORDERS_OWNER_EMAIL", "").strip().lower()
        if allowed and owner != allowed:
            raise HTTPException(403, "Not authorized")
        try:
            rows = (_db().table("crm_aum_history").select("*").eq("owner_email", owner)
                    .order("day", desc=True).limit(MAX_DAYS).execute().data or [])
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, f"Could not load the history (is the crm_aum_history table created?): {e}")
        return build_series(rows, days, now_fn().date())
