import os
import tempfile
import threading
import json
import re
from datetime import date
from functools import lru_cache
from contextlib import asynccontextmanager
from typing import Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError

from api.deps import AuthUser, get_current_user, get_optional_user, require_sensitive_access
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
from modules.logs import log, warn, request_failure
from modules.ai_coach import (
    QUICK_QUESTIONS, coach_chat, generate_insight, strip_disclaimer, title_chat,
)
from modules.categoriser import examples_from_overrides
from modules.llm_categoriser import categorise_merchants, has_llm as llm_available
from modules.pipeline import analyze_stored, recompute_for_goal, run_full_pipeline
from modules.spend_check import check_purchase
from modules.cash_balance import balance_view, spending_metrics
from modules.ai_privacy import AIPrivacy, redact_credentials
from schemas import (
    BudgetRequest,
    CashBalanceRequest,
    CoachRequest,
    GoalRequest,
    OverrideRequest,
    ProfileRequest,
    QuizRequest,
    SpendCheckRequest,
    RevisionRequest,
)

load_dotenv()

@asynccontextmanager
async def lifespan(app):
    _check_configuration()
    _warn_if_multi_worker()
    yield


_production = os.getenv('FINIO_ENV') == 'production'
app = FastAPI(title="Finio", description="AI-powered personal finance analyser", lifespan=lifespan,
              docs_url=None if _production else '/docs',
              redoc_url=None if _production else '/redoc',
              openapi_url=None if _production else '/openapi.json')

from api.privacy_contact import router as privacy_contact_router
app.include_router(privacy_contact_router)

# Explicit origins. a wildcard with allow_credentials=True is invalid per the
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
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)
if _origins:
    _cors["allow_origins"] = _origins
else:
    # Local dev: browsers often hit the frontend via IPv6 (::1) even when the
    # address bar says localhost. allow common loopback hosts/ports.
    _cors["allow_origins"] = _default_origins
    _cors["allow_origin_regex"] = r"http://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?"

from api.body_limit import BodyLimitMiddleware
from api.upload_auth import UploadAuthMiddleware
app.add_middleware(BodyLimitMiddleware, upload_limit=MAX_UPLOAD_BYTES + 1024 * 1024)
app.add_middleware(UploadAuthMiddleware)
app.add_middleware(CORSMiddleware, **_cors)
from modules.ai_policy import AIRequestMiddleware, preference_status, provider_allowed
app.add_middleware(AIRequestMiddleware)


def _security_headers(request):
    # Financial data: never let a response sit in a shared or browser cache.
    headers = {"Cache-Control": "no-store, no-cache, must-revalidate, private", "Pragma": "no-cache"}

    # Never let a browser second-guess a declared content type.
    headers["X-Content-Type-Options"] = "nosniff"

    # This API returns JSON only. there is no legitimate reason for any of it
    # to be framed, scripted, or embedded, so lock the lot down.
    headers["X-Frame-Options"] = "DENY"
    headers["Content-Security-Policy"] = (
        "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    )

    # Do not leak the path a user was on (which can name a page, a filter, or a
    # merchant) to any third party they click through to.
    headers["Referrer-Policy"] = "no-referrer"

    # Nothing here needs a camera, microphone, location or payment handler.
    headers["Permissions-Policy"] = (
        "accelerometer=(), camera=(), geolocation=(), gyroscope=(), "
        "magnetometer=(), microphone=(), payment=(), usb=()"
    )

    # Tell browsers to refuse plain HTTP in future. Only meaningful once a real
    # certificate is in front of this, so it is sent on HTTPS requests only,
    # sending it in local dev would pin localhost to HTTPS and break it.
    if request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https":
        headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )

    # uvicorn announces itself by default; no reason to name the stack.
    headers["Server"] = "finio"
    return headers


