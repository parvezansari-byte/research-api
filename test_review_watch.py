import json, os
from datetime import date

from fastapi import FastAPI
from fastapi.testclient import TestClient

import push_api
import review_watch_api as rw
from fake_supabase import FakeSb

ok = bad = 0
def check(c, m):
    global ok, bad
    if c: ok += 1
    else: bad += 1; print("FAIL", m)

TODAY = date(2026, 10, 28)
OWNER = push_api.OWNER_EMAIL
FLEXI = "HDFC Flexi Cap Fund - Direct Plan - Growth"

def row(i, cid, code, name, units, avg, buy):
    return {"id": i, "owner_email": OWNER, "client_id": cid, "scheme_code": code,
            "scheme_name": name, "folio_number": "", "units": units, "avg_nav": avg, "buy_date": buy}

# NAV 10 -> invested 100000 at avg 10 = 10000 units; NAV decides current value
NAVS = {"111": (10.0, "28-10-2026"), "222": (20.0, "28-10-2026"), "333": (13.0, "28-10-2026"), "444": None}
nav = lambda c: NAVS.get(c)
rows = [
    row(1, 1, "111", FLEXI, 10000, 10, "2022-01-03"),             # flat -> Behind
    row(2, 2, "222", FLEXI, 10000, 10, "2022-01-03"),             # doubled -> Ahead
    row(3, 1, "333", "Some Unknown Fund", 10000, 10, "2022-01-03"),  # unmatched
    row(4, 2, "111", FLEXI, 10000, 10, None),                     # no date
    row(5, 2, "444", FLEXI, 10000, 10, "2022-01-03"),             # no NAV
]
res = rw.build_watch(rows, {1: "Asha", 2: "Ravi"}, TODAY, nav)
b = res["behind"]
check(len(b) == 1 and b[0]["client_name"] == "Asha" and b[0]["key"] == "1:111", f"behind {b}")
check(res["counts"]["undated"] == 1 and res["counts"]["unpriced"] == 1, f"counts {res['counts']}")
check(res["counts"]["judged"] == 3 and res["counts"]["behind"] == 1, "judged/behind")
check(b[0]["vs_benchmark_pp"] < 0, "negative gap")
check(rw.build_watch([], {}, TODAY, nav)["behind"] == [], "empty")

# worst first + dedupe
NAVS["555"] = (13.0, "x")
rows2 = rows[:1] + [row(6, 3, "555", FLEXI, 10000, 10, "2022-01-03")]
r2 = rw.build_watch(rows2, {1: "Asha", 3: "Meera"}, TODAY, nav)["behind"]
check([x["client_name"] for x in r2] == ["Asha", "Meera"], f"worst first {[x['client_name'] for x in r2]}")
check(len(rw.build_watch(rows[:1] + rows[:1], {}, TODAY, nav)["behind"]) == 1, "dedupe")
check(rw.build_watch(rows[:1], {}, TODAY, nav)["behind"][0]["client_name"] == "Client 1", "name fallback")

check(len(rw.new_items(r2, {"1:111"})) == 1, "new_items")
t, bd = rw.push_text(r2)
check("2 funds" in t and "1 more" in bd and "2 clients" in bd, f"text {t} / {bd}")
t1, b1 = rw.push_text(r2[:1])
check(t1.startswith("1 fund ") and "more" not in b1, "singular")

# HTTP
os.environ["PUSH_TICK_SECRET"] = "s3"
os.environ.pop("ORDERS_OWNER_EMAIL", None)
sb = FakeSb()
sb.tables["crm_manual_holdings"] = [dict(r) for r in rows]
sb.tables["crm_clients"] = [{"id": 1, "owner_email": OWNER, "full_name": "Asha"},
                            {"id": 2, "owner_email": OWNER, "full_name": "Ravi"}]
sent = []
push_api.send_to_email = lambda sb_, email, title, body, data=None, kind=None: (sent.append((email, title, body, kind)) or {"sent": 1})
app = FastAPI(); rw.register_review_watch(app, lambda: sb, today_fn=lambda: TODAY, nav_lookup=nav)
c = TestClient(app)
r = c.get(f"/crm/{OWNER}/review-watch")
check(r.status_code == 200 and r.json()["counts"]["behind"] == 1, "GET ok")
os.environ["ORDERS_OWNER_EMAIL"] = "boss@x.com"
check(c.get(f"/crm/{OWNER}/review-watch").status_code == 403, "owner guard")
os.environ.pop("ORDERS_OWNER_EMAIL")

check(c.post("/portfolio/watch/tick").status_code == 403, "no secret")
check(c.post("/portfolio/watch/tick", headers={"X-Tick-Secret": "bad"}).status_code == 403, "bad secret")
H = {"X-Tick-Secret": "s3"}
r = c.post("/portfolio/watch/tick", headers=H).json()
check(r["behind"] == 1 and r["new"] == 1 and len(sent) == 1 and sent[0][3] == "review", f"tick1 {r}")
check(json.loads(push_api.state_get(sb, "review_watch")) == ["1:111"], "state saved")
r = c.post("/portfolio/watch/tick", headers=H).json()
check(r["new"] == 0 and len(sent) == 1, "second tick silent")
r = c.post("/portfolio/watch/tick?force=true", headers=H).json()
check(r["new"] == 1 and len(sent) == 2, "force resends")
NAVS["333"] = (13.0, "x"); sb.tables["crm_manual_holdings"].append(row(9, 2, "555", FLEXI, 10000, 10, "2022-01-03"))
r = c.post("/portfolio/watch/tick", headers=H).json()
check(r["behind"] == 2 and r["new"] == 1 and len(sent) == 3 and "Ravi" in sent[2][2], f"only new item pushed {r}")
os.environ.pop("PUSH_TICK_SECRET")
check(c.post("/portfolio/watch/tick", headers=H).status_code == 503, "secret unset")
print(f"{ok} passed, {bad} failed")
raise SystemExit(1 if bad else 0)
