"""
push_api.py - owner-only push notifications (Firebase Cloud Messaging).

* The app registers its FCM device token at POST /push/register.
* A GitHub Actions cron (.github/workflows/push_tick.yml) calls
  POST /push/tick every 15 minutes with a secret header. Each tick:
    - during NSE hours, re-computes the NIFTY 50 / BANK NIFTY signal and
      pushes only when its category flips (BUY CE / BUY PE / WAIT);
    - once per clock hour, pushes a market + top-headline digest.
  Last-seen state lives in the Supabase `push_state` table, so ticks are
  idempotent however late or early the cron runs.
* Alerts go to the owner account only.

Tables: push_tables.sql.  Env vars on Render: FCM_SERVICE_ACCOUNT_JSON,
PUSH_TICK_SECRET (plus the existing SUPABASE_URL / SUPABASE_KEY).

Everything that lives in api.py (signals, news, Yahoo) is imported lazily
at call time, so this module adds nothing to startup and api.py is unchanged.
"""
import hmac
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Header, HTTPException
from pydantic import BaseModel

logger = logging.getLogger("push_api")

OWNER_EMAIL = os.environ.get("PUSH_OWNER_EMAIL", "parvez.ansari@wealthy.in").strip().lower()
IST = timezone(timedelta(hours=5, minutes=30))
SIGNAL_INDICES = ("NIFTY 50", "BANK NIFTY")
DIGEST_FIRST_HOUR = 9       # IST, inclusive
DIGEST_LAST_HOUR = 21       # IST, inclusive
DIGEST_WEEKENDS = False
LABELS = {"BUY_CE": "Leans BUY CE", "BUY_PE": "Leans BUY PE", "WAIT": "WAIT (no clear edge)"}


class PushRegister(BaseModel):
    email: str
    token: str
    platform: Optional[str] = "android"


class PushTest(BaseModel):
    email: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def state_get(sb, key: str):
    rows = sb.table("push_state").select("value").eq("key", key).execute().data or []
    return rows[0]["value"] if rows else None


def state_set(sb, key: str, value: str):
    sb.table("push_state").upsert(
        {"key": key, "value": value, "updated_at": _now_iso()}, on_conflict="key"
    ).execute()


def save_history(sb, email: str, title: str, body: str, kind: str):
    """Keep a copy of every alert so the app can show a Notifications list.
    Never lets a history problem stop the push itself."""
    try:
        sb.table("push_history").insert(
            {"owner_email": email, "title": title[:200], "body": body[:2000],
             "kind": (kind or "info")[:20], "read": False}).execute()
    except Exception as e:  # table missing, DB hiccup ...
        print(f"push_history not saved: {e}")


def send_to_email(sb, email: str, title: str, body: str, data: Optional[dict] = None,
                  kind: Optional[str] = None) -> dict:
    """Send to every device registered for `email`; prune dead tokens."""
    import push_service

    save_history(sb, email, title, body, kind or (data or {}).get("type") or "info")

    tokens = [r["token"] for r in (
        sb.table("push_tokens").select("token").eq("owner_email", email).execute().data or [])]
    sent = failed = removed = 0
    for t in tokens:
        res = push_service.send(t, title, body, data)
        if res == "ok":
            sent += 1
        elif res == "invalid_token":
            sb.table("push_tokens").delete().eq("token", t).execute()
            removed += 1
        else:
            failed += 1
    return {"devices": len(tokens), "sent": sent, "failed": failed, "removed_dead": removed}


def check_signals(sb, signals_fn=None, sleep=time.sleep) -> dict:
    if signals_fn is None:
        import api
        signals_fn = api.signals
    out = {}
    for i, idx in enumerate(SIGNAL_INDICES):
        if i:
            sleep(3.5)  # Dhan allows one option-chain request per 3 s
        try:
            sig = signals_fn(idx, owner_email=OWNER_EMAIL)
        except Exception as e:
            out[idx] = f"error: {e}"
            continue
        score = sig.get("score", 0)
        cat = "BUY_CE" if score >= 2 else ("BUY_PE" if score <= -2 else "WAIT")
        key = f"signal:{idx}"
        prev = state_get(sb, key)
        if prev == cat:
            out[idx] = f"unchanged ({cat})"
            continue
        state_set(sb, key, cat)
        if prev is None:
            out[idx] = f"initialised ({cat}), no alert on first run"
            continue
        title = f"{idx}: {LABELS[cat]}"
        body = (f"{sig.get('agree')} indicators agree (was {LABELS.get(prev, prev)}). "
                "Open Nifty Signals for the breakdown. Not a guarantee.")
        out[idx] = {"changed": f"{prev}->{cat}",
                    **send_to_email(sb, OWNER_EMAIL, title, body, {"type": "signal", "index": idx})}
    return out


