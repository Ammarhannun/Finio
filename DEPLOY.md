# Finio launch checklist

Local hardening is implemented; this is **not evidence that production is ready**.
Use staging and synthetic statements first. Migration 006 was applied to the existing Finio Supabase project on 28 October 2026 to correct budget-month type compatibility. Live save/undo passed inside an explicitly rolled-back transaction; financial-table fingerprints were unchanged.

## 1. Reproduce local checks

Use Python 3.11 and Node 22. Keep secrets in ignored `.env`.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install --require-hashes -r requirements.lock
pip install pytest pip-audit
FINIO_DISABLE_LLM=1 python -m tests.test_full_suite
FINIO_DISABLE_LLM=1 python -m pytest tests/test_launch_safety.py tests/test_audit_regressions.py tests/test_verified_edits.py tests/test_schema_compatibility.py tests/test_review_and_invest.py tests/test_cash_balance.py tests/test_journey_fixes.py tests/test_pattern_refresh.py tests/test_security_hardening.py tests/test_account_security.py tests/test_manual_backup.py tests/test_ai_policy.py tests/test_monitor_service.py tests/test_hosted_security.py tests/test_upload_admission.py tests/test_acceptance_target.py -q
pip-audit -r requirements.lock --no-deps --disable-pip
npm ci --ignore-scripts
npm audit --audit-level=high
npm run check:frontend
npm run test:upload
npm run test:facts
npm run test:layout
npm run test:refresh
npm run test:auth
npm run test:dashboard
npm run test:preferences
npm run test:database
docker build --pull --no-cache -t finio .
```

The database tests execute actual SQL in isolated in-memory PostgreSQL/PGlite,
with simulated Supabase auth/grants: migration reruns, atomic rollback, refund
flow, anonymous RPC denial and two-user RLS. They do not certify hosted Supabase.
CI repeats these checks plus a Linux Docker build. Local Linux container smoke checks are recorded in the audit outputs; repeat them for the deployed image.

## 2. Back up, then migrate

Take a backup and verify its restoration procedure. Drain or pause requests to the old API before migration 005, then deploy the matching API and frontend together. The previous API does not implement revision checks; do not mix old and new writers. A rollback to the older app needs a compatible database plan rather than only an image switch.

In the target Supabase SQL editor, run:

1. `project-plan/supabase_schema.sql`
2. `migrations/001_pgvector.sql`
3. `migrations/002_chats.sql`
4. `migrations/003_spend_checks.sql`
5. `migrations/004_launch_safety.sql`
6. `migrations/005_verified_edits.sql`
7. `migrations/006_budget_month_compatibility.sql`
8. `migrations/20261029120515_privacy_and_chat_contracts.sql`
9. `migrations/20261101060230_account_security.sql`

All are rerunnable. Migration 005 adds revision checks, atomic edits, persistent one-level undo, guarded insight caching, and saved coach facts. Migration 006 removes text casts from budget-month writes so both the original text schema and existing date schemas work. The final migration adds owner deletion policies and persisted chat titles. `/ready` checks both `finio_ready_v7()` and `finio_security_ready()` and fails if either capability is missing. A migration-history listing alone is not proof; earlier SQL may have been applied manually.

Deploy the matching frontend and API after the migrations; older clients without revisions receive 428/409 and must reload. Upload replacement requires a new preview, and clears undo because the underlying history changes. Undo restores the previous snapshot, goal, budgets and editable profile fields together. It does not undo uploads or deletion.

Migration 004 remains required: an authenticated RPC commits the
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

Optional, and safe to leave unset:

| Server variable | Value |
| --- | --- |
| `SUPABASE_JWT_SECRET` | Verifies the access token locally (HS256) instead of calling Supabase on every request. Unset means one extra network round-trip per request; RLS still guards the data either way. |
| `SUPABASE_SERVICE_ROLE_KEY` | Bypasses RLS. Only `scripts/index_kb.py` needs it, to write the shared knowledge base. **Do not set it on the web service**: nothing there uses it, and it is the one key that can read every user's rows. |
| `FINIO_LOG_LEVEL` | Defaults to `INFO`. |
| `WEB_CONCURRENCY` / `UVICORN_WORKERS` | Leave unset. The rate limiter and period-view cache are per-process, so more than one worker multiplies the effective limit; the app logs a warning if you do. |

Use a dedicated OpenAI project with spend alerts/limits. Uploads require login,
are rate limited, allow two concurrent analyses per process, and cap new LLM
classification at 600 merchants and embeddings at 500 merchants per upload.
Set host-level request/concurrency limits and Supabase signup abuse protection
before public launch. Add a shared limiter/cache before scaling workers/replicas.
In-process controls alone do not prevent mass-account abuse.

AI requests also share four in-process provider slots and bounded completion
lengths. Common identifiers are masked before categorisation, coaching and
embedding requests. This is pattern-based minimisation, not anonymisation.
Optional completion storage is disabled, but provider retention still applies.
AI sharing is off by default for every account. The owner enables it in Profile;
the backend checks live Auth metadata before new HTTP provider requests. Missing
or unverifiable consent fails closed. Global `FINIO_DISABLE_LLM=1` still takes
precedence. Deploy the matching Profile module and refreshed CSP hashes with
the API. This preference uses Auth metadata and needs no new schema migration.
`SUPABASE_ANON_KEY` must never contain a secret/service-role key; the web client
rejects these. The optional local JWT verifier requires issuer, audience, expiry,
issued-at time, an authenticated role and a valid subject. It does not prove
that a session remains active after revocation. Keep token lifetimes bounded
and retain the new database session checks. Export/deletion also require authentication within five minutes.

PDF processing runs in a separate process with a 20-second wall timeout,
12-second CPU limit and an 8 MB result limit. Linux additionally limits its
data segment to 512 MB. The worker receives no server API keys in its environment.
Linux also applies Landlock filesystem and seccomp syscall restrictions before
parsing. It allows reads of only the input and runtime libraries, denies writes,
sockets, execution and process inspection, and requires a single-thread worker.
Production PDF imports fail closed if Landlock ABI 3+ or libseccomp is unavailable
or the host blocks installation. CSV imports continue to work. Run the image
with non-root, read-only files, dropped capabilities, no-new-privileges and a
bounded writable temporary directory. Run tests/pdf_sandbox_probe.py against
the actual host image before enabling PDFs. Development macOS PDF processing
has resource limits but no kernel sandbox.

See `SECURITY.md` for implemented boundaries, incident procedures and the
remaining owner and independent-assessment work. This release requires
`20261101060230_account_security.sql`; never release the new API without it.

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
The build also checks both configured service origins against `connect-src` in
`frontend/vercel.json`. Railway and Supabase default domains are already covered.
For a custom API or Supabase domain, add its exact HTTPS origin to that directive
before building. Do not replace the policy with a broad `https:` allowance.

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
- Set a goal, age, custom budget and correction, then explicitly replace history.
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

### Private privacy-request form

The public privacy page uses `privacy-contact.js` and `/privacy/contact`. The
operator's inbox is configured only on the backend and is never returned by
the public configuration endpoint or included in frontend build values.

Configure these protected backend environment values before enabling the form:

- `FINIO_CONTACT_TO`: the operator's private receiving inbox.
- `FINIO_CONTACT_FROM`: a sender on the verified Resend domain.
- `FINIO_CONTACT_RESEND_KEY`: a separate sending-only key restricted to that domain.
- `FINIO_CONTACT_TURNSTILE_SITE_KEY`: the public site key for the actual frontend hostname.
- `FINIO_CONTACT_TURNSTILE_SECRET`: the matching server secret, never a frontend variable.

The form stays unavailable if any value is missing. Production also rejects
Cloudflare dummy keys and multiple worker processes. The existing database
readiness endpoint does not establish contact-form delivery readiness.
Before launch, verify actual delivery to the private inbox and test incorrect,
expired and reused Turnstile tokens. Confirm the widget hostname and action
`privacy-contact`, the exact CORS origin, and the Turnstile script/frame CSP.
Changing hosts does not automatically carry over `vercel.json` response headers.

Delivery uses Resend's HTTPS API; no raw SMTP is needed on the application host.
The message is plain text with no attachments and a fixed destination. The reply
address is unverified and must not authorize account access, disclosure or deletion.
Recognisable credentials are redacted, but free text can still contain personal
information. The operator must define a retention and identity-verification process
for the inbox. Requests are not saved in Finio's financial database or sent to AI.

Limits are three attempts per client per hour, twenty total per rolling day and
one outbound request at a time. Failed attempts count. These limits are in memory,
reset on restart and require one worker/replica. Do not trust arbitrary forwarded
IP headers. Check actual proxy behavior and add platform abuse controls before
public use. The user's $0 budget does not authorize email overages or an upgrade.

The UI confirms submission only after provider acceptance. Acceptance is not a
guarantee of inbox delivery. An uncertain network failure preserves the draft and
does not falsely claim that no email was sent. No live form delivery has been
verified merely by passing local tests.

Regenerate and audit dependency locks regularly:
`uv pip compile requirements.txt --python 3.11 --generate-hashes -o requirements.lock`.
Rerun all checks after updates. Keep frontend CDN versions pinned.

Official references: [Railway Dockerfiles](https://docs.railway.com/builds/dockerfiles),
[health checks](https://docs.railway.com/deployments/healthchecks),
[configuration reference](https://docs.railway.com/config-as-code/reference).


## Hosted acceptance gate for this release

Use two new disposable accounts in **staging**, with working inboxes. Never use a
personal account or bank statement. The local database tests simulate auth and do
not prove hosted JWT verification, actual grants, email delivery or deployment.

1. Apply all migrations through `20261101060230_account_security.sql`, deploy the matching API/frontend, and verify both readiness capabilities via `/ready`. Deployment requires a separate instruction from the owner.
2. Sign up and sign in through the browser. Confirm email and sign out/in in both accounts.
3. Load each account's access token privately into `FINIO_TEST_TOKEN_A` and
   `FINIO_TEST_TOKEN_B`. Do not paste tokens into chat, commit them or print them.
4. Run the opt-in synthetic API checks against the staging API:

   ```bash
   python -m scripts.hosted_acceptance --base-url https://YOUR-STAGING-API \
     --confirm-disposable-test-accounts
   ```

   It refuses accounts with existing saved data. It uploads three synthetic rows
   per account, verifies exact refund totals and persistence, tests version
   conflicts and undo, checks exports and two-user separation, and deletes only
   the disposable accounts' Finio data. Authentication accounts remain. A failed
   run may leave synthetic data to inspect or delete in those test accounts.
5. In two browser tabs, save an edit in one and try saving the old form in the
   other. Confirm the conflict message, preserved draft and correct values after reload.
6. Reopen a chat and confirm its calculated facts retain their original date range.
7. Request recovery mail, follow the real inbox link, and complete password entry
   yourself. Verify the new password, configured redirect URL and expired-session flow.
8. After deletion, refresh and sign in again: no old financial data, undo backup,
   export rows or chat messages should return. Check the second account remains intact.

These hosted checks are **pending** until the staging URL/accounts and inbox access
are available. Local tests and a synthetic UI preview must not be called a live
account acceptance pass.

Apply `migrations/20261029120515_privacy_and_chat_contracts.sql` after 006. It adds authenticated owner DELETE policies for chat history, streaks and profiles, checks deletion atomically, and allows persisted chat titles. `/ready` requires `finio_ready_v7()`. Test with the deployed SELECT/INSERT/UPDATE-only policy layout, not just base FOR ALL policies. The migration was applied and verified on the configured Supabase project on 29 October 2026.


## Account security release (1 November 2026)

The new security migration was applied to the configured Supabase project and
both readiness capabilities returned true. Financial-table row counts were
unchanged. This is not an application deployment. The new frontend and API must
be released together after the hosted acceptance gate is completed.

Profile now supports TOTP setup and removal. Enrolled accounts complete an
additional sign-in page. The database checks `auth.sessions` and verified factors
for all nine financial tables, preserving existing ownership policies. A private,
restricted helper reads only the caller's Auth state. Export and deletion through
the Finio API require authentication in the previous five minutes. A reset email
does not remove MFA. An operator recovery process must verify identity before
removing a lost factor; do not add a password-only MFA bypass.

Before release, test enrollment, incorrect codes, a new password sign-in,
AAL1 denial through REST, successful AAL2 access, factor removal, cancellation,
expired verification, export/deletion and revoked-token denial. Use only new
synthetic accounts. Verify real inbox delivery separately; an admin-generated
recovery token is not evidence that email arrives.

### Monitoring and private logs

Use `--no-access-log` for Uvicorn. Dockerfile, Railway and the local launcher do
this. Log route templates and status codes only. Configure the public proxy and
host to omit query strings, tokens, cookies, request/response bodies and uploaded
filenames. Agree a retention period and restrict access before launch.

Run the read-only probe on the eventual host's scheduler:

```bash
python -m scripts.check_service --base-url https://YOUR-API
```

Before promotion, run the anonymous deployment checks too:

```bash
python -m scripts.hosted_security --api-url https://YOUR-API --frontend-origin https://YOUR-FRONTEND
```

This streams headers only, never response bodies. It checks liveness/readiness,
no-store and security headers, unauthenticated denial, upload authentication,
exact CORS origins, rejection of untrusted origins and disabled production docs.
It does not replace the authenticated acceptance or real inbox/browser checks.

It exits 1 when liveness or readiness fails. Route that failure to the operator's
chosen monitoring destination. No hosted schedule or alert channel is configured
by the code. Test notification delivery before relying on it. Review Auth failures,
new admin access, 5xx errors, resource pressure, backup failures and provider spend.

For transition-only email alerts, prepare an owner-readable private JSON config
outside this repository with `base_url`, `smtp_host`, `smtp_port`, `smtp_mode`,
`smtp_username`, `from_email` and `to_email`. SSL requires port 465; mandatory
STARTTLS requires 587. Put `FINIO_ALERT_SMTP_PASSWORD` in the scheduler's secret
environment, then run `python -m scripts.monitor_service --config /PRIVATE/monitor.json`.
It stores an owner-only sibling state file and lock. Run it on the host schedule
after configuring the destination. Exit 0 means healthy, 1 unhealthy, 2 a config,
network or delivery failure. Test failure, recovery and mail delivery before
relying on it. A private recipient draft exists in the owner's audit workspace;
there is no configured mail provider, schedule or public API yet.
The monitoring scripts are separate operator tools and are not copied into the
web application image. Install them in the protected scheduler environment.

### Recovery drill

Install `age` from its official distribution and use Docker Desktop. Then run:

```bash
python -m scripts.recovery_drill --age-directory /ABSOLUTE/PATH/TO/AGE-BINARIES \
  --output-directory /PRIVATE/PATH/OUTSIDE/THE/REPOSITORY
