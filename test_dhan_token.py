"""Tests for dhan_token (run: python test_dhan_token.py)."""
import base64, json, os, sys
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fake_supabase import FakeSb
import dhan_token as d

fails = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond: fails.append(name)

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
def jwt(hours, tag="a"):
    def b(x): return base64.urlsafe_b64encode(json.dumps(x).encode()).decode().rstrip("=")
    return f"{b({'alg':'HS256'})}.{b({'exp': (NOW + timedelta(hours=hours)).timestamp(), 't': tag})}.sig"

class Resp:
    def __init__(self, code, body=None, text=""):
        self.status_code, self._b, self.text = code, body, text
    def json(self):
        if self._b is None: raise ValueError("no json")
        return self._b

os.environ["DHAN_CLIENT_ID"] = "1000000001"
os.environ.pop("DHANHQ_ACCESS_TOKEN", None)

check("expiry read from JWT", d.jwt_expiry(jwt(5)) == NOW + timedelta(hours=5))
check("hours left", d.hours_left(jwt(5), NOW) == 5.0 and d.hours_left(jwt(-2), NOW) == -2.0)
check("garbage token has no expiry", d.jwt_expiry("abc") is None and d.jwt_expiry("") is None and d.hours_left("x.y.z") is None)

# status never leaks the token
os.environ["DHAN_ACCESS_TOKEN"] = jwt(10, "env")
sb = FakeSb()
s = d.status(sb, NOW)
check("status: environment token", s["source"] == "environment" and s["hours_left"] == 10.0 and not s["expired"])
check("status has no token text", "env" not in json.dumps(s).replace("environment", "") and "sig" not in json.dumps(s))

# best token = later expiry
d.save_stored(sb, jwt(20, "saved"))
check("saved token wins when it lasts longer", d.best_token(sb, NOW)[1] == "saved")
d.save_stored(sb, jwt(2, "saved-old"))
check("environment wins when the saved one is older", d.best_token(sb, NOW)[1] == "environment")

# renewal
calls = []
def good_get(url, headers):
    calls.append((url, dict(headers)))
    return Resp(200, {"accessToken": jwt(24, "renewed")})
sb = FakeSb(); os.environ["DHAN_ACCESS_TOKEN"] = jwt(10, "env")
r = d.renew(sb, http_get=good_get, now=NOW)
check("renews when under 20h", r["renewed"] and r["saved"] and abs(r["hours_left"] - 24) < 0.1)
check("sends token and client id in the headers", calls[0][0].endswith("/RenewToken") and calls[0][1]["dhanClientId"] == "1000000001" and calls[0][1]["access-token"].count(".") == 2)
check("new token installed in the environment", "renewed" in base64.urlsafe_b64decode(os.environ["DHAN_ACCESS_TOKEN"].split(".")[1] + "==").decode())
check("new token stored in push_state", d.load_stored(sb) == os.environ["DHAN_ACCESS_TOKEN"])
n = len(calls)
r = d.renew(sb, http_get=good_get, now=NOW)
check("no renewal when 24h left", not r["renewed"] and len(calls) == n)
r = d.renew(sb, force=True, http_get=good_get, now=NOW)
check("force renews anyway", r["renewed"] and len(calls) == n + 1)

# failure paths
def err(name, fn):
    try:
        fn(); ok = False
    except d.DhanTokenError:
        ok = True
    check(name, ok)
os.environ["DHAN_ACCESS_TOKEN"] = jwt(-1, "dead")
err("expired token cannot be renewed", lambda: d.renew(FakeSb(), http_get=good_get, now=NOW))
os.environ["DHAN_ACCESS_TOKEN"] = jwt(3)
err("Dhan 401 on renewal", lambda: d.renew(FakeSb(), http_get=lambda u, h: Resp(401, {}), now=NOW))
err("renewal reply without a token", lambda: d.renew(FakeSb(), http_get=lambda u, h: Resp(200, {"ok": 1}), now=NOW))
for k in ("DHAN_ACCESS_TOKEN",): os.environ.pop(k, None)
err("no token at all", lambda: d.renew(FakeSb(), http_get=good_get, now=NOW))
os.environ["DHAN_ACCESS_TOKEN"] = jwt(3); os.environ.pop("DHAN_CLIENT_ID")
err("no client id", lambda: d.renew(FakeSb(), http_get=good_get, now=NOW))
os.environ["DHAN_CLIENT_ID"] = "1000000001"

