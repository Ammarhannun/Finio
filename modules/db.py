"""Supabase persistence — transactions, snapshots, streaks, goals, budgets."""

import os
from datetime import date, datetime, timezone

from modules.history import update_streak


def is_configured():
    return bool(os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_ANON_KEY"))


def _month_bounds(month):
    """Return (first_day, first_day_of_next_month) for a 'YYYY-MM' string.

    Using the next month's start with a `<` comparison avoids needing to know
    how many days are in the month (e.g. there is no 2026-06-31).
    """
    year, mon = (int(part) for part in month.split("-")[:2])
    start = date(year, mon, 1)
    end = date(year + 1, 1, 1) if mon == 12 else date(year, mon + 1, 1)
    return start.isoformat(), end.isoformat()


def get_client(access_token=None):
    from supabase import create_client

    client = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_ANON_KEY"])
    if access_token:
        client.postgrest.auth(access_token)
    return client


def get_user(access_token):
    client = get_client()
    try:
        response = client.auth.get_user(access_token)
    except Exception as exc:
        raise ValueError("Invalid or expired token") from exc
    if not response or not response.user:
        raise ValueError("Invalid or expired token")
    return response.user


def get_user_id(access_token):
    return get_user(access_token).id


def _tx_rows(user_id, transactions):
    return [
        {
            "user_id": user_id,
            "date": tx["date"],
            "amount": tx["amount"],
            "merchant": tx["merchant"],
            "category": tx.get("category"),
            "is_expense": tx["is_expense"],
        }
        for tx in transactions
    ]


def replace_transactions(client, user_id, transactions, month):
    """Replace all transactions for this user in the given month (YYYY-MM)."""
    start, end = _month_bounds(month)
    client.table("transactions").delete().eq("user_id", user_id).gte(
        "date", start
    ).lt("date", end).execute()

    if not transactions:
        return
    client.table("transactions").insert(_tx_rows(user_id, transactions)).execute()


def replace_all_transactions(client, user_id, transactions):
    """Replace the user's ENTIRE stored history (used so the dashboard can
    re-slice into any period later without a re-upload)."""
    client.table("transactions").delete().eq("user_id", user_id).execute()
    if not transactions:
        return
    client.table("transactions").insert(_tx_rows(user_id, transactions)).execute()


def get_all_transactions(client, user_id):
    """Every stored transaction for the user, oldest first."""
    result = (
        client.table("transactions")
        .select("date, amount, merchant, category, is_expense")
        .eq("user_id", user_id)
        .order("date")
        .execute()
    )
    return result.data or []


def save_snapshot(client, user_id, month, summary_json):
    client.table("snapshots").upsert(
        {"user_id": user_id, "month": month, "summary_json": summary_json},
        on_conflict="user_id,month",
    ).execute()


def get_streak(client, user_id):
    result = (
        client.table("streaks")
        .select("current_streak, best_streak, last_upload")
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    if result.data:
        return result.data[0]
    return None


def save_streak(client, user_id, streak):
    client.table("streaks").upsert(
        {
            "user_id": user_id,
            "current_streak": streak["current_streak"],
            "best_streak": streak["best_streak"],
            "last_upload": streak["last_upload"],
        },
        on_conflict="user_id",
    ).execute()


def upsert_goal(client, user_id, goal_amount, goal_date, current_saved):
    client.table("goals").upsert(
        {
            "user_id": user_id,
            "name": "Savings goal",
            "target_amount": goal_amount,
            "target_date": goal_date,
            "current_saved": current_saved,
        },
        on_conflict="user_id",
    ).execute()


def upsert_budgets(client, user_id, month, budget_rows):
    for row in budget_rows:
        client.table("budgets").upsert(
            {
                "user_id": user_id,
                "category": row["category"],
                "monthly_limit": row["suggested_limit"],
                "month": month,
            },
            on_conflict="user_id,category,month",
        ).execute()


def upsert_user_profile(client, user_id, email=None, age=None, income_bracket=None):
    payload = {"id": user_id}
    if email is not None:
        payload["email"] = email
    if age is not None:
        payload["age"] = age
    if income_bracket is not None:
        payload["income_bracket"] = income_bracket
    if len(payload) > 1:
        client.table("users").upsert(payload, on_conflict="id").execute()


def get_latest_snapshot(client, user_id):
    result = (
        client.table("snapshots")
        .select("month, summary_json")
        .eq("user_id", user_id)
        .order("month", desc=True)
        .limit(1)
        .execute()
    )
    if result.data:
        row = result.data[0]
        # Normalise month back to "YYYY-MM" for internal use
        if row.get("month") and len(str(row["month"])) == 10:
            row["month"] = str(row["month"])[:7]
        return row
    return None


def get_transactions(client, user_id, month):
    """Fetch stored transactions for a given month (YYYY-MM)."""
    start, end = _month_bounds(month)
    result = (
        client.table("transactions")
        .select("date, amount, merchant, category, is_expense")
        .eq("user_id", user_id)
        .gte("date", start)
        .lt("date", end)
        .execute()
    )
    return result.data or []


def save_goal_recompute(
    client, user_id, month, recomputed, goal_amount, goal_date, age=None
):
    """Persist a user-set goal: update the snapshot's forecast/invest + goal row."""
    snapshot_row = get_latest_snapshot(client, user_id)
    if snapshot_row:
        summary = snapshot_row["summary_json"]
        summary["forecast"] = recomputed["forecast"]
        summary["invest"] = recomputed["invest"]
        month_date = f"{month}-01" if len(str(month)) == 7 else month
        save_snapshot(client, user_id, month_date, summary)

    upsert_goal(
        client,
        user_id,
        goal_amount,
        goal_date,
        recomputed["forecast"]["current_saved"],
    )
    if age is not None:
        upsert_user_profile(client, user_id, age=age)


def get_goal(client, user_id):
    result = (
        client.table("goals")
        .select("name, target_amount, target_date, current_saved")
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    if result.data:
        return result.data[0]
    return None


def get_budgets(client, user_id, month=None):
    query = client.table("budgets").select("category, monthly_limit, month").eq(
        "user_id", user_id
    )
    if month:
        month_date = f"{month}-01" if len(str(month)) == 7 else month
        query = query.eq("month", month_date)
    result = query.execute()
    return result.data or []


def load_dashboard(client, user_id):
    snapshot_row = get_latest_snapshot(client, user_id)
    if not snapshot_row:
        return None

    summary = snapshot_row["summary_json"]
    streak = get_streak(client, user_id)
    goal = get_goal(client, user_id)
    month = snapshot_row["month"]
    budget_rows = get_budgets(client, user_id, month)

    return {
        "month": month,
        "metrics": summary.get("metrics"),
        "analysis": summary.get("analysis"),
        "bills": summary.get("bills", []),
        "forecast": summary.get("forecast"),
        "budgets": summary.get("budgets"),
        "budget_limits": budget_rows,
        "invest": summary.get("invest"),
        "personality": summary.get("personality"),
        "snapshot": {
            "month": summary.get("month"),
            "saved_at": summary.get("saved_at"),
            "risk_score": summary.get("risk_score"),
            "risk_label": summary.get("risk_label"),
            "top_category": summary.get("top_category"),
            "bill_count": summary.get("bill_count"),
            "patterns": summary.get("patterns"),
        },
        "context": summary.get("context"),
        "goal_recommendation": summary.get("goal_recommendation"),
        "streak": streak,
        "goal": goal,
    }


def persist_analysis(
    access_token,
    result,
    *,
    goal_amount,
    goal_date,
    age=None,
    income_bracket=None,
):
    user = get_user(access_token)
    user_id = user.id
    client = get_client(access_token)
    # Always upsert the profile so the users row exists (email is NOT NULL)
    upsert_user_profile(
        client, user_id, email=user.email, age=age, income_bracket=income_bracket
    )
    # month is "YYYY-MM" — Supabase date column needs "YYYY-MM-01"
    month = result["snapshot"]["month"]
    month_date = f"{month}-01" if len(month) == 7 else month

    # Store the FULL history so the dashboard can re-slice to any period later.
    # Fall back to the current slice for older results without all_transactions.
    replace_all_transactions(
        client, user_id, result.get("all_transactions", result["transactions"])
    )

    summary_json = {
        **result["snapshot"],
        "context": result["context"],
        "metrics": result["metrics"],
        "analysis": result["analysis"],
        "bills": result["bills"],
        "forecast": result["forecast"],
        "budgets": result["budgets"],
        "invest": result["invest"],
        "personality": result["personality"],
        "goal_recommendation": result.get("goal_recommendation"),
    }
    save_snapshot(client, user_id, month_date, summary_json)

    existing = get_streak(client, user_id)
    streak = update_streak(
        existing["last_upload"] if existing else None,
        current_streak=existing["current_streak"] if existing else 0,
        best_streak=existing["best_streak"] if existing else 0,
    )
    save_streak(client, user_id, streak)

    upsert_goal(
        client,
        user_id,
        goal_amount,
        goal_date,
        result["forecast"]["current_saved"],
    )
    upsert_budgets(client, user_id, month_date, result["budgets"]["budgets"])

    return streak


def get_overrides(client, user_id):
    """The user's saved flow overrides (e.g. 'treat PAYID JOHN as income').

    Stored inside the snapshot JSON so no extra table is needed."""
    row = get_latest_snapshot(client, user_id)
    if row:
        return (row.get("summary_json") or {}).get("overrides", [])
    return []


def save_overrides(client, user_id, overrides, resliced):
    """Persist the overrides and refresh the stored snapshot numbers so every
    page reflects the reclassification on the next load."""
    row = get_latest_snapshot(client, user_id)
    if not row:
        return
    summary = row.get("summary_json") or {}
    summary["overrides"] = overrides
    for key in (
        "metrics", "analysis", "bills", "forecast", "budgets",
        "invest", "personality", "context", "goal_recommendation",
    ):
        if key in resliced:
            summary[key] = resliced[key]

    month = row["month"]
    month_date = f"{month}-01" if len(str(month)) == 7 else month
    save_snapshot(client, user_id, month_date, summary)
    upsert_goal(
        client, user_id,
        summary.get("forecast", {}).get("target_amount"),
        summary.get("forecast", {}).get("target_date"),
        summary.get("forecast", {}).get("current_saved"),
    )


def append_chat(client, user_id, role, message):
    client.table("chat_history").insert(
        {
            "user_id": user_id,
            "role": role,
            "message": message,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    ).execute()


def get_chat_history(client, user_id, limit=20):
    result = (
        client.table("chat_history")
        .select("role, message, timestamp")
        .eq("user_id", user_id)
        .order("timestamp", desc=True)
        .limit(limit)
        .execute()
    )
    return list(reversed(result.data or []))
