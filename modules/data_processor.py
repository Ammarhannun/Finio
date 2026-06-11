import pandas as pd

from config import (
    FLOW_EXPENSE,
    FLOW_INCOME,
    FLOW_TRANSFER,
    MIN_INCOME_FOR_RATE,
    TRANSFER_KEYWORDS,
)


def _is_transfer(description):
    text = str(description).upper()
    return any(keyword in text for keyword in TRANSFER_KEYWORDS)


def _default_flow(row):
    if row["is_transfer"]:
        return FLOW_TRANSFER
    return FLOW_INCOME if row["amount"] > 0 else FLOW_EXPENSE


def apply_flow_overrides(df, overrides):
    """Let the user (or coach) reclassify transactions.

    `overrides` is a list of {match, flow} rules; any row whose UPPERCASED
    description contains `match` gets its `flow` set to `flow` (one of
    income/expense/transfer). This is how 'count my transfers from Laith as
    income' is honoured without touching the parser or hard-coding rules.
    """
    if not overrides:
        return df
    desc = df["description"].astype(str).str.upper()
    for rule in overrides:
        match = str(rule.get("match", "")).upper().strip()
        flow = rule.get("flow")
        if not match or flow not in (FLOW_INCOME, FLOW_EXPENSE, FLOW_TRANSFER):
            continue
        df.loc[desc.str.contains(match, regex=False), "flow"] = flow
    return df


def add_flags(df, overrides=None):
    df = df.copy()

    df["is_transfer"] = df["description"].apply(_is_transfer)
    df["flow"] = df.apply(_default_flow, axis=1)
    df = apply_flow_overrides(df, overrides)

    # Keep is_transfer / is_expense consistent with the (possibly overridden) flow
    # so every downstream module agrees on what counts.
    df["is_transfer"] = df["flow"] == FLOW_TRANSFER
    df["is_expense"] = df["flow"] == FLOW_EXPENSE
    df["day_of_week"] = df["date"].dt.day_name()
    df["is_weekend"] = df["day_of_week"].isin(["Saturday", "Sunday"])
    df["month"] = df["date"].dt.to_period("M")
    return df


def savings_rate(net_saved, total_income):
    """Single definition of savings rate: net_saved / income, as a percentage.

    Returns None when real income is below MIN_INCOME_FOR_RATE — a percentage
    of near-zero income is noise, not signal.
    """
    if total_income is None or total_income < MIN_INCOME_FOR_RATE:
        return None
    return round(net_saved / total_income * 100, 1)


def compute_metrics(df):
    # Transfers (own-account / P2P) are internal movements, not income or spend.
    income_rows = df.loc[df["flow"] == FLOW_INCOME, "amount"]
    expense_rows = df.loc[df["flow"] == FLOW_EXPENSE, "amount"]
    total_income = income_rows.sum()
    total_spent = abs(expense_rows.sum())
    net_saved = total_income - total_spent

    min_date = df["date"].min()
    max_date = df["date"].max()
    num_days = (max_date - min_date).days + 1
    daily_burn_rate = total_spent / num_days if num_days > 0 else 0

    return {
        "total_income": round(total_income, 2),
        "total_spent": round(total_spent, 2),
        "net_saved": round(net_saved, 2),
        "savings_rate": savings_rate(net_saved, total_income),
        "transaction_count": len(df),
        "daily_burn_rate": round(daily_burn_rate, 2),
        "date_range": {
            "start": min_date.strftime("%Y-%m-%d"),
            "end": max_date.strftime("%Y-%m-%d"),
            "days": num_days,
        },
    }

def process_transactions(df, overrides=None):
    df = add_flags(df, overrides=overrides)
    metrics = compute_metrics(df)
    return df, metrics
