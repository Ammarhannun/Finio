"""Supabase persistence. transactions, snapshots, streaks, goals, budgets."""

import json
import os
from datetime import date, datetime, timezone

from modules.history import update_streak
from modules.logs import debug, log, warn
from modules.serialization import json_safe


def _vec(embedding):
    """pgvector accepts its text form '[0.1,0.2,...]'. We pass embeddings as a
    string (for both inserts and RPC) and cast ::vector in SQL. this sidesteps
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


def get_embedded_merchants(client, user_id):
    """Merchant names that already have a stored embedding, so a re-upload only
    pays to embed what's genuinely new."""
    try:
        rows = paged(lambda: (
            client.table("merchant_embeddings")
            .select("merchant")
            .eq("user_id", user_id)
            .order("merchant")
        ))
    except Exception:
        return set()
    return {r["merchant"] for r in rows if r.get("merchant")}


def match_merchants(client, user_id, embedding, k=5):
    res = client.rpc(
        "match_merchants",
        {"uid": user_id, "query_embedding": _vec(embedding), "match_count": k},
    ).execute()
    return res.data or []


def is_configured():
    return bool(os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_ANON_KEY"))


def security_status(client):
    """Server-verified session/factor state, without exposing Auth tables."""
    result = client.rpc('finio_security_status', {}).execute().data
    fields = ('session_active', 'mfa_satisfied', 'recent_auth')
    if not isinstance(result, dict) or any(not isinstance(result.get(key), bool) for key in fields):
        raise ValueError('Invalid account security state')
    return {key: result[key] for key in fields}


# ── Per-request read memo ────────────────────────────────────────────────────
# One /dashboard load used to fetch the snapshot blob 4x and the whole
# transaction history 2x, because get_overrides / get_budget_targets /
# get_custom_categories each re-read the same row. get_client() builds a FRESH
# client per request, so a cache attached to the client object lives exactly one
# request. it removes the duplicate reads without ever serving a stale row
# across requests. Writes clear it so read-after-write inside one request is
# still correct.
def _memo(client):
    memo = getattr(client, "_finio_memo", None)
    if memo is None:
        memo = {}
        try:
            client._finio_memo = memo
        except Exception:
            return None  # exotic client object → just skip caching
    return memo


def _memo_clear(client, *keys):
    memo = getattr(client, "_finio_memo", None)
    if not memo:
        return
    if keys:
        for k in keys:
            memo.pop(k, None)
    else:
        memo.clear()


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

    key = os.environ['SUPABASE_ANON_KEY']
    if key.startswith('sb_secret_'):
        raise ValueError('The web service requires a Supabase publishable or anon key.')
    if key.startswith('eyJ'):
        import base64
        try:
            role = json.loads(base64.urlsafe_b64decode(key.split('.')[1] + '==='))['role']
        except (ValueError, KeyError, IndexError):
            role = None
        if role != 'anon':
            raise ValueError('The web service requires a Supabase publishable or anon key.')
    client = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_ANON_KEY"])
    if access_token:
        client.postgrest.auth(access_token)
    return client


def get_admin_client():
    """Client using the SERVICE ROLE key (bypasses RLS). Server-side admin
    tasks only. e.g. indexing the global knowledge base. Never expose this
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
   . no network round-trip on every request. Falls back to the network
    auth.get_user check if the secret is absent or the token isn't HS256, so
    it's safe to leave unset. Data access is still guarded by Postgres RLS
    (the token is passed to PostgREST), so this only optimises identity lookup.
    """
    secret = os.getenv("SUPABASE_JWT_SECRET")
    if secret:
        import jwt
        try:
            payload = jwt.decode(
                access_token, secret, algorithms=["HS256"], audience="authenticated",
                issuer=os.environ['SUPABASE_URL'].rstrip('/') + '/auth/v1',
                options={'require': ['exp', 'iat', 'sub', 'iss', 'aud']},
            )
            from uuid import UUID
            UUID(payload['sub'])
            if payload.get('role') != 'authenticated':
                raise ValueError('Invalid or expired token')
            return payload['sub']
        except (jwt.InvalidAlgorithmError, jwt.InvalidSignatureError) as exc:
            # Asymmetric keys or rotation are verified by the Auth server.
            debug('local JWT verify', exc)
        except (jwt.PyJWTError, ValueError, TypeError, KeyError) as exc:
            raise ValueError('Invalid or expired token') from exc
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
            "flow": tx.get("flow") or ("transfer" if tx.get("category") == "Transfers" else
            "expense" if tx["is_expense"] or tx["amount"] < 0 else "income"),
            "balance": tx.get("balance"),
            "position": index,
        }
        for index, tx in enumerate(transactions)
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


