import pandas as pd
from config import (
    BILL_SKIP_KEYWORDS,
    BILL_KEYWORDS,
    BILL_RECURRING_CATEGORIES,
    BILL_SKIP_RECURRING_CATEGORIES,
)


def _is_transfer(description):
    text = str(description).upper()
    return any(keyword in text for keyword in BILL_SKIP_KEYWORDS)


def _has_bill_keyword(description):
    text = str(description).upper()
    return any(keyword in text for keyword in BILL_KEYWORDS)


def _amounts_similar(amounts, tolerance=0.15):
    if len(amounts) < 2:
        return True
    median = amounts.median()
    if median == 0:
        return False
    return ((amounts - median).abs() / median <= tolerance).all()


def detect_bills(df):
    expenses = df[df["amount"] < 0].copy()
    if expenses.empty:
        return []
    expenses = expenses[~expenses["description"].apply(_is_transfer)]
    if expenses.empty:
        return []
    expenses["amount_abs"] = expenses["amount"].abs()

    bills = []
    seen_merchants = set()

    def add_bill(merchant, amount, count, frequency):
        if merchant in seen_merchants:
            return
        bills.append({
            "merchant": merchant,
            "amount": round(amount, 2),
            "count": count,
            "frequency": frequency,
        })
        seen_merchants.add(merchant)

    has_category = "category" in expenses.columns

    for _, row in expenses.iterrows():
        if _has_bill_keyword(row["description"]):
            add_bill(
                row["description"],
                row["amount_abs"],
                1,
                "monthly",
            )

    if has_category:
        subs = expenses[expenses["category"] == "Subscriptions"]
        for description in subs["description"].unique():
            group = subs[subs["description"] == description]
            add_bill(
                description,
                group["amount_abs"].median(),
                len(group),
                "monthly" if len(group) == 1 else "recurring",
            )

    for description, group in expenses.groupby("description"):
        if len(group) < 2:
            continue
        if description in seen_merchants:
            continue
        if has_category:
            cat = group["category"].iloc[0]
            if cat in BILL_SKIP_RECURRING_CATEGORIES:
                continue
            if cat not in BILL_RECURRING_CATEGORIES:
                continue
        if not _amounts_similar(group["amount_abs"]):
            continue
        add_bill(
            description,
            group["amount_abs"].median(),
            len(group),
            "recurring",
        )

    return bills
