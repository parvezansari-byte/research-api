import os
from fastapi import FastAPI
from fastapi.testclient import TestClient
import privacy_api as p

ok = bad = 0
def check(c, m):
    global ok, bad
    if c: ok += 1
    else: bad += 1; print("FAIL", m)

app = FastAPI(); p.register_privacy(app); c = TestClient(app)
os.environ.pop("PRIVACY_ENTITY", None); os.environ.pop("PRIVACY_EMAIL", None)
r = c.get("/privacy")
check(r.status_code == 200 and "text/html" in r.headers["content-type"], "served")
check("Privacy Policy" in r.text and "/account/delete-page" in r.text and "{" not in r.text.split("<body>")[1], "content, no stray braces")
check("parvez.ansari@wealthy.in" in r.text, "default contact")
os.environ["PRIVACY_ENTITY"] = "Acme <b>Ltd"; os.environ["PRIVACY_EMAIL"] = "p@acme.in"
t = c.get("/privacy").text
check("Acme &lt;b&gt;Ltd" in t and "p@acme.in" in t, "env + escaped")
print(f"{ok} passed, {bad} failed")