_INSERT_CHUNK = 500


def replace_all_transactions(client, user_id, transactions):
    """Replace the user's ENTIRE stored history (used so the dashboard can
    re-slice into any period later without a re-upload).

    Migration 004 provides an atomic, authenticated database transaction.
    Never fall back to separate delete/insert calls when the RPC is missing.
    """
    if not transactions:
        # An empty parse must never be able to erase a real history.
        return

    client.rpc("finio_replace_transactions", {
        "p_rows": json_safe(_tx_rows(user_id, transactions)),
    }).execute()
    _memo_clear(client, ("all_tx", user_id))


def paged(query_factory):
    """Read until an empty page, even when the server cap is below our page size."""
    rows = []
    while True:
        page = query_factory().range(len(rows), len(rows) + 499).execute().data or []
        rows.extend(page)
        if not page:
            return rows


def get_all_transactions(client, user_id):
    """Every stored transaction for the user, oldest first."""
    memo = _memo(client)
    key = ("all_tx", user_id)
    if memo is not None and key in memo:
        return memo[key]

    rows = paged(lambda: (
        client.table("transactions")
        .select("date, amount, merchant, category, is_expense, flow, balance, position")
        .eq("user_id", user_id)
        .order("date")
        .order("position")
        .order("id")
    ))
    if memo is not None:
        memo[key] = rows
    return rows


def save_snapshot(client, user_id, month, summary_json):
    client.table("snapshots").upsert(
        {"user_id": user_id, "month": month, "summary_json": json_safe(summary_json)},
        on_conflict="user_id,month",
    ).execute()
    # The row just changed. drop the memo so a later read in this same request
    # sees the write, not the pre-write copy.
    _memo_clear(client, ("snapshot", user_id))


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
    # suggested_limit is None for categories with no target/baseline yet (the
    # honest "no budget set" state). monthly_limit is NOT NULL, so there's
    # nothing meaningful to persist for those; skip them rather than error.
    payload = [
        {
            "user_id": user_id,
            "category": row["category"],
            "monthly_limit": row["suggested_limit"],
            "month": month,
        }
        for row in budget_rows
        if row.get("suggested_limit") is not None
    ]
    if not payload:
        return
    # One round-trip for every category, not one per category.
    client.table("budgets").upsert(payload, on_conflict="user_id,category,month").execute()


def upsert_user_profile(client, user_id, email=None, age=None, income_bracket=None,
                        clear_age=False, clear_income_bracket=False):
    payload = {"id": user_id}
    if email is not None:
        payload["email"] = email
    if age is not None or clear_age:
        payload["age"] = age
    if income_bracket is not None or clear_income_bracket:
        payload["income_bracket"] = income_bracket
    if len(payload) > 1:
        client.table("users").upsert(payload, on_conflict="id").execute()


def _missing_column(exc, *names):
    """Only schema-column errors may use the previous read shape.

    Authentication, RLS, network and unrelated SQL failures must propagate.
    Never retry by changing a tenant filter or switching to an admin client.
    """
    message = str(getattr(exc, "message", "")).lower()
    return str(getattr(exc, "code", "")) in ("42703", "PGRST204") and any(
        name.lower() in message for name in names
    )


def _revision_columns(client):
    memo = _memo(client)
    return memo.get("revision_columns", True) if memo is not None else True


def _legacy_reads(client):
    memo = _memo(client)
    if memo is not None:
        memo["revision_columns"] = False


