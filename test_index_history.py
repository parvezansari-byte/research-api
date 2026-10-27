"""Tests for index_history_api (run: python test_index_history.py)."""
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import index_history_api as h

fails = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond: fails.append(name)

SERIES = [("2026-10-01", 100.0), ("2026-10-02", None), ("2026-10-03", 110.0), ("2026-10-06", float("nan")),
          ("2026-10-07", 90.0), ("2026-10-08", 105.0)]
b = h.build("NIFTY 50", "1M", SERIES)
check("bad points dropped", [p["d"] for p in b["points"]] == ["2026-10-01", "2026-10-03", "2026-10-07", "2026-10-08"])
check("change from first to last", b["first"] == 100.0 and b["last"] == 105.0 and b["change"] == 5.0 and b["change_pct"] == 5.0)
check("high and low", b["high"] == 110.0 and b["low"] == 90.0)
for bad in ([], [("2026-10-01", 100.0)], [("2026-10-01", None), ("2026-10-02", 0)]):
    try:
        h.build("X", "1M", bad); ok = False
    except ValueError:
        ok = True
    check(f"too little history rejected ({len(bad)})", ok)

h._cache.clear()
calls = []
def good(sym, p, i):
    calls.append((sym, p, i)); return SERIES
r = h.get_history("nifty 50", "1m", good, now=100.0)
check("symbol and yahoo period mapped", calls == [("^NSEI", "1mo", "1d")] and not r["stale"])
h.get_history("NIFTY 50", "1M", good, now=200.0)
check("cache within 10 minutes", len(calls) == 1)
h.get_history("NIFTY 50", "5Y", good, now=200.0)
check("5Y uses weekly candles", calls[-1] == ("^NSEI", "5y", "1wk"))
def boom(*a): raise RuntimeError("yahoo down")
r = h.get_history("NIFTY 50", "1M", boom, now=900.0)
check("stale served on failure", r["stale"] and r["points"])
try:
    h.get_history("NIFTY 50", "1M", boom, now=100.0 + 7 * 3600); ok = False
except HTTPException as e:
    ok = e.status_code == 503
check("too old -> 503", ok)
for args in (("DOW", "1M"), ("NIFTY 50", "2D")):
    try:
        h.get_history(*args, good); ok = False
    except HTTPException as e:
        ok = e.status_code == 400
    check(f"validation {args}", ok)

app = FastAPI(); h.register_index_history(app)
h._cache.clear()
h.fetch_yahoo = lambda s, p, i: SERIES
c = TestClient(app)
import index_history_api
r = c.get("/indices/history", params={"name": "SENSEX", "period": "3M"})
check("HTTP 200 shape", r.status_code == 200 and r.json()["name"] == "SENSEX" and len(r.json()["points"]) == 4)
check("HTTP 400", c.get("/indices/history", params={"name": "X"}).status_code == 400)
print("\nALL PASSED" if not fails else f"\nFAILED: {fails}")
raise SystemExit(1 if fails else 0)
