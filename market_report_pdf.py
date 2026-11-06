"""
market_report_pdf.py - draws the market report (see market_report_api.py) as an A4 PDF in the
style of the Global Cues update: navy header band, KPI tiles, charts, shaded tables.
ReportLab for layout, matplotlib (Agg) for charts. Fonts come from matplotlib's own DejaVu files
so the rupee sign and arrows render on any server.
"""
import io
import os
from xml.sax.saxutils import escape

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402
from reportlab.lib import colors  # noqa: E402
from reportlab.lib.enums import TA_LEFT  # noqa: E402
from reportlab.lib.pagesizes import A4  # noqa: E402
from reportlab.lib.styles import ParagraphStyle  # noqa: E402
from reportlab.lib.units import mm  # noqa: E402
from reportlab.pdfbase import pdfmetrics  # noqa: E402
from reportlab.pdfbase.ttfonts import TTFont  # noqa: E402
from reportlab.platypus import (BaseDocTemplate, Frame, Image, KeepTogether, PageTemplate,  # noqa: E402
                                Paragraph, Spacer, Table, TableStyle)

NAVY = colors.HexColor("#0B1F3F")
GOLD = colors.HexColor("#E8A93C")
TEAL = colors.HexColor("#12A594")
RED = colors.HexColor("#DC4A41")
GREY = colors.HexColor("#6B7280")
INK = colors.HexColor("#1F2937")
LIGHT = colors.HexColor("#F3F5F9")
GREEN_BG = colors.HexColor("#E8F6F3")
AMBER_BG = colors.HexColor("#FDF3DC")
RED_BG = colors.HexColor("#FBE9E7")
H_TEAL, H_RED, H_NAVY, H_GOLD, H_GREY = "#12A594", "#DC4A41", "#0B1F3F", "#E8A93C", "#6B7280"

_fonts_ready = False


def _register_fonts():
    global _fonts_ready
    if _fonts_ready:
        return
    d = os.path.join(matplotlib.get_data_path(), "fonts", "ttf")
    pdfmetrics.registerFont(TTFont("DV", os.path.join(d, "DejaVuSans.ttf")))
    pdfmetrics.registerFont(TTFont("DV-B", os.path.join(d, "DejaVuSans-Bold.ttf")))
    pdfmetrics.registerFont(TTFont("DV-I", os.path.join(d, "DejaVuSans-Oblique.ttf")))
    pdfmetrics.registerFontFamily("DV", normal="DV", bold="DV-B", italic="DV-I", boldItalic="DV-B")
    _fonts_ready = True


def _styles():
    base = dict(fontName="DV", textColor=INK, alignment=TA_LEFT)
    return {
        "body": ParagraphStyle("body", fontSize=9, leading=13.2, spaceAfter=6, **base),
        "small": ParagraphStyle("small", fontSize=7.5, leading=10, textColor=GREY, fontName="DV", spaceAfter=4),
        "h": ParagraphStyle("h", fontName="DV-B", fontSize=12.5, leading=16, textColor=NAVY, spaceBefore=10, spaceAfter=5),
        "cell": ParagraphStyle("cell", fontSize=8.2, leading=11, **base),
        "cellb": ParagraphStyle("cellb", fontSize=8.2, leading=11, fontName="DV-B", textColor=INK),
        "th": ParagraphStyle("th", fontSize=8.2, leading=11, fontName="DV-B", textColor=colors.white),
        "box": ParagraphStyle("box", fontSize=9, leading=13.5, **base),
        "boxw": ParagraphStyle("boxw", fontSize=9.5, leading=14.5, fontName="DV", textColor=colors.white),
    }


def P(text, style):
    return Paragraph(text, style)


def _c(p):
    return H_TEAL if p >= 0 else H_RED


def _arrow(p):
    return "▲" if p >= 0 else "▼"


def _png(fig, width_mm, height_mm=None):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=190, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    buf.seek(0)
    from PIL import Image as PILImage
    w, h = PILImage.open(io.BytesIO(buf.getvalue())).size
    buf.seek(0)
    W = width_mm * mm
    return Image(buf, width=W, height=W * h / w)


def _style_ax(ax):
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color("#C9CED6")
    ax.tick_params(colors="#4B5563", labelsize=7.5, length=3)
    ax.grid(axis="y", color="#E5E7EB", linewidth=0.7)
    ax.set_axisbelow(True)