def _inr(x: float) -> str:
    sign = "-" if x < 0 else ""
    x = abs(x)
    if x >= 1e7:
        return f"{sign}Rs {x / 1e7:.2f} Cr"
    if x >= 1e5:
        return f"{sign}Rs {x / 1e5:.2f} L"
    return f"{sign}Rs {x:,.0f}"


NAV_READY_SHARE = 0.8      # wait until this share of holdings has the new date ...
NAV_FALLBACK_HOUR = 23     # ... or until this hour (IST), then send anyway


def check_nav(sb, now=None, force: bool = False, nav_mod=None) -> str:
    """One alert per new AMFI NAV date, summarising the advisor's client holdings."""
    if nav_mod is None:
        import nav_service as nav_mod
    now = now or datetime.now(IST)
    try:
        nav_mod.refresh()
    except Exception as e:
        return f"error: {e}"
    latest = nav_mod.status().get("nav_date")           # 'YYYY-MM-DD' or None
    if not latest:
        return "NAV file not loaded yet"
    last = state_get(sb, "nav:alerted_date")
    if last == latest and not force:
        return f"already alerted for {latest}"

    rows = (sb.table("crm_manual_holdings").select("*")
            .eq("owner_email", OWNER_EMAIL).execute().data or [])
    value = day_move = base = 0.0
    found = fresh = 0
    clients = set()
    movers = []                                          # (pct, fund name)
    for r in rows:
        ch = nav_mod.day_change(r.get("scheme_code"))
        if not ch:
            continue
        found += 1
        clients.add(r.get("client_id"))
        units = float(r.get("units") or 0)
        value += units * ch["nav"]
        if ch["date"].isoformat() == latest:
            fresh += 1
        if ch["prev_nav"]:
            day_move += units * (ch["nav"] - ch["prev_nav"])
            base += units * ch["prev_nav"]
            movers.append(((ch["nav"] - ch["prev_nav"]) / ch["prev_nav"] * 100,
                           r.get("scheme_name") or r.get("scheme_code")))

    if not found:
        state_set(sb, "nav:alerted_date", latest)
        return f"no holdings to report for {latest}"
    if not force and fresh / found < NAV_READY_SHARE and now.hour < NAV_FALLBACK_HOUR:
        return f"waiting: {fresh}/{found} holdings have the {latest} NAV"
    if last is None and not force:
        state_set(sb, "nav:alerted_date", latest)
        return f"initialised ({latest}), no alert on first run"

    d = datetime.fromisoformat(latest).strftime("%d %b %Y")
    lines = [f"{found} holdings across {len(clients)} clients: {_inr(value)}."]
    if base:
        lines[0] = (f"{found} holdings across {len(clients)} clients: {_inr(value)} "
                    f"({'+' if day_move >= 0 else ''}{_inr(day_move)}, {day_move / base * 100:+.2f}% today).")
        movers.sort()
        if len(movers) >= 2:
            lines.append(f"Top: {movers[-1][1]} {movers[-1][0]:+.2f}%")
            lines.append(f"Weakest: {movers[0][1]} {movers[0][0]:+.2f}%")
    else:
        lines.append("Daily change will show from the next update.")
    result = send_to_email(sb, OWNER_EMAIL, f"NAV updated - {d}", "\n".join(lines),
                           {"type": "nav", "date": latest}, kind="nav")
    state_set(sb, "nav:alerted_date", latest)
    return {"date": latest, "holdings": found, **result}


def hourly_digest(sb, now, force: bool = False, quotes_fn=None, news_fn=None):
    hour_key = now.strftime("%Y-%m-%d %H")
    if not force and state_get(sb, "digest:last_hour") == hour_key:
        return "already sent this hour"

    def default_quotes():
        import api
        parts = []
        for name, ticker in (("NIFTY", "^NSEI"), ("BANK NIFTY", "^NSEBANK")):
            try:
                fi = api._myf.Ticker(ticker).fast_info
                last, prev = float(fi["last_price"]), float(fi["previous_close"])
                parts.append(f"{name} {last:,.0f} ({(last - prev) / prev * 100:+.2f}%)")
            except Exception:
                continue
        return parts

    def default_news():
        import api
        return api._m_cached("news", 600, api._m_fetch_news)

    parts = (quotes_fn or default_quotes)()
    headline = None
    try:
        rows = (news_fn or default_news)()
        last_link = state_get(sb, "digest:last_link")
        fresh = [r for r in rows if r.get("link") and r["link"] != last_link]
        high = [r for r in fresh if r.get("impact") == "High"]
        headline = (high or fresh or [None])[0]
    except Exception:
        headline = None

    lines = []
    if parts:
        lines.append(" | ".join(parts))
    if headline:
        lines.append(f"{headline['title']} ({headline['source']})")
    if not lines:
        return "nothing to send"

    title = "Market update " + now.strftime("%I:%M %p").lstrip("0")
    result = send_to_email(sb, OWNER_EMAIL, title, "\n".join(lines), {"type": "digest"})
    state_set(sb, "digest:last_hour", hour_key)
    if headline:
        state_set(sb, "digest:last_link", headline["link"])
    return result


