"""
allocation_api.py - sample equity / hybrid / debt allocation, with a growth chart and a PDF.

Three asset classes, each with up to MAX_FUNDS funds. Every percentage can be zero:
a class at 0% is simply left out, and a fund at 0% weight stays listed but gets no money.
Funds sit in categories (Large Cap, Mid Cap, Small Cap, Corporate Bond ...) so the plan
also reads category by category.

Auto   : class split from risk profile + years; funds picked per category (best Sharpe
         ratio among large, established funds) with category weights set by the profile.
Manual : the caller gives the class split and/or the funds with their weights.

Expected return of a fund = half its own record (5 year annualised return, else 3 year,
else a debt fund's yield) plus half a long-run assumption, kept inside sensible limits.
No usable record -> the assumption alone, flagged hypothetical. Low and high are the
base return -/+ one standard error of the CAGR (volatility / sqrt(years)).

Routes (public data, no login):
  GET  /allocation/options          funds by class and category, for dropdowns
  POST /allocation/plan             JSON request -> plan
  POST /allocation/pdf              same JSON request -> PDF file

Request JSON:
  {"mode": "sip"|"lumpsum", "amount": 10000, "years": 10, "risk": "balanced",
   "classes": {"equity": 50, "hybrid": 0, "debt": 50},            # optional, must add to 100
   "picks": {"equity": [{"name": "...", "weight": 40}, ...]}}     # optional, per class, max 5
"""
import difflib
import json
import math
import os
import re
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Dict, List, Optional

from fastapi import HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

HERE = os.path.dirname(os.path.abspath(__file__))
FUNDS_FILE = os.path.join(HERE, "funds_data.json")

CLASSES = ("equity", "hybrid", "debt")
LABELS = {"equity": "Equity", "hybrid": "Hybrid", "debt": "Debt"}
MAX_FUNDS = 5

HYPOTHETICAL = {"equity": {"cagr": 12.0, "vol": 15.0}, "hybrid": {"cagr": 10.0, "vol": 9.0},
                "debt": {"cagr": 7.0, "vol": 3.0}}
CAGR_LIMITS = {"equity": (4.0, 18.0), "hybrid": (4.0, 15.0), "debt": (3.0, 10.0)}   # % a year
MIN_AUM_CR = {"equity": 1000.0, "hybrid": 500.0, "debt": 1000.0}                    # auto picks
WITHIN_CORR = {"equity": 0.85, "hybrid": 0.8, "debt": 0.8}
CROSS_CORR = {("equity", "hybrid"): 0.85, ("equity", "debt"): 0.2, ("hybrid", "debt"): 0.4}

BASE_SPLIT = {"conservative": (20, 20), "balanced": (45, 20), "aggressive": (65, 15)}  # equity, hybrid

TEMPLATES = {
    "equity": {
        "conservative": [("Large Cap", 50), ("Flexi Cap", 30), ("Large & Mid Cap", 20)],
        "balanced": [("Large Cap", 30), ("Flexi Cap", 30), ("Large & Mid Cap", 15), ("Mid Cap", 15), ("Small Cap", 10)],
        "aggressive": [("Large Cap", 20), ("Flexi Cap", 25), ("Large & Mid Cap", 10), ("Mid Cap", 25), ("Small Cap", 20)],
    },
    "hybrid": {
        "conservative": [("Equity Savings", 40), ("Dynamic Asset Allocation", 40), ("Conservative Hybrid", 20)],
        "balanced": [("Dynamic Asset Allocation", 40), ("Aggressive Hybrid", 30), ("Multi Asset Allocation", 30)],
        "aggressive": [("Aggressive Hybrid", 50), ("Multi Asset Allocation", 30), ("Dynamic Asset Allocation", 20)],
    },
    "debt": {
        "short": [("Low Duration", 40), ("Short Duration", 30), ("Banking & PSU", 30)],      # up to 3 years
        "long": [("Corporate Bond", 40), ("Banking & PSU", 30), ("Short Duration", 30)],
    },
}

OPTION_CATEGORIES = {
    "equity": ("Large Cap", "Flexi Cap", "Multi Cap", "Large & Mid Cap", "Mid Cap", "Small Cap",
               "Focused", "Value / Contra", "Index Funds"),
    "hybrid": ("Aggressive Hybrid", "Dynamic Asset Allocation", "Multi Asset Allocation",
               "Equity Savings", "Conservative Hybrid", "Balanced Hybrid", "Arbitrage"),
    "debt": ("Corporate Bond", "Banking & PSU", "Short Duration", "Low Duration", "Medium Duration",
             "Ultra Short Duration", "Money Market", "Dynamic Bond", "Floater", "Gilt"),
}
OPTIONS_WITH_HISTORY = 8      # per category, best Sharpe first
OPTIONS_NEW_FUNDS = 3         # per category, funds with no usable record (shown as hypothetical)

# Lock-in and goal-specific funds are never suggested or listed.
EXCLUDED_WORDS = ("retirement", "children", "child", "tax saver", "elss", "lock-in", "lock in", "solution")

SIP_RANGE = (500, 1_000_000)
LUMP_RANGE = (1_000, 100_000_000)
IST = timezone(timedelta(hours=5, minutes=30))


# --------------------------------------------------------------------------- data
@lru_cache(maxsize=1)
def load_funds() -> tuple:
    with open(FUNDS_FILE, encoding="utf-8") as fh:
        return tuple(json.load(fh))


