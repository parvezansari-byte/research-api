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

# ---- category split from a filing shaped like NSE's real one (values are fractions)
def _ctx(cid, member, typed=False):
    inner = ('<xbrli:scenario><xbrldi:explicitMember dimension="in-bse-shp:CategoryOfShareholdersAxis">in-bse-shp:%s</xbrldi:explicitMember>%s</xbrli:scenario>'
             % (member, '<xbrldi:typedMember dimension="in-bse-shp:Row"><x>1</x></xbrldi:typedMember>' if typed else ""))
    return '<xbrli:context id="%s"><xbrli:entity><xbrli:identifier scheme="x">1</xbrli:identifier></xbrli:entity>%s</xbrli:context>' % (cid, inner)


def _fact(cid, v):
    return '<in-bse-shp:ShareholdingAsAPercentageOfTotalNumberOfShares contextRef="%s">%s</in-bse-shp:ShareholdingAsAPercentageOfTotalNumberOfShares>' % (cid, v)


ROWS = {"ShareholdingOfPromoterAndPromoterGroupMember": 0.5044, "MutualFundsOrUTIMember": 0.1031,
        "AlternativeInvestmentFundsMember": 0.0017, "BanksMember": 0.0005, "InsuranceCompaniesMember": 0.0947,
        "ProvidentFundsOrPensionFundsMember": 0.0162, "SovereignWealthFundsDomesticMember": 0.0008,
        "InstitutionsDomesticMember": 0.2172, "ForeignDirectInvestmentMember": 0.0017,
        "InstitutionsForeignPortfolioInvestorCategoryOneMember": 0.1598,
        "InstitutionsForeignPortfolioInvestorCategoryTwoMember": 0.0052,
        "ResidentIndividualShareholdersHoldingNominalShareCapitalUpToRsTwoLakhMember": 0.0834}


def build_xml(rows, with_decoy=True, extra=""):
    ctx, facts = "", ""
    for i, (m, v) in enumerate(rows.items()):
        ctx += _ctx("C%d" % i, m); facts += _fact("C%d" % i, v)
    if with_decoy:   # same member with a typed row: must be ignored
        ctx += _ctx("D1", "ShareholdingOfPromoterAndPromoterGroupMember", typed=True); facts += _fact("D1", 0.0001)
    return ('<?xml version="1.0"?><xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" '
            'xmlns:xbrldi="http://xbrl.org/2006/xbrldi" xmlns:in-bse-shp="http://x/shp">' + ctx + facts + extra + '</xbrli:xbrl>').encode()


cat = shp.parse_xbrl_categories(build_xml(ROWS))
check("typed-member decoy ignored", cat["ShareholdingOfPromoterAndPromoterGroupMember"] == 0.5044, str(cat))
sp = shp.split_from_categories(cat, 50.44)
check("split: dii from aggregate", sp["dii"] == 21.72, str(sp))
check("split: fii summed from parts", sp["fii"] == 16.67, str(sp))
check("split: mf and retail", sp["mf"] == 10.31 and sp["retail"] == 8.34, str(sp))
rows2 = dict(ROWS, InstitutionsForeignMember=0.17)
check("split: foreign aggregate preferred", shp.split_from_categories(shp.parse_xbrl_categories(build_xml(rows2)), 50.44)["fii"] == 17.0)
check("split rejected when promoter disagrees with summary", shp.split_from_categories(cat, 40.0) is None)
check("split rejected when no promoter row", shp.split_from_categories({"MutualFundsOrUTIMember": 0.1}) is None)
pct_form = {k: v * 100 for k, v in ROWS.items()}
check("percent-form values also work", shp.split_from_categories(pct_form, 50.44)["dii"] == 21.72)

qs = [dict(date="2026-09-30", promoter=50.44, public=49.56, fii=None, dii=None, mf=None, retail=None, xbrl="https://nsearchives.nseindia.com/a.xml"),
      dict(date="2026-06-30", promoter=50.48, public=49.52, fii=None, dii=None, mf=None, retail=None, xbrl="https://nsearchives.nseindia.com/b.xml"),
      dict(date="2026-03-31", promoter=50.0, public=50.0, fii=None, dii=None, mf=None, retail=None, xbrl=None)]
def xf(u):
    if u.endswith("b.xml"):
        raise ShpError("NSE answered 500")
    return build_xml(ROWS)
n = shp.enrich_with_detail(qs, xml_fetch=xf)
check("enrich fills newest, survives a failed quarter", n == 1 and qs[0]["fii"] == 16.67 and qs[1]["fii"] is None and qs[2]["fii"] is None, str(qs))

# end to end: get_shareholding enriches and caches; refreshes daily while detail is missing
sb3 = FakeSb()
rows_nse = [dict(SAMPLE[0], date="30-SEP-2026", pr_and_prgrp="50.44", public_val="49.56", xbrl="https://nsearchives.nseindia.com/a.xml")]
r = shp.get_shareholding(sb3, "XYZ", fetcher=lambda s: rows_nse, now=t0, xml_fetch=lambda u: build_xml(ROWS))
check("service returns fii/dii", r["latest"]["fii"] == 16.67 and r["latest"]["dii"] == 21.72, str(r["latest"]))
calls2 = []
shp.get_shareholding(sb3, "XYZ", fetcher=lambda s: calls2.append(1) or rows_nse, now=t0 + timedelta(days=5), xml_fetch=lambda u: build_xml(ROWS))
check("detail cached for the full week", not calls2)
sb4 = FakeSb()
shp.get_shareholding(sb4, "XYZ", fetcher=lambda s: rows_nse, now=t0, xml_fetch=lambda u: (_ for _ in ()).throw(ShpError("down")))
calls3 = []
shp.get_shareholding(sb4, "XYZ", fetcher=lambda s: calls3.append(1) or rows_nse, now=t0 + timedelta(days=2), xml_fetch=lambda u: build_xml(ROWS))
check("missing detail is retried after a day", len(calls3) == 1)
check("change for fii", shp.with_changes([dict(date="b", fii=16.67), dict(date="a", fii=16.0)])[0]["fii_chg"] == 0.67)

print("\n%d failure(s)" % len(failures))
raise SystemExit(1 if failures else 0)
