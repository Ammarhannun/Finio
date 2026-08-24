import os
import tempfile
from typing import Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from api.deps import AuthUser, get_current_user, get_optional_user
from config import (
    CATEGORIES,
    CURRENCY,
    COACH_CONTEXT_MESSAGES,
    DISCLAIMER,
    MAX_UPLOAD_BYTES,
    MAX_UPLOAD_MB,
    RATE_LIMITS,
)
from modules import db
from modules.logs import log, warn
from modules.ai_coach import (
    QUICK_QUESTIONS, coach_chat, generate_insight, strip_disclaimer, title_chat,
)
from modules.categoriser import examples_from_overrides
from modules.llm_categoriser import categorise_merchants, has_llm as llm_available
from modules.pipeline import analyze_stored, recompute_for_goal, run_full_pipeline
from modules.spend_check import check_purchase
from schemas import (
    BudgetRequest,
    CoachRequest,
    GoalRequest,
    OverrideRequest,
    ProfileRequest,
    QuizRequest,
    SpendCheckRequest,
)

load_dotenv()

app = FastAPI(title="Finio", description="AI-powered personal finance analyser")

# Explicit origins — a wildcard with allow_credentials=True is invalid per the
# CORS spec. Defaults cover local dev; override in prod via FINIO_ORIGINS
# (comma-separated).
_default_origins = [
    "http://localhost:5500", "http://127.0.0.1:5500",
    "http://[::1]:5500",
    "http://localhost:3000", "http://127.0.0.1:3000",
]
_origins = [
    o.strip() for o in os.getenv("FINIO_ORIGINS", "").split(",") if o.strip()
]
_cors: dict = dict(
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
if _origins:
    _cors["allow_origins"] = _origins
else:
    # Local dev: browsers often hit the frontend via IPv6 (::1) even when the
    # address bar says localhost — allow common loopback hosts/ports.
    _cors["allow_origins"] = _default_origins
    _cors["allow_origin_regex"] = r"http://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?"

app.add_middleware(CORSMiddleware, **_cors)


@app.get("/")
def health():
    return {"status": "ok", "app": "Finio", "db_configured": db.is_configured()}


# ── Per-user rate limiting on the paid endpoints ────────────────────────────
# In-process sliding window, deliberately dependency-free. The state lives in
# THIS process, so N uvicorn workers each enforce the limit independently and
# the effective ceiling becomes N x the configured value. That is a safe
# degradation (it still stops a runaway retry loop) but it must not be a
# surprise, so startup says so out loud when it detects more than one worker.
_RATE: dict = {}


@app.on_event("startup")
def _warn_if_multi_worker():
    workers = os.getenv("WEB_CONCURRENCY") or os.getenv("UVICORN_WORKERS")
    try:
        count = int(workers) if workers else 1
    except ValueError:
        count = 1
    if count > 1:
        log.warning(
            "Running %s workers: rate limits and the period-view cache are "
            "per-process, so the effective limit is %sx the configured value. "
            "Run a single worker, or move both to a shared store.",
            count, count,
        )


def _rate_limit(user_id: str, endpoint: str):
    import time as _t

    limit = RATE_LIMITS.get(endpoint)
    if not limit or not user_id:
        return
    max_calls, window = limit
    now = _t.monotonic()
    key = f"{user_id}|{endpoint}"
    hits = [t for t in _RATE.get(key, []) if now - t < window]
    if len(hits) >= max_calls:
        raise HTTPException(
            status_code=429,
            detail="That's a lot of requests in a short time — give it a minute.",
        )
    hits.append(now)
    _RATE[key] = hits
    # Keep the dict from growing forever in a long-lived process.
    if len(_RATE) > 5000:
        for k in [k for k, v in _RATE.items() if not v or now - v[-1] > 3600]:
            _RATE.pop(k, None)


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
    if user:
        _rate_limit(user.user_id, "/analyze")

    filename = (file.filename or "").lower()
    if not (filename.endswith(".csv") or filename.endswith(".pdf")):
        raise HTTPException(status_code=400, detail="Please upload a .csv or .pdf file")

    tmp_path = None
    suffix = ".pdf" if filename.endswith(".pdf") else ".csv"
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp_path = tmp.name
            # Stream to disk with a hard ceiling. Reading the whole upload into
            # memory first meant one oversized file could exhaust the server's
            # RAM before any validation ran.
            written = 0
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"File too large — the limit is {MAX_UPLOAD_MB} MB",
                    )
                tmp.write(chunk)
        if written == 0:
            raise HTTPException(status_code=400, detail="That file is empty")

        # Carry a returning user's past corrections onto the new statement:
        # their saved overrides re-apply by text-match, and become training
        # examples so the model categorises new-but-similar merchants their way.
        saved_overrides = None
        user_examples = None
        saved_custom = None
        saved_llm_cache = None
        saved_bracket = None
        if user:
            try:
                _client = db.get_client(user.token)
                saved_overrides = db.get_overrides(_client, user.user_id)
                saved_custom = db.get_custom_categories(_client, user.user_id)
                saved_llm_cache = db.get_llm_categories(_client, user.user_id)
                saved_bracket = (db.get_user_profile(_client, user.user_id) or {}).get("income_bracket")
                user_examples = examples_from_overrides(saved_overrides)
            except Exception as exc:
                # Losing saved corrections silently would make the categoriser
                # look like it had regressed for no reason.
                warn("saved corrections lookup", exc,
                     hint="this upload will categorise without your past edits")
                saved_overrides = user_examples = saved_custom = saved_llm_cache = None
                saved_bracket = None

        result = run_full_pipeline(
            tmp_path,
            goal_amount=goal_amount,
            goal_date=goal_date,
            age=age,
            overrides=saved_overrides,
            user_examples=user_examples,
            llm_cache=saved_llm_cache,
            custom_categories=saved_custom,
            income_bracket=saved_bracket,
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

        # Best-effort: embed each unique merchant for semantic merchant search.
        # Needs a key + the pgvector migration; silently skipped otherwise so it
        # never blocks or fails the upload.
        if user and result.get("persisted"):
            try:
                from modules.embeddings import embed_texts
                txs = result.get("all_transactions", result["transactions"])
                seen = {}
                for t in txs:
                    m = (t.get("merchant") or "").strip()
                    if m and m not in seen:
                        seen[m] = t.get("category")
                # Only embed merchants we haven't embedded before — a re-upload
                # of a mostly-overlapping statement used to pay for the whole
                # list again on every single upload.
                _client = db.get_client(user.token)
                known = db.get_embedded_merchants(_client, user.user_id)
                merchants = [m for m in seen if m not in known]
                vectors = embed_texts(merchants)
                if vectors:
                    rows = [{"merchant": m, "category": seen[m], "embedding": v}
                            for m, v in zip(merchants, vectors)]
                    db.upsert_merchant_embeddings(_client, user.user_id, rows)
            except Exception as exc:
                # Never block an upload on this, but say so — a bad key or an
                # unrun pgvector migration used to look identical to success.
                warn("merchant embeddings", exc,
                     hint="semantic merchant search will fall back to text match")

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


_VIEW_CACHE_MAX = 256


def _cache_evict():
    """Drop expired entries, then trim to a hard ceiling (oldest first).

    Without this the cache only ever shrank when a user wrote something, so an
    idle multi-user process grew a full analysis result per user per window and
    never gave the memory back."""
    now = time.time()
    for k in [k for k, v in _VIEW_CACHE.items() if now - v[0] >= _VIEW_TTL]:
        _VIEW_CACHE.pop(k, None)
    if len(_VIEW_CACHE) > _VIEW_CACHE_MAX:
        for k, _ in sorted(_VIEW_CACHE.items(), key=lambda kv: kv[1][0])[
            : len(_VIEW_CACHE) - _VIEW_CACHE_MAX
        ]:
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
    _cache_evict()

    all_tx = db.get_all_transactions(client, user.user_id)
    if not all_tx:
        return None
    goal = data.get("goal") or {}
    result = analyze_stored(
        all_tx,
        goal_amount=goal.get("target_amount"),
        goal_date=goal.get("target_date"),
        income_bracket=(db.get_user_profile(client, user.user_id) or {}).get("income_bracket"),
        overrides=db.get_overrides(client, user.user_id),
        budget_targets=db.get_budget_targets(client, user.user_id),
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
        "averages": resliced.get("averages"),
        "budget_targets": db.get_budget_targets(client, user.user_id),
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
        "pending_questions": db.get_pending_questions(client, user.user_id),
        "period": resliced["period"],
        "disclaimer": DISCLAIMER,
    }


@app.post("/reclassify")
def reclassify(user: AuthUser = Depends(get_current_user)):
    """Ask the model to categorise the user's merchants again, FROM SCRATCH, and
    return what it would change. Writes nothing.

    Cached classifications are deliberately bypassed — "re-classify" has to mean
    a fresh opinion, otherwise it would just replay the same stored answers.
    Merchants the user has corrected themselves are left alone and reported
    separately, so this can never quietly undo their own decisions.

    The frontend shows the result as a confirmation list; applying it goes
    through POST /overrides like any other edit.
    """
    _rate_limit(user.user_id, "/reclassify")
    client, data = _require_snapshot(user)

    all_tx = db.get_all_transactions(client, user.user_id)
    if not all_tx:
        raise HTTPException(status_code=404, detail="No transactions to reclassify")

    overrides = db.get_overrides(client, user.user_id)
    custom = db.get_custom_categories(client, user.user_id)
    categories = _all_categories(custom)

    # Current state, as the user sees it on the transactions page.
    current = analyze_stored(
        all_tx,
        goal_amount=(data.get("goal") or {}).get("target_amount"),
        goal_date=(data.get("goal") or {}).get("target_date"),
        overrides=overrides,
        period="all",
    )

    # Merchants the user has already ruled on by name — never re-decide these.
    pinned = {
        str(r["match"]).strip().upper()
        for r in overrides or [] if r.get("match")
    }

    # Aggregate the current expense rows per merchant.
    rows_by_merchant: dict = {}
    for t in current["transactions"]:
        if t.get("flow") != "expense":
            continue
        name = str(t.get("merchant") or "").strip()
        if not name:
            continue
        entry = rows_by_merchant.setdefault(
            name, {"category": t.get("category"), "count": 0, "total": 0.0}
        )
        entry["count"] += 1
        entry["total"] += abs(float(t.get("amount") or 0))

    candidates = [m for m in rows_by_merchant if m.upper() not in pinned]
    if not candidates:
        return {
            "changes": [], "unchanged": 0, "skipped_pinned": len(pinned),
            "available": bool(llm_available()), "disclaimer": DISCLAIMER,
        }

    fresh = categorise_merchants(
        candidates, categories,
        context={
            m: {"count": v["count"], "total": round(v["total"], 2),
                "avg": round(v["total"] / v["count"], 2)}
            for m, v in rows_by_merchant.items() if m in set(candidates)
        },
    )
    if not fresh:
        raise HTTPException(
            status_code=503,
            detail="Re-classification needs an OpenAI key on the server.",
        )

    changes = []
    unchanged = 0
    for merchant in candidates:
        res = fresh.get(merchant) or {}
        proposed = res.get("category")
        confidence = res.get("confidence")
        now = rows_by_merchant[merchant]["category"]
        # Only surface confident, genuinely different answers — an unsure guess
        # is not worth asking the user to confirm.
        if not proposed or confidence not in ("high", "medium") or proposed == now:
            unchanged += 1
            continue
        changes.append({
            "merchant": merchant,
            "from": now,
            "to": proposed,
            "confidence": confidence,
            "count": rows_by_merchant[merchant]["count"],
            "total": round(rows_by_merchant[merchant]["total"], 2),
        })

    # Biggest money impact first — that is what the user should check hardest.
    changes.sort(key=lambda c: c["total"], reverse=True)
    return {
        "changes": changes,
        "unchanged": unchanged,
        "skipped_pinned": len(pinned),
        "available": True,
        "disclaimer": DISCLAIMER,
    }


@app.get("/budgets")
def list_budgets(user: AuthUser = Depends(get_current_user)):
    """Current budget rows plus whichever limits the user set themselves."""
    client, data = _require_snapshot(user)
    return {
        "budgets": (data.get("budgets") or {}).get("budgets", []),
        "targets": db.get_budget_targets(client, user.user_id),
        "categories": _all_categories(db.get_custom_categories(client, user.user_id)),
        "disclaimer": DISCLAIMER,
    }


@app.post("/budgets")
def set_budgets(body: BudgetRequest, user: AuthUser = Depends(get_current_user)):
    """Set your own monthly limit for one or more categories (null clears one).
    Recomputes immediately so the bars and invest advice reflect it."""
    client, data = _require_snapshot(user)
    targets = dict(db.get_budget_targets(client, user.user_id))
    for category, amount in (body.targets or {}).items():
        if amount is None:
            targets.pop(category, None)
            continue
        try:
            value = float(amount)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"Bad amount for {category}")
        if value < 0:
            raise HTTPException(status_code=400, detail="Budgets cannot be negative")
        targets[category] = round(value, 2)

    all_tx = db.get_all_transactions(client, user.user_id)
    goal = data.get("goal") or {}
    resliced = analyze_stored(
        all_tx,
        goal_amount=goal.get("target_amount"),
        goal_date=goal.get("target_date"),
        overrides=db.get_overrides(client, user.user_id),
        budget_targets=targets,
        income_bracket=(db.get_user_profile(client, user.user_id) or {}).get("income_bracket"),
    ) if all_tx else None
    db.save_budget_targets(client, user.user_id, targets, resliced)
    _cache_clear_user(user.user_id)
    return {
        "targets": targets,
        "budgets": (resliced or {}).get("budgets", data.get("budgets")),
        "disclaimer": DISCLAIMER,
    }


