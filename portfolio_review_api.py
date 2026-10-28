"""
portfolio_review_api.py - mutual fund portfolio review.

    GET  /portfolio/funds?q=hdfc flexi        search the fund database by name (for the entry form)
    POST /portfolio/parse                     {filename, data_base64}  Excel (.xlsx) or CSV -> holdings
    POST /portfolio/review                    {holdings:[{name, invested, current, date}]} -> the review

For every holding: your absolute return and annualised return (CAGR) from the date you gave, set against
  * the fund's category (median trailing return of funds in the same category) and
  * the fund's benchmark (median trailing return of the index funds / ETFs that track it),
at the horizon closest to how long you have held it. Portfolio level: XIRR, allocation, cost, duplication.
Nothing is stored. Public routes (no personal data is kept). Illustration only, not investment advice.
"""
import base64
import csv
import difflib
import io
import math
import re
import statistics
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

from fastapi import HTTPException
from pydantic import BaseModel

import allocation_api as al

IST = timezone(timedelta(hours=5, minutes=30))
MAX_HOLDINGS = 60
MAX_UPLOAD_BYTES = 3_000_000
MAX_SHEET_ROWS = 3000
INDEX_CATS = ("EQ-INDX", "EQ-ETF", "PM-ETF")
MIN_PEERS = 3
VERDICT_PP = {"debt": 0.75, "default": 1.5}
SMALL_AUM_CR = 300.0
ASSET = {"EQ": "Equity", "HY": "Hybrid", "DT": "Debt", "PM": "Gold & silver", "FOF": "Fund of funds"}

# --------------------------------------------------------------------------- names
_DROP = re.compile(r"\b(regular|direct|plan|growth|option|scheme|fund|the|of|idcw|dividend|payout|reinvestment|"
                   r"reinvest|mutual|an|open|ended)\b")
_ALIAS = (("prudential", "pru"), ("sun life", "sl"), ("and", " "), ("limited", ""), ("ltd", ""))


def name_key(s: str) -> str:
    s = (s or "").lower().replace("&", " and ")
    s = re.split(r" - an? | – | — |\(an? ", s)[0]
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    for a, b in _ALIAS:
        s = re.sub(r"\b" + a + r"\b", b, s)
    s = _DROP.sub(" ", s)
    return " ".join(s.split())


_INDEX = None


def _index():
    global _INDEX
    if _INDEX is None:
        keys = {}
        for f in al.load_funds():
            k = name_key(f.get("name", ""))
            if k and k not in keys:
                keys[k] = f
        _INDEX = keys
    return _INDEX


def match_fund(name: str):
    """(fund record, score 0-1) or (None, best score)."""
    idx = _index()
    k = name_key(name)
    if not k:
        return None, 0.0
    if k in idx:
        return idx[k], 1.0
    utoks = set(k.split())
    best, best_score = None, 0.0
    unums = {t for t in utoks if t.isdigit()}
    for key, f in idx.items():
        ctoks = set(key.split())
        if {t for t in ctoks if t.isdigit()} != unums:                # Nifty 50 is not Nifty 500
            continue
        if key.split()[0] != k.split()[0]:                            # same fund house (SBI is not HSBC)
            continue
        score = 0.0
        if utoks == ctoks:                                            # same words, different order
            score = 0.98
        else:
            sm = difflib.SequenceMatcher(None, k, key)
            if sm.real_quick_ratio() >= 0.78 and sm.quick_ratio() >= 0.78:
                score = sm.ratio()
        if score >= 0.88 and score > best_score:
            best, best_score = f, score
    return (best, round(best_score, 2)) if best_score >= 0.78 else (None, round(best_score, 2))


def search_funds(q: str, limit: int = 12) -> list:
    toks = name_key(q).split()
    if not toks:
        return []
    out = []
    for key, f in _index().items():
        if all(t in key for t in toks):
            out.append((len(key), f["name"]))
    out.sort()
    if len(out) < limit:
        for key, f in _index().items():
            if f["name"] not in [n for _, n in out] and difflib.SequenceMatcher(None, " ".join(toks), key).ratio() > 0.7:
                out.append((len(key) + 100, f["name"]))
    return [n for _, n in out[:limit]]


# --------------------------------------------------------------------------- peer data
_MED = {}


