from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import risk_profile_api as r

ok = bad = 0
def check(c, m):
    global ok, bad
    if c: ok += 1
    else: bad += 1; print("FAIL", m)

N = len(r.QUESTIONS)
check(N == 8 and all(len(q["options"]) == 4 for q in r.QUESTIONS), "8 questions x 4 options")
check(len({q["id"] for q in r.QUESTIONS}) == N, "unique ids")
lo = r.score_profile([1] * N); hi = r.score_profile([4] * N); mid = r.score_profile([2] * 4 + [3] * 4)
check(lo["profile"] == "CONSERVATIVE" and lo["score"] == 8 and lo["mix"] == {"equity": 20, "hybrid": 20, "debt": 60}, lo)
check(hi["profile"] == "AGGRESSIVE" and hi["score"] == 32 and hi["mix"] == {"equity": 65, "hybrid": 15, "debt": 20} and not hi["adjusted"], hi)
check(mid["profile"] == "MODERATE" and mid["mix"] == {"equity": 45, "hybrid": 20, "debt": 35}, mid)
for t in (r.score_profile([1] * N), r.score_profile([4] * N), mid):
    check(sum(t["mix"].values()) == 100, "mix sums to 100")
# boundaries
check(r.score_profile([2, 2, 2, 2, 2, 2, 1, 1])["profile"] == "CONSERVATIVE" and r.score_profile([2, 2, 2, 2, 2, 2, 2, 1])["profile"] == "MODERATE", "14/15 boundary")
check(r.score_profile([3, 3, 3, 3, 3, 3, 3, 3])["score"] == 24 and r.score_profile([3] * N)["profile"] == "AGGRESSIVE", "24 is aggressive")
check(r.score_profile([3, 3, 3, 3, 3, 3, 3, 2])["profile"] == "MODERATE", "23 is moderate")
# overrides
x = r.score_profile([4, 4, 4, 4, 1, 4, 4, 4])
check(x["profile"] == "CONSERVATIVE" and len(x["adjusted"]) == 1, f"panic seller capped {x}")
x = r.score_profile([4, 1, 4, 4, 4, 4, 4, 4])
check(x["profile"] == "MODERATE" and "3 years" in x["adjusted"][0], f"short horizon capped {x}")
x = r.score_profile([4, 4, 4, 4, 4, 4, 4, 1])
check(x["profile"] == "MODERATE" and "emergency" in x["adjusted"][0], f"no emergency money {x}")
x = r.score_profile([4, 1, 4, 4, 1, 4, 4, 1])
check(x["profile"] == "CONSERVATIVE" and len(x["adjusted"]) == 1, f"caps don't stack oddly {x}")
check(r.score_profile([4, 1, 1, 1, 1, 1, 1, 1])["adjusted"] == [], "no cap needed when already low")
# profiles are values the client record accepts
check(set(r.PROFILES) == {"CONSERVATIVE", "MODERATE", "AGGRESSIVE"}, "values match CRM dropdown")
# http
app = FastAPI(); r.register_risk_profile(app); c = TestClient(app)
check(len(c.get("/risk/questions").json()["questions"]) == 8, "GET questions")
check(c.post("/risk/score", json={"answers": [3] * 8}).json()["profile"] == "AGGRESSIVE", "POST score")
for body in ({"answers": [3] * 7}, {"answers": [3] * 9}, {"answers": [0] + [3] * 7}, {"answers": [5] + [3] * 7}):
    check(c.post("/risk/score", json=body).status_code == 400, f"rejects {body}")
check(c.post("/risk/score", json={"answers": ["a"] * 8}).status_code == 422, "non-ints rejected")
print(f"{ok} passed, {bad} failed"); raise SystemExit(1 if bad else 0)