def category_of(f: dict) -> str:
    c = f.get("classification") or ""
    return c.split(" : ", 1)[1].strip() if " : " in c else c.strip()


def _num(v) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


_TARGET_DATE = re.compile(r"\b20[2-5]\d\b|\bfmp\b|fixed (maturity|term)|target maturity")


def suitable(f: dict) -> bool:
    """Open-ended, no lock-in or goal-specific wrapper, and not a target-date / fixed-maturity
    fund (those wind up on a date, e.g. 'PSU Bond Plus SDL Apr 2026')."""
    name = str(f.get("name", "")).lower()
    return ((f.get("fund_type") in (None, "Open Ended"))
            and not any(w in name for w in EXCLUDED_WORDS) and not _TARGET_DATE.search(name))


def public_fund(f: dict) -> dict:
    return {"name": f.get("name"), "category": category_of(f), "aum": _num(f.get("aum")),
            "expense_ratio": _num(f.get("expense_ratio")), "r_1y": _num(f.get("r_1y")),
            "r_3y": _num(f.get("r_3y")), "r_5y": _num(f.get("r_5y")),
            "sharpe": _num(f.get("sharpe")), "std_dev": _num(f.get("std_dev")),
            "ytm": _num(f.get("ytm"))}


def has_history(f: dict) -> bool:
    return _num(f.get("r_5y")) is not None or _num(f.get("r_3y")) is not None


# --------------------------------------------------------------------------- rules
def _round5(x: float) -> int:
    return int(round(x / 5.0) * 5)


def auto_split(risk: str, years: int) -> Dict[str, int]:
    eq, hy = BASE_SPLIT[risk]
    eq += -25 if years <= 3 else -10 if years <= 5 else 5 if years > 10 else 0
    eq = min(85, max(5, _round5(eq)))
    hy = max(0, _round5(min(hy, 95 - eq)))
    return {"equity": eq, "hybrid": hy, "debt": 100 - eq - hy}


def best_in_category(cls: str, category: str) -> Optional[dict]:
    ok = [f for f in load_funds()
          if f.get("sheet") == cls and category_of(f) == category and suitable(f)
          and (_num(f.get("aum")) or 0) >= MIN_AUM_CR[cls]
          and _num(f.get("r_3y")) is not None and _num(f.get("sharpe")) is not None]
    return max(ok, key=lambda f: (f["sharpe"], f["r_3y"])) if ok else None


def _template(cls: str, risk: str, years: int) -> list:
    if cls == "debt":
        return TEMPLATES["debt"]["short" if years <= 3 else "long"]
    return TEMPLATES[cls][risk]


def auto_picks(cls: str, risk: str, years: int) -> List[dict]:
    """[{fund, weight}] one fund per category, weights adding to exactly 100."""
    found = [(best_in_category(cls, cat), w) for cat, w in _template(cls, risk, years)]
    found = [(f, w) for f, w in found if f]
    total = sum(w for _, w in found)
    if not found:
        return []
    weights = [round(w * 100.0 / total, 1) for _, w in found]
    weights[0] = round(weights[0] + (100.0 - sum(weights)), 1)
    return [{"fund": f, "weight": w} for (f, _), w in zip(found, weights)]


def find_fund(name: str, cls: str) -> dict:
    key = (name or "").strip().lower()
    for f in load_funds():
        if f.get("sheet") == cls and (str(f.get("name", "")).lower() == key
                                      or str(f.get("key", "")).lower() == key):
            return f
    raise HTTPException(400, f"No {cls} fund named '{name}'")


def fund_return(f: Optional[dict], cls: str) -> dict:
    """Expected annual return (%) and volatility for one fund, and how it was derived."""
    lo, hi = CAGR_LIMITS[cls]
    hyp = HYPOTHETICAL[cls]
    f = f or {}
    hist, basis = None, "hypothetical"
    for key, label in (("r_5y", "5-year"), ("r_3y", "3-year")):
        v = _num(f.get(key))
        if v is not None:
            hist, basis = v, label
            break
    if hist is None and cls == "debt" and _num(f.get("ytm")) is not None:
        hist, basis = f["ytm"] - (_num(f.get("expense_ratio")) or 0), "current yield"
    capped = False
    if hist is None:
        expected = hyp["cagr"]
    else:
        raw = 0.5 * hist + 0.5 * hyp["cagr"]
        expected = min(hi, max(lo, raw))
        capped = expected != raw
    vol = _num(f.get("std_dev"))
    return {"cagr": round(expected, 2), "historical": None if hist is None else round(hist, 2),
            "basis": basis, "capped": capped, "hypothetical": basis == "hypothetical",
            "vol": round(hyp["vol"] if vol is None else vol, 2)}


def split_amount(total: float, weights: List[float]) -> List[int]:
    """Whole rupees that add up to total, in proportion to weights."""
    s = sum(weights)
    if s <= 0:
        return [0] * len(weights)
    raw = [total * w / s for w in weights]
    out = [int(round(x)) for x in raw]
    out[max(range(len(out)), key=lambda i: raw[i])] += int(round(total)) - sum(out)
    return out


def _vol(weights: List[float], vols: List[float], corr) -> float:
    """Portfolio volatility; corr(i, j) is the correlation between holdings i and j."""
    var = 0.0
    for i, (wi, si) in enumerate(zip(weights, vols)):
        for j, (wj, sj) in enumerate(zip(weights, vols)):
            var += wi * wj * si * sj * (1.0 if i == j else corr(i, j))
    return math.sqrt(max(var, 0.0))


