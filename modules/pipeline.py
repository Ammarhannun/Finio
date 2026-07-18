import pandas as pd

from config import DISCLAIMER, FLOW_EXPENSE
from modules.ai_coach import build_context
from modules.analytics import analyze, compute_averages
from modules.anomaly import detect_anomalies
from modules.bank_parser import parse_bank_csv
from modules.bill_detector import detect_bills
from modules.budget_setter import suggest_budgets
from modules.categoriser import categorise_data
from modules.data_processor import (
    add_flags,
    apply_category_overrides,
    compute_metrics,
)
from modules.history import build_snapshot
from modules.invest import invest_summary
from modules.period import available_months, filter_window, resolve_periods
from modules.personality import score_personality
from modules.savings_forecaster import (
    forecast_goal,
    forecast_spending,
    monthly_net_average,
    recommend_goal,
)

DEFAULT_AGE = 22


def _category_spend(df):
    """Spend per category for expense-flow rows (used for the budget baseline)."""
    if df.empty:
        return {}
    expenses = df[df["flow"] == FLOW_EXPENSE].copy()
    if expenses.empty:
        return {}
    # Negate, don't abs(): refunds (positive expenses) offset their category.
    expenses["spend"] = -expenses["amount"]
    out = expenses.groupby("category")["spend"].sum()
    return out[out > 0].to_dict()


def _records(df):
    """Serialise a transaction DataFrame to JSON-safe rows (merchant = description).

    Each row carries a stable `key` (tx_key) so the frontend can ask to
    reclassify exactly that transaction via an override rule. Uses the shared
    occurrence-aware key_series so duplicate transactions get distinct keys."""
    from modules.data_processor import key_series

    tx = df[["date", "amount", "description", "category", "is_expense", "flow"]].copy()
    tx["key"] = key_series(tx)
    tx["date"] = tx["date"].dt.strftime("%Y-%m-%d")
    tx = tx.rename(columns={"description": "merchant"})
    return tx.to_dict("records")


