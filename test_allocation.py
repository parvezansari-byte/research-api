"""Tests for allocation_api.py against the real funds_data.json. Run: python test_allocation.py"""
import math

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import allocation_api as al
from allocation_api import FundPick, PlanRequest

failures = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if not cond else ""))
    if not cond:
        failures.append(name)


def plan(**kw):
    return al.build_plan(PlanRequest(**kw))


def bad(**kw):
    try:
        plan(**kw)
        return False
    except HTTPException as e:
        return e.status_code == 400


# ---- class split rules
check("balanced 10y = 45/20/35", al.auto_split("balanced", 10) == {"equity": 45, "hybrid": 20, "debt": 35})
check("short horizon cuts equity", al.auto_split("balanced", 3)["equity"] == 20 and al.auto_split("conservative", 1)["equity"] == 5)
check("long horizon adds equity", al.auto_split("balanced", 15)["equity"] == 50)
ok = True
for r in al.BASE_SPLIT:
    for y in range(1, 41):
        s = al.auto_split(r, y)
        ok &= sum(s.values()) == 100 and all(v >= 0 for v in s.values()) and s["debt"] >= 5 and 5 <= s["equity"] <= 85
check("split always adds to 100, equity 5-85, debt >= 5", ok)

# ---- math
check("lump sum", abs(al.future_value("lumpsum", 100000, 12, 10) - 100000 * 1.12 ** 10) < 1e-6)
rm = 1.12 ** (1 / 12) - 1
check("SIP closed form", abs(al.future_value("sip", 10000, 12, 10) - 10000 * ((1 + rm) ** 120 - 1) / rm) < 1e-6)
check("SIP at 0%", al.future_value("sip", 10000, 0, 5) == 600000)
check("split_amount adds up", all(sum(al.split_amount(t, w)) == t for t in (10000, 9999, 1) for w in ([30, 30, 15, 15, 10], [1, 1, 1], [0, 50, 50])))
check("split_amount all zero weights", al.split_amount(100, [0, 0]) == [0, 0])

# ---- suitability
for n, expect in (("X Flexi Cap Fund", True), ("X Retirement Fund", False), ("X ELSS Tax Saver", False),
                  ("PSU Bond Plus SDL Apr 2026 Index Fund", False), ("Bharat Bond ETF April 2030", False),
                  ("X Fixed Maturity Plan Series 5", False), ("Nifty 50 Index Fund", True)):
    check(f"suitable({n}) is {expect}", al.suitable({"name": n}) is expect)
check("close-ended excluded", not al.suitable({"name": "Y", "fund_type": "Close Ended"}))

# ---- auto picks
for cls in al.CLASSES:
    for risk in al.BASE_SPLIT:
        for y in (2, 10):
            rows = al.auto_picks(cls, risk, y)
            names = [r["fund"]["name"] for r in rows]
            check(f"auto {cls}/{risk}/{y}y: 1-5 funds, weights = 100, no duplicates, all suitable",
                  1 <= len(rows) <= al.MAX_FUNDS and abs(sum(r["weight"] for r in rows) - 100) < 1e-9
                  and len(set(names)) == len(names) and all(al.suitable(r["fund"]) for r in rows))
top = al.best_in_category("equity", "Mid Cap")
elig = [f for f in al.load_funds() if f["sheet"] == "equity" and al.category_of(f) == "Mid Cap" and al.suitable(f)
        and (f.get("aum") or 0) >= 1000 and f.get("r_3y") is not None and f.get("sharpe") is not None]
check("best in category is top Sharpe", top["sharpe"] == max(f["sharpe"] for f in elig))
check("aggressive equity includes mid and small cap", {"Mid Cap", "Small Cap"} <= {al.category_of(r["fund"]) for r in al.auto_picks("equity", "aggressive", 10)})
check("short debt uses short categories", {al.category_of(r["fund"]) for r in al.auto_picks("debt", "balanced", 2)} <= {"Low Duration", "Short Duration", "Banking & PSU"})