def _median(vals):
    vals = [v for v in vals if v is not None]
    return round(statistics.median(vals), 2) if vals else None


def _group(kind: str, key: str):
    """medians of r_1y/r_3y/r_5y/expense for a category code ('cat') or a benchmark ('bench')."""
    ck = (kind, key)
    if ck in _MED:
        return _MED[ck]
    if kind == "cat":
        peers = [f for f in al.load_funds() if f.get("cat_code") == key]
    else:
        peers = [f for f in al.load_funds() if f.get("benchmark") == key and f.get("cat_code") in INDEX_CATS]
    res = None
    if len(peers) >= (5 if kind == "cat" else MIN_PEERS):
        res = {h: _median([al._num(p.get(h)) for p in peers]) for h in ("r_1y", "r_3y", "r_5y")}
        res["expense"] = _median([al._num(p.get("expense_ratio")) for p in peers])
        res["n"] = len(peers)
    _MED[ck] = res
    return res


def horizon_for(years: float) -> Optional[str]:
    if years < 1:
        return None
    if years < 2.5:
        return "r_1y"
    if years < 4.5:
        return "r_3y"
    return "r_5y"


def _pick(group, horizon):
    """value for the horizon, falling back to the next shorter one if missing."""
    if not group or not horizon:
        return None, None
    order = {"r_5y": ["r_5y", "r_3y", "r_1y"], "r_3y": ["r_3y", "r_1y"], "r_1y": ["r_1y"]}[horizon]
    for h in order:
        if group.get(h) is not None:
            return group[h], h
    return None, None


# --------------------------------------------------------------------------- numbers and dates
def parse_money(v) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(v) else None
    s = re.sub(r"(?i)\b(rs|inr)\b\.?", "", str(v).replace(",", ""))
    s = re.sub(r"[^\d.\-()]", "", s)
    if not s:
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        x = float(s)
    except ValueError:
        return None
    return -x if neg else x


_DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%d-%b-%Y", "%d %b %Y", "%d-%B-%Y", "%d %B %Y",
                 "%d/%m/%y", "%d-%m-%y", "%b %d, %Y", "%Y/%m/%d")


def parse_date(v) -> Optional[date]:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, (int, float)) and 20000 < float(v) < 80000:       # Excel serial date
        return (datetime(1899, 12, 30) + timedelta(days=float(v))).date()
    s = str(v).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def xirr(flows: list) -> Optional[float]:
    """flows: [(date, amount)] -> annual rate in %, or None. Bisection; needs both signs."""
    flows = [(d, a) for d, a in flows if a]
    if len(flows) < 2 or not (any(a > 0 for _, a in flows) and any(a < 0 for _, a in flows)):
        return None
    t0 = min(d for d, _ in flows)

    def f(r):
        return sum(a / (1 + r) ** ((d - t0).days / 365.0) for d, a in flows)

    lo, hi = -0.99, 20.0
    flo, fhi = f(lo), f(hi)
    if flo * fhi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        fm = f(mid)
        if abs(fm) < 1e-7:
            break
        if flo * fm < 0:
            hi, fhi = mid, fm
        else:
            lo, flo = mid, fm
    return round(mid * 100, 2)


# --------------------------------------------------------------------------- the review
class Holding(BaseModel):
    name: str
    invested: float
    current: float
    date: Optional[str] = None


class ReviewRequest(BaseModel):
    holdings: List[Holding]


def asset_class(f: Optional[dict]) -> str:
    if not f:
        return "Unclassified"
    return ASSET.get((f.get("cat_code") or "").split("-")[0], "Other")


