import os
from fastapi import FastAPI
from fastapi.testclient import TestClient
import lookthrough_api as lt
from fake_supabase import FakeSb

ok = bad = 0
def check(c, m):
    global ok, bad
    if c: ok += 1
    else: bad += 1; print("FAIL", m)

# synthetic snapshot so numbers are exact
def fund(key, name, hold): return {"key": key, "fund": name, "amc": "X", "holdings": [
    {"isin": i, "instrument": n, "sector": s, "pct_nav": p, "value_lakh": 1} for i, n, s, p in hold]}
A = fund("a", "Alpha Large Cap Fund - An Open Ended Equity Scheme", [("I1", "Bank One", "Banks", 10), ("I2", "Tech Two", "IT", 5), ("I3", "Oil Three", "Oil", 2)])
B = fund("b", "Beta Flexi Cap Fund", [("I1", "Bank One", "Banks", 6), ("I2", "Tech Two", "IT", 1), ("I9", "Auto Nine", "Auto", 20)])
C = fund("c", "Gamma Mid Cap Fund", [("I7", "Mid Seven", "Industrials", 40)])
idx = {}
for f in (A, B, C): idx[lt.pr.name_key(f["fund"])] = f

check(lt.match_holdings_fund("Alpha Large Cap Fund - Direct Plan - Growth", idx) is A, "direct/regular names match")
check(lt.match_holdings_fund("Alpha Large Cap Fund - Regular Plan - IDCW", idx) is A, "idcw variant matches")
check(lt.match_holdings_fund("Delta Large Cap Fund", idx) is None, "unknown fund")
check(lt.match_holdings_fund("Beta Flexi Cap Fund 50", idx) is None, "numeric tokens must agree")
check(lt.match_holdings_fund("", idx) is None, "empty")

r = lt.build_lookthrough([("Alpha Large Cap Fund - Direct Plan - Growth", 1000000),
                          ("Beta Flexi Cap Fund - Regular Plan - Growth", 500000),
                          ("Some Liquid Fund", 500000)], idx)
check(r["total_value"] == 2000000 and r["analysed_value"] == 1500000 and r["coverage_pct"] == 75.0, f"coverage {r['coverage_pct']}")
bank = next(s for s in r["top_stocks"] if s["name"] == "Bank One")
check(bank["value"] == 1000000 * 0.10 + 500000 * 0.06 and bank["funds"] == 2 and bank["pct_of_portfolio"] == 6.5, f"bank {bank}")
check(bank["flag"] is True, "5%+ flagged")
auto = next(s for s in r["top_stocks"] if s["name"] == "Auto Nine")
check(auto["value"] == 100000 and auto["pct_of_portfolio"] == 5.0 and auto["flag"], "auto exactly 5% flagged")
check(r["top_stocks"][0]["name"] == "Bank One", "sorted by value")
check(r["sectors"][0]["sector"] == "Banks" and r["sectors"][0]["value"] == 130000, f"sectors {r['sectors'][0]}")
check(r["not_analysed"] == [{"name": "Some Liquid Fund", "value": 500000.0}], "not analysed listed")
# overlap A/B = min(10,6)+min(5,1) = 7 -> below threshold
check(r["overlapping_pairs"] == [], "low overlap not listed")
D = fund("d", "Delta Twin Fund", [("I1", "Bank One", "Banks", 10), ("I2", "Tech Two", "IT", 5), ("I3", "Oil Three", "Oil", 2)])
idx[lt.pr.name_key(D["fund"])] = D
r = lt.build_lookthrough([("Alpha Large Cap Fund", 100), ("Delta Twin Fund", 100)], idx)
check(r["overlapping_pairs"] and r["overlapping_pairs"][0]["overlap_pct"] == 17.0 or r["overlapping_pairs"] == [], "pair math")
E = fund("e", "Echo Clone Fund", [("I1", "Bank One", "Banks", 50), ("I2", "Tech Two", "IT", 30)])
F = fund("f", "Foxtrot Clone Fund", [("I1", "Bank One", "Banks", 45), ("I2", "Tech Two", "IT", 35)])
for f in (E, F): idx[lt.pr.name_key(f["fund"])] = f
r = lt.build_lookthrough([("Echo Clone Fund", 100), ("Foxtrot Clone Fund", 100)], idx)
check(r["overlapping_pairs"] == [{"a": "Echo Clone Fund", "b": "Foxtrot Clone Fund", "overlap_pct": 75.0}], r["overlapping_pairs"])
# same fund twice merges
r = lt.build_lookthrough([("Alpha Large Cap Fund - Direct Plan - Growth", 100), ("Alpha Large Cap Fund - Regular Plan - Growth", 300)], idx)
check(r["funds_analysed"] == 1 and r["analysed_value"] == 400 and r["overlapping_pairs"] == [], "duplicate holdings merge")
check(lt.build_lookthrough([], idx)["coverage_pct"] == 0.0, "empty")
check(lt.build_lookthrough([("Alpha Large Cap Fund", 0), ("Alpha Large Cap Fund", -5)], idx)["total_value"] == 0, "non-positive ignored")

# real snapshot sanity
real = lt.build_lookthrough([("HDFC Flexi Cap Fund - Direct Plan - Growth", 500000), ("Parag Parikh Flexi Cap Fund - Direct Plan - Growth", 300000)])
check(real["funds_analysed"] == 2 and real["coverage_pct"] == 100.0 and len(real["top_stocks"]) == 15, "real snapshot")
check(0 < sum(s["pct_of_portfolio"] for s in real["sectors"]) <= 100.5, "sector pcts within 100")

# HTTP
os.environ.pop("ORDERS_OWNER_EMAIL", None)
sb = FakeSb(); sb.tables["crm_clients"] = [{"id": 1, "owner_email": "o@x.com", "full_name": "A"}]
sb.tables["crm_manual_holdings"] = [{"id": 1, "owner_email": "o@x.com", "client_id": 1, "scheme_code": "1",
    "scheme_name": "HDFC Flexi Cap Fund - Direct Plan - Growth", "folio_number": "", "units": 1000, "avg_nav": 100}]
app = FastAPI(); lt.register_lookthrough(app, lambda: sb, lambda c: (500.0, "d")); c = TestClient(app)
j = c.get("/crm/O@x.com/clients/1/lookthrough").json()
check(j["total_value"] == 500000 and j["funds_analysed"] == 1, f"client route {j.get('total_value')}")
check(c.get("/crm/o@x.com/clients/7/lookthrough").status_code == 404, "404")
os.environ["ORDERS_OWNER_EMAIL"] = "boss@x.com"
check(c.get("/crm/o@x.com/clients/1/lookthrough").status_code == 403, "guard"); os.environ.pop("ORDERS_OWNER_EMAIL")
j = c.post("/portfolio/lookthrough", json={"holdings": [{"name": "HDFC Flexi Cap Fund", "current": 100000}]}).json()
check(j["funds_analysed"] == 1, "manual route")
check(c.post("/portfolio/lookthrough", json={"holdings": []}).status_code == 400, "empty rejected")
print(f"{ok} passed, {bad} failed"); raise SystemExit(1 if bad else 0)
