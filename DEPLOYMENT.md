# Deploying Tarshish / FindHub to production

| Piece | Runs on | Code | Config file |
|---|---|---|---|
| Database | **Supabase** (PostgreSQL) | `tarshish_engine/core/schema_postgres.sql` | — |
| Storefront API (FastAPI) | **Vercel**, project 1 | `tarshish_engine/api/index.py` → `storefront_api/` | `tarshish_engine/vercel.json` |
| Storefront website (Next.js) | **Vercel**, project 2 | `tarshish_engine/storefront/` | `tarshish_engine/storefront/vercel.json` |
| Admin dashboard (Streamlit) | **Streamlit Community Cloud** (free) | `tarshish_engine/dashboard/app.py` | `tarshish_engine/dashboard/requirements.txt` |
| Engine: harvest, publish, queue worker | **GitHub Actions** (scheduled) | `tarshish_engine/main.py` | `.github/workflows/engine.yml` |

```
 shopper ──► storefront (Vercel) ──/api/*──► API (Vercel) ──┐
                                                            ├──► Supabase PostgreSQL
 you ──► dashboard (Streamlit Cloud, password) ─────────────┤
 GitHub Actions cron: harvest · publish · worker ───────────┘
```

Why the dashboard is not on Vercel: Streamlit needs a long-running server with WebSockets, and Vercel
only runs short serverless functions. The engine jobs run for minutes and may need ffmpeg, which is why
they run on GitHub Actions.

There is no `.env` file in production. Each platform has its own environment-variable settings, and
the variable **names** are the ones in [`tarshish_engine/.env.example`](tarshish_engine/.env.example).

Do the steps in order: each one needs a value from the step before.

---

## 1. Supabase — the database

