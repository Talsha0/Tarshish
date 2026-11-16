# Tarshish Engine

Plugin-based engine for automated affiliate content pipelines:
**fetch products ➔ generate content ➔ store ➔ publish or queue**.

Adding a pipeline for a new category, an AI model, a product source, a video renderer or a
publishing destination never requires a change in `core/`.

The same database feeds **FindHub**, the public deal storefront: a FastAPI backend in `api/` and a
Next.js site in `storefront/`. See [FindHub storefront](#findhub-storefront).

## Quick start

Python 3.14 or 3.15 (developed and tested on both; 3.11+ should work but is untested).
Every dependency installs from a prebuilt or pure-Python wheel, so no compiler is needed.

```bash
python -m venv --clear .venv
.venv\Scripts\activate            # Windows  (Linux/macOS: source .venv/bin/activate)
python -m pip install -r requirements.txt
copy .env.example .env            # optional: everything works offline without keys

python main.py --pipeline sports_he --mode immediate --dry-run
```

With no API keys the engine uses the offline Hebrew/English template writer and
the console publisher, so the command above works on a fresh checkout.

A step-by-step checklist from first run to production is in
[`../setup_roadmap.html`](../setup_roadmap.html) (open it in a browser).

### Running all three layers

The pipeline engine, the storefront backend and the storefront website are separate processes over one
SQLite file (`tarshish_engine.db`, WAL mode, so they can read and write at the same time). Run each in
its own terminal, from this folder, with the virtual environment active:

```bash
# 1. Python pipeline engine: fetch products, write and publish posts
python main.py --seed-storefront                                   # once: load the demo catalogue
python main.py --pipeline sports_he --mode immediate --dry-run

# 2. FastAPI storefront backend: products, search, click tracking
python -m uvicorn api.server:app --reload --port 8000              # same as: uvicorn api.server:app --reload --port 8000

# 3. Next.js storefront frontend (Node.js 20.9+)
cd storefront && npm install && npm run dev
```

Then open <http://localhost:3000> (it redirects to the Hebrew storefront, `/he`). The dashboard
(`python -m streamlit run dashboard/app.py`) is optional and shows the storefront's clicks live.

> **Rebuild `.venv` after moving the folder or upgrading Python.** A virtualenv
> stores absolute paths and packages compiled for one Python version, so run the
> first three commands again (`--clear` wipes the old environment first).
> `python main.py` prints an `[ENVIRONMENT ERROR]` with these steps when it detects this.

## CLI

| Command | What it does |
|---|---|
| `python main.py --pipeline sports_he --mode immediate --dry-run` | Simulate the pipeline and print the posts. No publishing, no paid LLM calls, no DB writes. |
| `python main.py --pipeline sports_he --mode immediate` | Generate and publish now. |
| `python main.py --pipeline sports_he --mode queued` | Generate and store as `QUEUED` with jittered slots. |
| `python main.py --mode worker` | Publish the queue with human-like delays inside active hours (`--once` for a single pass). |
| `python main.py --stats` | Queue size, published count, monthly AI spend. |
| `python main.py --flush-queue` | Delete pending/queued items (AI spend stays on record). |
| `python main.py --retry-failed` | Put failed items back in the queue. |
| `python main.py --list-pipelines` / `--sync-config` | List pipelines / upsert `config/pipelines.yaml` into the database. |
| `python main.py --seed-storefront` | Load the 14-product demo catalogue into the storefront (an upsert; safe to repeat). |
| `python main.py --seed-temu --category gadgets --keyword "smart gadgets" --limit 10` | Load real Temu products into the storefront: **one** RapidAPI search, no LLM calls. Prints inserted vs. skipped (duplicate) products. Add `--job ID` to collect an earlier search instead of starting one. |

Other flags: `--limit N`, `--force` (ignore de-duplication), `--log-level`.
Omit `--pipeline` to run every enabled pipeline. Exit codes: `0` ok, `1` failures,
`2` configuration error, `3` AI budget halt.

## Dashboard

```bash
streamlit run dashboard/app.py
python -m streamlit run dashboard/app.py     # same thing; prefer this form on Windows
```

1. **Pipelines** — flow cards (`source ➔ generator + LLM model ➔ social pages`), create/edit form,
   enable toggle, "Run Pipeline Now (Dry-Run)".
2. **KPIs & Budget** — views, clicks, conversions, revenue, net profit, monthly AI spend vs.
   `MAX_MONTHLY_AI_BUDGET_USD`, breakdowns by platform and pipeline, and **storefront click-throughs**
   per category and per source, counted live from `click_events` ("Refresh clicks" redraws them).
3. **Queue & Logs** — queued / published / failed items, retry, flush, redacted log tail.

Deep links: `?tab=kpis`, `?tab=queue`. Restart the dashboard after editing `.env`.
The dashboard has no login: keep it on `localhost` or behind a tunnel. It uses only Streamlit's basic
widgets plus HTML for tables and bars, so it does not need `pyarrow` to load.

## FindHub storefront

A consumer-facing deal catalogue served from the engine's own database. Hebrew (`/he`, right to left,
prices in ₪) is the default language; English (`/en`) is left to right and shows approximate US-dollar
prices.

