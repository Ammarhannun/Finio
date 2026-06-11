import pandas as pd
from config import DISCLAIMER, ETF_OPTIONS, FLOW_EXPENSE

NEEDS_PCT = 50
WANTS_PCT = 30
SAVINGS_PCT = 20

NEEDS_CATEGORIES = ["Groceries", "Transport", "Health", "Other"]
WANTS_CATEGORIES = ["Food & Dining", "Shopping", "Subscriptions"]

MIN_BUFFER_TO_INVEST = 500
MIN_SAVINGS_RATE_PCT = 10


def split_income_503020(monthly_income):
    return {
        "needs": round(monthly_income * NEEDS_PCT / 100, 2),
        "wants": round(monthly_income * WANTS_PCT / 100, 2),
        "savings": round(monthly_income * SAVINGS_PCT / 100, 2),
        "income": round(monthly_income, 2),
    }


def compare_to_actual(df, metrics, split):
    if "flow" in df.columns:
        expenses = df[df["flow"] == FLOW_EXPENSE].copy()
    else:
        expenses = df[df["amount"] < 0].copy()
    expenses["amount_abs"] = expenses["amount"].abs()

    needs_spent = expenses.loc[
        expenses["category"].isin(NEEDS_CATEGORIES), "amount_abs"
    ].sum()
    wants_spent = expenses.loc[
        expenses["category"].isin(WANTS_CATEGORIES), "amount_abs"
    ].sum()
    actual_saved = metrics["net_saved"]

    return {
        "needs_spent": round(needs_spent, 2),
        "wants_spent": round(wants_spent, 2),
        "actual_saved": round(actual_saved, 2),
        "needs_target": split["needs"],
        "wants_target": split["wants"],
        "savings_target": split["savings"],
        "needs_over": round(needs_spent - split["needs"], 2),
        "wants_over": round(wants_spent - split["wants"], 2),
        "savings_gap": round(split["savings"] - actual_saved, 2),
    }


def goal_progress(forecast_result, target_amount):
    current = forecast_result["current_saved"]
    pct = round((current / target_amount) * 100, 1) if target_amount else 0
    return {
        "current_saved": current,
        "target_amount": target_amount,
        "pct_complete": min(pct, 100),
        "on_track": forecast_result["on_track"],
        "projected_total": forecast_result["projected_total"],
    }


def first_1000_plan(current_saved):
    target = 1000
    remaining = max(target - current_saved, 0)
    if remaining == 0:
        steps = [
            "You've hit your first $1,000 — keep building your emergency buffer.",
            "Only consider investing money you won't need for bills or emergencies.",
            "Research fees and risk before choosing an ETF.",
        ]
    else:
        steps = [
            f"Save ${remaining:.2f} more to reach $1,000.",
            "Move savings to a separate account away from everyday spending.",
            "Automate a small transfer each payday before you spend.",
        ]
    return {
        "target": target,
        "current_saved": round(current_saved, 2),
        "remaining": round(remaining, 2),
        "steps": steps,
    }


def invest_readiness(metrics, compare, forecast_result):
    saved = metrics["net_saved"]
    # Single source of truth (may be None when income is negligible → treat as 0%).
    savings_rate = metrics.get("savings_rate") or 0

    if saved < MIN_BUFFER_TO_INVEST:
        return {
            "can_invest": False,
            "reason": f"Build a cash buffer first (aim for at least ${MIN_BUFFER_TO_INVEST}).",
            "priority": "emergency_fund",
        }
    if savings_rate < MIN_SAVINGS_RATE_PCT:
        return {
            "can_invest": False,
            "reason": (
                f"You're saving less than {MIN_SAVINGS_RATE_PCT}% of income — "
                "focus on spending and bills first."
            ),
            "priority": "stabilise_spending",
        }
    if compare["savings_gap"] > 0:
        return {
            "can_invest": False,
            "reason": "You're below the 20% savings target for this period — prioritise saving.",
            "priority": "close_savings_gap",
        }
    if not forecast_result.get("on_track", False):
        return {
            "can_invest": False,
            "reason": "You're not on track for your savings goal yet.",
            "priority": "savings_goal",
        }
    return {
        "can_invest": True,
        "reason": "You have savings headroom — investing may be an option after research.",
        "priority": "consider_etfs",
    }


def etf_nudge(age=None):
    if age is not None and age < 25:
        pick = "NDQ"
        reason = "Younger horizon — growth-focused (higher risk)."
    elif age is not None and age >= 40:
        pick = "A200"
        reason = "Broader AU market, lower cost."
    else:
        pick = "VGS"
        reason = "Diversified global exposure — common starter for Aussies."
    return {
        "recommended": pick,
        "options": ETF_OPTIONS,
        "reason": reason,
        "note": "Research fees and risk before investing.",
    }


def invest_summary(df, metrics, forecast_result, target_amount, age=None):
    income = metrics["total_income"]
    split = split_income_503020(income)
    actual_vs_target = compare_to_actual(df, metrics, split)
    readiness = invest_readiness(metrics, actual_vs_target, forecast_result)

    if readiness["can_invest"]:
        etf = etf_nudge(age)
    else:
        etf = {
            "recommended": None,
            "options": ETF_OPTIONS,
            "reason": readiness["reason"],
            "note": f"Focus on saving first ({readiness['priority']}).",
        }

    return {
        "split_503020": split,
        "actual_vs_target": actual_vs_target,
        "readiness": readiness,
        "goal": goal_progress(forecast_result, target_amount),
        "first_1000": first_1000_plan(forecast_result["current_saved"]),
        "etf": etf,
        "disclaimer": DISCLAIMER,
    }

