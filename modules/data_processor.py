import pandas as pd

from config import TRANSFER_KEYWORDS


def _is_transfer(description):
    text = str(description).upper()
    return any(keyword in text for keyword in TRANSFER_KEYWORDS)


def add_flags(df):
    df = df.copy()

    df["is_transfer"] = df["description"].apply(_is_transfer)
    df["is_expense"] = (df["amount"] < 0) & ~df["is_transfer"]
    df["day_of_week"] = df["date"].dt.day_name()
    df["is_weekend"] = df["day_of_week"].isin(["Saturday", "Sunday"])
    df["month"] = df["date"].dt.to_period("M")
    return df

def compute_metrics(df):
    # Transfers (own-account / P2P) are internal movements, not income or spend,
    # so they are excluded from both totals.
    real = df[~df["is_transfer"]] if "is_transfer" in df.columns else df
    expenses = real.loc[real["amount"] < 0, "amount"]
    income = real.loc[real["amount"] > 0, "amount"]
    total_spent = abs(expenses.sum())
    total_income = income.sum()
    min_date = df["date"].min()
    max_date = df["date"].max()
    num_days = (max_date - min_date).days + 1

    daily_burn_rate = total_spent / num_days if num_days > 0 else 0

    return {
        "total_income": round(total_income, 2),
        "total_spent": round(total_spent, 2),
        "transaction_count": len(df),
        "daily_burn_rate": round(daily_burn_rate, 2),
        "date_range": {
            "start": min_date.strftime("%Y-%m-%d"),
            "end": max_date.strftime("%Y-%m-%d"),
            "days": num_days,
        },
    }

def process_transactions(df):
    df = add_flags(df)
    metrics = compute_metrics(df)
    return df, metrics
