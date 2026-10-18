"""
nav_service.py - latest mutual fund NAVs from AMFI's daily file.

AMFI (the association of mutual funds) publishes one text file with the
latest declared NAV of every scheme. We download it once, keep it in memory,
and answer every NAV lookup from it - one download covers every fund, instead
of one web call per fund.

Refresh rules (see _needs_refresh):
  * the first lookup downloads it;
  * after that it is refreshed every 3 hours;
  * and, because funds declare NAVs through the evening, if the snapshot is
    still showing an earlier date than today (IST) it is re-checked hourly;
  * after a failed attempt nothing is retried for 5 minutes, so an AMFI
    outage can't make every request wait on a slow download;
  * a failed refresh keeps the previous snapshot (for up to 3 days) rather
    than losing the data.

If a scheme isn't in the AMFI file, or AMFI can't be reached, callers fall
back to mfapi.in (see holdings_api.fetch_nav). NAV is an end-of-day figure:
there is no intraday NAV.

File layout (semicolon separated, plus section/AMC heading lines we skip):
  Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Net Asset Value;Date
  120465;INF846K01DP8;-;Axis Bluechip Fund - Regular Plan - Growth;62.31;07-Oct-2026
"""
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

import requests
from fastapi import HTTPException

logger = logging.getLogger("nav_service")

_IST = timezone(timedelta(hours=5, minutes=30))

AMFI_URLS = [
    "https://portal.amfiindia.com/spages/NAVAll.txt",
    "https://www.amfiindia.com/spages/NAVAll.txt",
]
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; AdvantageApp/1.0)"}

REFRESH_SECONDS = 3 * 3600        # normal refresh interval
EVENING_RECHECK_SECONDS = 3600    # while the snapshot's date is behind today
RETRY_GAP_SECONDS = 300           # minimum gap between download attempts
MAX_STALE_SECONDS = 3 * 86400     # don't serve a snapshot older than this
MIN_ROWS = 1000                   # fewer parsed rows than this = suspicious file

_lock = threading.Lock()
_snapshot = {
    "data": {},            # scheme_code -> {"nav": float, "date": date, "name": str}
    "fetched_at": 0.0,
    "latest_date": None,   # newest NAV date in the file
    "last_attempt": 0.0,
    "last_error": None,
}


def _find_col(header: list, *needles, default: int):
    for i, h in enumerate(header):
        low = h.lower()
        if all(n in low for n in needles):
            return i
    return default


def parse_navall(text: str) -> dict:
    """
    Parse AMFI's file into {scheme_code: {nav, date, name}}; skips junk lines.

    Column positions are read from the header row ("Scheme Code;...;Net Asset
    Value;Date"), because AMFI has added columns (Plan, Option) before. If no
    header is seen, the older fixed layout (name=3, NAV=4, date=5) is assumed.
    """
    out = {}
    c_name, c_nav, c_date = 3, 4, 5
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(";")]
        if parts and parts[0].lower().startswith("scheme code"):
            c_name = _find_col(parts, "scheme", "name", default=3)
            c_nav = _find_col(parts, "net asset", default=_find_col(parts, "nav", default=4))
            c_date = _find_col(parts, "date", default=c_nav + 1)
            continue
        if len(parts) <= max(c_name, c_nav, c_date) or not parts[0].isdigit():
            continue
        try:
            nav = float(parts[c_nav])
            nav_date = datetime.strptime(parts[c_date], "%d-%b-%Y").date()
        except ValueError:
            continue  # "N.A.", blank, or an unexpected date format
        if nav <= 0:
            continue
        out[parts[0]] = {"nav": nav, "date": nav_date, "name": parts[c_name]}
    return out


_BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "text/plain,text/html;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
    "Referer": "https://www.amfiindia.com/",
}


def _looks_like_navall(text: str) -> bool:
    """True if the text has at least a few NAV rows (not an HTML block page)."""
    return len(parse_navall(text[:200000])) >= 50


def _snippet(text: str) -> str:
    return " ".join(text[:120].split())