# ---- fund returns
r = al.fund_return({"r_5y": 14.0, "r_3y": 20.0, "std_dev": 12}, "equity")
check("5y preferred and blended", r["cagr"] == 13.0 and r["historical"] == 14.0 and r["basis"] == "5-year" and not r["hypothetical"], str(r))
check("3y when no 5y", al.fund_return({"r_3y": 9.0}, "equity")["cagr"] == 10.5)
r = al.fund_return({"r_5y": 41.0}, "equity")
check("capped high", r["cagr"] == 18.0 and r["capped"])
check("floored", al.fund_return({"r_5y": -30.0}, "equity")["cagr"] == 4.0)
r = al.fund_return({}, "hybrid")
check("hybrid hypothetical", r["hypothetical"] and r["cagr"] == 10.0 and r["vol"] == 9.0)
r = al.fund_return({"ytm": 7.4, "expense_ratio": 0.4}, "debt")
check("debt uses yield", r["basis"] == "current yield" and r["cagr"] == 7.0)

# ---- auto plan
p = plan(mode="sip", amount=10000, years=10)
cl = {c["key"]: c for c in p["classes"]}
check("auto split used", p["auto"] and [cl[k]["pct"] for k in al.CLASSES] == [45, 20, 35])
check("class amounts add to total", sum(c["amount"] for c in p["classes"]) == 10000)
check("fund amounts add to class amounts", all(sum(f["amount"] for f in c["funds"]) == c["amount"] for c in p["classes"]))
check("at most 5 funds per class", all(len(c["funds"]) <= 5 for c in p["classes"]) and len(cl["equity"]["funds"]) == 5)
check("category shares add to 100", abs(sum(b["share"] for b in p["by_category"]) - 100) < 0.2, str(sum(b["share"] for b in p["by_category"])))
check("blend matches classes", abs(p["returns"]["portfolio"]["base"] - sum(c["pct"] / 100 * c["expected"] for c in p["classes"])) < 0.02)
ps = p["projection"]
check("projection 0..10, SIP starts at 0", [x["year"] for x in ps] == list(range(11)) and ps[0]["base"] == 0 and ps[0]["invested"] == 0)
check("low < base < high, base > invested", ps[-1]["low"] < ps[-1]["base"] < ps[-1]["high"] and ps[-1]["base"] > ps[-1]["invested"])
check("invested = 12 x years x amount", ps[-1]["invested"] == 1200000 and p["summary"]["base"] == ps[-1]["base"])
check("monotonic", all(ps[i]["base"] <= ps[i + 1]["base"] for i in range(10)))
check("each fund has weight/share/expected", all({"weight", "share", "expected", "amount", "category"} <= set(f) for c in p["classes"] for f in c["funds"]))
lp = plan(mode="lumpsum", amount=500000, years=7, risk="aggressive")
check("lump sum starts at the amount", lp["projection"][0]["base"] == 500000 and all(x["invested"] == 500000 for x in lp["projection"]))
check("aggressive 7y split", [c["pct"] for c in lp["classes"]] == [65, 15, 20])

# ---- zero allocation
z = plan(mode="sip", amount=10000, years=10, classes={"equity": 70, "hybrid": 0, "debt": 30})
zc = {c["key"]: c for c in z["classes"]}
check("hybrid 0% -> no hybrid funds, no money", zc["hybrid"]["funds"] == [] and zc["hybrid"]["amount"] == 0 and zc["hybrid"]["pct"] == 0)
check("not auto when classes are given", not z["auto"])
check("zero class does not enter the blend", abs(z["returns"]["portfolio"]["base"] - (0.7 * zc["equity"]["expected"] + 0.3 * zc["debt"]["expected"])) < 0.02)
check("hybrid missing from category list", all(b["class"] != "hybrid" for b in z["by_category"]))
z = plan(mode="lumpsum", amount=100000, years=5, classes={"equity": 100})
check("equity only", z["classes"][0]["amount"] == 100000 and z["classes"][2]["funds"] == [] and z["classes"][1]["funds"] == [])
z = plan(mode="lumpsum", amount=100000, years=5, classes={"debt": 100})
check("debt only equals debt return", z["returns"]["portfolio"]["base"] == z["classes"][2]["expected"])

