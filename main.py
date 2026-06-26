import os
import tempfile
from typing import Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from api.deps import AuthUser, get_current_user, get_optional_user
from config import CATEGORIES, DISCLAIMER
from modules import db
from modules.ai_coach import QUICK_QUESTIONS, coach_chat, generate_insight
from modules.categoriser import examples_from_overrides
from modules.pipeline import analyze_stored, recompute_for_goal, run_full_pipeline
from modules.spend_check import check_purchase
from schemas import (
    CoachRequest,
    GoalRequest,
    OverrideRequest,
    ProfileRequest,
    SpendCheckRequest,
)

load_dotenv()

app = FastAPI(title="Finio", description="AI-powered personal finance analyser")

# Explicit origins — a wildcard with allow_credentials=True is invalid per the
# CORS spec. Defaults cover local dev; override in prod via FINIO_ORIGINS
# (comma-separated).
_default_origins = [
    "http://localhost:5500", "http://127.0.0.1:5500",
    "http://localhost:3000", "http://127.0.0.1:3000",
]
_origins = [
    o.strip() for o in os.getenv("FINIO_ORIGINS", "").split(",") if o.strip()
] or _default_origins

app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def health():
    return {"status": "ok", "app": "Finio", "db_configured": db.is_configured()}


@app.post("/analyze")
async def analyze_csv(
    file: UploadFile = File(...),
    goal_amount: Optional[float] = Form(None),
    goal_date: Optional[str] = Form(None),
    age: Optional[int] = Form(None),
    period: Optional[str] = Form(None),
    period_start: Optional[str] = Form(None),
    period_end: Optional[str] = Form(None),
    user: Optional[AuthUser] = Depends(get_optional_user),
):
    filename = (file.filename or "").lower()
    if not (filename.endswith(".csv") or filename.endswith(".pdf")):
        raise HTTPException(status_code=400, detail="Please upload a .csv or .pdf file")

    tmp_path = None
    suffix = ".pdf" if filename.endswith(".pdf") else ".csv"
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp_path = tmp.name
            content = await file.read()
            tmp.write(content)

        # Carry a returning user's past corrections onto the new statement:
        # their saved overrides re-apply by text-match, and become training
        # examples so the model categorises new-but-similar merchants their way.
        saved_overrides = None
        user_examples = None
        saved_custom = None
        if user:
            try:
                _client = db.get_client(user.token)
                saved_overrides = db.get_overrides(_client, user.user_id)
                saved_custom = db.get_custom_categories(_client, user.user_id)
                user_examples = examples_from_overrides(saved_overrides)
            except Exception:
                saved_overrides = user_examples = saved_custom = None

        result = run_full_pipeline(
            tmp_path,
            goal_amount=goal_amount,
            goal_date=goal_date,
            age=age,
            overrides=saved_overrides,
            user_examples=user_examples,
            period=period,
            period_start=period_start,
            period_end=period_end,
        )

        if user:
            try:
                goal_used = result["goal_used"]
                streak = db.persist_analysis(
                    user.token,
                    result,
                    goal_amount=goal_used["amount"],
                    goal_date=goal_used["target_date"],
                    age=goal_used["age"],
                    overrides=saved_overrides,
                    custom_categories=saved_custom,
                )
                result["streak"] = streak
                result["persisted"] = True
                _cache_clear_user(user.user_id)
            except ValueError as exc:
                raise HTTPException(status_code=401, detail=str(exc)) from exc
            except Exception as exc:
                raise HTTPException(
                    status_code=500, detail=f"Failed to save to database: {exc}"
                ) from exc
        else:
            result["persisted"] = False

        return result
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)


def _require_snapshot(user: AuthUser):
    client = db.get_client(user.token)
    data = db.load_dashboard(client, user.user_id)
    if not data or not data.get("metrics"):
        raise HTTPException(
            status_code=404,
            detail="No analysis found — upload a CSV via POST /analyze first",
        )
    return client, data


def _available_months(client, user):
    all_tx = db.get_all_transactions(client, user.user_id)
    months = sorted({str(t["date"])[:7] for t in all_tx}) if all_tx else []
    return all_tx, months


import time

