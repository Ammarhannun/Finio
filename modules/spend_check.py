from config import DISCLAIMER

SAFETY_BUFFER_DAYS = 7

def check_purchase(df, metrics, purchase_amount, days_ahead=30):
    current = metrics["net_saved"]
    burn = metrics["daily_burn_rate"]
    safety_buffer = round(burn * SAFETY_BUFFER_DAYS, 2)
    projected = round(
        current - purchase_amount - (burn * days_ahead),
        2,
    )
    if projected >= safety_buffer:
        verdict = "green"
        message = "You can afford this. Projected balance stays above your safety buffer."
    elif projected >= 0:
        verdict = "yellow"
        message = "Tight. You would be above zero but below your safety buffer."
    else:
        verdict = "red"
        message = "Risky. Projected balance goes negative after this purchase."
    return {
        "verdict": verdict,
        "purchase_amount": purchase_amount,
        "current_net": round(current, 2),
        "projected_balance": projected,
        "safety_buffer": safety_buffer,
        "days_ahead": days_ahead,
        "message": message,
        "disclaimer": DISCLAIMER,
    }
