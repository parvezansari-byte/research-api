from fastapi import FastAPI
from fastapi.testclient import TestClient
import planner_api as p

ok = bad = 0
def check(c, m):
    global ok, bad
    if c: ok += 1
    else: bad += 1; print("FAIL", m)
G = p.GoalRequest

# zero inflation, zero return: need = target / months
r = p.plan_goal(G(target_today=1200000, years=10, inflation_pct=0, return_pct=0))
check(r["monthly_sip"] == 10000 and r["future_target"] == 1200000 and r["total_invested"] == 1200000 and r["growth"] == 0, r)
# 10,000/month at 12% a year (effective, end of month) for 10 years is about 22.2 lakh
fv = p.sip_fv(10000, 10, 12, 0)
check(2.20e6 < fv < 2.24e6, f"fv {fv}")
# solved SIP reproduces the target
r = p.plan_goal(G(target_today=5000000, years=15, inflation_pct=6, return_pct=12, existing=200000, step_up_pct=10))
check(abs(r["projected_corpus"] - r["future_target"]) < 5, f"solves {r['projected_corpus']} vs {r['future_target']}")
check(r["on_track"] and r["solved"] and r["last_year_sip"] > r["monthly_sip"], "on track, step-up grows")
# step-up lowers the starting SIP
a = p.plan_goal(G(target_today=5000000, years=15)); b = p.plan_goal(G(target_today=5000000, years=15, step_up_pct=10))
check(b["monthly_sip"] < a["monthly_sip"], "step-up lowers first SIP")
# existing money bigger than goal -> no SIP needed
r = p.plan_goal(G(target_today=100000, years=10, existing=10000000))
check(r["monthly_sip"] == 0 and r["on_track"], "already enough")
# user SIP projection, off track
r = p.plan_goal(G(target_today=5000000, years=10, monthly_sip=5000))
check(not r["solved"] and not r["on_track"] and r["monthly_sip"] == 5000, "projection off track")
s = {x["return_pct"]: x["corpus"] for x in r["scenarios"]}
check(s[10.0] < s[12.0] < s[14.0], "scenarios ordered")
check(len(r["path"]) == 10 and r["path"][-1]["value"] == round(r["projected_corpus"], 0) or abs(r["path"][-1]["value"] - r["projected_corpus"]) < 2, "path ends at corpus")
check(p.plan_goal(G(target_today=100000, years=5, return_pct=1))["scenarios"][0]["return_pct"] == 0, "scenario floor at 0")
# validation
app = FastAPI(); p.register_planner(app); c = TestClient(app)
for bad_body in ({"target_today": 0, "years": 5}, {"target_today": 1e5, "years": 0}, {"target_today": 1e5, "years": 51},
                 {"target_today": 1e5, "years": 5, "return_pct": 40}, {"target_today": 1e5, "years": 5, "existing": -1},
                 {"target_today": 1e5, "years": 5, "inflation_pct": 25}, {"target_today": 1e5, "years": 5, "step_up_pct": 50}):
    check(c.post("/planner/goal", json=bad_body).status_code == 400, f"rejects {bad_body}")
j = c.post("/planner/goal", json={"target_today": 2500000, "years": 12}).json()
check(j["monthly_sip"] > 0 and "note" in j, "http ok")
print(f"{ok} passed, {bad} failed"); raise SystemExit(1 if bad else 0)
