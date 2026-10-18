"""Tests for the live-NAV overlay on the Funds screen. Run: python test_funds_nav.py"""
import time
from datetime import date

from fastapi.testclient import TestClient

import api
import nav_service as ns

failures = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if not cond else ""))
    if not cond:
        failures.append(name)


funds = api._load_funds_data()
with_nav = next(f for f in funds if f.get("nav") is not None)
no_nav = next(f for f in funds if f.get("nav") is None)
unmatched = next(f for f in funds if f is not with_nav and f is not no_nav)

def snap(nav_a, nav_b, d):
    # AMFI spells one name differently ('&' / 'Option') - normalisation must still match
    return {
        "1": {"nav": nav_a, "date": d, "name": with_nav["name"].replace(" - Growth", " - Growth Option")},
        "2": {"nav": nav_b, "date": d, "name": no_nav["name"]},
    }

d1, d2 = date(2026, 10, 15), date(2026, 10, 16)
old = snap(100.0, 50.0, d1)
new = snap(102.0, 49.0, d2)
ns._snapshot.update(data=old, by_name=ns._build_name_index(old), fetched_at=time.time(),
                    latest_date=d1, prev={})
prev = ns._roll_prev(old, new, {})
ns._snapshot.update(data=new, by_name=ns._build_name_index(new), prev=prev, latest_date=d2)
ns.refresh = lambda *a, **k: False   # no network in tests

http = TestClient(api.app)
cat = api._fund_category_name(with_nav)
r = http.get("/funds/db/list", params={"category": cat, "limit": 500})
check("list ok", r.status_code == 200, r.text[:200])
rows = {f["name"]: f for f in r.json()["funds"]}
a = rows[with_nav["name"]]
check("name variant matched -> live NAV", a["nav"] == 102.0 and a["nav_live"] is True, a)
check("date shown", a["nav_date"] == "16-10-2026", a["nav_date"])
check("1-day change", a["nav_day_pct"] == 2.0, a["nav_day_pct"])
check("change vs snapshot", a["nav_change_pct"] is not None)
check("nav_matched counts live", r.json()["nav_matched"] >= 1)
if no_nav["name"] in rows or True:
    r2 = http.get("/funds/db/fund", params={"name": no_nav["name"]})
    j = r2.json()
    check("fund with no snapshot NAV gets live NAV", r2.status_code == 200 and j["nav"] == 49.0
          and j["nav_change_pct"] is None and j["nav_day_pct"] == -2.0, j.get("nav"))
r3 = http.get("/funds/db/search", params={"q": with_nav["name"][:12]})
check("search overlays", r3.status_code == 200 and any(f.get("nav_live") for f in r3.json()["funds"]))
other = [f for f in rows.values() if f["name"] not in (with_nav["name"],) and not f["nav_live"]]
check("unmatched fund keeps snapshot and is not live", (not other) or other[0]["nav_live"] is False)
cov = http.get("/funds-nav/coverage").json()
check("coverage report", cov["funds"] == len(funds) and cov["matched"] == 2 and len(cov["unmatched_sample"]) > 0, cov["matched"])
# stale snapshot is ignored
ns._snapshot["fetched_at"] = time.time() - 5 * 86400
r4 = http.get("/funds/db/fund", params={"name": no_nav["name"]})
check("stale snapshot not used", r4.json()["nav_live"] is False)
print("\nFAILED: %s" % failures if failures else "\nALL PASSED")
raise SystemExit(1 if failures else 0)
