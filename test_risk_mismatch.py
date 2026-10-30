import os
from fastapi import FastAPI
from fastapi.testclient import TestClient
import risk_mismatch_api as m
from fake_supabase import FakeSb

ok = bad = 0
def check(c, msg):
    global ok, bad
    if c: ok += 1
    else: bad += 1; print("FAIL", msg)

EQ = "HDFC Flexi Cap Fund - Direct Plan - Growth"
DEBT = "HDFC Short Term Debt Fund - Direct Plan - Growth"
HYB = "HDFC Balanced Advantage Fund - Direct Plan - Growth"
GOLD = "Nippon India Gold Savings Fund - Direct Plan - Growth"
# NAV 10 for everything, units = rupees / 10
NAV = lambda c: (10.0, "d")
def h(i, cid, code, name, rupees): return {"id": i, "owner_email": "o@x.com", "client_id": cid, "scheme_code": code, "scheme_name": name,
                                           "folio_number": "", "units": rupees / 10, "avg_nav": 10}
rows = [h(1, 1, "1", EQ, 750000), h(2, 1, "2", DEBT, 250000),                  # client 1: 75% equity
        h(3, 2, "3", EQ, 200000), h(4, 2, "4", DEBT, 600000), h(5, 2, "5", HYB, 200000),   # client 2: 20/20/60
        h(6, 3, "6", EQ, 100000), h(7, 3, "7", DEBT, 900000),                   # client 3: 10% equity
        h(8, 4, "8", GOLD, 500000),                                             # client 4: only gold
        h(9, 6, "9", EQ, 800000)]                                               # client 6: no profile
clients = [{"id": 1, "full_name": "Asha", "risk_profile": "CONSERVATIVE"},
           {"id": 2, "full_name": "Ravi", "risk_profile": "CONSERVATIVE"},
           {"id": 3, "full_name": "Meera", "risk_profile": "AGGRESSIVE"},
           {"id": 4, "full_name": "Gold", "risk_profile": "MODERATE"},
           {"id": 5, "full_name": "Empty", "risk_profile": "MODERATE"},
           {"id": 6, "full_name": "NoProfile", "risk_profile": None}]
r = m.build_mismatch(rows, clients, NAV)
f = {x["client_name"]: x for x in r["flagged"]}
check(set(f) == {"Asha", "Meera"}, f"flagged {list(f)}")
check(f["Asha"]["direction"] == "Too aggressive" and f["Asha"]["equity_gap_pp"] == 55.0 and f["Asha"]["actual"]["equity"] == 75.0, f["Asha"])
check(f["Meera"]["direction"] == "Too conservative" and f["Meera"]["equity_gap_pp"] == -55.0, f["Meera"])
check([x["client_name"] for x in r["flagged"]][0] in ("Asha", "Meera") and r["counts"] == {"flagged": 2, "in_line": 1, "no_risk_profile": 1, "no_holdings": 2}, r["counts"])
check(f["Asha"]["target"] == {"equity": 20.0, "hybrid": 20.0, "debt": 60.0}, "model target")
check(f["Asha"]["value"] == 1000000, "value")
# threshold
check(len(m.build_mismatch(rows, clients, NAV, 60)["flagged"]) == 0, "high threshold flags none")
check(m.build_mismatch(rows, clients, NAV, 0)["threshold_pp"] == 1.0 and m.build_mismatch(rows, clients, NAV, 999)["threshold_pp"] == 60.0, "threshold clamped")
# boundary: exactly 15pp away is flagged
rows2 = [h(1, 1, "1", EQ, 350000), h(2, 1, "2", DEBT, 650000)]     # 35% vs 20% target = 15
check(len(m.build_mismatch(rows2, clients[:1], NAV)["flagged"]) == 1, "exactly threshold flagged")
rows3 = [h(1, 1, "1", EQ, 340000), h(2, 1, "2", DEBT, 660000)]
check(len(m.build_mismatch(rows3, clients[:1], NAV)["flagged"]) == 0, "just inside not flagged")
check(m.build_mismatch([], [], NAV)["flagged"] == [], "empty")
check(m.build_mismatch(rows, [{"id": 1, "full_name": "x", "risk_profile": "weird"}], NAV)["counts"]["no_risk_profile"] == 1, "unknown profile treated as none")

os.environ.pop("ORDERS_OWNER_EMAIL", None)
sb = FakeSb(); sb.tables["crm_manual_holdings"] = [dict(x) for x in rows]
sb.tables["crm_clients"] = [{**c, "owner_email": "o@x.com"} for c in clients]
app = FastAPI(); m.register_risk_mismatch(app, lambda: sb, NAV); c = TestClient(app)
j = c.get("/crm/O@x.com/risk-mismatch").json()
check(j["counts"]["flagged"] == 2, "http")
check(c.get("/crm/o@x.com/risk-mismatch?threshold=70").json()["counts"]["flagged"] == 0, "threshold param")
os.environ["ORDERS_OWNER_EMAIL"] = "boss@x.com"
check(c.get("/crm/o@x.com/risk-mismatch").status_code == 403, "guard")
print(f"{ok} passed, {bad} failed"); raise SystemExit(1 if bad else 0)
