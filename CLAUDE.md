# Finio — project guide for Claude Code

Finio is an AI personal-finance web app. A user uploads a bank statement (CSV
or PDF); the backend parses it, categorises every transaction, and returns
metrics, spending patterns, recurring bills, budgets, an invest plan, a
spend-check verdict and an AI coach that can act on the data.

Currency defaults to AUD (`FINIO_CURRENCY` / `FINIO_LOCALE`), and the merchant
rules and knowledge base are Australian — but the product copy is not
country-specific, so don't reintroduce "for Australians" wording.

## Learning mode (important)

The owner (Ammar) is building this to learn.

- Explain what you are doing and why.
- Prefer small, reviewable changes; don't rewrite whole modules unless asked.
- **Verify by running things, not by reading them.** Almost every real bug in
  this codebase was found by executing the pipeline over real data, probing an
  endpoint, or measuring a rendered layout — never by reading the code and
  reasoning about it. A clean read means very little here.

## Status

Frontend/backend built. Launch hardening includes atomic uploads, explicit replacement,
refund/balance preservation, pagination, authenticated bounded uploads and account controls.
145 baseline checks plus launch regressions, database/RLS tests and an evaluation harness.
Deployment config exists (`DEPLOY.md`). Live migrations and disposable-account Auth/RLS were verified on 29 October 2026. Hosted deployment and email delivery still need separate acceptance checks.

```bash
source .venv/bin/activate  # run.sh prefers .venv, then the legacy venv
python -m tests.test_full_suite     # 145 tests, offline (no API calls)
python -m pytest tests/test_launch_safety.py -q
npm ci --ignore-scripts && npm run test:database && npm run check:frontend
npm run test:refresh  # calendar rollover, saved edits, cross-tab refresh and read races
python -m eval.run_eval             # categoriser + retrieval + coach quality
```

Tests set `FINIO_DISABLE_LLM=1` so they stay fast, free and offline. `run_eval`
is what measures the model path.

## Conventions

- Backend: Python + FastAPI (`main.py`), logic in `modules/`, constants in `config.py`.
- Frontend: **vanilla HTML/CSS/JS, no framework**, in `frontend/`.
- Never commit `.env`.
- Python 3.11+. Install hashed `requirements.lock`; regenerate and re-audit after upgrades.
- Apply schema + migrations 001 through 006, 20261029120515 and 20261101060230 before running this release. `/ready` gates deployment.
- Anonymous upload is forbidden; use `/demo` or `frontend/demo.html` for sample data.
- Every page showing financial figures carries:
  **"General information only, not financial advice"** — as its own element,
  never appended to a sentence.
- Escape every dynamic value interpolated into `innerHTML` (`escapeHtml`).
- Money via `formatAUD` / `formatMoney`; never hardcode a currency or locale.

## Architecture notes that are easy to get wrong

- **Transfers are not spending.** Money moved between your own accounts or to a
  person is neither income nor expense. A row categorised `Transfers` must
  never reach `total_spent` — `reconcile_flow_contradictions` enforces this.
- **The categoriser is model-first.** The LLM classifies every merchant;
  keyword rules and Naive Bayes are fallbacks. Measured on merchants no rule
  matches: 87% with the model, 20% offline. Do not put the rules back in front.
- **A low-confidence answer never sets a category.** It falls through and
  becomes a quiz question.
- **Edits recompute the stored snapshot atomically.** Every edit sends the revision it read; stale writes return 409. Never fetch a fresh revision merely to submit an old form. `/reanalyze` refreshes older analysis logic. One saved edit can be undone; uploads clear undo.
- **Rate limiter and period-view cache are in-process.** One worker only.
- **Upload admission precedes body buffering.** One active upload per account and two per process. Keep authentication/admission outside the body-limit middleware; route dependencies run after multipart parsing. Admission cleanup must also cover cancellation and disconnects.
- **Patterns refresh is read-only.** Check at local midnight, on return to a stale tab and after successful edits. Never run AI or write a new snapshot just because the day changed. Show actual uploaded transaction coverage separately from the last check time; new spending still requires a statement upload.
- **Layout breakpoints must be container queries**, not viewport media queries:
  opening the coach shrinks `<main>` without changing the window.

## API

Base URL (local): `http://127.0.0.1:8000`. CORS is **explicit origins**, set by
`FINIO_ORIGINS` (defaults to localhost — see `DEPLOY.md`).

