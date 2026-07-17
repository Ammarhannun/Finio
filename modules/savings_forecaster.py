import pandas as pd
from config import DISCLAIMER


def forecast_spending(df, metrics):
    """Project next month's spend and, if a real balance is known, when the user
    might run short. Uses the recent monthly spend trend (resample by month) so
    it reflects current habits, not the whole-history average.
    """
    out = {
        "projected_next_month": 0.0,
        "avg_monthly_spend": 0.0,
        "trend": None,            # "up" | "down" | None
        "runway_days": None,
        "run_short_date": None,
        "disclaimer": DISCLAIMER,
    }
    if df is None or df.empty:
        return out
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    exp = df[df["flow"] == "expense"] if "flow" in df.columns else df[df["amount"] < 0]
    if exp.empty:
        return out

    # Runway (balance / daily burn) is meaningful even with little history.
    balance = (metrics or {}).get("latest_balance")
    burn = (metrics or {}).get("daily_burn_rate") or 0
    if balance is not None and burn > 0:
        runway = int(balance / burn)
        out["runway_days"] = runway
        last_date = exp["date"].max()
        out["run_short_date"] = (last_date + pd.Timedelta(days=runway)).strftime("%Y-%m-%d")

    monthly = exp.set_index("date")["amount"].abs().resample("MS").sum()
    monthly = _complete_months(monthly, exp["date"])
    # One (possibly partial) month is not a trend — don't project from it.
    if len(monthly) < 2:
        return out
    avg = float(monthly.mean())
    recent = float(monthly.tail(3).mean())        # last 3 months ≈ current habits
    out["avg_monthly_spend"] = round(avg, 2)
    out["projected_next_month"] = round(recent, 2)
    prior = float(monthly.iloc[:-1].mean())
    out["trend"] = "up" if monthly.iloc[-1] > prior else "down"
    return out


def _complete_months(series_by_month, dates):
    """Drop incomplete EDGE months from a monthly resample.

    A statement rarely starts on the 1st or ends on the 31st; counting a half
    month as a whole one drags every average (goal recs, forecasts). The first
    month is kept only if the data starts on day 1; the last only if the data
    runs to that month's final day. Falls back to everything when nothing whole
    survives (better a rough number than none).
    """
    if not len(series_by_month):
        return series_by_month
    keep = series_by_month
    first_day = dates.min()
    last_day = dates.max()
    if first_day.day != 1:
        keep = keep.iloc[1:]
    if len(keep) and last_day != (last_day + pd.offsets.MonthEnd(0)):
        keep = keep.iloc[:-1]
    return keep if len(keep) else series_by_month


def monthly_net_average(df):
    """Average NET savings per COMPLETE calendar month (income minus spend,
    transfers excluded). Resampling by month means one big payday or a lean
    month doesn't skew it, and dropping partial edge months stops a half month
    being counted as a whole one.
    """
    df = df.copy()
    if "is_transfer" in df.columns:
        df = df[~df["is_transfer"]]
    if df.empty:
        return 0.0
    df["date"] = pd.to_datetime(df["date"])
    monthly = df.set_index("date")["amount"].resample("MS").sum()
    monthly = _complete_months(monthly, df["date"])
    return float(monthly.mean()) if len(monthly) else 0.0


