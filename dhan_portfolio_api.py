"""
dhan_portfolio_api.py - your Dhan stock holdings, valued.

    GET /trading/{owner_email}/portfolio

Takes the holdings Dhan reports (quantity and average cost) and adds the latest price from the same source the
watchlist uses (Dhan while the market is open, Yahoo otherwise), then works out invested value, current value,
profit or loss, today's change and each holding's share of the portfolio. Holdings whose price cannot be found
are listed as unpriced and left out of the totals' percentages. The route has an '@' in its path, so the sign-in
guard covers it, and TRADING_OWNER_EMAIL (if set) must match, the same rule as the other /trading routes.
"""
import os
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional

from fastapi import HTTPException

import watchlist_api as wl

IST = timezone(timedelta(hours=5, minutes=30))
CHUNK = wl.MAX_SYMBOLS


def _f(v, default=0.0) -> float:
    try:
        x = float(v)
        return x if x == x and abs(x) != float("inf") else default
    except (TypeError, ValueError):
        return default


def default_quotes(symbols: List[str]) -> dict:
    """symbol -> {price, change, change_pct, realtime}; symbols without a price are left out."""
    out = {}
    valid = [s for s in symbols if wl.SYMBOL_RE.match(s)]
    for i in range(0, len(valid), CHUNK):
        try:
            data = wl.get_quotes(",".join(valid[i:i + CHUNK]))
        except HTTPException:
            continue
        for q in data["quotes"]:
            out[q["symbol"]] = q
    return out


def build_portfolio(holdings: list, quotes_fn: Optional[Callable] = None, now: Optional[datetime] = None) -> dict:
    now = now or datetime.now(IST)
    rows = []
    for h in holdings or []:
        sym = str(h.get("tradingSymbol") or h.get("trading_symbol") or "").strip().upper()
        qty = _f(h.get("totalQty", h.get("quantity", 0)))
        avg = _f(h.get("avgCostPrice", h.get("average_price", 0)))
        if not sym or qty <= 0:
            continue
        rows.append({"symbol": sym, "qty": qty, "avg": avg, "isin": h.get("isin")})
    symbols = sorted({r["symbol"] for r in rows})
    quotes = (quotes_fn or default_quotes)(symbols) if symbols else {}

    out, inv_all, val_priced, inv_priced, day_total = [], 0.0, 0.0, 0.0, 0.0
    for r in rows:
        q = quotes.get(r["symbol"])
        invested = r["qty"] * r["avg"]
        inv_all += invested
        item = {"symbol": r["symbol"], "qty": r["qty"], "avg_price": round(r["avg"], 2), "invested": round(invested, 2),
                "price": None, "value": None, "pnl": None, "pnl_pct": None, "day_change": None,
                "day_change_pct": None, "weight_pct": None, "realtime": False}
        if q and q.get("price"):
            price = _f(q["price"])
            value = r["qty"] * price
            item.update({"price": round(price, 2), "value": round(value, 2), "pnl": round(value - invested, 2),
                         "pnl_pct": round((value / invested - 1) * 100, 2) if invested > 0 else None,
                         "realtime": bool(q.get("realtime"))})
            if q.get("change") is not None:
                day = r["qty"] * _f(q["change"])
                item["day_change"] = round(day, 2)
                item["day_change_pct"] = q.get("change_pct")
                day_total += day
            val_priced += value
            inv_priced += invested
        out.append(item)

    for it in out:
        if it["value"] is not None and val_priced > 0:
            it["weight_pct"] = round(100 * it["value"] / val_priced, 1)
    out.sort(key=lambda it: (it["value"] is None, -(it["value"] or 0), it["symbol"]))
    priced = [it for it in out if it["value"] is not None]
    pnl = val_priced - inv_priced
    top = priced[0] if priced else None
    notes = []
    unpriced = [it["symbol"] for it in out if it["value"] is None]
    if unpriced:
        notes.append(f"No price found for {', '.join(unpriced[:6])}; left out of the value and profit totals.")
    if top and top["weight_pct"] and top["weight_pct"] > 25 and len(priced) > 1:
        notes.append(f"{top['symbol']} is {top['weight_pct']:.0f}% of the priced portfolio.")
    if priced and not any(it["realtime"] for it in priced):
        notes.append("Prices are the last close (or delayed), not live ticks.")
    return {
        "as_of": now.astimezone(IST).strftime("%d-%b-%Y %H:%M"), "market_open": wl.market_open(now),
        "totals": {"invested": round(inv_all, 2), "priced_invested": round(inv_priced, 2),
                   "value": round(val_priced, 2), "pnl": round(pnl, 2),
                   "pnl_pct": round(pnl / inv_priced * 100, 2) if inv_priced > 0 else None,
                   "day_change": round(day_total, 2),
                   "day_change_pct": round(day_total / (val_priced - day_total) * 100, 2)
                   if val_priced - day_total > 0 else None,
                   "holdings": len(out), "priced": len(priced)},
        "holdings": out, "notes": notes,
    }


def register_dhan_portfolio(app, holdings_fn: Optional[Callable] = None, quotes_fn: Optional[Callable] = None):
    def _owner_check(owner_email: str):
        allowed = os.environ.get("TRADING_OWNER_EMAIL", "").strip().lower()
        if allowed and owner_email.strip().lower() != allowed:
            raise HTTPException(403, "Not authorized to use trading on this account")

    def _holdings():
        if holdings_fn:
            return holdings_fn()
        from dhanhq_api import get_dhan_client
        client = get_dhan_client()
        if client is None:
            raise HTTPException(500, "Dhan credentials are not configured on the server")
        return client.get_holdings()

    @app.get("/trading/{owner_email}/portfolio")
    def dhan_portfolio(owner_email: str):
        _owner_check(owner_email)
        try:
            holdings = _holdings()
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, f"Dhan error: {e}")
        return build_portfolio(holdings, quotes_fn)
