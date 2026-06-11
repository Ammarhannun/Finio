import pandas as pd

from config import DISCLAIMER, FLOW_EXPENSE
from modules.ai_coach import build_context
from modules.analytics import analyze
from modules.bank_parser import parse_bank_csv
from modules.bill_detector import detect_bills
from modules.budget_setter import suggest_budgets
from modules.categoriser import categorise_data
from modules.data_processor import add_flags, compute_metrics
from modules.history import build_snapshot
from modules.invest import invest_summary
from modules.period import available_months, filter_window, resolve_periods
from modules.personality import score_personality
from modules.savings_forecaster import forecast_goal, recommend_goal

DEFAULT_AGE = 22


def _category_spend(df):
    """Spend per category for expense-flow rows (used for the budget baseline)."""
    if df.empty:
        return {}
    expenses = df[df["flow"] == FLOW_EXPENSE].copy()
    if expenses.empty:
        return {}
    expenses["amount_abs"] = expenses["amount"].abs()
    return expenses.groupby("category")["amount_abs"].sum().to_dict()


def run_full_pipeline(
    csv_path,
    *,
    goal_amount=None,
    goal_date=None,
    age=None,
    overrides=None,
    budget_targets=None,
    period=None,
    period_anchor=None,
    period_start=None,
    period_end=None,
):
    # Parse, flag and categorise the FULL history once. Bills (which need
    # recurrence) and the budget baseline read from this; period-scoped numbers
    # read from the filtered slice below.
    full = parse_bank_csv(csv_path)
    full = add_flags(full, overrides=overrides)
    full = categorise_data(full)

    periods = resolve_periods(
        full, period=period, anchor=period_anchor,
        start=period_start, end=period_end,
    )
    df = filter_window(full, *periods["current"])
    prior_df = filter_window(full, *periods["prior"])

    metrics = compute_metrics(df)
    bills = detect_bills(full)

    # No goal supplied yet → recommend one from the user's actual numbers so the
    # dashboard has something sensible to show before they confirm/edit it.
    recommendation = recommend_goal(metrics)
    if goal_amount is None:
        goal_amount = recommendation["amount"]
    if goal_date is None:
        goal_date = recommendation["target_date"]
    if age is None:
        age = DEFAULT_AGE

    forecast = forecast_goal(df, goal_amount, goal_date)
    analysis = analyze(df, metrics, bills)
    baseline = _category_spend(prior_df) or None
    budgets = suggest_budgets(df, metrics, targets=budget_targets, baseline=baseline)
    invest = invest_summary(df, metrics, forecast, goal_amount, age=age)
    personality = score_personality(df, metrics, analysis, bills)
    snapshot = build_snapshot(df, metrics, analysis, bills)
    context = build_context(metrics, analysis, bills, budgets, invest, personality)

    tx = df[["date", "amount", "description", "category", "is_expense", "flow"]].copy()
    tx["date"] = tx["date"].dt.strftime("%Y-%m-%d")
    tx = tx.rename(columns={"description": "merchant"})
    transactions = tx.to_dict("records")

    return {
        "metrics": metrics,
        "analysis": analysis,
        "bills": bills,
        "forecast": forecast,
        "budgets": budgets,
        "invest": invest,
        "personality": personality,
        "snapshot": snapshot,
        "context": context,
        "transactions": transactions,
        "goal_recommendation": recommendation,
        "goal_used": {"amount": goal_amount, "target_date": goal_date, "age": age},
        "period": {
            "selected": periods["period"],
            "label": periods["label"],
            "start": periods["current"][0].strftime("%Y-%m-%d"),
            "end": periods["current"][1].strftime("%Y-%m-%d"),
            "available_months": available_months(full),
        },
        "disclaimer": DISCLAIMER,
    }


def _transactions_to_df(transactions):
    """Rebuild a minimal DataFrame from stored transaction rows."""
    from config import FLOW_EXPENSE, FLOW_INCOME, FLOW_TRANSFER, TRANSFERS_LABEL

    df = pd.DataFrame(transactions)
    df["date"] = pd.to_datetime(df["date"])
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce")

    # Restore flow so recompute matches the original analysis. Older snapshots
    # without a stored flow fall back to category/amount.
    if "flow" not in df.columns:
        df["flow"] = FLOW_EXPENSE
        df.loc[df.get("category") == TRANSFERS_LABEL, "flow"] = FLOW_TRANSFER
        df.loc[(df["flow"] != FLOW_TRANSFER) & (df["amount"] > 0), "flow"] = FLOW_INCOME
    df["is_transfer"] = df["flow"] == FLOW_TRANSFER
    return df


def recompute_for_goal(transactions, metrics, *, goal_amount, goal_date, age):
    """Recompute forecast + invest for a new goal, reusing stored transactions.

    Lets the user change their savings goal after analysis without re-uploading.
    """
    df = _transactions_to_df(transactions)
    forecast = forecast_goal(df, goal_amount, goal_date)
    invest = invest_summary(df, metrics, forecast, goal_amount, age=age)
    return {"forecast": forecast, "invest": invest}


if __name__ == "__main__":
    from config import SAMPLE_CSV

    result = run_full_pipeline(SAMPLE_CSV)
    print("risk:", result["analysis"]["risk_label"])
    print("bills:", len(result["bills"]))
    print("personality:", result["personality"]["personality_type"])
    print("transactions:", len(result["transactions"]))