def _error_headers(request):
    headers = _security_headers(request)
    origin = request.headers.get('origin')
    if origin and (origin in _cors['allow_origins'] or
                   (_cors.get('allow_origin_regex') and re.fullmatch(_cors['allow_origin_regex'], origin))):
        # Unhandled exceptions are outside CORSMiddleware. Keep approved
        # frontends able to display the generic reference, never private input.
        headers.update({'Access-Control-Allow-Origin': origin, 'Vary': 'Origin'})
    return headers


@app.middleware("http")
async def response_security(request: Request, call_next):
    from fastapi.responses import JSONResponse
    authorization = [v for k, v in request.scope['headers'] if k.lower() == b'authorization']
    lengths = [v for k, v in request.scope['headers'] if k.lower() == b'content-length']
    if len(authorization) > 1 or len(lengths) > 1:
        request_failure(request.method, '[rejected headers]', 400)
        return JSONResponse({'detail': 'Duplicate request headers are not allowed.'},
                            status_code=400, headers=_error_headers(request))
    length = request.headers.get("content-length", "0")
    if len(length) > 12 or not length.isdigit() or int(length) > MAX_UPLOAD_BYTES + 1024 * 1024:
        request_failure(request.method, '[rejected size]', 413)
        return JSONResponse({"detail": "Request too large. Upload a file under 10 MB."},
                            status_code=413, headers=_error_headers(request))
    response = await call_next(request)
    response.headers.update(_security_headers(request))
    request_failure(request.method, getattr(request.scope.get('route'), 'path', '[unmatched]'), response.status_code)
    return response


@app.exception_handler(RequestValidationError)
async def invalid_request(request: Request, exc: RequestValidationError):
    from fastapi.responses import JSONResponse
    # Pydantic's default errors include input and validator context, which can
    # contain pasted financial data or credentials. Return field locations only.
    privacy = AIPrivacy()
    detail = [{'loc': privacy.value(list(error['loc'])), 'type': error['type'],
               'msg': 'This field is required.' if error['type'] == 'missing' else
                      'Invalid value. Check this field and try again.'}
              for error in exc.errors()[:20]]
    return JSONResponse({'detail': detail}, status_code=422, headers=_error_headers(request))


@app.exception_handler(Exception)
async def unexpected_error(request: Request, exc: Exception):
    from fastapi.responses import JSONResponse
    import uuid
    reference = uuid.uuid4().hex[:12]
    # Do not log exception strings: provider errors may contain financial data.
    route = getattr(request.scope.get('route'), 'path', '[unmatched route]')
    log.error("Request failed: %s %s %s reference=%s", request.method, route,
              type(exc).__name__, reference)
    return JSONResponse({"detail": f"Something went wrong. Please try again. Reference: {reference}"},
                        status_code=500, headers=_error_headers(request))


