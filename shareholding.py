"""
shareholding.py - quarterly shareholding pattern (promoter / public ...) per stock.

Companies file their pattern with the exchanges every quarter. The numbers only
change four times a year, so each symbol is fetched once, saved in Supabase and
served from there for TTL_DAYS. If the exchange cannot be reached the saved copy
is still served (marked stale) so the app keeps working.

Routes (public data, no login needed):
  GET /shareholding/{symbol}              -> last 8 quarters, newest first
  GET /shareholding/{symbol}?refresh=1    -> force a fresh fetch
  GET /shareholding-probe/{symbol}        -> what the exchange answered, raw
                                             (needs header X-Tick-Secret)
  GET /shareholding-probe/{symbol}?xbrl=1 -> structure of the newest full filing

Source: NSE "corporate-share-holdings-master". NSE blocks many cloud servers, so
run the probe route from Render first. Fields that NSE's summary does not carry
(FII, DII) come back as null and the app simply hides them.
"""
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from collections import Counter
from urllib.parse import urlparse
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import requests
from fastapi import Header, HTTPException

logger = logging.getLogger("shareholding")

TABLE = "shareholding"
TTL_DAYS = 7
SYMBOL_RE = re.compile(r"^[A-Z0-9&\-]{1,20}$")
NSE_HOME = "https://www.nseindia.com"
NSE_URL = NSE_HOME + "/api/corporate-share-holdings-master?index=equities&symbol={sym}"
NSE_REFERER = NSE_HOME + "/companies-listing/corporate-filings-shareholding-pattern"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


class ShpError(Exception):
    pass


# --------------------------------------------------------------------------- parsing
def clean_symbol(raw: str) -> str:
    s = (raw or "").strip().upper()
    for junk in ("NSE:", "BSE:"):
        if s.startswith(junk):
            s = s[len(junk):]
    for tail in (".NS", ".BO"):
        if s.endswith(tail):
            s = s[: -len(tail)]
    return s


def _num(v) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return round(float(v), 2)
    t = str(v).strip().replace(",", "").replace("%", "")
    if t in ("", "-", "--", "NA", "N/A", "null"):
        return None
    try:
        return round(float(t), 2)
    except ValueError:
        return None


_DATE_FORMATS = ("%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d", "%d-%m-%Y", "%d %b %Y", "%d/%m/%Y")