# --------------------------------------------------------------------------- projection
def future_value(mode: str, amount: float, annual_pct: float, years: int) -> float:
    if mode == "lumpsum":
        return amount * (1 + annual_pct / 100) ** years
    n = 12 * years
    rm = (1 + annual_pct / 100) ** (1 / 12) - 1
    return amount * n if abs(rm) < 1e-12 else amount * ((1 + rm) ** n - 1) / rm


def invested(mode: str, amount: float, years: int) -> float:
    return amount if mode == "lumpsum" else amount * 12 * years


# --------------------------------------------------------------------------- look-through
HOLDINGS_FILE = os.path.join(HERE, "holdings_data.json")
MILESTONE_YEARS = (5, 10, 15, 20)
TOP_STOCKS, TOP_BONDS, TOP_SECTORS = 15, 10, 8
_HOLD = None
_RATING_START = re.compile(r"\s*(crisil|icra|care|ind|brickwork|fitch|acuite|sovereign|unrated)\b", re.I)
_RATING_GRADE = re.compile(r"\b(AAA|AA[+-]?|A1\+?)(\b|$)")


def _name_key(s: str) -> str:
    s = (s or "").lower().replace("&", " and ")
    s = re.split(r" - an? | \u2013 | \u2014 |\(an? ", s)[0]
    s = re.sub(r"monthly|- \d+ \w+ \d{4}.*", "", s)
    s = re.sub(r"\b(regular|direct|plan|growth|option|scheme|fund|the|of)\b", " ", s)
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", s).split())


def _holdings_index() -> dict:
    global _HOLD
    if _HOLD is None:
        try:
            with open(HOLDINGS_FILE, encoding="utf-8") as fh:
                rows = json.load(fh)
        except (OSError, ValueError):
            rows = []
        _HOLD = {_name_key(r.get("fund", "")): r for r in rows if r.get("holdings")}
    return _HOLD


def find_holdings(fund_name: str) -> Optional[dict]:
    idx = _holdings_index()
    k = _name_key(fund_name)
    if k in idx:
        return idx[k]
    near = difflib.get_close_matches(k, list(idx), 1, 0.9)
    return idx[near[0]] if near else None


def _is_bond(h: dict) -> bool:
    name, sector = h.get("instrument") or "", h.get("sector") or ""
    return bool(re.match(r"\s*[\d.]*\s*%", name) or "government of india" in name.lower()
                or _RATING_START.match(sector) or _RATING_GRADE.search(sector))


def _bond_issuer(name: str) -> str:
    if re.search(r"\bgoi\b|government of india|\bg-?sec\b|\bgs\b", name or "", re.I):
        return "Government of India (G-Sec)"
    if re.search(r"treasury bill|t-bill|\btbill", name or "", re.I):
        return "Treasury Bills"
    n = re.sub(r"^\s*[\d.]*\s*%\s*", "", name or "")
    n = re.sub(r"\(.*?\)", " ", n).replace("*", " ")
    n = re.sub(r"\b(ncd|frn|zcb|sr|series)\b.*$", "", n, flags=re.I)
    n = re.sub(r"[^\w.)]+$", "", " ".join(n.split()))
    return n or (name or "Unknown")


def _issuer_key(name: str) -> str:
    n = name.lower().replace("limited", "ltd").replace("corporation", "corp")
    k = re.sub(r"[^a-z0-9]", "", n)
    for stem, alias in (("nationalbankforagri", "nabard"), ("smallindustriesdev", "sidbi")):
        if k.startswith(stem):
            return alias
    return k


def look_through(classes: list, amount: float) -> dict:
    """Company-wise exposure: each fund's disclosed holdings scaled by its share of the whole plan."""
    stocks, bonds, sectors = {}, {}, {}
    covered, without, as_on = 0.0, [], ""
    for c in classes:
        for f in c["funds"]:
            if f["weight"] <= 0 or f["share"] <= 0:
                continue
            rec = find_holdings(f["name"])
            if not rec:
                without.append(f["name"])
                continue
            covered += f["share"]
            as_on = as_on or rec.get("as_on_date") or ""
            for h in rec["holdings"]:
                pct = _num(h.get("pct_nav"))
                if not pct or pct <= 0:
                    continue
                part = f["share"] * pct / 100.0            # % of the whole plan
                if _is_bond(h):
                    name = _bond_issuer(h.get("instrument"))
                    e = bonds.setdefault(_issuer_key(name), {"name": name, "rating": h.get("sector") or "", "pct": 0.0})
                else:
                    name = re.sub(r"[^\w.)]+$", "", (h.get("instrument") or "Unknown").strip())
                    e = stocks.setdefault(h.get("isin") or name.lower(),
                                          {"name": name, "sector": h.get("sector") or "", "pct": 0.0})
                    sec = h.get("sector") or "Other"
                    sectors[sec] = sectors.get(sec, 0.0) + part
                e["pct"] += part

    def rank(d, n):
        rows = sorted(d.values(), key=lambda r: -r["pct"])[:n]
        return [dict(r, pct=round(r["pct"], 2), amount=round(amount * r["pct"] / 100.0)) for r in rows]

    return {
        "stocks": rank(stocks, TOP_STOCKS), "bonds": rank(bonds, TOP_BONDS),
        "sectors": [{"sector": k, "pct": round(v, 2)} for k, v in
                    sorted(sectors.items(), key=lambda kv: -kv[1])[:TOP_SECTORS]],
        "stocks_total": round(sum(r["pct"] for r in stocks.values()), 2),
        "bonds_total": round(sum(r["pct"] for r in bonds.values()), 2),
        "covered_pct": round(covered, 2), "without": without, "as_on": as_on,
    }



