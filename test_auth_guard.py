"""Tests for auth_guard (run: python test_auth_guard.py)."""
import os, time
from fastapi import FastAPI
from fastapi.testclient import TestClient
import auth_guard as g

os.environ["AUTH_SECRET"] = "x" * 32
os.environ.pop("AUTH_ENFORCE", None)

def make_app():
    app = FastAPI()
    @app.post("/auth/login")
    def login(b: dict):
        if b.get("password") != "pw":
            from fastapi import HTTPException
            raise HTTPException(401, "nope")
        return {"email": b["email"].lower(), "name": "Pat"}
    @app.get("/crm/{email}/clients")
    def clients(email: str): return {"for": email}
    @app.get("/market/indices")
    def pub(): return {"ok": 1}
    @app.post("/push/test")
    def pt(): return {"sent": 1}
    @app.post("/push/tick")
    def tick(): return {"tick": 1}
    g.register_guard(app)
    return app

c = TestClient(make_app())
fails = []
def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond: fails.append(name)

# tokens
t = g.make_token("A@B.com")
check("token verifies", g.verify_token(t) == "a@b.com")
check("tampered rejected", g.verify_token(t[:-2] + "xx") is None)
check("expired rejected", g.verify_token(g.make_token("a@b.com", now=time.time() - 40*86400)) is None)
check("garbage rejected", g.verify_token("abc") is None and g.verify_token("") is None)
os.environ["AUTH_SECRET"] = "short"
check("no token without strong secret", g.make_token("a@b.com") is None)
os.environ["AUTH_SECRET"] = "x" * 32

# login hands out token, bad login unchanged
r = c.post("/auth/login", json={"email": "Pat@x.com", "password": "pw"})
tok = r.json().get("token")
check("login returns token", r.status_code == 200 and tok and r.json()["email"] == "pat@x.com")
check("bad login still 401", c.post("/auth/login", json={"email": "p@x.com", "password": "no"}).status_code == 401)

# permissive mode (AUTH_ENFORCE off): everything passes, counted
check("permissive: no token passes", c.get("/crm/pat@x.com/clients").status_code == 200)
check("stats counted missing", g.STATS["missing_token"] >= 1)
st = c.get("/auth/guard-status").json()
check("guard-status shows not enforcing", st["secret_set"] and not st["enforce"])

# enforce
os.environ["AUTH_ENFORCE"] = "1"
H = {"Authorization": f"Bearer {tok}"}
check("enforce: no token -> 401", c.get("/crm/pat@x.com/clients").status_code == 401)
check("enforce: own token ok", c.get("/crm/pat@x.com/clients", headers=H).status_code == 200)
check("enforce: email case/%40 ok", c.get("/crm/Pat%40x.com/clients", headers=H).status_code == 200)
check("enforce: other account -> 403", c.get("/crm/boss@x.com/clients", headers=H).status_code == 403)
check("enforce: bad token -> 401", c.get("/crm/pat@x.com/clients", headers={"Authorization": "Bearer junk"}).status_code == 401)
check("enforce: public route open", c.get("/market/indices").status_code == 200)
check("enforce: tick not guarded here", c.post("/push/tick").status_code == 200)
check("enforce: push test needs token", c.post("/push/test").status_code == 401)
check("enforce: non-owner token blocked on push", c.post("/push/test", headers=H).status_code == 403)
owner = g.make_token("parvez.ansari@wealthy.in")
check("enforce: owner token ok on push", c.post("/push/test", headers={"Authorization": f"Bearer {owner}"}).status_code == 200)
os.environ["AUTH_SECRET"] = ""
check("enforce off if secret missing", not g.enforcing())

# ---- browsers: every response, including the ones the guard rebuilds, carries CORS headers
from fastapi.middleware.cors import CORSMiddleware
os.environ["AUTH_SECRET"] = "x" * 32
os.environ["AUTH_ENFORCE"] = "1"
capp = FastAPI()
capp.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
@capp.post("/auth/login")
def clogin(b: dict): return {"email": b["email"], "name": "Pat"}
@capp.get("/crm/{email}/clients")
def cclients(email: str): return {"for": email}
g.register_guard(capp)
cc = TestClient(capp)
O = {"Origin": "http://localhost:5555"}
r = cc.post("/auth/login", json={"email": "a@b.com"}, headers=O)
check("login returns a token", r.status_code == 200 and "token" in r.json())
check("login response has CORS header", r.headers.get("access-control-allow-origin") in ("*", "http://localhost:5555"))
r = cc.get("/crm/a@b.com/clients", headers=O)
check("guard 401 has CORS header", r.status_code == 401 and r.headers.get("access-control-allow-origin") in ("*", "http://localhost:5555"))
r = cc.options("/auth/login", headers={**O, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type,authorization"})
check("preflight allowed", r.status_code == 200 and "access-control-allow-origin" in r.headers)
os.environ.pop("AUTH_ENFORCE", None)

print("\nALL PASSED" if not fails else f"\nFAILED: {fails}")
raise SystemExit(1 if fails else 0)