def get_user_profile(client, user_id):
    """Read existing profiles throughout the migration-005 rollout."""
    def query(with_revision):
        return (client.table("users")
                .select("email, age, income_bracket, monthly_income, monthly_savings_target" + (", revision" if with_revision else ""))
                .eq("id", user_id).limit(1).execute())
    try:
        result = query(_revision_columns(client))
    except Exception as exc:
        if not _missing_column(exc, "revision"):
            raise
        _legacy_reads(client)
        result = query(False)
    return result.data[0] if result.data else {}


def get_latest_snapshot(client, user_id):
    memo = _memo(client)
    key = ("snapshot", user_id)
    if memo is not None and key in memo:
        return memo[key]

    def query(with_revision):
        columns = "month, summary_json" + (", revision, undo_json" if with_revision else "")
        return (client.table("snapshots").select(columns).eq("user_id", user_id)
                .order("updated_at", desc=True).limit(1).execute())
    try:
        result = query(_revision_columns(client))
    except Exception as exc:
        if not _missing_column(exc, "revision", "undo_json"):
            raise
        _legacy_reads(client)
        result = query(False)
    row = result.data[0] if result.data else None
    if row and row.get("month") and len(str(row["month"])) == 10:
        row["month"] = str(row["month"])[:7]
    if memo is not None:
        memo[key] = row
    return row