| Address | What it is |
|---|---|
| `/{lang}` | Main storefront: every listed deal, a category pill bar, search, filters and sorting. |
| `/{lang}/c/{category}` | Category hub (`/he/c/sports`): its own headline, only that category, search scoped to it. Made for social bio links. |
| `/{lang}/p/{productId}` | Product deep link (`/he/p/1`): the product's hub with its Quick View already open. |

The language switch keeps the current page and its filters (`/he/c/sports` ⇄ `/en/c/sports`). Opening a
deal anywhere puts its `/p/{id}` address in the bar, so every open deal can be shared and Back closes it.

**What is listed.** A product appears only when it has a category and meets the storefront standard:
rating ≥ `STOREFRONT_MIN_RATING` (4.5) and orders ≥ `STOREFRONT_MIN_ORDERS` (200). A pipeline run files
new products under its product type; the seeder and `Database.upsert_product(product, listing={...})`
set the listing details a source does not know (`category`, `title_he`, `coupon_code`, `is_flash_sale`,
`stock_urgency_label`). A later pipeline run refreshes price, rating, orders and images and leaves those
details alone.

**Badges are computed, not typed in.** `Crowd Favorite` needs ≥ 1,000 orders and a rating ≥ 4.8.
`Flash Deal` follows `is_flash_sale`. `Algorithm Verified` means the product passed the standard above.
"High demand this week" appears after 5 real outbound clicks in 7 days; "Top rated / Most ordered in
category" compare against the category's current leaders. `stock_urgency_label` is the one free-text
field and is shown as stored, so only set it from data you can stand behind.

### API (`api/server.py`)

Interactive docs: <http://localhost:8000/api/docs>.

| Endpoint | Purpose |
|---|---|
| `GET /api/products` | `q` (fuzzy search), `category`, `source` (`aliexpress`, `temu`), `min_rating`, `min_orders`, `min_price`, `max_price`, `sort_by` (`rating_desc`, `price_asc`, `price_desc`, `orders_desc`, `discount_desc`), `page`, `page_size`. Returns the page, totals and the stores / price bounds for the filter controls. |
| `GET /api/products/{product_id}` | One product, for deep links and Quick View. |
| `GET /api/categories` | Active categories with product counts, plus the listing standard. |
| `POST /api/track-click/{product_id}` | Logs the confirmed outbound click (`language`, `referrer_path` in the JSON body) into `click_events` and returns the `affiliate_url`. |
| `GET /api/health` | Liveness and the number of listed products. |

The affiliate link is never part of a listing; only `track-click` hands it out, so every outbound visit
is counted. Search (`api/search.py`) forgives typos, partial words and plural endings in Hebrew and
English ("earbds", "smartwacth", "אוזנית"); it uses `rapidfuzz` when the library loads and a pure-Python
fallback otherwise. There is no rate limiting yet: put the site behind a proxy that has it before going public.

### Website (`storefront/`)

