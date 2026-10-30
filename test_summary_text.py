import os
from datetime import date
from fastapi import FastAPI
from fastapi.testclient import TestClient
import holdings_api, summary_text_api as s
from fake_supabase import FakeSb

ok = bad = 0
def check(c, m):
    global ok, bad
    if c: ok += 1
    else: bad += 1; print("FAIL", m)

check(s.inr(0) == "Rs 0" and s.inr(999) == "Rs 999" and s.inr(1000) == "Rs 1,000" and s.inr(125000) == "Rs 1,25,000"
      and s.inr(12345678) == "Rs 1,23,45,678" and s.inr(-2500) == "-Rs 2,500", "inr grouping")
check(s.short_name("HDFC Flexi Cap Fund - Direct Plan - Growth") == "HDFC Flexi Cap Fund" and len(s.short_name("X" * 80)) == 42, "short name")
T = date(2026, 10, 30)
def row(i, code, name, units, avg): return {"id": i, "owner_email": "o@x.com", "client_id": 1, "scheme_code": code, "scheme_name": name, "folio_number": "", "units": units, "avg_nav": avg}
NAV = {"1": (20.0, "d"), "2": (9.0, "d"), "3": None}
rows = [row(1, "1", "Alpha Fund - Direct Plan - Growth", 1000, 10), row(2, "2", "Beta Fund", 1000, 10), row(3, "3", "No NAV Fund", 10, 10)]
val = holdings_api.valuation(rows, lambda c: NAV.get(c))
r = s.build_summary("Asha", val, T, "Parvez, Wealthy")
t = r["text"]
check(t.startswith("*Portfolio summary for Asha*") and "As on 30 Oct 2026" in t, "header")
check(t.index("Alpha Fund") < t.index("Beta Fund"), "sorted by value")
check("Rs 10,000 → Rs 20,000 (+100.0%)" in t and "Rs 10,000 → Rs 9,000 (-10.0%)" in t, t)
check("*Total invested:* Rs 20,000" in t and "*Current value:* Rs 29,000" in t and "*Gain:* Rs 9,000 (+45.0%)" in t, "totals")
check("1 fund(s) left out" in t and "No NAV Fund" not in t, "unpriced excluded and flagged")
check(t.rstrip().endswith("— Parvez, Wealthy") and "subject to market risks" in t, "footer")
check(r["holdings"] == 2, "count")
many = [row(i, str(i), f"Fund {i}", 100, 10) for i in range(1, 21)]
v2 = holdings_api.valuation(many, lambda c: (10.0 + int(c), "d"))
t2 = s.build_summary("A", v2, T)["text"]
check(t2.count("•") == 16 and "• 5 more funds worth" in t2, f"cap at 15 + rollup {t2.count('•')}")
check("No holdings" in s.build_summary("A", holdings_api.valuation([], None), T)["text"], "empty")
os.environ.pop("ORDERS_OWNER_EMAIL", None); os.environ["ADVISOR_SIGNATURE"] = "Team"
sb = FakeSb(); sb.tables["crm_clients"] = [{"id": 1, "owner_email": "o@x.com", "full_name": "Asha", "phone": "9876543210"}]
sb.tables["crm_manual_holdings"] = rows
app = FastAPI(); s.register_summary_text(app, lambda: sb, lambda c: NAV.get(c), lambda: T); c = TestClient(app)
j = c.get("/crm/O@x.com/clients/1/summary-text").json()
check(j["phone"] == "9876543210" and "— Team" in j["text"] and j["holdings"] == 2, "http")
check(c.get("/crm/o@x.com/clients/9/summary-text").status_code == 404, "404")
os.environ["ORDERS_OWNER_EMAIL"] = "boss@x.com"
check(c.get("/crm/o@x.com/clients/1/summary-text").status_code == 403, "guard")
print(f"{ok} passed, {bad} failed"); raise SystemExit(1 if bad else 0)