@app.post("/quiz")
def quiz_answer(body: QuizRequest, user: AuthUser = Depends(get_current_user)):
    """Answer (or skip) one categorisation question. An answer becomes a
    permanent override rule — every number recomputes immediately — and the
    question is removed either way."""
    client, data = _require_snapshot(user)

    if not body.skip and (body.category or body.flow):
        all_tx = db.get_all_transactions(client, user.user_id)
        overrides = db.get_overrides(client, user.user_id)
        overrides = [r for r in overrides
                     if not (r.get("match") and r["match"].lower() == body.merchant.lower())]
        rule = {"match": body.merchant}
        if body.category:
            rule["category"] = body.category
        if body.flow:
            rule["flow"] = body.flow
        overrides.append(rule)
        goal = data.get("goal") or {}
        resliced = analyze_stored(
            all_tx,
            goal_amount=goal.get("target_amount"),
            goal_date=goal.get("target_date"),
            overrides=overrides,
        )
        db.save_overrides(
            client, user.user_id, overrides, resliced,
            custom_categories=db.get_custom_categories(client, user.user_id),
        )
        _cache_clear_user(user.user_id)

    remaining = db.remove_pending_question(client, user.user_id, body.merchant)
    return {"remaining_questions": remaining, "disclaimer": DISCLAIMER}


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

    # Marking money that LEFT the account as "income" is incoherent and used to
    # silently wreck the totals. It is now neutralised to a transfer, but the
    # user still needs telling — otherwise their numbers quietly disagree with
    # what they thought they set.
    from modules.pipeline import _restore_full_df
    from modules.data_processor import contradictory_flow_rules
    warnings = contradictory_flow_rules(_restore_full_df(all_tx), overrides)

    return {
        "overrides": overrides,
        "custom_categories": db.get_custom_categories(client, user.user_id),
        "metrics": resliced["metrics"],
        "forecast": resliced["forecast"],
        "invest": resliced["invest"],
        "flow_warnings": warnings,
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
    except Exception as exc:
        warn("profile email lookup", exc)
    db.upsert_user_profile(
        client, user.user_id, email=email,
        age=body.age, income_bracket=body.income_bracket,
    )
    if body.custom_categories is not None:
        db.save_custom_categories(client, user.user_id, body.custom_categories)
    # income_bracket feeds the goal recommendation and buffer sizing that
    # _period_view computes, so cached views are now wrong — drop them.
    _cache_clear_user(user.user_id)
    return _profile_payload(client, user)


def _alltime_insight_context(data):
    """Build an insight context from ALL-TIME figures so the recap matches the
    dashboard's 'All' view (not a partial latest-month slice). Falls back to the
    stored slice context for older snapshots without averages."""
    avg = data.get("averages") or {}
    alltime = avg.get("all")
    if not alltime:
        return data.get("context")
    from config import MIN_INCOME_FOR_RATE
    inc = alltime.get("income") or 0
    saved = alltime.get("saved") or 0
    # A savings-rate % is noise when income is near zero (e.g. income arriving as
    # not-yet-classified transfers) — omit it rather than show -1900%.
    rate = round(saved / inc * 100, 1) if inc >= MIN_INCOME_FOR_RATE else None
    return {
        "currency": CURRENCY,
        "income": inc,
        "spent": alltime.get("spent") or 0,
        "saved": saved,
        "savings_rate": rate,
        "top_categories": avg.get("top_categories", []),
        "period_label": "all time",
    }


@app.get("/insight")
def insight(
    period: Optional[str] = None,
    month: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    user: AuthUser = Depends(get_current_user),
):
    """A short natural-language recap of the user's finances (LLM when a key is
    set, template otherwise). The default (no-period) insight is CACHED in the
    snapshot — one LLM call per analysis, not one per dashboard load."""
    _rate_limit(user.user_id, "/insight")
    client, data = _require_snapshot(user)

    if not any([period, month, start, end]):
        cached = db.get_cached_insight(client, user.user_id)
        if cached:
            return strip_disclaimer(cached)
        context = _alltime_insight_context(data)
        if not context:
            raise HTTPException(status_code=404, detail="No insight yet — upload a statement first")
        result = generate_insight(context)
        db.save_cached_insight(client, user.user_id, result)
        return result

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
    db.save_spend_check(client, user.user_id, result)
    return result


@app.get("/spend-check/history")
def spend_check_history(user: AuthUser = Depends(get_current_user)):
    """Past spend checks, newest first, for the list under the form."""
    client = db.get_client(user.token)
    checks = db.get_spend_checks(client, user.user_id)
    # Hiding the card when the table is missing made the feature invisible with
    # no way to tell whether it was broken or simply unused. Say which.
    return {
        "checks": checks or [],
        "available": checks is not None,
        "setup_hint": None if checks is not None
                      else "Run migrations/003_spend_checks.sql in Supabase to save your check history.",
        "disclaimer": DISCLAIMER,
    }


@app.get("/coach/history")
def coach_history(chat_id: Optional[str] = None, user: AuthUser = Depends(get_current_user)):
    client = db.get_client(user.token)
    return {"history": db.get_chat_history(client, user.user_id, chat_id=chat_id)}


@app.get("/chats")
def chats(user: AuthUser = Depends(get_current_user)):
    """The user's coach conversations, newest first."""
    client = db.get_client(user.token)
    return {"chats": db.list_chats(client, user.user_id)}


@app.post("/coach")
def coach(body: CoachRequest, user: AuthUser = Depends(get_current_user)):
    _rate_limit(user.user_id, "/coach")
    client, data = _require_snapshot(user)

    # If the user is viewing a specific period, ground the coach in that same
    # window so its answers match what they see on screen.
    resliced = _period_view(client, user, data, period=body.period, month=body.month)
    context = (resliced or data).get("context")
    if not context:
        raise HTTPException(status_code=404, detail="No coach context — upload a CSV first")
    if body.page:
        # Tell the coach what's on the user's screen so help fits the page
        # (and reclassification proposals make sense on the transactions page).
        context = {**context, "current_page": body.page}

    chat_id = body.chat_id or "default"
    history_rows = db.get_chat_history(client, user.user_id, chat_id=chat_id)
    history = [
        {"role": row["role"], "content": row["message"]}
        for row in history_rows
        if row.get("role") in ("user", "assistant")
    ]
    # The full thread is kept for the UI, but only the most recent slice is
    # replayed to the model so a long chat doesn't grow the cost of every turn.
    history = history[-COACH_CONTEXT_MESSAGES:]

    # Give the coach the user's real transactions so its tools can compute
    # exact figures ("how much did I spend on coffee") instead of guessing.
    transactions = (
        resliced["transactions"] if resliced
        else db.get_transactions(client, user.user_id, data["month"])
    )

    response = coach_chat(body.message, context, history=history, transactions=transactions)

    db.append_chat(client, user.user_id, "user", body.message, chat_id=chat_id)
    db.append_chat(client, user.user_id, "assistant", response["text"], chat_id=chat_id)

    # Name the thread once — after the first exchange — so the sidebar shows a
    # short AI title instead of the raw first question.
    chat_title = db.get_chat_title(client, user.user_id, chat_id)
    if not chat_title:
        chat_title = title_chat(body.message, response.get("text"))
        try:
            db.set_chat_title(client, user.user_id, chat_id, chat_title)
        except Exception as exc:
            warn("chat title save", exc, hint="run migrations/002_chats.sql")

    return {
        **response,
        "chat_id": chat_id,
        "chat_title": chat_title,
        "quick_questions": QUICK_QUESTIONS,
    }