Next.js (App Router), Tailwind CSS, Framer Motion, lucide icons. The browser only talks to the Next.js
server, which forwards `/api/*` to the backend, so the storefront and its API share one domain.

```bash
cd storefront
npm install
npm run dev          # http://localhost:3000, reloads on edit
npm run build        # production build (type-checks the project)
npm run start        # serve the production build
```

Optional settings go in `storefront/.env.local` (see `storefront/.env.example`):

| Variable | Default | Meaning |
|---|---|---|
| `API_ORIGIN` | `http://127.0.0.1:8000` | Where the backend listens. Read when the site starts in dev and **when it is built** for production. |
| `NEXT_PUBLIC_SITE_URL` | `http://localhost:3000` | Public address, for canonical links and social previews. |
| `NEXT_PUBLIC_ILS_PER_USD` | `3.5` | Shekels per dollar for the approximate prices on `/en`. Update it when the rate moves. |

Saved deals live in the visitor's browser (`localStorage`); nothing about them reaches the server. All
texts are in `src/lib/i18n.ts`, and the design tokens (colours, type scale) in `tailwind.config.ts`.

## Configuration

* **`.env`** — secrets and settings, validated by Pydantic on startup (see `.env.example`).
  Set `TARSHISH_ENV_FILE` to load a different file, for example on a server.
* **`config/pipelines.yaml`** — seeds the `pipelines` table on first start. After that SQLite is the
  source of truth (edit in the dashboard); `--sync-config` pushes the file again. It ships two
  pipelines: `sports_he` (offline mock source) and `gadgets_he` (real Temu products ➔ Instagram).
  A pipeline names its product type with the `category:` key.
* **Secrets never enter the database or YAML.** A social page stores the *names* of the env vars:

  ```json
  {"platform": "telegram_pub", "page_name": "@my_channel",
   "token_env_var": "TG_SPORTS_BOT_TOKEN", "chat_id_env_var": "TG_SPORTS_CHAT_ID"}
  ```

  A missing variable logs a warning and skips only that target. Raw tokens in config are rejected.

### LLM models

`llm_model` (or `llm_provider`) accepts a model id or an alias:

| Value | Meaning |
|---|---|
| `anthropic` | Anthropic, default `claude-opus-5-5` (override with `ANTHROPIC_MODEL`) |
| `gemini` | Gemini, default `gemini-3.5-flash-lite` (override with `GEMINI_MODEL`) |
| `claude-…` / `gemini-…` | That exact model |
| `mock` | Free offline templates |

Cheaper Claude options: `claude-sonnet-5-5`, `claude-haiku-5-5`. For models that support it the
Anthropic request enables server-side refusal fallback (`fallbacks: "default"`) and runs at
`effort: low` (`ANTHROPIC_EFFORT`). Prices live in `MODEL_PRICING` in
`plugins/generators/llm_text_gen.py`; extend with `LLM_PRICE_OVERRIDES`.

### Temu source (`temu_rapidapi`)

Real Temu listings through the "Temu Product Scraper" API on RapidAPI. The plan meters **searches**
(the free BASIC plan allows 5 per month), so the plugin starts **one search per pipeline run** (or per
`--seed-temu`), asks for a batch of `TEMU_MAX_ITEMS` (20) and does the rest locally: the pipeline's
`min_rating` / `min_orders`, ranking by rating and sales volume, and de-duplication on the store id and
`canonical_hash`.

A search is a background job: the API answers with a job id, and the plugin waits and polls until the
results are ready (up to `TEMU_JOB_TIMEOUT`, 300 seconds). If a job takes longer, the error names its
id and `python main.py --seed-temu --category gadgets --job <id>` collects it later without starting a
new search.

| Variable | Meaning |
|---|---|
| `RAPIDAPI_KEY` | Required for live requests. Masked as `[REDACTED]` in every log line. |
| `RAPIDAPI_TEMU_HOST` | API host, default `temu-product-scraper.p.rapidapi.com`. |
| `TEMU_TRACKING_ID` | Your affiliate tracking value. Empty: links are plain product URLs and a warning is logged. |
| `TEMU_AFFILIATE_URL_TEMPLATE` | Default `{url}?_x_sessn_id={tracking_id}&refer_page_name=affiliate`. |
| `TEMU_MAX_ITEMS`, `TEMU_CURRENCY` | Batch size per search (20) and the currency of the reported prices (`USD`). |
| `TEMU_JOB_TIMEOUT` | Seconds to wait for a search job to finish (300). |