@app.get("/ready")
def readiness():
    if not db.is_configured() or (os.getenv("FINIO_ENV") == "production" and
                                 (not _origins or any(not o.startswith("https://") or '*' in o for o in _origins))):
        raise HTTPException(503, "Service is not ready")
    try:
        client = db.get_client()
        for capability in ('finio_ready_v7', 'finio_security_ready'):
            if client.rpc(capability, {}).execute().data is not True:
                raise ValueError('schema not ready')
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
    # every browser request from the real frontend is blocked. and it fails in
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
            "OPENAI_API_KEY is not set. The coach, insights and the model "
            "categoriser fall back to the offline path (measured 20% accuracy "
            "on unseen merchants vs 87% with the model).",
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
            detail="That's a lot of requests in a short time. Give it a minute.",
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
    revision: Optional[str] = Form(None),
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
                        detail=f"File too large. The limit is {MAX_UPLOAD_MB} MB",
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
        saved_cash_balance = None
        saved_statement_balance = None
        if user:
            try:
                _client = db.get_client(user.token)
                state = db.edit_state(_client, user.user_id)
                if state.get("editing_available") is False:
                    raise HTTPException(503, "Your saved data is available. Uploading a statement needs a service update.")
                if state.get("revision") != revision:
                    raise HTTPException(409, "Your saved data changed. Preview the statement again before replacing history.")
                saved_overrides = db.get_overrides(_client, user.user_id)
                saved_custom = db.get_custom_categories(_client, user.user_id)
                saved_llm_cache = db.get_llm_categories(_client, user.user_id)
                profile = db.get_user_profile(_client, user.user_id) or {}
                saved_bracket = profile.get("income_bracket")
                saved_age = profile.get("age")
                saved_targets = db.get_budget_targets(_client, user.user_id)
                saved_goal = db.get_goal(_client, user.user_id) or {}
                saved_summary = (db.get_latest_snapshot(_client, user.user_id) or {}).get('summary_json') or {}
                saved_cash_balance = saved_summary.get('cash_balance')
                saved_statement_balance = saved_summary.get('statement_balance')
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
            # Our own limit, written for the user. say which one they hit.
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
                result['cash_balance'] = saved_cash_balance
                new_statement_balance = result.get('statement_balance') or {}
                if saved_statement_balance and (saved_statement_balance.get('as_of') or '') > (new_statement_balance.get('as_of') or ''):
                    result['statement_balance'] = saved_statement_balance
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
                    expected_revision=revision,
                )
                result["streak"] = streak
                result["persisted"] = True
                _cache_clear_user(user.user_id)
            except ValueError as exc:
                raise HTTPException(status_code=401, detail=str(exc)) from exc
            except Exception as exc:
                if getattr(exc, "code", None) == "40001":
                    raise HTTPException(409, "Your data changed. Preview the statement again before replacing history.") from None
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
                # Only embed merchants we haven't embedded before. a re-upload
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
                # Never block an upload on this, but say so. a bad key or an
                # unrun pgvector migration used to look identical to success.
                warn("merchant embeddings", exc,
                     hint="semantic merchant search will fall back to text match")

        result['balance_view'] = balance_view(result)
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
            detail="No analysis found. Upload a CSV via POST /analyze first",
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
        return {
            "revision": db.edit_state(db.get_client(user.token), user.user_id).get("revision"),
            "count": len(frame), "start": frame.date.min().strftime("%Y-%m-%d"),
        "end": frame.date.max().strftime("%Y-%m-%d"),
        "money_out": round(float(-frame.loc[frame.amount < 0, "amount"].sum()), 2),
        "money_in": round(float(frame.loc[frame.amount > 0, "amount"].sum()), 2),
        "best_effort_pdf": suffix == ".pdf", "disclaimer": DISCLAIMER}
    finally:
        if path:
            os.remove(path)


@app.get("/data/export")
def export_data(user: AuthUser = Depends(get_current_user)):
    require_sensitive_access(user)
    _rate_limit(user.user_id, "/analyze")
    client = db.get_client(user.token)
    out = {"profile": db.get_user_profile(client, user.user_id),
           "ai_preference": preference_status()}
    for table in ("transactions", "snapshots", "budgets", "goals", "streaks", "chat_history", "spend_checks", "merchant_embeddings"):
        ordering = "merchant" if table == "merchant_embeddings" else "id"
        out[table] = db.paged(lambda table=table, ordering=ordering: client.table(table)
                             .select("*").eq("user_id", user.user_id).order(ordering))
    return {"format_version": 1, "data": out}


@app.post("/data/delete")
def delete_data(body: dict, user: AuthUser = Depends(upload_user)):
    if body != {"confirmation": "DELETE"}:
        raise HTTPException(422, "Type DELETE to permanently remove your saved Finio data.")
    require_sensitive_access(user)
    try:
        db.get_client(user.token).rpc("finio_delete_data", {}).execute()
    except Exception as exc:
        log.warning("Data deletion failed (%s)", type(exc).__name__)
        message = ("Could not delete your saved data. Nothing was deleted. Please contact support."
                   if str(getattr(exc, "code", "")) == "P0001" else
                   "Could not confirm deletion. Refresh and try again, or contact support.")
        raise HTTPException(503, message) from None
    _cache_clear_user(user.user_id)
    return {"deleted": True, "account_retained": True}


