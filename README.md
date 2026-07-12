# Finio

**An AI personal-finance web app for young Australians.** Upload a bank statement (CSV or PDF) and Finio turns it into a clear picture of your money: ML-categorised spending, smart budgets, recurring bills, unusual-charge alerts, a savings forecast, a spend-check verdict, and a chat coach that answers questions about your real numbers.

![Python](https://img.shields.io/badge/Python-3.9+-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi&logoColor=white)
![scikit-learn](https://img.shields.io/badge/scikit--learn-ML-F7931E?logo=scikitlearn&logoColor=white)
![Supabase](https://img.shields.io/badge/Supabase-Auth%20%2B%20pgvector-3ECF8E?logo=supabase&logoColor=white)
![OpenAI](https://img.shields.io/badge/OpenAI-optional-412991?logo=openai&logoColor=white)

> ⚠️ **General information only, not financial advice.**

---

## Features

- **Statement parsing** — CSV and text-based PDF bank statements, normalised across different bank export formats.
- **Transaction categorisation** — a rules-first + **Naive Bayes** ML pipeline, with **active learning**: your corrections become training examples that carry onto future uploads.
- **Budgets** — per-category suggested limits baselined against your prior period.
- **Recurring bill detection** — statistical (interval regularity + amount stability), not just keyword matching.
- **Anomaly detection** — per-category z-score flags charges that are unusually large *for you*.
- **Spending forecast** — next-month projection and a balance "runway" (when you might run short).
- **Spend check** — a green/yellow/red verdict on a planned purchase, using your real account balance.
- **Savings goals** — an achievable recommended target from your actual monthly surplus, with on-track projection.
- **AI coach** — a chat assistant that uses **tool-calling** over your real transactions for exact answers, grounded in a curated AU finance knowledge base via **retrieval (RAG)**.
- **Privacy-first** — bank statements and keys stay in your own Supabase project / `.env`; per-user Row Level Security.

## Tech stack

| Layer | Tech |
|---|---|
| Backend | Python, **FastAPI** |
| ML / data | **scikit-learn** (Naive Bayes, TF-IDF), **pandas**, NumPy |
| AI | **OpenAI** (`gpt-4o-mini` coach, `text-embedding-3-small` embeddings) — optional, graceful fallbacks |
| Retrieval | TF-IDF by default; **OpenAI embeddings + Supabase pgvector** when configured |
| Data / auth | **Supabase** (Postgres, Auth, pgvector, RLS) |
| Frontend | Dependency-free **HTML / CSS / JS** |
| Tests | 69-case suite (`tests/test_full_suite.py`) |

## AI / ML highlights

- **Hybrid categoriser** — deterministic keyword rules run first; a cached Naive Bayes model fills the rest; user overrides both force exact labels *and* augment the model (active learning).
- **RAG with graceful degradation** — semantic search (embeddings → pgvector `match_kb`) when a key + migration are present, automatically falling back to TF-IDF so it always works.
- **Tool-using coach** — the LLM calls typed tools (`category_total`, `filter_transactions`, `spend_check`, `lookup_concept`, …) to compute on real data instead of hallucinating figures.
- **Everything degrades without a key** — no OpenAI key → rule-based coach, TF-IDF retrieval, template insights. The app is fully usable offline.

## Getting started

**Prerequisites:** Python 3.9+, a [Supabase](https://supabase.com) project (free tier). An OpenAI key is optional.

```bash
# 1. Clone and enter the project
git clone https://github.com/Ammarhannun/main.git finio && cd finio

# 2. Create the environment
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 3. Configure secrets
cp .env.example .env         # then fill in SUPABASE_URL, SUPABASE_ANON_KEY (OPENAI_API_KEY optional)

# 4. Run everything (backend :8000 + frontend :5500, opens the browser)
./run.sh
```

Then open **http://localhost:5500/login.html**.

**Optional — enable semantic search (RAG on pgvector):**
1. Add a funded `OPENAI_API_KEY` to `.env`.
2. Run [`migrations/001_pgvector.sql`](migrations/001_pgvector.sql) in the Supabase SQL editor.
3. Index the knowledge base: `python -m scripts.index_kb`.

## Project structure

```
main/
├── main.py                 # FastAPI app + endpoints
├── modules/                # backend logic
│   ├── pipeline.py         # orchestration: parse → flag → categorise → analyse
│   ├── categoriser.py      # rules + Naive Bayes + active learning
│   ├── anomaly.py          # z-score unusual-spend detection
│   ├── savings_forecaster.py  # goal + spending forecasts
│   ├── ai_coach.py         # tool-calling coach + insights
│   ├── rag.py              # embeddings/pgvector + TF-IDF retrieval
│   ├── embeddings.py       # OpenAI embeddings (with fallback)
│   └── db.py               # Supabase persistence
├── data/kb/                # RAG knowledge base (AU finance)
├── migrations/             # pgvector SQL
├── frontend/               # vanilla HTML/CSS/JS
└── tests/                  # 69-case suite
```

## Testing

```bash
source venv/bin/activate
python -m tests.test_full_suite
```

## Roadmap

- **Agentic coach** — a bounded plan→act→observe agent with a `what-if` spending simulator, semantic merchant search, and regulated-domain guardrails.
- **Evaluation harness** — categoriser precision/recall/F1, RAG retrieval hit-rate/MRR, and LLM-as-judge scoring of coach answers, tracked for regressions.

## License

Personal / educational project. See repository for details.
