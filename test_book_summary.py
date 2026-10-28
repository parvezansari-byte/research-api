import os
from fastapi import FastAPI
from fastapi.testclient import TestClient
import book_summary_api as b
from fake_supabase import FakeSb

ok = bad = 0
def check(c, m):
    global ok, bad
    if c: ok += 1
    else: bad += 1; print("FAIL", m)

FLEXI = "HDFC Flexi Cap Fund - Direct Plan - Growth"
def row(i, cid, code, name, units, avg):
    return {"id": i, "owner_email": "o@x.com", "client_id": cid, "scheme_code": code, "scheme_name": name,
            "folio_number": "", "units": units, "avg_nav": avg}
NAVS = {"1": (20.0, "d"), "2": (10.0, "d"), "9": None}
nav = lambda c: NAVS.get(c)
rows = [row(1, 1, "1", FLEXI, 1000, 10),          # 10000 -> 20000
        row(2, 2, "1", FLEXI, 500, 10),           # 5000 -> 10000
        row(3, 2, "2", "Mystery Fund", 1000, 10), # 10000 -> 10000
        row(4, 2, "9", "No NAV Fund", 100, 10)]   # unpriced
r = b.build_book(rows, {1: "Asha", 2: "Ravi", 3: "Idle"}, nav)
t = r["totals"]
check(t["value"] == 40000 and t["invested"] == 25000 and t["gain"] == 15000 and t["gain_pct"] == 60.0, f"totals {t}")
check(t["clients"] == 2 and t["clients_total"] == 3 and t["funds"] == 2 and t["unpriced"] == 1 and t["holdings"] == 4, f"counts {t}")
check([c["client_name"] for c in r["clients"]] == ["Asha", "Ravi"] and r["clients"][0]["share_pct"] == 50.0, "clients sorted")
check(r["clients"][1]["funds"] == 2 and r["clients"][1]["gain_pct"] == 33.3, f"ravi {r['clients'][1]}")
check(r["funds"][0]["scheme_code"] == "1" and r["funds"][0]["clients"] == 2 and r["funds"][0]["share_pct"] == 75.0, "funds")
check(r["concentration"] == {"top_client_pct": 50.0, "top5_clients_pct": 100.0, "top_fund_pct": 75.0}, r["concentration"])
check(abs(sum(a["share_pct"] for a in r["asset_classes"]) - 100) < 0.2, "classes sum")
e = b.build_book([], {}, nav)
check(e["totals"]["value"] == 0 and e["clients"] == [] and e["concentration"]["top_client_pct"] is None, "empty")

sb = FakeSb()
sb.tables["crm_manual_holdings"] = rows
sb.tables["crm_clients"] = [{"id": 1, "owner_email": "o@x.com", "full_name": "Asha"}]
app = FastAPI(); b.register_book_summary(app, lambda: sb, nav); c = TestClient(app)
os.environ.pop("ORDERS_OWNER_EMAIL", None)
j = c.get("/crm/O@x.com/book-summary").json()
check(j["totals"]["value"] == 40000 and j["clients"][1]["client_name"] == "Client 2", "http + name fallback")
os.environ["ORDERS_OWNER_EMAIL"] = "boss@x.com"
check(c.get("/crm/o@x.com/book-summary").status_code == 403, "guard")
os.environ.pop("ORDERS_OWNER_EMAIL")
app2 = FastAPI(); b.register_book_summary(app2, lambda: None)
check(TestClient(app2).get("/crm/o@x.com/book-summary").status_code == 500, "no db")
print(f"{ok} passed, {bad} failed")
raise SystemExit(1 if bad else 0)
