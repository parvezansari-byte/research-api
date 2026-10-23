"""Tests for shareholding.py (fake Supabase, fake NSE). Run: python test_shareholding.py"""
import os
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

import shareholding as shp
from fake_supabase import FakeSb
from shareholding import ShpError, register_shareholding

failures = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if not cond else ""))
    if not cond:
        failures.append(name)


SAMPLE = [  # shaped like NSE's summary rows
    {"symbol": "ABC", "date": "30-JUN-2026", "pr_and_prgrp": "50.30", "public_val": "49.70", "employeeTrusts": "0"},
    {"symbol": "ABC", "date": "31-MAR-2026", "pr_and_prgrp": "50.00", "public_val": "50.00", "employeeTrusts": "0"},
    {"symbol": "ABC", "date": "31-Dec-2025", "pr_and_prgrp": "1,00.5x", "public_val": "-", "employeeTrusts": ""},
    {"symbol": "ABC", "date": "bad date", "pr_and_prgrp": "9", "public_val": "9"},
]
q = shp.parse_nse(SAMPLE)
check("parses 2 good rows, drops bad", len(q) == 2, str(q))
check("newest first", q[0]["date"] == "2026-06-30" and q[1]["date"] == "2026-03-31")
check("numbers", q[0]["promoter"] == 50.3 and q[0]["public"] == 49.7 and q[0]["fii"] is None)
ch = shp.with_changes(q)
check("change vs older quarter", ch[0]["promoter_chg"] == 0.3 and ch[0]["public_chg"] == -0.3, str(ch[0]))
check("oldest has no change", ch[1]["promoter_chg"] is None)
check("dict payload", len(shp.parse_nse({"data": SAMPLE})) == 2)
check("garbage payload", shp.parse_nse("x") == [] and shp.parse_nse(None) == [] and shp.parse_nse([1, 2]) == [])
check("symbol cleaning", shp.clean_symbol(" nse:reliance.ns ") == "RELIANCE" and shp.clean_symbol("m&m") == "M&M")

sb = FakeSb()
calls = []


def fetch_ok(sym):
    calls.append(sym)
    return SAMPLE


t0 = datetime(2026, 10, 23, tzinfo=timezone.utc)
r = shp.get_shareholding(sb, "ABC", fetcher=fetch_ok, now=t0)
check("first call fetches and saves", len(calls) == 1 and r["stale"] is False and r["as_of"] == "2026-06-30")
check("row saved", sb.tables["shareholding"][0]["symbol"] == "ABC")
shp.get_shareholding(sb, "ABC", fetcher=fetch_ok, now=t0 + timedelta(days=3))
check("fresh cache is reused", len(calls) == 1)
shp.get_shareholding(sb, "ABC", fetcher=fetch_ok, now=t0 + timedelta(days=8))
check("expired cache refetches", len(calls) == 2)
shp.get_shareholding(sb, "ABC", force=True, fetcher=fetch_ok, now=t0 + timedelta(days=8))
check("force refetches", len(calls) == 3)
assert len(sb.tables["shareholding"]) == 1


def fetch_down(sym):
    raise ShpError("NSE answered 403")


r = shp.get_shareholding(sb, "ABC", fetcher=fetch_down, now=t0 + timedelta(days=30))
check("stale copy served when NSE is down", r["stale"] is True and r["error"] == "NSE answered 403" and r["latest"]["promoter"] == 50.3)
try:
    shp.get_shareholding(sb, "NEW", fetcher=fetch_down, now=t0)
    check("no cache and NSE down raises", False)
except ShpError:
    check("no cache and NSE down raises", True)
try:
    shp.get_shareholding(sb, "EMPTY", fetcher=lambda s: [], now=t0)
    check("empty payload raises", False)
except ShpError:
    check("empty payload raises", True)

