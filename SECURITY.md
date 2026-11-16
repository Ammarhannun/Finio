# Finio security operations

This document describes implemented controls and the work required to operate
them. It is not a certification or independent penetration-test report.

## Existing boundaries

- Supabase Auth verifies users; database row policies restrict financial rows
  to their owner. Edit and upload RPCs execute with caller permissions.
- Financial writes are atomic and check revisions. Upload replacement requires
  confirmation. Data deletion clears financial records while retaining login.
- Fallback-model cache keys preserve the structure of correction examples and
  use SHA-256, preventing merchant text from aliasing another example set.
- Authenticated uploads are bounded before multipart parsing. Input bytes,
  transaction count, PDF pages and extracted text are capped. Temporary files
  are removed on success and failure.
- Upload admission happens before body buffering: one active upload per account
  and two total per application process. Rejected concurrent requests cannot
  allocate another body spool. Slots are released after errors, timeouts,
  disconnects and cancellation. The single-worker/replica requirement still applies.
- Financial API responses are never cached. Security headers cover normal,
  validation, early-rejection and unhandled-error responses. Production API
  documentation is disabled. CORS permits the configured frontend origins,
  supported methods and required headers without ambient cookie credentials.
- Secret Supabase keys cannot be used as the web process's public key. Optional
  local JWT verification checks required claims; it does not establish current
  session liveness after revocation.
- Customer accounts can enroll a TOTP authenticator from Profile. Enrolled
  accounts must complete the second step before financial pages load. Database
  policies enforce the verified factor on direct REST reads and writes too.
- Every financial table requires a live Auth session. Session revocation is
  checked in the database, including when a signed access token has not expired.
- Export and financial-data deletion through Finio require a live session, any
  enrolled MFA, and authentication within five minutes. Direct Data API access
  remains available to the authenticated owner under RLS; this is not a download
  prevention or data-loss prevention system.
- Account changes and sign-out clear private screens. Restored browser-history
  documents reload through authentication. Frontend responses disable browser
  caching, and therefore incur additional asset requests when revisited.

## AI data minimisation

All OpenAI entry points redact common contact/account identifiers and recognised
credentials. Merchant classification excludes raw bank-description examples,
uses request-scoped opaque IDs and ignores output names outside the input batch.
Coach transaction samples contain only necessary financial fields, and exact
tools still calculate against the original owner-scoped rows. Reversible
redaction maps live in request memory only. Credentials cannot be restored and
are removed from newly saved chat text. Existing stored chats are unchanged.

Pattern checks cannot recognise every personal name, address or identifier.
Selected amounts, dates, merchant/category text and financial context still go
to OpenAI. Do not claim anonymisation, end-to-end encryption or zero retention.
Completion storage is explicitly disabled, but provider security retention may
still apply. `FINIO_DISABLE_LLM=1` disables provider requests globally. HTTP
requests also require the account owner's explicit, current-version opt-in in
Profile. Missing, malformed or unverifiable consent blocks provider calls while
built-in calculations remain available. Consent is read from live Auth user
metadata and cached only for that request. It is a transmission preference,
never a financial-data authorization rule or trusted JWT claim. Changes apply
to new requests; they do not erase earlier requests or saved results. The choice
is included in export and retained with the login after financial-data deletion.
Existing accounts default to off until they opt in. No schema migration is needed.

Provider requests share four slots in the single application process. Coach
responses are capped at 1,024 output tokens; categorisation is bounded by batch
and output limits. These are availability/cost controls, not a global monetary
cap. Keep provider budgets, alerts and signup/IP abuse controls configured.

## PDF worker boundary

The runtime image pins the official Python base by digest and applies available
Debian security updates over HTTPS before installing application dependencies.
Package-manager retries are disabled during this update to avoid the affected
APT retry path while updating older base images. Refresh the Python base digest
when upstream publishes a security release, then rebuild and rescan.
The runtime image removes pip, setuptools and wheel after the hashed application
dependencies are installed. CI scans the full image with a pinned Trivy version,
retains the complete advisory report, and rejects high/critical findings that
have available fixes. Findings without vendor fixes remain in the report for
review. GitHub Actions are pinned to verified upstream commit hashes, with
Dependabot configured to propose weekly action/base-image updates after these
files reach the repository's default branch. No hosted CI run or update PR is
established by local configuration alone. Passing this gate is not a claim of
zero vulnerabilities or independent
assessment. Rebuild and rescan when the base image or dependencies change.

PDF parsing uses a separate, short-lived Python process with isolated Python
startup, a minimal environment and a private working directory. It receives no
application API keys in its environment. Input is at most 10 MB, processing has
a 20-second wall timeout and 12-second CPU limit, and output is capped at 8 MB.
Linux limits the data segment to 512 MB. Existing limits of 200 pages, five
million extracted characters and 20,000 transactions remain in force.

On Linux, the single-thread worker installs a Landlock filesystem policy and a
seccomp syscall filter before parsing the untrusted file. Read access is limited
to that input and Python/system runtime libraries; file writes, network sockets,
new processes, execution and process-inspection syscalls are denied. It receives
no inherited application credentials. Production refuses PDF processing when
the restrictions cannot start, including on a non-Linux host. Development on
macOS retains the resource-limited worker without kernel confinement.

