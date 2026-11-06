"""
market_report_api.py - daily / weekly / monthly market report (PDF like the "Global Cues, Indian
Markets & The Road Ahead" update), built from live data.

    GET /report/market.pdf?kind=daily|weekly|monthly&note=...   the PDF
    GET /report/market?kind=daily|weekly|monthly                the numbers and text as JSON

Public routes (no email in the path). Data: Yahoo Finance (indices, sectors, global markets,
commodities, rupee), NSE (breadth, movers, FII/DII; daily report only). Everything is fetched
through injectable functions so the tests run offline. A report keeps working when some sources
fail: the missing items are left out and listed under "Data notes".

The text is generated from the numbers by fixed rules - it states what moved and where the
recent support/resistance sits. It does NOT explain WHY markets moved (news); the optional
`note` is where the adviser adds that commentary, and it is printed in its own box.
"""
import hmac
import json
import logging
import os
import threading
import time
from datetime import date, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, List, Optional, Tuple

from fastapi import HTTPException, Header
from fastapi.responses import Response

log = logging.getLogger("market_report_api")

KINDS = {"daily": 1, "weekly": 5, "monthly": 21}          # sessions compared
TITLES = {"daily": "Daily Market Report", "weekly": "Weekly Market Report", "monthly": "Monthly Market Report"}
LOOKBACK = {"daily": 60, "weekly": 90, "monthly": 250}     # sessions drawn on the trend chart

INDEX_SYMS = {"NIFTY 50": "^NSEI", "SENSEX": "^BSESN", "BANK NIFTY": "^NSEBANK", "INDIA VIX": "^INDIAVIX"}
SECTOR_SYMS = {"Bank": "^NSEBANK", "IT": "^CNXIT", "Auto": "^CNXAUTO", "Pharma": "^CNXPHARMA",
               "FMCG": "^CNXFMCG", "Metal": "^CNXMETAL", "Energy": "^CNXENERGY", "Realty": "^CNXREALTY",
               "PSU Bank": "^CNXPSUBANK", "Media": ["^CNXMEDIA", "NIFTY_MEDIA.NS"], "Infra": "^CNXINFRA", "Fin Services": ["^CNXFIN", "NIFTY_FIN_SERVICE.NS"]}
GLOBAL_SYMS = {"S&P 500": "^GSPC", "Nasdaq": "^IXIC", "Nikkei 225": "^N225", "Dollar index": "DX-Y.NYB",
               "Brent crude": "BZ=F", "Gold": "GC=F", "USD/INR": "INR=X", "US 10Y yield": "^TNX"}
CACHE_S = 600
_hist_cache: Dict[str, tuple] = {}
_report_cache: Dict[tuple, tuple] = {}
_render_slots = threading.Semaphore(2)      # the route is public: at most 2 PDFs drawn at once


# ---------------------------------------------------------------- data fetching
def fetch_yahoo(symbol: str) -> List[Tuple[str, float]]:
    """[(YYYY-MM-DD, close)] oldest first, about 2 years of sessions."""
    now = time.time()
    hit = _hist_cache.get(symbol)
    if hit and now - hit[0] < CACHE_S:
        return hit[1]
    import yfinance as yf
    df = yf.Ticker(symbol).history(period="2y", interval="1d", auto_adjust=False)
    rows = []
    if df is not None and not df.empty and "Close" in df:
        for ts, c in df["Close"].dropna().items():
            try:
                if float(c) > 0:
                    rows.append((ts.strftime("%Y-%m-%d"), float(c)))
            except (AttributeError, ValueError, TypeError):
                continue
    if rows:
        _hist_cache[symbol] = (now, rows)
    return rows


def fetch_all(fetch: Callable[[str], list], symbols: Dict[str, object]) -> Dict[str, list]:
    def one(item):
        name, syms = item
        for sym in ([syms] if isinstance(syms, str) else syms):     # first symbol that returns data wins
            try:
                rows = fetch(sym)
                if rows:
                    return name, rows
            except Exception as e:
                log.warning("history %s failed: %s", sym, e)
        return name, []
    with ThreadPoolExecutor(max_workers=8) as ex:
        return dict(ex.map(one, symbols.items()))