def chart_trend(rep):
    ch = rep["chart"]
    n = len(ch["close"])
    xs = list(range(n))
    fig, ax = plt.subplots(figsize=(7.4, 3.0))
    ax.plot(xs, ch["close"], color=H_TEAL, linewidth=1.9, label="Nifty 50 close")
    ax.fill_between(xs, ch["close"], min(ch["close"]) * 0.995, color=H_TEAL, alpha=0.08)
    for key, col, lab in (("dma20", H_GOLD, "20-day avg"), ("dma50", "#6C8EF5", "50-day avg"), ("dma200", H_GREY, "200-day avg")):
        pts = [(i, v) for i, v in enumerate(ch[key]) if v is not None]
        if len(pts) > 2:
            ax.plot([i for i, _ in pts], [v for _, v in pts], color=col, linewidth=1.1, linestyle="--", label=lab)
    ax.scatter([n - 1], [ch["close"][-1]], color=H_TEAL, zorder=5, s=22)
    ax.annotate(f"{ch['close'][-1]:,.0f}", (n - 1, ch["close"][-1]), textcoords="offset points", xytext=(-4, 7),
                ha="right", fontsize=8, fontweight="bold", color=H_NAVY)
    ticks = sorted(set([0, n // 4, n // 2, 3 * n // 4, n - 1]))
    ax.set_xticks(ticks)
    ax.set_xticklabels([_short(ch["dates"][i]) for i in ticks])
    ax.set_title("Nifty 50 closing levels with moving averages", fontsize=9.5, fontweight="bold", color=H_NAVY, loc="left")
    ax.legend(fontsize=7, frameon=False, ncol=4, loc="upper left", bbox_to_anchor=(0, -0.12))
    _style_ax(ax)
    return _png(fig, 178)


def _short(d):
    import datetime
    try:
        return datetime.date.fromisoformat(d).strftime("%d %b %y")
    except ValueError:
        return d


def chart_bars(title, items, unit="%", width=178, height=None):
    items = [(n, v) for n, v in items if v is not None]
    h = height or max(1.6, 0.34 * len(items) + 0.9)
    fig, ax = plt.subplots(figsize=(7.4, h))
    names = [n for n, _ in items][::-1]
    vals = [v for _, v in items][::-1]
    ax.barh(names, vals, color=[H_TEAL if v >= 0 else H_RED for v in vals], height=0.62)
    span = (max(vals + [0]) - min(vals + [0])) or 1
    for i, v in enumerate(vals):
        ax.text(v + (0.015 * span if v >= 0 else -0.015 * span), i, f"{v:+.2f}{unit}", va="center",
                ha="left" if v >= 0 else "right", fontsize=7.5, fontweight="bold", color=H_NAVY)
    ax.set_xlim(min(vals + [0]) - span * 0.18, max(vals + [0]) + span * 0.18)
    ax.axvline(0, color="#9CA3AF", linewidth=0.8)
    ax.set_title(title, fontsize=9.5, fontweight="bold", color=H_NAVY, loc="left")
    _style_ax(ax)
    ax.grid(axis="y", visible=False)
    ax.grid(axis="x", color="#E5E7EB", linewidth=0.7)
    return _png(fig, width)


def chart_compare(rep):
    cmp_ = rep["compare"]
    if len(cmp_) < 2:
        return None
    fig, ax = plt.subplots(figsize=(7.4, 2.7))
    cols = {"NIFTY 50": H_TEAL, "SENSEX": "#6C8EF5", "BANK NIFTY": H_GOLD}
    for name, d in cmp_.items():
        ax.plot(range(len(d["rebased"])), d["rebased"], color=cols.get(name, H_GREY), linewidth=1.6, label=name.title())
    n = len(next(iter(cmp_.values()))["rebased"])
    dates = next(iter(cmp_.values()))["dates"]
    ticks = sorted(set([0, n // 3, 2 * n // 3, n - 1]))
    ax.set_xticks(ticks)
    ax.set_xticklabels([_short(dates[i]) for i in ticks])
    ax.axhline(100, color="#9CA3AF", linewidth=0.8)
    ax.set_title("Indices rebased to 100 at the start of the chart", fontsize=9.5, fontweight="bold", color=H_NAVY, loc="left")
    ax.legend(fontsize=7.5, frameon=False, ncol=3, loc="upper left")
    _style_ax(ax)
    return _png(fig, 178)


def chart_flows(fii):
    rows = {r["who"]: r for r in fii["rows"] if r.get("net") is not None}
    if not rows:
        return None
    fig, ax = plt.subplots(figsize=(7.4, 1.5))
    names = list(rows)[::-1]
    vals = [rows[n]["net"] for n in names]
    ax.barh(names, vals, color=[H_TEAL if v >= 0 else H_RED for v in vals], height=0.55)
    span = (max(vals + [0]) - min(vals + [0])) or 1
    for i, v in enumerate(vals):
        ax.text(v + (0.015 * span if v >= 0 else -0.015 * span), i, f"{v:+,.0f} cr", va="center",
                ha="left" if v >= 0 else "right", fontsize=8, fontweight="bold", color=H_NAVY)
    ax.set_xlim(min(vals + [0]) - span * 0.25, max(vals + [0]) + span * 0.25)
    ax.axvline(0, color="#9CA3AF", linewidth=0.8)
    ax.set_title("Net institutional flows, latest session (₹ crore)", fontsize=9.5, fontweight="bold", color=H_NAVY, loc="left")
    _style_ax(ax)
    ax.grid(axis="y", visible=False)
    return _png(fig, 178)


# ---------------------------------------------------------------- page furniture
def _make_doc(buf, rep):
    title = rep["title"]
    when = _short(rep["as_of"])

    def first(c, doc):
        w, h = A4
        c.saveState()
        c.setFillColor(NAVY)
        c.rect(0, h - 52 * mm, w, 52 * mm, stroke=0, fill=1)
        c.setFillColor(GOLD)
        c.rect(0, h - 52 * mm - 1.2 * mm, w, 1.2 * mm, stroke=0, fill=1)
        c.setFont("DV-B", 8)
        c.drawString(18 * mm, h - 17 * mm, f"MARKET UPDATE  ·  {rep['kind'].upper()}  ·  AS OF {when.upper()}")
        c.setFillColor(colors.white)
        c.setFont("DV-B", 24)
        c.drawString(18 * mm, h - 30 * mm, "Advantage")
        c.setFont("DV-B", 17)
        c.drawString(18 * mm, h - 40 * mm, title)
        c.setFont("DV", 7.5)
        c.setFillColor(colors.HexColor("#B8C2D6"))
        span = {"daily": rep.get("live") and f"{rep['live']} - not a closing report" or "Latest session versus the one before",
                "weekly": f"Last 5 sessions, since the close of {_short(rep['base_date'])}",
                "monthly": f"Last 21 sessions, since the close of {_short(rep['base_date'])}"}[rep["kind"]]
        c.drawString(18 * mm, h - 47 * mm, span)
        _footer(c, doc)
        c.restoreState()

    def later(c, doc):
        w, h = A4
        c.saveState()
        c.setFillColor(NAVY)
        c.rect(0, h - 12 * mm, w, 12 * mm, stroke=0, fill=1)
        c.setFillColor(GOLD)
        c.rect(0, h - 12 * mm - 0.8 * mm, w, 0.8 * mm, stroke=0, fill=1)
        c.setFillColor(colors.white)
        c.setFont("DV-B", 8)
        c.drawString(18 * mm, h - 8 * mm, f"Advantage · {title}")
        c.setFont("DV", 7.5)
        c.drawRightString(w - 18 * mm, h - 8 * mm, when)
        _footer(c, doc)
        c.restoreState()

    def _footer(c, doc):
        w, _ = A4
        c.setFont("DV", 6.8)
        c.setFillColor(GREY)
        c.drawString(18 * mm, 9 * mm, "For information only. Not investment advice. Please consult a SEBI-registered adviser before investing.")
        c.drawRightString(w - 18 * mm, 9 * mm, str(doc.page))

    doc = BaseDocTemplate(buf, pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm, topMargin=16 * mm,
                          bottomMargin=16 * mm, title=f"{title} - {when}", author="Advantage")
    fw, fh = A4[0] - 32 * mm, A4[1] - 32 * mm
    first_frame = Frame(16 * mm, 16 * mm, fw, fh - 40 * mm, id="f1", leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    later_frame = Frame(16 * mm, 16 * mm, fw, fh - 4 * mm, id="f2", leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates([PageTemplate(id="first", frames=[first_frame], onPage=first, autoNextPageTemplate="later"),
                          PageTemplate(id="later", frames=[later_frame], onPage=later)])
    return doc, fw


def _kpis(rep, st, fw):
    ix = rep["indices"]
    g = rep["global"]
    tiles = []

    def tile(label, value, pct, note="", good_up=True):
        col = H_GREY if pct is None else (_c(pct) if good_up else _c(-pct))
        line = "" if pct is None else f"<font size=7.5 color='{col}'>{_arrow(pct)} {abs(pct):.2f}%</font>"
        sub = f"<br/><font size=6.5 color='{H_GREY}'>{note}</font>" if note else ""
        tiles.append(P(f"<font size=6.8 color='{H_GREY}'>{label}</font><br/><font size=10.5 name='DV-B' color='{H_NAVY}'>{value}</font><br/>{line}{sub}", st["cell"]))

    if "NIFTY 50" in ix:
        tile("NIFTY 50", f"{ix['NIFTY 50']['last']:,.2f}", ix["NIFTY 50"]["pct"])
    if "SENSEX" in ix:
        tile("SENSEX", f"{ix['SENSEX']['last']:,.2f}", ix["SENSEX"]["pct"])
    if "BANK NIFTY" in ix:
        tile("BANK NIFTY", f"{ix['BANK NIFTY']['last']:,.2f}", ix["BANK NIFTY"]["pct"])
    if "USD/INR" in g:
        tile("USD/INR", f"{g['USD/INR']['last']:.2f}", g["USD/INR"]["pct"], "up = rupee weaker", good_up=False)
    if "Brent crude" in g:
        tile("BRENT", f"${g['Brent crude']['last']:.1f}", g["Brent crude"]["pct"], good_up=False)
    if "INDIA VIX" in ix:
        tile("INDIA VIX", f"{ix['INDIA VIX']['last']:.2f}", ix["INDIA VIX"]["pct"], good_up=False)
    tiles = tiles[:6]
    if not tiles:
        return Spacer(1, 2)
    cw = fw / len(tiles)
    t = Table([tiles], colWidths=[cw] * len(tiles))
    cmds = [("BACKGROUND", (0, 0), (-1, -1), LIGHT), ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING", (0, 0), (-1, -1), 8)]
    for i in range(len(tiles)):
        cmds.append(("LINEBEFORE", (i, 0), (i, 0), 2.2, [TEAL, colors.HexColor("#6C8EF5"), GOLD, colors.HexColor("#6C8EF5"), RED, GREY][i]))
    t.setStyle(TableStyle(cmds))
    return t


def _table(rows, widths, head=True, zebra=True, extra=None):
    t = Table(rows, colWidths=widths, repeatRows=1 if head else 0)
    cmds = [("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 7), ("RIGHTPADDING", (0, 0), (-1, -1), 7),
            ("LINEBELOW", (0, 0), (-1, -1), 0.4, colors.HexColor("#E5E7EB"))]
    if head:
        cmds += [("BACKGROUND", (0, 0), (-1, 0), NAVY)]
    if zebra:
        for r in range(1 if head else 0, len(rows)):
            if r % 2 == 0:
                cmds.append(("BACKGROUND", (0, r), (-1, r), LIGHT))
    t.setStyle(TableStyle(cmds + (extra or [])))
    return t


def _box(flow, bg, bar):
    t = Table([[flow]], colWidths=[None])
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), bg), ("LINEBEFORE", (0, 0), (0, 0), 3, bar),
                           ("LEFTPADDING", (0, 0), (-1, -1), 12), ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                           ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
    return t


def _flows_table(rep, st, fw):
    fl = rep.get("flows")
    if not fl:
        return None
    last = fl["latest"]
    import datetime as _dt
    mon = _dt.date.fromisoformat(last["d"]).strftime("%b")
    heads = [P("₹ crore (recorded)", st["th"]), P(f"{_short(last['d'])}", st["th"]),
             P(f"Last {fl['last5']['n']} session{'s' if fl['last5']['n'] != 1 else ''}", st["th"]), P(f"{mon} to date ({fl['mtd']['n']})", st["th"])]
    cell = lambda v: P(f"<font color='{_c(v)}'><b>{v:+,.0f}</b></font>", st["cell"])
    rows = [heads,
            [P("FII net", st["cellb"]), cell(last["fii"]), cell(fl["last5"]["fii"]), cell(fl["mtd"]["fii"])],
            [P("DII net", st["cellb"]), cell(last["dii"]), cell(fl["last5"]["dii"]), cell(fl["mtd"]["dii"])]]
    return _table(rows, [fw * 0.28, fw * 0.24, fw * 0.24, fw * 0.24])


def render_pdf(rep: dict, note: str = "", brief: dict = None) -> bytes:
    _register_fonts()
    st = _styles()
    buf = io.BytesIO()
    doc, fw = _make_doc(buf, rep)
    tx = rep["text"]
    f = []
    f.append(Spacer(1, 4))
    f.append(_kpis(rep, st, fw))
    k = rep["sessions"]
    lvl = "intraday levels (the market is open)" if rep.get("live") else "closing values"
    f.append(P(f"Moves shown are for the {'latest session' if k == 1 else f'last {k} sessions'}. Index levels are {lvl}; "
               "the US 10-year yield is shown as a level, with the change in basis points in the text.", st["small"]))
    n = 1

    def head(txt):
        nonlocal n
        f.append(P(f"<font color='{H_GOLD}'>{n}</font>&nbsp;&nbsp;{escape(txt)}", st["h"]))
        n += 1

    head("Market snapshot")
    f.append(P(escape(tx["summary"]), st["body"]))
    if note:
        f.append(_box(P("<b>Adviser's note.</b> " + escape(note).replace("\n", "<br/>"), st["box"]), AMBER_BG, GOLD))
        f.append(Spacer(1, 6))
    f.append(chart_trend(rep))

    if brief:
        tn = brief["tone"]
        bg, fg = {"red": (RED_BG, H_RED), "green": (GREEN_BG, H_TEAL)}.get(tn["colour"], (AMBER_BG, "#B7791F"))
        head("Market tone, flows and headlines")
        lines = f"<font color='{fg}'><b>Overall tone: {escape(tn['label'])}</b></font><br/>{escape(tn['note'])}"
        if brief.get("flow_line"):
            lines += f"<br/><b>Institutional flows:</b> {escape(brief['flow_line'])}"
        f.append(_box(P(lines, st["box"]), bg, colors.HexColor(fg)))
        f.append(P("The tone is a plain label computed from the index and volatility moves above. It is not a prediction.", st["small"]))
        if brief.get("news"):
            f.append(Spacer(1, 4))
            rows = [[P("Headline", st["th"]), P("Source", st["th"]), P("Impact", st["th"])]]
            for nw in brief["news"]:
                t = escape(str(nw.get("title", "")))
                link = str(nw.get("link") or "")
                if link.startswith("http"):
                    t = f"<link href='{escape(link, {chr(39): '&#39;'})}' color='#1F4E8C'>{t}</link>"
                sent = str(nw.get("sentiment") or "")
                col = H_TEAL if sent.lower().startswith("pos") else H_RED if sent.lower().startswith("neg") else "#555555"
                rows.append([P(t, st["cell"]),
                             P(f"{escape(str(nw.get('source', '')))}<br/>{escape(str(nw.get('age', '')))}", st["cell"]),
                             P(f"{escape(str(nw.get('impact', '')))}<br/><font color='{col}'>{escape(sent)}</font>", st["cell"])])
            f.append(_table(rows, [fw * 0.62, fw * 0.22, fw * 0.16]))
            f.append(P("Real headlines from the app's news feeds, ranked by impact then recency. Sentiment is a keyword label.", st["small"]))
        if brief.get("ai"):
            f.append(Spacer(1, 4))
            txt = escape(brief["ai"]).replace("**", "")
            f.append(_box(P("<b>AI commentary.</b> " + txt.replace("\n", "<br/>"), st["box"]), colors.HexColor("#EEF3FA"), NAVY))
            f.append(P("Written by an AI model from the numbers and headlines in this report only. It can be wrong and is not advice.", st["small"]))

    if rep["global"]:
        head("Global cues")
        for line in tx["global"]:
            f.append(P(escape(line), st["body"]))
        items = [("USD/INR (up = rupee weaker)" if nm == "USD/INR" else nm, v["pct"]) for nm, v in rep["global"].items()
                 if nm != "US 10Y yield" and v["pct"] is not None]
        if items:
            f.append(chart_bars(f"Global markets and commodities - change over the period", items, height=2.6))
    if rep["sectors"]:
        head("Sector performance")
        up = sum(1 for _, p in rep["sectors"] if p > 0)
        f.append(P(f"{up} of {len(rep['sectors'])} sectors we track rose over the period. "
                   f"The strongest was {escape(rep['sectors'][0][0])} ({rep['sectors'][0][1]:+.2f}%) and the weakest "
                   f"{escape(rep['sectors'][-1][0])} ({rep['sectors'][-1][1]:+.2f}%).", st["body"]))
        f.append(chart_bars("NSE sectoral indices - change over the period", rep["sectors"]))

    if rep["kind"] != "daily":
        cmp_ = chart_compare(rep)
        if cmp_:
            f.append(Spacer(1, 6))
            f.append(cmp_)

    # levels table
    lv = rep["levels"]
    head("Key levels on the Nifty")
    rows = [[P("Level", st["th"]), P("Value", st["th"]), P("Versus today's close", st["th"])]]

    def lrow(name, v):
        if v is None:
            return
        d = (lv["last"] / v - 1) * 100
        rows.append([P(name, st["cell"]), P(f"<b>{v:,.0f}</b>", st["cell"]),
                     P(f"<font color='{_c(d)}'>close is {abs(d):.1f}% {'above' if d >= 0 else 'below'}</font>", st["cell"])])
    lrow("52-week high", lv["hi52"])
    lrow("52-week low", lv["lo52"])
    lrow("20-day high", lv["hi20"])
    lrow("20-day low", lv["lo20"])
    lrow("20-day average", lv["dma20"])
    lrow("50-day average", lv["dma50"])
    lrow("200-day average", lv["dma200"])
    f.append(_table(rows, [fw * 0.34, fw * 0.2, fw * 0.46]))
    f.append(P("Support and resistance are read from these levels: the nearest ones below and above the close.", st["small"]))

    if rep["kind"] == "daily":
        if rep.get("fii"):
            head("FII / DII activity")
            rws = [r for r in rep["fii"]["rows"] if r.get("net") is not None]
            date = rws[0].get("date", "") if rws else ""
            tb = [[P("₹ crore", st["th"]), P("Buy", st["th"]), P("Sell", st["th"]), P(f"Net ({escape(str(date))})", st["th"])]]
            for r in rws:
                tb.append([P(r["who"], st["cellb"]), P(f"{r['buy']:,.0f}" if r.get("buy") is not None else "-", st["cell"]),
                           P(f"{r['sell']:,.0f}" if r.get("sell") is not None else "-", st["cell"]),
                           P(f"<font color='{_c(r['net'])}'><b>{r['net']:+,.0f}</b></font>", st["cell"])])
            f.append(_table(tb, [fw * 0.25, fw * 0.25, fw * 0.25, fw * 0.25]))
            fl = chart_flows(rep["fii"])
            if fl:
                f.append(Spacer(1, 4))
                f.append(fl)
            ft = _flows_table(rep, st, fw)
            if ft is not None and rep["flows"]["mtd"]["n"] >= 2:
                f.append(Spacer(1, 6))
                f.append(ft)
                f.append(P("Totals add up only the sessions this app has recorded, so they are lower than the exchange totals until "
                           "the record is complete.", st["small"]))
            f.append(P(f"Provisional figures, source: {escape(rep['fii'].get('source', 'NSE'))}. Foreign money leading while "
                       "domestic funds sell makes a rally depend more on global factors such as crude and the dollar.", st["small"]))
        if rep.get("breadth"):
            head("Market breadth")
            tb = [[P("Index", st["th"]), P("Advances", st["th"]), P("Declines", st["th"]), P("Advancing", st["th"])]]
            for r in rep["breadth"]["indices"]:
                tb.append([P(escape(r["name"]), st["cellb"]), P(str(r["advances"]), st["cell"]), P(str(r["declines"]), st["cell"]),
                           P(f"{r['adv_pct']}%" if r.get("adv_pct") is not None else "-", st["cell"])])
            f.append(_table(tb, [fw * 0.4, fw * 0.2, fw * 0.2, fw * 0.2]))
        mv = rep.get("movers")
        if mv and (mv.get("gainers") or mv.get("losers")):
            head("Nifty 50 top movers")
            g_, l_ = mv.get("gainers", []), mv.get("losers", [])
            tb = [[P("Top gainers", st["th"]), P("%", st["th"]), P("Top losers", st["th"]), P("%", st["th"])]]
            for i in range(max(len(g_), len(l_))):
                a = g_[i] if i < len(g_) else None
                b = l_[i] if i < len(l_) else None
                tb.append([P(escape(str((a or {}).get("name") or (a or {}).get("symbol", ""))), st["cell"]),
                           P(f"<font color='{H_TEAL}'>{a['change_pct']:+.2f}</font>" if a and a.get("change_pct") is not None else "", st["cell"]),
                           P(escape(str((b or {}).get("name") or (b or {}).get("symbol", ""))), st["cell"]),
                           P(f"<font color='{H_RED}'>{b['change_pct']:+.2f}</font>" if b and b.get("change_pct") is not None else "", st["cell"])])
            f.append(_table(tb, [fw * 0.34, fw * 0.16, fw * 0.34, fw * 0.16]))
            if mv.get("note"):
                f.append(P(escape(str(mv["note"])), st["small"]))

    if rep["kind"] != "daily":
        ft = _flows_table(rep, st, fw)
        if ft is not None:
            head("FII / DII activity")
            f.append(ft)
            f.append(P("Totals add up only the sessions this app has recorded (the daily report saves each day's NSE figures), "
                       "so they can be lower than the exchange totals. Provisional figures.", st["small"]))

    # outlook
    head("Outlook: bull, base and bear")
    f.append(P("Illustrative levels read from the chart. They are not forecasts.", st["small"]))
    rows = [[P("Scenario", st["th"]), P("Conditions", st["th"]), P("What it could mean", st["th"])]]
    colmap = {"BULL": (GREEN_BG, H_TEAL), "BASE": (AMBER_BG, "#B7791F"), "BEAR": (RED_BG, H_RED)}
    extra = []
    for i, (name, cond, res) in enumerate(tx["scenarios"], start=1):
        bg, fg = colmap[name]
        rows.append([P(f"<font color='{fg}'><b>{name}</b></font>", st["cell"]), P(escape(cond), st["cell"]), P(escape(res), st["cell"])])
        extra.append(("BACKGROUND", (0, i), (-1, i), bg))
    f.append(_table(rows, [fw * 0.14, fw * 0.46, fw * 0.4], zebra=False, extra=extra))

    head("What should investors do now?")
    rows = [[P(f"<b>{escape(a)}</b>", st["cell"]), P(escape(b), st["cell"])] for a, b in tx["guidance"]]
    f.append(_table(rows, [fw * 0.24, fw * 0.76], head=False, zebra=False))

    head("Key takeaway")
    f.append(_box(P(escape(tx["takeaway"]), st["boxw"]), NAVY, GOLD))
    f.append(Spacer(1, 8))
    miss = rep.get("missing") or []
    notes = ("<b>Data notes.</b> Prices are from Yahoo Finance and may be delayed; breadth, movers and FII/DII (daily report) are from NSE. "
             "Brent and gold are front-month futures contracts and can differ from spot quotes by a few dollars. "
             "FII/DII figures are for the previous completed session until NSE publishes the day's data. "
             "Round-number levels are shown because they often act as support or resistance. "
             "The market text is generated from the numbers and does not explain the news behind the moves; headlines and AI commentary, when shown, are in their own sections." +
             (f" Not available when this report was made: {escape(', '.join(miss))}." if miss else ""))
    f.append(P(notes, st["small"]))
    f.append(P("<b>Disclaimer.</b> This update is for information only. It is not investment, tax or legal advice. Scenario levels are "
               "illustrative, not forecasts. Markets carry risk; please consult a SEBI-registered adviser before investing. "
               f"Generated {escape(rep.get('generated', ''))}.", st["small"]))
    doc.build(f)
    return buf.getvalue()
