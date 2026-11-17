# Tarshish

A two-stage affiliate engine. **Stage 1** harvests products per category and store into SQLite, once, for
every language. **Stage 2** turns them into Hebrew and English posts, Stories and videos — each content type
written by its own LLM — and publishes or queues them to the account mapped to (category, language,
platform). The engine is plugin-based, so new categories, languages, models, product sources and publishing
platforms are added without touching the core; categories, model routing and the account matrix are edited
in the Streamlit dashboard.

The same database also feeds **FindHub**, a public deal storefront in Hebrew and English: a FastAPI backend
(`tarshish_engine/storefront_api/`) and a Next.js site (`tarshish_engine/storefront/`).

**Production:** storefront and API on Vercel, the database on Supabase, the dashboard on Streamlit
Community Cloud and the scheduled engine runs on GitHub Actions. Step by step: [`DEPLOYMENT.md`](DEPLOYMENT.md).

## Get it running

Needs Python 3.14 or 3.15 (older 3.11+ versions should work but are untested).

```bash
cd tarshish_engine
python -m venv --clear .venv
.venv\Scripts\activate            # Windows   (Linux/macOS: source .venv/bin/activate)
python -m pip install -r requirements-dev.txt
python -m pytest tests -q         # everything should pass
python main.py --harvest --source temu_mock --category gadgets    # Stage 1: offline catalogue
python main.py --publish --category gadgets --lang he --dry-run   # Stage 2: preview, nothing published
```

No API keys or accounts are needed for this: without them the engine writes captions from offline
templates and prints the posts to the console.

## The three layers

All three use one database: locally the SQLite file `tarshish_engine/tarshish_engine.db`, in production
Supabase PostgreSQL (`DATABASE_URL`). Run each in its own terminal from
`tarshish_engine/` (the storefront needs Node.js 20.9+).

```bash
# 1. Engine: harvest (Stage 1), then publish per language (Stage 2)
python main.py --seed-storefront          # once: load the 14-product demo catalogue
python main.py --harvest --category gadgets                        # real stores (needs RAPIDAPI_KEY)
python main.py --publish --category gadgets --lang he --content-types instagram_post,instagram_story
python main.py --publish --category gadgets --lang en --content-types instagram_post,instagram_story
python -m streamlit run dashboard/app.py  # optional: categories, accounts matrix, model routing, runs, KPIs

# 2. Storefront backend (FastAPI)  ->  http://localhost:8000/api/docs
python -m uvicorn storefront_api.server:app --reload --port 8000

# 3. Storefront website (Next.js)  ->  http://localhost:3000
cd storefront && npm install && npm run dev
```

## Where to look

| | |
|---|---|
| [`DEPLOYMENT.md`](DEPLOYMENT.md) | Production: Supabase, Vercel, Streamlit Community Cloud, GitHub Actions and every environment variable. |
| [`setup_roadmap.html`](setup_roadmap.html) | Step-by-step checklist, from first run to production. Open it in a browser. |
| [`tarshish_engine/README.md`](tarshish_engine/README.md) | Commands, configuration, architecture, how to add plugins. |
| [`.claude/`](.claude/CLAUDE.md) | Project knowledge for Claude Code. It loads automatically when you open the repo; `/verify` runs the checks. |

Secrets go only in `tarshish_engine/.env` (git-ignored) locally — copy `.env.example` to create it — and in
each platform's environment-variable settings in production (see `DEPLOYMENT.md`).

On Windows 11, Smart App Control can block freshly installed Python libraries. The commands above are
written to avoid that (`python -m …`); if you still see "An Application Control policy has blocked this
file", see *Windows Smart App Control* in [`tarshish_engine/README.md`](tarshish_engine/README.md).
