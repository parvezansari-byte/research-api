import datetime, json, math, os, random
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
# round levels give a realistic nearest resistance when the Nifty is at a 20-day high
import time as _t
closes = [20000 + i * 10 for i in range(300)]
lv = m.compute_levels(closes)
check(lv["resistance"] and lv["resistance"][0][0] <= lv["last"] * 1.02, f"near resistance {lv['resistance']}")
check(lv["support"] and lv["support"][0][0] < lv["last"], "support below")
# live label: IST 11:44 on a Thursday, as_of today -> label; after close or other day -> None
epoch = _t.mktime(_t.strptime("2026-11-05 06:14", "%Y-%m-%d %H:%M")) - _t.timezone   # 06:14 UTC = 11:44 IST
check(m.live_label(epoch, "2026-11-05") == "Intraday snapshot at 11:44 IST", f"live {m.live_label(epoch, '2026-11-05')}")
check(m.live_label(epoch, "2026-11-04") is None, "yesterday's data is not live")
late = epoch + 6 * 3600
check(m.live_label(late, "2026-11-05") is None, "after close")
sat = _t.mktime(_t.strptime("2026-11-07 06:14", "%Y-%m-%d %H:%M")) - _t.timezone
check(m.live_label(sat, "2026-11-07") is None, "weekend")
# fallback symbol: first empty, second returns data
calls = []
def fb(sym):
    calls.append(sym)
    return [] if sym == "^CNXMEDIA" else [("2026-11-04", 1.0)]
got = m.fetch_all(fb, {"Media": ["^CNXMEDIA", "NIFTY_MEDIA.NS"]})
check(got["Media"] and calls == ["^CNXMEDIA", "NIFTY_MEDIA.NS"], "fallback symbol")
rep = m.collect("daily", fetch, extras_fn=lambda k: {}, now_str="x", live=lambda a: "Intraday snapshot at 11:44 IST")
check(rep["live"] and rep["text"]["summary"].startswith("Intraday snapshot at 11:44 IST: the market is still open"), "live in text")
from market_report_pdf import render_pdf
check(render_pdf(rep, "")[:4] == b"%PDF", "live pdf renders")
# ---- FII/DII log
rows1 = [{"who": "FII", "date": "03-Nov-2026", "net": 6837.0}, {"who": "DII", "date": "03-Nov-2026", "net": -3679.0}]
log = m.update_flow_log([], rows1)
check(log == [{"d": "2026-11-03", "fii": 6837.0, "dii": -3679.0}], f"log1 {log}")
check(m.update_flow_log(log, rows1) == log, "idempotent per date")
rows2 = [{"who": "FII", "date": "04-Nov-2026", "net": 7284.0}, {"who": "DII", "date": "04-Nov-2026", "net": -3712.0}]
log2 = m.update_flow_log(log, rows2)
sm = m.flow_summary(log2)
check(sm["latest"]["d"] == "2026-11-04" and sm["last5"] == {"fii": 14121.0, "dii": -7391.0, "n": 2} and sm["mtd"]["n"] == 2, f"summary {sm}")
check(m.flow_summary([]) is None and m.update_flow_log(log2, [{"who": "FII", "date": "bad", "net": 1}]) == log2, "bad rows ignored")
log3 = log2 + [{"d": "2026-12-01", "fii": 1.0, "dii": 2.0}]
check(m.flow_summary(log3)["mtd"]["n"] == 1, "month to date resets")
check(len(m.update_flow_log([{"d": f"2026-01-{i:02d}", "fii": 1, "dii": 1} for i in range(1, 29)] * 4, rows1)) <= m.FLOW_KEEP, "log capped")
check(m.is_last_weekday_of_month(__import__("datetime").date(2026, 10, 30)) and not m.is_last_weekday_of_month(__import__("datetime").date(2026, 10, 29))
      and m.is_last_weekday_of_month(__import__("datetime").date(2026, 7, 31)) and not m.is_last_weekday_of_month(__import__("datetime").date(2026, 8, 28)), "last weekday")
# ---- daily report records flows and exposes totals; weekly report reuses the log
import push_api
from fake_supabase import FakeSb
sb = FakeSb()
sent = []
push_api.send_to_email = lambda sb_, e, t, b, data=None, kind=None: (sent.append((e, t, b, data, kind)) or {"sent": 1})
import os
asof = m.collect("daily", fetch, extras_fn=lambda k: {}, now_str="x")["as_of"]
def at(day, hhmm="11:30"):          # epoch for HH:MM UTC on `day`  (11:30 UTC = 17:00 IST)
    return _t.mktime(_t.strptime(f"{day} {hhmm}", "%Y-%m-%d %H:%M")) - _t.timezone
state = {"t": at(asof)}
m._report_cache.clear()
app3 = FastAPI()
m.register_market_report(app3, fetch=fetch, extras_fn=lambda k: EXTRAS if k == "daily" else {"breadth": None, "movers": None, "fii": None},
                         supabase_getter=lambda: sb, now_fn=lambda: state["t"])
c3 = TestClient(app3)
os.environ["PUSH_TICK_SECRET"] = "s"
H = {"X-Tick-Secret": "s"}
check(c3.post("/report/tick").status_code == 403, "tick needs secret")
check(c3.post("/report/tick?kind=hourly", headers=H).status_code == 400, "tick bad kind")
r = c3.post("/report/tick?kind=daily", headers=H).json()
check(r.get("title") == "Daily Market Report ready" and "Nifty" in r["body"] and "FII +6,837 cr" in r["body"], f"daily tick {r}")
check(len(sent) == 1 and sent[0][3] == {"type": "report", "kind": "daily"}, "one push sent")
check(c3.post("/report/tick?kind=daily", headers=H).json().get("skipped") == "already sent today" and len(sent) == 1, "once per day")
check(push_api.state_get(sb, m.FLOW_KEY) is not None and json.loads(push_api.state_get(sb, m.FLOW_KEY))[0]["fii"] == 6837.0, "flows recorded in push_state")
# a later day with no new session (holiday): data still ends on `asof`
state["t"] = at(__import__("datetime").date.fromisoformat(asof).isoformat() and "2026-11-20")
r = c3.post("/report/tick?kind=daily", headers=H).json()
check("no new session" in r.get("skipped", ""), f"holiday {r}")
# weekly report shows the recorded flows; PDF renders them
m._report_cache.clear()
rep = c3.get("/report/market?kind=weekly").json()
check(rep["flows"] and rep["flows"]["latest"]["d"] == "2026-11-03", f"weekly flows {rep.get('flows')}")
pdfr = c3.get("/report/market.pdf?kind=weekly")
check(pdfr.status_code == 200 and pdfr.content[:4] == b"%PDF", "weekly pdf with flows")
open("/tmp/mr_weekly_flows.pdf", "wb").write(pdfr.content)
# monthly tick only on the last weekday of the month
state["t"] = at("2026-10-29")
check(c3.post("/report/tick?kind=monthly", headers=H).json().get("skipped") == "not the last weekday of the month", "monthly skipped mid-month")
state["t"] = at("2026-10-30")
rm = c3.post("/report/tick?kind=monthly", headers=H).json()
check(rm.get("title") == "Monthly Market Report ready", f"monthly {rm}")
print(f"{ok} passed, {bad} failed")
