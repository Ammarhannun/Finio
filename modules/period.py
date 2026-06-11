"""Time-period selection.

Resolves a requested period (daily / weekly / monthly / custom / all) into a
concrete [start, end] window plus the matching prior window (used as the budget
baseline). The pipeline applies this at the very top, so every downstream
calculation describes one consistent slice of time — which also fixes uploads
of long histories collapsing to a single fixed span.
"""

import pandas as pd

from config import DEFAULT_PERIOD


def _month_bounds(ts):
    start = ts.replace(day=1).normalize()
    end = (start + pd.offsets.MonthEnd(0)).normalize()
    return start, end


def resolve_periods(df, period=None, anchor=None, start=None, end=None):
    """Return {'current': (start,end), 'prior': (start,end|None), 'period', 'label'}.

    `anchor` is the reference date a relative period hangs off (defaults to the
    latest transaction). `start`/`end` are only used for the 'custom' period.
    """
    period = (period or DEFAULT_PERIOD).lower()
    dates = pd.to_datetime(df["date"])
    min_date, max_date = dates.min().normalize(), dates.max().normalize()
    anchor = pd.to_datetime(anchor).normalize() if anchor else max_date

    if period == "custom" and start and end:
        cs, ce = pd.to_datetime(start).normalize(), pd.to_datetime(end).normalize()
        span = ce - cs
        pe = cs - pd.Timedelta(days=1)
        ps = pe - span
        label = f"{cs.date()} to {ce.date()}"
    elif period == "all":
        cs, ce, ps, pe = min_date, max_date, None, None
        label = "all time"
    elif period == "daily":
        cs = ce = anchor
        ps = pe = anchor - pd.Timedelta(days=1)
        label = str(anchor.date())
    elif period == "weekly":
        cs = anchor - pd.Timedelta(days=int(anchor.weekday()))  # Monday
        ce = cs + pd.Timedelta(days=6)
        ps, pe = cs - pd.Timedelta(days=7), cs - pd.Timedelta(days=1)
        label = f"week of {cs.date()}"
    else:  # monthly (default)
        cs, ce = _month_bounds(anchor)
        ps, pe = _month_bounds(cs - pd.Timedelta(days=1))
        label = cs.strftime("%Y-%m")
        period = "monthly"

    return {"current": (cs, ce), "prior": (ps, pe), "period": period, "label": label}


def filter_window(df, start, end):
    """Rows whose date falls in [start, end] (inclusive). No-op if either is None."""
    if start is None or end is None:
        return df
    dates = pd.to_datetime(df["date"])
    return df[(dates >= start) & (dates <= end)].copy()


def available_months(df):
    """Distinct YYYY-MM present in the data, for a frontend period picker."""
    return sorted(pd.to_datetime(df["date"]).dt.strftime("%Y-%m").unique().tolist())
