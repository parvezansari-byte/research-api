"""
Client reminders: upcoming birthdays and clients whose KYC isn't cleared.

  GET  /crm/{owner}/reminders?days=14   birthdays in the next N days + KYC pending
  POST /clients/reminders/tick          daily cron; pushes "birthdays today" once a day
"""
import hmac
import os
from datetime import date, datetime, timedelta, timezone

from fastapi import FastAPI, Header, HTTPException

KYC_OK = {"KYC_VALIDATED", "KYC_REGISTERED"}
IST = timezone(timedelta(hours=5, minutes=30))
STATE_KEY = "client_birthdays_sent"


def _parse(d):
    try:
        return date.fromisoformat(str(d)[:10])
    except Exception:
        return None


def _on_year(dob: date, year: int) -> date:
    try:
        return dob.replace(year=year)
    except ValueError:                      # 29 Feb in a non-leap year
        return date(year, 2, 28)


def build_reminders(clients: list, today: date, days: int = 14) -> dict:
    days = max(1, min(int(days), 90))
    birthdays, kyc = [], []
    for c in clients:
        name = c.get("full_name") or f'Client {c.get("id")}'
        dob = _parse(c.get("date_of_birth"))
        if dob and dob <= today:
            nxt = _on_year(dob, today.year)
            if nxt < today:
                nxt = _on_year(dob, today.year + 1)
            left = (nxt - today).days
            if left <= days:
                birthdays.append({"client_id": c.get("id"), "client_name": name,
                                  "date": nxt.isoformat(), "days_until": left,
                                  "turning": nxt.year - dob.year,
                                  "phone": c.get("phone")})
        status = (c.get("kyc_status") or "PENDING").upper()
        if status not in KYC_OK:
            kyc.append({"client_id": c.get("id"), "client_name": name, "status": status})
    birthdays.sort(key=lambda b: (b["days_until"], b["client_name"]))
    kyc.sort(key=lambda k: k["client_name"])
    return {"birthdays": birthdays, "kyc_pending": kyc,
            "counts": {"birthdays": len(birthdays),
                       "today": sum(1 for b in birthdays if b["days_until"] == 0),
                       "kyc_pending": len(kyc), "clients": len(clients)}}


def register_client_reminders(app: FastAPI, supabase_getter, now_fn=None):
    now_fn = now_fn or (lambda: datetime.now(IST))

    def _db():
        sb = supabase_getter()
        if sb is None:
            raise HTTPException(500, "Database not configured on the server")
        return sb

    def _clients(sb, owner):
        return sb.table("crm_clients").select("*").eq("owner_email", owner).execute().data or []

    @app.get("/crm/{owner_email}/reminders")
    def reminders(owner_email: str, days: int = 14):
        owner = owner_email.strip().lower()
        allowed = os.environ.get("ORDERS_OWNER_EMAIL", "").strip().lower()
        if allowed and owner != allowed:
            raise HTTPException(403, "Not authorized")
        try:
            return build_reminders(_clients(_db(), owner), now_fn().date(), days)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, f"Could not build reminders: {e}")

    @app.post("/clients/reminders/tick")
    def reminders_tick(force: bool = False, x_tick_secret: str = Header(default="")):
        import push_api
        secret = os.environ.get("PUSH_TICK_SECRET", "")
        if not secret:
            raise HTTPException(503, "PUSH_TICK_SECRET is not set on the server")
        if not hmac.compare_digest(x_tick_secret.encode(), secret.encode()):
            raise HTTPException(403, "Bad tick secret")
        sb = _db()
        today = now_fn().date()
        if not force and push_api.state_get(sb, STATE_KEY) == today.isoformat():
            return {"today": 0, "push": "already sent today"}
        owner = push_api.OWNER_EMAIL
        res = build_reminders(_clients(sb, owner), today, 1)
        names = [b for b in res["birthdays"] if b["days_until"] == 0]
        sent = None
        if names:
            n = len(names)
            title = "Client birthday today" if n == 1 else f"{n} client birthdays today"
            body = ", ".join(f'{b["client_name"]} ({b["turning"]})' for b in names[:5])
            if n > 5:
                body += f" and {n - 5} more"
            sent = push_api.send_to_email(sb, owner, title, body + ". A quick wish goes a long way.",
                                          {"type": "reminder"}, kind="reminder")
        push_api.state_set(sb, STATE_KEY, today.isoformat())
        return {"today": len(names), "push": sent}
