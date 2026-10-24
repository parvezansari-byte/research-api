"""Tests for allocation_api.py against the real funds_data.json. Run: python test_allocation.py"""
import math

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import allocation_api as al

failures = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if not cond else ""))
    if not cond:
        failures.append(name)


# ---- rules
check("balanced 10y = 60", al.auto_equity_pct("balanced", 10) == 60)
check("short horizon cuts equity", al.auto_equity_pct("balanced", 3) == 30 and al.auto_equity_pct("aggressive", 2) == 50)
check("long horizon adds 5", al.auto_equity_pct("balanced", 15) == 65)
check("always 10..90", all(10 <= al.auto_equity_pct(r, y) <= 90 for r in al.RISK_BASE for y in range(1, 41)))
check("conservative 1y = 10 (floor)", al.auto_equity_pct("conservative", 1) == 10)

# ---- math: closed forms
check("lump sum", abs(al.future_value("lumpsum", 100000, 12, 10) - 100000 * 1.12 ** 10) < 1e-6)
rm = 1.12 ** (1 / 12) - 1
check("SIP closed form", abs(al.future_value("sip", 10000, 12, 10) - 10000 * ((1 + rm) ** 120 - 1) / rm) < 1e-6)
check("SIP at 0% is just the money in", al.future_value("sip", 10000, 0, 5) == 600000)
check("invested", al.invested("sip", 5000, 10) == 600000 and al.invested("lumpsum", 5000, 10) == 5000)

# ---- real data picks
eq, de = al.pick_auto("equity", "balanced", 10), al.pick_auto("debt", "balanced", 10)
check("auto equity pick exists and is eligible", eq and eq["aum"] >= 1000 and eq["r_3y"] is not None and al.category_of(eq) in al.EQUITY_AUTO, str(eq and eq["name"]))
check("auto debt pick exists and is eligible", de and de["aum"] >= 1000 and al.category_of(de) in al.DEBT_AUTO_LONG, str(de and de["name"]))
best = max((f for f in al.load_funds() if f["sheet"] == "equity" and al.category_of(f) in al.EQUITY_AUTO and al.suitable(f)
            and (f.get("aum") or 0) >= 1000 and f.get("r_3y") is not None and f.get("sharpe") is not None), key=lambda f: (f["sharpe"], f["r_3y"]))
check("auto pick is the top Sharpe", best["name"] == eq["name"])
check("short horizon debt pool", al.category_of(al.pick_auto("debt", "balanced", 2)) in al.DEBT_AUTO_SHORT)
print("   picked:", eq["name"][:60], "|", de["name"][:60])

check("no retirement / ELSS style funds picked", all(al.suitable(al.pick_auto(b, r, y)) for b in ("equity", "debt") for r in al.RISK_BASE for y in (2, 10)))
check("lock-in funds not offered", not any(any(w in f["name"].lower() for w in al.EXCLUDED_WORDS) for b in al.build_options().values() for f in b))
check("suitable() rules", not al.suitable({"name": "X Retirement Fund"}) and al.suitable({"name": "X Flexi Cap Fund"}) and not al.suitable({"name": "Y", "fund_type": "Close Ended"}))

# ---- returns
r = al.fund_return({"r_5y": 14.0, "r_3y": 20.0, "std_dev": 12}, "equity")
check("5y preferred over 3y, blended with assumption", r["cagr"] == 13.0 and r["historical"] == 14.0 and r["basis"] == "5-year" and not r["hypothetical"], str(r))
r = al.fund_return({"r_3y": 9.0}, "equity")
check("3y used when no 5y", r["basis"] == "3-year" and r["cagr"] == 10.5 and r["historical"] == 9.0)
r = al.fund_return({"r_5y": 41.0}, "equity")
check("lucky returns are capped", r["cagr"] == 18.0 and r["capped"] and r["historical"] == 41.0)
r = al.fund_return({"r_5y": -30.0}, "equity")
check("floor applies", r["cagr"] == 4.0 and r["capped"])
r = al.fund_return({}, "equity")
check("hypothetical when no history", r["hypothetical"] and r["cagr"] == 12.0 and r["vol"] == 15.0 and r["historical"] is None)
r = al.fund_return({"ytm": 7.4, "expense_ratio": 0.4}, "debt")
check("debt falls back to yield", r["basis"] == "current yield" and r["cagr"] == 7.0 and r["historical"] == 7.0, str(r))
check("debt hypothetical", al.fund_return(None, "debt")["cagr"] == 7.0)

