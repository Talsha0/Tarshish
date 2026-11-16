# Tarshish Engine

Plugin-based engine for automated affiliate content pipelines:
**fetch products ➔ generate content ➔ store ➔ publish or queue**.

Adding a niche pipeline, an AI model, a product source, a video renderer or a
publishing destination never requires a change in `core/`.

## Quick start

Python 3.14 or 3.15 (developed and tested on both; 3.11+ should work but is untested).
Every dependency installs from a prebuilt or pure-Python wheel, so no compiler is needed.

```bash
python -m venv --clear .venv
.venv\Scripts\activate            # Windows  (Linux/macOS: source .venv/bin/activate)
pip install -r requirements.txt
copy .env.example .env            # optional: everything works offline without keys

python main.py --pipeline sports_he --mode immediate --dry-run
```

With no API keys the engine uses the offline Hebrew/English template writer and
the console publisher, so the command above works on a fresh checkout.

A step-by-step checklist from first run to production is in
[`../setup_roadmap.html`](../setup_roadmap.html) (open it in a browser).

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

Other flags: `--limit N`, `--force` (ignore de-duplication), `--log-level`.
Omit `--pipeline` to run every enabled pipeline. Exit codes: `0` ok, `1` failures,
`2` configuration error, `3` AI budget halt.

## Dashboard

```bash
streamlit run dashboard/app.py
```

1. **Pipelines** — flow cards (`source ➔ generator + LLM model ➔ social pages`), create/edit form,
   enable toggle, "Run Pipeline Now (Dry-Run)".
2. **KPIs & Budget** — views, clicks, conversions, revenue, net profit, monthly AI spend vs.
   `MAX_MONTHLY_AI_BUDGET_USD`, breakdowns by platform and pipeline.
3. **Queue & Logs** — queued / published / failed items, retry, flush, redacted log tail.

Deep links: `?tab=kpis`, `?tab=queue`. Restart the dashboard after editing `.env`.
If the `streamlit` command is blocked or not found, `python -m streamlit run dashboard/app.py` does the same.
The dashboard has no login: keep it on `localhost` or behind a tunnel.

## Configuration

* **`.env`** — secrets and settings, validated by Pydantic on startup (see `.env.example`).
  Set `TARSHISH_ENV_FILE` to load a different file, for example on a server.
* **`config/pipelines.yaml`** — seeds the `pipelines` table on first start. After that SQLite is the
  source of truth (edit in the dashboard); `--sync-config` pushes the file again.
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

`metrics_snapshots` rows are cumulative counters; KPIs use the latest snapshot per publication.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests -q
```

The suite needs no network, API keys or `.env`. Every test gets its own temporary database.

| File | Covers |
|---|---|
| `test_spec_contract.py` | The specified contract: model fields, plugin interfaces, schema, CLI flags, declared dependencies |
| `test_pipeline.py` | Dry-run, publishing, de-duplication, fault isolation, budget halt, queue, worker pacing, KPIs |
| `test_plugins.py` | Mock source, post formatting, Telegram (mocked transport), LLM request building and parsing |
| `test_llm_sdk_integration.py` | The real Anthropic and Gemini SDK clients against a local stand-in server |
| `test_config.py`, `test_resilience.py` | Startup validation, secret rejection, redaction, retries, budget guard |
| `test_dashboard.py` | The Streamlit app renders every tab and runs a dry-run |