@app.get('/security/status')
def account_security(user: AuthUser = Depends(get_current_user)):
    try:
        return db.security_status(db.get_client(user.token))
    except Exception:
        raise HTTPException(503, 'Account security checks are unavailable. Please try again later.') from None


@app.get('/ai/preferences')
def ai_preferences(user: AuthUser = Depends(get_current_user)):
    status = preference_status()
    if not status['available']:
        raise HTTPException(503, 'Could not check your AI preference. AI sharing remains off for this request.')
    return status


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
# Limit serialized payloads too: 256 histories with 20,000 rows can exceed
# 750 MB even before Python object overhead. This is a payload budget, not RSS.
_VIEW_CACHE_MAX_BYTES = 16 * 1024 * 1024


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
    for k, value in list(_VIEW_CACHE.items()):
        if len(value) < 3:
            _VIEW_CACHE[k] = (*value, len(json.dumps(value[1]).encode()))
    total = sum(v[2] for v in _VIEW_CACHE.values())
    for k, value in sorted(_VIEW_CACHE.items(), key=lambda kv: kv[1][0]):
        if len(_VIEW_CACHE) <= _VIEW_CACHE_MAX and total <= _VIEW_CACHE_MAX_BYTES:
            break
        total -= value[2]
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

    key = _view_key(user.user_id, period, month, start, end) + f"|{data.get('revision')}"
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
    size = len(json.dumps(result).encode())
    with _VIEW_LOCK:
        # A concurrent write invalidates this in-flight read. Return its
        # result to that caller, but never repopulate the cache with old data.
        if epoch == _VIEW_EPOCH and size <= _VIEW_CACHE_MAX_BYTES:
            _VIEW_CACHE[key] = (time.time(), result, size)
            _cache_evict_locked()
    return result


def _check_revision(body, state):
    if state.get("editing_available") is False:
        raise HTTPException(503, "Your saved data is available. Saving changes needs a service update.")
    expected = str(body.revision) if body.revision else None
    if state.get("revision") and expected is None:
        raise HTTPException(428, "Reload this page before saving so your edit uses the latest data.")
    if expected != state.get("revision"):
        raise HTTPException(409, "Your data changed in another tab. Reload, review the latest values, and try again.")


def _commit_edit(client, user, body, **kwargs):
    _check_revision(body, db.edit_state(client, user.user_id))
    try:
        state = db.commit_edit(client, user.user_id, body.revision, **kwargs)
    except Exception as exc:
        if getattr(exc, "code", None) == "40001":
            raise HTTPException(409, "Your data changed in another tab. Reload before saving.") from None
        code = str(getattr(exc, "code", ""))
        # Log codes, never database messages that may contain statement data.
        safe_code = code if re.fullmatch(r"[A-Z0-9]{5,10}", code) else "unknown"
        log.warning("Atomic edit failed (%s, code=%s)", type(exc).__name__, safe_code)
        if code in {"PGRST202", "PGRST204", "42703", "42883", "42804"}:
            message = "Your data is available, but saving needs a database update. Nothing changed."
        elif code == "42501":
            message = "Finio could not save with this session. Nothing changed. Sign in again and retry."
        else:
            message = "Could not save this edit. Nothing changed. Your input is kept; try saving again."
        raise HTTPException(503, message) from None
    _cache_clear_user(user.user_id)
    return state or {}


@app.post("/changes/undo")
def undo_change(body: RevisionRequest, user: AuthUser = Depends(get_current_user)):
    client, _ = _require_snapshot(user)
    if not db.edit_state(client, user.user_id).get("can_undo"):
        raise HTTPException(409, "No saved edit is available to undo.")
    return _commit_edit(client, user, body, undo=True)


