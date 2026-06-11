import pandas as pd
from config import DISCLAIMER, TRANSFERS_LABEL

def category_breakdown(df):
    expenses = df[df["amount"] < 0].copy()
    # Transfers are internal movements, not spend — keep them out of the breakdown.
    if "is_transfer" in expenses.columns:
        expenses = expenses[~expenses["is_transfer"]]
    expenses = expenses[expenses["category"] != TRANSFERS_LABEL]
    expenses["amount_abs"] = expenses["amount"].abs()
    totals = expenses.groupby("category")["amount_abs"].sum()
    total_spent = totals.sum()
    breakdown = []
    for category, amount in totals.sort_values(ascending=False).items():
        pct = round((amount / total_spent) * 100, 1) if total_spent else 0
        breakdown.append({
            "category": category,
            "amount": round(amount, 2),
            "pct": pct,
        })
    return breakdown

def detect_patterns(df, metrics, bills):
    patterns = []
    expenses = df[df["amount"] < 0].copy()
    expenses["amount_abs"] = expenses["amount"].abs()
    breakdown = category_breakdown(df)

    if breakdown:
        top = breakdown[0]
        patterns.append({
            "type": "top_category",
            "severity": "info",
            "message": f"{top['category']} is your biggest spend at {top['pct']}% (${top['amount']}).",
        })

    if "is_weekend" in expenses.columns:
        weekend = expenses.loc[expenses["is_weekend"], "amount_abs"].sum()
        weekday = expenses.loc[~expenses["is_weekend"], "amount_abs"].sum()
        if weekend > weekday:
            patterns.append({
                "type": "weekend_spend",
                "severity": "warning",
                "message": f"Weekend spend (${weekend:.2f}) beats weekdays (${weekday:.2f}).",
            })

    savings = metrics["total_income"] - metrics["total_spent"]
    if metrics["total_income"] > 0:
        save_pct = round((savings / metrics["total_income"]) * 100, 1)
        if save_pct < 10:
            patterns.append({
                "type": "low_savings",
                "severity": "warning",
                "message": f"You saved only {save_pct}% of income this period.",
            })
        elif save_pct >= 30:
            patterns.append({
                "type": "strong_savings",
                "severity": "info",
                "message": f"Strong savings rate: {save_pct}% of income kept.",
            })

    if bills:
        bill_total = sum(b["amount"] for b in bills)
        patterns.append({
            "type": "recurring_bills",
            "severity": "info",
            "message": f"{len(bills)} recurring bills totalling ${bill_total:.2f}/period.",
        })

    return patterns

RISK_LOW = 33
RISK_HIGH = 66


def risk_score(df, metrics, bills):
    score = 0
    income = metrics["total_income"]
    spent = metrics["total_spent"]

    if income > 0:
        spend_ratio = spent / income
        if spend_ratio > 0.9:
            score += 40
        elif spend_ratio > 0.7:
            score += 25
        elif spend_ratio > 0.5:
            score += 10

    breakdown = category_breakdown(df)
    if breakdown and breakdown[0]["pct"] > 35:
        score += 20

    if bills and income > 0:
        bill_total = sum(b["amount"] for b in bills)
        if (bill_total / income) > 0.15:
            score += 20

    if metrics["daily_burn_rate"] > 50:
        score += 10

    return min(score, 100)


def _risk_label(score):
    if score <= RISK_LOW:
        return "low"
    if score <= RISK_HIGH:
        return "medium"
    return "high"

def analyze(df, metrics, bills):
    score = risk_score(df, metrics, bills)
    return {
        "category_breakdown": category_breakdown(df),
        "patterns": detect_patterns(df, metrics, bills),
        "risk_score": score,
        "risk_label": _risk_label(score),
        "disclaimer": DISCLAIMER,
    }