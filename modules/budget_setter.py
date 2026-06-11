from config import CATEGORIES, DISCLAIMER
from modules.analytics import category_breakdown

HEADROOM_PCT = 10
FULL_MONTH_DAYS = 28


def _monthly_factor(metrics):
    days = metrics["date_range"]["days"]
    if days <= 0:
        return 1.0
    if days >= FULL_MONTH_DAYS:
        return 1.0
    return FULL_MONTH_DAYS / days


def suggest_budgets(df, metrics):
    factor = _monthly_factor(metrics)
    breakdown = category_breakdown(df)
    spent_by_category = {row["category"]: row["amount"] for row in breakdown}

    budgets = []
    for category in CATEGORIES:
        actual = spent_by_category.get(category, 0.0)
        monthly_actual = round(actual * factor, 2)
        suggested_limit = round(monthly_actual * (1 + HEADROOM_PCT / 100), 2)
        budgets.append({
            "category": category,
            "actual_spend": monthly_actual,
            "suggested_limit": suggested_limit,
            "headroom_pct": HEADROOM_PCT,
        })

    budgets.sort(key=lambda row: row["actual_spend"], reverse=True)
    return {
        "budgets": budgets,
        "month_factor": round(factor, 2),
        "disclaimer": DISCLAIMER,
    }