# ---- full plans
p = al.build_plan("sip", 10000, 10)
check("auto split balanced 10y", p["equity_pct"] == 60 and p["debt_pct"] == 40 and p["auto_split"])
check("amounts add up", p["split"]["equity"]["amount"] + p["split"]["debt"]["amount"] == 10000 and p["split"]["equity"]["amount"] == 6000)
ps = p["projection"]
check("projection years 0..10", [x["year"] for x in ps] == list(range(11)))
check("starts at zero for SIP", ps[0]["invested"] == 0 and ps[0]["base"] == 0)
check("ends: low < base < high and base > invested", ps[-1]["low"] < ps[-1]["base"] < ps[-1]["high"] and ps[-1]["base"] > ps[-1]["invested"], str(ps[-1]))
check("invested = 12 x 10 x amount", ps[-1]["invested"] == 1200000 and p["summary"]["invested"] == 1200000)
check("monotonic base", all(ps[i]["base"] <= ps[i + 1]["base"] for i in range(10)))
pf = p["returns"]["portfolio"]
check("blend of two funds", abs(pf["base"] - round(0.6 * p["returns"]["equity"]["cagr"] + 0.4 * p["returns"]["debt"]["cagr"], 2)) < 0.011)
check("band shrinks with time", al.build_plan("sip", 10000, 5)["returns"]["portfolio"]["high"] - al.build_plan("sip", 10000, 5)["returns"]["portfolio"]["base"]
      > al.build_plan("sip", 10000, 20)["returns"]["portfolio"]["high"] - al.build_plan("sip", 10000, 20)["returns"]["portfolio"]["base"])
check("summary matches series end", p["summary"]["base"] == ps[-1]["base"])

lp = al.build_plan("lumpsum", 500000, 7, risk="aggressive")
check("lump sum starts at the amount", lp["projection"][0]["invested"] == 500000 and lp["projection"][0]["base"] == 500000)
check("lump sum invested stays flat", all(x["invested"] == 500000 for x in lp["projection"]))
check("aggressive 7y = 80", lp["equity_pct"] == 80)

m = al.build_plan("sip", 10000, 10, equity_pct=100)
check("all equity: no debt money", m["split"]["debt"]["amount"] == 0 and not m["auto_split"])
m = al.build_plan("sip", 10000, 10, equity_pct=0)
check("all debt", m["split"]["equity"]["amount"] == 0 and m["returns"]["portfolio"]["base"] == m["returns"]["debt"]["cagr"])

opts = al.build_options()
check("options for both sides", len(opts["equity"]) > 20 and len(opts["debt"]) > 20, f"{len(opts['equity'])}/{len(opts['debt'])}")
pick = opts["equity"][3]["name"]
m = al.build_plan("sip", 10000, 10, equity_pct=50, equity_fund=pick, debt_fund=opts["debt"][0]["name"])
check("manual funds are used", m["split"]["equity"]["fund"]["name"] == pick and m["split"]["debt"]["fund"]["name"] == opts["debt"][0]["name"])
nohist = next((f for f in al.load_funds() if f["sheet"] == "equity" and f.get("r_3y") is None and f.get("r_5y") is None), None)
if nohist:
    m = al.build_plan("lumpsum", 100000, 10, equity_fund=nohist["name"])
    check("fund without history is flagged hypothetical", m["hypothetical"] and m["returns"]["equity"]["basis"] == "hypothetical")
    print("   no-history example:", nohist["name"][:60])

# ---- validation
def bad(*a, **k):
    try:
        al.build_plan(*a, **k)
        return False
    except HTTPException as e:
        return e.status_code == 400
check("rejects bad mode", bad("weekly", 1000, 5))
check("rejects tiny SIP", bad("sip", 100, 5))
check("rejects 0 years", bad("sip", 1000, 0))
check("rejects 41 years", bad("sip", 1000, 41))
check("rejects bad risk", bad("sip", 1000, 5, risk="yolo"))
check("rejects equity_pct 120", bad("sip", 1000, 5, equity_pct=120))
check("rejects unknown fund", bad("sip", 1000, 5, equity_fund="No Such Fund"))
check("a debt fund can't go in the equity slot", bad("sip", 1000, 5, equity_fund=opts["debt"][0]["name"]))

# ---- HTTP
app = FastAPI()
al.register_allocation(app)
c = TestClient(app)
j = c.get("/allocation/plan?mode=sip&amount=15000&years=12&risk=aggressive").json()
check("route works", j["equity_pct"] == 85 and len(j["projection"]) == 13, str(j)[:200])
check("route 400s on bad input", c.get("/allocation/plan?mode=sip&amount=1&years=5").status_code == 400)
check("route options", len(c.get("/allocation/options").json()["equity"]) > 20)
check("all numbers finite", all(math.isfinite(v) for row in j["projection"] for v in row.values()))

print("\n%d failure(s)" % len(failures))
raise SystemExit(1 if failures else 0)
