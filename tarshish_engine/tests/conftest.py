"""Shared fixtures: an isolated engine (temp DB, no credentials, no real sleeping).

The engine uses a temporary SQLite file by default. Set ``TARSHISH_TEST_DATABASE_URL`` to a
PostgreSQL connection string to run the same tests against PostgreSQL instead: every test then
gets its own empty schema, dropped afterwards (CI does this against a Postgres service).
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from urllib.parse import quote

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import bootstrap  # noqa: E402
from core import resilience  # noqa: E402
from core.config import Settings  # noqa: E402
from core.database import close_pools  # noqa: E402
from plugins.sources import aliexpress_providers, temu_providers  # noqa: E402

_CREDENTIAL_VARS = (
    "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "TG_SPORTS_BOT_TOKEN", "TG_SPORTS_CHAT_ID",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "AFFILIATE_DISCLOSURE", "LLM_PRICE_OVERRIDES",
    "ANTHROPIC_MODEL", "GEMINI_MODEL", "ANTHROPIC_EFFORT", "LLM_MAX_OUTPUT_TOKENS",
    "STOREFRONT_ORIGINS", "STOREFRONT_MIN_RATING", "STOREFRONT_MIN_ORDERS",
    "RAPIDAPI_KEY", "RAPIDAPI_TEMU_HOST", "TEMU_TRACKING_ID", "TEMU_AFFILIATE_URL_TEMPLATE", "TEMU_MAX_ITEMS",
    "TEMU_CURRENCY", "TEMU_JOB_TIMEOUT", "INSTAGRAM_ACCESS_TOKEN", "INSTAGRAM_BUSINESS_ACCOUNT_ID", "INSTAGRAM_GRAPH_API_VERSION",
    "RAPIDAPI_KEY_TEMU_1", "RAPIDAPI_KEY_TEMU_2", "RAPIDAPI_KEY_TEMU_3", "RAPIDAPI_KEY_TEMU_4",
    "DRY_RUN_PUBLISHER",
    "RAPIDAPI_KEY_ALI_1", "RAPIDAPI_KEY_ALI_2", "RAPIDAPI_KEY_ALI_3", "ALIEXPRESS_TRACKING_ID",
    "ALIEXPRESS_AFFILIATE_URL_TEMPLATE", "ALIEXPRESS_MAX_ITEMS", "ALIEXPRESS_CURRENCY",
    "DATABASE_URL", "POSTGRES_URL", "DATABASE_POOL_SIZE", "DASHBOARD_PASSWORD",
    "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_SECRET_KEY", "SUPABASE_STORAGE_BUCKET",
)
POSTGRES_TEST_URL = os.environ.get("TARSHISH_TEST_DATABASE_URL", "").strip()


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch, tmp_path):
    """No test may depend on (or leak) the developer's real credentials, and none really sleeps."""
    for name in _CREDENTIAL_VARS:
        monkeypatch.delenv(name, raising=False)
    # Entry points that load `.env` themselves (CLI, dashboard) must not find the developer's file.
    monkeypatch.setenv("TARSHISH_ENV_FILE", str(tmp_path / "no-such.env"))
    # The Temu source uses its built-in single-provider chain unless a test names a chain file,
    # and no test inherits another one's "quota exhausted" memory.
    monkeypatch.setenv("TEMU_PROVIDERS_CONFIG_PATH", str(tmp_path / "no-such-temu-providers.yaml"))
    temu_providers.reset_run_state()
    # The same for the AliExpress chain: the built-in chain unless a test names a file.
    monkeypatch.setenv("ALIEXPRESS_PROVIDERS_CONFIG_PATH", str(tmp_path / "no-such-aliexpress-providers.yaml"))
    aliexpress_providers.reset_run_state()
    slept: list[float] = []
    monkeypatch.setattr(resilience, "_sleep", slept.append)
    yield slept
    resilience.setup_logging("INFO", None, console=False)  # release the temp log file (Windows)


@pytest.fixture
def postgres_url():
    """A fresh PostgreSQL schema for this test (``None`` when testing on SQLite)."""
    if not POSTGRES_TEST_URL:
        yield None
        return
    import psycopg

    schema = f"test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(POSTGRES_TEST_URL, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
    separator = "&" if "?" in POSTGRES_TEST_URL else "?"
    yield f"{POSTGRES_TEST_URL}{separator}options={quote(f'-csearch_path={schema}')}"
    close_pools()
    with psycopg.connect(POSTGRES_TEST_URL, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA {schema} CASCADE")


@pytest.fixture
def make_settings(tmp_path, postgres_url):
    def factory(**overrides) -> Settings:
        values = dict(
            database_url=postgres_url,
            database_path=tmp_path / "test.db",
            local_media_dir=tmp_path / "media",
            log_file=tmp_path / "logs" / "test.log",
            config_dir=ROOT / "config",
            max_monthly_ai_budget_usd=5.0,
            storage_mode="local",
            worker_active_hours_start=0,
            worker_active_hours_end=24,
            worker_min_delay_seconds=60,
            worker_max_delay_seconds=120,
        )
        values.update(overrides)
        return Settings(**values)

    return factory


@pytest.fixture
def engine(make_settings):
    return bootstrap(settings=make_settings(), console_logging=False)


@pytest.fixture
def harvested(engine):
    """Stage 1 done offline: the mock catalogues harvested into ``products`` (sports + gadgets)."""
    engine.harvester.harvest("sports", "aliexpress_mock")
    engine.harvester.harvest("gadgets", "temu_mock")
    return engine


@pytest.fixture
def console_route(engine):
    """A credential-free route: content type ``console_post`` ➔ console_pub accounts per category × language."""
    from core.config import ContentProfile, SocialAccount

    engine.registry.save_content_profile(ContentProfile(
        content_type="console_post", platform="console", generator="llm_text_gen", llm_model="mock",
    ))
    for category in ("sports", "gadgets"):
        for language in ("he", "en"):
            engine.registry.save_social_account(SocialAccount(
                category_id=category, language=language, platform="console",
                account_handle=f"{category}_{language}_console",
            ))
    return engine