def _fetch_one(url: str):
    """Try plain requests, then a browser-impersonating client. Returns (text, note)."""
    note = ""
    try:
        r = requests.get(url, headers=_BROWSER_HEADERS, timeout=40)
        text = r.content.decode("utf-8-sig", errors="replace")
        if r.status_code == 200 and _looks_like_navall(text):
            return text, ""
        note = f"HTTP {r.status_code}, {len(text)} chars, starts: '{_snippet(text)}'"
    except Exception as e:
        note = f"requests failed: {e}"
    try:  # some sites only answer real-browser TLS fingerprints
        from curl_cffi import requests as cffi
        r = cffi.get(url, headers=_BROWSER_HEADERS, timeout=40, impersonate="chrome")
        text = r.content.decode("utf-8-sig", errors="replace")
        if r.status_code == 200 and _looks_like_navall(text):
            return text, ""
        note += f" | browser-mode HTTP {r.status_code}, starts: '{_snippet(text)}'"
    except Exception as e:
        note += f" | browser-mode failed: {e}"
    return None, note


def _download() -> str:
    problems = []
    for url in AMFI_URLS:
        text, note = _fetch_one(url)
        if text:
            return text
        problems.append(f"{url.split('/')[2]}: {note}")
    raise RuntimeError(" || ".join(problems)[:600])


def _needs_refresh(now: float) -> bool:
    if now - _snapshot["last_attempt"] < RETRY_GAP_SECONDS:
        return False
    if not _snapshot["data"]:
        return True
    age = now - _snapshot["fetched_at"]
    if age > REFRESH_SECONDS:
        return True
    today_ist = datetime.fromtimestamp(now, _IST).date()
    latest = _snapshot["latest_date"]
    return bool(latest and latest < today_ist and age > EVENING_RECHECK_SECONDS)


def refresh(force: bool = False, now: float = None) -> bool:
    """Download and parse AMFI's file if due. True if a new snapshot was stored."""
    now = time.time() if now is None else now
    with _lock:
        if not force and not _needs_refresh(now):
            return False
        _snapshot["last_attempt"] = now
        try:
            data = parse_navall(_download())
            if len(data) < MIN_ROWS:
                raise ValueError(f"only {len(data)} schemes could be read from the file")
        except Exception as e:
            _snapshot["last_error"] = str(e)[:700]
            logger.warning("AMFI NAV refresh failed: %s", e)
            return False
        _snapshot.update(
            data=data,
            fetched_at=now,
            latest_date=max(v["date"] for v in data.values()),
            last_error=None,
        )
        logger.info("AMFI NAV snapshot loaded: %d schemes", len(data))
        return True


def get_nav(scheme_code: str, now: float = None):
    """(nav, 'DD-MM-YYYY') from the AMFI snapshot, or None if unavailable."""
    code = str(scheme_code).strip()
    if not code.isdigit():
        return None
    now = time.time() if now is None else now
    refresh(now=now)
    if now - _snapshot["fetched_at"] > MAX_STALE_SECONDS:
        return None
    row = _snapshot["data"].get(code)
    if not row:
        return None
    return row["nav"], row["date"].strftime("%d-%m-%Y")


def scheme_name(scheme_code: str):
    row = _snapshot["data"].get(str(scheme_code).strip())
    return row["name"] if row else None


def status(now: float = None) -> dict:
    now = time.time() if now is None else now
    fetched = _snapshot["fetched_at"]
    latest = _snapshot["latest_date"]
    return {
        "source": "AMFI",
        "schemes": len(_snapshot["data"]),
        "nav_date": latest.isoformat() if latest else None,
        "fetched_at": datetime.fromtimestamp(fetched, timezone.utc).isoformat() if fetched else None,
        "age_minutes": round((now - fetched) / 60, 1) if fetched else None,
        "last_error": _snapshot["last_error"],
    }


def register_nav(app):
    """Attach the NAV routes to the FastAPI app."""

    @app.get("/nav/status")
    def nav_status():
        refresh()  # makes the first call after a restart useful
        return status()

    @app.get("/nav/{scheme_code}")
    def nav_one(scheme_code: str):
        if not scheme_code.isdigit():
            raise HTTPException(400, "scheme_code must be numeric")
        found = get_nav(scheme_code)
        if found:
            return {"scheme_code": scheme_code, "nav": found[0], "date": found[1],
                    "name": scheme_name(scheme_code), "source": "AMFI"}
        import holdings_api  # mfapi.in fallback lives there
        fallback = holdings_api.fetch_nav_mfapi(scheme_code)
        if fallback:
            return {"scheme_code": scheme_code, "nav": fallback[0], "date": fallback[1],
                    "name": None, "source": "mfapi"}
        raise HTTPException(404, "No NAV found for this scheme code")