Search terms are per pipeline, under `options.search_config`: `keyword` (default: the pipeline's
category), `l2Category`, `priceMin`, `priceMax`, `maxItems`. Details worth knowing:

* **A `--dry-run` spends no quota.** It previews the Temu products already stored for the category, so
  run `--seed-temu` once first. Without stored products the preview is empty.
* **Products the pipeline already posted are ranked last**, behind fresh ones, and the orchestrator skips
  them before any LLM call. `--force` and back-filling a newly added page therefore keep working. Set
  `skip_published: true` in `search_config` to drop them at the source instead.
* **`--seed-temu`** filters against the storefront standard (`STOREFRONT_MIN_RATING` / `_MIN_ORDERS`),
  inserts the best `--limit` products that SQLite does not hold yet, and leaves stored ones untouched.
* A product without a photo in the API payload gets a labelled stand-in picture. The storefront shows
  it; the Instagram publisher refuses to post it.

### Instagram publisher (`instagram_pub`)

Publishes through the Instagram Graph API in two steps (create a media container, then publish it);
video content becomes a Reel once Meta has processed it. It needs an Instagram business account and a
token with `instagram_content_publish`. Credentials come from `INSTAGRAM_ACCESS_TOKEN` and
`INSTAGRAM_BUSINESS_ACCOUNT_ID`, or from the variables a social page names:

```json
{"platform": "instagram_pub", "page_name": "@my_gadgets",
 "token_env_var": "IG_GADGETS_TOKEN", "account_id_env_var": "IG_GADGETS_ACCOUNT_ID"}
```

* Captions cannot carry a clickable link, so the post ends with `🔗 Link in bio -> /c/<category>`
  (Hebrew wording for Hebrew posts) instead of the affiliate URL. Put the storefront link in the bio.
* Meta downloads the media itself: the image or video must be a public URL (not a local file).
* Without credentials the page is skipped with a warning and the other targets still run. To preview
  the exact Instagram caption and media URL in a dry-run, set `DRY_RUN_PUBLISHER=instagram_pub`.
* `INSTAGRAM_GRAPH_API_VERSION` selects the Graph API version (default `v21.0`).

## Resilience

* **AI budget guardrail** — before every paid LLM call the month-to-date spend is checked; at
  `MAX_MONTHLY_AI_BUDGET_USD` (default `$5`) `BudgetExceededError` is raised and the run halts.
* **Retries** — `@with_retry(max_attempts=3, base_delay=2.0)` wraps source, LLM and publisher calls.
* **Redaction** — one global logger masks API keys, bot tokens and secrets as `[REDACTED]`.
* **Fault isolation** — a failing product, target or pipeline never stops the others.
* **De-duplication** — `canonical_hash` prevents re-posting the same item, also across sources.

## Extending (no change to `core/`)

| To add | Create | Reference as |
|---|---|---|
| Product source | `plugins/sources/amazon.py` with a `BaseSource` subclass | `source: "amazon"` |
| Generator / video renderer | `plugins/generators/my_gen.py` with a `BaseGenerator` subclass | `generator: "my_gen"` |
| Publisher | `plugins/publishers/tiktok_pub.py` with a `BasePublisher` subclass | `publishers: ["tiktok_pub"]` |
| Storage backend | `storage/s3_storage.py` with a `BaseStorage` subclass, `plugin_name = "s3"` | `STORAGE_MODE=s3` |

Publishers declare `required_credentials` and `default_env_vars`; any `<name>_env_var` key on a
social page is resolved from the environment and handed to the publisher.

## Database notes

SQLite (`tarshish_engine.db`) in WAL mode. Tables: `pipelines`, `products`, `publications`,
`metrics_snapshots`, plus two additions to the original spec:

* **`ai_usage`** — append-only ledger of LLM spend. The budget guardrail and `--stats` sum this
  table, so flushing the queue cannot erase money that was already spent.
* Extra columns: `publications.created_at / target_page / target_json / content_json / attempts`
  (queue replay and per-page de-duplication), `products.currency`, `pipelines.options_json`.
* **Storefront columns on `products`** — `category`, `title_he`, `original_price`, `discount_percent`,
  `rating`, `orders_count`, `image_urls_json`, `coupon_code`, `is_flash_sale`, `stock_urgency_label`.
  They are added with `ALTER TABLE` on the first start after the upgrade (`Database._migrate`); existing
  rows are kept and stay off the storefront until a pipeline run or the seeder upserts them again.
* **`click_events`** — one row per confirmed outbound click on the storefront: `product_id`, `category`,
  `source`, `language`, `referrer_path`, `clicked_at`.

`metrics_snapshots` rows are cumulative counters; KPIs use the latest snapshot per publication.

## Tests

```bash
python -m pip install -r requirements-dev.txt
python -m pytest tests -q
```

The suite needs no network, API keys or `.env`. Every test gets its own temporary database, kept under
`.pytest_tmp/` inside the project (see `pytest.ini`) rather than in the shared system temp folder.

| File | Covers |
|---|---|
| `test_spec_contract.py` | The specified contract: model fields, plugin interfaces, schema, CLI flags, declared dependencies |
| `test_pipeline.py` | Dry-run, publishing, de-duplication, fault isolation, budget halt, queue, worker pacing, KPIs |
| `test_plugins.py` | Mock source, post formatting, Telegram (mocked transport), LLM request building and parsing |
| `test_llm_sdk_integration.py` | The real Anthropic and Gemini SDK clients against a local stand-in server |
| `test_config.py`, `test_resilience.py` | Startup validation, secret rejection, redaction, retries, budget guard |
| `test_dashboard.py` | The Streamlit app end to end: every tab, dry-run, the pipeline form, enable toggle, queue actions, storefront clicks, and rendering without `pyarrow` |
| `test_storefront.py` | Schema migration, the seeder, search (typos, Hebrew), every API endpoint, click tracking, click metrics |
| `test_temu_instagram.py` | Temu source (one request, local filtering, retries, key redaction), `--seed-temu`, Instagram publisher (image, Reel, dry-run, errors), the `gadgets_he` pipeline |

The storefront website is checked by its own build: `cd storefront && npm run build` fails on any
TypeScript error.

## Windows Smart App Control

**Symptoms.** `ImportError: DLL load failed while importing …: An Application Control policy has blocked
this file`, a `pip` / `streamlit` / `pytest` command that ends in "Access is denied" or "Permission denied",
or `[ENVIRONMENT ERROR] Windows blocked a library this program needs`.

**Cause.** On Windows 11 with Smart App Control switched on, Windows refuses to load files that are unsigned
and not yet known to Microsoft's reputation service. Python packages ship unsigned native libraries and pip
generates unsigned launchers, so a fresh install can be blocked. The verdict is not stable: the same package
can load in one environment and be blocked in the next.

**What the project does about it.**

* Every documented command uses `python -m …` (`python.exe` is signed) instead of the generated
  `pip.exe`, `streamlit.exe` and `pytest.exe` launchers.
* The dashboard avoids Streamlit's dataframe, data-editor and chart elements, so it does not need `pyarrow`,
  the library blocked most often.
* If an LLM SDK cannot be imported, the generator says so and falls back to the offline templates.

**If a library the engine cannot do without is blocked** (for example `pydantic_core`):

1. Try again a little later, then rebuild the environment (`python -m venv --clear .venv` and reinstall).
2. See exactly which file was refused: Event Viewer → Applications and Services Logs → Microsoft → Windows →
   CodeIntegrity → Operational, event 3077.
3. Develop inside WSL 2 or a Linux container, where Smart App Control does not apply.
4. Turning Smart App Control off (Windows Security → App & browser control → Smart App Control) removes the
   problem. On many Windows versions it cannot be switched back on without resetting the PC, so read the
   notice on that settings page and decide deliberately.
