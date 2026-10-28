"""
Tests for orders_api.py / nse_service.py using an in-memory fake of Supabase.
Run:  python test_orders.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

import nse_service
from fake_supabase import FakeSb
from orders_api import register_orders
import holdings_api


OWNER = "adv@example.com"
sb = FakeSb()
sb.tables["crm_clients"] = [
    {"id": 1, "owner_email": OWNER, "kyc_status": "KYC_VALIDATED", "nse_client_code": "C1"},
    {"id": 2, "owner_email": OWNER, "kyc_status": "PENDING", "nse_client_code": None},
    {"id": 3, "owner_email": "other@example.com", "kyc_status": "KYC_VALIDATED"},
]
sb.tables["crm_manual_holdings"] = [
    {"id": 1, "owner_email": OWNER, "client_id": 1, "scheme_code": "119551",
     "scheme_name": "Test Fund - Regular - Growth", "folio_number": "", "units": 100.0, "avg_nav": 40.0},
]
NAV = {"value": (50.0, "07-10-2026")}
holdings_api.fetch_nav = lambda code: NAV["value"]
app = FastAPI()
register_orders(app, lambda: sb)
http = TestClient(app)

_n = [0]


def body(**kw):
    _n[0] += 1
    d = dict(scheme_code="119551", scheme_name="Test Fund - Regular - Growth",
             order_type="PURCHASE", amount=5000, confirmed=True,
             idempotency_key=f"key-{_n[0]:020d}")
    d.update(kw)
    return d


def post(client_id=1, owner=OWNER, **kw):
    return http.post(f"/crm/{owner}/clients/{client_id}/orders", json=body(**kw))


failures = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if not cond else ""))
    if not cond:
        failures.append(name)


for k in ("ORDERS_OWNER_EMAIL", "NSE_MODE", "ORDERS_MAX_AMOUNT"):
    os.environ.pop(k, None)

# 1 happy path
r = post()
j = r.json()
check("purchase placed", r.status_code == 200 and j["status"] == "SUBMITTED"
      and j["nse_order_ref"].startswith("STUB-") and j["mode"] == "STUB", r.text)
oid = j["id"]
ev = http.get(f"/crm/{OWNER}/orders/{oid}").json()["events"]
check("audit trail CREATED+SUBMITTED", [e["event"] for e in ev][:2] == ["CREATED", "SUBMITTED"], str(ev))

# 2 duplicate key
b = body()
r1 = http.post(f"/crm/{OWNER}/clients/1/orders", json=b).json()
r2 = http.post(f"/crm/{OWNER}/clients/1/orders", json=b).json()
check("duplicate key returns same order", r2["duplicate"] is True and r1["id"] == r2["id"])
check("only one row for that key", sum(1 for o in sb.tables["crm_orders"]
      if o["idempotency_key"] == b["idempotency_key"]) == 1)

# 3 validation
check("unconfirmed rejected", post(confirmed=False).status_code == 400)
check("bad order type rejected", post(order_type="BUYY").status_code == 400)
check("short idempotency key rejected", post(idempotency_key="abc").status_code == 400)
check("amount below minimum rejected", post(amount=50).status_code == 400)
check("zero amount rejected", post(amount=0).status_code == 400)
check("amount above safety limit rejected", post(amount=5_000_000).status_code == 400)
check("missing scheme rejected", post(scheme_code=" ").status_code == 400)

# 4 KYC / ownership
r = post(client_id=2)
check("pending KYC blocked", r.status_code == 409 and "KYC" in r.text, r.text)
check("other advisor's client is 404", post(client_id=3).status_code == 404)
check("no order row saved for blocked ones",
      all(o["client_id"] == 1 for o in sb.tables["crm_orders"]))

# 5 SIP
check("SIP needs day", post(order_type="SIP", amount=1000, sip_installments=12).status_code == 400)
check("SIP day 29 rejected", post(order_type="SIP", amount=1000, sip_day=29, sip_installments=12).status_code == 400)
r = post(order_type="SIP", amount=1000, sip_day=5, sip_installments=12)
check("SIP placed", r.status_code == 200 and r.json()["sip_day"] == 5, r.text)

# 6 redeem
check("redeem both amount+units rejected", post(order_type="REDEEM", amount=1000, units=5).status_code == 400)
check("redeem neither rejected", post(order_type="REDEEM", amount=None).status_code == 400)
r = post(order_type="REDEEM", amount=None, units=12.5)
check("redeem by units placed", r.status_code == 200 and r.json()["units"] == 12.5 and r.json()["amount"] is None, r.text)

# 6b redemption vs holdings (100 units held, NAV 50)
open_redeem = lambda: [o for o in sb.tables["crm_orders"] if o["order_type"] == "REDEEM"
                       and o["status"] not in ("ALLOTTED", "FAILED", "CANCELLED")]
for o in sb.tables["crm_orders"]:      # settle everything placed so far
    o["status"] = "ALLOTTED"
r = post(order_type="REDEEM", amount=None, units=150)
check("redeem more units than held -> 409", r.status_code == 409 and "100.000" in r.text, r.text)
r = post(order_type="REDEEM", amount=6000)
check("redeem amount worth more than held -> 409", r.status_code == 409, r.text)
r = post(order_type="REDEEM", amount=1000, scheme_code="999999", scheme_name="Other Fund")
check("redeem fund with no holding -> 409", r.status_code == 409 and "Holdings" in r.text, r.text)
r = post(client_id=2, order_type="REDEEM", amount=None, units=1)
check("pending-KYC client still blocked first", r.status_code == 409 and "KYC" in r.text, r.text)
r = post(order_type="REDEEM", amount=3000)       # 60 units at NAV 50
first = r.json()
check("redeem within holding placed", r.status_code == 200 and first["status"] == "SUBMITTED", r.text)
r = post(order_type="REDEEM", amount=None, units=50)  # 60 pending + 50 > 100
check("open redemption reserves units -> 409", r.status_code == 409 and "open redemption" in r.text, r.text)
r = post(order_type="REDEEM", amount=None, units=40)  # exactly the remaining 40
check("remaining units can be redeemed", r.status_code == 200, r.text)
for o in sb.tables["crm_orders"]:
    o["status"] = "CANCELLED" if o["order_type"] == "REDEEM" else o["status"]
NAV["value"] = None
r = post(order_type="REDEEM", amount=1000)
check("amount redemption without NAV -> 503", r.status_code == 503, r.text)
r = post(order_type="REDEEM", amount=None, units=5)
check("units redemption works without NAV", r.status_code == 200, r.text)
NAV["value"] = (50.0, "07-10-2026")
for o in sb.tables["crm_orders"]:
    if o["order_type"] == "REDEEM":
        o["status"] = "ALLOTTED"

# 7 simulated rejection
r = post(amount=111)
check("test amount 111 fails with reason", r.json()["status"] == "FAILED" and "Simulated" in r.json()["failure_reason"], r.text)

# 8 status progression
oid = post().json()["id"]
row = next(o for o in sb.tables["crm_orders"] if o["id"] == oid)
row["created_at"] = (datetime.now(timezone.utc) - timedelta(seconds=12)).isoformat()
lst = http.get(f"/crm/{OWNER}/clients/1/orders").json()
check("status advances to CONFIRMED", next(o for o in lst["orders"] if o["id"] == oid)["status"] == "CONFIRMED")
row["created_at"] = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
d = http.get(f"/crm/{OWNER}/orders/{oid}").json()
check("status advances to ALLOTTED", d["order"]["status"] == "ALLOTTED")
check("status changes audited", sum(e["event"] == "STATUS_CHANGED" for e in d["events"]) == 2)

# 9 cancel
open_id = post().json()["id"]
r = http.post(f"/crm/{OWNER}/orders/{open_id}/cancel")
check("open order cancels", r.status_code == 200 and r.json()["status"] == "CANCELLED", r.text)
check("cancelled twice -> 409", http.post(f"/crm/{OWNER}/orders/{open_id}/cancel").status_code == 409)
check("allotted can't cancel", http.post(f"/crm/{OWNER}/orders/{oid}/cancel").status_code == 409)

# 10 owner guard
os.environ["ORDERS_OWNER_EMAIL"] = "me@example.com"
check("wrong owner gets 403", post().status_code == 403)
check("list also guarded", http.get(f"/crm/{OWNER}/clients/1/orders").status_code == 403)
os.environ.pop("ORDERS_OWNER_EMAIL")

# 11 live mode refuses
before = len(sb.tables["crm_orders"])
os.environ["NSE_MODE"] = "live"
r = post()
check("live without owner email -> 500", r.status_code == 500, r.text)
os.environ["ORDERS_OWNER_EMAIL"] = OWNER
r = post()
check("live not implemented -> 501", r.status_code == 501, r.text)
check("live refusal creates no order", len(sb.tables["crm_orders"]) == before)
for k in ("ORDERS_OWNER_EMAIL", "NSE_MODE"):
    os.environ.pop(k, None)

# 12 configurable cap
os.environ["ORDERS_MAX_AMOUNT"] = "2000"
check("cap honoured", post(amount=2500).status_code == 400 and post(amount=1500).status_code == 200)
os.environ.pop("ORDERS_MAX_AMOUNT")

# 13 timestamp parsing
for s in ("2026-10-07T10:00:00.123456+00:00", "2026-10-07T10:00:00.12345+00:00",
          "2026-10-07T10:00:00+00:00", "2026-10-07 10:00:00.5Z"):
    try:
        nse_service.parse_ts(s)
        ok = True
    except Exception as e:
        ok = False
    check(f"parse_ts {s}", ok)

print()
print("ALL PASSED" if not failures else f"{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
