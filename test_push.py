"""Tests for push_api.py. Run: python test_push.py"""
import os
from datetime import datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

import push_api
import push_service
from fake_supabase import FakeSb

OWNER = push_api.OWNER_EMAIL
sb = FakeSb()
app = FastAPI()
push_api.register_push(app, lambda: sb)
http = TestClient(app)
failures = []
sent = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if not cond else ""))
    if not cond:
        failures.append(name)


def fake_send(token, title, body, data=None):
    sent.append((token, title, body))
    return "invalid_token" if token.startswith("dead") else "ok"


push_service.send = fake_send
TOK = "t" * 30

# register
r = http.post("/push/register", json={"email": "evil@x.com", "token": TOK})
check("register rejects non-owner", r.status_code == 403)
r = http.post("/push/register", json={"email": OWNER, "token": "short"})
check("register rejects short token", r.status_code == 400)
r = http.post("/push/register", json={"email": OWNER.upper(), "token": TOK})
check("register ok", r.status_code == 200)
http.post("/push/register", json={"email": OWNER, "token": TOK})
check("register is idempotent", len(sb.tables["push_tokens"]) == 1)
http.post("/push/register", json={"email": OWNER, "token": "dead" + "d" * 30})

# test
r = http.post("/push/test", json={"email": "evil@x.com"})
check("test rejects non-owner", r.status_code == 403)
r = http.post("/push/test", json={"email": OWNER})
check("test sends + prunes dead", r.status_code == 200 and r.json()["sent"] == 1
      and r.json()["removed_dead"] == 1 and len(sb.tables["push_tokens"]) == 1, r.text)

# tick auth
r = http.post("/push/tick")
check("tick 503 without server secret", r.status_code == 503)
os.environ["PUSH_TICK_SECRET"] = "s3cret"
r = http.post("/push/tick", headers={"X-Tick-Secret": "nope"})
check("tick rejects bad secret", r.status_code == 403)
r = http.post("/push/tick")
check("tick rejects missing secret", r.status_code == 403)

# signals: first run silent, flip alerts, same = silent
score = {"v": 3}
fn = lambda idx, owner_email: {"score": score["v"], "agree": "3/4"}
sent.clear()
out = push_api.check_signals(sb, fn, sleep=lambda s: None)
check("first run initialises, no alert", not sent and "initialised" in out["NIFTY 50"], out)
out = push_api.check_signals(sb, fn, sleep=lambda s: None)
check("unchanged = no alert", not sent and "unchanged" in out["NIFTY 50"], out)
score["v"] = -3
out = push_api.check_signals(sb, fn, sleep=lambda s: None)
check("flip alerts owner", len(sent) == 2 and "BUY PE" in sent[0][1], (sent, out))
def boom(idx, owner_email): raise RuntimeError("dhan down")
out = push_api.check_signals(sb, boom, sleep=lambda s: None)
check("signal error is reported not raised", "error" in out["NIFTY 50"])

# digest: once per hour, dedupes headline
now = datetime(2026, 10, 19, 10, 5, tzinfo=push_api.IST)
news = lambda: [{"title": "RBI holds rates", "source": "ET", "link": "L1", "impact": "High"}]
quotes = lambda: ["NIFTY 25,000 (+0.50%)"]
sent.clear()
push_api.hourly_digest(sb, now, quotes_fn=quotes, news_fn=news)
check("digest sent", len(sent) == 1 and "RBI holds rates" in sent[0][2], sent)
check("digest once per hour", push_api.hourly_digest(sb, now, quotes_fn=quotes, news_fn=news) == "already sent this hour")
later = datetime(2026, 10, 19, 11, 5, tzinfo=push_api.IST)
sent.clear()
push_api.hourly_digest(sb, later, quotes_fn=quotes, news_fn=news)
check("next hour omits repeated headline", len(sent) == 1 and "RBI" not in sent[0][2], sent)
check("nothing to send", push_api.hourly_digest(sb, datetime(2026, 10, 19, 12, 5, tzinfo=push_api.IST),
      quotes_fn=lambda: [], news_fn=lambda: []) == "nothing to send")

# force tick wiring with stubbed internals
push_api.check_signals = lambda sb_: "sig-ok"
push_api.hourly_digest = lambda sb_, now_, force=False: "dig-ok"
r = http.post("/push/tick?force=true", headers={"X-Tick-Secret": "s3cret"})
check("forced tick runs both jobs", r.status_code == 200 and r.json()["signal"] == "sig-ok"
      and r.json()["digest"] == "dig-ok", r.text)

# hookup via orders_api
import importlib, orders_api
app2 = FastAPI()
orders_api.register_orders(app2, lambda: sb)
paths = {r.path for r in app2.routes}
check("orders hookup also mounts push, holdings, nav",
      {"/push/tick", "/push/register", "/nav/status"} <= paths and
      any("portfolio" in p for p in paths))

# ---- notification history ---------------------------------------------------
sb2 = FakeSb()
app3 = FastAPI()
push_api.register_push(app3, lambda: sb2)
h2 = TestClient(app3)
h2.post("/push/register", json={"email": OWNER, "token": "h" * 30})
for i in range(12):
    push_api.send_to_email(sb2, OWNER, f"alert {i}", f"body {i}", {"type": "signal"})
push_api.send_to_email(sb2, OWNER, "dig", "digest body", {"type": "digest"})
r = h2.get(f"/push/{OWNER}/history?limit=5")
j = r.json()
check("history returns newest first, limited", r.status_code == 200 and len(j["items"]) == 5
      and j["items"][0]["title"] == "dig" and j["items"][1]["title"] == "alert 11", r.text)
check("history counts unread", j["unread"] == 13, str(j.get("unread")))
check("history keeps kind", j["items"][0]["kind"] == "digest" and j["items"][1]["kind"] == "signal")
check("history rejects other accounts", h2.get("/push/evil@x.com/history").status_code == 403)
check("mark-read rejects other accounts", h2.post("/push/evil@x.com/history/read").status_code == 403)
check("mark-read ok", h2.post(f"/push/{OWNER}/history/read").json() == {"ok": True})
j = h2.get(f"/push/{OWNER}/history").json()
check("unread is zero after mark-read", j["unread"] == 0 and len(j["items"]) == 13 and all(i["read"] for i in j["items"]))
# test pushes are saved too, and a broken history table never blocks the push
r = h2.post("/push/test", json={"email": OWNER})
check("test push saved as kind=test", h2.get(f"/push/{OWNER}/history?limit=1").json()["items"][0]["kind"] == "test")
class BrokenSb(FakeSb):
    def table(self, name):
        if name == "push_history":
            raise Exception("relation does not exist")
        return super().table(name)
bsb = BrokenSb(); bsb.tables["push_tokens"] = [{"token": "k" * 30, "owner_email": OWNER}]
res = push_api.send_to_email(bsb, OWNER, "t", "b", {"type": "signal"})
check("missing history table does not stop the push", res["sent"] == 1, str(res))

print("\nFAILED: %s" % failures if failures else "\nALL PASSED")
raise SystemExit(1 if failures else 0)