# In-process cache of resliced views. Period-switching re-runs the full-history
# pipeline (restore + bill detection + budgets…) every time; cache the result
# per (user, period window) and clear the user's entries on ANY write so it can
# never serve stale numbers. TTL is just a safety net.
_VIEW_CACHE: dict = {}
_VIEW_TTL = 300


def _view_key(user_id, period, month, start, end):
    return f"{user_id}|{period}|{month}|{start}|{end}"


def _cache_clear_user(user_id):
    for k in [k for k in _VIEW_CACHE if k.startswith(f"{user_id}|")]:
        _VIEW_CACHE.pop(k, None)


def _period_view(client, user, data, *, period=None, month=None, start=None, end=None):
    """Re-slice the user's whole stored history into the requested window,
    honouring any saved flow overrides and keeping their goal. Returns None when
    no period is requested, so callers fall back to the stored snapshot.

    This is the single place period slicing happens, so EVERY page (dashboard,
    invest, coach, spend check) describes the same window."""
    if not any([period, month, start, end]):
        return None

    key = _view_key(user.user_id, period, month, start, end)
    hit = _VIEW_CACHE.get(key)
    if hit and (time.time() - hit[0]) < _VIEW_TTL:
        return hit[1]

    all_tx = db.get_all_transactions(client, user.user_id)
    if not all_tx:
        return None
    goal = data.get("goal") or {}
    result = analyze_stored(
        all_tx,
        goal_amount=goal.get("target_amount"),
        goal_date=goal.get("target_date"),
        overrides=db.get_overrides(client, user.user_id),
        period=period,
        period_anchor=f"{month}-01" if month else None,
        period_start=start,
        period_end=end,
    )
    _VIEW_CACHE[key] = (time.time(), result)
    return result


@app.get("/dashboard")
def dashboard(
    period: Optional[str] = None,
    month: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    user: AuthUser = Depends(get_current_user),
):
    client, data = _require_snapshot(user)
    _, available = _available_months(client, user)

    resliced = _period_view(
        client, user, data, period=period, month=month, start=start, end=end
    )
    if resliced is None:
        # No period requested → return the stored (default) snapshot as-is.
        data["available_months"] = available
        data["disclaimer"] = DISCLAIMER
        return data

    return {
        "month": data.get("month"),
        "metrics": resliced["metrics"],
        "analysis": resliced["analysis"],
        "bills": resliced["bills"],
        "anomalies": resliced.get("anomalies", []),
        "forecast": resliced["forecast"],
        "spend_forecast": resliced.get("spend_forecast"),
        "budgets": resliced["budgets"],
        "invest": resliced["invest"],
        "personality": resliced["personality"],
        "context": resliced["context"],
        "goal": data.get("goal"),
        "goal_recommendation": resliced["goal_recommendation"],
        "period": resliced["period"],
        "available_months": available,
        "streak": data.get("streak"),
        "disclaimer": DISCLAIMER,
    }


@app.get("/invest")
def invest(
    period: Optional[str] = None,
    month: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    user: AuthUser = Depends(get_current_user),
):
    client, data = _require_snapshot(user)
    _, available = _available_months(client, user)
    resliced = _period_view(
        client, user, data, period=period, month=month, start=start, end=end
    )
    view = resliced or data
    return {
        "invest": view.get("invest"),
        "forecast": view.get("forecast"),
        "goal": data.get("goal"),
        "metrics": view.get("metrics"),
        "period": resliced["period"] if resliced else None,
        "available_months": available,
        "disclaimer": DISCLAIMER,
    }


def _all_categories(custom):
    """Spend categories offered in the editor: the built-in buckets plus any the
    user invented. Transfers stay out — that's a flow, not a spend category."""
    cats = list(CATEGORIES)
    for c in custom or []:
        if c and c not in cats:
            cats.append(c)
    return cats