1. Create a project at [supabase.com](https://supabase.com). Pick the region **Central EU (Frankfurt)**:
   both Vercel projects are pinned to Frankfurt (`fra1`), and every API request makes several database
   queries, so the two must be close. If you choose another region, change `"regions"` in both
   `vercel.json` files to the nearest [Vercel region](https://vercel.com/docs/edge-network/regions).
2. Save the database password you choose.
3. Get the connection string: **Connect** (top bar) → **Transaction pooler** → URI. It looks like

   ```
   postgresql://postgres.<project-ref>:<password>@aws-0-eu-central-1.pooler.supabase.com:6543/postgres
   ```

   Put your password in place of `[YOUR-PASSWORD]`. If the password has characters such as `@ : / ? #`,
   URL-encode them (`@` → `%40`), or pick a password of letters and digits. Add `?sslmode=require` at
   the end. **This is `DATABASE_URL`** — every other step uses it.

   Use the **pooler** string, not the "Direct connection" one (`db.<ref>.supabase.co`): the direct host
   is IPv6-only, and Vercel, Streamlit Cloud and GitHub Actions cannot reach it.
4. Tables: nothing to do. The engine creates them on its first connection. (Alternatively, paste
   `tarshish_engine/core/schema_postgres.sql` into **SQL Editor** and run it.) Row Level Security is
   switched on for every table, so Supabase's public REST API — reachable with the anon key — sees no
   data; only connections with `DATABASE_URL` can read or write.
5. **Only if you publish videos or Stories** (`VIDEO_RENDER_ENABLED=true`, or the `instagram_story`
   content type with generated media): **Storage** → **New bucket** → name `media`, **Public bucket** on.
   Then note the project URL (`https://<project-ref>.supabase.co`) and the `service_role` / secret key
   (**Project Settings → API Keys**) for step 5. Plain image posts use the stores' own photo URLs and
   need no bucket.

### Already have local data?

Copy your local SQLite database (products, configuration edited in the dashboard, publication history)
into Supabase once, from your machine, before the first scheduled run:

```bash
cd tarshish_engine
# Windows PowerShell: $env:DATABASE_URL="postgresql://..."   (Linux/macOS: export DATABASE_URL="postgresql://...")
python main.py --import-sqlite tarshish_engine.db
```

Ids are kept, so the history stays connected. It refuses to run if Supabase already has products or
publications. Without local data, either run the **harvest** job (step 5) or load the demo catalogue
with the **seed-storefront** job.

---

## 2. Vercel — the storefront API

**Add New… → Project → import this repository**, then:

| Setting | Value |
|---|---|
| Project name | e.g. `findhub-api` |
| Root Directory | `tarshish_engine` |
| Framework Preset | Other (`vercel.json` sets it) |
| Build / Install / Output | leave as they are (`vercel.json` sets them) |

Environment variables (**Settings → Environment Variables**, all environments):

| Variable | Required | Value |
|---|---|---|
| `DATABASE_URL` | **yes** | the Supabase pooler string from step 1 |
| `STOREFRONT_ORIGINS` | recommended | the storefront's URL, e.g. `https://findhub.vercel.app` (comma-separated if several). The storefront proxies `/api` itself, so this only matters for other front ends calling the API from a browser. |
| `STOREFRONT_MIN_RATING` / `STOREFRONT_MIN_ORDERS` | no | listing standard (default 4.5 / 200) |
| `STOREFRONT_CACHE_SECONDS` | no | how long Vercel's CDN may cache listings (default 60, `0` = off) |
| `DATABASE_POOL_SIZE` | no | connections per function instance (default 4; 2 is plenty here) |
| `LOG_LEVEL` | no | `INFO` (default) or `DEBUG` |

Deploy, then open `https://<api-project>.vercel.app/api/health` — expect
`{"status":"ok","products":…}` — and `/api/docs` for the interactive documentation.

How it works: `vercel.json` routes every `/api/*` request to the single Python function
`api/index.py`, installs `requirements.txt` (only what the API needs) and leaves the dashboard, tests
and storefront out of the bundle (`.vercelignore`). Python 3.12 (Vercel's default) and newer work.

> If the URL answers with a Vercel login page, **Settings → Deployment Protection** is protecting it.
> Use the production domain (`<project>.vercel.app`), which Standard Protection leaves public, or turn
> protection off for this project. The storefront must be able to call it without logging in.

---

## 3. Vercel — the storefront website

**Add New… → Project → import the same repository again**, then:

| Setting | Value |
|---|---|
| Project name | e.g. `findhub` |
| Root Directory | `tarshish_engine/storefront` |
| Framework Preset | Next.js (detected) |

Environment variables:

| Variable | Required | Value |
|---|---|---|
| `BACKEND_API_URL` | **yes** | the backend's URL, e.g. `https://findhub-api.vercel.app` from step 2 (no trailing slash). Used at **build** time for the `/api` and `/api/backend` proxy — redeploy after changing it. A build without it fails on purpose. `API_ORIGIN`, its older name, still works. |
| `NEXT_PUBLIC_SITE_URL` | once you have a domain | e.g. `https://findhub.co.il` — canonical links and social previews. Defaults to the Vercel production domain. |
| `NEXT_PUBLIC_ILS_PER_USD` | no | shekels per dollar for the English price hint (default 3.5) |

Deploy and open the site: `/` redirects to `/he`. Add your own domain under **Settings → Domains**,
then set `NEXT_PUBLIC_SITE_URL` to it, update `STOREFRONT_ORIGINS` on the API project, and redeploy both.

---

## 3b. Optional: the HTTP admin API

The storefront is strictly the public site: it has no admin pages and does not proxy `/api/admin/*`.
Operators work in the Streamlit dashboard (step 4). The backend still carries an HTTP admin API
(`tarshish_engine/storefront_api/admin.py`, every `python main.py` operation behind an `X-Admin-Key`
header) for scripts that call the backend directly. It stays closed (HTTP 503) unless `ADMIN_API_SECRET`
is set, so leave it unset unless you use it.

| Variable | Where | Value |
|---|---|---|
| `ADMIN_API_SECRET` | backend only | 16+ random characters, sent as `X-Admin-Key`. Unset (or the example value) keeps the admin API closed. |
| `ALLOWED_ORIGINS` | backend | extra browser origins allowed to call the API directly |

Its background jobs (harvest, publish) need a long-running backend
(`python -m uvicorn storefront_api.server:app --host 127.0.0.1 --port 8000 --workers 1`); a serverless
deployment refuses them and says why.

---

## 4. Streamlit Community Cloud — the dashboard

1. Sign in at [share.streamlit.io](https://share.streamlit.io) with GitHub → **Create app** →
   **Deploy a public app from GitHub**.
2. Repository: this one. Branch: your production branch (e.g. `main`).
   Main file path: **`tarshish_engine/dashboard/app.py`**.
3. **Advanced settings** → Python version **3.13** → **Secrets** (TOML; every top-level key becomes an
   environment variable):

   ```toml
   DATABASE_URL = "postgresql://postgres.<ref>:<password>@aws-0-eu-central-1.pooler.supabase.com:6543/postgres?sslmode=require"
   DASHBOARD_PASSWORD = "a long passphrase"
   LOG_FILE = "none"

   # Only what the "Harvest now" / "Run pipeline" buttons need — the same values as GitHub Actions:
   RAPIDAPI_KEY = "…"
   ANTHROPIC_API_KEY = "…"
   INSTAGRAM_ACCESS_TOKEN = "…"
   INSTAGRAM_BUSINESS_ACCOUNT_ID = "…"
   MAX_MONTHLY_AI_BUDGET_USD = "5.0"
   ```

4. Deploy. Dependencies come from `tarshish_engine/dashboard/requirements.txt`, and
   `dashboard/packages.txt` installs ffmpeg.

The dashboard asks for `DASHBOARD_PASSWORD` before showing anything, and refuses to open on a
production database if the password is not set — it can edit the configuration and start paid,
public publishing runs. For a second layer, if your Streamlit workspace allows private apps, make the
app private in its **Settings → Sharing** and invite only your own email address.

Free apps go to sleep after some days without visitors; opening the URL wakes them up in under a
minute. Logs are in the app's **Manage app** panel (the "Logs" section of the Queue tab stays empty,
since `LOG_FILE=none`).

---

## 5. GitHub Actions — the engine

`.github/workflows/engine.yml` runs the engine on a schedule (times are UTC):

| Job | When | Command |
|---|---|---|
| harvest | daily 03:17 | `python main.py --harvest` — every enabled category, every store |
| publish | daily 03:47 | `python main.py --publish` — every (category, language) with an enabled account; queued by default |
| worker | hourly at :07 | `python main.py --mode worker --once` — publishes what is due, inside `WORKER_ACTIVE_HOURS` |

**Settings → Secrets and variables → Actions**: add each setting as a **repository secret** (or as a
*variable* for non-secret values such as `PUBLISHING_MODE`). The workflow passes every secret and
variable to the engine under the same name, so a new account in the dashboard that names a new
variable (e.g. `INSTAGRAM_ACCESS_TOKEN_SPORTS_EN`) only needs a new secret here.

| Secret | Required | Notes |
|---|---|---|
| `DATABASE_URL` | **yes** | step 1 |
| `RAPIDAPI_KEY` (+ `RAPIDAPI_KEY_TEMU_*`, `RAPIDAPI_KEY_ALI_*`) | for harvesting | |
| `TEMU_TRACKING_ID`, `ALIEXPRESS_TRACKING_ID` | for affiliate links | |
| `ANTHROPIC_API_KEY` and/or `GEMINI_API_KEY` | for AI captions | without them, the free LLM chain (`GROQ_API_KEY` / `OPENROUTER_API_KEY` / `GEMINI_API_KEY`) writes instead, with a warning; with no key at all, offline templates |
| `INSTAGRAM_ACCESS_TOKEN`, `INSTAGRAM_BUSINESS_ACCOUNT_ID`, `…_GADGETS_EN`, `TG_*` … | per account | the names in the Social Accounts matrix |
| `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET`, `YOUTUBE_REFRESH_TOKEN` (+ `…_GADGETS_EN`) | for YouTube Shorts | OAuth refresh token per channel, `youtube.upload` scope |
| `TIKTOK_CLIENT_KEY`, `TIKTOK_CLIENT_SECRET`, `TIKTOK_ACCESS_TOKEN_*`, `TIKTOK_REFRESH_TOKEN_*` | for TikTok | the refresh token keeps the 24-hour access token fresh on scheduled runs |
| `STORAGE_MODE=supabase`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` | for videos / Stories | step 1.5 |
| `VIDEO_RENDER_ENABLED=true` | for rendered videos | the workflow then installs ffmpeg |
| `MAX_MONTHLY_AI_BUDGET_USD`, `PUBLISHING_MODE`, `PUBLISH_BATCH_LIMIT`, `HARVEST_LIMIT`, `WORKER_*` | no | defaults as in `.env.example` |

Then, in the **Actions** tab:

1. Open **Engine** → **Run workflow** → job `verify-instagram` (and `verify-youtube`, `verify-tiktok`) to check the accounts.
2. Run `harvest`, then `publish` with **dry run** ticked to preview the posts in the log.
3. Run `publish` for real; the hourly `worker` takes it from there.

Scheduled workflows only run from the repository's **default branch**, so merge this work into it.
On a public repository GitHub pauses schedules after 60 days without commits (re-enable them in
the Actions tab). Each run takes about a minute; the hourly worker plus the daily jobs use roughly 800 of the 2,000 free
monthly minutes of a private repository.

`.github/workflows/ci.yml` runs the tests (on SQLite and on PostgreSQL) and a production build of the
storefront on every push and pull request.

---

## Checklist after deploying

- [ ] `https://<api>.vercel.app/api/health` → `"status": "ok"` and a product count above 0
- [ ] Storefront `/he` and `/en` show products; clicking a deal opens the store (and the click appears
      under *Storefront clicks* in the dashboard)
- [ ] Dashboard asks for the password, then shows the categories from Supabase
- [ ] Actions → Engine → `verify-instagram` passes; a dry-run `publish` prints the expected posts
- [ ] Supabase → **Advisors → Security** shows no "RLS disabled" warning for the engine's tables

## Local development is unchanged

Without `DATABASE_URL` everything uses the local SQLite file, exactly as before (see the READMEs). To
work against Supabase locally, put `DATABASE_URL` in `tarshish_engine/.env` — carefully: that is the
production data.
