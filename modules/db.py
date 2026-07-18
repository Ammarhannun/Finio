"""Supabase persistence — transactions, snapshots, streaks, goals, budgets."""

import json
import os
from datetime import date, datetime, timezone

from modules.history import update_streak


def _vec(embedding):
    """pgvector accepts its text form '[0.1,0.2,...]'. We pass embeddings as a
    string (for both inserts and RPC) and cast ::vector in SQL — this sidesteps
    PostgREST mistyping a raw float array."""
    return json.dumps([float(x) for x in embedding])


def upsert_kb_chunks(client, rows):
    """rows: [{id, title, content, embedding(list[float])}] → kb_chunks."""
    payload = [
        {"id": r["id"], "title": r["title"], "content": r["content"],
         "embedding": _vec(r["embedding"])}
        for r in rows
    ]
    if payload:
        client.table("kb_chunks").upsert(payload, on_conflict="id").execute()


def match_kb(client, embedding, k=2):
    res = client.rpc(
        "match_kb", {"query_embedding": _vec(embedding), "match_count": k}
    ).execute()
    return res.data or []


def upsert_merchant_embeddings(client, user_id, rows):
    """rows: [{merchant, category, embedding(list[float])}] for one user."""
    payload = [
        {"user_id": user_id, "merchant": r["merchant"],
         "category": r.get("category"), "embedding": _vec(r["embedding"])}
        for r in rows
    ]
    if payload:
        client.table("merchant_embeddings").upsert(
            payload, on_conflict="user_id,merchant"
        ).execute()


def match_merchants(client, user_id, embedding, k=5):
    res = client.rpc(
        "match_merchants",
        {"uid": user_id, "query_embedding": _vec(embedding), "match_count": k},
    ).execute()
    return res.data or []


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


def get_admin_client():
    """Client using the SERVICE ROLE key (bypasses RLS). Server-side admin
    tasks only — e.g. indexing the global knowledge base. Never expose this
    key to the browser. Returns None when the key isn't configured."""
    from supabase import create_client

    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not key:
        return None
    return create_client(os.environ["SUPABASE_URL"], key)


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
    """The user's id from their access token.

    When SUPABASE_JWT_SECRET is set we verify the JWT signature LOCALLY (HS256)
    — no network round-trip on every request. Falls back to the network
    auth.get_user check if the secret is absent or the token isn't HS256, so
    it's safe to leave unset. Data access is still guarded by Postgres RLS
    (the token is passed to PostgREST), so this only optimises identity lookup.
    """
    secret = os.getenv("SUPABASE_JWT_SECRET")
    if secret:
        try:
            import jwt

            payload = jwt.decode(
                access_token, secret, algorithms=["HS256"], audience="authenticated"
            )
            if payload.get("sub"):
                return payload["sub"]
        except Exception:
            pass  # fall back to the authoritative network check
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


def get_user_profile(client, user_id):
    """The user's stored profile row (email, age, income bracket)."""
    result = (
        client.table("users")
        .select("email, age, income_bracket")
        .eq("id", user_id)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else {}


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
        "anomalies": summary.get("anomalies", []),
        "averages": summary.get("averages"),
        "forecast": summary.get("forecast"),
        "spend_forecast": summary.get("spend_forecast"),
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
        # True when the stored snapshot was computed by older analysis logic —
        # the frontend nudges the user to hit Re-analyse.
        "snapshot_stale": (summary.get("snapshot_version") or 0) < _snapshot_version(),
    }


def _snapshot_version():
    from config import SNAPSHOT_VERSION
    return SNAPSHOT_VERSION


def persist_analysis(
    access_token,
    result,
    *,
    goal_amount,
    goal_date,
    age=None,
    income_bracket=None,
    overrides=None,
    custom_categories=None,
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
        "anomalies": result.get("anomalies", []),
        "averages": result.get("averages"),
        "forecast": result["forecast"],
        "spend_forecast": result.get("spend_forecast"),
        "budgets": result["budgets"],
        "invest": result["invest"],
        "personality": result["personality"],
        "goal_recommendation": result.get("goal_recommendation"),
    }
    # Carry the user's corrections forward so they persist across re-uploads.
    if overrides is not None:
        summary_json["overrides"] = overrides
    if custom_categories is not None:
        summary_json["custom_categories"] = custom_categories
    from config import SNAPSHOT_VERSION
    summary_json["snapshot_version"] = SNAPSHOT_VERSION
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