```

Use `--help` for a custom Docker host/config. The script uses an isolated Docker
network, no host port, a nonpersistent test database and invented records. It
pipes a custom-format dump directly into encryption, restores it into a second
database, compares all fixture tables, checks access policies and rejects tampered
ciphertext. Temporary test identities are removed. It never backs up production.

The owner selected manual encrypted backups on Free. Follow `MANUAL-BACKUPS.md`
and `python -m scripts.manual_backup --help` for the prepared command. It prompts
privately, verifies TLS, streams into encryption and publishes only a completed
file. The first real encrypted backup and local isolated restore passed on
2 November 2026. All nine financial tables matched source row fingerprints;
restored readiness, ownership and session-revocation checks passed. See the
verification receipt and encrypted recovery-metadata companion in the owner's
backup directory. Preserve those with the matching encrypted dump.
Establish independent key recovery and a manual backup schedule. Store public recipients and backup
artifacts off-host, keep private decryption keys out of the repository and web
service, restrict database credentials and record retention and deletion periods.
A PostgreSQL logical dump alone does not back up every hosted setting or Storage
object. Inventory Auth, Storage, functions, project configuration and operator
recovery material too. Restore into an isolated project, measure acceptable data
loss/recovery time, and check RLS/MFA before permitting access. Never restore over
the running project as a test. The encrypted dump, recovery companion and both
receipts were copied to owner-only Google Drive storage on 3 November 2026, and
the downloaded dump matched its SHA-256 checksum. The private key stayed off
Drive. An independent protected key copy and a hosted-project recovery test
remain pending. The successful local
database restore does not verify hosted Auth/email, Storage objects or settings.
