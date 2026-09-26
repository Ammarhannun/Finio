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

All phases are built and tested. 131 tests, plus an evaluation harness.
Deployment config exists (`DEPLOY.md`) but the app is **not deployed yet**.

```bash
source venv/bin/activate
python -m tests.test_full_suite     # 131 tests, offline (no API calls)
python -m eval.run_eval             # categoriser + retrieval + coach quality
```

Tests set `FINIO_DISABLE_LLM=1` so they stay fast, free and offline. `run_eval`
is what measures the model path.

## Conventions

- Backend: Python + FastAPI (`main.py`), logic in `modules/`, constants in `config.py`.
- Frontend: **vanilla HTML/CSS/JS, no framework**, in `frontend/`.
- Never commit `.env`.
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
- **Everything reads from a stored snapshot**, not a live recompute — so edits
  need `/reanalyze` to show up. `SNAPSHOT_VERSION` bumps prompt the user.
- **Rate limiter and period-view cache are in-process.** One worker only.
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
`migrations/001` → `003`. Features whose migration hasn't run degrade and say
so; they never break the page.
