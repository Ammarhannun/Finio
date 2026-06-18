import pandas as pd
from config import DISCLAIMER


def recommend_goal(metrics, horizon_months=6):
    """Suggest a savings goal (amount + date) from the user's actual numbers.

    Picks a horizon a few months out and scales the target to roughly what the
    user already saves per month, so it feels achievable rather than arbitrary.
    """
    date_range = metrics.get("date_range", {}) or {}
    days = date_range.get("days") or 30
    end = date_range.get("end")

    saved = metrics.get("net_saved", 0) or 0
    monthly_saved = (saved / days) * 30 if days else 0

    base = pd.Timestamp(end) if end else pd.Timestamp.today()
    target_date = (base + pd.DateOffset(months=horizon_months)).strftime("%Y-%m-%d")

    if monthly_saved <= 0:
        amount = 1000
        rationale = (
            "You spent more than you earned this period, so let's start small. "
            "A $1,000 starter buffer is a solid first goal."
        )
    else:
        raw = monthly_saved * horizon_months
        amount = max(int(round(raw / 100.0)) * 100, 500)
        rationale = (
            f"You're saving about ${monthly_saved:,.0f}/month. At that pace, "
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
    monthly = df.set_index("date")["amount"].resample("MS").sum()
    monthly_rate = float(monthly.mean()) if len(monthly) else 0.0

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
