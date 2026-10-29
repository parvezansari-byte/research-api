"""
Goal planner: POST /planner/goal

How much to invest every month to reach a goal, allowing for inflation,
money already set aside and an optional yearly step-up in the SIP.
Returns are an ASSUMPTION the caller chooses; results also show what happens
if returns come in 2 points lower or higher.
"""
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel


class GoalRequest(BaseModel):
    target_today: float                 # goal in today's money
    years: int
    inflation_pct: float = 6.0
    return_pct: float = 12.0            # expected yearly return
    existing: float = 0.0               # money already earmarked for the goal
    step_up_pct: float = 0.0            # yearly increase in the SIP
    monthly_sip: Optional[float] = None  # if given, project this SIP instead of solving


def _monthly_rate(annual_pct: float) -> float:
    return (1 + annual_pct / 100) ** (1 / 12) - 1


def sip_fv(first_sip: float, years: int, annual_pct: float, step_up_pct: float) -> float:
    """Value at the end of `years` of a monthly SIP that rises by step_up_pct each year."""
    i = _monthly_rate(annual_pct)
    g = step_up_pct / 100
    n = years * 12
    return sum(first_sip * (1 + g) ** (m // 12) * (1 + i) ** (n - 1 - m) for m in range(n))


def _invested(first_sip: float, years: int, step_up_pct: float) -> float:
    g = step_up_pct / 100
    return sum(first_sip * (1 + g) ** (m // 12) for m in range(years * 12))


def _check(req: GoalRequest):
    if not (0 < req.target_today <= 1e11):
        raise HTTPException(400, "Enter a goal amount")
    if not (1 <= req.years <= 50):
        raise HTTPException(400, "Years must be between 1 and 50")
    if not (0 <= req.inflation_pct <= 20):
        raise HTTPException(400, "Inflation must be between 0 and 20%")
    if not (0 <= req.return_pct <= 30):
        raise HTTPException(400, "Return must be between 0 and 30%")
    if not (0 <= req.step_up_pct <= 30):
        raise HTTPException(400, "Step-up must be between 0 and 30%")
    if req.existing < 0 or req.existing > 1e11:
        raise HTTPException(400, "Existing savings can't be negative")
    if req.monthly_sip is not None and not (0 <= req.monthly_sip <= 1e9):
        raise HTTPException(400, "SIP amount looks wrong")


def plan_goal(req: GoalRequest) -> dict:
    _check(req)
    y = req.years
    future_target = req.target_today * (1 + req.inflation_pct / 100) ** y

    def corpus_with(first_sip, ret):
        return req.existing * (1 + ret / 100) ** y + sip_fv(first_sip, y, ret, req.step_up_pct)

    unit = sip_fv(1.0, y, req.return_pct, req.step_up_pct)
    gap = max(0.0, future_target - req.existing * (1 + req.return_pct / 100) ** y)
    solved = gap / unit if unit else 0.0
    sip = float(req.monthly_sip) if req.monthly_sip is not None else solved
    final = corpus_with(sip, req.return_pct)
    invested = req.existing + _invested(sip, y, req.step_up_pct)

    path = []
    for yr in range(1, y + 1):
        v = req.existing * (1 + req.return_pct / 100) ** yr + sip_fv(sip, yr, req.return_pct, req.step_up_pct)
        path.append({"year": yr, "value": round(v, 0)})

    scenarios = []
    for d in (-2, 0, 2):
        r = max(0.0, req.return_pct + d)
        v = corpus_with(sip, r)
        scenarios.append({"return_pct": r, "corpus": round(v, 0),
                          "vs_goal_pct": round(v / future_target * 100, 1) if future_target else None})
    last_year_sip = sip * (1 + req.step_up_pct / 100) ** (y - 1)
    return {
        "future_target": round(future_target, 0),
        "monthly_sip": round(sip, 0),
        "last_year_sip": round(last_year_sip, 0),
        "solved": req.monthly_sip is None,
        "projected_corpus": round(final, 0),
        "total_invested": round(invested, 0),
        "growth": round(final - invested, 0),
        "existing_grown": round(req.existing * (1 + req.return_pct / 100) ** y, 0),
        "on_track": final + 1 >= future_target,
        "scenarios": scenarios,
        "path": path,
        "note": "Returns are assumptions, not promises. Mutual fund returns are not guaranteed.",
    }


def register_planner(app: FastAPI):
    @app.post("/planner/goal")
    def goal(req: GoalRequest):
        return plan_goal(req)