# HTTP layer
app = FastAPI()
register_shareholding(app, lambda: sb)
http = TestClient(app)
shp.fetch_nse = fetch_ok  # route uses default fetcher bound at def time, so test via cache instead
sb.tables["shareholding"][0]["fetched_at"] = datetime.now(timezone.utc).isoformat()
j = http.get("/shareholding/abc.ns").json()
check("route serves cached", j["symbol"] == "ABC" and len(j["quarters"]) == 2, str(j))
check("bad symbol 400", http.get("/shareholding/a b$").status_code in (400, 404))
check("unknown symbol with NSE unreachable is 502 not crash", http.get("/shareholding/ZZZ").status_code == 502)
os.environ["PUSH_TICK_SECRET"] = "s3cret-s3cret"
check("probe needs secret", http.get("/shareholding-probe/ABC").status_code == 403)
check("probe wrong secret", http.get("/shareholding-probe/ABC", headers={"X-Tick-Secret": "x"}).status_code == 403)
p = shp.probe("ABC", fetcher_raw=fetch_ok)
check("probe reports keys", p["ok"] and p["rows"] == 4 and "pr_and_prgrp" in p["first_row_keys"] and p["parsed_quarters"] == 2, str(p))
p = shp.probe("ABC", fetcher_raw=fetch_down)
check("probe reports failure", p["ok"] is False and "403" in p["error"])

# ---- full filing probe
XML = b"""<?xml version="1.0"?>
<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:xbrldi="http://xbrl.org/2006/xbrldi"
 xmlns:in-capmkt="http://x/in-capmkt">
 <xbrli:context id="C1"><xbrli:entity><xbrli:segment>
   <xbrldi:explicitMember dimension="in-capmkt:ShareholderCategoryAxis">in-capmkt:ForeignPortfolioInvestorsMember</xbrldi:explicitMember>
 </xbrli:segment></xbrli:entity></xbrli:context>
 <xbrli:context id="C2"><xbrli:entity><xbrli:segment>
   <xbrldi:explicitMember dimension="in-capmkt:ShareholderCategoryAxis">in-capmkt:MutualFundsMember</xbrldi:explicitMember>
 </xbrli:segment></xbrli:entity></xbrli:context>
 <in-capmkt:ShareholdingAsAPercentageOfTotalNumberOfShares contextRef="C1">19.2</in-capmkt:ShareholdingAsAPercentageOfTotalNumberOfShares>
 <in-capmkt:ShareholdingAsAPercentageOfTotalNumberOfShares contextRef="C2">6.1</in-capmkt:ShareholdingAsAPercentageOfTotalNumberOfShares>
 <in-capmkt:NameOfCompany contextRef="C1">Sample</in-capmkt:NameOfCompany>
</xbrli:xbrl>"""
sm = shp.summarize_xbrl(XML)
check("summary counts contexts and facts", sm["contexts"] == 2 and sm["percentage_facts"] == 2, str(sm))
check("summary lists categories", "ForeignPortfolioInvestorsMember" in " ".join(sm["context_members"]) or any("Foreign" in m for m in sm["context_members"]))
check("fact carries its category", sm["sample_facts"][0]["value"] == "19.2" and sm["sample_facts"][0]["members"])
with_link = [dict(SAMPLE[0], xbrl="https://nsearchives.nseindia.com/corporate/xbrl/x.xml")]
px = shp.probe_xbrl("ABC", raw_fetch=lambda s: with_link, xml_fetch=lambda u: XML)
check("probe_xbrl ok", px["ok"] and px["percentage_facts"] == 2 and px["filing"].endswith("x.xml"), str(px))
check("probe_xbrl no link", shp.probe_xbrl("ABC", raw_fetch=lambda s: SAMPLE)["ok"] is False)
check("probe_xbrl bad xml", "not valid XML" in shp.probe_xbrl("ABC", raw_fetch=lambda s: with_link, xml_fetch=lambda u: b"<a>")["error"])
for bad in ("http://nsearchives.nseindia.com/a.xml", "https://evil.example.com/a.xml", "https://nseindia.com.evil.io/a.xml", ""):
    try:
        shp.fetch_xbrl(bad); check("blocks " + bad, False)
    except ShpError:
        check("blocks " + (bad or "empty link"), True)
check("quarter keeps filing link", shp.parse_nse(with_link)[0]["xbrl"].endswith("x.xml"))

print("\n%d failure(s)" % len(failures))
raise SystemExit(1 if failures else 0)
