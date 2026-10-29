"""
Risk profile questionnaire.

  GET  /risk/questions   the 8 questions (id, text, options[1..4])
  POST /risk/score       {"answers": [a1..a8]} -> profile, score, suggested mix

Profiles match the values the client record already accepts:
CONSERVATIVE, MODERATE, AGGRESSIVE. Option 1 is always the most cautious answer.
This is a conversation starter, not a regulatory suitability assessment.
"""
from typing import List

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import allocation_api as al

QUESTIONS = [
    {"id": "age", "text": "Your age",
     "options": ["Over 60", "46 to 60", "31 to 45", "30 or younger"]},
    {"id": "horizon", "text": "When will you need most of this money?",
     "options": ["Within 3 years", "3 to 5 years", "5 to 10 years", "More than 10 years"]},
    {"id": "income", "text": "How stable is your income?",
     "options": ["Irregular or none", "Somewhat variable", "Steady salary or business", "Steady and growing, with other income"]},
    {"id": "dependents", "text": "Financial responsibilities",
     "options": ["Many dependents and loans", "Some dependents or a large loan", "A few commitments", "Few or none"]},
    {"id": "fall", "text": "Your investment falls 20% in a few months. You would:",
     "options": ["Sell everything", "Sell some to limit the loss", "Hold and wait", "Invest more at lower prices"]},
    {"id": "experience", "text": "Your experience with market investments",
     "options": ["None, only deposits", "Some mutual funds", "Several years in funds or shares", "Experienced across asset classes"]},
    {"id": "goal", "text": "What matters most?",
     "options": ["Not losing money", "Steady, modest returns", "Growth with some ups and downs", "Maximum growth, I can handle big swings"]},
    {"id": "emergency", "text": "Emergency money kept aside (months of expenses)",
     "options": ["None", "Under 3 months", "3 to 6 months", "More than 6 months"]},
]

PROFILES = {"CONSERVATIVE": "conservative", "MODERATE": "balanced", "AGGRESSIVE": "aggressive"}
LABEL = {"CONSERVATIVE": "Conservative", "MODERATE": "Moderate", "AGGRESSIVE": "Aggressive"}
ORDER = ["CONSERVATIVE", "MODERATE", "AGGRESSIVE"]


class ScoreRequest(BaseModel):
    answers: List[int]


def score_profile(answers: List[int]) -> dict:
    if len(answers) != len(QUESTIONS):
        raise HTTPException(400, f"Answer all {len(QUESTIONS)} questions")
    if any((not isinstance(a, int)) or a < 1 or a > 4 for a in answers):
        raise HTTPException(400, "Each answer must be between 1 and 4")
    total = sum(answers)
    profile = "CONSERVATIVE" if total <= 14 else "MODERATE" if total <= 23 else "AGGRESSIVE"
    ids = [q["id"] for q in QUESTIONS]
    a = dict(zip(ids, answers))
    capped = []
    if a["fall"] == 1 and ORDER.index(profile) > 0:
        profile = "CONSERVATIVE"
        capped.append("You would sell everything in a 20% fall, so the profile is held at Conservative.")
    if a["horizon"] == 1 and ORDER.index(profile) > 1:
        profile = "MODERATE"
        capped.append("Money needed within 3 years, so the profile is held at Moderate.")
    if a["emergency"] == 1 and ORDER.index(profile) > 1:
        profile = "MODERATE"
        capped.append("No emergency money set aside, so the profile is held at Moderate until that is built.")
    eq, hy = al.BASE_SPLIT[PROFILES[profile]]
    mix = {"equity": eq, "hybrid": hy, "debt": 100 - eq - hy}
    return {
        "profile": profile, "label": LABEL[profile], "score": total, "max_score": 4 * len(QUESTIONS),
        "mix": mix, "adjusted": capped,
        "note": "A starting point for the conversation, not a formal suitability assessment.",
    }


def register_risk_profile(app: FastAPI):
    @app.get("/risk/questions")
    def questions():
        return {"questions": QUESTIONS}

    @app.post("/risk/score")
    def score(req: ScoreRequest):
        return score_profile(req.answers)
