import os
from datetime import date
from fastapi import FastAPI
from fastapi.testclient import TestClient
import tax_gains_api as t
from fake_supabase import FakeSb

ok = bad = 0
def check(c, m):
    global ok, bad
    if c: ok += 1
    else: bad += 1; print("FAIL", m)

T = date(2026, 10, 28)
FLEXI = "HDFC Flexi Cap Fund - Direct Plan - Growth"
DEBT = "HDFC Short Term Debt Fund - Direct Plan - Growth"
HYB = "HDFC Balanced Advantage Fund - Direct Plan - Growth"
def h(name, inv, cur, bd): return {"scheme_name": name, "invested": inv, "current_value": cur, "buy_date": bd}
H = [h(FLEXI, 100000, 400000, "2024-01-10"),   # long, +300000
     h(FLEXI, 100000, 150000, "2026-03-01"),   # short, +50000
     h(FLEXI, 100000, 120000, "2025-10-30"),   # short, 363 days -> long in 3 days, +20000
     h(FLEXI, 100000, 90000, "2026-09-01"),    # short, loss
     h(FLEXI, 100000, 130000, None),           # no date
     h(DEBT, 100000, 110000, "2025-01-01"),
     h(HYB, 100000, 120000, "2024-01-01"),
     h("Mystery Fund", 100000, 120000, "2024-01-01"),
     {"scheme_name": FLEXI, "invested": 1, "current_value": None, "buy_date": "2024-01-01"}]
r = t.build_tax(H, T, 30)
e = r["equity"]
check(e["long_term_gain"] == 300000 and e["short_term_gain"] == 50000 + 20000 - 10000, f"gains {e}")
check(e["ltcg_tax"] == (300000 - 125000) * 0.125 and e["stcg_tax"] == 60000 * 0.2, f"tax {e}")
check(e["exemption_left"] == 0, "no headroom")
check(r["debt"]["gain"] == 10000 and r["debt"]["tax"] == 3000, f"debt {r['debt']}")
check(r["total_tax"] == 21875 + 12000 + 3000, f"total {r['total_tax']}")
check(r["skipped"] == {"no_date": 1, "no_nav": 1}, r["skipped"])
check(len(r["turning_long_term"]) == 1 and r["turning_long_term"][0]["days_left"] == 3, f"soon {r['turning_long_term']}")
tr = {x["scheme_name"]: x for x in r["holdings"]}
check(tr[HYB]["treatment"].startswith("Rules depend") and tr["Mystery Fund"]["asset_class"] == "Unclassified", "no-estimate classes")
check(r["holdings"][0]["gain"] == 300000, "sorted by gain")
# boundary: exactly 365 days is short, 366 long
b = t.build_tax([h(FLEXI, 100, 200, "2025-10-28")], T)["holdings"][0]
b2 = t.build_tax([h(FLEXI, 100, 200, "2025-10-27")], T)["holdings"][0]
check(b["days"] == 365 and b["term"] == "short" and b2["term"] == "long", "12 month boundary")
# exemption headroom
r = t.build_tax([h(FLEXI, 100000, 160000, "2024-01-01")], T)
check(r["equity"]["exemption_left"] == 65000 and r["equity"]["ltcg_tax"] == 0, "headroom")
# losses do not create negative tax
r = t.build_tax([h(FLEXI, 100000, 50000, "2024-01-01"), h(DEBT, 100000, 90000, "2025-01-01")], T)
check(r["total_tax"] == 0, "losses -> no tax")
check(t.build_tax([h(DEBT, 100000, 110000, "2025-01-01")], T, 999)["debt"]["slab_pct"] == 50, "slab capped")
check(t.build_tax([], T)["total_tax"] == 0, "empty")

os.environ.pop("ORDERS_OWNER_EMAIL", None)
sb = FakeSb()
sb.tables["crm_clients"] = [{"id": 1, "owner_email": "o@x.com", "full_name": "A"}]
sb.tables["crm_manual_holdings"] = [{"id": 1, "owner_email": "o@x.com", "client_id": 1, "scheme_code": "1",
    "scheme_name": FLEXI, "folio_number": "", "units": 1000, "avg_nav": 100, "buy_date": "2024-01-01"}]
app = FastAPI(); t.register_tax_gains(app, lambda: sb, lambda c: (150.0, "d"), lambda: T); c = TestClient(app)
j = c.get("/crm/O@x.com/clients/1/tax-gains").json()
check(j["equity"]["long_term_gain"] == 50000 and j["total_tax"] == 0, f"http {j['equity']}")
check(c.get("/crm/o@x.com/clients/2/tax-gains").status_code == 404, "unknown client")
os.environ["ORDERS_OWNER_EMAIL"] = "boss@x.com"
check(c.get("/crm/o@x.com/clients/1/tax-gains").status_code == 403, "guard"); os.environ.pop("ORDERS_OWNER_EMAIL")
print(f"{ok} passed, {bad} failed"); raise SystemExit(1 if bad else 0)
