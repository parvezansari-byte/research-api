import base64, io, csv
from datetime import date
import openpyxl
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import portfolio_review_api as p

ok = bad = 0
def check(c, m):
    global ok, bad
    if c: ok += 1
    else: bad += 1; print("FAIL", m)

T = date(2026, 10, 28)
# money / date
check(p.parse_money("Rs. 1,25,000.50") == 125000.5, "money rs")
check(p.parse_money("₹ 2,000") == 2000, "money rupee")
check(p.parse_money("(500)") == -500, "money neg")
check(p.parse_money("abc") is None and p.parse_money(None) is None, "money none")
check(p.parse_date("15/10/2021") == date(2021, 10, 15), "date dmy")
check(p.parse_date("2021-10-15") == date(2021, 10, 15), "date iso")
check(p.parse_date("15-Oct-2021") == date(2021, 10, 15), "date mon")
check(p.parse_date(44484) == date(2021, 10, 15), "excel serial")
check(p.parse_date("junk") is None and p.parse_date("") is None, "date junk")
# xirr
x = p.xirr([(date(2021, 10, 28), -100000), (T, 161051)])
check(abs(x - 10.0) < 0.1, f"xirr 10% got {x}")
check(p.xirr([(T, 100)]) is None, "xirr one flow")
# matching
f, s = p.match_fund("HDFC Flexi Cap Fund - Direct Plan - Growth")
check(f and "HDFC Flexi Cap" in f["name"], "match direct->regular")
check(p.match_fund("SBI Nifty 50 Index Fund")[0] is None or "Next" not in p.match_fund("SBI Nifty 50 Index Fund")[0]["name"], "nifty 50 not next")
check(p.match_fund("Nonexistent Fund XYZ")[0] is None, "no match")
check(p.search_funds("hdfc flexi") and all("HDFC" in n for n in p.search_funds("hdfc flexi")), "search")
check(p.search_funds("") == [], "empty search")
# review
H = p.Holding
r = p.build_review([H(name="HDFC Flexi Cap Fund", invested=100000, current=165000, date="2021-10-15"),
                    H(name="Quant Small Cap Fund", invested=50000, current=52000, date="2026-03-01"),
                    H(name="Made Up Fund", invested=10000, current=11000, date="2023-01-01"),
                    H(name="Axis Midcap Fund", invested=20000, current=25000)], T)
a, b, c, d = r["holdings"]
check(a["matched"] and a["cagr_pct"] and a["benchmark_pct"] is not None and a["verdict"] in ("Ahead", "In line", "Behind"), "holding a")
check(b["verdict"] == "Too early", "too early")
check(not c["matched"] and c["verdict"] == "No comparison" and c["flags"], "unmatched")
check(d["verdict"] == "Add a date" and d["abs_pct"] == 25.0, "no date")
check(r["totals"]["invested"] == 180000 and r["totals"]["current"] == 253000, "totals")
check(r["totals"]["xirr_pct"] is not None, "xirr present")
check(abs(sum(x["pct"] for x in r["allocation"]["by_asset"]) - 100) < 0.2, "alloc 100")
check(r["observations"] and r["notes"], "obs notes")
# verdict direction
rh = p.review_holding(H(name="HDFC Flexi Cap Fund", invested=100, current=400, date="2021-10-15"), T)
check(rh["verdict"] == "Ahead", "ahead")
rh = p.review_holding(H(name="HDFC Flexi Cap Fund", invested=100, current=100, date="2021-10-15"), T)
check(rh["verdict"] == "Behind", "behind")
# validation
for hs, frag in [([], "at least one"), ([H(name="", invested=1, current=1)], "empty"),
                 ([H(name="A", invested=0, current=1)], "above 0"), ([H(name="A", invested=1, current=-1)], "negative"),
                 ([H(name="A", invested=1, current=1, date="2030-01-01")], "future"),
                 ([H(name="A", invested=1, current=1, date="zzz")], "could not read")]:
    try: p.build_review(hs, T); check(False, "should reject " + frag)
    except HTTPException as e: check(e.status_code == 400 and frag in e.detail, "msg " + frag + " " + e.detail)
try: p.build_review([H(name="A", invested=1, current=1)] * 61, T); check(False, "limit")
except HTTPException: check(True, "")
# upload: xlsx
wb = openpyxl.Workbook(); ws = wb.active
ws.append(["My portfolio as on today"]); ws.append([])
ws.append(["Scheme Name", "Units", "Invested Value", "Current Value", "Purchase Date"])
ws.append(["HDFC Flexi Cap Fund - Growth", 100, "₹1,00,000", 165000, date(2021, 10, 15)])
ws.append(["Axis Midcap Fund", 10, 50000, 61000, "10/01/2025"])
ws.append(["Bad row", 1, "x", 5, "01/01/2024"])
ws.append(["Total", 0, 150000, 226000, None])
buf = io.BytesIO(); wb.save(buf)
u = p.parse_upload("pf.xlsx", buf.getvalue())
check(len(u["rows"]) == 2 and u["skipped"] == 1 and u["rows"][0]["invested"] == 100000 and u["rows"][1]["date"] == "2025-01-10", f"xlsx {u}")
# csv with semicolons and no date
txt = "Fund;Cost;Market value\nSBI Small Cap Fund;1000;1500\n"
u = p.parse_upload("a.csv", txt.encode())
check(len(u["rows"]) == 1 and u["rows"][0]["date"] is None and any("No date column" in w for w in u["warnings"]), "csv nodate")
for fn, raw, frag in [("a.xls", b"x", "xls"), ("a.pdf", b"x", "Upload an"), ("a.csv", b"foo,bar\n1,2\n", "columns"),
                      ("a.csv", b"Fund,Invested,Current\n,,\n", "No usable")]:
    try: p.parse_upload(fn, raw); check(False, "reject " + fn)
    except HTTPException as e: check(frag in e.detail, f"{fn}: {e.detail}")
