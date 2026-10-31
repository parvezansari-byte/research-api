import bcrypt
from fastapi import FastAPI
from fastapi.testclient import TestClient
import account_delete_api as a
from fake_supabase import FakeSb

ok = bad = 0
def check(c, m):
    global ok, bad
    if c: ok += 1
    else: bad += 1; print("FAIL", m)

pw = bcrypt.hashpw(b"secret123", bcrypt.gensalt()).decode()
sb = FakeSb()
sb.tables["users"] = [{"email": "a@x.com", "password_hash": pw}, {"email": "b@x.com", "password_hash": pw}]
for t, col in a.USER_TABLES:
    sb.tables[t] = [{col: "a@x.com", "id": 1}, {col: "b@x.com", "id": 2}]
for t, col in a.OWNER_TABLES:
    if t not in ("aum_history", "followups"):
        sb.tables[t] = [{col: "a@x.com", "id": 1}, {col: "b@x.com", "id": 2}]
sb.tables["crm_orders"] = [{"owner_email": "a@x.com", "id": 1}]
sb.tables["crm_order_events"] = [{"owner_email": "a@x.com", "id": 1}]

seeded = [x for x in a.OWNER_TABLES if x[0] in sb.tables]
app = FastAPI(); a.register_account_delete(app, lambda: sb); c = TestClient(app)
check("Permanently delete" in c.get("/account/delete-page").text, "page")
r = c.post("/account/delete", json={"email": "a@x.com", "password": "nope"})
check(r.status_code == 401 and len(sb.tables["users"]) == 2, "wrong password")
r = c.post("/account/delete", json={"email": "ghost@x.com", "password": "x"})
check(r.status_code == 401, "unknown email")
r = c.post("/account/delete", json={"email": " A@x.com ", "password": "secret123"})
check(r.status_code == 200 and r.json()["ok"], f"delete {r.text}")
check([u["email"] for u in sb.tables["users"]] == ["b@x.com"], "only a removed")
for t, col in a.USER_TABLES + seeded:
    left = [row[col] for row in sb.tables[t]]
    check("a@x.com" not in left and "b@x.com" in left, f"{t} scoped: {left}")
check(len(sb.tables["crm_orders"]) == 1 and len(sb.tables["crm_order_events"]) == 1, "orders kept")
# rate limit
a._fails.clear()
for _ in range(5):
    c.post("/account/delete", json={"email": "b@x.com", "password": "bad"})
r = c.post("/account/delete", json={"email": "b@x.com", "password": "secret123"})
check(r.status_code == 429 and len(sb.tables["users"]) == 1, "rate limited")
print(f"{ok} passed, {bad} failed")
