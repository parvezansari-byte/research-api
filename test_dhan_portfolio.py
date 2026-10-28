import os
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

import dhan_portfolio_api as d

ok = bad = 0
def check(c, m):
    global ok, bad
    if c: ok += 1
    else: bad += 1; print("FAIL", m)

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 10, 28, 11, 0, tzinfo=IST)           # Wednesday, market open
H = [{"tradingSymbol": "TCS", "totalQty": 10, "avgCostPrice": 3000},
     {"tradingSymbol": "RELIANCE", "totalQty": 20, "avgCostPrice": 1000},
     {"tradingSymbol": "OBSCURE", "totalQty": 5, "avgCostPrice": 200},
     {"tradingSymbol": "ZERO", "totalQty": 0, "avgCostPrice": 10},
     {"tradingSymbol": "", "totalQty": 3, "avgCostPrice": 10}]
Q = {"TCS": {"symbol": "TCS", "price": 3300, "change": 30, "change_pct": 0.92, "realtime": True},
     "RELIANCE": {"symbol": "RELIANCE", "price": 900, "change": -10, "change_pct": -1.1, "realtime": True}}
r = d.build_portfolio(H, lambda s: {k: v for k, v in Q.items() if k in s}, NOW)
t = r["totals"]
check(t["holdings"] == 3 and t["priced"] == 2, f"counts {t}")
check(t["invested"] == 10 * 3000 + 20 * 1000 + 5 * 200 and t["priced_invested"] == 50000, "invested")
check(t["value"] == 33000 + 18000 and t["pnl"] == 1000 and t["pnl_pct"] == 2.0, f"pnl {t}")
check(t["day_change"] == 300 - 200 and t["day_change_pct"] is not None, "day change")
check([h["symbol"] for h in r["holdings"]] == ["TCS", "RELIANCE", "OBSCURE"], "sorted, unpriced last")
check(abs(sum(h["weight_pct"] for h in r["holdings"] if h["weight_pct"]) - 100) < 0.2, "weights")
tcs = r["holdings"][0]
check(tcs["pnl"] == 3000 and tcs["pnl_pct"] == 10.0 and tcs["realtime"], "tcs row")
check(r["holdings"][2]["price"] is None and r["holdings"][2]["pnl"] is None, "unpriced row")
check(any("OBSCURE" in n for n in r["notes"]) and any("%" in n for n in r["notes"]), f"notes {r['notes']}")
check(r["market_open"] is True, "market open")
e = d.build_portfolio([], lambda s: {}, NOW)
check(e["totals"]["holdings"] == 0 and e["totals"]["pnl_pct"] is None and e["holdings"] == [], "empty")
r2 = d.build_portfolio([{"trading_symbol": "TCS", "quantity": 2, "average_price": 100}], lambda s: {"TCS": {"price": 150}}, NOW)
check(r2["holdings"][0]["pnl"] == 100 and r2["holdings"][0]["day_change"] is None, "alt field names, no change")
check(any("last close" in n for n in r2["notes"]), "delayed note")
r3 = d.build_portfolio([{"tradingSymbol": "A", "totalQty": "x", "avgCostPrice": None}], lambda s: {}, NOW)
check(r3["holdings"] == [], "bad numbers skipped")
# default_quotes survives a failing source
import watchlist_api as wl
orig = wl.get_quotes
wl.get_quotes = lambda raw: (_ for _ in ()).throw(d.HTTPException(503, "x"))
check(d.default_quotes(["TCS", "BAD SYM"]) == {}, "quotes failure -> empty")
wl.get_quotes = lambda raw: {"quotes": [{"symbol": s, "price": 1} for s in raw.split(",")]}
check(set(d.default_quotes(["A%d" % i for i in range(60)])) == {"A%d" % i for i in range(60)}, "chunks of 25")
wl.get_quotes = orig
# http
os.environ.pop("TRADING_OWNER_EMAIL", None)
app = FastAPI(); d.register_dhan_portfolio(app, lambda: H, lambda s: {k: v for k, v in Q.items() if k in s}); c = TestClient(app)
r = c.get("/trading/me@x.com/portfolio")
check(r.status_code == 200 and r.json()["totals"]["priced"] == 2, "http ok")
os.environ["TRADING_OWNER_EMAIL"] = "owner@x.com"
check(c.get("/trading/me@x.com/portfolio").status_code == 403, "owner guard")
check(c.get("/trading/OWNER@x.com/portfolio").status_code == 200, "owner passes, case-insensitive")
def boom(): raise RuntimeError("dhan down")
app2 = FastAPI(); d.register_dhan_portfolio(app2, boom); c2 = TestClient(app2)
r = c2.get("/trading/owner@x.com/portfolio")
check(r.status_code == 502 and "dhan down" in r.json()["detail"], "dhan error -> 502")
os.environ.pop("TRADING_OWNER_EMAIL")
print(f"{ok} passed, {bad} failed")
raise SystemExit(1 if bad else 0)