def review_holding(h: Holding, today: date) -> dict:
    inv, cur = float(h.invested), float(h.current)
    d = parse_date(h.date)
    f, score = match_fund(h.name)
    days = (today - d).days if d else None
    years = days / 365.0 if days is not None else None
    abs_pct = round((cur / inv - 1) * 100, 2)
    cagr = round(((cur / inv) ** (1 / years) - 1) * 100, 2) if (years and years >= 1 and cur > 0) else None

    out = {"name": h.name.strip(), "matched": f is not None, "match_score": score,
           "matched_name": f.get("name") if f else None, "invested": round(inv, 2), "current": round(cur, 2),
           "gain": round(cur - inv, 2), "abs_pct": abs_pct, "date": d.isoformat() if d else None, "days": days,
           "cagr_pct": cagr, "asset_class": asset_class(f), "category": al.category_of(f) if f else None,
           "benchmark": f.get("benchmark") if f else None, "plan_note": None, "horizon": None,
           "category_pct": None, "benchmark_pct": None, "fund_pct": None, "vs_category_pp": None,
           "vs_benchmark_pp": None, "verdict": "No comparison", "verdict_why": "", "expense_ratio": None,
           "expense_median": None, "aum": None, "flags": []}
    if re.search(r"\bdirect\b", h.name, re.I):
        out["plan_note"] = "Direct plan: compared with the regular-plan record, so it should do about 0.5-1 pp better."

    hz = horizon_for(years) if years is not None else None
    if f:
        out["expense_ratio"], out["aum"] = al._num(f.get("expense_ratio")), al._num(f.get("aum"))
        cat = _group("cat", f.get("cat_code"))
        ben = _group("bench", f.get("benchmark")) if f.get("benchmark") else None
        out["expense_median"] = cat.get("expense") if cat else None
        cat_v, cat_h = _pick(cat, hz)
        ben_v, ben_h = _pick(ben, hz)
        fund_v, _ = _pick({k: al._num(f.get(k)) for k in ("r_1y", "r_3y", "r_5y")}, hz)
        out["horizon"] = {"r_1y": "1 year", "r_3y": "3 years", "r_5y": "5 years"}.get(ben_h or cat_h)
        out["category_pct"], out["benchmark_pct"], out["fund_pct"] = cat_v, ben_v, fund_v
        if cagr is not None:
            if cat_v is not None:
                out["vs_category_pp"] = round(cagr - cat_v, 2)
            if ben_v is not None:
                out["vs_benchmark_pp"] = round(cagr - ben_v, 2)
        if out["expense_ratio"] is not None and out["expense_median"] is not None \
                and out["expense_ratio"] > out["expense_median"] + 0.4 and not out["plan_note"]:
            out["flags"].append(f"Costlier than most {out['category'] or 'similar'} funds "
                                f"({out['expense_ratio']:.2f}% vs {out['expense_median']:.2f}% typical)")
        if out["aum"] is not None and out["aum"] < SMALL_AUM_CR:
            out["flags"].append(f"Small fund (assets about Rs. {out['aum']:.0f} crore)")
        if f.get("fund_type") not in (None, "Open Ended"):
            out["flags"].append(f"{f.get('fund_type')} scheme")
    else:
        out["flags"].append("Not found in our fund database, so shown without comparison")

    # verdict
    thr = VERDICT_PP["debt"] if out["asset_class"] == "Debt" else VERDICT_PP["default"]
    if d is None:
        out["verdict"], out["verdict_why"] = "Add a date", "A date is needed to annualise the return."
    elif years < 1:
        out["verdict"], out["verdict_why"] = "Too early", "Held for under a year; annualised comparison is not meaningful yet."
    elif out["matched"]:
        ref, ref_name = (out["vs_benchmark_pp"], "benchmark") if out["vs_benchmark_pp"] is not None else \
                        (out["vs_category_pp"], "category")
        if ref is None:
            out["verdict"], out["verdict_why"] = "No comparison", "No peer data for this fund's category."
        elif ref > thr:
            out["verdict"], out["verdict_why"] = "Ahead", f"{ref:+.1f} pp a year against its {ref_name}."
        elif ref < -thr:
            out["verdict"], out["verdict_why"] = "Behind", f"{ref:+.1f} pp a year against its {ref_name}."
        else:
            out["verdict"], out["verdict_why"] = "In line", f"{ref:+.1f} pp a year against its {ref_name}."
    return out