@app.get("/transactions")
def transactions(
    period: Optional[str] = None,
    month: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    user: AuthUser = Depends(get_current_user),
):
    """Every categorised transaction for the requested window, so the dashboard
    can show what counted as income / spending / a bill and let the user fix it.
    Defaults to all-time (most useful for correcting classifications)."""
    client, data = _require_snapshot(user)
    all_tx = db.get_all_transactions(client, user.user_id)
    if not all_tx:
        raise HTTPException(status_code=404, detail="No transactions found")

    goal = data.get("goal") or {}
    resliced = analyze_stored(
        all_tx,
        goal_amount=goal.get("target_amount"),
        goal_date=goal.get("target_date"),
        overrides=db.get_overrides(client, user.user_id),
        period=period or "all",
        period_anchor=f"{month}-01" if month else None,
        period_start=start,
        period_end=end,
    )

    txs = resliced["transactions"]
    bill_merchants = {
        str(b.get("merchant", "")).strip().upper() for b in (resliced["bills"] or [])
    }
    for t in txs:
        t["is_bill"] = str(t.get("merchant", "")).strip().upper() in bill_merchants

    custom = db.get_custom_categories(client, user.user_id)
    return {
        "transactions": txs,
        "categories": _all_categories(custom),
        "custom_categories": custom,
        "period": resliced["period"],
        "disclaimer": DISCLAIMER,
    }


@app.post("/overrides")
def set_overrides(body: OverrideRequest, user: AuthUser = Depends(get_current_user)):
    """Reclassify transactions (e.g. mark a regular transfer as income, or a
    merchant as an expense) so every number on the platform gets more accurate.
    Persists the rules and refreshes the stored snapshot."""
    client, data = _require_snapshot(user)
    all_tx = db.get_all_transactions(client, user.user_id)
    if not all_tx:
        raise HTTPException(status_code=404, detail="No transactions to reclassify")

    overrides = [r.model_dump(exclude_none=True) for r in body.rules]
    goal = data.get("goal") or {}
    resliced = analyze_stored(
        all_tx,
        goal_amount=goal.get("target_amount"),
        goal_date=goal.get("target_date"),
        overrides=overrides,
    )
    db.save_overrides(
        client, user.user_id, overrides, resliced,
        custom_categories=body.custom_categories,
    )
    _cache_clear_user(user.user_id)
    return {
        "overrides": overrides,
        "custom_categories": db.get_custom_categories(client, user.user_id),
        "metrics": resliced["metrics"],
        "forecast": resliced["forecast"],
        "invest": resliced["invest"],
        "disclaimer": DISCLAIMER,
    }


@app.post("/reanalyze")
def reanalyze(user: AuthUser = Depends(get_current_user)):
    """Recompute the saved snapshot from the user's stored transactions (honouring
    their overrides) without needing them to re-upload the file. Refreshes the
    persisted metrics, budgets, recommendation, etc."""
    client, data = _require_snapshot(user)
    all_tx = db.get_all_transactions(client, user.user_id)
    if not all_tx:
        raise HTTPException(status_code=404, detail="No transactions to re-analyse")

    overrides = db.get_overrides(client, user.user_id)
    goal = data.get("goal") or {}
    resliced = analyze_stored(
        all_tx,
        goal_amount=goal.get("target_amount"),
        goal_date=goal.get("target_date"),
        overrides=overrides,
    )
    # Reuse the snapshot-refresh path (writes metrics/analysis/budgets/forecast/…).
    db.save_overrides(
        client, user.user_id, overrides, resliced,
        custom_categories=db.get_custom_categories(client, user.user_id),
    )
    _cache_clear_user(user.user_id)
    return {"ok": True, "metrics": resliced["metrics"], "disclaimer": DISCLAIMER}


@app.get("/overrides")
def list_overrides(user: AuthUser = Depends(get_current_user)):
    client, _ = _require_snapshot(user)
    return {
        "overrides": db.get_overrides(client, user.user_id),
        "custom_categories": db.get_custom_categories(client, user.user_id),
    }


@app.post("/goal")
def set_goal(body: GoalRequest, user: AuthUser = Depends(get_current_user)):
    client, data = _require_snapshot(user)
    transactions = db.get_transactions(client, user.user_id, data["month"])
    if not transactions:
        raise HTTPException(
            status_code=404, detail="No transactions found — upload a statement first"
        )

    goal_date = body.target_date.isoformat()
    recomputed = recompute_for_goal(
        transactions,
        data["metrics"],
        goal_amount=body.amount,
        goal_date=goal_date,
        age=body.age,
    )
    db.save_goal_recompute(
        client,
        user.user_id,
        data["month"],
        recomputed,
        body.amount,
        goal_date,
        age=body.age,
    )
    _cache_clear_user(user.user_id)
    return {
        **recomputed,
        "goal": db.get_goal(client, user.user_id),
        "disclaimer": DISCLAIMER,
    }


