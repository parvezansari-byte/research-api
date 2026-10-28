"""
dhan_token.py - keeps the Dhan access token alive.

Dhan access tokens last 24 hours. Dhan's RenewToken call swaps a still-valid token for a new
24-hour one, so renewing every few hours means it never lapses. The newest token is saved in the
Supabase `push_state` table (key `dhan_access_token`), because Render's environment variables
cannot be changed from inside the app, and reloaded at start-up.

    GET  /dhan/status    how long the current token has left (never shows the token)
    POST /dhan/renew     renew if less than 20 hours are left (?force=1 renews regardless)
    POST /dhan/token     {"token": "..."}  install a fresh token you generated on web.dhan.co

All three need the X-Tick-Secret header (PUSH_TICK_SECRET), like /push/tick.
A renewal only works while the token is still valid. If it has already expired, generate a new
one on web.dhan.co (My Profile -> Access DhanHQ APIs) and post it to /dhan/token.
"""
import base64
import hmac
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from fastapi import Header, HTTPException
from pydantic import BaseModel

log = logging.getLogger("dhan_token")

IST = timezone(timedelta(hours=5, minutes=30))
STATE_KEY = "dhan_access_token"
RENEW_BELOW_HOURS = 20.0
BASE = "https://api.dhan.co/v2"


class DhanTokenError(Exception):
    pass


# --------------------------------------------------------------------------- token helpers
def _env_token() -> str:
    return (os.environ.get("DHAN_ACCESS_TOKEN") or os.environ.get("DHANHQ_ACCESS_TOKEN") or "").strip()


def _client_id() -> str:
    return (os.environ.get("DHAN_CLIENT_ID") or os.environ.get("DHANHQ_CLIENT_ID") or "").strip()


