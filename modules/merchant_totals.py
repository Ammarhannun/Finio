"""Conservative refund identity for display aggregates, never transaction edits."""
import re
from modules.bank_parser import clean_merchant_name
from config import FLOW_EXPENSE


def net_merchant_totals(df):
    if df is None or df.empty:
        return []
    expenses = df[df["flow"] == FLOW_EXPENSE]
    column = "merchant_clean" if "merchant_clean" in expenses else "description"
    rows = [(clean_merchant_name(name), float(amount))
            for name, amount in zip(expenses[column], expenses["amount"])]
    purchases = {name for name, amount in rows if amount < 0}
    totals = {}
    for name, amount in rows:
        identity = name
        if amount > 0 and name not in purchases:
            # Only credit rows with an explicit edge marker and an exact
            # matching purchase merchant can be joined. No fuzzy matching.
            candidate = re.sub(r"^(?:REFUND(?: FROM)?|REVERSAL(?: FROM)?)\s+", "", name)
            candidate = re.sub(r"\s+(?:REFUND|REVERSAL)$", "", candidate).strip()
            if candidate in purchases:
                identity = candidate
        totals[identity] = totals.get(identity, 0.0) - amount
    # Keep unmatched or excess refunds as net credits, so the list reconciles.
    return [{"merchant": name, "total": round(total, 2)}
            for name, total in sorted(totals.items(), key=lambda item: (-item[1], item[0]))
            if round(total, 2) != 0]