# ---- manual picks
opts = al.build_options()
eq = [f for cat in opts["equity"].values() for f in cat if f["has_history"]]
five = [FundPick(name=eq[i * 10]["name"], weight=w) for i, w in enumerate([40, 30, 15, 15, 0])]
m = plan(mode="sip", amount=20000, years=10, classes={"equity": 60, "hybrid": 0, "debt": 40}, picks={"equity": five})
mc = {c["key"]: c for c in m["classes"]}["equity"]
check("manual 5 funds kept in order", [f["name"] for f in mc["funds"]] == [p.name for p in five])
check("zero-weight fund listed with no money", mc["funds"][4]["weight"] == 0 and mc["funds"][4]["amount"] == 0)
check("amounts follow weights", mc["amount"] == 12000 and mc["funds"][0]["amount"] == 4800 and mc["funds"][1]["amount"] == 3600)
check("zero-weight fund not in category list", all(b["category"] != mc["funds"][4]["category"] or b["share"] > 0 for b in m["by_category"]))
check("debt still auto-picked", len({c["key"]: c for c in m["classes"]}["debt"]["funds"]) >= 1)
check("weights adding to 100 are not flagged", not m["normalized"])
m2 = plan(mode="sip", amount=20000, years=10, classes={"equity": 100, "hybrid": 0, "debt": 0},
          picks={"equity": [FundPick(name=eq[0]["name"], weight=1), FundPick(name=eq[10]["name"], weight=1)]})
check("weights are scaled to 100 and flagged", m2["normalized"] and [f["weight"] for f in m2["classes"][0]["funds"]] == [50.0, 50.0])
m3 = plan(mode="sip", amount=5000, years=10, classes={"equity": 0, "hybrid": 0, "debt": 100},
          picks={"equity": [FundPick(name="Whatever", weight=100)]})
check("picks for a 0% class are ignored", m3["classes"][0]["funds"] == [])

# ---- hypothetical when a fund has no record
fresh = next(f for cat in opts["equity"].values() for f in cat if not f["has_history"])
h = plan(mode="lumpsum", amount=100000, years=10, classes={"equity": 100},
         picks={"equity": [FundPick(name=fresh["name"], weight=100)]})
hf = h["classes"][0]["funds"][0]
check("no-record fund is hypothetical", h["hypothetical"] and hf["hypothetical"] and hf["expected"] == 12.0 and hf["historical"] is None, str(hf))
check("hypothetical noted", any("Hypothetical" in n for n in h["notes"]))
h0 = plan(mode="lumpsum", amount=100000, years=10, classes={"equity": 100},
          picks={"equity": [FundPick(name=fresh["name"], weight=0), FundPick(name=eq[0]["name"], weight=100)]})
check("zero-weight hypothetical fund does not flag the plan", not h0["hypothetical"])

# ---- validation
check("bad mode", bad(mode="weekly"))
check("tiny SIP", bad(mode="sip", amount=100))
check("0 years", bad(years=0))
check("41 years", bad(years=41))
check("bad risk", bad(risk="yolo"))
check("classes must add to 100", bad(classes={"equity": 50, "hybrid": 0, "debt": 40}))
check("class over 100", bad(classes={"equity": 120, "hybrid": -20, "debt": 0}))
check("unknown class", bad(classes={"gold": 100}))
check("six funds", bad(classes={"equity": 100}, picks={"equity": [FundPick(name=eq[i]["name"], weight=10) for i in range(6)]}))
check("duplicate fund", bad(classes={"equity": 100}, picks={"equity": [FundPick(name=eq[0]["name"], weight=50), FundPick(name=eq[0]["name"].upper(), weight=50)]}))
check("negative weight", bad(classes={"equity": 100}, picks={"equity": [FundPick(name=eq[0]["name"], weight=-5), FundPick(name=eq[1]["name"], weight=105)]}))
check("all weights zero in a funded class", bad(classes={"equity": 100}, picks={"equity": [FundPick(name=eq[0]["name"], weight=0)]}))
check("unknown fund", bad(classes={"equity": 100}, picks={"equity": [FundPick(name="No Such Fund", weight=100)]}))
debt_fund = next(iter(opts["debt"].values()))[0]["name"]
check("debt fund cannot go in equity", bad(classes={"equity": 100}, picks={"equity": [FundPick(name=debt_fund, weight=100)]}))
check("unknown class in picks", bad(picks={"gold": [FundPick(name="x", weight=1)]}))