# plain-text and nested reply shapes
tok = jwt(24, "plain")
check("plain JWT reply accepted", d.renew_remote("1", "x", lambda u, h: Resp(200, None, text=tok)) == tok)
check("nested data.accessToken accepted", d.renew_remote("1", "x", lambda u, h: Resp(200, {"data": {"accessToken": tok}})) == tok)

# GET not allowed -> falls back to POST
os.environ["DHAN_ACCESS_TOKEN"] = jwt(3)
posted = []
def post_ok(u, h): posted.append(u); return Resp(200, {"accessToken": jwt(24, "viapost")})
new = d.renew_remote("1", "x", http_get=lambda u, h: Resp(405, {}), http_post=post_ok)
check("405 on GET falls back to POST", posted and d.jwt_expiry(new) is not None)

# saving fails -> still renewed, with a warning
class BadSb:
    def table(self, *_): raise RuntimeError("db down")
os.environ["DHAN_ACCESS_TOKEN"] = jwt(3)
r = d.renew(BadSb(), http_get=good_get, now=NOW)
check("renewed even if saving fails, with a warning", r["renewed"] and r["saved"] is False and "warning" in r)

# manual token
sb = FakeSb()
fresh = jwt(24, "manual")
r = d.set_token(sb, fresh, http_get=lambda u, h: Resp(200, {}), now=NOW)
check("manual token accepted and saved", r["saved"] and d.load_stored(sb) == fresh and os.environ["DHAN_ACCESS_TOKEN"] == fresh)
err("manual: not a token", lambda: d.set_token(sb, "hello", http_get=lambda u, h: Resp(200, {}), now=NOW))
err("manual: expired token", lambda: d.set_token(sb, jwt(-5), http_get=lambda u, h: Resp(200, {}), now=NOW))
err("manual: Dhan rejects it", lambda: d.set_token(sb, jwt(24), http_get=lambda u, h: Resp(401, {}), now=NOW))

# startup load
sb = FakeSb(); os.environ["DHAN_ACCESS_TOKEN"] = jwt(1, "stale-env")
d.save_stored(sb, jwt(20, "newer-saved"))
d.load_on_startup(sb)
check("start-up uses the newer saved token", "newer-saved" in base64.urlsafe_b64decode(os.environ["DHAN_ACCESS_TOKEN"].split(".")[1] + "==").decode())

# HTTP + auth
os.environ["PUSH_TICK_SECRET"] = "s3cret"
os.environ["DHAN_ACCESS_TOKEN"] = jwt(3)
app = FastAPI(); sbx = FakeSb(); d.register_dhan_token(app, lambda: sbx)
c = TestClient(app)
check("status needs the secret", c.get("/dhan/status").status_code == 403)
check("status with the secret", c.get("/dhan/status", headers={"X-Tick-Secret": "s3cret"}).status_code == 200)
check("renew needs the secret", c.post("/dhan/renew").status_code == 403)
r = c.post("/dhan/token", headers={"X-Tick-Secret": "s3cret"}, json={"token": "nope"})
check("bad manual token -> 400", r.status_code == 400)
d._http_get = lambda u, h: Resp(401, {})
r = c.post("/dhan/renew", headers={"X-Tick-Secret": "s3cret"})
check("failed renewal -> 409 with a reason", r.status_code == 409 and "401" in r.json()["detail"])
def netfail(u, h): raise ConnectionError("no route")
d._http_get = netfail
os.environ["DHAN_ACCESS_TOKEN"] = jwt(3)
r = c.post("/dhan/renew", headers={"X-Tick-Secret": "s3cret"})
check("network failure -> 409, not a crash", r.status_code == 409 and "Could not reach Dhan" in r.json()["detail"])
os.environ.pop("PUSH_TICK_SECRET")
check("no server secret -> 503", c.get("/dhan/status", headers={"X-Tick-Secret": "x"}).status_code == 503)

print("\nALL PASSED" if not fails else f"\nFAILED: {fails}")
sys.exit(1 if fails else 0)