class FundPick(BaseModel):
    name: str
    weight: float = 0.0


class PlanRequest(BaseModel):
    mode: str = "sip"
    amount: float = 10000
    years: int = 10
    risk: str = "balanced"
    classes: Optional[Dict[str, float]] = None
    picks: Optional[Dict[str, List[FundPick]]] = None


def build_plan(req: PlanRequest) -> dict:
    mode, amount, years, risk = req.mode, req.amount, req.years, req.risk
    if mode not in ("sip", "lumpsum"):
        raise HTTPException(400, "mode must be sip or lumpsum")
    lo, hi = SIP_RANGE if mode == "sip" else LUMP_RANGE
    if not lo <= amount <= hi:
        raise HTTPException(400, f"amount must be between {lo} and {hi}")
    if not 1 <= years <= 40:
        raise HTTPException(400, "years must be between 1 and 40")
    if risk not in BASE_SPLIT:
        raise HTTPException(400, "risk must be conservative, balanced or aggressive")

    # ---- class split
    auto = req.classes is None
    if auto:
        split = {k: float(v) for k, v in auto_split(risk, years).items()}
    else:
        bad = [k for k in req.classes if k not in CLASSES]
        if bad:
            raise HTTPException(400, f"unknown asset class: {bad[0]}")
        split = {k: float(req.classes.get(k, 0)) for k in CLASSES}
        if any(v < 0 or v > 100 for v in split.values()):
            raise HTTPException(400, "each class must be between 0 and 100")
        if abs(sum(split.values()) - 100) > 0.01:
            raise HTTPException(400, f"classes must add up to 100 (they add up to {sum(split.values()):g})")

    # ---- funds per class
    picks_in = req.picks or {}
    for k in picks_in:
        if k not in CLASSES:
            raise HTTPException(400, f"unknown asset class in picks: {k}")
    normalized = False
    class_rows = {}
    for cls in CLASSES:
        if split[cls] <= 0:
            class_rows[cls] = []
            continue
        given = picks_in.get(cls)
        if given:
            if len(given) > MAX_FUNDS:
                raise HTTPException(400, f"at most {MAX_FUNDS} {cls} funds")
            names = [p.name.strip().lower() for p in given]
            if len(set(names)) != len(names):
                raise HTTPException(400, f"a {cls} fund is listed twice")
            if any(p.weight < 0 for p in given):
                raise HTTPException(400, "fund weights cannot be negative")
            total_w = sum(p.weight for p in given)
            if total_w <= 0:
                raise HTTPException(400, f"{LABELS[cls]} is {split[cls]:g}% but every fund weight is 0")
            if abs(total_w - 100) > 0.01:
                normalized = True
            class_rows[cls] = [{"fund": find_fund(p.name, cls), "weight": p.weight * 100.0 / total_w}
                               for p in given]
        else:
            rows = auto_picks(cls, risk, years)
            if not rows:
                raise HTTPException(400, f"no {cls} funds available")
            class_rows[cls] = rows

    # ---- per class numbers
    classes_out, by_category, any_hyp = [], {}, False
    class_amounts = split_amount(amount, [split[c] for c in CLASSES])
    live = []                          # classes that hold money: (cls, weight, expected, vol)
    for cls, class_amount in zip(CLASSES, class_amounts):
        rows = class_rows[cls]
        weights = [r["weight"] for r in rows]
        amounts = split_amount(class_amount, weights)
        returns = [fund_return(r["fund"], cls) for r in rows]
        funds_out = []
        for r, a, ret in zip(rows, amounts, returns):
            w = r["weight"]
            if w > 0 and ret["hypothetical"]:
                any_hyp = True
            row = dict(public_fund(r["fund"]), weight=round(w, 2), amount=a,
                       share=round(split[cls] * w / 100.0, 2), expected=ret["cagr"],
                       historical=ret["historical"], basis=ret["basis"],
                       hypothetical=ret["hypothetical"])
            funds_out.append(row)
            if w > 0:
                key = (cls, row["category"])
                agg = by_category.setdefault(key, {"class": cls, "category": row["category"],
                                                   "share": 0.0, "amount": 0})
                agg["share"] = round(agg["share"] + row["share"], 2)
                agg["amount"] += a
        active = [(w, ret) for w, ret in zip(weights, returns) if w > 0]
        expected = sum(w / 100.0 * ret["cagr"] for w, ret in active) if active else None
        vol = (_vol([w / 100.0 for w, _ in active], [ret["vol"] for _, ret in active],
                    lambda i, j: WITHIN_CORR[cls]) if active else None)
        classes_out.append({"key": cls, "label": LABELS[cls], "pct": round(split[cls], 2),
                            "amount": class_amount,
                            "expected": None if expected is None else round(expected, 2),
                            "volatility": None if vol is None else round(vol, 2), "funds": funds_out})
        if active:
            live.append((cls, split[cls] / 100.0, expected, vol))

    base = sum(w * e for _, w, e, _ in live)

    def cross(i, j):
        a, b = live[i][0], live[j][0]
        return CROSS_CORR.get((a, b)) or CROSS_CORR.get((b, a), 0.5)

    vol = _vol([w for _, w, _, _ in live], [v for _, _, _, v in live], cross)
    band = vol / math.sqrt(years)
    rates = {"low": max(-10.0, base - band), "base": base, "high": base + band}

    series = []
    for y in range(0, years + 1):
        row = {"year": y, "invested": round(invested(mode, amount, y))}
        for k, r in rates.items():
            row[k] = round(future_value(mode, amount, r, y)) if y else (round(amount) if mode == "lumpsum" else 0)
        series.append(row)
    end = series[-1]
    cats = sorted(by_category.values(), key=lambda c: -c["share"])
    miles = []
    for y in MILESTONE_YEARS:
        b = vol / math.sqrt(y)
        miles.append({"year": y, "invested": round(invested(mode, amount, y)),
                      "low": round(future_value(mode, amount, max(-10.0, base - b), y)),
                      "base": round(future_value(mode, amount, base, y)),
                      "high": round(future_value(mode, amount, base + b, y))})
    for m in miles:
        m["gain"] = m["base"] - m["invested"]

    return {
        "mode": mode, "amount": amount, "years": years, "risk": risk, "auto": auto,
        "normalized": normalized, "classes": classes_out, "by_category": cats,
        "returns": {"portfolio": {"base": round(base, 2), "low": round(rates["low"], 2),
                                  "high": round(rates["high"], 2), "volatility": round(vol, 2)}},
        "hypothetical": any_hyp, "projection": series, "milestones": miles,
        "look_through": look_through(classes_out, amount),
        "summary": {"invested": end["invested"], "low": end["low"], "base": end["base"],
                    "high": end["high"], "gain_base": end["base"] - end["invested"]},
        "how": ("Class split from your risk profile and time horizon; one fund per category, "
                "the best Sharpe ratio among large, established funds." if auto else
                "Split and funds as you chose them."),
        "notes": ["Illustration only, not investment advice.",
                  "Past returns do not guarantee future returns. Low and high are one standard error "
                  "either side of the expected return.",
                  "Figures are for regular plans. Direct plans usually return about 0.5 to 1 percentage "
                  "point more.",
                  "Assumes the mix is rebalanced every year."]
                 + (["Fund weights did not add to 100, so they were scaled to 100."] if normalized else [])
                 + (["Hypothetical: a fund has no usable track record, so a standard assumption "
                     "(12% equity, 10% hybrid, 7% debt a year) is used for it."] if any_hyp else []),
    }