@app.get("/dashboard")
def dashboard(
    period: Optional[str] = None,
    month: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    user: AuthUser = Depends(get_current_user),
):
    client, data = _require_snapshot(user)
    all_tx, available = _available_months(client, user)
    # Coverage comes from actual uploaded rows, never a selected month's end
    # or the time a page was refreshed. This read needs no schema change.
    freshness = {
        "transactions_through": max((str(row.get("date") or "")[:10] for row in all_tx), default="") or None,
        "source": "uploaded_statements",
    }
    if all_tx:
        from modules.merchant_totals import net_merchant_totals
        import pandas as pd
        data = {**data, "averages": {**(data.get("averages") or {}),
                "top_merchants": net_merchant_totals(pd.DataFrame(all_tx).rename(columns={"merchant": "description"}))}}

    resliced = _period_view(
        client, user, data, period=period, month=month, start=start, end=end
    )
    if resliced is None:
        # No period requested → return the stored (default) snapshot as-is.
        data["available_months"] = available
        data["disclaimer"] = DISCLAIMER
        data['balance_view'] = balance_view(data)
        data['data_freshness'] = freshness
        return data

    return {
        "month": data.get("month"),
        "data_freshness": freshness,
        **{k: data.get(k) for k in ("revision", "can_undo", "undo_label", "editing_available", "snapshot", "snapshot_stale")},
        "review": resliced.get("review"),
        "history_review": resliced.get("history_review"),
        "cash_balance": data.get('cash_balance'),
        "statement_balance": data.get('statement_balance'),
        "balance_view": balance_view({**data, 'averages': resliced.get('averages'), 'history_review': resliced.get('history_review')}),
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
        "goal": {**(data.get("goal") or {}),
                 **{k: (view.get("forecast") or {}).get(k) for k in ("current_saved", "target_amount", "target_date")}},
        "metrics": view.get("metrics"),
        "review": view.get("review"),
        "history_review": view.get("history_review"),
        "period": resliced["period"] if resliced else None,
        "available_months": available,
        "disclaimer": DISCLAIMER,
    }


def _all_categories(custom):
    """Spend categories offered in the editor: the built-in buckets plus any the
    user invented. Transfers stay out. that's a flow, not a spend category."""
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
    from modules.pipeline import _restore_full_df
    from modules.review import unresolved_transfers
    from modules.llm_categoriser import build_questions
    overrides = db.get_overrides(client, user.user_id)
    questions, review_keys = [], []
    if txs:
        frame = _restore_full_df(txs)
        questions = build_questions(frame, db.get_llm_categories(client, user.user_id), overrides, limit=20000)
        review_keys = unresolved_transfers(frame, overrides)['key'].tolist()
    return {
        "transactions": txs,
        **{k: data.get(k) for k in ("revision", "can_undo", "undo_label", "editing_available")},
        "categories": _all_categories(custom),
        "custom_categories": custom,
        "pending_questions": questions,
        "review": resliced["review"],
        "review_keys": review_keys,
        "period": resliced["period"],
        "disclaimer": DISCLAIMER,
    }


@app.post("/reclassify")
def reclassify(user: AuthUser = Depends(get_current_user)):
    """Ask the model to categorise the user's merchants again, FROM SCRATCH, and
    return what it would change. Writes nothing.

    Cached classifications are deliberately bypassed. "re-classify" has to mean
    a fresh opinion, otherwise it would just replay the same stored answers.
    Merchants the user has corrected themselves are left alone and reported
    separately, so this can never quietly undo their own decisions.

    The frontend shows the result as a confirmation list; applying it goes
    through POST /overrides like any other edit.
    """
    if not provider_allowed():
        raise HTTPException(403, 'AI sharing is off or could not be verified. Enable it in Profile to request AI reclassification.')
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

    # Merchants the user has already ruled on by name. never re-decide these.
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
        # Only surface confident, genuinely different answers. an unsure guess
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

    # Biggest money impact first. that is what the user should check hardest.
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
        **{k: data.get(k) for k in ("revision", "can_undo", "undo_label", "editing_available")},
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
    state = _commit_edit(client, user, body, resliced=resliced, changes={"budget_targets": targets}, label="Budget limits")
    return {
        **state,
        "targets": targets,
        "budgets": (resliced or {}).get("budgets", data.get("budgets")),
        "disclaimer": DISCLAIMER,
    }


