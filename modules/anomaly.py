"""Unusual-spend detection.

Flags expense transactions that are abnormally large *for their category*,
judged against the user's own history. This is personalised: a $120 dinner is
normal for a big spender but an outlier for someone whose dinners average $25.
Pure stats, no API key.

Why a *robust* score rather than a plain z-score
------------------------------------------------
The obvious version — (x - mean) / std — has two flaws that made it miss the
exact blowouts it exists to catch:

1. **A hard ceiling.** For a sample of size n, the largest attainable z-score is
   (n - 1) / sqrt(n). That is 1.79 at n=5 and only 2.48 at n=8, so against a
   threshold of 2.5 *nothing* in a category with 8 or fewer transactions could
   ever be flagged, no matter how extreme. A $5,000 charge among $25 coffees
   scored 0 hits.
2. **Masking.** The outlier is itself inside the mean and std it is measured
   against, so one big spike inflates the baseline and hides itself.

The modified z-score (Iglewicz & Hoaglin) uses the median and the median
absolute deviation instead. The median and MAD barely move when one point is
extreme, so there is no ceiling and no masking, and it stays meaningful on the
small per-category samples a few months of statements actually produce.
"""

import pandas as pd

# Modified z-score threshold. 3.5 is the standard Iglewicz-Hoaglin cutoff.
Z_THRESHOLD = 3.5
MIN_HISTORY = 4       # robust stats need far less history than mean/std did
MIN_AMOUNT = 20       # ignore trivial amounts where small swings look "unusual"

# Scale factors that put the MAD (and the mean-absolute-deviation fallback) on
# the same footing as a standard deviation for normally distributed data.
_MAD_SCALE = 0.6745
_MEANAD_SCALE = 1.253314


def _robust_scores(values):
    """Modified z-scores for a series of amounts, or None if it has no spread.

    Falls back to the mean absolute deviation when the MAD is zero — that
    happens whenever more than half the charges are the same amount (a $25
    coffee every day), which is common in real statements and would otherwise
    make the whole category unscoreable.
    """
    median = values.median()
    mad = (values - median).abs().median()
    if mad > 0:
        # M_i = 0.6745 * (x - median) / MAD
        return _MAD_SCALE * (values - median) / mad, median

    mean_ad = (values - median).abs().mean()
    if mean_ad > 0:
        return (values - median) / (_MEANAD_SCALE * mean_ad), median

    return None, median  # every charge identical → nothing to flag


def detect_anomalies(df, limit=5):
    """Return the most unusual expense transactions (biggest score first).

    Each item: merchant, category, amount, date, typical (category median), z.
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
        scores, median = _robust_scores(g["amt"])
        if scores is None:
            continue
        for idx, row in g.iterrows():
            if row["amt"] < MIN_AMOUNT:
                continue
            score = scores.loc[idx]
            # Only ever flag overspending, never an unusually cheap purchase.
            if score >= Z_THRESHOLD:
                anomalies.append({
                    "merchant": str(row.get(name_col) or "Unknown"),
                    "category": cat,
                    "amount": round(float(row["amt"]), 2),
                    "date": pd.Timestamp(row["date"]).strftime("%Y-%m-%d"),
                    "typical": round(float(median), 2),
                    "z": round(float(score), 1),
                })
    anomalies.sort(key=lambda a: a["z"], reverse=True)
    return anomalies[:limit]