def get_transactions(client, user_id, month):
    """Fetch stored transactions for a given month (YYYY-MM)."""
    start, end = _month_bounds(month)
    return paged(lambda: (
        client.table("transactions")
        .select("date, amount, merchant, category, is_expense, flow, balance, position")
        .eq("user_id", user_id)
        .gte("date", start)
        .lt("date", end)
        .order("date").order("position").order("id")
    ))


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
        **edit_state(client, user_id),
        "review": summary.get("review"),
        "history_review": summary.get("history_review"),
        "cash_balance": summary.get("cash_balance"),
        "statement_balance": summary.get("statement_balance"),
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
        "budget_targets": summary.get("budget_targets", {}) or {},
        # True when the stored snapshot was computed by older analysis logic,
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
    budget_targets=None,
    expected_revision=None,
):
    user = get_user(access_token)
    user_id = user.id
    client = get_client(access_token)
    # month is "YYYY-MM". Supabase date column needs "YYYY-MM-01"
    month = result["snapshot"]["month"]
    month_date = f"{month}-01" if len(month) == 7 else month

    # Store the FULL history so the dashboard can re-slice to any period later.
    # Fall back to the current slice for older results without all_transactions.

    summary_json = {
        **result["snapshot"],
        "review": result.get("review"),
        "history_review": result.get("history_review"),
        "cash_balance": result.get("cash_balance"),
        "statement_balance": result.get("statement_balance"),
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
    summary_json["llm_categories"] = result.get("llm_categories", {})
    if budget_targets is not None:
        summary_json["budget_targets"] = budget_targets
    summary_json["pending_questions"] = result.get("pending_questions", [])
    from config import SNAPSHOT_VERSION
    summary_json["snapshot_version"] = SNAPSHOT_VERSION

    existing = get_streak(client, user_id)
    streak = update_streak(
        existing["last_upload"] if existing else None,
        current_streak=existing["current_streak"] if existing else 0,
        best_streak=existing["best_streak"] if existing else 0,
    )
    # A single database transaction commits every part of an upload. There is
    # deliberately no delete/insert fallback if migration 004 is missing.
    client.rpc("finio_commit_analysis", json_safe({
        "p_rows": _tx_rows(user_id, result.get("all_transactions", result["transactions"])),
        "p_month": month_date,
        "p_summary": summary_json,
        "p_profile": {"email": user.email, "age": age, "income_bracket": income_bracket},
        "p_goal": {"amount": goal_amount, "target_date": goal_date,
        "current_saved": result["forecast"]["current_saved"]},
        "p_streak": streak,
        "p_budgets": result["budgets"]["budgets"],
        "p_expected_revision": expected_revision,
    })).execute()
    _memo_clear(client)

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
    if not _revision_columns(client):
        return
    client.rpc("finio_cache_insight", {"p_expected_revision": row.get("revision"),
                                       "p_insight": json_safe(insight)}).execute()
    _memo_clear(client)


def get_custom_categories(client, user_id):
    """The user's own categories, stored in the snapshot JSON (no extra table)."""
    row = get_latest_snapshot(client, user_id)
    if row:
        return (row.get("summary_json") or {}).get("custom_categories", [])
    return []


def get_budget_targets(client, user_id):
    """The user's own monthly budget limits ({category: amount}), stored in the
    snapshot JSON like overrides so no extra table is needed."""
    row = get_latest_snapshot(client, user_id)
    if row:
        return (row.get("summary_json") or {}).get("budget_targets", {}) or {}
    return {}


def get_llm_categories(client, user_id):
    """Cached LLM merchant classifications (merchant -> {category, confidence})."""
    row = get_latest_snapshot(client, user_id)
    if row:
        return (row.get("summary_json") or {}).get("llm_categories", {})
    return {}


def get_pending_questions(client, user_id):
    row = get_latest_snapshot(client, user_id)
    if row:
        return (row.get("summary_json") or {}).get("pending_questions", [])
    return []


def save_spend_check(client, user_id, result):
    """Log one spend check. Best-effort: the feature is a history, not part of
    the answer, so a missing table (003 migration not run) must never stop the
    user getting their verdict."""
    try:
        client.table("spend_checks").insert({
            "user_id": user_id,
            "merchant": (result.get("merchant") or "").strip()[:120] or None,
            "amount": float(result.get("purchase_amount") or 0),
            "days_ahead": int(result.get("days_ahead") or 0),
            "verdict": result.get("verdict"),
            "projected_balance": (float(result["projected_balance"])
                                  if result.get("projected_balance") is not None else None),
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as exc:
        warn("spend-check history", exc, hint="run migrations/003_spend_checks.sql")


def get_spend_checks(client, user_id, limit=20):
    """The user's recent spend checks, newest first. Empty list when the table
    isn't there yet, so the page just shows nothing rather than erroring."""
    try:
        result = (
            client.table("spend_checks")
            .select("merchant, amount, days_ahead, verdict, projected_balance, checked_at")
            .eq("user_id", user_id)
            .order("checked_at", desc=True)
            .limit(limit)
            .execute()
        )
    except Exception as exc:
        debug("spend-check history read", exc)
        # None (not []) means "cannot read this", which the API turns into an
        # explanation. An empty list means "read fine, nothing logged yet".
        return None
    return result.data or []


def append_chat(client, user_id, role, message, chat_id="default", verified_facts=None):
    row = {
        "user_id": user_id,
        "role": role,
        "message": message,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "chat_id": chat_id,
        "verified_facts": json_safe(verified_facts or []),
    }
    client.table("chat_history").insert(row).execute()


def get_chat_title(client, user_id, chat_id="default"):
    """Stored AI title for a chat (role='title'), or None."""
    try:
        result = (
            client.table("chat_history")
            .select("message")
            .eq("user_id", user_id)
            .eq("chat_id", chat_id)
            .eq("role", "title")
            .order("timestamp", desc=True)
            .limit(1)
            .execute()
        )
    except Exception:
        return None
    rows = result.data or []
    return (rows[0].get("message") or "").strip() or None if rows else None


def set_chat_title(client, user_id, chat_id, title):
    """Persist an AI title as a special chat_history row (role='title')."""
    title = (title or "").strip()
    if not title:
        return
    # Keep a single title row per chat. drop older ones when we can.
    try:
        client.table("chat_history").delete().eq(
            "user_id", user_id
        ).eq("chat_id", chat_id).eq("role", "title").execute()
    except Exception as exc:
        debug("chat title cleanup", exc)
    append_chat(client, user_id, "title", title[:80], chat_id=chat_id)


def get_chat_history(client, user_id, limit=None, chat_id=None):
    """Read old/new chat rows without widening the user or conversation scope."""
    if limit is None:
        from config import CHAT_HISTORY_LIMIT
        limit = CHAT_HISTORY_LIMIT

    def query(with_facts):
        columns = "role, message, timestamp, chat_id" + (", verified_facts" if with_facts else "")
        q = client.table("chat_history").select(columns).eq("user_id", user_id).neq("role", "title")
        if chat_id:
            q = q.eq("chat_id", chat_id)
        return q.order("timestamp", desc=True).limit(limit).execute()

    try:
        result = query(True)
    except Exception as exc:
        if not _missing_column(exc, "verified_facts"):
            raise
        result = query(False)
    return list(reversed(result.data or []))



def list_chats(client, user_id, limit=2000):
    """The user's chats, newest activity first: [{chat_id, title, last_ts, count}].
    Prefers a stored AI title (role='title'); otherwise the first user message.
    Falls back to one 'default' chat when the 002 migration hasn't been run.

    Rows are fetched NEWEST-first: an oldest-first fetch with a row cap silently
    dropped the user's most recent conversations once their total message count
    passed the cap. the sidebar would stop showing new chats entirely.
    """
    try:
        result = (
            client.table("chat_history")
            .select("chat_id, role, message, timestamp")
            .eq("user_id", user_id)
            .order("timestamp", desc=True)
            .limit(limit)
            .execute()
        )
    except Exception:
        return [{"chat_id": "default", "title": "Chat", "last_ts": None, "count": 0}]

    chats = {}
    # Walk oldest→newest within the fetched window so "first user message"
    # (the fallback title) and last_ts both come out right.
    for row in reversed(result.data or []):
        cid = row.get("chat_id") or "default"
        c = chats.setdefault(
            cid, {"chat_id": cid, "title": None, "ai_title": None, "last_ts": None, "count": 0}
        )
        if row.get("role") == "title":
            c["ai_title"] = (row.get("message") or "").strip() or c["ai_title"]
            continue
        c["count"] += 1
        c["last_ts"] = row["timestamp"]
        if c["title"] is None and row["role"] == "user":
            c["title"] = (row["message"] or "")[:48]
    out = list(chats.values())
    for c in out:
        c["title"] = c.pop("ai_title", None) or c["title"] or "New chat"
    out.sort(key=lambda c: c["last_ts"] or "", reverse=True)
    return out


def edit_state(client, user_id):
    row = get_latest_snapshot(client, user_id)
    profile_revision = get_user_profile(client, user_id).get("revision") if not row else None
    available = _revision_columns(client)
    if row:
        undo = row.get("undo_json") or {}
        return {"revision": row.get("revision"), "can_undo": available and bool(undo),
                "undo_label": undo.get("label"), "editing_available": available}
    return {"revision": profile_revision, "can_undo": False, "editing_available": available}



def commit_edit(client, user_id, revision, *, resliced=None, changes=None, profile=None, label="Edit", undo=False):
    """One CAS-protected transaction; never fall back to partial writes."""
    from copy import deepcopy
    row = get_latest_snapshot(client, user_id)
    summary = deepcopy(row.get("summary_json") or {}) if row else None
    if summary is not None:
        if resliced:
            for key in ("metrics", "analysis", "bills", "anomalies", "averages", "forecast",
                        "spend_forecast", "budgets", "invest", "personality", "context", "goal_recommendation", "review", "history_review"):
                if key in resliced:
                    summary[key] = resliced[key]
            summary["snapshot_version"] = _snapshot_version()
        summary.update(changes or {})
        if changes and "overrides" in changes and "pending_questions" not in changes:
            from modules.pipeline import _restore_full_df
            from modules.data_processor import apply_flow_overrides, apply_category_overrides, reconcile_flow_contradictions
            from modules.llm_categoriser import build_questions
            frame = _restore_full_df(get_all_transactions(client, user_id))
            frame = apply_category_overrides(apply_flow_overrides(frame, changes["overrides"]), changes["overrides"])
            frame, _ = reconcile_flow_contradictions(frame)
            summary["pending_questions"] = build_questions(frame, summary.get("llm_categories", {}), changes["overrides"], limit=20000)
        summary.pop("insight", None)
    result = client.rpc("finio_edit_analysis", json_safe({
        "p_expected_revision": str(revision) if revision else None,
        "p_summary": summary, "p_profile": profile or {}, "p_label": label, "p_undo": undo,
    })).execute()
    _memo_clear(client)
    return result.data
