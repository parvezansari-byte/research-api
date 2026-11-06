"""
market_report_brief.py - the "brief" part of the market report: overall tone, top headlines, AI commentary
and the e-mail with the PDF attached (Brevo HTTP API; same BREVO_API_KEY / EMAIL_SENDER as the old macro report).

Everything external is injectable so the tests run offline:
    news_fn()            -> list of {title, source, sentiment, impact, age, link}
    ai_fn(rep, news)     -> str or None  (None = not available; the PDF then leaves the section out)
    post(url, **kw)      -> requests.post style
"""
import base64
import logging
import os
import re
import time
from typing import Callable, List, Optional

log = logging.getLogger("market_report_brief")

AI_FAIL_MARKERS = ("AI narrative unavailable", "AI commentary isn't available", "AI commentary is temporarily unavailable")


# ---------------------------------------------------------------- tone
def tone(rep: dict) -> dict:
    """Plain risk-on / risk-off label from the numbers only (same thresholds as the old macro report)."""
    idx = [rep["indices"][n]["pct"] for n in ("NIFTY 50", "SENSEX", "BANK NIFTY") if n in rep.get("indices", {})]
    vix = (rep.get("indices", {}).get("INDIA VIX") or {}).get("pct", 0) or 0
    avg = sum(idx) / len(idx) if idx else 0.0
    if avg <= -1.5 and vix >= 5:
        label, colour = "RISK-OFF", "red"
    elif avg >= 1.5 and vix <= -5:
        label, colour = "RISK-ON", "green"
    else:
        label, colour = "MIXED / NEUTRAL", "amber"
    note = f"Average move of Nifty 50, Sensex and Bank Nifty: {avg:+.2f}%; India VIX {vix:+.1f}%."
    return {"label": label, "colour": colour, "note": note}


# ---------------------------------------------------------------- news and AI (defaults reuse api.py)
def default_news(limit: int = 5) -> list:
    try:
        import api
        return api._mr_fetch_news_highlights(limit)
    except Exception:
        log.exception("news failed")
        return []


def default_ai(rep: dict, news: list) -> Optional[str]:
    try:
        import api
        snap = []
        for name, v in rep.get("indices", {}).items():
            snap.append({"category": "Indices", "name": name, "value": f"{v['last']:,.2f}", "change_pct": v["pct"]})
        for name, v in rep.get("global", {}).items():
            if v.get("pct") is not None:
                snap.append({"category": "Global", "name": name, "value": f"{v['last']:,.2f}", "change_pct": v["pct"]})
        fl = flow_totals(rep)
        text = api._mr_ai_narrative(snap, fl, news, rep["kind"])
    except Exception:
        log.exception("ai failed")
        return None
    if not text or any(text.startswith(m) for m in AI_FAIL_MARKERS):
        return None
    return text


def flow_totals(rep: dict) -> Optional[dict]:
    """FII/DII net in Rs crore for the report period: the latest session for daily, the recorded sessions otherwise."""
    if rep["kind"] == "daily":
        rows = [r for r in (rep.get("fii") or {}).get("rows", []) if r.get("net") is not None]
        f = next((r["net"] for r in rows if r["who"] == "FII"), None)
        d = next((r["net"] for r in rows if r["who"] == "DII"), None)
        if f is None or d is None:
            return None
        return {"fii_net_total": f, "dii_net_total": d, "total_days": 1}
    fl = rep.get("flows") or {}
    blk = fl.get("last5") if rep["kind"] == "weekly" else fl.get("mtd")
    if not blk or not blk.get("n"):
        return None
    return {"fii_net_total": blk["fii"], "dii_net_total": blk["dii"], "total_days": blk["n"]}


def build_brief(rep: dict, news_fn: Callable = default_news, ai_fn: Callable = default_ai, with_ai: bool = True) -> dict:
    news = []
    try:
        news = list(news_fn(5) or [])[:5]
    except Exception:
        log.exception("news_fn")
    ai = None
    if with_ai:
        try:
            ai = ai_fn(rep, news)
        except Exception:
            log.exception("ai_fn")
    fl = flow_totals(rep)
    line = (f"FII net Rs {fl['fii_net_total']:+,.0f} cr | DII net Rs {fl['dii_net_total']:+,.0f} cr"
            f" ({fl['total_days']} session{'s' if fl['total_days'] != 1 else ''})") if fl else None
    return {"tone": tone(rep), "news": news, "ai": ai, "flow_line": line}


# ---------------------------------------------------------------- e-mail
def _safe(s) -> str:
    return re.sub(r"[\r\n]+", " ", str(s or "")).strip()


def email_parts(rep: dict, brief: dict) -> tuple:
    kind = rep["kind"].capitalize()
    subject = _safe(f"Advantage - {kind} Market Report ({brief['tone']['label']}) - {rep['as_of']}")
    news = brief["news"][:3]
    nl = "\n".join(f"  - {_safe(n.get('title'))} ({_safe(n.get('source'))}, {_safe(n.get('age'))})" for n in news) \
        or "  (no major headlines right now)"
    body = (f"Your {rep['kind']} market report is attached.\n\n"
            f"Overall tone: {brief['tone']['label']}\n"
            f"{brief['flow_line'] or 'FII/DII data unavailable'}\n\n"
            f"{rep['text']['summary']}\n\n"
            f"Top headlines:\n{nl}\n\n"
            "Charts, key levels, sector and global tables, scenarios and commentary are in the attached PDF.\n"
            "This is an automated email from your Advantage app - not investment advice.")
    return subject, body


def send_email(rep: dict, brief: dict, pdf: bytes, to_addr: str, post: Optional[Callable] = None) -> None:
    api_key = os.environ.get("BREVO_API_KEY")
    sender = os.environ.get("EMAIL_SENDER") or os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USER")
    if not api_key or not sender:
        raise RuntimeError("Email is not configured (BREVO_API_KEY / EMAIL_SENDER missing)")
    if post is None:
        import requests
        post = requests.post
    subject, body = email_parts(rep, brief)
    payload = {"sender": {"email": sender, "name": "Advantage"}, "to": [{"email": to_addr}], "subject": subject,
               "textContent": body,
               "attachment": [{"content": base64.b64encode(pdf).decode("ascii"),
                               "name": f"advantage_{rep['kind']}_market_report_{rep['as_of']}.pdf"}]}
    r = post("https://api.brevo.com/v3/smtp/email",
             headers={"api-key": api_key, "Content-Type": "application/json", "accept": "application/json"},
             json=payload, timeout=25)
    if r.status_code >= 300:
        raise RuntimeError(f"Email send failed ({r.status_code}): {str(r.text)[:200]}")


def recipients(sb, kind: str) -> List[str]:
    out: List[str] = []
    try:
        res = sb.table("alert_settings").select("user_email").eq(kind, True).execute()
        out = [r["user_email"].strip() for r in (res.data or []) if r.get("user_email")]
    except Exception:
        log.exception("recipients")
    extra = os.environ.get("EMAIL_RECIPIENT", "").strip()
    for e in [x.strip() for x in extra.split(",") if x.strip()]:
        out.append(e)
    seen, uniq = set(), []
    for e in out:
        if e.lower() not in seen:
            seen.add(e.lower()); uniq.append(e)
    return uniq
