import os
from fastapi import FastAPI
from fastapi.testclient import TestClient
import holdings_import_api as m
import nav_service
from fake_supabase import FakeSb

ok = bad = 0
def check(c, msg):
    global ok, bad
    if c: ok += 1
    else: bad += 1; print("FAIL", msg)

NAMES = {"100": "Alpha Fund - Direct Plan - Growth", "200": "Beta Fund - Direct Plan - Growth"}
nav_service.scheme_name = lambda c: NAMES.get(str(c))
nav_service.lookup_by_name = lambda n: {"code": "200"} if "beta" in n.lower() else None
nav_service.refresh = lambda *a, **k: True

R = m.ImportRow
x = m.resolve_row(R(scheme_code="100", units=10, avg_nav=50, buy_date="2023-01-05"))
check(x["scheme_name"].startswith("Alpha") and x["buy"].isoformat() == "2023-01-05", "code row")
check(m.resolve_row(R(scheme_code="100.0", units=1, avg_nav=1))["scheme_code"] == "100", "float code")
check(m.resolve_row(R(scheme_name="Beta Fund", units=1, avg_nav=1))["scheme_code"] == "200", "name row")
for bad_row, why in [(R(units=1, avg_nav=1), "no id"), (R(scheme_code="abc", units=1, avg_nav=1), "bad code"),
                     (R(scheme_code="999", units=1, avg_nav=1), "unknown code"),
                     (R(scheme_name="Zeta", units=1, avg_nav=1), "unknown name"),
                     (R(scheme_code="100", units=0, avg_nav=1), "zero units"),
                     (R(scheme_code="100", units=1, avg_nav=1, buy_date="2999-01-01"), "future date"),
                     (R(scheme_code="100", units=1, avg_nav=1, buy_date="x"), "bad date")]:
    try:
        m.resolve_row(bad_row); check(False, why)
    except ValueError:
        check(True, why)

sb = FakeSb()
sb.tables["crm_clients"] = [{"id": 1, "owner_email": "o@x.com", "full_name": "A"}]
sb.tables["crm_manual_holdings"] = []
app = FastAPI(); m.register_holdings_import(app, lambda: sb); c = TestClient(app)
os.environ.pop("ORDERS_OWNER_EMAIL", None)
U = "/crm/O@x.com/clients/1/portfolio/import"
rows = [{"scheme_code": "100", "units": 10, "avg_nav": 50, "buy_date": "2023-01-01"},
        {"scheme_name": "Beta Fund", "units": 5, "avg_nav": 20},
        {"scheme_code": "777", "units": 1, "avg_nav": 1}]
d = c.post(U, json={"rows": rows, "dry_run": True}).json()
check(d["valid"] == 2 and len(d["errors"]) == 1 and d["errors"][0]["row"] == 3, f"dry {d}")
check(sb.tables["crm_manual_holdings"] == [], "dry run saves nothing")
r = c.post(U, json={"rows": rows}).json()
check(r["added"] == 2 and r["merged"] == 0 and len(sb.tables["crm_manual_holdings"]) == 2, f"add {r}")
r = c.post(U, json={"rows": [{"scheme_code": "100", "units": 10, "avg_nav": 70}]}).json()
h = [x for x in sb.tables["crm_manual_holdings"] if x["scheme_code"] == "100"][0]
check(r["merged"] == 1 and h["units"] == 20 and h["avg_nav"] == 60, f"merge {r} {h}")
check(c.post(U, json={"rows": []}).status_code == 400, "empty")
check(c.post(U, json={"rows": [{}] * 301}).status_code == 400, "too many")
check(c.post("/crm/O@x.com/clients/9/portfolio/import", json={"rows": rows}).status_code == 404, "client 404")
os.environ["ORDERS_OWNER_EMAIL"] = "boss@x.com"
check(c.post(U, json={"rows": rows}).status_code == 403, "403")
print(f"{ok} passed, {bad} failed")