def analyze_window(
    full,
    *,
    goal_amount=None,
    goal_date=None,
    age=None,
    budget_targets=None,
    period=None,
    period_anchor=None,
    period_start=None,
    period_end=None,
):
    """Compute every dashboard number for ONE time slice of an already
    parsed+flagged+categorised history. Shared by fresh uploads and by
    re-slicing stored transactions, so a period change is consistent everywhere.
    """
    periods = resolve_periods(
        full, period=period, anchor=period_anchor,
        start=period_start, end=period_end,
    )
    df = filter_window(full, *periods["current"])
    prior_df = filter_window(full, *periods["prior"])

    metrics = compute_metrics(df)
    bills = detect_bills(full)
    # Unusual charges are judged against the user's WHOLE history (stable baseline).
    anomalies = detect_anomalies(full)
    # "What I usually spend" per day/week/month + chart series + top merchants.
    averages = compute_averages(full)

    # No goal supplied yet → recommend one from the user's actual numbers so the
    # dashboard has something sensible to show before they confirm/edit it. Use
    # the robust monthly average over the WHOLE history (not just this slice) so
    # the suggested amount doesn't swing with a daily/weekly view.
    recommendation = recommend_goal(metrics, monthly_saved=monthly_net_average(full))
    if goal_amount is None:
        goal_amount = recommendation["amount"]
    if goal_date is None:
        goal_date = recommendation["target_date"]
    if age is None:
        age = DEFAULT_AGE

    # Forecast the goal from the WHOLE history, not just this slice, so the
    # running total and monthly trend stay realistic on a daily/weekly view.
    forecast = forecast_goal(full, goal_amount, goal_date)
    spend_forecast = forecast_spending(full, metrics)
    analysis = analyze(df, metrics, bills)
    baseline = _category_spend(prior_df) or None
    budgets = suggest_budgets(df, metrics, targets=budget_targets, baseline=baseline)
    invest = invest_summary(df, metrics, forecast, goal_amount, age=age)
    personality = score_personality(df, metrics, analysis, bills)
    snapshot = build_snapshot(df, metrics, analysis, bills)
    context = build_context(metrics, analysis, bills, budgets, invest, personality)

    return {
        "metrics": metrics,
        "analysis": analysis,
        "bills": bills,
        "anomalies": anomalies,
        "averages": averages,
        "forecast": forecast,
        "spend_forecast": spend_forecast,
        "budgets": budgets,
        "invest": invest,
        "personality": personality,
        "snapshot": snapshot,
        "context": context,
        "transactions": _records(df),
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


def run_full_pipeline(
    csv_path,
    *,
    goal_amount=None,
    goal_date=None,
    age=None,
    overrides=None,
    user_examples=None,
    llm_cache=None,
    custom_categories=None,
    budget_targets=None,
    period=None,
    period_anchor=None,
    period_start=None,
    period_end=None,
):
    # Parse, flag and categorise the FULL history once. Bills (which need
    # recurrence) and the budget baseline read from this; period-scoped numbers
    # read from the filtered slice inside analyze_window.
    full = parse_bank_csv(csv_path)
    full = add_flags(full, overrides=overrides)
    # user_examples (past corrections) personalise the fallback model; llm_cache
    # holds previous LLM merchant classifications so only NEW merchants cost a
    # call. llm_meta collects the merged cache for persistence.
    from config import CATEGORIES as _CATS
    cats = list(_CATS) + [c for c in (custom_categories or []) if c not in _CATS]
    llm_meta = {}
    full = categorise_data(full, user_examples=user_examples,
                           llm_cache=llm_cache, categories=cats, llm_meta=llm_meta)
    # Category overrides land AFTER categorisation so a user-chosen category
    # (incl. their custom ones) beats the rules/ML guess.
    full = apply_category_overrides(full, overrides)

    result = analyze_window(
        full,
        goal_amount=goal_amount, goal_date=goal_date, age=age,
        budget_targets=budget_targets, period=period,
        period_anchor=period_anchor, period_start=period_start, period_end=period_end,
    )
    # Persist the WHOLE history (not just the current slice) so the dashboard can
    # re-slice into other periods later without a re-upload.
    result["all_transactions"] = _records(full)
    # LLM merchant cache + the short "help me categorise" quiz.
    from modules.llm_categoriser import build_questions
    result["llm_categories"] = llm_meta.get("cache", llm_cache or {})
    result["pending_questions"] = build_questions(
        full, result["llm_categories"], overrides=overrides
    )
    return result


def analyze_stored(
    transactions,
    *,
    goal_amount=None,
    goal_date=None,
    age=None,
    overrides=None,
    period=None,
    period_anchor=None,
    period_start=None,
    period_end=None,
):
    """Re-run analyze_window over a user's stored full history for a new period.

    `overrides` (a list of {match, flow} rules) lets the user reclassify
    transactions, e.g. mark a regular transfer as income, before the re-slice.
    """
    full = _restore_full_df(transactions)
    if overrides:
        from config import FLOW_EXPENSE, FLOW_TRANSFER
        from modules.data_processor import (
            apply_category_overrides,
            apply_flow_overrides,
        )

        # Apply only the user's rules on top of the restored flow, then keep the
        # derived flags in sync — don't rebuild flow from scratch (that would
        # lose the transfer classification decided at upload time). Category
        # overrides apply to the already-stored categories.
        full = apply_flow_overrides(full, overrides)
        full = apply_category_overrides(full, overrides)
        full["is_transfer"] = full["flow"] == FLOW_TRANSFER
        full["is_expense"] = full["flow"] == FLOW_EXPENSE
    return analyze_window(
        full,
        goal_amount=goal_amount, goal_date=goal_date, age=age, period=period,
        period_anchor=period_anchor, period_start=period_start, period_end=period_end,
    )


def _restore_flow(df):
    """Reconstruct the flow column from stored rows (DB doesn't persist flow)."""
    from config import FLOW_EXPENSE, FLOW_INCOME, FLOW_TRANSFER, TRANSFERS_LABEL

    if "flow" not in df.columns:
        df["flow"] = FLOW_EXPENSE
        df.loc[df.get("category") == TRANSFERS_LABEL, "flow"] = FLOW_TRANSFER
        df.loc[(df["flow"] != FLOW_TRANSFER) & (df["amount"] > 0), "flow"] = FLOW_INCOME
    df["is_transfer"] = df["flow"] == FLOW_TRANSFER
    df["is_expense"] = df["flow"] == FLOW_EXPENSE
    return df


def _transactions_to_df(transactions):
    """Rebuild a minimal DataFrame from stored transaction rows (goal recompute)."""
    df = pd.DataFrame(transactions)
    df["date"] = pd.to_datetime(df["date"])
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
    return _restore_flow(df)


def _restore_full_df(transactions):
    """Rebuild a full, analysis-ready DataFrame from stored rows.

    Adds back the flow + calendar flags every downstream module expects, so a
    re-slice produces the same shape as a fresh upload. Merchant names are
    already cleaned, so merchant_clean mirrors the stored merchant.
    """
    df = pd.DataFrame(transactions)
    df["date"] = pd.to_datetime(df["date"])
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
    df = df.rename(columns={"merchant": "description"})
    if "description" not in df.columns:
        df["description"] = ""
    if "category" not in df.columns:
        df["category"] = None

    df = _restore_flow(df)
    df["merchant_clean"] = df["description"]
    df["day_of_week"] = df["date"].dt.day_name()
    df["is_weekend"] = df["day_of_week"].isin(["Saturday", "Sunday"])
    df["month"] = df["date"].dt.to_period("M")
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
