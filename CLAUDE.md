# Finio — project guide for Claude Code

Finio is an AI-powered personal finance web app for young Australians (18-30).
Users upload a bank-statement CSV; the backend runs ML + analysis and returns
metrics, spending patterns, budgets, an invest plan, a spend-check verdict, and
an AI coach. Currency is AUD.

## Learning mode (important)

The owner (Ammar) is building this to learn. When working on the backend:
- Explain what you are doing and why.
- Prefer small, reviewable changes.
- Do not rewrite whole modules unless asked.

The **frontend (Phase 8) is the current task** and may be built more freely, but
still build it **one page at a time** and pause for review.

## Current status

- Phases 1-7 are DONE: data layer, ML models, core logic, AI coach, FastAPI API,
  and Supabase persistence are all complete and tested (46 passing tests).
- **Phase 8 = frontend** (this is what to build now).
- Phase 9 = deploy (Railway + Vercel) — later.

## Tech / conventions

- Backend: Python + FastAPI (`main.py`), modules in `modules/`, constants in `config.py`.
- Frontend: **vanilla HTML/CSS/JS, no framework.** Build it in a new `frontend/` folder.
- Mobile-friendly, modern dark UI.
- Run backend tests: `source venv/bin/activate && python -m tests.test_full_suite`
- Never commit `.env`.
- Every page showing financial info must include the disclaimer:
  **"General information only, not financial advice"**
- Format money as AUD, e.g. `$1,044.98`.

## Backend API (already built — do not change without asking)

Base URL (local): `http://127.0.0.1:8000`
CORS is open in dev.

Auth: Supabase Auth. The frontend logs the user in with the Supabase JS client,
then sends `Authorization: Bearer <access_token>` on every request except `GET /`.

| Method | Path | Auth | Body / Params | Returns |
|--------|------|------|---------------|---------|
| GET | `/` | none | — | `{status, app, db_configured}` |
| POST | `/analyze` | optional | multipart: `file` (CSV), `goal_amount`, `goal_date` (YYYY-MM-DD), `age` | full analysis JSON; persists to DB if Bearer token present (`persisted: true`) |
| GET | `/dashboard` | required | — | `metrics`, `analysis`, `bills`, `budgets`, `budget_limits`, `invest`, `personality`, `snapshot`, `context`, `streak`, `goal`, `disclaimer` |
| GET | `/invest` | required | — | `invest`, `forecast`, `goal`, `metrics`, `disclaimer` |
| POST | `/spend-check` | required | JSON: `{merchant, amount, days_ahead}` (amount > 0, days_ahead 1-90) | `{verdict: green/yellow/red, projected_balance, safety_buffer, message, ...}` |
| POST | `/coach` | required | JSON: `{message}` (1-2000 chars) | `{text, source, disclaimer, quick_questions}` |
| GET | `/coach/history` | required | — | `{history: [{role, message, timestamp}]}` |

Notes:
- `/analyze` must be called (with a token) before `/dashboard`, `/invest`,
  `/spend-check`, and `/coach` return data. Without prior analysis they return
  `404 {"detail": "No analysis found — upload a CSV via POST /analyze first"}`.
- `401` means missing/invalid/expired token → send the user back to login.
- Multipart upload: with `FormData`, do NOT set `Content-Type` manually.

## Key response shapes (from `/dashboard`)

- `metrics`: `{total_income, total_spent, transaction_count, daily_burn_rate, date_range:{start,end,days}}`
- `analysis`: `{category_breakdown:[{category,amount,pct}], patterns:[{type,severity,message}], risk_score, risk_label}`
- `bills`: `[{merchant, amount, count, frequency}]`
- `budgets`: `{budgets:[{category, actual_spend, suggested_limit, headroom_pct}]}`
- `invest`: `{split_503020, actual_vs_target, readiness:{can_invest,reason}, goal, first_1000, etf:{recommended,options,reason}}`
- `personality`: `{personality_type, scores, savings_rate, action_plan:[...]}`
- `streak`: `{current_streak, best_streak, last_upload}`

## Supabase (frontend auth)

- URL: `https://uymdybutxttkgqmluyfq.supabase.co`
- Anon key: in `.env` as `SUPABASE_ANON_KEY` (anon key is public — safe in browser JS).
- Use `@supabase/supabase-js` (CDN is fine). After sign-in:
  `const { data: { session } } = await supabase.auth.getSession();`
  then `session.access_token` is the Bearer token.

## 5 frontend pages to build (one at a time)

1. **Login / Signup** — Supabase email auth; store session; redirect to Dashboard.
2. **Dashboard** — CSV upload (`POST /analyze`), then income/spent/saved cards,
   streak badge, budget bars, personality card (reads `GET /dashboard`).
3. **Patterns** — pattern cards with severity, recurring bills list, category
   breakdown chart (reads `GET /dashboard`).
4. **AI Coach** — chat UI (`POST /coach`), quick questions, history (`GET /coach/history`).
5. **Spend Check** — merchant + amount form → green/yellow/red verdict (`POST /spend-check`).
6. **Invest** — 50/30/20 split, savings goal progress, ETF nudge, First $1000 (`GET /invest`).

Build order: login + shared `api.js` first, then one page at a time.