def register_push(app, supabase_getter):
    """Attach /push/register, /push/test and /push/tick."""

    def _db():
        sb = supabase_getter()
        if sb is None:
            raise HTTPException(503, "Database is not configured on the server")
        return sb

    def _owner_only(email: str) -> str:
        email = (email or "").strip().lower()
        if email != OWNER_EMAIL:
            raise HTTPException(403, "Push alerts are only available to the account owner")
        return email

    @app.post("/push/register")
    def push_register(req: PushRegister):
        email = _owner_only(req.email)
        token = req.token.strip()
        if len(token) < 20:
            raise HTTPException(400, "A valid device token is required")
        sb = _db()
        try:
            sb.table("push_tokens").upsert(
                {"token": token, "owner_email": email,
                 "platform": (req.platform or "android")[:20], "updated_at": _now_iso()},
                on_conflict="token",
            ).execute()
        except Exception as e:
            raise HTTPException(500, f"Could not save device token: {e}")
        return {"ok": True}

    @app.post("/push/test")
    def push_test(req: PushTest):
        import push_service

        email = _owner_only(req.email)
        sb = _db()
        try:
            result = send_to_email(
                sb, email, "Advantage test", "Push notifications are working on this phone.",
                kind="test")
        except push_service.PushConfigError as e:
            raise HTTPException(503, str(e))
        if result["devices"] == 0:
            raise HTTPException(404, "No device registered yet - open the app once, allow notifications, then retry.")
        return result

    @app.get("/push/{email}/history")
    def push_history(email: str, limit: int = 50):
        """Latest alerts for the Notifications screen, newest first."""
        email = _owner_only(email)
        limit = max(1, min(int(limit), 200))
        sb = _db()
        try:
            rows = (sb.table("push_history").select("*").eq("owner_email", email)
                    .order("id", desc=True).limit(limit).execute().data or [])
            unread = len(sb.table("push_history").select("id").eq("owner_email", email)
                         .eq("read", False).execute().data or [])
        except Exception as e:
            raise HTTPException(500, f"Could not load notifications: {e}")
        return {"unread": unread,
                "items": [{"id": r["id"], "title": r.get("title", ""), "body": r.get("body", ""),
                           "kind": r.get("kind", "info"), "read": bool(r.get("read")),
                           "created_at": r.get("created_at")} for r in rows]}

    @app.post("/push/{email}/history/read")
    def push_history_read(email: str):
        email = _owner_only(email)
        sb = _db()
        try:
            sb.table("push_history").update({"read": True}).eq(
                "owner_email", email).eq("read", False).execute()
        except Exception as e:
            raise HTTPException(500, f"Could not mark notifications read: {e}")
        return {"ok": True}

    @app.post("/push/tick")
    def push_tick(job: str = "all", force: bool = False,
                  x_tick_secret: str = Header(default="")):
        """Called by the GitHub Actions cron. force=true skips the time windows
        and the once-per-hour guard (for manual testing)."""
        import push_service

        secret = os.environ.get("PUSH_TICK_SECRET", "")
        if not secret:
            raise HTTPException(503, "PUSH_TICK_SECRET is not set on the server")
        if not hmac.compare_digest(x_tick_secret.encode(), secret.encode()):
            raise HTTPException(403, "Bad tick secret")

        sb = _db()
        now = datetime.now(IST)
        minutes = now.hour * 60 + now.minute
        weekday = now.weekday() < 5
        in_market = weekday and (9 * 60 + 15) <= minutes <= (15 * 60 + 30)
        in_digest = ((weekday or DIGEST_WEEKENDS)
                     and DIGEST_FIRST_HOUR <= now.hour <= DIGEST_LAST_HOUR)
        result = {"time_ist": now.strftime("%a %d-%b %H:%M"),
                  "signal": "skipped (outside market hours)",
                  "digest": "skipped (outside digest hours)",
                  "nav": "skipped"}
        try:
            if job in ("all", "signal") and (in_market or force):
                result["signal"] = check_signals(sb)
            if job in ("all", "digest") and (in_digest or force):
                result["digest"] = hourly_digest(sb, now, force=force)
            if job in ("all", "nav"):
                result["nav"] = check_nav(sb, now=now, force=force)
        except push_service.PushConfigError as e:
            raise HTTPException(503, str(e))
        return result
