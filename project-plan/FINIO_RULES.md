# Finio — AI-Powered Personal Finance Analyser
# Built by Ammar Hannun
# Project rules — also in `.cursor/rules/finio.mdc` (Cursor auto-loads)

## What this project is
Finio is an AI-powered personal finance web app for young Australians (18-30).
Users upload their bank statement CSV and Finio analyses it using real
ML models, detects spending habits, provides an AI coach powered by OpenAI API,
gives a live spend verdict, and tells users where to invest their leftover money.

It is also a B2B API product that Australian banks and fintechs can integrate.

## One-line pitch
"Finio tells you not just where your money went — but exactly where it should go next."

## My role
I am building this myself to learn. Your job is to ASSIST me, not build it for me.
- Explain what I need to do and why
- Give small code snippets (max 10-15 lines) when I am stuck
- Tell me what functions/libraries to use and how they work
- Review my code and point out bugs
- Answer specific questions I ask
- Do NOT write full files unless I explicitly ask for it
- Do NOT scaffold the whole project
- If I ask you to "just do it", remind me I want to learn and explain what to do instead

## Tech stack
- Backend: Python + FastAPI
- ML Models: scikit-learn (Naive Bayes classifier, DBSCAN, linear regression)
- AI Coach: OpenAI API (gpt-4o-mini)
- Data processing: pandas + numpy
- Database: Supabase (PostgreSQL)
- Auth: Supabase Auth
- Frontend: Claude-generated HTML/JS (built last)
- Deploy: GitHub → Railway (backend) + Vercel (frontend)
- Code editor: Cursor

## Folder structure
finio/
└── main/
    ├── main.py                   ← FastAPI app + all routes
    ├── config.py                 ← colours, keywords, constants
    ├── requirements.txt
    ├── .env                      ← API keys (never commit this)
    ├── .cursor/rules/finio.mdc   ← Cursor rules (always apply)
    ├── project-plan/FINIO_RULES.md
    └── modules/
        ├── bank_parser.py        ← reads + cleans any bank CSV
        ├── data_processor.py     ← adds flags + summary metrics
        ├── categoriser.py        ← ML text classifier (Naive Bayes) [DONE]
        ├── bill_detector.py      ← recurring bills [DONE]
        ├── savings_forecaster.py ← linear regression [DONE]
        ├── analytics.py          ← patterns + risk score [DONE]
        ├── spend_check.py        ← verdict formula [DONE]
        ├── budget_setter.py      ← suggested budgets (OpenAI explains in Phase 6) [DONE]
        ├── ai_coach.py           ← OpenAI coach [DONE]
        ├── history.py            ← snapshots + streaks [DONE]
        ├── personality.py        ← financial personality score [DONE]
        └── invest.py             ← 50/30/20 + ETF nudges [DONE]

## 5 pages (web app navigation)
1. Dashboard   — CSV upload, metric cards, streak badge, charts, budget bars, personality card
2. Patterns    — pattern cards, severity badges, bill detector, charts
3. AI Coach    — OpenAI chat with full financial context, quick questions, conversation history
4. Spend Check — item + amount input, green/yellow/red verdict, invest vs spend comparison
5. Invest      — 50/30/20 split, ETF nudges, savings goal tracker, progress tracker, First $1000 mode

## Where AI is used
- AI Coach: OpenAI API — full financial summary injected as context
- Budget Setter: OpenAI API — explains budget suggestions in plain English
- Personality Score: OpenAI API — writes personalised 3-step action plan
- Invest Nudges: OpenAI API — explains ETF recommendations based on age + income
- Auto-categorisation: Naive Bayes ML model — classifies merchant names
- Bill Detector: DBSCAN clustering — finds recurring payments
- Savings Forecaster: Linear regression — predicts if user hits savings goal
- Spend Check: Custom algorithm — projected balance vs safety buffer
- 50/30/20 Engine: Statistical algorithm — splits income into buckets

