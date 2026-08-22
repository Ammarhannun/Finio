import pandas as pd
import math

from config import CRYPTO_OPTIONS, DISCLAIMER, ETF_OPTIONS, FLOW_EXPENSE

NEEDS_PCT = 50
WANTS_PCT = 30
SAVINGS_PCT = 20

# 50/30/20 needs the whole of spending accounted for, or the comparison lies.
# NEEDS is the explicit list; WANTS is deliberately "everything else" rather
# than a second fixed list. With two fixed lists, any category in neither
# silently vanished — "Housing & Rent" was missing, so rent, the largest need
# most people have, was left out of the split entirely ($1,800 of rent showed
# as $0 of needs). User-invented categories disappeared the same way.
#
# Everything not named here counts as a want, which is the safer default: this
# breakdown exists to surface discretionary spending, so an unrecognised
# category should not be quietly excused as essential.
NEEDS_CATEGORIES = ["Groceries", "Transport", "Health", "Housing & Rent", "Other"]

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
    # Everything that isn't a need — including Entertainment and any category
    # the user invented — so no spending can fall out of the comparison.
    wants_spent = expenses.loc[
        ~expenses["category"].isin(NEEDS_CATEGORIES), "amount_abs"
    ].sum()
    actual_saved = metrics["net_saved"]

    needs_spent = round(needs_spent, 2)
    wants_spent = round(wants_spent, 2)
    actual_saved = round(actual_saved, 2)

    # Readable rows the UI can render and colour directly: `good` is True when
    # you're within target (spending under, or saving over) → green, else red.
    rows = [
        {
            "label": "Needs (50%)", "actual": needs_spent, "target": split["needs"],
            "difference": round(needs_spent - split["needs"], 2),
            "good": bool(needs_spent <= split["needs"]),
        },
        {
            "label": "Wants (30%)", "actual": wants_spent, "target": split["wants"],
            "difference": round(wants_spent - split["wants"], 2),
            "good": bool(wants_spent <= split["wants"]),
        },
        {
            "label": "Savings (20%)", "actual": actual_saved, "target": split["savings"],
            "difference": round(actual_saved - split["savings"], 2),
            "good": bool(actual_saved >= split["savings"]),
        },
    ]

    return {
        "rows": rows,
        "needs_spent": needs_spent,
        "wants_spent": wants_spent,
        "actual_saved": actual_saved,
        "needs_target": split["needs"],
        "wants_target": split["wants"],
        "savings_target": split["savings"],
        # Kept for invest_readiness; positive savings_gap means below target.
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
            "You've hit your first $1,000. Keep building your emergency buffer.",
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


def invest_readiness(metrics, compare, forecast_result, monthly_saved=None):
    """Are they ready to invest, and if not, WHAT is missing and WHEN will it
    clear?

    Returns a transparent checklist instead of a single verdict, so the user can
    see every gate, how close they are, and the first thing to fix. `saved` is
    the accumulated running total (forecast.current_saved), not one period's
    net — someone with months of savings shouldn't look broke because this
    month was quiet.
    """
    saved = forecast_result.get("current_saved")
    if saved is None:
        saved = metrics.get("net_saved", 0) or 0
    # Single source of truth (may be None when income is negligible → treat as 0%).
    # Cast out of numpy: numpy.bool_/float leak into the JSON response and
    # FastAPI cannot serialise them.
    saved = float(saved)
    savings_rate = float(metrics.get("savings_rate") or 0)
    gap = float(compare.get("savings_gap", 0) or 0)
    on_track = bool(forecast_result.get("on_track", False))

    checks = [
        {
            "id": "emergency_fund",
            "label": f"Cash buffer of ${MIN_BUFFER_TO_INVEST:,}",
            "passed": bool(saved >= MIN_BUFFER_TO_INVEST),
            "current": round(float(saved), 2),
            "target": float(MIN_BUFFER_TO_INVEST),
            "unit": "aud",
            "fix": f"Save ${max(MIN_BUFFER_TO_INVEST - saved, 0):,.0f} more before investing.",
        },
        {
            "id": "stabilise_spending",
            "label": f"Saving at least {MIN_SAVINGS_RATE_PCT}% of income",
            "passed": bool(savings_rate >= MIN_SAVINGS_RATE_PCT),
            "current": round(float(savings_rate), 1),
            "target": float(MIN_SAVINGS_RATE_PCT),
            "unit": "pct",
            "fix": "Trim your biggest category or a subscription to lift your savings rate.",
        },
        {
            "id": "close_savings_gap",
            "label": "Meeting the 20% savings target",
            "passed": bool(gap <= 0),
            # Show what they saved against the target, not the raw gap, so the
            # UI can render "current / target" consistently with the others.
            "current": round(float(compare.get("actual_saved", 0) or 0), 2),
            "target": round(float(compare.get("savings_target", 0) or 0), 2),
            "unit": "aud",
            "fix": f"You're ${gap:,.0f} short of the 20% savings target this period.",
        },
        {
            "id": "savings_goal",
            "label": "On track for your savings goal",
            "passed": on_track,
            "current": round(float(forecast_result.get("projected_total", 0) or 0), 2),
            "target": round(float(forecast_result.get("target_amount", 0) or 0), 2),
            "unit": "aud",
            "fix": "Raise your monthly saving or push the goal date back.",
        },
    ]

    blockers = [c for c in checks if not c["passed"]]
    can_invest = not blockers
    done = len(checks) - len(blockers)

    # When could the FIRST blocker clear? Only the cash buffer has an honest,
    # arithmetic answer (money needed ÷ money saved per month).
    ready_when = None
    if blockers and blockers[0]["id"] == "emergency_fund" and (monthly_saved or 0) > 0:
        months = math.ceil((MIN_BUFFER_TO_INVEST - saved) / monthly_saved)
        if 0 < months <= 60:
            ready_when = {
                "months": months,
                "text": (f"At about ${monthly_saved:,.0f} saved a month, you'd clear "
                         f"the buffer in roughly {months} month{'s' if months > 1 else ''}."),
            }

    if can_invest:
        reason = "You have savings headroom. Investing may be an option after research."
        priority = "consider_etfs"
    else:
        reason = blockers[0]["fix"]
        priority = blockers[0]["id"]

    return {
        "can_invest": can_invest,
        "reason": reason,
        "priority": priority,
        "checks": checks,
        "steps_done": done,
        "steps_total": len(checks),
        "next_step": blockers[0]["label"] if blockers else None,
        "ready_when": ready_when,
    }


def etf_nudge(age=None):
    if age is not None and age < 25:
        pick = "NDQ"
        reason = "Younger horizon with a growth tilt (higher risk)."
    elif age is not None and age >= 40:
        pick = "A200"
        reason = "Broader AU market, lower cost."
    else:
        pick = "VGS"
        reason = "Diversified global exposure, a common starter for Aussies."
    return {
        "recommended": pick,
        "options": ETF_OPTIONS,
        "reason": reason,
        "note": "Research fees and risk before investing.",
    }


def investment_menu(can_invest, age=None):
    """A plain language menu of where money can go, safest first.

    Goes beyond ETFs so the user sees the full picture: cash, index funds,
    crypto and super, each with its risk band. Crypto is listed for awareness
    only, with a heavy caveat, never as a recommendation. The whole menu is
    information only and unlocks once the buffer and savings are in place.
    """
    etf = etf_nudge(age)
    return [
        {
            "type": "Cash",
            "name": "High interest savings account",
            "risk": "Very low",
            "options": [],
            "note": "The right home for your emergency buffer and anything you need within a year.",
        },
        {
            "type": "ETFs",
            "name": "Low cost index funds",
            "risk": "Medium",
            "options": ETF_OPTIONS,
            "note": etf["reason"],
        },
        {
            "type": "Super",
            "name": "Extra super contributions",
            "risk": "Low to medium",
            "options": [],
            "note": "Tax friendly for the long run, but your money is locked away until retirement.",
        },
        {
            "type": "Crypto",
            "name": "Bitcoin or Ethereum",
            "risk": "Very high",
            "options": CRYPTO_OPTIONS,
            "note": "Extremely volatile and can fall fast. Only ever money you could lose, and only after your buffer and goals are sorted.",
        },
    ]


def invest_summary(df, metrics, forecast_result, target_amount, age=None,
                   monthly_saved=None):
    income = metrics["total_income"]
    split = split_income_503020(income)
    actual_vs_target = compare_to_actual(df, metrics, split)
    readiness = invest_readiness(metrics, actual_vs_target, forecast_result,
                                monthly_saved=monthly_saved)

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
        "options": investment_menu(readiness["can_invest"], age),
        "disclaimer": DISCLAIMER,
    }

