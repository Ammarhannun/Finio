# Finio

**An AI personal-finance app for young Australians.** Upload a bank statement (CSV or PDF) and Finio turns it into a clear picture of your money: LLM-categorised spending, what you *usually* earn and spend, recurring bills, unusual charges, forecasts, and a coach that answers questions about your real numbers and can fix its own mistakes when you tell it to.

![Python](https://img.shields.io/badge/Python-3.9+-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi&logoColor=white)
![scikit-learn](https://img.shields.io/badge/scikit--learn-ML-F7931E?logo=scikitlearn&logoColor=white)
![Supabase](https://img.shields.io/badge/Supabase-Auth%20%2B%20pgvector-3ECF8E?logo=supabase&logoColor=white)
![OpenAI](https://img.shields.io/badge/OpenAI-optional-412991?logo=openai&logoColor=white)
![Tests](https://img.shields.io/badge/tests-74%20passing-2f8f4e)

> ⚠️ **General information only, not financial advice.**

---

## Features

- **Statement parsing** — CSV and text-based PDF statements, normalised across bank export formats, with merchant-name cleaning (`ENGIE 138808 AU AUS` → `ENGIE`).
- **Categorisation that actually works** — an **LLM classifies every merchant**, with keyword rules and Naive Bayes as offline fallbacks, and every classification cached. Measured on 57 labelled Australian merchants: **100% accuracy with the model, 87.7% offline** (`python -m eval.run_eval`). On a real 733-transaction statement the uncategorised "Other" pile is **under 1%**.
- **Quick questions** — when Finio genuinely can't tell (a person-to-person transfer, an opaque reference) it asks you up to **6 short questions**, ranked by money at stake. Recurring incoming transfers are recognised as likely income; your own banking-app transfers are not. Every answer becomes a permanent rule.
- **"What I usually spend"** — Day / Week / Month show your real averages over your whole history (complete calendar months only), not a partial latest slice. "All" shows true all-time totals.
- **Spending over time** — a month-by-month chart of what you spent versus kept, plus your top merchants.
- **Recurring bill detection** — statistical (interval regularity + amount stability), not keyword guessing.
- **Anomaly detection** — per-category z-scores flag charges that are unusually large *for you*.
- **Forecasts** — next-month spend projection and a balance "runway", plus an achievable savings goal sized to your situation.
- **Spend check** — a green/yellow/red verdict on a planned purchase using your real balance.
- **AI coach, everywhere** — a floating assistant on every page that you can split-screen with the app. It uses **tool-calling** over your real transactions for exact figures, answers concept questions from a **RAG** knowledge base, keeps **multiple named conversations**, and can **propose transaction fixes you apply with one click**.
- **Privacy-first** — statements and keys stay in your own Supabase project and `.env`; per-user Row Level Security.

## Tech stack

| Layer | Tech |
|---|---|
| Backend | Python, **FastAPI** |
| ML / data | **scikit-learn** (Naive Bayes, TF-IDF), **pandas**, NumPy |
| AI | **OpenAI** (`gpt-4o-mini` categoriser + coach, `text-embedding-3-small`) — optional, with fallbacks throughout |
| Retrieval | TF-IDF by default; **embeddings + Supabase pgvector** when configured |
| Data / auth | **Supabase** (Postgres, Auth, pgvector, RLS) |
| Frontend | Dependency-free **HTML / CSS / JS** + Chart.js |
| Quality | 74-case test suite + an **AI evaluation harness** |

## AI / ML highlights

- **Layered categoriser, model first** — the LLM decides every merchant, given its amount, frequency and raw bank description, because a keyword list cannot tell that `CALTEX WOOLWORTHS FUEL` is fuel rather than groceries. Keyword rules and Naive Bayes catch what it is unsure about and carry the whole job offline. An unconfident answer never sets a category — it becomes a quiz question. Your corrections outrank all three and are fed back as training examples, so it personalises to you.
- **Human-in-the-loop by design** — the model reports a confidence per merchant; low-confidence cases become the quiz instead of silent wrong guesses.
- **RAG with graceful degradation** — semantic search (embeddings → pgvector) when configured, automatically falling back to TF-IDF, so retrieval always works.
- **Tool-using coach** — the LLM calls typed tools (`category_total`, `filter_transactions`, `spend_check`, `lookup_concept`, `propose_reclassification`) to compute on real data rather than hallucinate figures. Write actions are always confirmed by the user.
- **Measured, not assumed** — see the evaluation harness below.
- **Runs with no API key at all** — rule-based coach, TF-IDF retrieval, template insights.

## Evaluation

```bash
python -m eval.run_eval              # all suites
python -m eval.run_eval --no-coach   # offline only, no API calls
```

| Suite | Metric | Baseline |
|---|---|---|
| Categoriser | macro F1 on 41 **held-out** merchants | **0.906** (90.2% accuracy) |
| Retrieval | hit-rate@2 / MRR over 15 probes | **100% / 1.000** |
| Coach | LLM-as-judge: helpfulness / accuracy / safety | **4.88 / 5.00 / 5.00** |

The coach suite includes adversarial probes (a "put everything in Bitcoin" request and a prompt-injection attempt) — both are correctly refused. The offline suites also run inside the test suite against quality floors, so a change that makes the AI worse fails CI instead of shipping.

## Getting started

**Prerequisites:** Python 3.9+, a [Supabase](https://supabase.com) project (free tier). An OpenAI key is optional but unlocks the LLM categoriser, coach and semantic search.

```bash
# 1. Clone
git clone https://github.com/Ammarhannun/Finio.git finio && cd finio

# 2. Environment
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 3. Secrets
cp .env.example .env      # fill in SUPABASE_URL, SUPABASE_ANON_KEY (OPENAI_API_KEY optional)

# 4. Database — run these in the Supabase SQL editor
#    project-plan/supabase_schema.sql   (core tables + RLS)
#    migrations/001_pgvector.sql        (optional: semantic search)
#    migrations/002_chats.sql           (optional: multiple coach chats)

# 5. Optional: index the knowledge base for semantic RAG
#    (needs SUPABASE_SERVICE_ROLE_KEY in .env — RLS blocks anon writes)
python -m scripts.index_kb

# 6. Run (backend :8000 + frontend :5500, opens the browser)
./run.sh
```

Then open **http://localhost:5500/login.html**.

## Project structure

```
main/
├── main.py                    # FastAPI app + endpoints
├── modules/
│   ├── pipeline.py            # parse → flag → categorise → analyse → persist
│   ├── categoriser.py         # LLM-first + rules + Naive Bayes + active learning
│   ├── llm_categoriser.py     # batched LLM classification + the quiz
│   ├── analytics.py           # breakdowns, patterns, averages, top merchants
│   ├── anomaly.py             # z-score unusual-spend detection
│   ├── bill_detector.py       # statistical recurring bills
│   ├── savings_forecaster.py  # goal + spending forecasts
│   ├── ai_coach.py            # tool-calling coach, insights, chat titles
│   ├── rag.py / embeddings.py # retrieval (pgvector or TF-IDF)
│   └── db.py                  # Supabase persistence
├── eval/                      # evaluation harness + datasets
├── data/kb/                   # RAG knowledge base (AU finance)
├── migrations/                # pgvector + chats SQL
├── frontend/                  # vanilla HTML/CSS/JS (+ coach widget)
└── tests/                     # 74-case suite
```

## Testing

```bash
source venv/bin/activate
python -m tests.test_full_suite
```

## Roadmap

- Live hosted demo (Railway + Vercel).
- Receipt OCR: photograph a receipt, get a categorised transaction.
- Feeding the quiz answers back into a per-user fine-tune signal.

## License

Personal / educational project. See repository for details.
