"""Tests for movers_api (run: python test_movers.py)."""
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import movers_api as m

fails = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond: fails.append(name)

def row(sym, price, pct, chg=0.0, **kw):
    return dict(symbol=sym, lastPrice=price, pChange=pct, change=chg, **kw)

PAYLOAD = {"timestamp": "27-Oct-2026 15:30:00", "data": [
    row("NIFTY 500", 22000, 0.4, 80, priority=1),
    row("RELIANCE", "2,950.5", 2.5, 71),
    row("TCS", 4100, -1.2, -50),
    row("INFY", 1500, "3.1", 45),
    row("HDFCBANK", 1600, -3.4, -56),
    row("ITC", 450, 0, 0),
    row("BADROW", None, 1.0),
    row("ZEROPX", 0, 5.0),
    "junk",
]}

d = m.parse_movers(PAYLOAD, "NIFTY 500", 10)
check("index row skipped", all(s["symbol"] != "NIFTY 500" for s in d["gainers"] + d["losers"]))
check("gainers sorted high to low", [s["symbol"] for s in d["gainers"]] == ["INFY", "RELIANCE"])
check("losers sorted worst first", [s["symbol"] for s in d["losers"]] == ["HDFCBANK", "TCS"])
check("flat stock in neither list", all(s["symbol"] != "ITC" for s in d["gainers"] + d["losers"]))
check("bad rows dropped, comma price parsed", d["count"] == 5 and d["gainers"][1]["price"] == 2950.5)
check("company name looked up", m.company_names().get("RELIANCE", "").lower().startswith("reliance"))
check("n limits", len(m.parse_movers(PAYLOAD, "NIFTY 500", 1)["gainers"]) == 1)
for bad in ({}, {"data": []}, {"data": "x"}, None):
    try:
        m.parse_movers(bad, "NIFTY 500"); ok = False
    except m.MoversError:
        ok = True
    check(f"bad payload rejected: {str(bad)[:20]}", ok)

m._cache.clear()
calls = []
def good(idx): calls.append(idx); return PAYLOAD
r = m.get_movers("nifty 500", 1, good, now=100.0)
check("lowercase index accepted, n applied", r["index"] == "NIFTY 500" and len(r["gainers"]) == 1 and not r["stale"])
r2 = m.get_movers("NIFTY 500", 2, good, now=150.0)
check("cache reused across different n", len(calls) == 1 and len(r2["gainers"]) == 2)
def boom(idx): raise m.MoversError("NSE answered 403")
r3 = m.get_movers("NIFTY 500", 5, boom, now=400.0)
check("stale served when NSE fails", r3["stale"] and r3["gainers"])
try:
    m.get_movers("NIFTY 500", 5, boom, now=100.0 + 4000); ok = False
except HTTPException as e:
    ok = e.status_code == 503 and "403" in e.detail
check("too old -> 503", ok)
for args in (("BANKNIFTY", 5), ("NIFTY 50", 0), ("NIFTY 50", 99)):
    try:
        m.get_movers(*args, good); ok = False
    except HTTPException as e:
        ok = e.status_code == 400
    check(f"validation {args}", ok)

app = FastAPI(); m.register_movers(app)
m._cache.clear()
m.fetch_nse = lambda idx: PAYLOAD
m.get_movers.__kwdefaults__ = None
c = TestClient(app)
r = c.get("/market/movers", params={"index": "NIFTY 50", "n": 5})
check("HTTP 200", r.status_code == 200 and r.json()["gainers"][0]["symbol"] == "INFY")
check("HTTP 400 on bad index", c.get("/market/movers", params={"index": "X"}).status_code == 400)
print("\nALL PASSED" if not fails else f"\nFAILED: {fails}")
raise SystemExit(1 if fails else 0)
