"""
allocation_api.py - sample equity / debt allocation with a projected growth chart.

One request gives: the equity-debt split, one fund for each side, and a yearly
projection for a monthly SIP or a lump sum. It is an illustration built from the
fund snapshot (funds_data.json), not personal advice.

Auto mode  : the split comes from the risk profile and the number of years, and
             each fund is the highest Sharpe ratio fund among large, established
             funds of the suitable categories.
Manual mode: the caller sets the equity percentage and may pick either fund.

Expected return of a fund = half its own record (5 year annualised return, else 3
year, else a debt fund's yield) plus half a long-run assumption (12% equity, 7%
debt), so one strong market cycle is not read as the forecast. Funds with no usable
record use the assumption alone, flagged hypothetical. The result is kept inside
4-18% (equity) or 3-10% (debt). The low and high lines are the base
return -/+ one standard error of the CAGR (volatility / sqrt(years)).

Routes (public data, no login):
  GET /allocation/options
  GET /allocation/plan?mode=sip|lumpsum&amount=10000&years=10
        [&risk=balanced][&equity_pct=60][&equity_fund=NAME][&debt_fund=NAME]
"""
import json
import math
import os
from functools import lru_cache
from typing import Optional

from fastapi import HTTPException

HERE = os.path.dirname(os.path.abspath(__file__))
FUNDS_FILE = os.path.join(HERE, "funds_data.json")

HYPOTHETICAL = {"equity": {"cagr": 12.0, "vol": 15.0}, "debt": {"cagr": 7.0, "vol": 3.0}}
CAGR_LIMITS = {"equity": (4.0, 18.0), "debt": (3.0, 10.0)}   # % a year
RISK_BASE = {"conservative": 30, "balanced": 60, "aggressive": 80}
CORRELATION = 0.2          # equity vs debt
MIN_AUM_CR = 1000.0        # auto picks only from funds at least this large

EQUITY_AUTO = ("Flexi Cap", "Large Cap", "Large & Mid Cap", "Multi Cap")
EQUITY_AUTO_AGGRESSIVE = EQUITY_AUTO + ("Mid Cap",)
DEBT_AUTO_SHORT = ("Low Duration", "Short Duration", "Banking & PSU")          # up to 3 years
DEBT_AUTO_LONG = ("Corporate Bond", "Banking & PSU", "Short Duration")
EQUITY_OPTIONS = EQUITY_AUTO + ("Mid Cap", "Small Cap", "Focused", "Value / Contra")
DEBT_OPTIONS = ("Corporate Bond", "Banking & PSU", "Short Duration", "Low Duration", "Medium Duration",
                "Ultra Short Duration", "Money Market", "Dynamic Bond", "Floater", "Gilt")
OPTIONS_PER_CATEGORY = {"equity": 8, "debt": 6}

# Lock-in and goal-specific funds are never suggested automatically or listed for picking.
EXCLUDED_WORDS = ("retirement", "children", "child", "tax saver", "elss", "lock-in", "lock in", "solution")

SIP_RANGE = (500, 1_000_000)
LUMP_RANGE = (1_000, 100_000_000)


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


def suitable(f: dict) -> bool:
    """Open-ended and no lock-in / goal-specific wrapper."""
    name = str(f.get("name", "")).lower()
    return (f.get("fund_type") in (None, "Open Ended")) and not any(w in name for w in EXCLUDED_WORDS)


def public_fund(f: dict) -> dict:
    return {"name": f.get("name"), "category": category_of(f), "aum": _num(f.get("aum")),
            "expense_ratio": _num(f.get("expense_ratio")), "r_1y": _num(f.get("r_1y")),
            "r_3y": _num(f.get("r_3y")), "r_5y": _num(f.get("r_5y")),
            "sharpe": _num(f.get("sharpe")), "std_dev": _num(f.get("std_dev")),
            "ytm": _num(f.get("ytm"))}


# --------------------------------------------------------------------------- rules
def auto_equity_pct(risk: str, years: int) -> int:
    base = RISK_BASE[risk]
    if years <= 3:
        base -= 30
    elif years <= 5:
        base -= 15
    elif years > 10:
        base += 5
    return int(min(90, max(10, round(base / 5) * 5)))


def _pool(bucket: str, risk: str, years: int) -> tuple:
    if bucket == "equity":
        return EQUITY_AUTO_AGGRESSIVE if risk == "aggressive" else EQUITY_AUTO
    return DEBT_AUTO_SHORT if years <= 3 else DEBT_AUTO_LONG


def pick_auto(bucket: str, risk: str, years: int) -> Optional[dict]:
    cats = _pool(bucket, risk, years)
    ok = [f for f in load_funds()
          if f.get("sheet") == bucket and category_of(f) in cats and suitable(f)
          and (_num(f.get("aum")) or 0) >= MIN_AUM_CR
          and _num(f.get("r_3y")) is not None and _num(f.get("sharpe")) is not None]
    if not ok:
        return None
    return max(ok, key=lambda f: (f["sharpe"], f["r_3y"]))


def find_fund(name: str, bucket: str) -> dict:
    key = (name or "").strip().lower()
    for f in load_funds():
        if f.get("sheet") == bucket and (str(f.get("name", "")).lower() == key
                                         or str(f.get("key", "")).lower() == key):
            return f
    raise HTTPException(400, f"No {bucket} fund named '{name}'")


