import pandas as pd
from sklearn.linear_model import LinearRegression
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
            "You spent more than you earned this period, so let's start small — "
            "a $1,000 starter buffer is a solid first goal."
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


def forecast_goal(df, target_amount, target_date):
    target_date = pd.Timestamp(target_date)
    df = df.copy()
    # Exclude transfers so current_saved equals the canonical net_saved
    # (income - spend), matching the dashboard and every other page.
    if "is_transfer" in df.columns:
        df = df[~df["is_transfer"]]
    start_date = df["date"].min()
    last_date = df["date"].max()
    daily = df.groupby("date", as_index=False)["amount"].sum()
    date_range = pd.date_range(start_date, last_date, freq="D")
    daily = daily.set_index("date").reindex(date_range, fill_value=0).reset_index()
    daily.columns = ["date", "amount"]
    daily["days_since_start"] = (daily["date"] - start_date).dt.days

    current_saved = float(daily["amount"].sum())
    last_day = int(daily["days_since_start"].iloc[-1])
    target_day = (target_date - start_date).days

    model = LinearRegression()
    model.fit([[0], [last_day]], [0, current_saved])
    net_savings_rate = float(model.coef_[0])
    projected_total = current_saved + net_savings_rate * (target_day - last_day)
    days_remaining = (target_date - last_date).days
    on_track = projected_total >= target_amount
    return {
        "on_track": on_track,
        "current_saved": round(current_saved, 2),
        "projected_total": round(projected_total, 2),
        "target_amount": target_amount,
        "target_date": target_date.strftime("%Y-%m-%d"),
        "net_savings_rate": round(net_savings_rate, 2),
        "days_remaining": max(days_remaining, 0),
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
