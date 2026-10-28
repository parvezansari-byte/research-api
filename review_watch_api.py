"""
Review watch: tells the advisor when a fund a client holds has fallen behind
its category / benchmark.

  GET  /crm/{owner}/review-watch   every client holding that is Behind today
  POST /portfolio/watch/tick       weekly cron; pushes only NEWLY-behind funds

Uses the same verdict as the portfolio review (portfolio_review_api), applied
to the holdings recorded in crm_manual_holdings with their purchase date and
the latest NAV. Holdings without a purchase date are counted but not judged.
"""
import hmac
import json
import os
from datetime import date

from fastapi import FastAPI, Header, HTTPException

import holdings_api
import portfolio_review_api as pr

MAX_HOLDINGS = 300
STATE_KEY = "review_watch"


def build_watch(rows: list, clients: dict, today: date, nav_lookup=None) -> dict:
    """rows: crm_manual_holdings rows. clients: {client_id: full_name}."""
    rows = rows[:MAX_HOLDINGS]
    val = holdings_api.valuation(rows, nav_lookup)
    behind, undated, unpriced, judged = [], 0, 0, 0
    for h in val["holdings"]:
        if h.get("current_value") is None:
            unpriced += 1
            continue
        if not h.get("buy_date"):
            undated += 1
            continue
        try:
            rev = pr.review_holding(pr.Holding(
                name=h["scheme_name"], invested=h["invested"],
                current=h["current_value"], date=str(h["buy_date"])[:10]), today)
        except Exception:
            continue
        judged += 1
        if rev["verdict"] != "Behind":
            continue
        behind.append({
            "key": f'{h["client_id"]}:{h["scheme_code"]}',
            "client_id": h["client_id"],
            "client_name": clients.get(h["client_id"]) or f'Client {h["client_id"]}',
            "scheme_code": h["scheme_code"], "scheme_name": h["scheme_name"],
            "invested": rev["invested"], "current": rev["current"],
            "cagr_pct": rev["cagr_pct"], "abs_pct": rev["abs_pct"],
            "vs_category_pp": rev["vs_category_pp"],
            "vs_benchmark_pp": rev["vs_benchmark_pp"],
            "why": rev["verdict_why"], "category": rev["category"],
        })
    behind.sort(key=lambda b: (b["vs_benchmark_pp"] if b["vs_benchmark_pp"] is not None else 0))
    seen, uniq = set(), []
    for b in behind:
        if b["key"] not in seen:
            seen.add(b["key"]); uniq.append(b)
    return {"behind": uniq,
            "counts": {"holdings": len(val["holdings"]), "judged": judged,
                       "behind": len(uniq), "undated": undated, "unpriced": unpriced}}


def new_items(behind: list, previous_keys: set) -> list:
    return [b for b in behind if b["key"] not in previous_keys]


def push_text(items: list) -> tuple:
    n = len(items)
    clients = sorted({i["client_name"] for i in items})
    title = f"{n} fund{'s' if n != 1 else ''} fell behind benchmark"
    first = items[0]
    body = f'{first["scheme_name"].split(" - ")[0][:40]} ({first["client_name"]})'
    if n > 1:
        body += f" and {n - 1} more across {len(clients)} client{'s' if len(clients) != 1 else ''}"
    return title, body + ". Open Clients > Review watch."


def register_review_watch(app: FastAPI, supabase_getter, today_fn=date.today, nav_lookup=None):
    def _db():
        sb = supabase_getter()
        if sb is None:
            raise HTTPException(500, "Database not configured on the server")
        return sb

    def _compute(sb, owner):
        rows = (sb.table("crm_manual_holdings").select("*")
                .eq("owner_email", owner).execute().data or [])
        cl = (sb.table("crm_clients").select("*").eq("owner_email", owner).execute().data or [])
        names = {c["id"]: c.get("full_name") for c in cl}
        return build_watch(rows, names, today_fn(), nav_lookup)

    @app.get("/crm/{owner_email}/review-watch")
    def review_watch(owner_email: str):
        owner = owner_email.strip().lower()
        allowed = os.environ.get("ORDERS_OWNER_EMAIL", "").strip().lower()
        if allowed and owner != allowed:
            raise HTTPException(403, "Not authorized")
        try:
            return _compute(_db(), owner)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, f"Could not build the watch list: {e}")

    @app.post("/portfolio/watch/tick")
    def watch_tick(force: bool = False, x_tick_secret: str = Header(default="")):
        import push_api
        secret = os.environ.get("PUSH_TICK_SECRET", "")
        if not secret:
            raise HTTPException(503, "PUSH_TICK_SECRET is not set on the server")
        if not hmac.compare_digest(x_tick_secret.encode(), secret.encode()):
            raise HTTPException(403, "Bad tick secret")
        sb = _db()
        owner = push_api.OWNER_EMAIL
        res = _compute(sb, owner)
        try:
            prev = set(json.loads(push_api.state_get(sb, STATE_KEY) or "[]"))
        except Exception:
            prev = set()
        fresh = res["behind"] if force else new_items(res["behind"], prev)
        sent = None
        if fresh:
            title, body = push_text(fresh)
            sent = push_api.send_to_email(sb, owner, title, body,
                                          {"type": "review"}, kind="review")
        push_api.state_set(sb, STATE_KEY, json.dumps(sorted(b["key"] for b in res["behind"])))
        return {"behind": len(res["behind"]), "new": len(fresh), "push": sent}