def build_review(holdings: List[Holding], today: Optional[date] = None) -> dict:
    today = today or datetime.now(IST).date()
    if not holdings:
        raise HTTPException(400, "Add at least one holding")
    if len(holdings) > MAX_HOLDINGS:
        raise HTTPException(400, f"At most {MAX_HOLDINGS} holdings at a time")
    for i, h in enumerate(holdings, 1):
        if not h.name.strip():
            raise HTTPException(400, f"Row {i}: fund name is empty")
        if not (math.isfinite(h.invested) and h.invested > 0):
            raise HTTPException(400, f"Row {i} ({h.name[:30]}): invested value must be above 0")
        if not (math.isfinite(h.current) and h.current >= 0):
            raise HTTPException(400, f"Row {i} ({h.name[:30]}): current value cannot be negative")
        if h.date:
            d = parse_date(h.date)
            if d is None:
                raise HTTPException(400, f"Row {i} ({h.name[:30]}): could not read the date '{h.date}'")
            if d > today:
                raise HTTPException(400, f"Row {i} ({h.name[:30]}): the date is in the future")
    rows = [review_holding(h, today) for h in holdings]

    inv = sum(r["invested"] for r in rows)
    cur = sum(r["current"] for r in rows)
    dated = [(parse_date(h.date), r) for h, r in zip(holdings, rows) if h.date]
    flows = [(d, -r["invested"]) for d, r in dated if d] + [(today, sum(r["current"] for _, r in dated))]
    longest = max((r["days"] for _, r in dated), default=0)
    x = xirr(flows) if longest >= 365 else None
    totals = {"invested": round(inv, 2), "current": round(cur, 2), "gain": round(cur - inv, 2),
              "abs_pct": round((cur / inv - 1) * 100, 2), "xirr_pct": x, "funds": len(rows),
              "matched": sum(1 for r in rows if r["matched"]), "dated": len(dated),
              "ahead": sum(1 for r in rows if r["verdict"] == "Ahead"),
              "in_line": sum(1 for r in rows if r["verdict"] == "In line"),
              "behind": sum(1 for r in rows if r["verdict"] == "Behind")}

    def split(keyfn):
        agg = {}
        for r in rows:
            k = keyfn(r) or "Unclassified"
            agg[k] = agg.get(k, 0.0) + r["current"]
        tot = sum(agg.values()) or 1.0
        return [{"name": k, "value": round(v, 2), "pct": round(100 * v / tot, 1)}
                for k, v in sorted(agg.items(), key=lambda kv: -kv[1])]

    alloc = {"by_asset": split(lambda r: r["asset_class"]), "by_category": split(lambda r: r["category"])}

    obs = []
    if totals["behind"]:
        names = ", ".join(r["name"].split(" - ")[0][:34] for r in rows if r["verdict"] == "Behind")
        obs.append(f"{totals['behind']} of {len(rows)} funds are behind their benchmark or category: {names}.")
    cats = {}
    for r in rows:
        if r["category"]:
            cats[r["category"]] = cats.get(r["category"], 0) + 1
    for c, n in sorted(cats.items(), key=lambda kv: -kv[1]):
        if n >= 3:
            obs.append(f"You hold {n} funds in {c}. Funds in the same category tend to own many of the same stocks, "
                       "so they add little diversification.")
            break
    if cur > 0:
        top = max(rows, key=lambda r: r["current"])
        if top["current"] / cur > 0.30 and len(rows) > 1:
            obs.append(f"{top['name'].split(' - ')[0][:40]} is {100 * top['current'] / cur:.0f}% of the portfolio.")
    exp = [(r["expense_ratio"], r["current"]) for r in rows if r["expense_ratio"] is not None and r["current"] > 0]
    if exp:
        w = sum(e * c for e, c in exp) / sum(c for _, c in exp)
        obs.append(f"Weighted average expense ratio is {w:.2f}% a year (regular plans). "
                   "Direct plans usually cost 0.5 to 1 percentage point less.")
    if len(rows) > 12:
        obs.append(f"{len(rows)} funds is a lot to track; many portfolios work with 5 to 8.")
    small = [r for r in rows if any(fl.startswith("Small fund") for fl in r["flags"])]
    if small:
        obs.append(f"{len(small)} fund(s) have under Rs. {SMALL_AUM_CR:.0f} crore in assets.")
    unmatched = [r["name"] for r in rows if not r["matched"]]
    if unmatched:
        obs.append(f"{len(unmatched)} fund(s) could not be matched to our database: {', '.join(n[:30] for n in unmatched[:3])}.")

    return {"as_of": today.strftime("%d-%b-%Y"), "totals": totals, "holdings": rows, "allocation": alloc,
            "observations": obs[:7],
            "notes": ["Illustration only, not investment advice. Past returns do not guarantee future returns.",
                      "Your return is annualised from the date you entered to today, treating each holding as one "
                      "investment on that date. For SIPs, enter each instalment as its own row for an exact figure.",
                      "Category and benchmark figures are trailing returns (1, 3 or 5 years) for the horizon closest "
                      "to your holding period, so the comparison is approximate. Benchmark = median of the index "
                      "funds and ETFs that track it.",
                      "A fund is Ahead or Behind when it is more than 1.5 percentage points a year (0.75 for debt) "
                      "from its benchmark, or its category when no benchmark data exists."]}


