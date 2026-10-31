"""
account_delete_api.py - delete an account and its data (Google Play requirement).

POST /account/delete        {"email": "...", "password": "..."}   -> deletes everything
GET  /account/delete-page   a plain web form for the same thing (the URL Play asks for)

The password is the proof of ownership (same bcrypt check as login), so the route is public
like /auth/login. Wrong passwords are rate limited per email.

Deleted: the login, OTP codes, expenses, loans, holdings, categories, pending payments, alert
settings, push tokens/history, and (for advisors) clients, their holdings, interactions, AUM
history and follow-ups.
Kept: order records and their event log (crm_orders, crm_order_events) - transaction records
may have to be retained by law. They stay private to the server.
"""
import html
import os
import time
from typing import Dict, List

from fastapi import HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

# (table, column holding the account's email)
USER_TABLES = [
    ("expenses", "user_email"), ("loans", "user_email"), ("holdings", "user_email"),
    ("user_categories", "user_email"), ("pending_payments", "user_email"),
    ("alert_settings", "user_email"), ("otp_codes", "email"),
]
OWNER_TABLES = [
    ("push_tokens", "owner_email"), ("push_history", "owner_email"),
    ("crm_manual_holdings", "owner_email"), ("crm_client_interactions", "owner_email"),
    ("aum_history", "owner_email"), ("followups", "owner_email"),
    ("crm_clients", "owner_email"),
]
MAX_FAILS, WINDOW = 5, 900
_fails: Dict[str, List[float]] = {}


class DeleteRequest(BaseModel):
    email: str
    password: str


def _limited(email: str, now: float) -> bool:
    recent = [t for t in _fails.get(email, []) if now - t < WINDOW]
    _fails[email] = recent
    return len(recent) >= MAX_FAILS


def password_ok(stored: str, password: str) -> bool:
    import bcrypt
    try:
        return bcrypt.checkpw(password.encode(), (stored or "").encode())
    except (ValueError, TypeError):
        return False


def delete_account(sb, email: str) -> dict:
    """Delete all rows for the account. Returns {"deleted": [...], "skipped": [...]}."""
    done, skipped = [], []
    for table, col in USER_TABLES + OWNER_TABLES:
        try:
            sb.table(table).delete().eq(col, email).execute()
            done.append(table)
        except Exception as e:  # table may not exist on this deployment
            skipped.append(f"{table}: {e}")
    # the login itself goes last, so a failure above can be retried with the same password
    sb.table("users").delete().eq("email", email).execute()
    done.append("users")
    return {"deleted": done, "skipped": skipped}


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Delete your Advantage account</title>
<style>body{font-family:system-ui,sans-serif;max-width:480px;margin:40px auto;padding:0 16px;color:#222}
input,button{width:100%;padding:12px;margin:6px 0;font-size:16px;box-sizing:border-box}
button{background:#b3261e;color:#fff;border:0;border-radius:6px}#m{margin-top:12px}</style></head><body>
<h2>Delete your Advantage account</h2>
<p>Enter the email and password you use in the Advantage app. Your account and the data linked to
it (expenses, loans, holdings, alerts, and for advisors the client book) will be permanently deleted.
Order and transaction records may be kept where the law requires it.</p>
<input id="e" type="email" placeholder="Email" autocomplete="username">
<input id="p" type="password" placeholder="Password" autocomplete="current-password">
<button onclick="go()">Permanently delete my account</button><div id="m"></div>
<script>
async function go(){
  const m=document.getElementById('m');
  if(!confirm('This cannot be undone. Delete the account?'))return;
  m.textContent='Working...';
  try{
    const r=await fetch('/account/delete',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({email:document.getElementById('e').value,password:document.getElementById('p').value})});
    const j=await r.json().catch(()=>({}));
    m.textContent=r.ok?'Your account has been deleted.':(j.detail||'Request failed ('+r.status+')');
  }catch(x){m.textContent='Could not reach the server. Try again in a minute.';}
}
</script></body></html>"""


def register_account_delete(app, supabase_getter):
    @app.get("/account/delete-page", response_class=HTMLResponse)
    def delete_page():
        return HTMLResponse(PAGE)

    @app.post("/account/delete")
    def delete_endpoint(req: DeleteRequest):
        email = req.email.strip().lower()
        now = time.time()
        if _limited(email, now):
            raise HTTPException(429, "Too many attempts. Try again in 15 minutes.")
        sb = supabase_getter()
        if sb is None:
            raise HTTPException(500, "Database not configured on the server")
        try:
            res = sb.table("users").select("*").eq("email", email).execute()
        except Exception as e:
            raise HTTPException(502, f"Database error: {e}")
        rec = res.data[0] if res.data else None
        if not rec or not password_ok(rec.get("password_hash", ""), req.password):
            _fails.setdefault(email, []).append(now)
            raise HTTPException(401, "Email or password doesn't match")
        try:
            out = delete_account(sb, email)
        except Exception as e:
            raise HTTPException(502, f"Could not finish deleting: {e}")
        _fails.pop(email, None)
        return {"ok": True, "deleted": out["deleted"], "skipped": out["skipped"]}
