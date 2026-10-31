"""
privacy_api.py - public privacy policy page for the Play Store listing.

GET /privacy   HTML page (public: no email in the path, so the auth guard ignores it)

Env (Render, optional):
  PRIVACY_ENTITY  legal name shown on the page   (default "Wealthy - Advantage app")
  PRIVACY_EMAIL   contact for privacy requests   (default parvez.ansari@wealthy.in)
"""
import html
import os

from fastapi.responses import HTMLResponse

UPDATED = "31 October 2026"

BODY = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Advantage - Privacy Policy</title>
<style>body{{font-family:system-ui,sans-serif;max-width:720px;margin:32px auto;padding:0 16px;line-height:1.55;color:#222}}
h1{{font-size:26px}}h2{{font-size:18px;margin-top:28px}}li{{margin:4px 0}}</style></head><body>
<h1>Advantage - Privacy Policy</h1>
<p>Last updated: {updated}</p>
<p>{entity} ("we", "us") provides the Advantage mobile app: mutual fund and market research, portfolio
tools, a personal finance tracker, and a client-management area for financial advisors. This policy
explains what we collect, why, and the choices you have.</p>

<h2>What we collect</h2>
<ul>
<li><b>Account details:</b> your name, email address and a password (stored only as a one-way hash).</li>
<li><b>Finance tracker data you enter:</b> expenses, categories, loans, pending payments and alert settings.</li>
<li><b>Holdings and portfolio data you enter or upload:</b> funds or shares, units, buy prices and dates,
and files you choose to import (for example a CSV of holdings).</li>
<li><b>Advisor client records (advisor accounts only):</b> the advisor enters details of their own clients,
such as name, PAN, phone, email, date of birth, KYC status, risk profile, notes, holdings and orders.
The advisor is responsible for having the client's permission to record this information.</li>
<li><b>Device notification token:</b> to send you alerts and reminders you have turned on.</li>
<li><b>Technical data:</b> basic server logs (such as IP address and request time) used for security and
to keep the service running.</li>
</ul>
<p>The app does <b>not</b> read your SMS messages, contacts, location, camera or microphone.</p>

<h2>How we use it</h2>
<ul>
<li>To run the app: sign you in, show your portfolio value, reviews and reports, and send alerts.</li>
<li>To keep the service secure and to prevent misuse.</li>
<li>To comply with legal and regulatory obligations, including keeping order and transaction records.</li>
</ul>
<p>We do not sell your personal data and we do not use it for advertising.</p>

<h2>Who processes it for us</h2>
<ul>
<li>Cloud database and hosting providers that store and serve the data for the app.</li>
<li>Google Firebase Cloud Messaging, to deliver push notifications.</li>
<li>Public market data sources (such as AMFI and NSE) are queried for prices; no personal data is sent to them.</li>
</ul>
<p>Data is sent over encrypted (HTTPS) connections. We share personal data with others only where the
law requires it.</p>

<h2>How long we keep it</h2>
<p>We keep your data while your account is active. When you delete your account, your login and the data
linked to it are deleted. Order and transaction records may be retained for as long as the law requires.</p>

<h2>Your choices</h2>
<ul>
<li><b>Delete your account and data:</b> in the app, open the menu next to the Advantage title and choose
"Delete account", or use <a href="/account/delete-page">this web page</a>.</li>
<li><b>Access or correct your data:</b> you can view and edit it inside the app, or write to us.</li>
<li><b>Notifications:</b> you can turn them off in the app or in your phone settings.</li>
</ul>

<h2>Not investment advice</h2>
<p>Research, signals, reviews and calculators in the app are for information only and are not a
recommendation to buy or sell. Mutual fund investments are subject to market risks; read all
scheme-related documents carefully.</p>

<h2>Children</h2>
<p>The app is not meant for people under 18 and we do not knowingly collect their data.</p>

<h2>Changes and contact</h2>
<p>We may update this policy and will change the date above when we do. Questions or requests:
<a href="mailto:{email}">{email}</a>.</p>
</body></html>"""


def render() -> str:
    entity = html.escape(os.environ.get("PRIVACY_ENTITY", "").strip() or "Wealthy - Advantage app")
    email = html.escape(os.environ.get("PRIVACY_EMAIL", "").strip() or "parvez.ansari@wealthy.in")
    return BODY.format(updated=UPDATED, entity=entity, email=email)


def register_privacy(app):
    @app.get("/privacy", response_class=HTMLResponse)
    def privacy():
        return HTMLResponse(render())