The container needs Landlock ABI 3 or newer, libseccomp and an outer host policy
that permits installation of these restrictions. The image build verifies the
library; tests/pdf_sandbox_probe.py verifies actual parsing and denial behavior
in the release Linux image. An unsupported kernel can still serve CSV imports.
Use non-root deployment, read-only files, bounded temporary storage and host
limits too. Keep the kernel/runtime patched; this boundary does not eliminate
all parser or host risks. Never bake credentials into the image.

## Owner tasks before public uploads

1. Enable and verify two-factor authentication on source control, hosting,
   Supabase, OpenAI and business email. Minimise staff access and protect recovery
   codes. The owner's Supabase admin authenticator was verified on 2 November
   2026. Porkbun app MFA and the Google 2-Step Verification route used for Resend
   were observed enabled on 6 November. Source-control, hosting and OpenAI
   account protections still need their own verification.
   App MFA and recent verification are implemented,
   but are optional until each customer enrolls an authenticator.
2. Enable Supabase leaked-password protection where supported. Review and repair
   advisor warnings about mutable search paths, extension placement and callable
   privileged functions. Confirm trigger/event-trigger behaviour before changing
   privileges. Migration `20261101060230_account_security.sql` was applied to the
   configured project on 1 November 2026. Both readiness capabilities pass, all
   nine restrictive policies are present, and financial-table row counts were
   unchanged. Seven database warnings were resolved. Leaked-password screening
   remains unavailable on the project's Free plan.
3. Verify production TLS, origins, session expiry, email confirmation/recovery,
   provider access and direct API/database ownership checks with disposable users.
4. Establish an operator identity and privacy/security contact, defined retention
   and backup periods, processor agreements and any required legal review. The
   owner withdrew permission to publish their personal email on 6 November
   2026, and the public contact card was removed. A public operator identity
   and appropriate privacy-request channel remain unresolved. The previously
   supplied private security-alert recipient is separate and is not published.
5. Arrange real encrypted backups and test a restore into an isolated project.
   `scripts/recovery_drill.py` passed with a full synthetic database, matching
   restored records, working RLS/MFA and rejection of tampered ciphertext. It is
   a recovery-procedure test, not a backup of the live project. The owner chose
   manual encrypted backups on Free. `scripts/manual_backup.py` and
   `MANUAL-BACKUPS.md` document that workflow. The first real encrypted backup
   and isolated local restore passed on 2 November 2026: all nine financial
   tables matched live counts/fingerprints, and restored access checks passed.
   The live project was unchanged. The encrypted dump, recovery companion and
   both receipts were copied to owner-only Google Drive storage on 3 November;
   the downloaded dump matched its local SHA-256 checksum. The private identity
   stayed off Drive. On 6 November the owner confirmed saving the key in Apple
   Passwords with iCloud sync and checking access on another trusted device.
   This is owner attestation; the password-manager copy was not cryptographically
   checked by the agent. The owner still needs retention decisions and a separate
   hosted-project recovery check. Deletion does not imply immediate removal
   from backups. Manual backups are not automatically scheduled.
6. Obtain a scoped independent assessment covering authenticated app/API routes,
   database policies, account recovery, uploads, AI tools and cloud configuration.
   Resolve material findings and retain retest evidence. Consider OWASP ASVS as
   the verification framework. Supplier certificates do not certify this app.

## Security events and incident response

Provider errors must log only operational metadata, not prompts, statements,
tokens or exception bodies. Error references let operators correlate failures.
The application logs failed requests using matched route templates and status
codes, without raw URL paths, query values or response bodies. Keep Uvicorn
access logs disabled with `--no-access-log`, and configure proxy/host logs to
omit query strings and sensitive headers. `scripts/check_service.py` distinguishes
liveness from readiness, returns a nonzero exit on failure and prints status
metadata only. It does not schedule itself or send alerts.

`scripts/monitor_service.py` adds one scheduled check with private TLS SMTP
alerts for failure and recovery. Stable states stay quiet; a failed delivery
does not advance saved state and is retried on the next check. It sends only
liveness/readiness status codes, never response bodies or financial data.
Store its config/state outside Git, use owner-only file permissions and supply
the mail credential through the scheduler's protected environment. The private
recipient has been supplied, but no host URL, mail provider, schedule or real
delivery test is configured. This does not monitor Auth events or provider spend.

Configure host/Auth alerts for unusual sign-ins, repeated failures, rate limits,
provider spending, failed backups and unexpected server errors.

On a suspected incident, contain affected services or accounts, preserve
restricted evidence, revoke exposed credentials/sessions, assess affected data
and obtain specialist advice. Determine notification obligations, communicate
through an established contact and restore only from verified clean sources.
Record the cause, corrective work and retest. Do not place incident evidence or
customer records in issues, public logs, Git or AI prompts.

## Verification

Use the commands in `DEPLOY.md`. Security tests exercise redaction, exact financial
matching, credential removal, concurrent provider limits, JWT/key rejection,
private error responses, parser timeouts and real synthetic PDF parsing. Browser
tests cover cross-tab account changes and cached-document restoration. Isolated
PostgreSQL tests cover real migration execution, ownership and atomic writes.
Hosted configuration, real email recovery/alert delivery, independent key
recovery and an external assessment remain separate gates. Customer MFA
and sensitive-action checks have local unit, browser and database coverage. No code test establishes that an application is perfectly
secure or compliant.