@app.post("/quiz")
def quiz_answer(body: QuizRequest, user: AuthUser = Depends(get_current_user)):
    """Answer (or skip) one categorisation question. An answer becomes a
    permanent override rule. every number recomputes immediately. and the
    question is removed either way."""
    client, data = _require_snapshot(user)

    resliced = None
    changes = {}
    if not body.skip and (body.category or body.flow):
        all_tx = db.get_all_transactions(client, user.user_id)
        overrides = db.get_overrides(client, user.user_id)
        if body.tx_keys is not None:
            from modules.pipeline import _restore_full_df, _records
            own = {t['key']: t for t in _records(_restore_full_df(all_tx))}
            keys = set(body.tx_keys)
            if any(k not in own or own[k]['merchant'].strip().upper() != body.merchant.strip().upper() for k in keys):
                raise HTTPException(422, "These transactions no longer match this review. Reload before answering.")
            overrides = [r for r in overrides if r.get('tx_key') not in keys]
            rules = [{'tx_key': k} for k in sorted(keys)]
        else:
            overrides = [r for r in overrides
                         if not (r.get("match") and r["match"].lower() == body.merchant.lower())]
            rules = [{"match": body.merchant}]
        for rule in rules:
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
        changes["overrides"] = overrides

    remaining = [q for q in db.get_pending_questions(client, user.user_id)
                 if q.get("merchant", "").strip().upper() != body.merchant.strip().upper()]
    changes["pending_questions"] = remaining
    state = _commit_edit(client, user, body, resliced=resliced, changes=changes, label="Transaction review")
    return {**state, "remaining_questions": remaining, "disclaimer": DISCLAIMER}


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
    changes = {"overrides": overrides}
    if body.custom_categories is not None:
        changes["custom_categories"] = body.custom_categories
    state = _commit_edit(client, user, body, resliced=resliced, changes=changes, label="Transaction categories")

    # Marking money that LEFT the account as "income" is incoherent and used to
    # silently wreck the totals. It is now neutralised to a transfer, but the
    # user still needs telling. otherwise their numbers quietly disagree with
    # what they thought they set.
    from modules.pipeline import _restore_full_df
    from modules.data_processor import contradictory_flow_rules
    warnings = contradictory_flow_rules(_restore_full_df(all_tx), overrides)

    return {
        **state,
        "overrides": overrides,
        "custom_categories": db.get_custom_categories(client, user.user_id),
        "metrics": resliced["metrics"],
        "forecast": resliced["forecast"],
        "invest": resliced["invest"],
        "flow_warnings": warnings,
        "disclaimer": DISCLAIMER,
    }


@app.post("/reanalyze")
def reanalyze(body: RevisionRequest, user: AuthUser = Depends(get_current_user)):
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
    state = _commit_edit(client, user, body, resliced=resliced, changes={"overrides": overrides}, label="Re-analysis")
    return {**state, "ok": True, "metrics": resliced["metrics"], "disclaimer": DISCLAIMER}


@app.get("/overrides")
def list_overrides(user: AuthUser = Depends(get_current_user)):
    client, data = _require_snapshot(user)
    return {
        **{k: data.get(k) for k in ("revision", "can_undo", "undo_label", "editing_available")},
        "overrides": db.get_overrides(client, user.user_id),
        "custom_categories": db.get_custom_categories(client, user.user_id),
    }


