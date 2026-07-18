import pandas as pd
from config import DISCLAIMER, FLOW_EXPENSE, FLOW_INCOME, TRANSFERS_LABEL


def compute_averages(df):
    """What the user USUALLY earns/spends/saves per day, week and month,
    averaged over their whole history — plus a month-by-month series for the
    spending chart, and their top merchants.

    This powers the dashboard's Day/Week/Month view: not "what happened in the
    latest slice" but "what a typical day/week/month looks like for you".
    """
    from modules.savings_forecaster import _complete_months

    out = {"daily": None, "weekly": None, "monthly": None,
           "months_used": 0, "spend_series": [], "top_merchants": []}
    if df is None or df.empty:
        return out
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    exp = df[df["flow"] == FLOW_EXPENSE]
    inc = df[df["flow"] == FLOW_INCOME]

    days = (df["date"].max() - df["date"].min()).days + 1
    total_spent = float(-exp["amount"].sum()) if not exp.empty else 0.0
    total_income = float(inc["amount"].sum()) if not inc.empty else 0.0

    def block(spent, income):
        return {"spent": round(spent, 2), "income": round(income, 2),
                "saved": round(income - spent, 2)}

    if days > 0:
        d_sp, d_in = total_spent / days, total_income / days
        out["daily"] = block(d_sp, d_in)
        out["weekly"] = block(d_sp * 7, d_in * 7)

    # Monthly average from COMPLETE months when we have them (a real "usual
    # month"); otherwise scale the daily rate.
    m_spent = exp.set_index("date")["amount"].resample("MS").sum().mul(-1) if not exp.empty else pd.Series(dtype=float)
    m_income = inc.set_index("date")["amount"].resample("MS").sum() if not inc.empty else pd.Series(dtype=float)
    m_spent_c = _complete_months(m_spent, df["date"]) if len(m_spent) else m_spent
    if len(m_spent_c) >= 1 and days >= 28:
        m_income_c = _complete_months(m_income, df["date"]) if len(m_income) else m_income
        out["monthly"] = block(
            float(m_spent_c.mean()),
            float(m_income_c.mean()) if len(m_income_c) else 0.0,
        )
        out["months_used"] = int(len(m_spent_c))
    elif days > 0:
        out["monthly"] = block(total_spent / days * 30.44, total_income / days * 30.44)

    # Month-by-month spending (every month incl. partial edges — it's a chart,
    # the shape matters more than perfection; saved uses income of same month).
    if len(m_spent):
        inc_by_m = m_income.to_dict() if len(m_income) else {}
        for ts, spent in m_spent.items():
            income = float(inc_by_m.get(ts, 0.0))
            out["spend_series"].append({
                "month": ts.strftime("%Y-%m"),
                "spent": round(float(spent), 2),
                "saved": round(income - float(spent), 2),
            })

    # Where the money actually goes: top merchants by total spend.
    if not exp.empty:
        name_col = "merchant_clean" if "merchant_clean" in exp.columns else "description"
        top = (-exp.groupby(exp[name_col].astype(str))["amount"].sum()).sort_values(ascending=False)
        out["top_merchants"] = [
            {"merchant": m, "total": round(float(v), 2)}
            for m, v in top.head(5).items() if v > 0
        ]
    return out


def category_breakdown(df):
    # Spend = expense-flow rows only; transfers/income never enter the breakdown.
    if "flow" in df.columns:
        expenses = df[df["flow"] == FLOW_EXPENSE].copy()
    else:
        expenses = df[df["amount"] < 0].copy()
    expenses = expenses[expenses["category"] != TRANSFERS_LABEL]
    # Negate, don't abs(): refunds (positive expenses) offset their category.
    expenses["amount_abs"] = -expenses["amount"]
    totals = expenses.groupby("category")["amount_abs"].sum()
    totals = totals[totals > 0]
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
    if "flow" in df.columns:
        expenses = df[df["flow"] == FLOW_EXPENSE].copy()
    else:
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

    save_pct = metrics.get("savings_rate")
    if save_pct is not None:
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