# --------------------------------------------------------------------------- upload parsing
def classify_header(h: str) -> Optional[str]:
    s = re.sub(r"[^a-z0-9 ]", " ", str(h or "").lower())
    s = " ".join(s.split())
    if not s:
        return None
    if "date" in s or s in ("since", "invested on"):
        return "date"
    if any(w in s for w in ("invest", "cost", "purchase", "principal")) and "unit" not in s and "nav" not in s:
        return "invested"
    if any(w in s for w in ("current", "market", "present", "valuation", "latest")) or s == "value":
        if "nav" not in s and "unit" not in s:
            return "current"
    if any(w in s for w in ("scheme", "fund", "name", "instrument", "security")):
        return "name"
    return None


def _grid_from_upload(filename: str, raw: bytes) -> list:
    fn = (filename or "").lower()
    if fn.endswith(".xlsx") or fn.endswith(".xlsm"):
        try:
            import openpyxl
        except ImportError:
            raise HTTPException(503, "Excel upload is not available on the server yet (openpyxl missing)")
        try:
            wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        except Exception:
            raise HTTPException(400, "Could not open that Excel file. Save it as .xlsx (or .csv) and try again")
        best = []
        for ws in wb.worksheets:
            rows = []
            for i, r in enumerate(ws.iter_rows(values_only=True)):
                if i >= MAX_SHEET_ROWS:
                    break
                rows.append(list(r))
            if len(rows) > len(best) and _find_header(rows) is not None:
                best = rows
        return best or [list(r) for r in (wb.worksheets[0].iter_rows(values_only=True))][:MAX_SHEET_ROWS]
    if fn.endswith(".xls"):
        raise HTTPException(400, "Old .xls files are not supported. Open it in Excel, save as .xlsx or .csv, and upload that")
    if fn.endswith(".csv") or fn.endswith(".txt"):
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
        try:
            dialect = csv.Sniffer().sniff(text[:4000], delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        return [r for r in csv.reader(io.StringIO(text), dialect)][:MAX_SHEET_ROWS]
    raise HTTPException(400, "Upload an .xlsx or .csv file")


def _find_header(grid: list):
    for i, row in enumerate(grid[:25]):
        cols = {}
        for j, cell in enumerate(row):
            t = classify_header(cell)
            if t and t not in cols:
                cols[t] = j
        if "name" in cols and ("invested" in cols or "current" in cols):
            return i, cols
    return None


def parse_upload(filename: str, raw: bytes) -> dict:
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, "That file is too large (limit 3 MB)")
    grid = _grid_from_upload(filename, raw)
    found = _find_header(grid)
    if found is None:
        raise HTTPException(400, "Could not find the columns. The first row needs headings such as: "
                                 "Fund name, Invested value, Current value, Date")
    hi, cols = found
    rows, skipped, warnings = [], 0, []
    for line_no, row in enumerate(grid[hi + 1:], hi + 2):
        def cell(k):
            j = cols.get(k)
            return row[j] if j is not None and j < len(row) else None
        name = str(cell("name") or "").strip()
        if not name or re.match(r"^(grand\s+)?total\b", name, re.I):
            continue
        inv, cur = parse_money(cell("invested")), parse_money(cell("current"))
        if inv is None or inv <= 0 or cur is None or cur < 0:
            skipped += 1
            warnings.append(f"Row {line_no} ({name[:30]}): needs an invested value above 0 and a current value")
            continue
        d = parse_date(cell("date"))
        if cell("date") not in (None, "") and d is None:
            warnings.append(f"Row {line_no} ({name[:30]}): could not read the date, so it was left blank")
        rows.append({"name": name, "invested": round(inv, 2), "current": round(cur, 2),
                     "date": d.isoformat() if d else None})
    if not rows:
        raise HTTPException(400, "No usable rows found. Each row needs a fund name, an invested value and a current value")
    if "date" not in cols:
        warnings.insert(0, "No date column found, so returns will show as absolute only. Add a Date column to compare "
                           "annualised returns.")
    return {"rows": rows[:MAX_HOLDINGS], "skipped": skipped, "warnings": warnings[:10],
            "truncated": len(rows) > MAX_HOLDINGS, "columns_found": sorted(cols)}


