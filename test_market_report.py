import datetime, math, random
from fastapi import FastAPI
from fastapi.testclient import TestClient
import market_report_api as m

ok = bad = 0
def check(c, msg):
    global ok, bad
    if c: ok += 1
    else: bad += 1; print("FAIL", msg)

def series(end, n=520, drift=0.0003, vol=0.008, seed=1, start="2024-11-01"):
    rnd = random.Random(seed)
    xs = [end]
    for _ in range(n - 1):
        xs.append(xs[-1] / (1 + drift + rnd.gauss(0, vol)))
    xs.reverse()
    d0 = datetime.date.fromisoformat("2026-11-04") - datetime.timedelta(days=int(n * 1.45))
    out, d = [], d0
    for x in xs:
        while d.weekday() >= 5:
            d += datetime.timedelta(days=1)
        out.append((d.isoformat(), x)); d += datetime.timedelta(days=1)
    return out

ENDS = {"^NSEI": 23960.6, "^BSESN": 77301.4, "^NSEBANK": 55600, "^INDIAVIX": 13.2, "^CNXIT": 36000, "^CNXAUTO": 24000,
        "^CNXPHARMA": 21000, "^CNXFMCG": 56000, "^CNXMETAL": 9800, "^CNXENERGY": 36000, "^CNXREALTY": 950, "^CNXPSUBANK": 7200,
        "^CNXMEDIA": 1700, "^CNXINFRA": 9000, "^GSPC": 6900, "^IXIC": 23500, "^N225": 50000, "DX-Y.NYB": 101.2,
        "BZ=F": 103.6, "GC=F": 4000, "INR=X": 94.64, "^TNX": 4.91}
def fetch(sym):
    if sym == "^CNXFIN":
        return []                       # one source missing
    return series(ENDS[sym], seed=abs(hash(sym)) % 997, vol=0.004 if sym == "^TNX" else 0.008)
EXTRAS = {"breadth": {"indices": [{"name": "Nifty 50", "advances": 33, "declines": 16, "unchanged": 1, "adv_pct": 66.0}]},
          "movers": {"gainers": [{"name": "Alpha", "symbol": "ALPHA", "change_pct": 4.2}], "losers": [{"name": "Beta", "symbol": "BETA", "change_pct": -2.1}]},
          "fii": {"rows": [{"who": "FII", "date": "03-Nov-2026", "buy": 20000.0, "sell": 13163.0, "net": 6837.0},
                           {"who": "DII", "date": "03-Nov-2026", "buy": 9000.0, "sell": 12679.0, "net": -3679.0}], "source": "NSE"}}

for kind in ("daily", "weekly", "monthly"):
    rep = m.collect(kind, fetch, extras_fn=lambda k: EXTRAS if k == "daily" else {"breadth": None, "movers": None, "fii": None}, now_str="4 November 2026, 20:30 IST")
    n = rep["indices"]["NIFTY 50"]
    k = m.KINDS[kind]
    s = series(ENDS["^NSEI"], seed=abs(hash("^NSEI")) % 997)
    check(abs(n["pct"] - (s[-1][1] / s[-1 - k][1] - 1) * 100) < 1e-6, f"{kind} pct")
    check(rep["sectors"] == sorted(rep["sectors"], key=lambda x: -x[1]) and "Fin Services" not in dict(rep["sectors"]), f"{kind} sectors sorted, failed one skipped")
    check("Fin Services" in rep["missing"], f"{kind} missing listed")
    lv = rep["levels"]
    check(all(r[0] > lv["last"] for r in lv["resistance"]) and all(x[0] < lv["last"] for x in lv["support"]), f"{kind} levels sides")
    check(len(rep["text"]["scenarios"]) >= 2 and rep["text"]["summary"].startswith("The Nifty 50"), f"{kind} text")
    check(("breadth" in rep["missing"]) == False if kind == "daily" else "breadth" not in rep["missing"], f"{kind} extras")
    pdf = None
    from market_report_pdf import render_pdf
    pdf = render_pdf(rep, "Crude fell on US-Iran talk reports.\nFIIs bought." if kind == "daily" else "")
    check(pdf[:4] == b"%PDF" and len(pdf) > 30000, f"{kind} pdf {len(pdf)}")
    open(f"/tmp/mr_{kind}.pdf", "wb").write(pdf)

# too little data -> 503; bad kind -> 400
app = FastAPI(); m.register_market_report(app, fetch=fetch, extras_fn=lambda k: EXTRAS if k == 'daily' else {'breadth': None, 'movers': None, 'fii': None}); c = TestClient(app)
r = c.get("/report/market.pdf?kind=weekly&note=hi")
check(r.status_code == 200 and r.headers["content-type"] == "application/pdf", f"pdf route {r.status_code} {r.text[:200] if r.status_code != 200 else ''}")
check(c.get("/report/market?kind=daily").status_code == 200 and "chart" not in c.get("/report/market?kind=daily").json(), "json route")
check(c.get("/report/market?kind=hourly").status_code == 400, "bad kind")
m._report_cache.clear()
app2 = FastAPI(); m.register_market_report(app2, fetch=lambda s: [], extras_fn=lambda k: {}); c2 = TestClient(app2)
check(c2.get("/report/market.pdf").status_code == 503, "no data 503")
print(f"{ok} passed, {bad} failed")