def recommend_goal(metrics, horizon_months=6, monthly_saved=None):
    """Suggest a savings goal (amount + date) from the user's actual numbers.

    Scales the target to what the user really saves per month so it feels
    achievable rather than arbitrary. Prefer the robust monthly figure
    (`monthly_saved`, averaged across calendar months by the caller); fall back
    to the current slice's net_saved when it isn't supplied.

    If they're not saving yet, recommend a starter emergency buffer sized to
    roughly one month of their spending (with a sensible floor) instead of an
    arbitrary number, so the goal still means something.
    """
    date_range = metrics.get("date_range", {}) or {}
    days = date_range.get("days") or 30
    end = date_range.get("end")

    if monthly_saved is None:
        saved = metrics.get("net_saved", 0) or 0
        monthly_saved = (saved / days) * 30 if days else 0

    base = pd.Timestamp(end) if end else pd.Timestamp.today()
    target_date = (base + pd.DateOffset(months=horizon_months)).strftime("%Y-%m-%d")

    def _round100(x):
        return int(round(x / 100.0)) * 100

    if monthly_saved <= 0:
        # One month of spending makes a meaningful first buffer; floor at $500.
        monthly_spend = (metrics.get("total_spent", 0) or 0) / days * 30 if days else 0
        amount = max(_round100(monthly_spend), 500)
        rationale = (
            "You spent more than you earned this period, so start with a safety "
            f"buffer. About ${amount:,} covers roughly a month of your spending."
        )
    else:
        amount = max(_round100(monthly_saved * horizon_months), 500)
        rationale = (
            f"You save about ${monthly_saved:,.0f} a month. At that pace, "
            f"${amount:,} in {horizon_months} months is realistic."
        )

    return {
        "amount": amount,
        "target_date": target_date,
        "horizon_months": horizon_months,
        "monthly_saved": round(monthly_saved, 2),
        "rationale": rationale,
        "disclaimer": DISCLAIMER,
    }


def _empty_forecast(target_amount, target_date):
    return {
        "on_track": False,
        "current_saved": 0.0,
        "projected_total": 0.0,
        "monthly_rate": 0.0,
        "target_amount": target_amount,
        "target_date": pd.Timestamp(target_date).strftime("%Y-%m-%d"),
        "net_savings_rate": 0.0,
        "months_remaining": 0,
        "days_remaining": 0,
        "disclaimer": DISCLAIMER,
    }


def forecast_goal(df, target_amount, target_date):
    """Project savings to the goal date from a realistic monthly trend.

    Instead of fitting a line through just the first and last day (which swings
    wildly on a short or partial window), we average the user's NET savings per
    calendar month across their whole history. One big payday or a single lean
    month no longer distorts the trajectory, so the on track verdict feels real.

    Pass the FULL history here (not a single period slice) so current_saved is
    the true running total toward the goal.
    """
    target_date = pd.Timestamp(target_date)
    df = df.copy()
    # Exclude transfers so current_saved equals the canonical net_saved
    # (income - spend), matching the dashboard and every other page.
    if "is_transfer" in df.columns:
        df = df[~df["is_transfer"]]
    if df.empty:
        return _empty_forecast(target_amount, target_date)

    df["date"] = pd.to_datetime(df["date"])
    last_date = df["date"].max()
    current_saved = float(df["amount"].sum())

    # Average net per calendar month = the robust monthly savings rate.
    monthly_rate = monthly_net_average(df)

    # Whole calendar months from the last data point to the target month.
    months_remaining = max(
        (target_date.year - last_date.year) * 12
        + (target_date.month - last_date.month),
        0,
    )
    projected_total = current_saved + monthly_rate * months_remaining
    days_remaining = max((target_date - last_date).days, 0)
    on_track = projected_total >= target_amount
    return {
        "on_track": bool(on_track),
        "current_saved": round(current_saved, 2),
        "projected_total": round(projected_total, 2),
        "monthly_rate": round(monthly_rate, 2),
        "target_amount": target_amount,
        "target_date": target_date.strftime("%Y-%m-%d"),
        # Kept for any caller still reading a daily rate.
        "net_savings_rate": round(monthly_rate / 30.44, 2),
        "months_remaining": months_remaining,
        "days_remaining": days_remaining,
        "disclaimer": DISCLAIMER,
    }

if __name__ == "__main__":
    from config import SAMPLE_CSV
    from modules.bank_parser import parse_bank_csv
    from modules.data_processor import process_transactions

    df = parse_bank_csv(SAMPLE_CSV)
    df, metrics = process_transactions(df)
    result = forecast_goal(df, target_amount=5000, target_date="2026-06-30")
    print(result)
    print("metrics:", metrics)