# ---- options
check("options cover all three classes", set(opts) == set(al.CLASSES) and all(len(v) >= 5 for v in opts.values()))
allf = [f for c in opts.values() for cat in c.values() for f in cat]
check("options: no unsuitable funds", all(al.suitable({"name": f["name"]}) for f in allf))
check("options: some new funds without a record, flagged", any(not f["has_history"] for f in allf) and all(f["has_history"] == (f["r_3y"] is not None or f["r_5y"] is not None) for f in allf))
check("options: at most 8+3 per category", all(len(v) <= 11 for c in opts.values() for v in c.values()))
check("options: categories match their class", all(f["category"] == cat for c in opts.values() for cat, fs in c.items() for f in fs))

# ---- PDF
def pdf_of(**kw):
    return al.build_pdf(plan(**kw))

b = pdf_of(mode="sip", amount=10000, years=10)
check("PDF is a PDF", b[:5] == b"%PDF-" and len(b) > 5000, str(len(b)))
check("PDF for lump sum with a zero class", pdf_of(mode="lumpsum", amount=250000, years=1, classes={"equity": 100})[:5] == b"%PDF-")
check("PDF with hypothetical fund and weights", pdf_of(mode="lumpsum", amount=100000, years=10, classes={"equity": 100}, picks={"equity": [FundPick(name=fresh["name"], weight=100)]})[:5] == b"%PDF-")
many = {c: [FundPick(name=f["name"], weight=20) for f in [x for cat in opts[c].values() for x in cat][::7][:5]] for c in al.CLASSES}
check("PDF with 15 funds", pdf_of(mode="sip", amount=50000, years=30, classes={"equity": 40, "hybrid": 30, "debt": 30}, picks=many)[:5] == b"%PDF-")
check("PDF text helper is latin-1 safe", al._s("₹ 5 – ‘x’ हि").encode("latin-1") and al._inr(2164084) == "Rs. 21,64,084" and al._inr(950) == "Rs. 950")

# ---- HTTP
app = FastAPI()
al.register_allocation(app)
c = TestClient(app)
j = c.post("/allocation/plan", json={"mode": "sip", "amount": 15000, "years": 12, "risk": "aggressive"}).json()
check("POST plan works", [x["pct"] for x in j["classes"]] == [70, 15, 15] and len(j["projection"]) == 13, str(j)[:160])
j = c.post("/allocation/plan", json={"mode": "sip", "amount": 15000, "years": 12, "classes": {"equity": 50, "hybrid": 0, "debt": 50}}).json()
check("POST plan with classes", j["classes"][1]["funds"] == [] and not j["auto"])
check("POST plan 400 on bad input", c.post("/allocation/plan", json={"amount": 1}).status_code == 400)
check("POST plan 422 on malformed body", c.post("/allocation/plan", json={"picks": "nope"}).status_code == 422)
resp = c.post("/allocation/pdf", json={"mode": "lumpsum", "amount": 500000, "years": 10})
check("POST pdf returns a PDF file", resp.status_code == 200 and resp.headers["content-type"] == "application/pdf" and resp.content[:5] == b"%PDF-" and "attachment" in resp.headers["content-disposition"])
check("POST pdf 400 on bad input", c.post("/allocation/pdf", json={"years": 99}).status_code == 400)
o = c.get("/allocation/options").json()
check("GET options", set(o) == set(al.CLASSES))
check("all numbers finite", all(math.isfinite(v) for row in j["projection"] for v in row.values()))

print("\n%d failure(s)" % len(failures))
raise SystemExit(1 if failures else 0)
