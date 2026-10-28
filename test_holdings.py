"""
Tests for holdings_api.py using an in-memory fake of Supabase.
Run:  python test_holdings.py
"""
import os
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient

import holdings_api
from fake_supabase import FakeSb
from holdings_api import register_holdings, valuation

OWNER = "adv@example.com"
sb = FakeSb()
sb.tables["crm_clients"] = [
    {"id": 1, "owner_email": OWNER},
    {"id": 3, "owner_email": "other@example.com"},
]
NAVS = {"111111": (50.0, "07-10-2026"), "222222": (10.0, "07-10-2026"), "333333": None}
holdings_api.fetch_nav = lambda code: NAVS.get(code)
app = FastAPI()
register_holdings(app, lambda: sb)
http = TestClient(app)
failures = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if not cond else ""))
    if not cond:
        failures.append(name)


os.environ.pop("ORDERS_OWNER_EMAIL", None)
base = f"/crm/{OWNER}/clients/1/portfolio"

# empty portfolio
j = http.get(base).json()
check("empty portfolio", j["holdings"] == [] and j["totals"]["total"] == 0 and j["totals"]["gain_pct"] is None, str(j))

# add
r = http.post(base, json=dict(scheme_code="111111", scheme_name="Fund A", units=100, avg_nav=40))
check("add holding", r.status_code == 200 and r.json()["units"] == 100, r.text)
r = http.post(base, json=dict(scheme_code="222222", scheme_name="Fund B", units=1000, avg_nav=12, folio_number=" 123/45 "))
check("add second holding with folio", r.status_code == 200 and r.json()["folio_number"] == "123/45", r.text)
r = http.post(base, json=dict(scheme_code="333333", scheme_name="Fund C", units=10, avg_nav=100))
check("add holding without NAV available", r.status_code == 200)

# merge into existing line: 100@40 + 100@60 -> 200@50
r = http.post(base, json=dict(scheme_code="111111", scheme_name="Fund A", units=100, avg_nav=60))
check("same fund merges with weighted average", r.status_code == 200 and r.json()["units"] == 200 and abs(r.json()["avg_nav"] - 50) < 1e-9, r.text)
check("merge did not create a second row", sum(1 for h in sb.tables["crm_manual_holdings"] if h["scheme_code"] == "111111") == 1)

# valuation
j = http.get(base).json()
by = {h["scheme_code"]: h for h in j["holdings"]}
a = by["111111"]
check("value = units x NAV", a["current_value"] == 10000.0 and a["invested"] == 10000.0 and a["gain"] == 0.0, str(a))
b = by["222222"]
check("gain computed", b["invested"] == 12000.0 and b["current_value"] == 10000.0 and b["gain"] == -2000.0 and b["gain_pct"] == -16.67, str(b))
c = by["333333"]
check("missing NAV shows None, not zero", c["current_nav"] is None and c["current_value"] is None and c["gain"] is None)
t = j["totals"]
check("totals count priced holdings only", t["priced"] == 2 and t["total"] == 3 and t["current_value"] == 20000.0
      and t["gain"] == -2000.0 and t["gain_pct"] == round(-2000 / 22000 * 100, 2), str(t))
check("totals invested includes unpriced", t["invested"] == 23000.0, str(t))

# validation
for label, body in [
    ("zero units", dict(scheme_code="111111", scheme_name="A", units=0, avg_nav=10)),
    ("negative NAV", dict(scheme_code="111111", scheme_name="A", units=1, avg_nav=-5)),
    ("huge units", dict(scheme_code="111111", scheme_name="A", units=1e12, avg_nav=10)),
    ("non-numeric scheme code", dict(scheme_code="12ab", scheme_name="A", units=1, avg_nav=10)),
    ("blank name", dict(scheme_code="111111", scheme_name=" ", units=1, avg_nav=10)),
]:
    check(f"rejects {label}", http.post(base, json=body).status_code == 400)

# ownership
r = http.post(f"/crm/{OWNER}/clients/3/portfolio", json=dict(scheme_code="111111", scheme_name="A", units=1, avg_nav=1))
check("other advisor's client -> 404", r.status_code == 404)
check("portfolio of other advisor's client -> 404", http.get(f"/crm/{OWNER}/clients/3/portfolio").status_code == 404)

# update
hid = a["id"]
r = http.put(f"/crm/{OWNER}/portfolio/{hid}", json=dict(units=150, avg_nav=45))
check("edit units and NAV", r.status_code == 200 and r.json()["units"] == 150 and r.json()["avg_nav"] == 45, r.text)
check("empty edit rejected", http.put(f"/crm/{OWNER}/portfolio/{hid}", json={}).status_code == 400)
check("zero units on edit rejected", http.put(f"/crm/{OWNER}/portfolio/{hid}", json=dict(units=0)).status_code == 400)
check("edit someone else's holding -> 404", http.put("/crm/other@example.com/portfolio/%d" % hid, json=dict(units=1)).status_code == 404)

# held_units helper
check("held_units sums folios", holdings_api.held_units(sb, OWNER, 1, "111111") == 150.0)
check("held_units None when no record", holdings_api.held_units(sb, OWNER, 1, "999999") is None)

# delete
r = http.delete(f"/crm/{OWNER}/portfolio/{hid}")
check("delete holding", r.status_code == 200 and holdings_api.held_units(sb, OWNER, 1, "111111") is None)
check("delete again -> 404", http.delete(f"/crm/{OWNER}/portfolio/{hid}").status_code == 404)
check("delete someone else's -> 404", http.delete("/crm/other@example.com/portfolio/%d" % 2).status_code == 404)

# owner guard
os.environ["ORDERS_OWNER_EMAIL"] = "me@example.com"
check("owner guard blocks others", http.get(base).status_code == 403)
os.environ.pop("ORDERS_OWNER_EMAIL")

# valuation helper alone
v = valuation([], nav_lookup=lambda c: None)
check("valuation of nothing", v["totals"]["total"] == 0)

# real fetch_nav input hygiene (no network call for bad codes)
import importlib
real = importlib.reload(holdings_api)
check("fetch_nav rejects non-numeric code", real.fetch_nav("../etc") is None and real.fetch_nav("") is None)

print()
print("ALL PASSED" if not failures else f"{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