def jwt_expiry(token: str) -> Optional[datetime]:
    """The 'exp' claim of the token (a JWT), or None if it cannot be read. Not verified."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload))["exp"]
        return datetime.fromtimestamp(float(exp), tz=timezone.utc)
    except Exception:
        return None


def hours_left(token: str, now: Optional[datetime] = None) -> Optional[float]:
    exp = jwt_expiry(token)
    if exp is None:
        return None
    now = now or datetime.now(timezone.utc)
    return round((exp - now).total_seconds() / 3600.0, 2)


def install(token: str) -> None:
    """Make the running server use this token (also drops the cached Dhan client)."""
    os.environ["DHAN_ACCESS_TOKEN"] = token
    try:
        import dhanhq_api
        dhanhq_api._client_singleton = None
    except Exception:
        pass


# --------------------------------------------------------------------------- storage
def load_stored(sb) -> Optional[str]:
    try:
        rows = sb.table("push_state").select("value").eq("key", STATE_KEY).execute().data or []
        return rows[0]["value"] if rows and rows[0].get("value") else None
    except Exception as e:
        log.warning("could not read stored Dhan token: %s", e)
        return None


def save_stored(sb, token: str) -> None:
    sb.table("push_state").upsert(
        {"key": STATE_KEY, "value": token, "updated_at": datetime.now(timezone.utc).isoformat()},
        on_conflict="key").execute()


def best_token(sb, now: Optional[datetime] = None) -> tuple:
    """(token, source) - whichever of the saved and the environment token expires later."""
    env, stored = _env_token(), (load_stored(sb) if sb else None)
    cands = [(t, s) for t, s in ((stored, "saved"), (env, "environment")) if t]
    if not cands:
        return "", "none"
    cands.sort(key=lambda c: (jwt_expiry(c[0]) or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
    return cands[0]


# --------------------------------------------------------------------------- Dhan calls
def _http_get(url, headers):
    import requests
    return requests.get(url, headers=headers, timeout=20)


def _http_post(url, headers):
    import requests
    return requests.post(url, headers=headers, timeout=20)


def renew_remote(client_id: str, token: str, http_get: Optional[Callable] = None,
                 http_post: Optional[Callable] = None) -> str:
    http_get = http_get or _http_get
    http_post = http_post or _http_post
    hdrs = {"access-token": token, "dhanClientId": client_id}
    try:
        r = http_get(BASE + "/RenewToken", hdrs)
        if r.status_code in (404, 405):           # Dhan's docs do not say GET or POST; try the other
            r = http_post(BASE + "/RenewToken", hdrs)
    except Exception as e:
        raise DhanTokenError(f"Could not reach Dhan: {e.__class__.__name__}")
    if r.status_code != 200:
        raise DhanTokenError(f"Dhan answered {r.status_code} to the renewal "
                             "(an expired token cannot be renewed - generate a new one on web.dhan.co)")
    new = None
    try:
        body = r.json()
        if isinstance(body, dict):
            data = body.get("data") if isinstance(body.get("data"), dict) else body
            new = data.get("accessToken") or data.get("access_token") or data.get("token")
        elif isinstance(body, str):
            new = body
    except ValueError:
        text = (r.text or "").strip().strip('"')
        new = text if text.count(".") == 2 else None
    if not new or jwt_expiry(new) is None:
        raise DhanTokenError("Dhan's renewal reply did not contain a readable token")
    return new


def verify(client_id: str, token: str, http_get: Optional[Callable] = None) -> bool:
    http_get = http_get or _http_get
    try:
        r = http_get(BASE + "/profile", {"access-token": token, "dhanClientId": client_id})
        return r.status_code == 200
    except Exception:
        return False


# --------------------------------------------------------------------------- the service
def status(sb, now: Optional[datetime] = None) -> dict:
    now = now or datetime.now(timezone.utc)
    token, source = best_token(sb, now)
    left = hours_left(token, now) if token else None
    exp = jwt_expiry(token) if token else None
    return {"client_id_set": bool(_client_id()), "token_present": bool(token), "source": source,
            "hours_left": left, "expired": (left is not None and left <= 0),
            "expires_at": exp.astimezone(IST).strftime("%d-%b-%Y %H:%M IST") if exp else None}


def renew(sb, force: bool = False, http_get: Optional[Callable] = None, now: Optional[datetime] = None) -> dict:
    now = now or datetime.now(timezone.utc)
    cid = _client_id()
    if not cid:
        raise DhanTokenError("DHAN_CLIENT_ID is not set on the server")
    token, source = best_token(sb, now)
    if not token:
        raise DhanTokenError("No Dhan token is set. Post one to /dhan/token")
    left = hours_left(token, now)
    if left is not None and left <= 0:
        raise DhanTokenError("The Dhan token has expired. Generate a new one on web.dhan.co and post it to /dhan/token")
    if not force and left is not None and left >= RENEW_BELOW_HOURS:
        return {"renewed": False, "reason": f"{left} hours left, no need yet", **status(sb, now)}
    new = renew_remote(cid, token, http_get)
    install(new)                       # use it right away, even if saving fails
    try:
        save_stored(sb, new)
    except Exception as e:
        log.error("renewed but could not save the Dhan token: %s", e)
        return {"renewed": True, "saved": False,
                "warning": "Renewed, but could not save it; a restart would fall back to the old token",
                **status(sb, now)}
    return {"renewed": True, "saved": True, **status(sb, now)}


def set_token(sb, token: str, http_get: Optional[Callable] = None, now: Optional[datetime] = None) -> dict:
    token = (token or "").strip()
    now = now or datetime.now(timezone.utc)
    left = hours_left(token, now)
    if left is None:
        raise DhanTokenError("That does not look like a Dhan access token")
    if left <= 0:
        raise DhanTokenError("That token has already expired")
    if not verify(_client_id(), token, http_get):
        raise DhanTokenError("Dhan did not accept that token (wrong client id, or it was replaced)")
    install(token)
    try:
        save_stored(sb, token)
    except Exception as e:
        raise DhanTokenError(f"Token works but could not be saved: {e}")
    return {"saved": True, **status(sb, now)}


def load_on_startup(sb) -> None:
    """Use the saved token if it outlives the one in the environment."""
    try:
        token, source = best_token(sb)
        if token and source == "saved":
            install(token)
            log.info("Using the saved Dhan token")
    except Exception as e:
        log.warning("Dhan token start-up load skipped: %s", e)


# --------------------------------------------------------------------------- routes
class TokenBody(BaseModel):
    token: str


def register_dhan_token(app, supabase_getter):
    def _sb():
        try:
            return supabase_getter()
        except Exception as e:
            raise HTTPException(503, f"Database unavailable: {e}")

    def _auth(secret_header: str):
        secret = os.environ.get("PUSH_TICK_SECRET", "")
        if not secret:
            raise HTTPException(503, "PUSH_TICK_SECRET is not set on the server")
        if not hmac.compare_digest((secret_header or "").encode(), secret.encode()):
            raise HTTPException(403, "Bad tick secret")

    try:
        load_on_startup(supabase_getter())
    except Exception:
        pass

    @app.get("/dhan/status")
    def dhan_status(x_tick_secret: str = Header(default="")):
        _auth(x_tick_secret)
        return status(_sb())

    @app.post("/dhan/renew")
    def dhan_renew(force: int = 0, x_tick_secret: str = Header(default="")):
        _auth(x_tick_secret)
        try:
            return renew(_sb(), force=bool(force))
        except DhanTokenError as e:
            raise HTTPException(409, str(e))

    @app.post("/dhan/token")
    def dhan_set_token(body: TokenBody, x_tick_secret: str = Header(default="")):
        _auth(x_tick_secret)
        try:
            return set_token(_sb(), body.token)
        except DhanTokenError as e:
            raise HTTPException(400, str(e))
