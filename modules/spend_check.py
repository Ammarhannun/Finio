from config import DISCLAIMER
from datetime import date

SAFETY_BUFFER_DAYS = 7

def recent_statement_balance(metrics):
    """Return a dated statement balance only when it is at most 30 days old."""
    balance = metrics.get("latest_balance")
    date_range = metrics.get("date_range") or {}
    balance_date = metrics.get("balance_date") or date_range.get("end")
    try:
        age_days = (date.today() - date.fromisoformat(balance_date)).days
    except (TypeError, ValueError):
        age_days = None
    if balance is None or age_days is None or age_days > 30 or age_days < 0:
        return None, balance_date
    return float(balance), balance_date


def check_purchase(df, metrics, purchase_amount, days_ahead=30):
    # A statement's running balance is historical, not necessarily cash on hand.
    # Net savings is a flow and must never be presented as an account balance.
    current, balance_date = recent_statement_balance(metrics)
    uses_balance = metrics.get("latest_balance") is not None
    live_balance = bool(metrics.get("user_entered_balance"))
    if current is None or (not live_balance and balance_date != date.today().isoformat()):
        reason = ("This statement has no account balance" if not uses_balance else
        "This statement balance is not from today")
        return {
            "verdict": "unknown", "purchase_amount": purchase_amount,
            "current_net": None, "uses_balance": uses_balance,
            "projected_balance": None, "safety_buffer": None,
        "days_ahead": days_ahead,
        "balance_date": balance_date,
        "message": f"{reason}. Enter your current available balance to see an estimate, then check upcoming bills before deciding.",
        "disclaimer": DISCLAIMER,
        }
    # Refund-heavy periods can make net spending negative; they cannot justify
    # a negative safety buffer or make a purchase look safer by projection.
    burn = max(0.0, float(metrics.get("daily_burn_rate") or 0))
    safety_buffer = round(burn * SAFETY_BUFFER_DAYS, 2)
    projected = round(
        current - purchase_amount - (burn * days_ahead),
        2,
    )
    basis = ("the balance you entered" if live_balance else
             f"your statement balance on {balance_date}")
    if projected >= safety_buffer:
        verdict = "green"
        message = f"This estimate stays above the buffer using {basis}. Check your live balance and upcoming bills before deciding."
    elif projected >= 0:
        verdict = "yellow"
        message = f"Tight. Based on {basis}, you would be above zero but below your safety buffer."
    else:
        verdict = "red"
        message = f"Risky. Based on {basis}, your projected balance goes negative after this purchase."
    return {
        "verdict": verdict,
        "purchase_amount": purchase_amount,
        "current_net": round(current, 2),
        "uses_balance": uses_balance,
        "projected_balance": projected,
        "safety_buffer": safety_buffer,
        "days_ahead": days_ahead,
        "balance_date": balance_date,
        "balance_source": "entered" if live_balance else "statement",
        "message": message,
        "disclaimer": DISCLAIMER,
    }
