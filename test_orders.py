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
from orders_api import register_orders


class FakeResult:
    def __init__(self, data):
        self.data = data


class FakeQuery:
    def __init__(self, db, name):
        self.db, self.name = db, name
        self.rows = db.tables.setdefault(name, [])
        self.filters, self.mode, self.payload = [], "select", None
        self.order_by = None

    def select(self, *_):
        self.mode = "select"
        return self

    def eq(self, col, val):
        self.filters.append((col, val))
        return self

    def order(self, col, desc=False):
        self.order_by = (col, desc)
        return self

    def insert(self, row):
        self.mode, self.payload = "insert", row
        return self

    def update(self, fields):
        self.mode, self.payload = "update", fields
        return self

    def _match(self):
        return [r for r in self.rows if all(r.get(c) == v for c, v in self.filters)]

    def execute(self):
        if self.mode == "insert":
            row = dict(self.payload)
            if self.name == "crm_orders":
                for r in self.rows:
                    if (r["owner_email"], r["idempotency_key"]) == (
                            row["owner_email"], row["idempotency_key"]):
                        raise Exception("duplicate key value violates unique constraint")
            row["id"] = len(self.rows) + 1
            row.setdefault("created_at", datetime.now(timezone.utc).isoformat())
            self.rows.append(row)
            return FakeResult([dict(row)])
        if self.mode == "update":
            hit = self._match()
            for r in hit:
                r.update(self.payload)
            return FakeResult([dict(r) for r in hit])
        out = [dict(r) for r in self._match()]
        if self.order_by:
            col, desc = self.order_by
            out.sort(key=lambda r: str(r.get(col)), reverse=desc)
        return FakeResult(out)


class FakeSb:
    def __init__(self):
        self.tables = {}

    def table(self, name):
        return FakeQuery(self, name)


OWNER = "adv@example.com"
sb = FakeSb()
sb.tables["crm_clients"] = [
    {"id": 1, "owner_email": OWNER, "kyc_status": "KYC_VALIDATED", "nse_client_code": "C1"},
    {"id": 2, "owner_email": OWNER, "kyc_status": "PENDING", "nse_client_code": None},
    {"id": 3, "owner_email": "other@example.com", "kyc_status": "KYC_VALIDATED"},
]
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
