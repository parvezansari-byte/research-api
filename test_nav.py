"""
Tests for nav_service.py (AMFI daily NAV feed). No network: the download is faked.
Run:  python test_nav.py
"""
import sys
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

import holdings_api
import nav_service as ns

IST = timezone(timedelta(hours=5, minutes=30))

SAMPLE = """Open Ended Schemes(Equity Scheme - Large Cap Fund)

Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Net Asset Value;Date

Axis Mutual Fund

120465;INF846K01DP8;-;Axis Bluechip Fund - Regular Plan - Growth;62.31;07-Oct-2026
120466;INF846K01DQ6;-;Axis Bluechip Fund - Direct Plan - Growth;70.5;07-Oct-2026

Open Ended Schemes(Debt Scheme - Liquid Fund)

Some Mutual Fund

130001;INF000000001;-;Some Liquid Fund - Regular Plan;N.A.;07-Oct-2026
130002;INF000000002;-;Some Liquid Fund - Direct Plan;0;07-Oct-2026
130003;INF000000003;-;Some Liquid Fund - Odd Date;12.5;2026/10/07
130004;INF000000004;-;Older NAV Fund;100.1234;03-Oct-2026
abc;INF000000005;-;Not a scheme code;5;07-Oct-2026
short;line
"""

failures = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if not cond else ""))
    if not cond:
        failures.append(name)


def reset():
    ns._snapshot.update(data={}, fetched_at=0.0, latest_date=None, last_attempt=0.0, last_error=None)


# ---- parsing
p = ns.parse_navall(SAMPLE)
check("parses only valid scheme lines", sorted(p) == ["120465", "120466", "130004"], str(sorted(p)))
check("NAV and date parsed", p["120465"]["nav"] == 62.31 and str(p["120465"]["date"]) == "2026-10-07")
check("keeps 4 decimal NAVs", p["130004"]["nav"] == 100.1234)
check("scheme name kept", p["120466"]["name"].startswith("Axis Bluechip Fund - Direct"))
check("empty text parses to nothing", ns.parse_navall("") == {})

# ---- refresh behaviour with a faked download
ns.MIN_ROWS = 2
calls = {"n": 0, "text": SAMPLE, "fail": False}


def fake_download():
    calls["n"] += 1
    if calls["fail"]:
        raise RuntimeError("AMFI down")
    return calls["text"]


ns._download = fake_download
noon = datetime(2026, 10, 7, 12, 0, tzinfo=IST).timestamp()   # same day as the data

reset()
check("first lookup downloads and answers", ns.get_nav("120465", now=noon) == (62.31, "07-10-2026") and calls["n"] == 1)
check("date shown as DD-MM-YYYY", ns.get_nav("130004", now=noon)[1] == "03-10-2026")
check("second lookup uses the snapshot", ns.get_nav("120466", now=noon + 60) == (70.5, "07-10-2026") and calls["n"] == 1)
check("unknown scheme -> None (caller falls back)", ns.get_nav("999999", now=noon + 60) is None)
check("non-numeric code -> None, no download", ns.get_nav("../x", now=noon) is None and calls["n"] == 1)
check("no refresh within 3h when date is current", ns.get_nav("120465", now=noon + 2 * 3600) is not None and calls["n"] == 1)
check("refresh after 3h", (ns.get_nav("120465", now=noon + 3 * 3600 + 10), calls["n"])[1] == 2)

# evening re-check: snapshot dated 07-Oct, now it's 08-Oct and an hour has passed
reset(); calls["n"] = 0
ns.get_nav("120465", now=noon)
next_morning = datetime(2026, 10, 8, 9, 0, tzinfo=IST).timestamp()
check("stale date triggers hourly re-check", (ns.get_nav("120465", now=next_morning), calls["n"])[1] == 2)
check("...but not again within the hour", (ns.get_nav("120465", now=next_morning + 600), calls["n"])[1] == 2)
calls["text"] = SAMPLE.replace("07-Oct-2026", "08-Oct-2026")
ns.get_nav("120465", now=next_morning + 4000)
check("new day's NAVs picked up", ns.get_nav("120465", now=next_morning + 4010) == (62.31, "08-10-2026"))
calls["text"] = SAMPLE

# ---- failures
reset(); calls["n"] = 0; calls["fail"] = True
check("download failure with no snapshot -> None", ns.get_nav("120465", now=noon) is None)
check("error recorded", "AMFI down" in (ns.status(now=noon)["last_error"] or ""))
check("no hammering after a failure", (ns.get_nav("120465", now=noon + 60), calls["n"])[1] == 1)
calls["fail"] = False
check("retries after the gap and recovers", ns.get_nav("120465", now=noon + 400) == (62.31, "07-10-2026"))
calls["fail"] = True
check("failed refresh keeps the old snapshot", ns.get_nav("120465", now=noon + 5 * 3600) == (62.31, "07-10-2026"))
check("snapshot older than 3 days is not served", ns.get_nav("120465", now=noon + 4 * 86400) is None)
calls["fail"] = False

reset()
ns.MIN_ROWS = 1000
check("suspiciously small file is rejected", ns.get_nav("120465", now=noon) is None and "could be read" in (ns._snapshot["last_error"] or ""))
ns.MIN_ROWS = 2

# ---- holdings_api.fetch_nav: AMFI first, mfapi fallback
reset(); calls["n"] = 0
mf_calls = []
holdings_api.fetch_nav_mfapi = lambda code: (mf_calls.append(code), (9.99, "01-01-2026"))[1]
check("fetch_nav prefers AMFI", holdings_api.fetch_nav("120465") == (62.31, holdings_api.nav_service.get_nav("120465")[1]) and not mf_calls)
check("fetch_nav falls back to mfapi for unlisted scheme", holdings_api.fetch_nav("999999") == (9.99, "01-01-2026") and mf_calls == ["999999"])
check("fetch_nav rejects non-numeric", holdings_api.fetch_nav("abc") is None)
calls["fail"] = True; reset()
check("AMFI outage -> mfapi answers", holdings_api.fetch_nav("120465") == (9.99, "01-01-2026"))
calls["fail"] = False; reset()

# ---- routes
app = FastAPI()
ns.register_nav(app)
http = TestClient(app)
r = http.get("/nav/status").json()
check("status route", r["schemes"] == 3 and r["nav_date"] == "2026-10-07" and r["source"] == "AMFI" and r["last_error"] is None, str(r))
r = http.get("/nav/120465")
check("nav route from AMFI", r.status_code == 200 and r.json()["source"] == "AMFI" and r.json()["nav"] == 62.31 and "Axis" in r.json()["name"], r.text)
r = http.get("/nav/999999")
check("nav route falls back to mfapi", r.status_code == 200 and r.json()["source"] == "mfapi" and r.json()["nav"] == 9.99, r.text)
holdings_api.fetch_nav_mfapi = lambda code: None
check("nav route 404 when nobody has it", http.get("/nav/999998").status_code == 404)
check("nav route rejects non-numeric", http.get("/nav/abc").status_code == 400)

print()
print("ALL PASSED" if not failures else f"{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
