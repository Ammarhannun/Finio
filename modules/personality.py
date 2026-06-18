from config import DISCLAIMER
from modules.analytics import category_breakdown


def _category_pct(df, category):
    for row in category_breakdown(df):
        if row["category"] == category:
            return row["pct"]
    return 0.0


def _build_action_plan(personality_type, analysis, metrics):
    patterns = analysis.get("patterns", [])
    top_msg = patterns[0]["message"] if patterns else "Review your spending patterns."

    plans = {
        "Planner": [
            f"Keep your strong savings habit (${metrics['net_saved']:.0f} saved this period).",
            "Set a clear goal amount and date in the Invest tab.",
            top_msg,
        ],
        "Spender": [
            "Pick one discretionary category to trim by 10% next month.",
            "Use Spend Check before purchases over $100.",
            top_msg,
        ],
        "Subscriber": [
            "List all recurring bills and cancel one you rarely use.",
            "Compare subscription total to your income, aim under 10%.",
            top_msg,
        ],
        "Balanced": [
            "Track spending weekly so small habits don't drift.",
            "Aim to keep needs under 50% and wants under 30% of income.",
            top_msg,
        ],
    }
    return plans.get(personality_type, plans["Balanced"])


def score_personality(df, metrics, analysis, bills):
    saved = metrics["net_saved"]
    # Single source of truth (data_processor.savings_rate); may be None.
    savings_rate = metrics.get("savings_rate")
    rate_for_scoring = savings_rate if savings_rate is not None else 0

    food_pct = _category_pct(df, "Food & Dining")
    shop_pct = _category_pct(df, "Shopping")
    subs_pct = _category_pct(df, "Subscriptions")

    bill_total = sum(b["amount"] for b in bills) if bills else 0
    bill_share = (bill_total / metrics["total_income"] * 100) if metrics["total_income"] else 0

    scores = {
        "planner": 0,
        "spender": 0,
        "subscriber": 0,
    }

    if rate_for_scoring >= 30:
        scores["planner"] += 3
    elif rate_for_scoring >= 15:
        scores["planner"] += 1

    if food_pct + shop_pct >= 35:
        scores["spender"] += 3
    elif food_pct + shop_pct >= 25:
        scores["spender"] += 1

    if subs_pct >= 8 or bill_share >= 12 or len(bills) >= 4:
        scores["subscriber"] += 3
    elif subs_pct >= 5 or len(bills) >= 2:
        scores["subscriber"] += 1

    max_score = max(scores.values())
    if max_score == 0:
        personality_type = "Balanced"
    else:
        personality_type = max(scores, key=scores.get).capitalize()

    return {
        "personality_type": personality_type,
        "scores": scores,
        "savings_rate": savings_rate,
        "action_plan": _build_action_plan(personality_type, analysis, metrics),
        "disclaimer": DISCLAIMER,
    }