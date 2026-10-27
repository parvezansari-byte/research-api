"""Tests for breadth_api (run: python test_breadth.py)."""
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import breadth_api as b

fails = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond: fails.append(name)

PAYLOAD = {"timestamp": "27-Oct-2026 15:30:00", "data": [
    {"index": "NIFTY 50", "advances": "32", "declines": 17, "unchanged": "1"},
    {"index": "NIFTY 500", "advances": "270", "declines": "210", "unchanged": 20},
    {"index": "NIFTY BANK", "advances": 5, "declines": 7, "unchanged": 0},
    {"index": "NIFTY SMALLCAP 100", "advances": "40", "declines": "58", "unchanged": "2"},
    "junk",
]}

d = b.parse_breadth(PAYLOAD)
names = [i["name"] for i in d["indices"]]
check("only the wanted indices, in order", names == ["Nifty 50", "Nifty Smallcap 100", "Nifty 500"])
n50 = d["indices"][0]
check("numbers parsed from strings", (n50["advances"], n50["declines"], n50["unchanged"]) == (32, 17, 1))
check("adv_pct", n50["adv_pct"] == 64.0)
check("as_of kept", d["as_of"] == "27-Oct-2026 15:30:00")
for bad in ({}, {"data": "x"}, {"data": []}, {"data": [{"index": "NIFTY 50", "advances": None, "declines": None}]}, None):
    try:
        b.parse_breadth(bad); ok = False
    except b.BreadthError:
        ok = True
    check(f"bad payload rejected: {str(bad)[:30]}", ok)

# caching: fresh, stale fallback, 503
b._cache.update(at=0.0, data=None)
calls = []
def good():
    calls.append(1); return PAYLOAD
r1 = b.get_breadth(good, now=1000.0)
r2 = b.get_breadth(good, now=1100.0)
check("second call within 5 min served from cache", len(calls) == 1 and r2["stale"] is False)
def boom(): raise b.BreadthError("NSE answered 403")
r3 = b.get_breadth(boom, now=1400.0)
check("NSE failing after cache expiry serves stale", r3["stale"] is True and r3["indices"])
try:
    b.get_breadth(boom, now=1000.0 + 4000); ok = False
except HTTPException as e:
    ok = e.status_code == 503 and "403" in e.detail
check("too old -> 503 with reason", ok)
b._cache.update(at=0.0, data=None)
def weird(): raise ValueError("kaboom")
try:
    b.get_breadth(weird, now=5.0); ok = False
except HTTPException as e:
    ok = e.status_code == 503
check("unexpected error -> 503 not 500", ok)

app = FastAPI(); b.register_breadth(app)
b._cache.update(at=0.0, data=None)
orig = b.fetch_nse
b.fetch_nse = lambda: PAYLOAD
b.get_breadth.__defaults__ = (b.fetch_nse, None)
c = TestClient(app)
r = c.get("/market/breadth")
check("HTTP 200 with indices", r.status_code == 200 and len(r.json()["indices"]) == 3)
print("\nALL PASSED" if not fails else f"\nFAILED: {fails}")
raise SystemExit(1 if fails else 0)
