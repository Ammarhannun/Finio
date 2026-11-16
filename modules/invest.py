from modules.spend_check import recent_statement_balance

from config import CRYPTO_OPTIONS, DISCLAIMER, ETF_OPTIONS, FLOW_EXPENSE

NEEDS_PCT = 50
WANTS_PCT = 30
SAVINGS_PCT = 20

# 50/30/20 needs the whole of spending accounted for, or the comparison lies.
# NEEDS is the explicit list; WANTS is deliberately "everything else" rather
# than a second fixed list. With two fixed lists, any category in neither
# silently vanished. "Housing & Rent" was missing, so rent, the largest need
# most people have, was left out of the split entirely ($1,800 of rent showed
# as $0 of needs). User-invented categories disappeared the same way.
#
# Everything not named here counts as a want, which is the safer default: this
# breakdown exists to surface discretionary spending, so an unrecognised
# category should not be quietly excused as essential.
NEEDS_CATEGORIES = ["Groceries", "Transport", "Health", "Housing & Rent", "Other"]

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
    # Refunds are positive expense-flow rows and offset spending.
    expenses["net_spend"] = -expenses["amount"]

    needs_spent = expenses.loc[
        expenses["category"].isin(NEEDS_CATEGORIES), "net_spend"
    ].sum()
    # Everything that isn't a need. including Entertainment and any category
    # the user invented. so no spending can fall out of the comparison.
    wants_spent = expenses.loc[
        ~expenses["category"].isin(NEEDS_CATEGORIES), "net_spend"
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
        # Difference from this example; the user may choose a different split.
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
            "Your uploaded history shows $1,000 in net savings. Check your actual savings account balance before treating this as an emergency buffer.",
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
    """Independent statement facts, never an investment eligibility decision.

    A percentage budget and a completed savings goal are personal choices,
    not prerequisites imposed by Finio. Keep the legacy can_invest flag false
    because a statement cannot establish suitability or available cash.
    """
    balance, balance_date = recent_statement_balance(metrics)
    current = float(forecast_result.get("current_saved", 0) or 0)
    target = float(forecast_result.get("target_amount", 0) or 0)
    net = float(metrics.get("net_saved", 0) or 0)
    checks = [
        {"id":"statement_balance", "label":"Recent statement balance", "passed":balance is not None,
         "current":float(balance) if balance is not None else None, "target":None, "unit":"aud",
         "fix":"Check your bank for your current available balance. A statement balance is historical."},
        {"id":"recorded_cashflow", "label":"Income minus spending in this window", "passed":net >= 0,
         "current":round(net,2), "target":None, "unit":"aud",
         "fix":"This is recorded cash flow, not money confirmed available to invest."},
        {"id":"savings_goal", "label":"Recorded movement toward your goal", "passed":target > 0 and current >= target,
         "current":round(current,2), "target":target or None, "unit":"aud",
         "fix":"Uses recorded net movement across your uploaded history, not the projected total or your savings balance."},
    ]
    return {"can_invest":False, "assessment":"not_assessed",
            "reason":"These are independent facts to review, not steps or a test of whether you should invest.",
            "priority":"review_your_plan", "checks":checks,
            "steps_done":sum(bool(c["passed"]) for c in checks), "steps_total":len(checks),
            "next_step":None, "ready_when":None, "balance_date":balance_date}


def etf_nudge(age=None):
    reason = "These are examples to research, not selections based on your age or statement. Compare diversification, fees, risk and your time horizon."
    return {
            "recommended": None,
            "options": ETF_OPTIONS,
        "reason": reason,
        "note": "Research fees and risk before investing.",
    }


def investment_menu(can_invest, age=None):
    """A plain language menu of where money can go, safest first.

    Goes beyond ETFs so the user sees the full picture: cash, index funds,
    crypto and super, each with its risk band. Crypto is listed for awareness
    only, with a heavy caveat, never as a recommendation. The whole menu is
    information only and remains visible regardless of the checklist result.
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

    etf = etf_nudge(age)

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
