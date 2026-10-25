import os
import tempfile
import threading
from datetime import date
from functools import lru_cache
from contextlib import asynccontextmanager
from typing import Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile, Request
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
from modules.serialization import json_safe
from modules.bank_parser import StatementRejected, parse_bank_csv
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

@asynccontextmanager
async def lifespan(app):
    _check_configuration()
    _warn_if_multi_worker()
    yield


app = FastAPI(title="Finio", description="AI-powered personal finance analyser", lifespan=lifespan)

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

from api.body_limit import BodyLimitMiddleware
app.add_middleware(BodyLimitMiddleware, upload_limit=MAX_UPLOAD_BYTES + 1024 * 1024)
app.add_middleware(CORSMiddleware, **_cors)


@app.middleware("http")
async def response_security(request: Request, call_next):
    from fastapi.responses import JSONResponse
    length = request.headers.get("content-length", "0")
    if not length.isdigit() or int(length) > MAX_UPLOAD_BYTES + 1024 * 1024:
        return JSONResponse({"detail": "Request too large. Upload a file under 10 MB."}, status_code=413)
    response = await call_next(request)

    # Financial data: never let a response sit in a shared or browser cache.
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
    response.headers["Pragma"] = "no-cache"

    # Never let a browser second-guess a declared content type.
    response.headers["X-Content-Type-Options"] = "nosniff"

    # This API returns JSON only — there is no legitimate reason for any of it
    # to be framed, scripted, or embedded, so lock the lot down.
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    )

    # Do not leak the path a user was on (which can name a page, a filter, or a
    # merchant) to any third party they click through to.
    response.headers["Referrer-Policy"] = "no-referrer"

    # Nothing here needs a camera, microphone, location or payment handler.
    response.headers["Permissions-Policy"] = (
        "accelerometer=(), camera=(), geolocation=(), gyroscope=(), "
        "magnetometer=(), microphone=(), payment=(), usb=()"
    )

    # Tell browsers to refuse plain HTTP in future. Only meaningful once a real
    # certificate is in front of this, so it is sent on HTTPS requests only —
    # sending it in local dev would pin localhost to HTTPS and break it.
    if request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https":
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )

    # uvicorn announces itself by default; no reason to name the stack.
    response.headers["Server"] = "finio"
    return response


@app.exception_handler(Exception)
async def unexpected_error(request: Request, exc: Exception):
    from fastapi.responses import JSONResponse
    import uuid
    reference = uuid.uuid4().hex[:12]
    # Do not log exception strings: provider errors may contain financial data.
    log.error("Request failed: %s %s %s reference=%s", request.method, request.url.path,
              type(exc).__name__, reference)
    return JSONResponse({"detail": f"Something went wrong. Please try again. Reference: {reference}"},
                        status_code=500)


@app.get("/ready")
def readiness():
    if not db.is_configured() or (os.getenv("FINIO_ENV") == "production" and
                                 (not _origins or any(not o.startswith("https://") or '*' in o for o in _origins))):
        raise HTTPException(503, "Service is not ready")
    try:
        if db.get_client().rpc("finio_ready", {}).execute().data is not True:
            raise ValueError("schema not ready")
    except Exception:
        raise HTTPException(503, "Service is not ready") from None
    return {"status": "ready"}


_UPLOAD_SLOTS = threading.BoundedSemaphore(2)
_ACTIVE_UPLOADS = set()
_UPLOAD_LOCK = threading.Lock()


def upload_user(user: AuthUser = Depends(get_current_user)):
    with _UPLOAD_LOCK:
        if user.user_id in _ACTIVE_UPLOADS or not _UPLOAD_SLOTS.acquire(blocking=False):
            raise HTTPException(429, "An upload is already processing. Please try again shortly.")
        _ACTIVE_UPLOADS.add(user.user_id)
    try:
        yield user
    finally:
        with _UPLOAD_LOCK:
            _ACTIVE_UPLOADS.discard(user.user_id)
            _UPLOAD_SLOTS.release()


