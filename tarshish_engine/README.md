# Tarshish Engine

Plugin-based affiliate engine with a **two-stage, decoupled architecture**:

1. **Harvest** (language-agnostic) — fetch products per `(category, source)` into the database, once.
2. **Publish** (per language) — for `(category, language, content types)`, turn harvested products into
   localized posts, Stories and videos with the model routed to each content type, and publish or queue
   them to the account mapped to `(category, language, platform)`.

Adding a category, a language, an AI model, a product source, a video renderer or a publishing
destination never requires a change in `core/`.

The same database feeds **FindHub**, the public deal storefront: a FastAPI backend in `storefront_api/` and a
Next.js site in `storefront/`. See [FindHub storefront](#findhub-storefront).

The database is a local SQLite file for development and Supabase PostgreSQL in production
(`DATABASE_URL`). Production runs on Vercel (storefront + API), Streamlit Community Cloud (dashboard)
and GitHub Actions (scheduled harvest, publish and queue worker): see [`../DEPLOYMENT.md`](../DEPLOYMENT.md).

## Quick start

Python 3.14 or 3.15 (developed and tested on both; 3.11+ should work but is untested).
Every dependency installs from a prebuilt or pure-Python wheel, so no compiler is needed.

```bash
python -m venv --clear .venv
.venv\Scripts\activate            # Windows  (Linux/macOS: source .venv/bin/activate)
python -m pip install -r requirements-dev.txt   # everything: engine, API, dashboard, tests
copy .env.example .env            # optional: everything works offline without keys

python main.py --harvest --source temu_mock --category gadgets          # Stage 1: offline catalogue, no quota
python main.py --publish --category gadgets --lang he --dry-run         # Stage 2: preview the Hebrew posts + Stories
```

With no API keys the engine uses the offline Hebrew/English template writer and prints the
previews to the console, so the commands above work on a fresh checkout.

A step-by-step checklist from first run to production is in
[`../setup_roadmap.html`](../setup_roadmap.html) (open it in a browser).

### Running all three layers

The pipeline engine, the storefront backend and the storefront website are separate processes over one
database: locally the SQLite file `tarshish_engine.db` (WAL mode, so they can read and write at the same
time), or the Supabase database when `DATABASE_URL` is set. Run each in
its own terminal, from this folder, with the virtual environment active:

```bash
# 1. Python engine: harvest products (Stage 1), write and publish posts (Stage 2)
python main.py --seed-storefront                                   # once: load the demo catalogue
python main.py --harvest --source temu_mock --category gadgets
python main.py --publish --category gadgets --lang he --dry-run

# 2. FastAPI storefront backend: products, search, click tracking
python -m uvicorn storefront_api.server:app --reload --port 8000   # same as: uvicorn storefront_api.server:app --reload --port 8000

# 3. Next.js storefront frontend (Node.js 20.9+)
cd storefront && npm install && npm run dev
```

Then open <http://localhost:3000> (it redirects to the Hebrew storefront, `/he`). The dashboard
(`python -m streamlit run dashboard/app.py`) is optional and shows the storefront's clicks live.

> **Rebuild `.venv` after moving the folder or upgrading Python.** A virtualenv
> stores absolute paths and packages compiled for one Python version, so run the
> first three commands again (`--clear` wipes the old environment first).
> `python main.py` prints an `[ENVIRONMENT ERROR]` with these steps when it detects this.

## Architecture: two decoupled stages

```
                    config/categories.yaml ─┐   config/content_profiles.yaml ─┐   config/social_accounts.yaml ─┐
                                            ▼                                 ▼                                ▼
                                    categories table                content_profiles table           social_accounts table
                              (search rules per store,          (content type ➔ generator,        ((category, language, platform)
                               English, every language)           LLM model, tone, format)          ➔ handle + .env var NAMES)
                                            │                                 │                                │
  Stage 1 · --harvest                       ▼                                 │                                │
  (category, source) ──► source plugin ──► products  (one row per item, no language)                          │
                                            │                                 │                                │
  Stage 2 · --publish                       ▼                                 ▼                                ▼
  (category, language, content types) ──► unpublished products ──► generator [model] ──► publisher ──► account
                                                                                   publications (category, language,
                                                                                   content type, account, status)
```

* **Configuration is normalized into three concerns**, each a YAML seed file and a SQLite table edited in
  the dashboard. Nothing is repeated per language: search rules live once per category, models once per
  content type, and only the account matrix has a language axis.
* **Stage 1 never knows about languages.** `--harvest` reads `categories`, hands the category's rules for a
  store to that store's source plugin (`options["search_config"]`), re-applies the category's quality bar
  and upserts into `products` keyed on `(source, external_id)`. The same item from a second store is
  recognised by `canonical_hash` and kept once. No LLM call, no AI budget.
* **Stage 2 never fetches.** `--publish` resolves, per content type, the profile (generator + model +
  publish format) and the account for `(category, language, profile.platform)`, then selects harvested
  products of the category that have **no publication yet for that language, account and content type**
  (best sellers first). PUBLISHED, QUEUED/PENDING and FAILED rows all count as handled — failed ones are
  recovered with `--retry-failed`, never silently re-generated; `--force` lifts the rule.
* A **publishing pipeline** is a `(category, language)` pair with id `<category>_<language>`
  (`gadgets_he`); it labels `publications.pipeline_id`, the AI ledger, `--stats` and the KPIs.

## CLI

**Stage 1 — harvest** (language-agnostic, no LLM)

| Command | What it does |
|---|---|
| `python main.py --harvest` | Every enabled category, from every store it has search rules for (`temu_rapidapi`, `aliexpress_rapidapi`). |
| `python main.py --harvest --category gadgets` | One category. |
| `python main.py --harvest --source temu_rapidapi` | One store, every enabled category. |
| `python main.py --harvest --source temu_mock --category gadgets` | The offline catalogue (also `aliexpress_mock`): no API key, no quota. |

**Stage 2 — publish** (per category × language × content type)

| Command | What it does |
|---|---|
| `python main.py --publish --category gadgets --lang he --content-types instagram_post,instagram_story` | Generate with each content type's model and queue for the Hebrew account (`PUBLISHING_MODE`, default `queued`). |
| `python main.py --publish --category gadgets --lang en --dry-run` | Simulate every enabled content type that has an English account: no posts, no paid LLM calls, no DB writes. |
| `python main.py --publish --category gadgets --lang he --mode immediate` | Publish now instead of queueing. |
| `python main.py --publish --category gadgets` | Every language that has an enabled account for the category (omit `--category` too for everything). |
| `python main.py --mode worker` | Publish the queue with human-like delays inside active hours (`--once` for a single pass). |

**Management**

| Command | What it does |
|---|---|
| `python main.py --list-config` | Categories, content-type routing and the social-accounts matrix (with credential status). |
| `python main.py --sync-config` | Upsert `config/categories.yaml`, `content_profiles.yaml` and `social_accounts.yaml` into the database (overwrites dashboard edits). |
| `python main.py --stats [--category C --lang L]` | Queue size, published count, monthly AI spend, harvested products per category and source. |
| `python main.py --flush-queue` / `--retry-failed` | Delete pending/queued items (AI spend stays on record) / put failed items back in the queue; `--category` + `--lang` narrow them to one pipeline. |
| `python main.py --seed-storefront` | Load the 14-product demo catalogue into the storefront (an upsert; safe to repeat). |
| `python main.py --seed-temu --category gadgets --keyword "smart watch" --limit 10` | An ad-hoc harvest from `temu_rapidapi` that overrides the category's keyword for one run (filters against the storefront standard). Add `--job ID` to collect an earlier background search. `--seed-aliexpress` does the same for AliExpress. Both exit 1 when no provider delivers. |
| `python main.py --verify-instagram [--category C --lang L]` | Read-only check of the matrix's Instagram accounts. |
| `python main.py --verify-youtube [--category C --lang L]` | Read-only YouTube check: refreshes the OAuth token, confirms `youtube.upload`, shows the daily quota budget. |
| `python main.py --verify-tiktok [--category C --lang L]` | Read-only TikTok check: access token (refreshed when possible), Direct Post permission, allowed privacy levels. |

Other flags: `--limit N` (harvest: products kept per category and source; publish: products per content
type), `--force` (ignore de-duplication), `--log-level`. Exit codes: `0` ok, `1` failures,
`2` configuration error (or no stage chosen), `3` AI budget halt.

## Dashboard

```bash
streamlit run dashboard/app.py
python -m streamlit run dashboard/app.py     # same thing; prefer this form on Windows
```

1. **Categories & Search** — one card per category: display names (English / Hebrew), the Temu and
   AliExpress search rules, the quality bar, how many products are harvested per store, an enable toggle and
   **Harvest Now** for any source plugin. A form adds or edits categories (other keys of the stored rules,
   such as `l1Category` or `sort`, are preserved). The Temu and AliExpress provider-chain cards live here.
2. **Social Accounts** — the `category × language × platform` matrix, Hebrew and English columns side by side;
   each cell shows the handle, its `.env` variable names and whether it can publish (✅ ready, ⚠️ variable
   missing, ⏸ disabled, 🧩 no publisher plugin). A form links, edits or unlinks a cell. Raw tokens are refused.
   **Test Instagram Connection** checks the Instagram accounts read-only.
3. **Content Types & AI Models** — per content type: platform, generator, LLM model (any id can be typed),
   publish format, tone and an optional custom prompt. One change applies to every category and language.
4. **Run Pipelines & KPIs** — run a targeted pipeline (category, language, content types, dry-run on by
   default, mode, products per type) and read its routing table and the generated captions; the harvested
   inventory; then **KPIs & Budget** (views, clicks, revenue, net profit, monthly AI spend vs.
   `MAX_MONTHLY_AI_BUDGET_USD`, breakdowns by platform, content type and pipeline, storefront click-throughs)
   and **Queue & Logs** (queued / published / failed, retry, flush, worker pass, redacted log tail).

Deep links: `?tab=categories`, `?tab=accounts`, `?tab=content`, `?tab=run`. Restart the dashboard after
editing `.env`. The dashboard has no login: keep it on `localhost` or behind a tunnel. It uses only
Streamlit's basic widgets plus HTML for tables and bars, so it does not need `pyarrow` to load.

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
rating ≥ `STOREFRONT_MIN_RATING` (4.5) and orders ≥ `STOREFRONT_MIN_ORDERS` (200). A harvest files
products under their category id; the seeder and `Database.upsert_product(product, listing={...})`
set the listing details a source does not know (`category`, `title_he`, `coupon_code`, `is_flash_sale`,
`stock_urgency_label`). A later harvest refreshes price, rating, orders and images and leaves those
details alone.

**Badges are computed, not typed in.** `Crowd Favorite` needs ≥ 1,000 orders and a rating ≥ 4.8.
`Flash Deal` follows `is_flash_sale`. `Algorithm Verified` means the product passed the standard above.
"High demand this week" appears after 5 real outbound clicks in 7 days; "Top rated / Most ordered in
category" compare against the category's current leaders. `stock_urgency_label` is the one free-text
field and is shown as stored, so only set it from data you can stand behind.

### API (`storefront_api/server.py`)

On Vercel the same app is served by `api/index.py` (see `vercel.json`); `api/` holds only that entry
file because Vercel turns every Python file there into a function.

Interactive docs: <http://localhost:8000/api/docs>.

| Endpoint | Purpose |
|---|---|
| `GET /api/products` | `q` (fuzzy search), `category`, `source` (`aliexpress`, `temu`), `min_rating`, `min_orders`, `min_price`, `max_price`, `sort_by` (`rating_desc`, `price_asc`, `price_desc`, `orders_desc`, `discount_desc`), `page`, `page_size`. Returns the page, totals and the stores / price bounds for the filter controls. |
| `GET /api/products/{product_id}` | One product, for deep links and Quick View. |
| `GET /api/categories` | Active categories with product counts, plus the listing standard. |
| `POST /api/track-click/{product_id}` | Logs the confirmed outbound click (`language`, `referrer_path` in the JSON body) into `click_events` and returns the `affiliate_url`. |
| `GET /api/health` | Liveness and the number of listed products. |

The affiliate link is never part of a listing; only `track-click` hands it out, so every outbound visit
is counted. Search (`storefront_api/search.py`) forgives typos, partial words and plural endings in Hebrew and
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
* **`config/categories.yaml`** — categories and their language-agnostic search rules:

  ```yaml
  - category_id: "gadgets"                 # URL slug; the storefront serves /c/gadgets
    display_name_en: "Smart Gadgets"
    display_name_he: "גאדג'טים חכמים"
    temu_search: {keyword: "smart tech gadgets", l2Category: "Electronics & Appliances", priceMin: 1, priceMax: 100}
    aliexpress_search: {keyword: "smart gadgets", priceMin: 1, priceMax: 100}
    min_rating: 4.5
    min_orders: 500
    enabled: true
  ```

  `temu_rapidapi` and `temu_mock` read `temu_search`; the AliExpress sources read `aliexpress_search`. A
  store added later as a plugin is searched with `display_name_en`, so it needs no schema change.
* **`config/content_profiles.yaml`** — content type ➔ generator plugin, LLM model, platform, publish format
  and tone. Shipped: `instagram_post` (`llm_text_gen`, `claude-sonnet-5-5`, feed image),
  `instagram_story` (`llm_story_gen`, `claude-haiku-5-5`, Story), `tiktok_video` (`ffmpeg_video_gen`,
  `claude-sonnet-5-5`) and `telegram_post` (`llm_text_gen`, `claude-haiku-5-5`).
* **`config/social_accounts.yaml`** — the `(category_id, language, platform)` matrix: `account_handle`,
  `token_env_var`, `account_id_env_var`, `enabled`. The platform picks the publisher plugin by convention
  (`instagram` ➔ `instagram_pub`); `account_id_env_var` feeds that publisher's account credential
  (Instagram business account id, Telegram chat id). The TikTok rows ship disabled: no `tiktok_pub`
  publisher is installed yet.
* The three files **seed** their tables on first start. After that SQLite is the source of truth (edit in
  the dashboard); `python main.py --sync-config` pushes the files again.
* **Secrets never enter the database or YAML.** Accounts store the *names* of the env vars; a missing
  variable logs a warning and skips only that account. Raw tokens in config are rejected.

### LLM models

A content profile's `llm_model` accepts a model id or an alias:

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

Real Temu listings through RapidAPI. Free plans meter **searches** (5 to 50 a month) and any single
endpoint can be out of quota, unsubscribed, down or empty, so the source walks an **ordered chain of
providers** and stops at the first one that returns usable products. The providers behind it are not
asked, which keeps their quota. Each provider gets one search per run: a batch of `TEMU_MAX_ITEMS` (20)
that is filtered (`min_rating` / `min_orders`), ranked by rating and sales volume, and de-duplicated on
the store id and `canonical_hash` locally.

A provider is skipped, with one structured warning in the log, when:

| Situation | Outcome in logs, `--seed-temu` and the dashboard |
|---|---|
| Its API key variable is empty (and `RAPIDAPI_KEY` too) | `missing_key` — no request is made |
| HTTP 429 | `quota_exhausted` — that key is not asked again on that API for the rest of the run |
| HTTP 401 / 403 | `auth_failed` — wrong key, or the key is not subscribed to that API |
| Timeout or 5xx after two quick retries | `unavailable` |
| HTTP 200 with no items | `empty` |
| Items, but none passes the quality filter | `filtered_out` |
| A background search that outlives `TEMU_JOB_TIMEOUT` | `job_pending` — collect it later with `--job <id>` |

When every provider fails, the source logs a summary, returns nothing, and `--seed-temu` exits with
code 1. The dashboard's Categories & Search tab shows the chain, which keys are present and which provider
delivered last (**Temu RapidAPI Provider Chain**).

**The chain is `config/temu_providers.yaml`**, read on every run (no `--sync-config`):

| Provider | API | Free plan | Notes |
|---|---|---|---|
| `temu_primary_scraper` | [Temu Product Scraper](https://rapidapi.com/gioopasquale/api/temu-product-scraper) | 5 searches a month, 5 results each | No product photos. May answer with a background job, which the source waits for. |
| `temu_secondary_scraper`, `temu_realtime_backup` | — | — | **Switched off**: their hosts did not exist on RapidAPI on 2026-11-17. The adapters are ready for an API with that schema. |
| `temuscout` | [TemuScout API](https://rapidapi.com/dataocean/api/temuscout-api-temu-data-api) | 50 searches a month, 20–40 results each | Answers at once, with photos, list price and currency. Subscribe the key to it first; until then it answers 403 and is skipped at no cost. |

Adapters live in `plugins/sources/temu_providers/`. `TemuProductScraperAdapter`, `TemuScraper2Adapter`
and `TemuApiRealtimeAdapter` have fixed schemas. `adapter: generic` is described entirely in YAML —
query parameters as templates, dotted paths to the item list and to each field — so **another RapidAPI
Temu endpoint is a YAML entry, not a Python change** (`temuscout` is written that way; copy it).
For more quota on one API, list it twice with two `api_key_env` names (two RapidAPI accounts).

| Variable | Meaning |
|---|---|
| `RAPIDAPI_KEY` | Used by every provider whose own variable is empty. One RapidAPI key works for all APIs the account is subscribed to. |
| `RAPIDAPI_KEY_TEMU_1` … `_4` | Optional key per provider (the names come from `api_key_env` in the chain file). |
| `TEMU_PROVIDERS_CONFIG_PATH` | The chain file, default `config/temu_providers.yaml`. Without the file only the first provider is used. |
| `RAPIDAPI_TEMU_HOST` | Moves the first `TemuProductScraperAdapter` provider to another RapidAPI host. |
| `TEMU_TRACKING_ID` | Your affiliate tracking value. Empty: links are plain product URLs and a warning is logged. |
| `TEMU_AFFILIATE_URL_TEMPLATE` | Default `{url}?_x_sessn_id={tracking_id}&refer_page_name=affiliate`. |
| `TEMU_MAX_ITEMS`, `TEMU_CURRENCY` | Batch size per search (20) and the currency for providers that report none (`USD`). |
| `TEMU_JOB_TIMEOUT` | Seconds to wait for a background search to finish (300). |

Every variable whose name contains `KEY` is masked as `[REDACTED]` in logs, and keys are sent to
`*.rapidapi.com` hosts only (the chain file is rejected otherwise).

Search terms are a category's `temu_search` rules: `keyword` (default: the category id), `l1Category`,
`l2Category`, `priceMin`, `priceMax`, `maxItems`. Each provider is sent the ones its API supports.
Details worth knowing:

* **`--harvest --dry-run` spends no quota.** It previews the Temu products already stored for the category,
  or mock stand-ins, and writes nothing.
* **Harvesting is the only place a store is asked.** Publishing in two languages costs one search, not two.
* **Already-stored products** are reported as such; their price, rating and orders are refreshed in place.
* **`--seed-temu`** is an ad-hoc harvest that can override the keyword and filters against the storefront
  standard (`STOREFRONT_MIN_RATING` / `_MIN_ORDERS`) instead of the category's.
* A product without a photo in the API payload gets a labelled stand-in picture. The storefront shows
  it; the Instagram publisher refuses to post it.

### AliExpress RapidAPI source (`aliexpress_rapidapi`)

The same failover design as the Temu source, with its own chain in `config/aliexpress_providers.yaml`
and adapters in `plugins/sources/aliexpress_providers/` (`AliExpressDataHubAdapter`,
`AliExpressTrueApiAdapter`, `AliExpressUnofficialAdapter`, and the YAML-driven
`GenericConfigurableAliExpressAdapter`). Providers are tried top to bottom and the walk stops at the first one
that yields usable products; a missing key, HTTP 429 (remembered for the run), 401/403, timeout/5xx after two
quick retries, an empty reply or a batch that fails the quality filter all move on to the next provider.

| Variable | Purpose |
| --- | --- |
| `RAPIDAPI_KEY_ALI_1` … `_3` | One key per provider (falls back to `RAPIDAPI_KEY`); masked as `[REDACTED]` in logs. |
| `ALIEXPRESS_TRACKING_ID` | Your Portals tracking id; empty = the API's promotion link, or the plain product URL. |
| `ALIEXPRESS_AFFILIATE_URL_TEMPLATE` | Default `{url}?aff_fcid={tracking_id}&aff_platform=portals-tool`. A promotion link the API returns is kept as-is. |
| `ALIEXPRESS_PROVIDERS_CONFIG_PATH` | The chain file; without it the same three providers are used. |

A `--harvest --dry-run` spends no quota: it previews stored AliExpress products, else the `aliexpress_mock` catalogue.
The dashboard's **AliExpress RapidAPI Provider Chain** card shows the order, key status and the last run.

### Instagram publisher (`instagram_pub`)

Publishes through Meta's **Instagram API with Unified Login** (`https://graph.facebook.com/v21.0`, token in an
`Authorization: Bearer` header) in two steps: create a media container, poll its `status_code` with
exponential backoff until `FINISHED`, then `media_publish` it. It needs a Professional (Business or Creator)
account linked to a Meta Business Portfolio (no Facebook Page) and a Business Integration / System User token
with `instagram_business_basic` and `instagram_business_content_publish`. Credentials come from `INSTAGRAM_ACCESS_TOKEN` and
`INSTAGRAM_BUSINESS_ACCOUNT_ID`, or from the variables a social account names:

```yaml
- {category_id: "gadgets", language: "en", platform: "instagram", account_handle: "gadgets_en",
   token_env_var: "INSTAGRAM_ACCESS_TOKEN_GADGETS_EN", account_id_env_var: "INSTAGRAM_BUSINESS_ACCOUNT_ID_GADGETS_EN"}
```

* Captions cannot carry a clickable link, so the post ends with `🔗 Link in bio -> /c/<category>`
  (Hebrew wording for Hebrew posts) instead of the affiliate URL. Put the storefront link in the bio.
* Meta downloads the media itself: the image or video must be a public URL (not a local file).
* Without credentials the account is skipped with a warning and the other content types still run. To preview
  the exact Instagram caption and media URL in a dry-run, set `DRY_RUN_PUBLISHER=instagram_pub`.
* `INSTAGRAM_GRAPH_API_VERSION` selects the Graph API version (default `v21.0`).
* Formats come from the content profile's `publish_format`: `image` (feed photo), `reel`, or `story`
  (`media_type=STORIES`, image or video). Without one, video content becomes a Reel and anything else an
  image post. Stories take **no caption** (Meta rejects the field): `llm_story_gen` writes a ≤ 90-character
  overlay hook and a 9:16 frame layout (`output/story_frames/`) for the text to be baked into the media.
  The same account receives feed posts and Stories, each written by its own model.
* Meta errors are decoded (`error.code` / `error_subcode`) into a next step: expired token (190/463),
  missing permission (10, 200–299), unreachable media URL (9004/2207052), daily publishing limit (2207042)…
* `python main.py --verify-instagram [--category C --lang L]` (or **Test Instagram Connection** in the dashboard)
  checks the token (`/debug_token`, falling back to `/me/permissions`), the account type and both
  permissions with read-only calls, and prints `Connected as @user | BUSINESS | Permissions: ✓ … ✓ …`.

### YouTube Shorts publisher (`youtube_pub`)

Uploads through the **YouTube Data API v3** (`videos.insert`, `part=snippet,status`) as a chunked **resumable
upload**, so a dropped connection resumes instead of starting over. OAuth 2.0: `YOUTUBE_CLIENT_ID` /
`YOUTUBE_CLIENT_SECRET` plus a refresh token per channel (`YOUTUBE_REFRESH_TOKEN`, or the account's
`token_env_var`), exchanged for an access token at each upload. Generate one with
`python -m plugins.publishers.youtube_pub --authorize client_secret.json` (`google-auth-oauthlib`).

* Title = first caption line + `#Shorts` (≤ 100 characters); description = caption, affiliate link, hashtags,
  disclosure and `#Shorts`. Category 28 (Science & Technology) for tech collections, otherwise 22 (People &
  Blogs); override with `YOUTUBE_CATEGORY_ID`. `YOUTUBE_PRIVACY_STATUS` = public | unlisted | private.
* Each upload costs 1,600 quota units (default quota 10,000 a day ➔ 6 uploads). The publisher stops at
  `YOUTUBE_DAILY_QUOTA_UNITS` and, after `quotaExceeded`, skips further uploads until midnight Pacific time.
* Token refresh failures say what to do (`invalid_grant` ➔ a revoked token, or a consent screen still in
  Testing, whose tokens expire after 7 days).

### TikTok publisher (`tiktok_pub`)

**Content Posting API — Direct Post** (`open.tiktokapis.com`): `creator_info/query` ➔ `video/init`
(`post_info` + `FILE_UPLOAD` `source_info`) ➔ chunked `PUT` to the returned `upload_url` ➔ `status/fetch` until
`PUBLISH_COMPLETE`. The user access token (`TIKTOK_ACCESS_TOKEN`, or the account's `token_env_var`) lasts 24 hours;
with `TIKTOK_CLIENT_KEY`, `TIKTOK_CLIENT_SECRET` and a refresh token (`TIKTOK_REFRESH_TOKEN`, or the account's
`account_id_env_var`) it is refreshed before each post.

* The caption ends with `🔗 Link in bio -> /c/<category>` (TikTok captions carry no clickable link).
* `TIKTOK_PRIVACY_LEVEL` must be one the creator allows; unaudited apps can only post `SELF_ONLY`.
  `TIKTOK_COMMERCIAL_CONTENT` = none | your_brand | branded_content sets the disclosure toggles.
* Calls are paced under TikTok's 6 requests per minute per token and back off on `rate_limit_exceeded`; errors
  carry TikTok's `log_id` and a next step (`unaudited_client_can_only_post_to_private_accounts`,
  `access_token_invalid`, `spam_risk_too_many_posts`…).

Both video publishers need a real MP4 (`VIDEO_RENDER_ENABLED=true` + ffmpeg, or a source video; a stored copy
is downloaded first) and check it with `ffprobe` before uploading: vertical 9:16 and at most 60 seconds.

## Resilience

* **AI budget guardrail** — before every paid LLM call the month-to-date spend is checked; at
  `MAX_MONTHLY_AI_BUDGET_USD` (default `$5`) `BudgetExceededError` is raised and the run halts.
* **Retries** — `@with_retry(max_attempts=3, base_delay=2.0)` wraps source, LLM and publisher calls.
* **Redaction** — one global logger masks API keys, bot tokens and secrets as `[REDACTED]`.
* **Fault isolation** — a failing product, content type, harvest pair or pipeline never stops the others.
* **De-duplication** — `canonical_hash` keeps one product per item across stores at harvest time and
  prevents re-posting it to the same account in the same format.

## Extending (no change to `core/`)

| To add | Create | Use it |
|---|---|---|
| Product source | `plugins/sources/amazon.py` with a `BaseSource` subclass | `--harvest --source amazon` |
| Generator / video renderer | `plugins/generators/my_gen.py` with a `BaseGenerator` subclass | a content profile's `generator: "my_gen"` |
| Publisher | `plugins/publishers/tiktok_pub.py` with a `BasePublisher` subclass | accounts with `platform: "tiktok"` |
| Category, language, content type | a row in the dashboard or the YAML seed files | — |
| Storage backend | `storage/s3_storage.py` with a `BaseStorage` subclass, `plugin_name = "s3"` | `STORAGE_MODE=s3` |

Publishers declare `required_credentials`, `default_env_vars` and `account_credential` (the credential an
account's `account_id_env_var` feeds); generators read `content_type`, `tone` and `publish_format` from
`context.options`. `llm_story_gen` and `ffmpeg_video_gen` subclass `llm_text_gen` and override only the
default prompt, the offline writer and `finish()`.

## Database notes

Schema v3, on SQLite (`tarshish_engine.db`, WAL mode) or PostgreSQL (`DATABASE_URL`; the same tables
in `core/schema_postgres.sql`, applied on the first connect, with Row Level Security on so Supabase's
public APIs see nothing). Every query is written once for both; `core/database.py` translates the
placeholders for PostgreSQL. Copy a local database into Supabase with
`python main.py --import-sqlite tarshish_engine.db` (with `DATABASE_URL` set).

* **Configuration** — `categories` (`category_id` PK, display names, `temu_search_json`,
  `aliexpress_search_json`, `min_rating`, `min_orders`, `enabled`), `content_profiles` (`content_type` PK,
  `platform`, `generator`, `llm_model`, `publish_format`, `tone`, `prompt_template`, `enabled`),
  `social_accounts` (PK `(category_id, language, platform)`, `account_handle`, `token_env_var`,
  `account_id_env_var`, `enabled`).
* **`products`** — harvested once per `(source, external_id)`, filed under `category`; no language column.
  Storefront columns: `title_he`, `original_price`, `discount_percent`, `rating`, `orders_count`,
  `image_urls_json`, `coupon_code`, `is_flash_sale`, `stock_urgency_label`; plus `raw_url`, `video_url`.
* **`publications`** — one row per product × account × content type: `pipeline_id` (`<category>_<language>`),
  `category`, `language`, `content_format` (the content type), `account_platform`, publisher `platform`,
  `target_page`, status, caption, model, cost share, schedule, and the replay snapshot (`content_json`,
  `target_json`).
* **`ai_usage`** — append-only ledger of LLM spend. The budget guardrail and `--stats` sum this
  table, so flushing the queue cannot erase money that was already spent.
* **`metrics_snapshots`** (cumulative counters; KPIs use the latest per publication) and **`click_events`**
  (one row per confirmed outbound storefront click).

**Upgrading an existing database** happens on the first start, in place: `publications` is rebuilt without
its foreign key to the old `pipelines` table (ids, metrics and history are kept, and rows gain the
category/language of the pipeline they belonged to), and the old products column for categories is folded
into `products.category`. The legacy `pipelines` table is left untouched and unused; drop it by hand once
you no longer need it. Re-create the old pipelines as rows of the account matrix (Social Accounts tab).

## Tests

```bash
python -m pip install -r requirements-dev.txt
python -m pytest tests -q
```

The suite needs no network, API keys or `.env`. Every test gets its own temporary database, kept under
`.pytest_tmp/` inside the project (see `pytest.ini`) rather than in the shared system temp folder.

To run the same suite on PostgreSQL (as CI does), point it at a server; each test gets its own schema:

```bash
TARSHISH_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres python -m pytest tests -q
```

| File | Covers |
|---|---|
| `test_spec_contract.py` | The specified contract: model fields, plugin interfaces, schema, CLI flags, declared dependencies |
| `test_harvester.py` | Stage 1: one harvest per category and source for every language, refresh in place, quality bar, planning, the CLI harvest ➔ publish flow in he/en, the v2 ➔ v3 database upgrade |
| `test_orchestrator.py` | Stage 2: reads only harvested products, dry-run, per-account de-duplication, he/en accounts, fault isolation, budget halt, queue, worker pacing, paused accounts, KPIs |
| `test_plugins.py` | Mock source, post formatting, Telegram (mocked transport), LLM request building and parsing |
| `test_llm_sdk_integration.py` | The real Anthropic and Gemini SDK clients against a local stand-in server |
| `test_config.py`, `test_resilience.py` | The three seed files, validation, secret rejection, round trips, seed-once semantics, redaction, retries, budget guard |
| `test_dashboard.py` | The Streamlit app end to end: four tabs, Harvest Now, the category / account / content-type forms, the run form, queue actions, storefront clicks, and rendering without `pyarrow` |
| `test_storefront.py` | Schema migration, the seeder, search (typos, Hebrew), every API endpoint, click tracking, click metrics |
| `test_video_publishers.py` | YouTube Shorts (resumable chunks, `#Shorts` metadata, quota, token refresh, resume after 5xx, verify) and TikTok Direct Post (init/upload/status, chunk plan, token refresh, unaudited and rate-limit errors, verify) against mocked Google / TikTok endpoints; the CLI flags |
| `test_instagram_publisher.py` | Instagram Unified Login: feed image, Reel backoff, Story image/video (no caption), Meta error hints, `--verify-instagram` and the dashboard button |
| `test_temu_instagram.py` | Temu source (one request, local filtering, retries, background jobs, key redaction), `--seed-temu`, Instagram publisher (image, Reel, dry-run, errors), gadgets harvested once and published per language, posts and Stories with their own models |
| `test_temu_failover.py` | Temu provider chain: the 429 ➔ empty ➔ success failover, every failure mode, key fallback and exhausted-key memory, the four adapters, chain-file validation, key masking, the CLI summary |
| `test_aliexpress_failover.py` | AliExpress provider chain: 429 ➔ empty ➔ success, 401/403, retries, missing keys and `RAPIDAPI_KEY` fallback, the four adapters, affiliate links, key masking, dry-run, `--seed-aliexpress` |

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
