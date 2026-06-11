"""Budget limits that can actually detect overspending.

The old logic set every limit to this period's spend x 1.1, which is circular —
you can never be "over budget" against your own spending. Limits now come from,
in order of preference:

  1. a real target you set for the category;
  2. otherwise your prior period's spend in that category (a baseline to beat);
  3. otherwise unset — we don't invent a limit, so the bar shows "no budget yet"
     instead of a misleading full/empty bar.

headroom_pct is computed per category (None when there's no limit), and a zero
or missing limit never renders as a full red bar or NaN.
"""

from config import CATEGORIES, DISCLAIMER, FLOW_EXPENSE


def _spend_by_category(rows):
    rows = rows.copy()
    rows["amount_abs"] = rows["amount"].abs()
    return rows.groupby("category")["amount_abs"].sum().to_dict()


def suggest_budgets(df, metrics, targets=None, baseline=None):
    targets = targets or {}

    if "flow" in df.columns:
        expenses = df[df["flow"] == FLOW_EXPENSE].copy()
    else:
        expenses = df[df["amount"] < 0].copy()

    # Current vs prior period split by calendar month, so spending this period
    # can be measured against the previous one. A caller may also pass an
    # explicit `baseline` (e.g. once Phase 5 defines the period externally).
    months = sorted(expenses["month"].unique()) if "month" in expenses.columns else []
    if len(months) >= 1:
        current = expenses[expenses["month"] == months[-1]]
        current_spend = _spend_by_category(current)
    else:
        current_spend = _spend_by_category(expenses)

    if baseline is None:
        if len(months) >= 2:
            prior = expenses[expenses["month"] == months[-2]]
            baseline = _spend_by_category(prior)
        else:
            baseline = {}

    budgets = []
    for category in CATEGORIES:
        actual = round(current_spend.get(category, 0.0), 2)

        if category in targets:
            limit, source = round(float(targets[category]), 2), "target"
        elif category in baseline:
            limit, source = round(float(baseline[category]), 2), "baseline"
        else:
            limit, source = None, "unset"

        # Divide-by-zero / no-budget guard: no headroom %, not flagged as over.
        if limit and limit > 0:
            headroom_pct = round((limit - actual) / limit * 100, 1)
            over_budget = bool(actual > limit)
        else:
            headroom_pct = None
            over_budget = False

        budgets.append({
            "category": category,
            "actual_spend": float(actual),
            "suggested_limit": limit,
            "limit_source": source,
            "headroom_pct": headroom_pct,
            "over_budget": over_budget,
        })

    budgets.sort(key=lambda row: row["actual_spend"], reverse=True)
    return {
        "budgets": budgets,
        "current_month": str(months[-1]) if months else None,
        "baseline_month": str(months[-2]) if len(months) >= 2 else None,
        "disclaimer": DISCLAIMER,
    }