## Database — Supabase tables
- users: id, email, age, income_bracket
- transactions: user_id, date, amount, merchant, category, is_expense
- budgets: user_id, category, monthly_limit, month
- snapshots: user_id, month, summary_json
- goals: user_id, name, target_amount, target_date, current_saved
- streaks: user_id, current_streak, best_streak
- chat_history: user_id, role, message, timestamp
- api_keys: user_id, key_hash, tier

## Bank CSV format
Users export CSV from any bank (CommBank, NAB, Westpac, ANZ, Up, etc.). Column names vary;
`bank_parser.py` maps them to a normalized schema: date, amount, description, balance (optional).

Typical fields:
- Date (often DD/MM/YYYY in AU exports)
- Amount (signed float: negative = expense, positive = income) OR separate Debit/Credit columns
- Description (messy merchant text e.g. "UBER* EATS AU 12345 SYDNEY")
- Balance (running total — not all banks include this)

No category column — we classify transactions ourselves using the ML model.

## Spending categories (7 buckets)
- Food & Dining — restaurants, delivery, takeaway, cafes
- Groceries — supermarkets, convenience stores
- Transport — Opal, Uber trip, fuel, parking
- Subscriptions — streaming, gym, phone plans
- Shopping — retail, Amazon, electronics, clothes
- Health — pharmacy, medical
- Other — rent, salary labels, transfers, uncategorised

Income (positive amounts) uses `is_expense=False` — not ML-classified.

## Build phases (current progress)

**Sync rule:** When you mark a phase DONE or change what you are working on, update this section **and** `.cursor/rules/finio.mdc` (`Current progress` + `Next step`) in the same edit.

Phase 1 — Setup (DONE)
Phase 2 — Auth + Database (Supabase) (DONE)
Phase 3 — Data layer (bank_parser.py, data_processor.py) (DONE)
Phase 4 — ML models (DONE)
  - categoriser.py (DONE) — Naive Bayes, `categorise_data(df)`, trained on `data/training_merchants.csv`
  - bill_detector.py (DONE) — `detect_bills`, skips transfers/P2P and repeat shopping
  - savings_forecaster.py (DONE) — `forecast_goal`, linear regression on savings growth
Phase 5 — Core logic (DONE)
  - analytics.py (DONE) — `analyze`, patterns + risk score
  - spend_check.py (DONE) — `check_purchase`, green/yellow/red
  - invest.py (DONE) — `invest_summary`, 50/30/20 + invest readiness
  - history.py (DONE) — `build_snapshot`, `update_streak`; persisted via `db.py` in Phase 7
  - personality.py (DONE) — `score_personality`, rule-based action plan
  - budget_setter.py (DONE) — `suggest_budgets`, per-category limits
Phase 6 — AI layer (DONE)
  - ai_coach.py (DONE) — OpenAI `coach_chat`, `explain_budgets`, `enhance_action_plan`, `explain_etf_nudge`; fallbacks without `OPENAI_API_KEY`
Phase 7 — API layer (DONE)
  - main.py — FastAPI routes + CORS
  - modules/pipeline.py — `run_full_pipeline`
  - modules/db.py — Supabase persistence (transactions, snapshots, streaks, goals, budgets, chat)
  - api/deps.py — JWT auth via Supabase
  - schemas.py — request models
  - project-plan/supabase_schema.sql — table + RLS reference
Phase 8 — Frontend [NEXT]
Phase 9 — Deploy (GitHub → Railway + Vercel)

## Important rules
- Keep `.cursor/rules/finio.mdc` and this file in sync whenever phase progress changes
- Never commit .env to GitHub
- Always use the virtual environment (venv)
- Test every module with sample data before moving to the next
- The app must work without an OpenAI API key (rule-based fallback)
- All financial advice must include disclaimer: "General information only, not financial advice"
- Australian context: AUD currency, AU bank CSV exports, ASX ETFs (VGS, A200, NDQ)