def build_options() -> dict:
    """{class: {category: [fund, ...]}}. Best Sharpe first, then a few funds with no record."""
    out = {}
    for cls, cats in OPTION_CATEGORIES.items():
        out[cls] = {}
        for cat in cats:
            pool = [f for f in load_funds() if f.get("sheet") == cls and category_of(f) == cat and suitable(f)]
            seasoned = sorted((f for f in pool if has_history(f) and _num(f.get("sharpe")) is not None
                               and (_num(f.get("aum")) or 0) >= 300), key=lambda f: -f["sharpe"])
            fresh = sorted((f for f in pool if not has_history(f)), key=lambda f: -(_num(f.get("aum")) or 0))
            rows = ([dict(public_fund(f), has_history=True) for f in seasoned[:OPTIONS_WITH_HISTORY]]
                    + [dict(public_fund(f), has_history=False) for f in fresh[:OPTIONS_NEW_FUNDS]])
            if rows:
                out[cls][cat] = rows
    return out


# --------------------------------------------------------------------------- PDF
def _s(text) -> str:
    """fpdf's built-in fonts are latin-1 only."""
    t = str(text if text is not None else "")
    t = (t.replace("₹", "Rs.").replace("–", "-").replace("—", "-").replace("’", "'")
          .replace("‘", "'").replace("·", "-").replace("•", "-").replace("…", "..."))
    return t.encode("latin-1", "replace").decode("latin-1")


def _inr(v) -> str:
    n = int(round(v))
    s = str(abs(n))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts) + "," + tail
    return ("-" if n < 0 else "") + "Rs. " + s


def _short(v: float) -> str:
    a = abs(v)
    sign = "-" if v < 0 else ""
    if a >= 1e7:
        return f"{sign}Rs. {a / 1e7:.2f} Cr"
    if a >= 1e5:
        return f"{sign}Rs. {a / 1e5:.2f} L"
    if a >= 1e3:
        return f"{sign}Rs. {a / 1e3:.1f}K"
    return f"{sign}Rs. {a:.0f}"


def _pc(v, dp=1) -> str:
    return "-" if v is None else f"{v:.{dp}f}%"