@app.get("/demo")
@lru_cache(maxsize=1)
def demo():
    # Fixed synthetic data, offline, cached once; never invokes a paid model.
    from config import SAMPLE_CSV
    from modules.data_processor import add_flags
    from modules.pipeline import analyze_window
    from modules.categoriser import rule_category
    frame = add_flags(parse_bank_csv(SAMPLE_CSV))
    frame["category"] = frame.apply(lambda row: (
        "Transfers" if row["flow"] == "transfer" else "Income" if row["flow"] == "income"
        else rule_category(row["description"]) or "Other"), axis=1)
    return {**analyze_window(frame), "demo": True}


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
_RATE_LOCK = threading.Lock()


# Required for the app to do anything at all. Checked once at startup so a
# missing value is a clear line in the logs, not a KeyError buried in the first
# request that touches the database.
_REQUIRED_ENV = ("SUPABASE_URL", "SUPABASE_ANON_KEY")


def _check_configuration():
    missing = [k for k in _REQUIRED_ENV if not os.getenv(k)]
    if missing:
        log.error(
            "MISSING REQUIRED CONFIG: %s. Every authenticated request will "
            "fail with a 500 until these are set.", ", ".join(missing),
        )

    # The CORS default only covers localhost. Deployed WITHOUT FINIO_ORIGINS,
    # every browser request from the real frontend is blocked — and it fails in
    # the browser, so the server logs look perfectly healthy while the site is
    # completely broken. Refuse to be quiet about it.
    if not _origins:
        log.warning(
            "FINIO_ORIGINS is not set, so CORS only allows localhost. If this "
            "is a deployed environment, set FINIO_ORIGINS to your frontend's "
            "URL (e.g. https://finio.vercel.app) or the browser will block "
            "every request.",
        )
    else:
        log.info("CORS allowing: %s", ", ".join(_origins))

    if not os.getenv("OPENAI_API_KEY"):
        log.warning(
            "OPENAI_API_KEY is not set — the coach, insights and the model "
            "categoriser fall back to the offline path (measured 20%% accuracy "
            "on unseen merchants vs 87%% with the model).",
        )


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
    with _RATE_LOCK:
        _rate_limit_locked(user_id, endpoint)