def live_extras(kind: str) -> dict:
    """Breadth, movers and FII/DII (daily report only). Each part may be None."""
    out = {"breadth": None, "movers": None, "fii": None}
    if kind != "daily":
        return out
    try:
        import breadth_api
        out["breadth"] = breadth_api.get_breadth()
    except Exception as e:
        log.warning("breadth failed: %s", e)
    try:
        import movers_api
        out["movers"] = movers_api.get_movers("NIFTY 50", 5)
    except Exception as e:
        log.warning("movers failed: %s", e)
    try:
        import api
        rows, src = api._m_fetch_fii_dii()
        out["fii"] = {"rows": rows, "source": src}
    except Exception as e:
        log.warning("fii/dii failed: %s", e)
    return out


# ---------------------------------------------------------------- FII / DII log
FLOW_KEY = "fii_dii_log"          # push_state row: JSON list of {"d": "YYYY-MM-DD", "fii": net, "dii": net}
FLOW_KEEP = 90


def parse_flow_date(s) -> Optional[str]:
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(str(s).strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def update_flow_log(log: list, rows: list) -> list:
    """Add (or replace) the day in `rows` (NSE FII/DII rows) and return the new log."""
    net = {r.get("who"): r.get("net") for r in rows if r.get("net") is not None}
    d = next((parse_flow_date(r.get("date")) for r in rows if parse_flow_date(r.get("date"))), None)
    if d is None or "FII" not in net or "DII" not in net:
        return log
    new = [e for e in log if e.get("d") != d] + [{"d": d, "fii": float(net["FII"]), "dii": float(net["DII"])}]
    new.sort(key=lambda e: e["d"])
    return new[-FLOW_KEEP:]


def flow_summary(log: list) -> Optional[dict]:
    """Latest day, last 5 recorded sessions and month-to-date, from the days this app has recorded."""
    if not log:
        return None
    last = log[-1]
    l5 = log[-5:]
    mtd = [e for e in log if e["d"][:7] == last["d"][:7]]
    s = lambda es, k: sum(e[k] for e in es)
    return {"latest": last,
            "last5": {"fii": s(l5, "fii"), "dii": s(l5, "dii"), "n": len(l5)},
            "mtd": {"fii": s(mtd, "fii"), "dii": s(mtd, "dii"), "n": len(mtd)}}


def attach_flows(rep: dict, sb) -> None:
    """Record today's flows (daily report) and attach the running totals. Never raises."""
    rep["flows"] = None
    if sb is None:
        return
    try:
        import push_api
        log = json.loads(push_api.state_get(sb, FLOW_KEY) or "[]")
        fii = rep.get("fii")
        if fii and fii.get("rows"):
            new = update_flow_log(log, fii["rows"])
            if new != log:
                push_api.state_set(sb, FLOW_KEY, json.dumps(new))
                log = new
        rep["flows"] = flow_summary(log)
    except Exception as e:
        log_ = logging.getLogger("market_report_api")
        log_.warning("flow log failed: %s", e)


def is_last_weekday_of_month(d: date) -> bool:
    nxt = d + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    return nxt.month != d.month


def push_text(rep: dict) -> tuple:
    ix = rep["indices"]
    n = ix["NIFTY 50"]
    when = {"daily": "today", "weekly": "this week", "monthly": "this month"}[rep["kind"]]
    parts = [f"Nifty {sgn(n['pct'])} to {num(n['last'])} {when}"]
    if "SENSEX" in ix:
        parts.append(f"Sensex {sgn(ix['SENSEX']['pct'])}")
    if "BANK NIFTY" in ix:
        parts.append(f"Bank Nifty {sgn(ix['BANK NIFTY']['pct'])}")
    body = " | ".join(parts)
    fl = (rep.get("flows") or {}).get("latest")
    if rep["kind"] == "daily" and fl:
        body += f". FII {fl['fii']:+,.0f} cr, DII {fl['dii']:+,.0f} cr"
    return f"{TITLES[rep['kind']]} ready", body


# ---------------------------------------------------------------- number helpers
def live_label(now_epoch: float, as_of: str) -> Optional[str]:
    """'Intraday snapshot at 11:44 IST' while the NSE session is open and as_of is today's date, else None."""
    ist = time.gmtime(now_epoch + 19800)
    mins = ist.tm_hour * 60 + ist.tm_min
    if ist.tm_wday < 5 and 9 * 60 + 15 <= mins < 15 * 60 + 45 and as_of == time.strftime("%Y-%m-%d", ist):
        return f"Intraday snapshot at {ist.tm_hour:02d}:{ist.tm_min:02d} IST"
    return None


def pct_change(series: list, k: int) -> Optional[float]:
    if len(series) <= k:
        return None
    a, b = series[-1 - k][1], series[-1][1]
    return (b - a) / a * 100 if a else None


def sma(closes: list, n: int) -> Optional[float]:
    return sum(closes[-n:]) / n if len(closes) >= n else None


def sma_series(closes: list, n: int) -> list:
    out = []
    for i in range(len(closes)):
        out.append(sum(closes[i - n + 1:i + 1]) / n if i >= n - 1 else None)
    return out


def num(x: float, d: int = 2) -> str:
    return f"{x:,.{d}f}"


def sgn(p: Optional[float], d: int = 2) -> str:
    return "n/a" if p is None else f"{'+' if p >= 0 else '-'}{abs(p):.{d}f}%"


def level(x: float) -> str:
    return f"{round(x):,}"


def verb(p: float, up="rose", down="fell") -> str:
    return up if p >= 0 else down


# ---------------------------------------------------------------- analysis
def compute_levels(closes: list) -> dict:
    last = closes[-1]
    win = closes[-252:]
    lv = {"last": last, "dma20": sma(closes, 20), "dma50": sma(closes, 50), "dma200": sma(closes, 200),
          "hi20": max(closes[-20:]), "lo20": min(closes[-20:]), "hi52": max(win), "lo52": min(win)}
    lv["from_hi_pct"] = (last / lv["hi52"] - 1) * 100
    step = 500 if last >= 40000 else 250 if last >= 10000 else 100 if last >= 2000 else 50
    base = int(last // step) * step
    cands = [("20-day high", lv["hi20"]), ("52-week high", lv["hi52"]), ("20-day average", lv["dma20"]),
             ("50-day average", lv["dma50"]), ("200-day average", lv["dma200"]), ("20-day low", lv["lo20"]),
             ("round level", base), ("round level", base + step), ("round level", base + 2 * step),
             ("round level", base - step)]
    above, below = [], []
    for name, v in cands:
        if v is None:
            continue
        (above if v > last * 1.0005 else below if v < last * 0.9995 else []).append((v, name))
    above.sort()
    below.sort(reverse=True)
    # a named technical level beats a round number when the two sit within 0.4% of each other
    above.sort(key=lambda x: (x[0], x[1] == "round level"))
    below.sort(key=lambda x: (-x[0], x[1] == "round level"))
    dedup = lambda xs: [x for i, x in enumerate(xs) if all(abs(x[0] - y[0]) / x[0] > 0.004 for y in xs[:i])]
    lv["resistance"] = dedup(above)[:2]
    lv["support"] = dedup(below)[:2]
    return lv


def build_text(kind: str, d: dict, live: Optional[str] = None) -> dict:
    when = {"daily": "today", "weekly": "this week", "monthly": "this month"}[kind]
    n = d["indices"]["NIFTY 50"]
    nif = f"The Nifty 50 {verb(n['pct'])} {abs(n['pct']):.2f}% {when} to {num(n['last'])}"
    clauses = [nif]
    sx, bn = d["indices"].get("SENSEX"), d["indices"].get("BANK NIFTY")
    if sx:
        clauses.append(f"the Sensex {verb(sx['pct'])} {abs(sx['pct']):.2f}% to {num(sx['last'])}")
    if bn:
        clauses.append(f"the Bank Nifty {verb(bn['pct'])} {abs(bn['pct']):.2f}% to {num(bn['last'])}")
    if len(clauses) == 1:
        summary = clauses[0] + "."
    else:
        summary = ", ".join(clauses[:-1]) + " and " + clauses[-1] + "."
    if live:
        summary = f"{live}: the market is still open, so these are not final closing figures. " + summary
    sec = d["sectors"]
    if len(sec) >= 2:
        best, worst = sec[0], sec[-1]
        summary += (f" Among sectors, {best[0]} did best ({sgn(best[1])}) and {worst[0]} lagged ({sgn(worst[1])}); "
                    f"{sum(1 for _, p in sec if p > 0)} of {len(sec)} sectors we track ended higher.")
    vix = d["indices"].get("INDIA VIX")
    if vix:
        summary += f" India VIX, the market's fear gauge, is at {num(vix['last'])}."
    lv = d["levels"]
    if lv["dma200"]:
        pos = "above" if lv["last"] > lv["dma200"] else "below"
        summary += (f" The Nifty is {pos} its 200-day average ({level(lv['dma200'])}) and "
                    f"{abs(lv['from_hi_pct']):.1f}% below its 52-week high of {level(lv['hi52'])}."
                    if lv["from_hi_pct"] < -0.05 else
                    f" The Nifty is {pos} its 200-day average ({level(lv['dma200'])}) and at its 52-week high.")
    g = d["global"]
    bits = []
    for name in ("S&P 500", "Nasdaq", "Nikkei 225"):
        if name in g and g[name]["pct"] is not None:
            bits.append(f"{name} {sgn(g[name]['pct'])}")
    gl = []
    if bits:
        gl.append("Overseas equities over the same window: " + ", ".join(bits) + ".")
    if "US 10Y yield" in g and g["US 10Y yield"]["last"]:
        y = g["US 10Y yield"]
        gl.append(f"The US 10-year yield is at {y['last']:.2f}% ({'+' if y['chg'] >= 0 else '-'}{abs(y['chg']) * 100:.0f} bps)."
                  " Higher US yields tend to pull money away from emerging markets; lower yields help.")
    if "Dollar index" in g and g["Dollar index"]["pct"] is not None:
        gl.append(f"The dollar index is at {num(g['Dollar index']['last'])} ({sgn(g['Dollar index']['pct'])}).")
    if "Brent crude" in g and g["Brent crude"]["pct"] is not None:
        b = g["Brent crude"]
        gl.append(f"Brent crude is near ${b['last']:.0f} a barrel ({sgn(b['pct'])}). India imports most of its oil, so "
                  "cheaper crude helps inflation, the current account and the rupee, and dearer crude does the opposite.")
    if "USD/INR" in g and g["USD/INR"]["pct"] is not None:
        r = g["USD/INR"]
        gl.append(f"USD/INR is at {num(r['last'])}; the rupee {'weakened' if r['pct'] > 0 else 'strengthened'} "
                  f"{abs(r['pct']):.2f}% against the dollar.")
    if "Gold" in g and g["Gold"]["pct"] is not None:
        gl.append(f"Gold is at ${g['Gold']['last']:,.0f} an ounce ({sgn(g['Gold']['pct'])}).")
    s1 = lv["support"][0] if lv["support"] else None
    r1 = lv["resistance"][0] if lv["resistance"] else None
    r2 = lv["resistance"][1] if len(lv["resistance"]) > 1 else None
    s2 = lv["support"][1] if len(lv["support"]) > 1 else None
    scen = []
    if r1:
        scen.append(("BULL", f"Nifty closes above {level(r1[0])} ({r1[1]})",
                     f"{level(r1[0])}-{level(r2[0])} comes into view" if r2 else "A move to fresh highs is possible"))
    else:
        scen.append(("BULL", "Nifty keeps making new highs", "No overhead resistance on the chart"))
    if s1 and r1:
        scen.append(("BASE", f"Nifty stays between {level(s1[0])} and {level(r1[0])}",
                     "Range-bound; stock-specific moves matter more"))
    elif s1:
        scen.append(("BASE", f"Nifty holds above {level(s1[0])}", "Trend continues with pauses"))
    else:
        scen.append(("BASE", "Nifty moves sideways", "Range-bound"))
    if s1:
        scen.append(("BEAR", f"Nifty closes below {level(s1[0])} ({s1[1]})",
                     f"{level(s2[0])} ({s2[1]}) comes into play" if s2 else "A deeper fall is possible"))
    drop = lv["from_hi_pct"]
    guide = [
        ("Long-term investors", "Keep your asset allocation. Review it once or twice a year rather than after every market move."),
        ("SIP investors", "Keep your SIPs running. They buy more units when markets fall and fewer when they rise."),
        ("Fresh lump sums", ("Invest in three or four tranches over 8-12 weeks rather than all at once. "
                             f"The market is {abs(drop):.0f}% below its 52-week high and could fall further."
                             if drop < -4 else
                             "Near highs, invest in tranches over several weeks rather than all at once.")),
        ("Short-term traders", ("Decide the stop-loss before you enter. " +
                                (f"{level(s1[0])} ({s1[1]}) is the nearest support on the chart. " if s1 else "") +
                                "Keep position sizes small when markets are volatile.")),
        ("Mutual fund distributors", "Use this report to start rebalancing conversations with clients instead of chasing returns."),
    ]
    take = [f"The Nifty {verb(n['pct'], 'gained', 'lost')} {abs(n['pct']):.2f}% {when}."]
    if s1 and r1:
        take.append(f"On the chart, {level(s1[0])} is the nearest support and {level(r1[0])} the nearest resistance.")
    take.append("Stay invested, add new money gradually and keep your asset allocation in line with your goals.")
    return {"summary": summary, "global": gl, "scenarios": scen, "guidance": guide, "takeaway": " ".join(take)}


def analyse(kind: str, hist: Dict[str, list], extras: dict, now_str: str, live: Optional[str] = None) -> dict:
    k = KINDS[kind]
    nifty = hist.get("NIFTY 50") or []
    if len(nifty) < k + 20:
        raise ValueError("Not enough Nifty history to build a report")
    indices = {}
    for name in INDEX_SYMS:
        s = hist.get(name) or []
        p = pct_change(s, k)
        if p is not None:
            indices[name] = {"last": s[-1][1], "pct": p, "chg": s[-1][1] - s[-1 - k][1], "date": s[-1][0], "base_date": s[-1 - k][0]}
    sectors = []
    for name in SECTOR_SYMS:
        p = pct_change(hist.get("sec:" + name) or [], k)
        if p is not None:
            sectors.append((name, p))
    sectors.sort(key=lambda x: -x[1])
    glob = {}
    for name in GLOBAL_SYMS:
        s = hist.get("g:" + name) or []
        if len(s) > k:
            glob[name] = {"last": s[-1][1], "pct": pct_change(s, k), "chg": s[-1][1] - s[-1 - k][1]}
    closes = [c for _, c in nifty]
    look = min(LOOKBACK[kind], len(nifty))
    if callable(live):
        live = live(nifty[-1][0])
    out = {
        "kind": kind, "title": TITLES[kind], "generated": now_str, "live": live,
        "as_of": nifty[-1][0], "base_date": nifty[-1 - k][0], "sessions": k,
        "indices": indices, "sectors": sectors, "global": glob, "levels": compute_levels(closes),
        "chart": {"dates": [d for d, _ in nifty[-look:]], "close": closes[-look:],
                  "dma20": sma_series(closes, 20)[-look:], "dma50": sma_series(closes, 50)[-look:],
                  "dma200": sma_series(closes, 200)[-look:]},
        "compare": {},
        "breadth": extras.get("breadth"), "movers": extras.get("movers"), "fii": extras.get("fii"),
    }
    for name in ("NIFTY 50", "SENSEX", "BANK NIFTY"):
        s = hist.get(name) or []
        s = s[-look:]
        if len(s) > 2:
            out["compare"][name] = {"dates": [d for d, _ in s], "rebased": [c / s[0][1] * 100 for _, c in s]}
    out["text"] = build_text(kind, out, live)
    missing = [n for n in list(SECTOR_SYMS) if not any(n == s[0] for s in sectors)]
    missing += [n for n in GLOBAL_SYMS if n not in glob]
    missing += [n for n in INDEX_SYMS if n not in indices]
    if kind == "daily":
        for key, label in (("breadth", "market breadth"), ("movers", "top movers"), ("fii", "FII/DII flows")):
            if not extras.get(key):
                missing.append(label)
    out["missing"] = missing
    return out


def collect(kind: str, fetch: Callable = fetch_yahoo, extras_fn: Callable = live_extras, now_str: str = "",
            live=None) -> dict:
    syms = {}
    syms.update(INDEX_SYMS)
    syms.update({"sec:" + n: s for n, s in SECTOR_SYMS.items()})
    syms.update({"g:" + n: s for n, s in GLOBAL_SYMS.items()})
    hist = fetch_all(fetch, syms)
    return analyse(kind, hist, extras_fn(kind), now_str, live)


def register_market_report(app, fetch: Callable = fetch_yahoo, extras_fn: Callable = live_extras,
                           supabase_getter: Optional[Callable] = None, now_fn: Callable = time.time,
                           news_fn: Optional[Callable] = None, ai_fn: Optional[Callable] = None,
                           post: Optional[Callable] = None):
    import market_report_brief as brief_mod
    news_fn = news_fn or brief_mod.default_news
    ai_fn = ai_fn or brief_mod.default_ai
    _last_user_mail: Dict[str, float] = {}

    def get_brief(rep: dict) -> dict:
        b = rep.get("_brief")
        if b is None:
            b = brief_mod.build_brief(rep, news_fn, ai_fn)
            rep["_brief"] = b
        return b

    def make_pdf(rep: dict, note: str = "") -> bytes:
        from market_report_pdf import render_pdf
        if not _render_slots.acquire(timeout=20):
            raise HTTPException(503, "Busy building other reports. Try again in a minute.")
        try:
            return render_pdf(rep, note, get_brief(rep))
        finally:
            _render_slots.release()

    def email_all(rep: dict, sb, kind: str, to: Optional[str] = None, note: str = "") -> list:
        addrs = [to] if to else brief_mod.recipients(sb, kind)
        if not addrs:
            return []
        pdf = make_pdf(rep, note)
        out = []
        for a in addrs:
            try:
                brief_mod.send_email(rep, get_brief(rep), pdf, a, post)
                out.append({"to": a, "ok": True})
            except Exception as e:
                log.warning("email to %s failed: %s", a, e)
                out.append({"to": a, "ok": False, "error": str(e)[:200]})
        return out

    def _sb():
        try:
            return supabase_getter() if supabase_getter else None
        except Exception:
            return None

    def get(kind: str) -> dict:
        if kind not in KINDS:
            raise HTTPException(400, "kind must be daily, weekly or monthly")
        now = time.time()
        hit = _report_cache.get((kind,))
        if hit and now - hit[0] < CACHE_S:
            return hit[1]
        try:
            rep = collect(kind, fetch, extras_fn=extras_fn, now_str=time.strftime("%d %B %Y, %H:%M IST", time.gmtime(now + 19800)),
                          live=lambda as_of: live_label(now, as_of))
        except ValueError as e:
            raise HTTPException(503, str(e))
        except Exception as e:
            log.exception("report failed")
            raise HTTPException(502, f"Could not build the report: {e}")
        attach_flows(rep, _sb())
        _report_cache[(kind,)] = (now, rep)
        return rep

    @app.post("/report/tick")
    def report_tick(kind: str = "daily", force: bool = False, email: bool = True, x_tick_secret: str = Header(default="")):
        """Cron: build the report after the close, record FII/DII, and push a summary to the owner."""
        import push_api
        secret = os.environ.get("PUSH_TICK_SECRET", "")
        if not secret:
            raise HTTPException(503, "PUSH_TICK_SECRET is not set on the server")
        if not hmac.compare_digest(x_tick_secret.encode(), secret.encode()):
            raise HTTPException(403, "Bad tick secret")
        if kind not in KINDS:
            raise HTTPException(400, "kind must be daily, weekly or monthly")
        sb = _sb()
        if sb is None:
            raise HTTPException(500, "Database not configured on the server")
        ist = time.gmtime(now_fn() + 19800)
        today = date(ist.tm_year, ist.tm_mon, ist.tm_mday)
        if kind == "monthly" and not force and not is_last_weekday_of_month(today):
            return {"skipped": "not the last weekday of the month"}
        key = f"market_report_sent_{kind}"
        if not force and push_api.state_get(sb, key) == today.isoformat():
            return {"skipped": "already sent today"}
        _report_cache.pop((kind,), None)
        rep = get(kind)
        if kind == "daily" and not force and rep["as_of"] != today.isoformat():
            return {"skipped": f"no new session today (latest data is {rep['as_of']})"}
        title, body = push_text(rep)
        sent = push_api.send_to_email(sb, push_api.OWNER_EMAIL, title, body, {"type": "report", "kind": kind}, kind="report")
        push_api.state_set(sb, key, today.isoformat())
        mailed = []
        mkey = f"market_report_emailed_{kind}"
        if email and (force or push_api.state_get(sb, mkey) != today.isoformat()):
            try:
                mailed = email_all(rep, sb, kind)
            except Exception as e:
                log.warning("email step failed: %s", e)
                mailed = [{"ok": False, "error": str(e)[:200]}]
            push_api.state_set(sb, mkey, today.isoformat())
        return {"kind": kind, "as_of": rep["as_of"], "title": title, "body": body, "push": sent, "email": mailed}

    @app.post("/report/email-all")
    def report_email_all(kind: str = "daily", to: str = "", x_tick_secret: str = Header(default="")):
        """Admin/cron: email the report now (to one address with ?to=, else to every opted-in user)."""
        secret = os.environ.get("PUSH_TICK_SECRET", "")
        if not secret:
            raise HTTPException(503, "PUSH_TICK_SECRET is not set on the server")
        if not hmac.compare_digest(x_tick_secret.encode(), secret.encode()):
            raise HTTPException(403, "Bad tick secret")
        if kind not in KINDS:
            raise HTTPException(400, "kind must be daily, weekly or monthly")
        rep = get(kind)
        return {"kind": kind, "as_of": rep["as_of"], "email": email_all(rep, _sb(), kind, to.strip() or None)}

    @app.post("/report/{email}/email")
    def report_email_me(email: str, kind: str = "daily", note: str = ""):
        """App button: email the report to the signed-in user (the path holds the user's email, so auth_guard
        checks the login token). One e-mail per minute per address."""
        email = email.strip()
        if "@" not in email or len(email) > 200:
            raise HTTPException(400, "Bad email")
        if kind not in KINDS:
            raise HTTPException(400, "kind must be daily, weekly or monthly")
        now = time.time()
        if now - _last_user_mail.get(email.lower(), 0) < 60:
            raise HTTPException(429, "Please wait a minute before sending another email.")
        _last_user_mail[email.lower()] = now
        rep = get(kind)
        res = email_all(rep, _sb(), kind, email, note.strip()[:1500])
        if not res or not res[0]["ok"]:
            raise HTTPException(502, (res[0].get("error") if res else "Could not send") or "Could not send")
        return {"sent": True, "to": email, "kind": kind, "as_of": rep["as_of"]}

    @app.get("/report/market")
    def report_json(kind: str = "daily"):
        rep = get(kind)
        slim = {k: v for k, v in rep.items() if k not in ("chart", "compare", "_brief")}
        return slim

    @app.get("/report/market.pdf")
    def report_pdf(kind: str = "daily", note: str = ""):
        rep = get(kind)
        pdf = make_pdf(rep, note.strip()[:1500])
        name = f"advantage_{kind}_market_report_{rep['as_of']}.pdf"
        return Response(pdf, media_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="{name}"'})
