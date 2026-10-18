"""
push_service.py - sends Firebase Cloud Messaging (FCM) push notifications.

Uses FCM's HTTP v1 API directly with a Google service-account key (no
firebase-admin dependency). The service-account JSON is read from the
FCM_SERVICE_ACCOUNT_JSON environment variable (paste the whole file's
contents as the value in the Render dashboard).

Notifications are sent as FCM "notification" messages, which Android shows
in the system tray by itself when the app is closed or in the background -
no app code needs to be running for them to appear.
"""
import json
import logging
import os

import requests

logger = logging.getLogger("push_service")

_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
_cached = {"creds": None, "project_id": None}


class PushConfigError(Exception):
    """FCM isn't configured (missing / invalid service account)."""


def _load_credentials():
    if _cached["creds"] is not None:
        return _cached["creds"], _cached["project_id"]

    raw = os.environ.get("FCM_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        raise PushConfigError("FCM_SERVICE_ACCOUNT_JSON is not set on the server")
    try:
        info = json.loads(raw)
    except Exception as e:
        raise PushConfigError(f"FCM_SERVICE_ACCOUNT_JSON is not valid JSON: {e}")
    if not info.get("project_id") or not info.get("private_key"):
        raise PushConfigError("FCM_SERVICE_ACCOUNT_JSON is missing project_id / private_key")

    from google.oauth2 import service_account

    creds = service_account.Credentials.from_service_account_info(info, scopes=[_SCOPE])
    _cached["creds"] = creds
    _cached["project_id"] = info["project_id"]
    return creds, info["project_id"]


def _access_token() -> tuple[str, str]:
    from google.auth.transport.requests import Request

    creds, project_id = _load_credentials()
    if not creds.valid:
        creds.refresh(Request())
    return creds.token, project_id


def send(token: str, title: str, body: str, data: dict | None = None) -> str:
    """
    Send one notification to one device token.

    Returns "ok", "invalid_token" (the device uninstalled / token expired -
    caller should delete it), or "error".
    """
    try:
        access_token, project_id = _access_token()
    except PushConfigError:
        raise
    except Exception as e:
        logger.warning("FCM auth failed: %s", e)
        return "error"

    message = {
        "message": {
            "token": token,
            "notification": {"title": title, "body": body},
            "android": {"priority": "HIGH"},
        }
    }
    if data:
        # FCM data values must all be strings.
        message["message"]["data"] = {k: str(v) for k, v in data.items()}

    try:
        r = requests.post(
            f"https://fcm.googleapis.com/v1/projects/{project_id}/messages:send",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8",
            },
            json=message,
            timeout=20,
        )
    except Exception as e:
        logger.warning("FCM request failed: %s", e)
        return "error"

    if r.status_code == 200:
        return "ok"

    # 404 UNREGISTERED / 400 INVALID_ARGUMENT on the token = dead token.
    try:
        status = (r.json().get("error") or {}).get("status", "")
    except Exception:
        status = ""
    logger.warning("FCM send failed %s %s", r.status_code, r.text[:300])
    if r.status_code == 404 or status in ("UNREGISTERED", "NOT_FOUND"):
        return "invalid_token"
    return "error"