def fund_return(f: Optional[dict], bucket: str) -> dict:
    """Expected annual return (%) and volatility for one side.

    expected = half the fund's own record + half a long-run assumption, so one strong
    market cycle does not become the forecast. With no usable record it is the
    assumption alone and is flagged hypothetical."""
    lo, hi = CAGR_LIMITS[bucket]
    hyp = HYPOTHETICAL[bucket]
    f = f or {}
    hist, basis = None, "hypothetical"
    for key, label in (("r_5y", "5-year"), ("r_3y", "3-year")):
        v = _num(f.get(key))
        if v is not None:
            hist, basis = v, label
            break
    if hist is None and bucket == "debt" and _num(f.get("ytm")) is not None:
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


# --------------------------------------------------------------------------- projection
def future_value(mode: str, amount: float, annual_pct: float, years: int) -> float:
    if mode == "lumpsum":
        return amount * (1 + annual_pct / 100) ** years
    n = 12 * years
    rm = (1 + annual_pct / 100) ** (1 / 12) - 1
    return amount * n if abs(rm) < 1e-12 else amount * ((1 + rm) ** n - 1) / rm


def invested(mode: str, amount: float, years: int) -> float:
    return amount if mode == "lumpsum" else amount * 12 * years


def build_plan(mode: str, amount: float, years: int, risk: str = "balanced",
               equity_pct: Optional[int] = None, equity_fund: Optional[str] = None,
               debt_fund: Optional[str] = None) -> dict:
    if mode not in ("sip", "lumpsum"):
        raise HTTPException(400, "mode must be sip or lumpsum")
    lo, hi = SIP_RANGE if mode == "sip" else LUMP_RANGE
    if not lo <= amount <= hi:
        raise HTTPException(400, f"amount must be between {lo} and {hi}")
    if not 1 <= years <= 40:
        raise HTTPException(400, "years must be between 1 and 40")
    if risk not in RISK_BASE:
        raise HTTPException(400, "risk must be conservative, balanced or aggressive")
    if equity_pct is not None and not 0 <= equity_pct <= 100:
        raise HTTPException(400, "equity_pct must be between 0 and 100")

    auto_split = equity_pct is None
    eq_pct = auto_equity_pct(risk, years) if auto_split else int(equity_pct)
    de_pct = 100 - eq_pct

    eq = find_fund(equity_fund, "equity") if equity_fund else pick_auto("equity", risk, years)
    de = find_fund(debt_fund, "debt") if debt_fund else pick_auto("debt", risk, years)
    r_eq, r_de = fund_return(eq, "equity"), fund_return(de, "debt")

    we, wd = eq_pct / 100, de_pct / 100
    base = we * r_eq["cagr"] + wd * r_de["cagr"]
    vol = math.sqrt((we * r_eq["vol"]) ** 2 + (wd * r_de["vol"]) ** 2
                    + 2 * CORRELATION * we * wd * r_eq["vol"] * r_de["vol"])
    band = vol / math.sqrt(years)
    rates = {"low": max(-10.0, base - band), "base": base, "high": base + band}

    series = []
    for y in range(0, years + 1):
        row = {"year": y, "invested": round(invested(mode, amount, y))}
        for k, r in rates.items():
            row[k] = round(future_value(mode, amount, r, y)) if y else round(invested(mode, amount, 0) if mode == "lumpsum" else 0)
        series.append(row)
    end = series[-1]
    eq_amt = round(amount * we)

    return {
        "mode": mode, "amount": amount, "years": years, "risk": risk,
        "auto_split": auto_split, "equity_pct": eq_pct, "debt_pct": de_pct,
        "split": {"equity": {"pct": eq_pct, "amount": eq_amt, "fund": public_fund(eq) if eq else None},
                  "debt": {"pct": de_pct, "amount": round(amount - eq_amt), "fund": public_fund(de) if de else None}},
        "returns": {"equity": r_eq, "debt": r_de,
                    "portfolio": {"base": round(base, 2), "low": round(rates["low"], 2),
                                  "high": round(rates["high"], 2), "volatility": round(vol, 2)}},
        "hypothetical": r_eq["hypothetical"] or r_de["hypothetical"],
        "projection": series,
        "summary": {"invested": end["invested"], "low": end["low"], "base": end["base"], "high": end["high"],
                    "gain_base": end["base"] - end["invested"]},
        "how": ("Split from your risk profile and time horizon; funds chosen by best Sharpe ratio among "
                "large, established funds." if auto_split else
                "Split and funds as you chose them."),
        "notes": ["Illustration only, not investment advice.",
                  "Past returns do not guarantee future returns. Low and high are one standard error "
                  "either side of the expected return.",
                  "Figures are for regular plans. Direct plans usually return about 0.5 to 1 percentage "
                  "point more.",
                  "Assumes the equity-debt mix is rebalanced every year."],
    }


def build_options() -> dict:
    out = {}
    for bucket, cats in (("equity", EQUITY_OPTIONS), ("debt", DEBT_OPTIONS)):
        rows = []
        for cat in cats:
            funds = [f for f in load_funds() if f.get("sheet") == bucket and category_of(f) == cat
                     and suitable(f) and _num(f.get("sharpe")) is not None and _num(f.get("r_3y")) is not None
                     and (_num(f.get("aum")) or 0) >= 300]
            funds.sort(key=lambda f: -f["sharpe"])
            rows += [public_fund(f) for f in funds[:OPTIONS_PER_CATEGORY[bucket]]]
        out[bucket] = rows
    return out


def register_allocation(app):
    @app.get("/allocation/options")
    def allocation_options():
        return build_options()

    @app.get("/allocation/plan")
    def allocation_plan(mode: str = "sip", amount: float = 10000, years: int = 10,
                        risk: str = "balanced", equity_pct: Optional[int] = None,
                        equity_fund: Optional[str] = None, debt_fund: Optional[str] = None):
        return build_plan(mode, amount, years, risk, equity_pct, equity_fund, debt_fund)
