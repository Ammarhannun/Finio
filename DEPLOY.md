# Deploying Finio

Backend on Railway, frontend on Vercel, database already on Supabase.

Do the steps in order. Step 3 is the one that silently breaks everything if
skipped — the failure happens in the browser, so the server logs look healthy
while the site does not work at all.

---

## 1. Database

Run these in the **Supabase SQL editor**, in this order. All are safe to re-run.

| # | File | Creates |
|---|------|---------|
| 1 | `project-plan/supabase_schema.sql` | the 7 core tables + row level security |
| 2 | `migrations/001_pgvector.sql` | `kb_chunks`, `merchant_embeddings` (semantic search) |
| 3 | `migrations/002_chats.sql` | `chat_id` on `chat_history` (multiple coach chats) |
| 4 | `migrations/003_spend_checks.sql` | `spend_checks` (spend-check history) |

Skipping 2–4 is survivable: those features degrade and say so. Skipping 1 is not.

Then index the knowledge base (needs `SUPABASE_SERVICE_ROLE_KEY`):

```bash
python -m scripts.index_kb
```

## 2. Backend → Railway

Point Railway at this repo. `Procfile` and `railway.json` already define the
start command and health check.

Set these variables:

| Variable | Value | Required |
|----------|-------|----------|
| `SUPABASE_URL` | your project URL | yes |
| `SUPABASE_ANON_KEY` | the publishable key | yes |
| `SUPABASE_SERVICE_ROLE_KEY` | secret key — **server only, never the browser** | for KB indexing |
| `OPENAI_API_KEY` | `sk-proj-…` | for the coach + categoriser |
| `FINIO_ORIGINS` | your Vercel URL — see step 3 | **yes** |
| `SUPABASE_JWT_SECRET` | optional; verifies tokens locally instead of a network hop per request | no |
| `FINIO_LOG_LEVEL` | `INFO` | no |

Do **not** raise the worker count. The rate limiter and the period-view cache
are in process memory, so N workers give N times the intended limit and N
separate caches. Startup warns if it sees more than one.

## 3. CORS — the step that breaks everything

The CORS default only allows `localhost`. Deployed without `FINIO_ORIGINS`,
the browser blocks every request while the server reports itself healthy.

After Vercel gives you a URL, set on Railway:

```
FINIO_ORIGINS=https://your-app.vercel.app
```

Comma-separate for several (a preview domain and a custom domain). No trailing
slash. Startup logs `CORS allowing: …` when it is set, and warns loudly when it
is not — check the logs after the first deploy.

## 4. Frontend → Vercel

Root directory: `frontend/`. No build step; it is static.

Before deploying, set the API base in `frontend/config.js`:

```js
export const API_BASE_URL = 'https://your-backend.up.railway.app';
```

Leave it empty and it falls back to `<current-origin>/api`, which is only
correct if you put both behind one domain. Separate Railway and Vercel
deployments must set it explicitly.

## 5. Check it worked

```bash
curl https://your-backend.up.railway.app/          # {"status":"ok","db_configured":true}
curl -o /dev/null -w "%{http_code}\n" \
     https://your-backend.up.railway.app/dashboard # 401 — auth is on
```

Then in the browser: sign up, confirm the email, upload a statement, and check
the dashboard renders. If the pages load but every number is empty, it is
almost always CORS (step 3) — the browser console will say so.

## Known limits

- **Single worker.** Rate limiting and the view cache are per process. Moving
  both to Redis is the prerequisite for scaling out, not more workers.
- **Currency defaults to AUD.** `FINIO_CURRENCY` / `FINIO_LOCALE` on the
  backend, `CURRENCY` / `LOCALE` in `frontend/config.js`. Keep them in step.
- **Without `OPENAI_API_KEY`** the categoriser falls back to keyword rules:
  measured 20% accuracy on unseen merchants versus 87% with the model.