@app.post("/goal")
def set_goal(body: GoalRequest, user: AuthUser = Depends(get_current_user)):
    client, data = _require_snapshot(user)
    transactions = db.get_all_transactions(client, user.user_id)
    if not transactions:
        raise HTTPException(
            status_code=404, detail="No transactions found. Upload a statement first"
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
    state = _commit_edit(client, user, body, resliced=recomputed,
                         profile={"age": body.age} if body.age is not None else {}, label="Savings goal")
    return {
        **recomputed,
        **state,
        "goal": db.get_goal(client, user.user_id),
        "disclaimer": DISCLAIMER,
    }


def _profile_payload(client, user: AuthUser):
    state = db.edit_state(client, user.user_id)
    profile = db.get_user_profile(client, user.user_id)
    email = profile.get("email")
    if not email:
        try:
            email = db.get_user(user.token).email
        except Exception:
            email = None
    return {
        **state,
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
    state = db.edit_state(client, user.user_id)
    _check_revision(body, state)
    patch = {k: getattr(body, k) for k in ("age", "income_bracket") if k in body.model_fields_set}
    patch["email"] = email
    row = db.get_latest_snapshot(client, user.user_id)
    resliced = None
    if row:
        profile = {**db.get_user_profile(client, user.user_id), **patch}
        goal = db.get_goal(client, user.user_id) or {}
        resliced = analyze_stored(db.get_all_transactions(client, user.user_id),
            goal_amount=goal.get("target_amount"), goal_date=goal.get("target_date"),
            overrides=db.get_overrides(client, user.user_id),
            budget_targets=db.get_budget_targets(client, user.user_id),
            age=profile.get("age"), income_bracket=profile.get("income_bracket"))
    if not row and body.custom_categories is not None:
        raise HTTPException(422, "Upload a statement before adding custom categories.")
    changes = {"custom_categories": body.custom_categories} if body.custom_categories is not None else {}
    _commit_edit(client, user, body, resliced=resliced, changes=changes, profile=patch, label="Profile")
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
    # not-yet-classified transfers). omit it rather than show -1900%.
    rate = round(saved / inc * 100, 1) if inc >= MIN_INCOME_FOR_RATE else None
    return {
        "currency": CURRENCY,
        "income": inc,
        "spent": alltime.get("spent") or 0,
        "saved": saved,
        "savings_rate": rate,
        "top_categories": avg.get("top_categories", []),
        "period_label": "all time",
        "transfer_review": data.get("history_review") or data.get("review"),
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
    snapshot. one LLM call per analysis, not one per dashboard load."""
    _rate_limit(user.user_id, "/insight")
    client, data = _require_snapshot(user)

    if data.get("editing_available") is False:
        return {"text": "Showing the figures calculated from your saved statement.",
                "source": "saved", "disclaimer": DISCLAIMER}

    if not provider_allowed():
        context = (_period_view(client, user, data, period=period, month=month, start=start, end=end)
                   if any([period, month, start, end]) else None)
        facts = ((context or data).get('context') if any([period, month, start, end])
                 else _alltime_insight_context(data))
        if not facts:
            raise HTTPException(404, 'No insight yet. Upload a statement first')
        return generate_insight(facts)

    if not any([period, month, start, end]):
        context = _alltime_insight_context(data)
        if ((context or {}).get("transfer_review") or {}).get("count"):
            return generate_insight(context)
        cached = db.get_cached_insight(client, user.user_id)
        if cached:
            return strip_disclaimer(cached)
        context = _alltime_insight_context(data)
        if not context:
            raise HTTPException(status_code=404, detail="No insight yet. Upload a statement first")
        result = generate_insight(context)
        db.save_cached_insight(client, user.user_id, result)
        return result

    resliced = _period_view(client, user, data, period=period, month=month, start=start, end=end)
    context = (resliced or data).get("context")
    if not context:
        raise HTTPException(status_code=404, detail="No insight yet. Upload a statement first")
    return generate_insight(context)


@app.post("/spend-check")
def spend_check(body: SpendCheckRequest, user: AuthUser = Depends(get_current_user)):
    client, data = _require_snapshot(user)
    resliced = _period_view(client, user, data, period=body.period, month=body.month)
    metrics = spending_metrics((resliced or data)['metrics'], data)
    if body.current_balance is not None:
        metrics["latest_balance"] = body.current_balance
        metrics["balance_date"] = date.today().isoformat()
        metrics["user_entered_balance"] = True
        metrics['balance_source'] = 'entered'
    result = check_purchase(None, metrics, body.amount, body.days_ahead)
    result["merchant"] = body.merchant
    db.save_spend_check(client, user.user_id, result)
    return result


@app.post('/balance')
def save_cash_balance(body: CashBalanceRequest, user: AuthUser = Depends(get_current_user)):
    client, _ = _require_snapshot(user)
    saved = None if body.amount is None else {
        'amount': round(body.amount, 2), 'as_of': (body.as_of or date.today()).isoformat(),
    }
    return _commit_edit(client, user, body, changes={'cash_balance': saved}, label='Current balance')


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
    rows = db.get_chat_history(client, user.user_id, chat_id=chat_id)
    revision = db.edit_state(client, user.user_id).get("revision")
    for row in rows:
        row["proposed_actions"] = [
            {**fact["action"], "expired": fact["action"].get("revision") != revision}
            for fact in row.get("verified_facts", [])
            if fact.get("kind") == "proposal" and isinstance(fact.get("action"), dict)]
        row["verified_facts"] = [fact for fact in row.get("verified_facts", []) if fact.get("kind") != "proposal"]
    return {"history": rows}


@app.get("/chats")
def chats(user: AuthUser = Depends(get_current_user)):
    """The user's coach conversations, newest first."""
    client = db.get_client(user.token)
    return {"chats": db.list_chats(client, user.user_id)}


@app.post("/coach")
def coach(body: CoachRequest, user: AuthUser = Depends(get_current_user)):
    _rate_limit(user.user_id, "/coach")
    client, data = _require_snapshot(user)

    if data.get("editing_available") is False:
        raise HTTPException(503, "Your saved chats are available. New coach replies need a service update.")

    from modules.coach_scope import resolve_coach_scope
    all_transactions, available = _available_months(client, user)
    scope = resolve_coach_scope(body.message, body.period, body.month, available)
    if scope.get("error"):
        return {"text": scope["error"], "source": "calculated", "verified_facts": [],
                "proposed_actions": [], "chat_id": body.chat_id or "default"}
    resliced = _period_view(client, user, data, period=scope["period"], month=scope["month"])
    context = (resliced or data).get("context")
    if not context:
        raise HTTPException(status_code=404, detail="No coach context. Upload a CSV first")
    source_metrics = spending_metrics((resliced or data).get('metrics') or {}, data)
    context = {**context, 'read_scope': scope['label'],
    'available_balance': balance_view(data),
    "latest_balance": source_metrics.get("latest_balance"),
    "balance_date": source_metrics.get("balance_date"),
    **{k: source_metrics.get(k) for k in ('daily_burn_rate', 'balance_source', 'user_entered_balance', 'has_spending_baseline', 'transfer_review_count')}}
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

    response = coach_chat(body.message, context, history=history, transactions=transactions,
                          proposal_transactions=all_transactions)

    for fact in response.get("verified_facts", []):
        fact["revision"] = data.get("revision")
    for proposal in response.get("proposed_actions", []):
        proposal["revision"] = data.get("revision")
    response['text'] = redact_credentials(response['text'])
    db.append_chat(client, user.user_id, "user", redact_credentials(body.message), chat_id=chat_id)
    # Pending actions retain the revision they were created from. History can
    # restore them, but must never refresh an old proposal to a new revision.
    metadata = response.get("verified_facts", []) + [
        {"kind": "proposal", "action": action} for action in response.get("proposed_actions", [])]
    db.append_chat(client, user.user_id, "assistant", response["text"], chat_id=chat_id, verified_facts=metadata)

    # Name the thread once. after the first exchange. so the sidebar shows a
    # short AI title instead of the raw first question.
    chat_title = db.get_chat_title(client, user.user_id, chat_id)
    if not chat_title:
        chat_title = title_chat(body.message, response.get("text"))
        try:
            db.set_chat_title(client, user.user_id, chat_id, chat_title)
        except Exception as exc:
            warn("chat title save", exc, hint="check the privacy_and_chat_contracts migration")

    return {
        **response,
        "chat_id": chat_id,
        "chat_title": chat_title,
        "quick_questions": QUICK_QUESTIONS,
    }
