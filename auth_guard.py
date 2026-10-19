"""
auth_guard.py - signed login tokens + a guard for every route that carries an email.

Why: login used to return only {email, name}. After that the app put the email in
the URL (/crm/<email>/..., /trading/<email>/..., /expenses/<email>...) and the
server believed it, so anyone who knew an email could read or change that account.

How it works (no changes to api.py routes):
* POST /auth/login, /auth/signup, /auth/signup/verify - on success the JSON reply
  gets an extra "token" field (HMAC-signed, 30 days).
* Every request whose URL has an email as its second segment (/x/<email>/...),
  plus /push/register and /push/test, must carry  Authorization: Bearer <token>
  for THAT email (push: the owner).
* Safe rollout: with AUTH_ENFORCE unset the guard only counts missing/bad tokens
  (see GET /auth/guard-status) and lets everything through. Set AUTH_ENFORCE=1
  on Render once the app sends tokens.

Env vars (Render):
  AUTH_SECRET   long random string (16+ chars). Without it no tokens are issued
                and enforcement cannot be switched on.
  AUTH_ENFORCE  "1" to block requests without a valid token.
"""

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Optional
from urllib.parse import unquote

TOKEN_DAYS = 30
ISSUE_PATHS = {"/auth/login", "/auth/signup", "/auth/signup/verify"}
OWNER_ONLY_PATHS = {"/push/register", "/push/test"}

STATS = {"allowed_with_token": 0, "missing_token": 0, "bad_token": 0,
         "wrong_account": 0, "blocked": 0}


def _secret() -> bytes:
    s = os.environ.get("AUTH_SECRET", "").strip()
    return s.encode() if len(s) >= 16 else b""


def enforcing() -> bool:
    return os.environ.get("AUTH_ENFORCE", "").strip() == "1" and bool(_secret())


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def make_token(email: str, now: Optional[float] = None, days: float = TOKEN_DAYS) -> Optional[str]:
    key = _secret()
    if not key:
        return None
    now = time.time() if now is None else now
    payload = _b64(json.dumps({"e": email.strip().lower(), "x": int(now + days * 86400)},
                              separators=(",", ":")).encode())
    sig = _b64(hmac.new(key, payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{sig}"


def verify_token(token: str, now: Optional[float] = None) -> Optional[str]:
    """Return the email inside a valid, unexpired token, else None."""
    key = _secret()
    if not key or not token or "." not in token:
        return None
    payload, _, sig = token.partition(".")
    good = _b64(hmac.new(key, payload.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, good):
        return None
    try:
        data = json.loads(_unb64(payload))
        if (time.time() if now is None else now) > float(data["x"]):
            return None
        return str(data["e"]).strip().lower()
    except Exception:
        return None


def scoped_email(path: str) -> Optional[str]:
    """'/crm/a@b.com/clients' -> 'a@b.com'. None when the second segment isn't an email."""
    parts = path.split("/")
    if len(parts) >= 3:
        seg = unquote(parts[2]).strip().lower()
        if "@" in seg:
            return seg
    return None


def _bearer(request) -> str:
    h = request.headers.get("authorization", "")
    return h[7:].strip() if h.lower().startswith("bearer ") else ""


def register_guard(app):
    from fastapi.responses import JSONResponse, Response

    @app.get("/auth/guard-status")
    def guard_status():
        return {"secret_set": bool(_secret()), "enforce": enforcing(), **STATS}

    @app.middleware("http")
    async def auth_guard(request, call_next):
        path, method = request.url.path, request.method

        # ---- hand out a token when login / signup succeeds -------------------
        if method == "POST" and path in ISSUE_PATHS:
            resp = await call_next(request)
            if resp.status_code != 200:
                return resp
            body = b"".join([chunk async for chunk in resp.body_iterator])
            try:
                data = json.loads(body)
                token = make_token(data.get("email", "")) if data.get("email") else None
                if token:
                    data["token"] = token
                    return JSONResponse(data, status_code=200)
            except Exception:
                pass
            return Response(content=body, status_code=resp.status_code,
                            media_type=resp.media_type,
                            headers={k: v for k, v in resp.headers.items()
                                     if k.lower() != "content-length"})

        if method == "OPTIONS":
            return await call_next(request)

        # ---- who does this request claim to be? ------------------------------
        owner_only = path in OWNER_ONLY_PATHS
        wanted = scoped_email(path)
        if wanted is None and not owner_only:
            return await call_next(request)          # public route

        if owner_only:
            from push_api import OWNER_EMAIL
            wanted = OWNER_EMAIL

        token = _bearer(request)
        who = verify_token(token) if token else None
        if not token:
            STATS["missing_token"] += 1
            problem = (401, "Please sign in again")
        elif who is None:
            STATS["bad_token"] += 1
            problem = (401, "Your session has expired. Please sign in again")
        elif who != wanted:
            STATS["wrong_account"] += 1
            problem = (403, "This session is not allowed to use that account")
        else:
            STATS["allowed_with_token"] += 1
            problem = None

        if problem and enforcing():
            STATS["blocked"] += 1
            return JSONResponse({"detail": problem[1]}, status_code=problem[0])
        return await call_next(request)