# --------------------------------------------------------------------------- PDF
def build_pdf(rev: dict, now: Optional[datetime] = None) -> bytes:
    from fpdf import FPDF
    _s, _inr = al._s, al._inr
    now = now or datetime.now(IST)
    INK, MUTED, VAULT, GOLD = (24, 36, 34), (95, 111, 108), (10, 46, 36), (201, 162, 39)
    VC = {"Ahead": (30, 140, 80), "Behind": (200, 70, 60), "In line": (176, 133, 10)}

    class Doc(FPDF):
        def footer(self):
            self.set_y(-12)
            self.set_font("Helvetica", "", 7.5)
            self.set_text_color(*MUTED)
            self.cell(0, 5, _s("Illustration only, not investment advice. Past returns do not guarantee future returns."))
            self.set_x(-30)
            self.cell(18, 5, f"Page {self.page_no()}", align="R")

    pdf = Doc(orientation="P", unit="mm", format="A4")
    pdf.set_margins(12, 12, 12)
    pdf.set_auto_page_break(auto=True, margin=16)
    pdf.add_page()
    W = 186.0

    def pc(v, sign=False):
        return "-" if v is None else (f"{v:+.1f}%" if sign else f"{v:.1f}%")

    def section(title):
        pdf.ln(4)
        pdf.set_fill_color(*GOLD)
        pdf.rect(12, pdf.get_y() + 1.2, 2.2, 5, "F")
        pdf.set_x(16)
        pdf.set_font("Helvetica", "B", 11.5)
        pdf.set_text_color(*INK)
        pdf.cell(0, 7.5, _s(title), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(1)

    def clip(text, width, size, style=""):
        pdf.set_font("Helvetica", style, size)
        t = _s(text)
        while t and pdf.get_string_width(t) > width - 1.5:
            t = t[:-1]
        return t

    pdf.set_fill_color(*VAULT)
    pdf.rect(0, 0, 210, 30, "F")
    pdf.set_xy(12, 8)
    pdf.set_font("Helvetica", "B", 18)
    pdf.set_text_color(*GOLD)
    pdf.cell(0, 8, "Advantage - Portfolio Review", new_x="LMARGIN", new_y="NEXT")
    pdf.set_x(12)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(220, 239, 230)
    pdf.cell(0, 6, _s(f"As on {rev['as_of']}  -  generated {now.strftime('%d %b %Y, %I:%M %p')} IST"),
             new_x="LMARGIN", new_y="NEXT")
    pdf.set_y(36)

    t = rev["totals"]
    boxes = [("Invested", _inr(t["invested"])), ("Current value", _inr(t["current"])),
             ("Gain", f"{_inr(t['gain'])} ({pc(t['abs_pct'], True)})"),
             ("XIRR a year", pc(t["xirr_pct"]))]
    bw = W / 4
    y0 = pdf.get_y()
    for i, (k, v) in enumerate(boxes):
        pdf.set_xy(12 + i * bw, y0)
        pdf.set_fill_color(240, 245, 242)
        pdf.rect(12 + i * bw + 0.8, y0, bw - 1.6, 15, "F")
        pdf.set_xy(12 + i * bw + 2.5, y0 + 2)
        pdf.set_font("Helvetica", "", 7.5)
        pdf.set_text_color(*MUTED)
        pdf.cell(bw - 4, 4, _s(k))
        pdf.set_xy(12 + i * bw + 2.5, y0 + 7)
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_text_color(*INK)
        pdf.cell(bw - 4, 5, clip(v, bw - 4, 9, "B"))
    pdf.set_y(y0 + 18)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(*INK)
    pdf.cell(0, 5.5, _s(f"{t['funds']} funds: {t['ahead']} ahead, {t['in_line']} in line, {t['behind']} behind "
                        "their benchmark or category"), new_x="LMARGIN", new_y="NEXT")

    section("Allocation")
    for a in rev["allocation"]["by_asset"] + rev["allocation"]["by_category"]:
        pdf.set_font("Helvetica", "", 8.5)
        pdf.set_text_color(*INK)
        pdf.cell(52, 5, clip(a["name"], 52, 8.5))
        x, y = pdf.get_x(), pdf.get_y()
        pdf.set_fill_color(225, 232, 229)
        pdf.rect(x, y + 1, 100, 3, "F")
        pdf.set_fill_color(*GOLD)
        pdf.rect(x, y + 1, max(0.5, a["pct"]), 3, "F")
        pdf.set_x(x + 102)
        pdf.cell(0, 5, f"{a['pct']:.1f}%", new_x="LMARGIN", new_y="NEXT")

    section("Fund by fund")
    cols = [("Fund", 62), ("Invested", 22), ("Current", 22), ("Total", 15), ("A year", 15), ("Peer", 15), ("Verdict", 35)]
    pdf.set_fill_color(*VAULT)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 8)
    for n, w in cols:
        pdf.cell(w, 6, _s(n), fill=True)
    pdf.ln(6)
    for h in rev["holdings"]:
        peer = h["benchmark_pct"] if h["benchmark_pct"] is not None else h["category_pct"]
        vals = [clip(h["name"].split(" - ")[0], 62, 7.5), _inr(h["invested"]), _inr(h["current"]),
                pc(h["abs_pct"], True), pc(h["cagr_pct"]), pc(peer), h["verdict"]]
        if pdf.get_y() > 265:
            pdf.add_page()
        for i, ((n, w), v) in enumerate(zip(cols, vals)):
            pdf.set_font("Helvetica", "B" if i == 6 else "", 7.5)
            pdf.set_text_color(*(VC.get(h["verdict"], INK) if i == 6 else INK))
            pdf.cell(w, 5.6, clip(v, w, 7.5, "B" if i == 6 else ""), border="B")
        pdf.ln(5.6)
        extra = [h["verdict_why"]] + h["flags"]
        pdf.set_font("Helvetica", "", 7)
        pdf.set_text_color(*MUTED)
        for e in [x for x in extra if x]:
            pdf.cell(W, 3.8, clip("   " + e, W, 7), new_x="LMARGIN", new_y="NEXT")
        if h["benchmark"]:
            pdf.cell(W, 3.8, clip(f"   Benchmark: {h['benchmark']} ({h['horizon'] or 'n/a'} view)", W, 7),
                     new_x="LMARGIN", new_y="NEXT")

    if rev["observations"]:
        section("Things to notice")
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(*INK)
        for o in rev["observations"]:
            pdf.set_x(12)
            pdf.multi_cell(W, 5, _s("- " + o), new_x="LMARGIN", new_y="NEXT")
    section("Notes")
    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(*MUTED)
    for n in rev["notes"]:
        pdf.set_x(12)
        pdf.multi_cell(W, 4.2, _s("- " + n), new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


class UploadRequest(BaseModel):
    filename: str
    data_base64: str


def register_portfolio_review(app):
    @app.get("/portfolio/funds")
    def portfolio_funds(q: str = "", limit: int = 12):
        return {"funds": search_funds(q, max(1, min(limit, 25)))}

    @app.post("/portfolio/parse")
    def portfolio_parse(req: UploadRequest):
        try:
            raw = base64.b64decode(req.data_base64, validate=False)
        except Exception:
            raise HTTPException(400, "The file could not be read")
        return parse_upload(req.filename, raw)

    @app.post("/portfolio/review")
    def portfolio_review(req: ReviewRequest):
        return build_review(req.holdings)

    @app.post("/portfolio/pdf")
    def portfolio_pdf(req: ReviewRequest):
        from fastapi import Response
        data = build_pdf(build_review(req.holdings))
        return Response(content=data, media_type="application/pdf",
                        headers={"Content-Disposition": 'attachment; filename="advantage_portfolio_review.pdf"'})