def get_cached_insight(client, user_id):
    """The stored AI insight for the current snapshot (None if not generated)."""
    row = get_latest_snapshot(client, user_id)
    if row:
        return (row.get("summary_json") or {}).get("insight")
    return None


def save_cached_insight(client, user_id, insight):
    """Cache the AI insight inside the snapshot so it's ONE LLM call per
    analysis, not one per dashboard load."""
    row = get_latest_snapshot(client, user_id)
    if not row:
        return
    summary = row.get("summary_json") or {}
    summary["insight"] = insight
    month = row["month"]
    month_date = f"{month}-01" if len(str(month)) == 7 else month
    save_snapshot(client, user_id, month_date, summary)


def get_custom_categories(client, user_id):
    """The user's own categories, stored in the snapshot JSON (no extra table)."""
    row = get_latest_snapshot(client, user_id)
    if row:
        return (row.get("summary_json") or {}).get("custom_categories", [])
    return []


def save_custom_categories(client, user_id, custom_categories):
    """Persist the user's custom categories into the snapshot JSON. No-ops if
    there's no snapshot yet (categories are created from the dashboard, which
    only exists after an upload)."""
    row = get_latest_snapshot(client, user_id)
    if not row:
        return
    summary = row.get("summary_json") or {}
    summary["custom_categories"] = custom_categories
    month = row["month"]
    month_date = f"{month}-01" if len(str(month)) == 7 else month
    save_snapshot(client, user_id, month_date, summary)


def save_overrides(client, user_id, overrides, resliced, custom_categories=None):
    """Persist the overrides and refresh the stored snapshot numbers so every
    page reflects the reclassification on the next load."""
    row = get_latest_snapshot(client, user_id)
    if not row:
        return
    summary = row.get("summary_json") or {}
    summary["overrides"] = overrides
    # Numbers changed → the cached AI insight no longer matches; regenerate lazily.
    summary.pop("insight", None)
    # A recompute brings the snapshot up to current logic.
    from config import SNAPSHOT_VERSION
    summary["snapshot_version"] = SNAPSHOT_VERSION
    if custom_categories is not None:
        summary["custom_categories"] = custom_categories
    for key in (
        "metrics", "analysis", "bills", "anomalies", "averages", "forecast", "spend_forecast",
        "budgets", "invest", "personality", "context", "goal_recommendation",
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


def append_chat(client, user_id, role, message, chat_id="default"):
    row = {
        "user_id": user_id,
        "role": role,
        "message": message,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "chat_id": chat_id,
    }
    try:
        client.table("chat_history").insert(row).execute()
    except Exception:
        # chat_id column missing (002 migration not run yet) → single-chat mode.
        row.pop("chat_id", None)
        client.table("chat_history").insert(row).execute()


def get_chat_history(client, user_id, limit=20, chat_id=None):
    """Messages for one chat (or the legacy single stream when chat_id is None
    or the 002 migration hasn't been run)."""
    def _q(with_chat):
        q = (
            client.table("chat_history")
            .select("role, message, timestamp" + (", chat_id" if with_chat else ""))
            .eq("user_id", user_id)
        )
        if with_chat and chat_id:
            q = q.eq("chat_id", chat_id)
        return q.order("timestamp", desc=True).limit(limit).execute()

    try:
        result = _q(True)
    except Exception:
        result = _q(False)
    return list(reversed(result.data or []))


def list_chats(client, user_id, limit=200):
    """The user's chats, newest activity first: [{chat_id, title, last_ts, count}].
    Title = first user message in the chat. Falls back to one 'default' chat
    when the 002 migration hasn't been run."""
    try:
        result = (
            client.table("chat_history")
            .select("chat_id, role, message, timestamp")
            .eq("user_id", user_id)
            .order("timestamp", desc=False)
            .limit(limit)
            .execute()
        )
    except Exception:
        return [{"chat_id": "default", "title": "Chat", "last_ts": None, "count": 0}]

    chats = {}
    for row in result.data or []:
        cid = row.get("chat_id") or "default"
        c = chats.setdefault(cid, {"chat_id": cid, "title": None, "last_ts": None, "count": 0})
        c["count"] += 1
        c["last_ts"] = row["timestamp"]
        if c["title"] is None and row["role"] == "user":
            c["title"] = (row["message"] or "")[:48]
    out = list(chats.values())
    for c in out:
        c["title"] = c["title"] or "New chat"
    out.sort(key=lambda c: c["last_ts"] or "", reverse=True)
    return out
