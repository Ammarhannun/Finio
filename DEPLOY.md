# Finio launch checklist

Local hardening is implemented; this is **not evidence that production is ready**.
Use staging and synthetic statements first. No live migrations have been applied by this review.

## 1. Reproduce local checks

Use Python 3.11 and Node 22. Keep secrets in ignored `.env`.

```bash
python3.11 -m venv venv
source venv/bin/activate
pip install --require-hashes -r requirements.lock
pip install pytest pip-audit
FINIO_DISABLE_LLM=1 python -m tests.test_full_suite
FINIO_DISABLE_LLM=1 python -m pytest tests/test_launch_safety.py -q
pip-audit -r requirements.lock --no-deps --disable-pip
npm ci --ignore-scripts
npm run check:frontend
npm run test:database
docker build -t finio .
```

The database tests execute actual SQL in isolated in-memory PostgreSQL/PGlite,
with simulated Supabase auth/grants: migration reruns, atomic rollback, refund
flow, anonymous RPC denial and two-user RLS. They do not certify hosted Supabase.
CI repeats these checks plus a Linux Docker build. The local Docker build was
not verified during this review because the Docker daemon was unavailable.

## 2. Back up, then migrate

Take a backup and verify its restoration procedure. In the target Supabase SQL editor, run:

1. `project-plan/supabase_schema.sql`
2. `migrations/001_pgvector.sql`
3. `migrations/002_chats.sql`
4. `migrations/003_spend_checks.sql`
5. `migrations/004_launch_safety.sql`

All are rerunnable. Migration 004 is required: an authenticated RPC commits the
transactions, snapshot, goal, streak and budgets atomically. There is no separate
delete/insert fallback. It also persists refund flow, balances and row ordering.
Old releases did not save balances: re-upload the original statement if needed.
Re-analyse stale snapshots after migrating.

Do not run migrations on app startup. Retain the pre-migration backup and previous
image. Prefer a forward repair; restoring a backup overwrites subsequent changes.

Optional KB indexing: run `python -m scripts.index_kb` separately with a service-role
key. Do not provide that admin key to the web process.

## 3. Backend hosting

Use the repository root and Dockerfile (hashed dependencies, non-root runtime).
Railway: one worker, **one replica**, health check `/ready`; `/` is liveness only.
The existing `railway.json` expresses these settings. Verify effective settings
in the host dashboard: current Railway docs deprecate Config as Code for new
infrastructure workflows.

| Server variable | Value |
| --- | --- |
| `SUPABASE_URL` | Target Supabase project |
| `SUPABASE_ANON_KEY` | Publishable/anon key, not service-role |
| `OPENAI_API_KEY` | Server secret for AI; optional offline fallback |
| `FINIO_ENV` | `production` |
| `FINIO_ORIGINS` | Exact HTTPS frontend origin(s), comma-separated, no trailing slash/wildcard |
| `FINIO_CURRENCY` / `FINIO_LOCALE` | `AUD` / `en-AU`, matching frontend |
| `FINIO_DISABLE_LLM` | `0` for AI, `1` for offline tests |
| `PORT` | Host-supplied; container defaults to 8000 |

Use a dedicated OpenAI project with spend alerts/limits. Uploads require login,
are rate limited, allow two concurrent analyses per process, and cap new LLM
classification at 600 merchants and embeddings at 500 merchants per upload.
Set host-level request/concurrency limits and Supabase signup abuse protection
before public launch. Add a shared limiter/cache before scaling workers/replicas.
In-process controls alone do not prevent mass-account abuse.

## 4. Frontend hosting

Vercel root: `frontend`. Build: `node build-config.mjs`. Output: `.`.
`vercel.json` supplies these settings. Set frontend build variables:

- `FINIO_API_BASE_URL`: HTTPS backend origin, no path.
- `FINIO_PUBLIC_SUPABASE_URL`: same project as backend.
- `FINIO_PUBLIC_SUPABASE_KEY`: publishable or legacy anon key only.
- `FINIO_CURRENCY=AUD`, `FINIO_LOCALE=en-AU` (optional defaults).

Build fails for missing values or secret/service-role keys. Never paste server
secrets into `frontend/config.js`; that remains local public configuration.
A missing production API origin is an error, not a fallback to a nonexistent `/api`.

## 5. Auth and privacy configuration

Enable email confirmation in Supabase Auth. Set the frontend Site URL and allow
the exact confirmation/recovery destinations:
`https://YOUR-FRONTEND/login.html` and
`https://YOUR-FRONTEND/login.html?reset=1`.
Verify the final redirects with Vercel clean URLs enabled. Configure production
SMTP and a strong server password policy (signup UI asks for 12 characters).
Test confirmation, expired links, resend throttling, recovery and sign-out.

Before real users, review `frontend/privacy.html` against actual provider settings.
Add operator identity, privacy/support contact, retention periods, account-closure
process and any required terms. This page describes implementation, not compliance.
Financial-data deletion retains the Supabase login and does not instantly erase
provider backups. Define backup/log retention and a verified deletion-support process.
Do not log statements, prompts, keys or financial payloads.

## 6. Live staging acceptance gate

- `/ready` returns 200 with the required schema, 503 if it is missing.
- Anonymous dashboard/upload/preview requests return 401.
- Landing, demo and privacy work without login on mobile and desktop.
- Two synthetic users can sign up, confirm email, recover access and sign out.
- A uploads salary, expense, transfer and refund fixtures; verify the review count,
  date range, totals and balance, including after reload.
- Upload more than 1,000 rows and verify the full history is retained.
- Set goal, situation, custom budget and correction, then explicitly replace history.
  Verify preferences survive and failed saves retain previous data.
- B cannot read/update/delete A's data through the app **or directly through
  Supabase REST using B's JWT**.
- Test coach, budget/goal editing, quiz corrections and spend check with a
  current balance. Verify that yesterday's statement alone cannot show an
  approval, and that an entered balance is not saved in check history.
- Export A's data and verify ownership. In a disposable account, type DELETE;
  verify financial data/chats are gone but login remains.
- Test supported CSV/text PDF, malformed/oversize files and encrypted/scanned PDFs.
  Compare PDF preview count, dates, money-in and money-out with the source
  statement; PDF extraction is best-effort and may not support every layout.
- Verify actual CORS origins, error monitoring, backup restoration and rollback.

Do not invite real users until this gate passes. Local tests do not certify
hosted auth, email delivery or every bank's PDF format.

## Maintenance

Regenerate and audit dependency locks regularly:
`uv pip compile requirements.txt --python 3.11 --generate-hashes -o requirements.lock`.
Rerun all checks after updates. Keep frontend CDN versions pinned.

Official references: [Railway Dockerfiles](https://docs.railway.com/builds/dockerfiles),
[health checks](https://docs.railway.com/deployments/healthchecks),
[configuration reference](https://docs.railway.com/config-as-code/reference).
