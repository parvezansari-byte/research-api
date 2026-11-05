import os
from datetime import date, datetime, timedelta, timezone
from fastapi import FastAPI
from fastapi.testclient import TestClient
import push_api
import followups_api as f
from fake_supabase import FakeSb

ok = bad = 0
def check(c, m):
    global ok, bad
    if c: ok += 1
    else: bad += 1; print("FAIL", m)

T = date(2026, 10, 30)
rows = [{"client_id": 1, "due_date": "2026-10-25", "note": "annual review"},
        {"client_id": 2, "due_date": "2026-10-30", "note": ""},
        {"client_id": 3, "due_date": "2026-11-05", "note": "SIP step-up"},
        {"client_id": 4, "due_date": "2027-03-01", "note": "far"},
        {"client_id": 5, "due_date": "bad", "note": "x"}]
r = f.build_due(rows, {1: "Asha", 2: "Ravi", 3: "Meera", 4: "Far", 5: "Bad"}, T, 14)
check([x["client_name"] for x in r["followups"]] == ["Asha", "Ravi", "Meera"], r["followups"])
check(r["followups"][0]["overdue"] and r["followups"][0]["days_until"] == -5 and not r["followups"][1]["overdue"], "overdue flags")
check(r["counts"] == {"total": 3, "overdue": 1, "today": 1}, r["counts"])
check(f.build_due(rows, {}, T, 0)["counts"]["total"] == 2 and f.build_due(rows, {}, T, 0)["followups"][0]["client_name"] == "Client 1", "days=0, name fallback")
check(f.build_due([], {}, T)["followups"] == [], "empty")

os.environ.pop("ORDERS_OWNER_EMAIL", None); os.environ["PUSH_TICK_SECRET"] = "s"
OWNER = push_api.OWNER_EMAIL
IST = timezone(timedelta(hours=5, minutes=30)); now = [datetime(2026, 10, 30, 8, 35, tzinfo=IST)]
sb = FakeSb(); sb.tables["crm_clients"] = [{"id": i, "owner_email": OWNER, "full_name": n} for i, n in ((1, "Asha"), (2, "Ravi"), (3, "Meera"))]
sent = []
push_api.send_to_email = lambda s_, e, t, b, data=None, kind=None: (sent.append((t, b, kind)) or {"sent": 1})
app = FastAPI(); f.register_followups(app, lambda: sb, lambda: now[0]); c = TestClient(app)
U = f"/crm/{OWNER}/clients"
check(c.put(f"{U}/1/followup", json={"due_date": "2026-10-25", "note": "annual review"}).json()["saved"], "set")
check(c.put(f"{U}/1/followup", json={"due_date": "2026-10-26", "note": "moved"}).status_code == 200 and len(sb.tables["crm_followups"]) == 1, "replaces, one per client")
check(c.get(f"{U}/1/followup").json()["followup"] == {"due_date": "2026-10-26", "note": "moved"}, "get one")
check(c.get(f"{U}/2/followup").json()["followup"] is None, "none yet")
check(c.put(f"{U}/9/followup", json={"due_date": "2026-10-25"}).status_code == 404, "unknown client")
check(c.put(f"{U}/1/followup", json={"due_date": "nope"}).status_code == 400, "bad date")
check(len(c.put(f"{U}/2/followup", json={"due_date": "2026-11-01", "note": "x" * 999}).json()["note"]) == f.MAX_NOTE, "note capped")
c.put(f"{U}/3/followup", json={"due_date": "2026-10-30", "note": "SIP step-up"})
j = c.get(f"/crm/{OWNER}/followups?days=7").json()
check(j["counts"] == {"total": 3, "overdue": 1, "today": 1} and j["followups"][0]["client_name"] == "Asha", j["counts"])
# orphan follow-up of a deleted client is ignored
sb.tables["crm_followups"].append({"id": "x", "owner_email": OWNER, "client_id": 77, "due_date": "2026-10-01", "note": ""})
check(c.get(f"/crm/{OWNER}/followups").json()["counts"]["total"] == 3, "orphan ignored")
# tick
check(c.post("/clients/followups/tick").status_code == 403, "no secret")
j = c.post("/clients/followups/tick", headers={"X-Tick-Secret": "s"}).json()
check(j["due"] == 2 and len(sent) == 1 and sent[0][2] == "followup" and "2 client follow-ups" in sent[0][0], f"tick {j} {sent}")
check("already" in c.post("/clients/followups/tick", headers={"X-Tick-Secret": "s"}).json()["push"] and len(sent) == 1, "once a day")
now[0] = datetime(2026, 10, 31, 8, 35, tzinfo=IST)
check(c.post("/clients/followups/tick", headers={"X-Tick-Secret": "s"}).json()["due"] == 2 and len(sent) == 2, "overdue items repeat next day")
check(c.delete(f"{U}/1/followup").json()["cleared"] and c.get(f"{U}/1/followup").json()["followup"] is None, "clear")
os.environ["ORDERS_OWNER_EMAIL"] = "boss@x.com"
check(c.get(f"/crm/{OWNER}/followups").status_code == 403 and c.put(f"{U}/1/followup", json={"due_date": "2026-10-25"}).status_code == 403, "guard")
print(f"{ok} passed, {bad} failed"); raise SystemExit(1 if bad else 0)