def _profile_payload(client, user: AuthUser):
    profile = db.get_user_profile(client, user.user_id)
    email = profile.get("email")
    if not email:
        try:
            email = db.get_user(user.token).email
        except Exception:
            email = None
    return {
        "email": email,
        "age": profile.get("age"),
        "income_bracket": profile.get("income_bracket"),
        "custom_categories": db.get_custom_categories(client, user.user_id),
        "streak": db.get_streak(client, user.user_id),
    }


@app.get("/profile")
def get_profile(user: AuthUser = Depends(get_current_user)):
    """The user's account details for the profile page. Works before any upload
    (e.g. right after signup) so age can be set early."""
    client = db.get_client(user.token)
    return _profile_payload(client, user)


@app.post("/profile")
def update_profile(
    body: ProfileRequest, user: AuthUser = Depends(get_current_user)
):
    client = db.get_client(user.token)
    # Ensure the users row exists (email is NOT NULL) and apply the edits.
    email = None
    try:
        email = db.get_user(user.token).email
    except Exception:
        pass
    db.upsert_user_profile(
        client, user.user_id, email=email,
        age=body.age, income_bracket=body.income_bracket,
    )
    if body.custom_categories is not None:
        db.save_custom_categories(client, user.user_id, body.custom_categories)
    return _profile_payload(client, user)


@app.get("/insight")
def insight(
    period: Optional[str] = None,
    month: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    user: AuthUser = Depends(get_current_user),
):
    """A short natural-language recap of the user's finances (LLM when a key is
    set, template otherwise). Fetched lazily so it isn't computed on every load."""
    client, data = _require_snapshot(user)
    resliced = _period_view(client, user, data, period=period, month=month, start=start, end=end)
    context = (resliced or data).get("context")
    if not context:
        raise HTTPException(status_code=404, detail="No insight yet — upload a statement first")
    return generate_insight(context)


@app.post("/spend-check")
def spend_check(body: SpendCheckRequest, user: AuthUser = Depends(get_current_user)):
    client, data = _require_snapshot(user)
    resliced = _period_view(client, user, data, period=body.period, month=body.month)
    metrics = dict((resliced or data)["metrics"])
    # Real balance is a point-in-time fact, not per-period — re-sliced stored rows
    # don't carry it, so borrow it from the saved snapshot when missing.
    if metrics.get("latest_balance") is None:
        metrics["latest_balance"] = (data.get("metrics") or {}).get("latest_balance")
    result = check_purchase(None, metrics, body.amount, body.days_ahead)
    result["merchant"] = body.merchant
    return result


@app.get("/coach/history")
def coach_history(user: AuthUser = Depends(get_current_user)):
    client = db.get_client(user.token)
    return {"history": db.get_chat_history(client, user.user_id)}


@app.post("/coach")
def coach(body: CoachRequest, user: AuthUser = Depends(get_current_user)):
    client, data = _require_snapshot(user)

    # If the user is viewing a specific period, ground the coach in that same
    # window so its answers match what they see on screen.
    resliced = _period_view(client, user, data, period=body.period, month=body.month)
    context = (resliced or data).get("context")
    if not context:
        raise HTTPException(status_code=404, detail="No coach context — upload a CSV first")

    history_rows = db.get_chat_history(client, user.user_id)
    history = [{"role": row["role"], "content": row["message"]} for row in history_rows]

    # Give the coach the user's real transactions so its tools can compute
    # exact figures ("how much did I spend on coffee") instead of guessing.
    transactions = (
        resliced["transactions"] if resliced
        else db.get_transactions(client, user.user_id, data["month"])
    )

    response = coach_chat(body.message, context, history=history, transactions=transactions)

    db.append_chat(client, user.user_id, "user", body.message)
    db.append_chat(client, user.user_id, "assistant", response["text"])

    return {
        **response,
        "quick_questions": QUICK_QUESTIONS,
    }
