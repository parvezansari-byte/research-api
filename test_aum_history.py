import os
from datetime import date, datetime, timedelta, timezone
from fastapi import FastAPI
from fastapi.testclient import TestClient
import push_api
import aum_history_api as a
from fake_supabase import FakeSb

ok = bad = 0
def check(c, m):
    global ok, bad
    if c: ok += 1
    else: bad += 1; print("FAIL", m)

OWNER = push_api.OWNER_EMAIL
IST = timezone(timedelta(hours=5, minutes=30))
clock = [datetime(2026, 10, 30, 23, 0, tzinfo=IST)]
NAV = {"1": (20.0, "d"), "2": None}
def row(i, cid, code, units, avg): return {"id": i, "owner_email": OWNER, "client_id": cid, "scheme_code": code,
    "scheme_name": "HDFC Flexi Cap Fund - Direct Plan - Growth", "folio_number": "", "units": units, "avg_nav": avg}
sb = FakeSb()
sb.tables["crm_clients"] = [{"id": 1, "owner_email": OWNER, "full_name": "A"}, {"id": 2, "owner_email": OWNER, "full_name": "B"}]
sb.tables["crm_manual_holdings"] = [row(1, 1, "1", 1000, 10), row(2, 2, "1", 500, 10)]

r = a.snapshot_owner(sb, OWNER, date(2026, 10, 29), lambda c: NAV.get(c))
check(r == {"saved": True, "day": "2026-10-29", "value": 30000.0, "invested": 15000.0}, r)
r = a.snapshot_owner(sb, OWNER, date(2026, 10, 29), lambda c: (22.0, "d"))
check(len(sb.tables["crm_aum_history"]) == 1 and sb.tables["crm_aum_history"][0]["value"] == 33000.0, "same day overwrites")
sb2 = FakeSb(); sb2.tables["crm_manual_holdings"] = [row(1, 1, "2", 100, 10)]; sb2.tables["crm_clients"] = []
check(a.snapshot_owner(sb2, OWNER, date(2026, 10, 29), lambda c: None)["saved"] is False and not sb2.tables.get("crm_aum_history"), "nothing priced -> not saved")
check(a.snapshot_owner(FakeSb(), OWNER, date(2026, 10, 29), lambda c: None)["saved"] is False, "empty book -> not saved")

rows = [{"day": "2026-09-01", "value": 100, "invested": 100, "clients": 1},
        {"day": "2026-10-01", "value": 130, "invested": 110, "clients": 1},
        {"day": "2026-10-30", "value": 150, "invested": 120, "clients": 2},
        {"day": "2025-01-01", "value": 1, "invested": 1, "clients": 1}]
s = a.build_series(rows, 90, date(2026, 10, 30))
check([p["day"] for p in s["points"]] == ["2026-09-01", "2026-10-01", "2026-10-30"], "window + sorted")
c = s["change"]
check(c["value_change"] == 50 and c["value_change_pct"] == 50.0 and c["new_money"] == 20 and c["market_movement"] == 30, c)
check(a.build_series(rows, 10, date(2026, 10, 30))["change"] is None, "one point -> no change")
check(a.build_series([], 30, date(2026, 10, 30)) == {"points": [], "days": 30, "change": None}, "empty")
check(a.build_series(rows, 99999, date(2026, 10, 30))["days"] == a.MAX_DAYS, "days capped")

os.environ["PUSH_TICK_SECRET"] = "s"; os.environ.pop("ORDERS_OWNER_EMAIL", None)
app = FastAPI(); a.register_aum_history(app, lambda: sb, lambda c: NAV.get(c) or (20.0, "d"), lambda: clock[0]); c = TestClient(app)
check(c.post("/crm/aum/snapshot").status_code == 403, "no secret")
j = c.post("/crm/aum/snapshot", headers={"X-Tick-Secret": "s"}).json()
check(j["saved"] and j["day"] == "2026-10-30", f"tick {j}")
h = c.get(f"/crm/{OWNER}/aum-history?days=30").json()
check(len(h["points"]) == 2 and h["change"]["to"] == "2026-10-30", f"history {h}")
os.environ["ORDERS_OWNER_EMAIL"] = "boss@x.com"
check(c.get(f"/crm/{OWNER}/aum-history").status_code == 403, "guard"); os.environ.pop("ORDERS_OWNER_EMAIL")
os.environ.pop("PUSH_TICK_SECRET")
check(c.post("/crm/aum/snapshot", headers={"X-Tick-Secret": "s"}).status_code == 503, "secret unset")
print(f"{ok} passed, {bad} failed"); raise SystemExit(1 if bad else 0)