def _rate_limit_locked(user_id: str, endpoint: str):
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
def analyze_csv(
    file: UploadFile = File(...),
    goal_amount: Optional[float] = Form(None, gt=0, le=100_000_000, allow_inf_nan=False),
    goal_date: Optional[date] = Form(None),
    age: Optional[int] = Form(None, ge=16, le=100),
    period: Optional[str] = Form(None),
    period_start: Optional[str] = Form(None),
    period_end: Optional[str] = Form(None),
    confirm_replace: bool = Form(False),
    user: AuthUser = Depends(upload_user),
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
            while chunk := file.file.read(1024 * 1024):
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
        saved_targets = {}
        saved_goal = {}
        saved_age = None
        if user:
            try:
                _client = db.get_client(user.token)
                saved_overrides = db.get_overrides(_client, user.user_id)
                saved_custom = db.get_custom_categories(_client, user.user_id)
                saved_llm_cache = db.get_llm_categories(_client, user.user_id)
                profile = db.get_user_profile(_client, user.user_id) or {}
                saved_bracket = profile.get("income_bracket")
                saved_age = profile.get("age")
                saved_targets = db.get_budget_targets(_client, user.user_id)
                saved_goal = db.get_goal(_client, user.user_id) or {}
                if db.get_all_transactions(_client, user.user_id) and not confirm_replace:
                    raise HTTPException(409, "This replaces your saved transaction history. Confirm replacement or upload your complete history.")
                user_examples = examples_from_overrides(saved_overrides)
            except HTTPException:
                raise
            except Exception as exc:
                # Losing saved corrections silently would make the categoriser
                # look like it had regressed for no reason.
                log.warning("Upload stopped: saved settings unavailable (%s)", type(exc).__name__)
                raise HTTPException(503, "Could not load your saved settings. Nothing has been replaced; please try again.") from None

        try:
            result = run_full_pipeline(
            tmp_path,
            goal_amount=goal_amount if goal_amount is not None else saved_goal.get("target_amount"),
            goal_date=goal_date.isoformat() if goal_date else saved_goal.get("target_date"),
            age=age if age is not None else saved_age,
            overrides=saved_overrides,
            user_examples=user_examples,
            llm_cache=saved_llm_cache,
            custom_categories=saved_custom,
            income_bracket=saved_bracket,
            budget_targets=saved_targets,
            period=period,
            period_start=period_start,
            period_end=period_end,
            )
        except StatementRejected as exc:
            # Our own limit, written for the user — say which one they hit.
            raise HTTPException(422, str(exc)) from None
        except (ValueError, UnicodeError) as exc:
            raise HTTPException(422, "Could not read this statement. Use a CSV with date, description and amount columns, or a text-based PDF.") from None
        except Exception as exc:
            from pdfminer.pdfparser import PDFSyntaxError
            from pdfminer.pdfdocument import PDFPasswordIncorrect
            if isinstance(exc, (PDFSyntaxError, PDFPasswordIncorrect)):
                raise HTTPException(422, "This PDF could not be read. Try an unlocked text-based PDF or export a CSV.") from None
            raise

        if user:
            try:
                goal_used = result["goal_used"]
                streak = db.persist_analysis(
                    user.token,
                    result,
                    goal_amount=goal_used["amount"],
                    goal_date=goal_used["target_date"],
                    age=age if age is not None else saved_age,
                    income_bracket=saved_bracket,
                    overrides=saved_overrides,
                    custom_categories=saved_custom,
                    budget_targets=saved_targets,
                )
                result["streak"] = streak
                result["persisted"] = True
                _cache_clear_user(user.user_id)
            except ValueError as exc:
                raise HTTPException(status_code=401, detail=str(exc)) from exc
            except Exception as exc:
                raise HTTPException(
                    status_code=503, detail="Could not save your analysis. Your previous data is unchanged. Please try again."
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
                merchants = [m for m in seen if m not in known][:500]
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

        return json_safe(result)
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


@app.post("/analyze/preview")
def preview_statement(file: UploadFile = File(...), user: AuthUser = Depends(upload_user)):
    """Parse locally for review. No AI calls or persistence before confirmation."""
    _rate_limit(user.user_id, "/analyze")
    suffix = os.path.splitext(file.filename or "")[1].lower()
    if suffix not in (".csv", ".pdf"):
        raise HTTPException(400, "Please choose a CSV or text-based PDF.")
    path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            path = tmp.name
            size = 0
            while chunk := file.file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "The file must be under 10 MB.")
                tmp.write(chunk)
        try:
            frame = parse_bank_csv(path)
        except StatementRejected as exc:
            raise HTTPException(422, str(exc)) from None
        except Exception:
            raise HTTPException(422, "Could not read the statement. Try a CSV with date, description and amount columns, or an unlocked text-based PDF.") from None
        return {"count": len(frame), "start": frame.date.min().strftime("%Y-%m-%d"),
        "end": frame.date.max().strftime("%Y-%m-%d"),
        "money_out": round(float(-frame.loc[frame.amount < 0, "amount"].sum()), 2),
        "money_in": round(float(frame.loc[frame.amount > 0, "amount"].sum()), 2),
        "best_effort_pdf": suffix == ".pdf", "disclaimer": DISCLAIMER}
    finally:
        if path:
            os.remove(path)


@app.get("/data/export")
def export_data(user: AuthUser = Depends(get_current_user)):
    _rate_limit(user.user_id, "/analyze")
    client = db.get_client(user.token)
    out = {"profile": db.get_user_profile(client, user.user_id)}
    for table in ("transactions", "snapshots", "budgets", "goals", "streaks", "chat_history", "spend_checks", "merchant_embeddings"):
        ordering = "merchant" if table == "merchant_embeddings" else "id"
        out[table] = db.paged(lambda table=table, ordering=ordering: client.table(table)
                             .select("*").eq("user_id", user.user_id).order(ordering))
    return {"format_version": 1, "data": out}


@app.post("/data/delete")
def delete_data(body: dict, user: AuthUser = Depends(upload_user)):
    if body != {"confirmation": "DELETE"}:
        raise HTTPException(422, "Type DELETE to permanently remove your saved Finio data.")
    db.get_client(user.token).rpc("finio_delete_data", {}).execute()
    _cache_clear_user(user.user_id)
    return {"deleted": True, "account_retained": True}


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
_VIEW_LOCK = threading.RLock()
_VIEW_EPOCH = 0


def _view_key(user_id, period, month, start, end):
    return f"{user_id}|{period}|{month}|{start}|{end}"


def _cache_clear_user(user_id):
    global _VIEW_EPOCH
    with _VIEW_LOCK:
        _VIEW_EPOCH += 1
        for k in [k for k in _VIEW_CACHE if k.startswith(f"{user_id}|")]:
            _VIEW_CACHE.pop(k, None)


_VIEW_CACHE_MAX = 256


def _cache_evict():
    with _VIEW_LOCK:
        _cache_evict_locked()


def _cache_evict_locked():
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
    import re
    if period and period not in ("daily", "weekly", "monthly", "all", "custom"):
        raise HTTPException(422, "Choose a supported time period.")
    if month and not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(422, "Month must be YYYY-MM.")
    if month:
        try:
            date.fromisoformat(month + '-01')
        except ValueError:
            raise HTTPException(422, "Choose a valid month.") from None
    if period == "custom":
        try:
            if not start or not end or date.fromisoformat(start) > date.fromisoformat(end):
                raise ValueError()
        except ValueError:
            raise HTTPException(422, "Choose a valid start and end date.") from None

    key = _view_key(user.user_id, period, month, start, end)
    with _VIEW_LOCK:
        epoch = _VIEW_EPOCH
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
        age=(db.get_user_profile(client, user.user_id) or {}).get("age"),
        overrides=db.get_overrides(client, user.user_id),
        budget_targets=db.get_budget_targets(client, user.user_id),
        period=period,
        period_anchor=f"{month}-01" if month else None,
        period_start=start,
        period_end=end,
    )
    with _VIEW_LOCK:
        # A concurrent write invalidates this in-flight read. Return its
        # result to that caller, but never repopulate the cache with old data.
        if epoch == _VIEW_EPOCH:
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
        age=(db.get_user_profile(client, user.user_id) or {}).get("age"),
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
            budget_targets=db.get_budget_targets(client, user.user_id),
            age=(db.get_user_profile(client, user.user_id) or {}).get("age"),
            income_bracket=(db.get_user_profile(client, user.user_id) or {}).get("income_bracket"),
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
        budget_targets=db.get_budget_targets(client, user.user_id),
        age=(db.get_user_profile(client, user.user_id) or {}).get("age"),
        income_bracket=(db.get_user_profile(client, user.user_id) or {}).get("income_bracket"),
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
        budget_targets=db.get_budget_targets(client, user.user_id),
        age=(db.get_user_profile(client, user.user_id) or {}).get("age"),
        income_bracket=(db.get_user_profile(client, user.user_id) or {}).get("income_bracket"),
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
    transactions = db.get_all_transactions(client, user.user_id)
    if not transactions:
        raise HTTPException(
            status_code=404, detail="No transactions found — upload a statement first"
        )

    goal_date = body.target_date.isoformat()
    recomputed = analyze_stored(
        transactions,
        goal_amount=body.amount,
        goal_date=goal_date,
        age=body.age if body.age is not None else (db.get_user_profile(client, user.user_id) or {}).get("age"),
        overrides=db.get_overrides(client, user.user_id),
        budget_targets=db.get_budget_targets(client, user.user_id),
        income_bracket=(db.get_user_profile(client, user.user_id) or {}).get("income_bracket"),
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
        clear_age="age" in body.model_fields_set and body.age is None,
        clear_income_bracket="income_bracket" in body.model_fields_set and body.income_bracket is None,
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
    # Balance and its date must come from the SAME saved statement point, not
    # the selected analysis slice (which may be empty or an older month).
    source_metrics = data.get("metrics") or {}
    metrics["latest_balance"] = source_metrics.get("latest_balance")
    metrics["balance_date"] = (source_metrics.get("date_range") or {}).get("end")
    if body.current_balance is not None:
        metrics["latest_balance"] = body.current_balance
        metrics["balance_date"] = date.today().isoformat()
        metrics["user_entered_balance"] = True
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
    source_metrics = data.get("metrics") or {}
    context = {**context,
    "latest_balance": source_metrics.get("latest_balance"),
    "balance_date": (source_metrics.get("date_range") or {}).get("end")}
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
