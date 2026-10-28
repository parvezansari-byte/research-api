import os
from datetime import date, datetime, timedelta, timezone
from fastapi import FastAPI
from fastapi.testclient import TestClient
import push_api
import client_reminders_api as r
from fake_supabase import FakeSb

ok = bad = 0
def check(c, m):
    global ok, bad
    if c: ok += 1
    else: bad += 1; print("FAIL", m)

T = date(2026, 10, 28)
def cl(i, name, dob, kyc="KYC_VALIDATED"):
    return {"id": i, "owner_email": push_api.OWNER_EMAIL, "full_name": name, "date_of_birth": dob, "kyc_status": kyc}
C = [cl(1, "Asha", "1980-10-28"), cl(2, "Ravi", "1990-11-05"), cl(3, "Meera", "1985-12-25"),
     cl(4, "Old", "1970-10-27"), cl(5, "Leap", "1992-02-29"), cl(6, "NoDob", None, "PENDING"),
     cl(7, "Bad", "garbage", "HOLD"), cl(8, "Future", "2030-10-30")]
x = r.build_reminders(C, T, 14)
b = x["birthdays"]
check([i["client_name"] for i in b] == ["Asha", "Ravi"], f"window {[i['client_name'] for i in b]}")
check(b[0]["days_until"] == 0 and b[0]["turning"] == 46 and b[1]["days_until"] == 8 and b[1]["turning"] == 36, f"{b}")
check(x["counts"]["today"] == 1 and x["counts"]["kyc_pending"] == 2 and x["counts"]["clients"] == 8, x["counts"])
check([k["client_name"] for k in x["kyc_pending"]] == ["Bad", "NoDob"] and x["kyc_pending"][1]["status"] == "PENDING", "kyc")
check(len(r.build_reminders(C, T, 60)["birthdays"]) == 3, "wider window picks Meera")
check(r.build_reminders(C, T, 365)["counts"]["clients"] == 8 and len(r.build_reminders(C, T, 999)["birthdays"]) == len(r.build_reminders(C, T, 90)["birthdays"]), "days capped at 90")
# year wrap + leap
check([i["client_name"] for i in r.build_reminders([cl(1, "A", "1990-01-02")], date(2026, 12, 30), 7)["birthdays"]] == ["A"], "year wrap")
lp = r.build_reminders([cl(1, "L", "1992-02-29")], date(2027, 2, 27), 3)["birthdays"]
check(lp and lp[0]["date"] == "2027-02-28" and lp[0]["days_until"] == 1, f"leap {lp}")
check(r.build_reminders([], T)["birthdays"] == [], "empty")

os.environ["PUSH_TICK_SECRET"] = "s"; os.environ.pop("ORDERS_OWNER_EMAIL", None)
IST = timezone(timedelta(hours=5, minutes=30)); now = [datetime(2026, 10, 28, 8, 30, tzinfo=IST)]
sb = FakeSb(); sb.tables["crm_clients"] = [dict(c) for c in C]
sent = []
push_api.send_to_email = lambda sb_, e, t, b, data=None, kind=None: (sent.append((e, t, b, kind)) or {"sent": 1})
app = FastAPI(); r.register_client_reminders(app, lambda: sb, lambda: now[0]); c = TestClient(app)
check(c.get(f"/crm/{push_api.OWNER_EMAIL}/reminders?days=14").json()["counts"]["birthdays"] == 2, "GET")
os.environ["ORDERS_OWNER_EMAIL"] = "boss@x.com"
check(c.get(f"/crm/{push_api.OWNER_EMAIL}/reminders").status_code == 403, "guard"); os.environ.pop("ORDERS_OWNER_EMAIL")
check(c.post("/clients/reminders/tick", headers={"X-Tick-Secret": "no"}).status_code == 403, "bad secret")
j = c.post("/clients/reminders/tick", headers={"X-Tick-Secret": "s"}).json()
check(j["today"] == 1 and len(sent) == 1 and "Asha (46)" in sent[0][2] and sent[0][3] == "reminder", f"tick {j} {sent}")
j = c.post("/clients/reminders/tick", headers={"X-Tick-Secret": "s"}).json()
check(len(sent) == 1 and "already" in j["push"], "once a day")
check(c.post("/clients/reminders/tick?force=true", headers={"X-Tick-Secret": "s"}).json()["today"] == 1 and len(sent) == 2, "force")
now[0] = datetime(2026, 10, 29, 8, 30, tzinfo=IST)
j = c.post("/clients/reminders/tick", headers={"X-Tick-Secret": "s"}).json()
check(j["today"] == 0 and len(sent) == 2, "no birthday, no push")
print(f"{ok} passed, {bad} failed"); raise SystemExit(1 if bad else 0)