def build_pdf(plan: dict, now: Optional[datetime] = None) -> bytes:
    from fpdf import FPDF

    now = now or datetime.now(IST)
    INK, MUTED, VAULT, GOLD = (24, 36, 34), (95, 111, 108), (10, 46, 36), (201, 162, 39)
    COL = {"equity": (201, 162, 39), "hybrid": (79, 195, 201), "debt": (108, 142, 245)}
    LINE = {"invested": (130, 130, 130), "low": (200, 70, 60), "base": (201, 162, 39), "high": (30, 140, 80)}

    class Doc(FPDF):
        def footer(self):
            self.set_y(-12)
            self.set_font("Helvetica", "", 7.5)
            self.set_text_color(*MUTED)
            self.cell(0, 5, _s("Illustration only, not investment advice. Past returns do not guarantee "
                               "future returns."), align="L")
            self.set_x(-30)
            self.cell(18, 5, f"Page {self.page_no()}", align="R")

    pdf = Doc(orientation="P", unit="mm", format="A4")
    pdf.set_margins(12, 12, 12)
    pdf.set_auto_page_break(auto=True, margin=16)
    pdf.add_page()
    W = 186.0

    def clip(text, width, size, style=""):
        pdf.set_font("Helvetica", style, size)
        t = _s(text)
        while t and pdf.get_string_width(t) > width - 1.5:
            t = t[:-1]
        return t

    def section(title):
        pdf.ln(4)
        pdf.set_fill_color(*GOLD)
        pdf.rect(12, pdf.get_y() + 1.2, 2.2, 5, "F")
        pdf.set_x(16)
        pdf.set_font("Helvetica", "B", 11.5)
        pdf.set_text_color(*INK)
        pdf.cell(0, 7.5, _s(title), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(1)

    # ---- header
    pdf.set_fill_color(*VAULT)
    pdf.rect(0, 0, 210, 30, "F")
    pdf.set_xy(12, 8)
    pdf.set_font("Helvetica", "B", 18)
    pdf.set_text_color(*GOLD)
    pdf.cell(0, 8, "Advantage - Sample Allocation", new_x="LMARGIN", new_y="NEXT")
    pdf.set_x(12)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(220, 239, 230)
    pdf.cell(0, 6, _s(f"Generated {now.strftime('%d %b %Y, %I:%M %p')} IST"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_y(36)

    # ---- plan facts
    per = " a month" if plan["mode"] == "sip" else ""
    facts = [("Investment", ("Monthly SIP" if plan["mode"] == "sip" else "Lump sum") + " of " + _inr(plan["amount"]) + per),
             ("Period", f"{plan['years']} {'year' if plan['years'] == 1 else 'years'}"),
             ("How chosen", f"Auto allocation, {plan['risk']} profile" if plan["auto"] else "Custom (manual) allocation"),
             ("Total invested", _inr(plan["summary"]["invested"]))]
    for k, v in facts:
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(*MUTED)
        pdf.cell(38, 5.5, _s(k))
        pdf.set_font("Helvetica", "B", 9.5)
        pdf.set_text_color(*INK)
        pdf.cell(0, 5.5, _s(v), new_x="LMARGIN", new_y="NEXT")

    if plan["hypothetical"]:
        pdf.ln(2)
        pdf.set_fill_color(251, 239, 215)
        pdf.set_text_color(154, 91, 0)
        pdf.set_font("Helvetica", "B", 8.5)
        pdf.cell(W, 6.5, _s("  HYPOTHETICAL: at least one fund has no usable track record, so a standard "
                            "assumption is used."), fill=True, new_x="LMARGIN", new_y="NEXT")

    # ---- class mix bar
    section("Asset mix")
    y0, x = pdf.get_y(), 12.0
    for c in plan["classes"]:
        if c["pct"] > 0:
            w = W * c["pct"] / 100.0
            pdf.set_fill_color(*COL[c["key"]])
            pdf.rect(x, y0, w, 7, "F")
            x += w
    pdf.set_y(y0 + 9)
    for c in plan["classes"]:
        pdf.set_fill_color(*COL[c["key"]])
        pdf.rect(pdf.get_x(), pdf.get_y() + 1.3, 3, 3, "F")
        pdf.set_x(pdf.get_x() + 5)
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_text_color(*INK)
        txt = f"{c['label']} {c['pct']:g}%  ({_inr(c['amount'])}{per})"
        pdf.cell(pdf.get_string_width(txt) + 8, 5.5, _s(txt))
    pdf.ln(7)

    # ---- fund table
    section("Funds")
    cols = [("Class", 13), ("Category", 38), ("Fund", 55), ("Weight", 14), ("Of total", 15), ("Amount", 24),
            ("Past", 13), ("Expected", 14)]

    def header_row():
        pdf.set_fill_color(*VAULT)
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Helvetica", "B", 7.5)
        for name, w in cols:
            pdf.cell(w, 6, _s(name), fill=True, align="L" if name in ("Class", "Category", "Fund") else "R")
        pdf.ln(6)

    header_row()
    shade = False
    for c in plan["classes"]:
        for f in c["funds"]:
            if pdf.get_y() > 265:
                pdf.add_page()
                header_row()
            pdf.set_fill_color(*((244, 247, 246) if shade else (255, 255, 255)))
            shade = not shade
            zero = f["weight"] <= 0
            pdf.set_text_color(*(MUTED if zero else INK))
            hyp = f["hypothetical"]
            cells = [(c["label"], "L", ""), (f["category"], "L", ""), (f["name"].replace(" - Regular Plan - Growth", "").replace("Fund - ", "Fund "), "L", ""),
                     (_pc(f["weight"]), "R", ""), (_pc(f["share"]), "R", ""),
                     (_inr(f["amount"]) if f["amount"] else "-", "R", ""),
                     ("n/a" if f["historical"] is None else _pc(f["historical"]), "R", ""),
                     (_pc(f["expected"]) + ("*" if hyp else ""), "R", "")]
            for (txt, al, st), (_, w) in zip(cells, cols):
                pdf.cell(w, 5.6, clip(txt, w, 7.5, st), fill=True, align=al)
            pdf.ln(5.6)
    pdf.set_font("Helvetica", "", 7.5)
    pdf.set_text_color(*MUTED)
    pdf.ln(1)
    pdf.cell(0, 4.5, _s("Past = the fund's own 5-year (else 3-year) annual return. Expected = half the past return "
                        "plus half a long-run assumption. * hypothetical."), new_x="LMARGIN", new_y="NEXT")

    # ---- by category
    if plan["by_category"]:
        section("Category-wise allocation")
        for cat in plan["by_category"]:
            if pdf.get_y() > 270:
                pdf.add_page()
            pdf.set_font("Helvetica", "", 8.5)
            pdf.set_text_color(*INK)
            pdf.cell(52, 5.4, clip(f"{cat['category']}  ({LABELS[cat['class']]})", 52, 8.5))
            bx = pdf.get_x()
            pdf.set_fill_color(228, 236, 233)
            pdf.rect(bx, pdf.get_y() + 1.3, 80, 2.8, "F")
            pdf.set_fill_color(*COL[cat["class"]])
            pdf.rect(bx, pdf.get_y() + 1.3, max(0.6, 80 * cat["share"] / 100.0), 2.8, "F")
            pdf.set_x(bx + 83)
            pdf.cell(16, 5.4, _pc(cat["share"]), align="R")
            pdf.cell(35, 5.4, _s(_inr(cat["amount"]) + per), align="R")
            pdf.ln(5.4)

    # ---- projection
    if pdf.get_y() > 175:
        pdf.add_page()
    section("Projected growth")
    s, pr = plan["summary"], plan["returns"]["portfolio"]
    boxes = [("Invested", _short(s["invested"]), MUTED), ("Expected", _short(s["base"]), GOLD),
             ("Low", _short(s["low"]), LINE["low"]), ("High", _short(s["high"]), LINE["high"])]
    bw = W / 4.0
    y0 = pdf.get_y()
    for i, (lab, val, colr) in enumerate(boxes):
        pdf.set_xy(12 + i * bw, y0)
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(*MUTED)
        pdf.cell(bw, 4.5, _s(lab))
        pdf.set_xy(12 + i * bw, y0 + 4.5)
        pdf.set_font("Helvetica", "B", 13)
        pdf.set_text_color(*colr)
        pdf.cell(bw, 7, _s(val))
    pdf.set_xy(12, y0 + 13)
    pdf.set_font("Helvetica", "", 8.5)
    pdf.set_text_color(*INK)
    pdf.cell(0, 5, _s(f"Expected {pr['base']:.1f}% a year for the whole mix (range {pr['low']:.1f}% to "
                      f"{pr['high']:.1f}%). Gain over money put in: {_short(s['gain_base'])}."),
             new_x="LMARGIN", new_y="NEXT")

    # chart
    cx, cy, cw, ch = 30.0, pdf.get_y() + 4, 166.0, 62.0
    pts = plan["projection"]
    ymax = max(p["high"] for p in pts) * 1.05 or 1.0
    xmax = max(1, plan["years"])
    pdf.set_draw_color(225, 232, 230)
    pdf.set_line_width(0.2)
    pdf.set_font("Helvetica", "", 7)
    pdf.set_text_color(*MUTED)
    for i in range(5):
        gy = cy + ch - ch * i / 4.0
        pdf.line(cx, gy, cx + cw, gy)
        pdf.set_xy(12, gy - 2)
        pdf.cell(16, 4, _s(_short(ymax * i / 4.0)), align="R")
    step = max(1, math.ceil(plan["years"] / 6))
    for yr in range(0, plan["years"] + 1, step):
        gx = cx + cw * yr / xmax
        pdf.set_xy(gx - 5, cy + ch + 1)
        pdf.cell(10, 4, f"{yr}y", align="C")
    for key in ("invested", "low", "high", "base"):
        pdf.set_draw_color(*LINE[key])
        pdf.set_line_width(0.9 if key == "base" else 0.5)
        prev = None
        for p in pts:
            px, py = cx + cw * p["year"] / xmax, cy + ch - ch * p[key] / ymax
            if prev:
                pdf.line(prev[0], prev[1], px, py)
            prev = (px, py)
    pdf.set_y(cy + ch + 7)
    lx = 30.0
    for key, lab in (("invested", "Invested"), ("low", "Low"), ("base", "Expected"), ("high", "High")):
        pdf.set_draw_color(*LINE[key])
        pdf.set_line_width(0.9)
        pdf.line(lx, pdf.get_y() + 2.6, lx + 7, pdf.get_y() + 2.6)
        pdf.set_xy(lx + 9, pdf.get_y())
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(*MUTED)
        pdf.cell(20, 5.2, lab)
        lx += 34
    pdf.ln(8)

    # ---- value at 5 / 10 / 15 / 20 years
    if pdf.get_y() > 215:
        pdf.add_page()
    section("Invested vs expected value")
    bw = W / 4.0
    y0 = pdf.get_y()
    bh = 36.0
    for i, m in enumerate(plan["milestones"]):
        x = 12 + i * bw
        pdf.set_draw_color(205, 218, 214)
        pdf.set_fill_color(247, 250, 249)
        pdf.set_line_width(0.3)
        pdf.rect(x + 0.8, y0, bw - 1.6, bh, "DF")
        pdf.set_xy(x + 3, y0 + 1.8)
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_text_color(*VAULT)
        pdf.cell(bw - 6, 5, f"{m['year']} years")
        rows = [("Invested", _short(m["invested"]), MUTED), ("Expected", _short(m["base"]), GOLD),
                ("Gain", ("+" if m["gain"] >= 0 else "") + _short(m["gain"]), LINE["high"]),
                ("Range", _short(m["low"]) + " - " + _short(m["high"]), MUTED)]
        for j, (lab, val, colr) in enumerate(rows):
            yy = y0 + 8.5 + j * 6.6
            pdf.set_xy(x + 3, yy)
            pdf.set_font("Helvetica", "", 7)
            pdf.set_text_color(*MUTED)
            pdf.cell(14, 5, _s(lab))
            pdf.set_xy(x + 15, yy)
            pdf.set_font("Helvetica", "B" if lab in ("Expected", "Gain") else "", 8 if lab != "Range" else 6.5)
            pdf.set_text_color(*colr)
            pdf.cell(bw - 18, 5, _s(val), align="R")
    pdf.set_y(y0 + bh + 2)
    pdf.set_font("Helvetica", "", 7.5)
    pdf.set_text_color(*MUTED)
    pdf.multi_cell(W, 4, _s("Same mix and same expected return carried to each horizon" +
                            (" (monthly SIP). " if plan["mode"] == "sip" else " (one-time investment). ") +
                            "Range is one standard error either side of expected; it narrows as the horizon grows."),
                   new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    # ---- company-wise look-through
    lt = plan.get("look_through") or {}
    section("Stock-wise and bond-wise (company-wise)")
    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(*MUTED)
    if not lt.get("stocks") and not lt.get("bonds"):
        pdf.multi_cell(W, 4.5, _s("Company-level holdings are not available for the funds in this plan."),
                       new_x="LMARGIN", new_y="NEXT")
    else:
        pdf.multi_cell(W, 4.2, _s(
            f"Each fund's disclosed holdings scaled by its share of your plan. Covers {lt['covered_pct']:.0f}% of the plan"
            + (f" (holdings as on {lt['as_on']})" if lt.get("as_on") else "") + "."),
            new_x="LMARGIN", new_y="NEXT")
        pdf.ln(1)

        def table(title, rows, second, total):
            if not rows:
                return
            if pdf.get_y() > 245:
                pdf.add_page()
            pdf.set_font("Helvetica", "B", 9)
            pdf.set_text_color(*VAULT)
            pdf.cell(0, 6, _s(f"{title}  -  top {len(rows)} make up {total:.1f}% of the plan"),
                     new_x="LMARGIN", new_y="NEXT")
            cw = [90, 52, 18, 26]
            pdf.set_fill_color(*VAULT)
            pdf.set_text_color(255, 255, 255)
            pdf.set_font("Helvetica", "B", 7.5)
            for (lab, al), w in zip((("Company", "L"), (second, "L"), ("Of plan", "R"), ("Amount", "R")), cw):
                pdf.cell(w, 5.6, lab, fill=True, align=al)
            pdf.ln(5.6)
            for i, r in enumerate(rows):
                if pdf.get_y() > 272:
                    pdf.add_page()
                pdf.set_fill_color(*((244, 247, 246) if i % 2 else (255, 255, 255)))
                pdf.set_text_color(*INK)
                sub = r.get("sector") or r.get("rating") or ""
                cells = [(r["name"], "L"), (sub, "L"), (_pc(r["pct"], 2), "R"),
                         (_inr(r["amount"]) + per if r["amount"] else "-", "R")]
                for (txt, al), w in zip(cells, cw):
                    pdf.cell(w, 5.2, clip(txt, w, 7.5), fill=True, align=al)
                pdf.ln(5.2)
            pdf.ln(2)

        table("Stocks", lt["stocks"], "Sector", lt["stocks_total"])
        table("Bonds (by issuer)", lt["bonds"], "Rating", lt["bonds_total"])
        if lt.get("sectors"):
            if pdf.get_y() > 245:
                pdf.add_page()
            pdf.set_font("Helvetica", "B", 9)
            pdf.set_text_color(*VAULT)
            pdf.cell(0, 6, "Stock sectors", new_x="LMARGIN", new_y="NEXT")
            for s_ in lt["sectors"]:
                pdf.set_font("Helvetica", "", 8.5)
                pdf.set_text_color(*INK)
                pdf.cell(60, 5.2, clip(s_["sector"], 60, 8.5))
                bx = pdf.get_x()
                pdf.set_fill_color(228, 236, 233)
                pdf.rect(bx, pdf.get_y() + 1.3, 70, 2.8, "F")
                pdf.set_fill_color(*COL["equity"])
                top = lt["sectors"][0]["pct"] or 1.0
                pdf.rect(bx, pdf.get_y() + 1.3, max(0.6, 70 * s_["pct"] / top), 2.8, "F")
                pdf.set_x(bx + 73)
                pdf.cell(18, 5.2, _pc(s_["pct"], 2), align="R")
                pdf.ln(5.2)
        if lt.get("without"):
            pdf.ln(1)
            pdf.set_font("Helvetica", "", 7.5)
            pdf.set_text_color(*MUTED)
            names = ", ".join(n.replace(" - Regular Plan - Growth", "") for n in lt["without"])
            pdf.multi_cell(W, 4, _s("Holdings not available for: " + names + ". Their share is not in the tables above."),
                           new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    # ---- notes
    section("Notes")
    pdf.set_font("Helvetica", "", 8.5)
    pdf.set_text_color(*INK)
    for n in plan["notes"]:
        pdf.set_x(12)
        pdf.multi_cell(W, 4.8, _s("- " + n), new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


def register_allocation(app):
    @app.get("/allocation/options")
    def allocation_options():
        return build_options()

    @app.post("/allocation/plan")
    def allocation_plan(req: PlanRequest):
        return build_plan(req)

    @app.post("/allocation/pdf")
    def allocation_pdf(req: PlanRequest):
        data = build_pdf(build_plan(req))
        return Response(content=data, media_type="application/pdf",
                        headers={"Content-Disposition": 'attachment; filename="advantage_allocation.pdf"'})
