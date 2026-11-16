# Tarshish

Automated affiliate content pipelines: fetch products, generate posts with an LLM, publish or queue them
to social channels. The engine is plugin-based, so new categories, models, product sources and publishing
platforms are added without touching the core.

The same database also feeds **FindHub**, a public deal storefront in Hebrew and English: a FastAPI backend
(`tarshish_engine/api/`) and a Next.js site (`tarshish_engine/storefront/`).

## Get it running

Needs Python 3.14 or 3.15 (older 3.11+ versions should work but are untested).

```bash
cd tarshish_engine
python -m venv --clear .venv
.venv\Scripts\activate            # Windows   (Linux/macOS: source .venv/bin/activate)
python -m pip install -r requirements-dev.txt
python -m pytest tests -q         # everything should pass
python main.py --pipeline sports_he --mode immediate --dry-run
```

No API keys or accounts are needed for this: without them the engine writes captions from offline
templates and prints the posts to the console.

## The three layers

All three use the one SQLite file, `tarshish_engine/tarshish_engine.db`. Run each in its own terminal from
`tarshish_engine/` (the storefront needs Node.js 20.9+).

```bash
# 1. Pipeline engine
python main.py --seed-storefront          # once: load the 14-product demo catalogue
python main.py --pipeline sports_he --mode immediate --dry-run

# 2. Storefront backend (FastAPI)  ->  http://localhost:8000/api/docs
python -m uvicorn api.server:app --reload --port 8000

# 3. Storefront website (Next.js)  ->  http://localhost:3000
cd storefront && npm install && npm run dev
```

## Where to look

| | |
|---|---|
| [`setup_roadmap.html`](setup_roadmap.html) | Step-by-step checklist, from first run to production. Open it in a browser. |
| [`tarshish_engine/README.md`](tarshish_engine/README.md) | Commands, configuration, architecture, how to add plugins. |
| [`.claude/`](.claude/CLAUDE.md) | Project knowledge for Claude Code. It loads automatically when you open the repo; `/verify` runs the checks. |

Secrets go only in `tarshish_engine/.env` (git-ignored). Copy `.env.example` to create it.

On Windows 11, Smart App Control can block freshly installed Python libraries. The commands above are
written to avoid that (`python -m …`); if you still see "An Application Control policy has blocked this
file", see *Windows Smart App Control* in [`tarshish_engine/README.md`](tarshish_engine/README.md).