Auth: Supabase. The frontend signs in with the JS client and sends
`Authorization: Bearer <access_token>` on everything except `GET /`.

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/` | health (no auth) |
| POST | `/analyze` | upload a statement (CSV/PDF, 10 MB cap); persists when authed |
| POST | `/reanalyze` | recompute the snapshot from stored transactions |
| GET | `/dashboard` | metrics, analysis, bills, budgets, invest, personality, averages |
| GET | `/transactions` | every categorised transaction + the quiz questions |
| GET · POST | `/overrides` | the user's reclassification rules |
| POST | `/reclassify` | ask the model to re-categorise; returns a **proposal**, writes nothing |
| POST | `/quiz` | answer or skip one categorisation question |
| GET · POST | `/budgets` | budget rows and the user's own limits |
| POST | `/goal` | set the savings goal |
| GET · POST | `/profile` | age, income bracket, custom categories, streak |
| GET | `/insight` | one-line AI recap (cached in the snapshot) |
| GET | `/invest` | 50/30/20, readiness gates, ETFs, first $1,000 |
| POST | `/spend-check` | can I afford this? → green / yellow / red |
| GET | `/spend-check/history` | past checks (needs migration 003) |
| POST | `/coach` | chat; can propose reclassifications, budgets, goals, profile edits |
| GET | `/coach/history` · `/chats` | messages for one chat; the chat list |

Notes:
- `/analyze` must run first, or the data endpoints return
  `404 {"detail": "No analysis found — upload a CSV via POST /analyze first"}`.
- `401` = missing/expired token → send the user to login.
- `413` = upload over 10 MB. `429` = rate limited (`/coach`, `/analyze`,
  `/insight`, `/reclassify`).
- With `FormData`, do **not** set `Content-Type` manually.

## AI writes are always confirmed

The coach's `propose_*` tools and `/reclassify` return proposals. Nothing is
written until the user clicks Apply. Keep it that way.

## Database

Migration order is in `DEPLOY.md`: `project-plan/supabase_schema.sql`, then
`migrations/001` → `006`. Required migrations are gated by `/ready`; never fall back to partial writes when an atomic RPC is unavailable. Migration 006 fixes budget-month writes on live databases with a date column; test both text and date layouts.

Apply `migrations/20261029120515_privacy_and_chat_contracts.sql` after 006. It adds authenticated owner DELETE policies for chat history, streaks and profiles, checks deletion atomically, and allows persisted chat titles. `/ready` requires `finio_ready_v7()`. Test with the deployed SELECT/INSERT/UPDATE-only policy layout, not just base FOR ALL policies. The migration was applied and verified on the configured Supabase project on 29 October 2026.


Account security now includes optional customer TOTP, a separate MFA verification
page and recent verification for Finio export/deletion. Migration
`20261101060230_account_security.sql` enforces live sessions and opted-in MFA
through restrictive RLS policies. It was applied on 1 November 2026. `/ready`
requires both `finio_ready_v7()` and `finio_security_ready()`. Keep Uvicorn access
logs off. See `SECURITY.md`, `scripts/check_service.py` and
`scripts/recovery_drill.py` for monitoring and synthetic recovery checks. No public
application deployment or independent assessment was performed by this work.

The owner chose manual encrypted backups on the Free plan. See
`MANUAL-BACKUPS.md` and `scripts/manual_backup.py`; never store a decryption
identity or dump in the repository. The recovery drill uses the same encrypted
stream helper. The first real encrypted backup and isolated local restore were
verified on 2 November 2026, with all nine financial tables matching live
fingerprints. The encrypted dump, encrypted recovery companion and both receipts
were copied to owner-only Drive storage on 3 November, with the downloaded dump
checksum verified. The private key stayed off Drive. An independent protected
key copy and hosted-project recovery remain owner tasks. Never restore over production
as a test or put database passwords into command arguments, chat or logs.

Account AI sharing now defaults off. Profile saves explicit versioned consent
in live Auth user metadata; HTTP calls fail closed before provider transmission
when consent is missing or unavailable. This is not a row authorization rule.
No new schema migration is required. Linux PDF workers require Landlock ABI 3+
and libseccomp, deny filesystem writes/sockets/process execution and fail closed
in production when unavailable. Run the Linux sandbox probe on the target host.
Private transition-only SMTP monitoring is prepared in scripts/monitor_service.py;
the operator still needs a host URL, SMTP provider, protected credentials, schedule
and delivery test. On 6 November 2026, the owner withdrew permission to publish
their personal email as the privacy/support contact. Keep the private alert
recipient separate; a public contact channel remains unresolved.

Deployment preparation is now authorized, with a $0 spending limit. A separate
Sydney Supabase test project, jaoghiqapxhmfromyeyt, was created in the existing
organization and all nine reviewed schema/migration files were applied. Both
readiness functions pass; all nine financial tables have RLS/restrictive policies.
Never use the live recovery dump as staging fixtures. Vercel project
finio-security-staging has authentication protection for all deployment URLs
and currently points to a Render staging API. Staging is not a public
production launch. Paid services and independent testing are not authorized
under the $0 limit. See the deployment evidence.