def _date(v) -> Optional[str]:
    t = str(v or "").strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(t.title() if "%b" in fmt or "%B" in fmt else t, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_nse(payload) -> list:
    """NSE summary rows -> [{date, promoter, public, other, fii, dii}] newest first."""
    rows = payload
    if isinstance(payload, dict):
        rows = payload.get("data") or payload.get("shareholding") or payload.get("rows") or []
    if not isinstance(rows, list):
        return []
    out = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        d = _date(r.get("date") or r.get("asOnDate") or r.get("quarter"))
        promoter = _num(r.get("pr_and_prgrp"))
        public = _num(r.get("public_val"))
        if d is None or (promoter is None and public is None):
            continue
        out[d] = {"date": d, "promoter": promoter, "public": public,
                  "other": _num(r.get("employeeTrusts")), "fii": None, "dii": None,
                  "mf": None, "retail": None,
                  "xbrl": str(r.get("xbrl") or "") or None}
    return [out[k] for k in sorted(out, reverse=True)]


def with_changes(quarters: list) -> list:
    """Adds promoter_chg / public_chg: change in percentage points vs the older quarter."""
    res = []
    for i, q in enumerate(quarters):
        q = dict(q)
        older = quarters[i + 1] if i + 1 < len(quarters) else None
        for key in ("promoter", "public", "fii", "dii"):
            a, b = q.get(key), (older or {}).get(key)
            q[key + "_chg"] = round(a - b, 2) if a is not None and b is not None else None
        res.append(q)
    return res


# --------------------------------------------------------------------------- fetching
def fetch_nse(symbol: str):
    """Returns the decoded JSON from NSE. Raises ShpError with the reason."""
    s = requests.Session()
    s.headers.update(HEADERS)
    try:
        s.get(NSE_HOME, timeout=10)  # NSE wants the cookies from the home page
        r = s.get(NSE_URL.format(sym=requests.utils.quote(symbol)),
                  headers={"Referer": NSE_REFERER}, timeout=15)
    except requests.RequestException as e:
        raise ShpError(f"could not reach NSE: {e.__class__.__name__}")
    if r.status_code != 200:
        raise ShpError(f"NSE answered {r.status_code}")
    try:
        return r.json()
    except ValueError:
        raise ShpError("NSE did not return JSON")


# --------------------------------------------------------------------------- cache + service
def _now(now=None):
    return now or datetime.now(timezone.utc)


def _read(sb, symbol):
    try:
        rows = sb.table(TABLE).select("*").eq("symbol", symbol).execute().data or []
    except Exception as e:
        logger.warning("shareholding cache read failed: %s", e)
        return None
    return rows[0] if rows else None


def _is_fresh(row, now, ttl_days: float = TTL_DAYS) -> bool:
    try:
        at = datetime.fromisoformat(str(row["fetched_at"]).replace("Z", "+00:00"))
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        return now - at < timedelta(days=ttl_days)
    except Exception:
        return False


def _shape(symbol, quarters, fetched_at, stale, error=None):
    q = with_changes(quarters)[:8]
    return {"symbol": symbol, "source": "NSE", "as_of": q[0]["date"] if q else None,
            "latest": q[0] if q else None, "quarters": q, "stale": stale,
            "fetched_at": fetched_at, "error": error}


def get_shareholding(sb, symbol: str, force: bool = False,
                     fetcher: Callable = fetch_nse, now=None, xml_fetch=None) -> dict:
    now = _now(now)
    cached = _read(sb, symbol)
    cached_q = ((cached or {}).get("data") or {}).get("quarters") or []
    # a newest quarter that has a filing link but no split yet is retried daily
    missing_detail = bool(cached_q) and bool(cached_q[0].get("xbrl")) and cached_q[0].get("fii") is None
    ttl = 1 if missing_detail else TTL_DAYS
    if cached and cached_q and not force and _is_fresh(cached, now, ttl):
        return _shape(symbol, cached_q, cached["fetched_at"], False)
    try:
        quarters = parse_nse(fetcher(symbol))
        if not quarters:
            raise ShpError("no shareholding rows for this symbol")
        enrich_with_detail(quarters, xml_fetch=xml_fetch)
    except ShpError as e:
        if cached_q:
            return _shape(symbol, cached_q, cached["fetched_at"], True, str(e))
        raise
    stamp = now.isoformat()
    try:
        sb.table(TABLE).upsert({"symbol": symbol, "data": {"quarters": quarters},
                                "fetched_at": stamp}, on_conflict="symbol").execute()
    except Exception as e:
        logger.warning("shareholding cache write failed: %s", e)
    return _shape(symbol, quarters, stamp, False)


def probe(symbol: str, fetcher_raw=None) -> dict:
    """Shows exactly what NSE answers from this server, without raising."""
    t0 = time.time()
    info = {"symbol": symbol, "ok": False}
    try:
        data = (fetcher_raw or fetch_nse)(symbol)
        rows = data if isinstance(data, list) else (data.get("data") if isinstance(data, dict) else None)
        first = rows[0] if isinstance(rows, list) and rows else None
        info.update(ok=True, type=type(data).__name__,
                    rows=len(rows) if isinstance(rows, list) else None,
                    first_row_keys=sorted(first.keys()) if isinstance(first, dict) else None,
                    first_row=first, parsed_quarters=len(parse_nse(data)))
    except ShpError as e:
        info["error"] = str(e)
    info["ms"] = int((time.time() - t0) * 1000)
    return info


# --------------------------------------------------------------------------- full filing (XBRL)
XBRL_HOSTS = ("nsearchives.nseindia.com", "www.nseindia.com", "archives.nseindia.com")
XBRL_MAX_BYTES = 4_000_000


def _session():
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def fetch_xbrl(url: str, session=None) -> bytes:
    """Downloads one filing from NSE's archive. Only NSE hosts are allowed."""
    u = urlparse(url or "")
    if u.scheme != "https" or u.hostname not in XBRL_HOSTS:
        raise ShpError("filing link is not an NSE address")
    try:
        if session is None:
            session = _session()
            session.get(NSE_HOME, timeout=10)
        s = session
        r = s.get(url, headers={"Referer": NSE_REFERER}, timeout=30)
    except requests.RequestException as e:
        raise ShpError(f"could not download the filing: {e.__class__.__name__}")
    if r.status_code != 200:
        raise ShpError(f"filing download answered {r.status_code}")
    if len(r.content) > XBRL_MAX_BYTES:
        raise ShpError("filing is larger than expected")
    return r.content


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def summarize_xbrl(xml_bytes: bytes, max_facts: int = 60) -> dict:
    """Describes the structure of a shareholding filing: tag names, the categories
    (contexts) it uses, and sample percentage facts. Used to write the parser."""
    root = ET.fromstring(xml_bytes)
    tags = Counter(_local(e.tag) for e in root.iter())
    contexts = {}
    for c in root.iter():
        if _local(c.tag) != "context":
            continue
        members = [(m.get("dimension", "").rsplit(":", 1)[-1], (m.text or "").strip())
                   for m in c.iter() if _local(m.tag) == "explicitMember"]
        contexts[c.get("id")] = members
    facts = []
    for e in root.iter():
        name = _local(e.tag)
        if "ercentage" in name and e.get("contextRef") and (e.text or "").strip():
            facts.append({"tag": name, "context": e.get("contextRef"),
                          "members": contexts.get(e.get("contextRef")),
                          "value": (e.text or "").strip()})
    return {"root": _local(root.tag), "bytes": len(xml_bytes),
            "distinct_tags": len(tags), "top_tags": tags.most_common(40),
            "contexts": len(contexts),
            "context_members": sorted({m for ms in contexts.values() for _, m in ms})[:80],
            "percentage_facts": len(facts), "sample_facts": facts[:max_facts]}


CAT_AXIS = "CategoryOfShareholdersAxis"
PCT_TAG = "ShareholdingAsAPercentageOfTotalNumberOfShares"
FOREIGN_PARTS = ("ForeignDirectInvestmentMember", "InstitutionsForeignPortfolioInvestorCategoryOneMember",
                 "InstitutionsForeignPortfolioInvestorCategoryTwoMember", "OtherInstitutionsForeignMember")
DOMESTIC_PARTS = ("MutualFundsOrUTIMember", "AlternativeInvestmentFundsMember", "BanksMember",
                  "InsuranceCompaniesMember", "ProvidentFundsOrPensionFundsMember",
                  "SovereignWealthFundsDomesticMember", "NBFCsRegisteredWithRBIMember",
                  "OtherFinancialInstitutionsMember")
RETAIL_MEMBER = "ResidentIndividualShareholdersHoldingNominalShareCapitalUpToRsTwoLakhMember"


def parse_xbrl_categories(xml_bytes: bytes) -> dict:
    """{category member name: raw shareholding value} for plain category rows only
    (contexts with exactly the category axis, no typed members)."""
    root = ET.fromstring(xml_bytes)
    cat_of = {}
    for c in root.iter():
        if _local(c.tag) != "context":
            continue
        explicit = [m for m in c.iter() if _local(m.tag) == "explicitMember"]
        typed = any(_local(m.tag) == "typedMember" for m in c.iter())
        if typed or len(explicit) != 1:
            continue
        if not (explicit[0].get("dimension") or "").endswith(CAT_AXIS):
            continue
        cat_of[c.get("id")] = (explicit[0].text or "").strip().rsplit(":", 1)[-1]
    out = {}
    for e in root.iter():
        if _local(e.tag) != PCT_TAG:
            continue
        member = cat_of.get(e.get("contextRef"))
        val = _num(e.text)
        if member and val is not None and member not in out:
            out[member] = float(e.text.strip().replace(",", ""))
    return out


def split_from_categories(cat: dict, summary_promoter=None) -> Optional[dict]:
    """Percent split {fii, dii, mf, retail}, or None if the filing doesn't add up."""
    raw = cat.get("ShareholdingOfPromoterAndPromoterGroupMember")
    if raw is None:
        return None
    scale = 100.0 if raw <= 1.5 else 1.0          # fractions (0.5044) or percent (50.44)
    if summary_promoter is not None and abs(raw * scale - summary_promoter) > 0.15:
        return None                                # does not match NSE's own summary

    def pct(name):
        v = cat.get(name)
        return None if v is None else round(v * scale, 2)

    def total(names):
        vals = [cat[n] for n in names if cat.get(n) is not None]
        return round(sum(vals) * scale, 2) if vals else None

    fii = pct("InstitutionsForeignMember")
    dii = pct("InstitutionsDomesticMember")
    return {"fii": fii if fii is not None else total(FOREIGN_PARTS),
            "dii": dii if dii is not None else total(DOMESTIC_PARTS),
            "mf": pct("MutualFundsOrUTIMember"), "retail": pct(RETAIL_MEMBER)}


def enrich_with_detail(quarters: list, xml_fetch=None, limit: int = 4, budget_s: float = 30.0) -> int:
    """Fills fii / dii / mf / retail on the newest quarters from their full filings.
    Never raises; a quarter that fails keeps null values. Returns how many worked."""
    done, t0, session = 0, time.time(), None
    for q in quarters[:limit]:
        if not q.get("xbrl") or time.time() - t0 > budget_s:
            continue
        try:
            if xml_fetch is None:
                if session is None:
                    session = _session()
                    session.get(NSE_HOME, timeout=10)
                data = fetch_xbrl(q["xbrl"], session)
            else:
                data = xml_fetch(q["xbrl"])
            split = split_from_categories(parse_xbrl_categories(data), q.get("promoter"))
        except (ShpError, ET.ParseError, requests.RequestException) as e:
            logger.warning("filing detail failed for %s: %s", q.get("date"), e)
            continue
        if split:
            q.update(split)
            done += 1
    return done


def probe_xbrl(symbol: str, raw_fetch=None, xml_fetch=None) -> dict:
    """Fetches the newest filing for a symbol and summarises it (never raises)."""
    info = {"symbol": symbol, "ok": False}
    try:
        data = (raw_fetch or fetch_nse)(symbol)
        quarters = parse_nse(data)
        link = next((q["xbrl"] for q in quarters if q.get("xbrl")), None)
        if not link:
            raise ShpError("no filing link in NSE's rows")
        info["filing"] = link
        info.update(summarize_xbrl((xml_fetch or fetch_xbrl)(link)))
        info["ok"] = True
    except ShpError as e:
        info["error"] = str(e)
    except ET.ParseError as e:
        info["error"] = f"filing is not valid XML: {e}"
    return info


def register_shareholding(app, supabase_getter):
    def _sb():
        try:
            return supabase_getter()
        except Exception as e:
            raise HTTPException(503, f"Database unavailable: {e}")

    @app.get("/shareholding/{symbol}")
    def shareholding(symbol: str, refresh: int = 0):
        sym = clean_symbol(symbol)
        if not SYMBOL_RE.match(sym):
            raise HTTPException(400, "Invalid stock symbol")
        try:
            return get_shareholding(_sb(), sym, force=bool(refresh))
        except ShpError as e:
            raise HTTPException(502, f"Shareholding is not available right now ({e})")

    @app.get("/shareholding-probe/{symbol}")
    def shareholding_probe(symbol: str, xbrl: int = 0, x_tick_secret: Optional[str] = Header(None)):
        secret = os.environ.get("PUSH_TICK_SECRET", "")
        if not secret or x_tick_secret != secret:
            raise HTTPException(403, "Bad tick secret")
        sym = clean_symbol(symbol)
        if not SYMBOL_RE.match(sym):
            raise HTTPException(400, "Invalid stock symbol")
        return probe_xbrl(sym) if xbrl else probe(sym)
