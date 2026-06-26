import hashlib

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


def tx_key(date, merchant, amount, occ=0):
    """A stable identity for one transaction: hash of its date, merchant and
    amount (plus an occurrence index when the same triple repeats, so two
    identical charges on one day get DISTINCT keys instead of colliding).
    Lets the user reclassify a SINGLE row precisely. Stable across period
    re-slicing because it's derived from the unchanging stored fields.

    occ=0 keeps the original hash, so previously-saved single-row overrides
    still match; only genuine duplicates (occ>0) get a disambiguated key.
    """
    d = pd.Timestamp(date).strftime("%Y-%m-%d")
    m = str(merchant).strip().upper()
    a = f"{float(amount):.2f}"
    base = f"{d}|{m}|{a}"
    if occ:
        base = f"{base}|{occ}"
    return hashlib.md5(base.encode()).hexdigest()[:16]


def key_series(df):
    """tx_key for every row of a DataFrame (cols: date, description, amount).
    Duplicates of the same (date, merchant, amount) get an incrementing occ in
    row order, so identical transactions resolve to different keys."""
    d = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    m = df["description"].astype(str).str.strip().str.upper()
    a = df["amount"].astype(float).map(lambda x: f"{x:.2f}")
    base = d + "|" + m + "|" + a
    occ = base.groupby(base).cumcount()
    return [
        tx_key(dd, mm, aa, o)
        for dd, mm, aa, o in zip(df["date"], df["description"], df["amount"], occ)
    ]


def add_tx_key(df):
    """Add the per-row tx_key column (idempotent)."""
    df["tx_key"] = key_series(df)
    return df


def _override_mask(df, rule):
    """Which rows a single override rule targets.

    A rule either pins one transaction by `tx_key` (precise, single-row edits)
    or matches every row whose UPPERCASED description contains `match` (the
    original text-rule behaviour, e.g. 'treat PAYID LAITH as income')."""
    key = rule.get("tx_key")
    if key:
        if "tx_key" not in df.columns:
            add_tx_key(df)
        return df["tx_key"] == key
    match = str(rule.get("match", "")).upper().strip()
    if not match:
        return pd.Series(False, index=df.index)
    return df["description"].astype(str).str.upper().str.contains(match, regex=False)


def apply_flow_overrides(df, overrides):
    """Apply the FLOW half of the user's reclassification rules.

    `overrides` is a list of rules, each `{match?|tx_key?, flow?, category?}`.
    Here we only honour `flow` (one of income/expense/transfer). Run this BEFORE
    categorisation so a row flipped to/from a transfer is categorised correctly.
    Category changes are applied separately by apply_category_overrides.
    """
    if not overrides:
        return df
    for rule in overrides:
        flow = rule.get("flow")
        if flow not in (FLOW_INCOME, FLOW_EXPENSE, FLOW_TRANSFER):
            continue
        df.loc[_override_mask(df, rule), "flow"] = flow
    return df


def apply_category_overrides(df, overrides):
    """Apply the CATEGORY half of the user's reclassification rules.

    Run this AFTER categorisation so a user-chosen category (including their own
    custom categories) wins over the rules/ML guess. A rule with no `category`
    is ignored here (it was a flow-only rule)."""
    if not overrides or "category" not in df.columns:
        return df
    for rule in overrides:
        category = rule.get("category")
        if not category:
            continue
        df.loc[_override_mask(df, rule), "category"] = category
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

    # Real account balance at the end of the window, when the statement carried a
    # running-balance column. Lets spend-check reason about actual money on hand
    # instead of period net savings. None when no balance data (e.g. re-sliced
    # stored rows, which don't persist balance) → callers fall back to net_saved.
    latest_balance = None
    if "balance" in df.columns:
        bal = pd.to_numeric(df.sort_values("date")["balance"], errors="coerce").dropna()
        if not bal.empty:
            latest_balance = round(float(bal.iloc[-1]), 2)

    return {
        "total_income": round(total_income, 2),
        "total_spent": round(total_spent, 2),
        "net_saved": round(net_saved, 2),
        "savings_rate": savings_rate(net_saved, total_income),
        "latest_balance": latest_balance,
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
