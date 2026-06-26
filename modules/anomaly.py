"""Unusual-spend detection.

Flags expense transactions that are abnormally large *for their category*,
judged against the user's own history with a per-category z-score. This is
personalised: a $120 dinner is normal for a big spender but an outlier for
someone whose dinners average $25. Pure stats, no API key.
"""

import pandas as pd

Z_THRESHOLD = 2.5     # how many std devs above the category mean counts as unusual
MIN_HISTORY = 5       # need enough same-category history to have a stable mean
MIN_AMOUNT = 20       # ignore trivial amounts where small swings look "unusual"


def detect_anomalies(df, limit=5):
    """Return the most unusual expense transactions (biggest z-score first).

    Each item: merchant, category, amount, date, typical (category mean), z.
    Pass the user's FULL history so the per-category baseline is stable.
    """
    if df is None or df.empty or "flow" not in df.columns:
        return []
    exp = df[df["flow"] == "expense"].copy()
    if exp.empty:
        return []
    exp["amt"] = exp["amount"].abs()

    name_col = "merchant_clean" if "merchant_clean" in exp.columns else "description"
    anomalies = []
    for cat, g in exp.groupby("category"):
        if len(g) < MIN_HISTORY:
            continue
        mean = g["amt"].mean()
        std = g["amt"].std()
        if not std or std == 0:
            continue
        for _, row in g.iterrows():
            if row["amt"] < MIN_AMOUNT:
                continue
            z = (row["amt"] - mean) / std
            if z >= Z_THRESHOLD:
                anomalies.append({
                    "merchant": str(row.get(name_col) or "Unknown"),
                    "category": cat,
                    "amount": round(float(row["amt"]), 2),
                    "date": pd.Timestamp(row["date"]).strftime("%Y-%m-%d"),
                    "typical": round(float(mean), 2),
                    "z": round(float(z), 1),
                })
    anomalies.sort(key=lambda a: a["z"], reverse=True)
    return anomalies[:limit]
