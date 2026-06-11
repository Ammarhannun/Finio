"""Recurring-bill detection.

A transaction stream is a *bill* only when it looks like one on all three axes:
recurrence (happens >=3 times), stable amount (low coefficient of variation),
and regular timing (gaps cluster near a known billing period). Anything that
fails any test — one-off charges, variable restaurant/grocery spend, irregular
payments — is dropped. Transfers never reach here (they are excluded upstream).
"""

import pandas as pd

from config import (
    BILL_AMOUNT_CV_MAX,
    BILL_MIN_OCCURRENCES,
    BILL_PERIODS,
    BILL_PERIOD_TOLERANCE,
    BILL_REGULARITY_MIN_FRACTION,
    FLOW_EXPENSE,
)


def _coefficient_of_variation(values):
    """std / mean — a unitless measure of spread. Lower = more stable."""
    mean = values.mean()
    if mean == 0:
        return float("inf")
    return float(values.std(ddof=0) / mean)


def _classify_interval(dates):
    """Return (frequency_label, is_regular) for a series of purchase dates.

    Matches the median gap between purchases to the nearest known billing
    period, and checks the gaps are consistent enough to call it a schedule.
    Returns (None, False) when the timing isn't bill-like.
    """
    days = pd.to_datetime(pd.Series(list(dates))).dt.normalize().drop_duplicates().sort_values()
    gaps = days.diff().dropna().dt.days
    gaps = gaps[gaps > 0].astype("float64")
    if len(gaps) < BILL_MIN_OCCURRENCES - 1:
        return None, False

    median_gap = gaps.median()
    if median_gap <= 0:
        return None, False

    # Nearest known period by relative error.
    label, rel_err = min(
        ((name, abs(median_gap - days_) / days_) for name, days_ in BILL_PERIODS.items()),
        key=lambda pair: pair[1],
    )
    if rel_err > BILL_PERIOD_TOLERANCE:
        return None, False

    # Each gap should sit near an integer multiple of the period — this accepts
    # the odd skipped/retried charge (a 42-day gap on a fortnightly bill is 3x)
    # while still rejecting genuinely irregular spend.
    period = BILL_PERIODS[label]
    multiples = (gaps / period).round().clip(lower=1)
    residuals = (gaps - multiples * period).abs() / period
    on_schedule = (residuals <= BILL_PERIOD_TOLERANCE).mean()
    is_regular = on_schedule >= BILL_REGULARITY_MIN_FRACTION
    return label, is_regular


def detect_bills(df):
    if "flow" in df.columns:
        expenses = df[df["flow"] == FLOW_EXPENSE].copy()
    else:
        expenses = df[df["amount"] < 0].copy()
    if expenses.empty:
        return []
    expenses["amount_abs"] = expenses["amount"].abs()

    # Group by the cleaned merchant name when available so the same biller lines
    # up even if the raw description carries store/suburb noise.
    key = "merchant_clean" if "merchant_clean" in expenses.columns else "description"

    bills = []
    for merchant, group in expenses.groupby(key):
        if len(group) < BILL_MIN_OCCURRENCES:
            continue
        amounts = group["amount_abs"]
        if _coefficient_of_variation(amounts) > BILL_AMOUNT_CV_MAX:
            continue
        frequency, is_regular = _classify_interval(group["date"])
        if frequency is None or not is_regular:
            continue
        bills.append({
            "merchant": str(merchant),
            "amount": round(float(amounts.median()), 2),
            "count": int(len(group)),
            "frequency": frequency,
        })

    bills.sort(key=lambda b: b["amount"], reverse=True)
    return bills