# http
app = FastAPI(); p.register_portfolio_review(app); cl = TestClient(app)
check(cl.get("/portfolio/funds?q=axis midcap").json()["funds"], "http search")
r = cl.post("/portfolio/parse", json={"filename": "a.csv", "data_base64": base64.b64encode(txt.encode()).decode()})
check(r.status_code == 200 and r.json()["rows"], "http parse")
r = cl.post("/portfolio/review", json={"holdings": [{"name": "Axis Midcap Fund", "invested": 1000, "current": 1200, "date": "2023-01-01"}]})
check(r.status_code == 200 and r.json()["holdings"][0]["matched"], "http review")
r = cl.post("/portfolio/review", json={"holdings": [{"name": "A", "invested": -1, "current": 1}]})
check(r.status_code == 400, "http 400")
# SIP
ds = p.sip_dates(date(2026, 1, 31), date(2026, 5, 31))
check(ds == [date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31), date(2026, 4, 30), date(2026, 5, 31)], f"sip clamp {ds}")
check(len(p.sip_dates(date(2023, 10, 28), T)) == 37, "sip count")
rs = p.build_review([H(name="Axis Midcap Fund", current=480000, sip_amount=10000, sip_start="2023-10-28")], T)
h0 = rs["holdings"][0]
check(h0["invested"] == 370000 and h0["sip"]["instalments"] == 37 and h0["sip"]["running"], f"sip rows {h0['sip']}")
check(h0["cagr_pct"] is not None and 15 < h0["cagr_pct"] < 40, f"sip xirr {h0['cagr_pct']}")
check(abs(rs["totals"]["xirr_pct"] - h0["cagr_pct"]) < 0.05, "single sip: portfolio xirr = fund xirr")
check(h0["verdict"] in ("Ahead", "In line", "Behind"), "sip verdict")
check("_flows" not in h0, "no private field leaked")
rs2 = p.build_review([H(name="Axis Midcap Fund", current=130000, sip_amount=10000, sip_start="2024-01-15", sip_end="2024-12-15"),
                      H(name="Quant Small Cap Fund", invested=50000, current=70000, date="2022-01-10")], T)
check(rs2["holdings"][0]["sip"]["instalments"] == 12 and not rs2["holdings"][0]["sip"]["running"], "ended sip")
check(rs2["totals"]["invested"] == 170000 and rs2["totals"]["xirr_pct"] is not None, "mixed totals")
for hs, frag in [([H(name="A", current=1, sip_amount=100)], "start date"),
                 ([H(name="A", current=1, sip_amount=100, sip_start="2030-01-01")], "future"),
                 ([H(name="A", current=1, sip_amount=100, sip_start="2024-01-01", sip_end="2023-01-01")], "end date"),
                 ([H(name="A", current=1, sip_amount=-5, sip_start="2024-01-01")], "SIP amount"),
                 ([H(name="A", current=1)], "invested value")]:
    try: p.build_review(hs, T); check(False, "sip reject " + frag)
    except HTTPException as e: check(frag in e.detail, f"sip msg {frag}: {e.detail}")
rv = p.build_review([H(name="HDFC Flexi Cap Fund", invested=100000, current=165000, date="2021-10-15"),
                     H(name="Made Up ₹ Fund – x", invested=10000, current=11000, date="2023-01-01")], T)
pdfb = p.build_pdf(rv)
check(pdfb[:4] == b"%PDF" and len(pdfb) > 1500, "pdf bytes")
pdfs = p.build_pdf(rs)
check(pdfs[:4] == b"%PDF", "sip pdf")
open("/tmp/pr_test.pdf", "wb").write(pdfb)
r = cl.post("/portfolio/pdf", json={"holdings": [{"name": "Axis Midcap Fund", "invested": 1000, "current": 1200, "date": "2023-01-01"}]})
check(r.status_code == 200 and r.content[:4] == b"%PDF" and r.headers["content-type"] == "application/pdf", "http pdf")
r = cl.post("/portfolio/pdf", json={"holdings": []})
check(r.status_code == 400, "http pdf 400")
print(f"{ok} passed, {bad} failed")
raise SystemExit(1 if bad else 0)
