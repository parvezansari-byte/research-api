"""Tests for watchlist_api (run: python test_watchlist.py)."""
from datetime import datetime
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import watchlist_api as w

fails = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond: fails.append(name)

IST = w.IST
OPEN = datetime(2026, 10, 27, 11, 0, tzinfo=IST)       # Tuesday 11:00
PRE = datetime(2026, 10, 27, 9, 0, tzinfo=IST)
CLOSED = datetime(2026, 10, 27, 18, 0, tzinfo=IST)
SAT = datetime(2026, 10, 24, 11, 0, tzinfo=IST)

check("market hours", w.market_open(OPEN) and not w.market_open(PRE) and not w.market_open(CLOSED) and not w.market_open(SAT))
check("symbols parsed", w.parse_symbols("reliance, TCS.NS,tcs,M&M") == ["RELIANCE", "TCS", "M&M"])
for bad in ("", " , ", "A B", "X" * 25, ",".join(f"S{i}" for i in range(30))):
    try:
        w.parse_symbols(bad); ok = False
    except HTTPException as e:
        ok = e.status_code == 400
    check(f"rejects {bad[:20]!r}", ok)

H = {"AAA": [("2026-10-23", 100.0), ("2026-10-26", 110.0), ("2026-10-27", 112.0)],
     "BBB": [("2026-10-23", 50.0), ("2026-10-26", 60.0)]}
q = w.build_quotes(["AAA"], H, CLOSED)["quotes"][0]
check("after close: last vs previous close", q["price"] == 112.0 and q["change"] == 2.0 and q["change_pct"] == 1.82 and not q["realtime"])
q = w.build_quotes(["BBB"], H, SAT)["quotes"][0]
check("weekend: last session vs the one before", q["price"] == 60.0 and q["change"] == 10.0 and q["change_pct"] == 20.0)
r = w.build_quotes(["AAA"], H, OPEN, lambda s: 115.0)
q = r["quotes"][0]
check("open + Dhan: live price, change vs prior close", r["market_open"] and q["realtime"] and q["price"] == 115.0 and q["change"] == 5.0)
q = w.build_quotes(["BBB"], H, OPEN, lambda s: 63.0)["quotes"][0]
check("open, today's bar missing: prior close = last bar", q["price"] == 63.0 and q["change"] == 3.0 and q["realtime"])
q = w.build_quotes(["AAA"], H, OPEN, lambda s: None)["quotes"][0]
check("open, Dhan fails: falls back to Yahoo", not q["realtime"] and q["price"] == 112.0 and q["change"] == 2.0)
r = w.build_quotes(["AAA", "ZZZ"], H, CLOSED)
check("unknown symbol reported missing", r["missing"] == ["ZZZ"] and len(r["quotes"]) == 1)
r = w.build_quotes(["ZZZ"], {}, OPEN, lambda s: 10.0)
check("Dhan-only symbol still shown, no change", r["quotes"][0]["price"] == 10.0 and r["quotes"][0]["change"] is None)

w._cache.clear()
calls = []
def hf(syms): calls.append(1); return H
d1 = w.get_quotes("aaa,bbb", hf, lambda s: None, CLOSED, clock=100.0)
d2 = w.get_quotes("AAA,BBB", hf, lambda s: None, CLOSED, clock=105.0)
check("cached for 15s", len(calls) == 1 and d1 is d2)
w.get_quotes("AAA,BBB", hf, lambda s: None, CLOSED, clock=130.0)
check("refetched after 15s", len(calls) == 2)
def boom(s): raise RuntimeError("yahoo down")
r = w.get_quotes("AAA,BBB", boom, lambda s: None, CLOSED, clock=200.0)
check("history failure serves stale", r.get("stale") is True and r["quotes"])
w._cache.clear()
try:
    w.get_quotes("AAA", boom, lambda s: None, CLOSED, clock=1.0); ok = False
except HTTPException as e:
    ok = e.status_code == 503
check("nothing available -> 503", ok)

app = FastAPI(); w.register_watchlist(app)
w._cache.clear()
w.fetch_yahoo = lambda syms: H
w.fetch_dhan_ltp = lambda s: None
c = TestClient(app)
r = c.get("/market/quotes", params={"symbols": "AAA,BBB"})
check("HTTP 200", r.status_code == 200 and len(r.json()["quotes"]) == 2)
check("HTTP 400", c.get("/market/quotes", params={"symbols": "bad symbol"}).status_code == 400)
print("\nALL PASSED" if not fails else f"\nFAILED: {fails}")
raise SystemExit(1 if fails else 0)
