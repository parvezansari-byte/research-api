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

# ---- full stock list for an index
m._cache.clear()
full = m.get_index_stocks("nifty 50", good, now=100.0)
check("index-stocks lists every stock, best first", [s["symbol"] for s in full["stocks"]] == ["INFY", "RELIANCE", "ITC", "TCS", "HDFCBANK"])
check("index-stocks counts", (full["rising"], full["falling"], full["unchanged"], full["count"]) == (2, 2, 1, 5))
calls2 = []
def good2(idx): calls2.append(idx); return PAYLOAD
m._cache.clear()
m.get_movers("NIFTY 50", 3, good2, now=1.0)
m.get_index_stocks("NIFTY 50", good2, now=2.0)
check("movers and index-stocks share one NSE call", len(calls2) == 1)
check("movers reply stays small (no full list)", "stocks" not in m.get_movers("NIFTY 50", 3, good2, now=3.0))
try:
    m.get_index_stocks("BANKNIFTY", good2); ok = False
except HTTPException as e:
    ok = e.status_code == 400
check("index-stocks validates the index", ok)
# NSE URL keeps %20 for spaces
seen = {}
class FakeSession:
    def __init__(self): self.headers = {}
    def get(self, url, **kw):
        seen["url"] = url
        class R: status_code = 200
        R.json = staticmethod(lambda: PAYLOAD)
        return R
import importlib.util
spec = importlib.util.spec_from_file_location("movers_real", m.__file__)
mm = importlib.util.module_from_spec(spec); spec.loader.exec_module(mm)      # fresh copy with the real fetch_nse
mm.requests.Session = FakeSession
mm.fetch_nse("NIFTY 50")
check("NSE URL encodes the space as %20, not +", seen["url"].endswith("?index=NIFTY%2050"))
r = TestClient(app).get("/market/index-stocks", params={"index": "NIFTY 50"})
check("HTTP index-stocks", r.status_code == 200 and r.json()["count"] == 5)

# ---- Yahoo fallback when NSE refuses
import movers_api as mv
check("constituents: Nifty 50 has 50", len(mv.constituents("NIFTY 50")) == 50)
check("constituents: Nifty 500 is the three lists, no duplicates", 480 <= len(mv.constituents("NIFTY 500")) <= 520 and len(set(mv.constituents("NIFTY 500"))) == len(mv.constituents("NIFTY 500")))
check("constituents: midcap and smallcap lists load", len(mv.constituents("NIFTY MIDCAP 150")) > 100 and len(mv.constituents("NIFTY SMALLCAP 250")) > 200)
check("constituents: unknown index empty", mv.constituents("X") == [])
HIST = {"AAA": [("2026-10-26", 100.0), ("2026-10-27", 103.0)], "BBB": [("2026-10-26", 50.0), ("2026-10-27", 48.0)],
        "CCC": [("2026-10-27", 10.0)], "DDD": [("2026-10-26", 0.0), ("2026-10-27", 5.0)]}
pay = mv.yahoo_payload("NIFTY 50", history_fetch=lambda syms: HIST)
parsed = mv.parse_movers(pay, "NIFTY 50", 10)
check("Yahoo payload parses like NSE's", [s["symbol"] for s in parsed["gainers"]] == ["AAA"] and [s["symbol"] for s in parsed["losers"]] == ["BBB"])
check("source and note carried", parsed["source"] == "Yahoo" and "delayed" in parsed["note"])
check("midcap 100 note says which list is shown", "Midcap 150" in mv.yahoo_payload("NIFTY MIDCAP 100", history_fetch=lambda s: HIST)["_note"])
try:
    mv.yahoo_payload("NIFTY 50", history_fetch=lambda syms: {}); ok = False
except mv.MoversError:
    ok = True
check("Yahoo with nothing -> MoversError", ok)

orig_nse, orig_y = mv.fetch_nse, mv.yahoo_payload
mv.fetch_nse = lambda idx: (_ for _ in ()).throw(mv.MoversError("NSE answered 404"))
mv.yahoo_payload = lambda idx, history_fetch=None: pay
check("fetch_best falls back to Yahoo when NSE fails", mv.fetch_best("NIFTY 50")["_source"] == "Yahoo")
mv.yahoo_payload = lambda idx, history_fetch=None: (_ for _ in ()).throw(mv.MoversError("Yahoo returned no prices"))
try:
    mv.fetch_best("NIFTY 50"); ok = False
except mv.MoversError as e:
    ok = "404" in str(e) and "Yahoo returned no prices" in str(e)
check("both failing reports both reasons", ok)
mv.fetch_nse = lambda idx: PAYLOAD
check("fetch_best uses NSE when it works", mv.fetch_best("NIFTY 50")["timestamp"] == PAYLOAD["timestamp"])
mv.fetch_nse, mv.yahoo_payload = orig_nse, orig_y
mv._cache.clear()
full = mv.get_index_stocks("NIFTY 50", lambda idx: pay, now=1.0)
check("index-stocks passes the source and note through", full["source"] == "Yahoo" and "delayed" in full["note"])
print("\nALL PASSED" if not fails else f"\nFAILED: {fails}")
raise SystemExit(1 if fails else 0)
