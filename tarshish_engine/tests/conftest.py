"""Shared fixtures: an isolated engine (temp DB, no credentials, no real sleeping)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import bootstrap  # noqa: E402
from core import resilience  # noqa: E402
from core.config import Settings  # noqa: E402
from plugins.sources import temu_providers  # noqa: E402

_CREDENTIAL_VARS = (
    "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "TG_SPORTS_BOT_TOKEN", "TG_SPORTS_CHAT_ID",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "AFFILIATE_DISCLOSURE", "LLM_PRICE_OVERRIDES",
    "ANTHROPIC_MODEL", "GEMINI_MODEL", "ANTHROPIC_EFFORT", "LLM_MAX_OUTPUT_TOKENS",
    "STOREFRONT_ORIGINS", "STOREFRONT_MIN_RATING", "STOREFRONT_MIN_ORDERS",
    "RAPIDAPI_KEY", "RAPIDAPI_TEMU_HOST", "TEMU_TRACKING_ID", "TEMU_AFFILIATE_URL_TEMPLATE", "TEMU_MAX_ITEMS",
    "TEMU_CURRENCY", "TEMU_JOB_TIMEOUT", "INSTAGRAM_ACCESS_TOKEN", "INSTAGRAM_BUSINESS_ACCOUNT_ID", "INSTAGRAM_GRAPH_API_VERSION",
    "RAPIDAPI_KEY_TEMU_1", "RAPIDAPI_KEY_TEMU_2", "RAPIDAPI_KEY_TEMU_3", "RAPIDAPI_KEY_TEMU_4",
    "DRY_RUN_PUBLISHER",
)


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
    slept: list[float] = []
    monkeypatch.setattr(resilience, "_sleep", slept.append)
    yield slept
    resilience.setup_logging("INFO", None, console=False)  # release the temp log file (Windows)


@pytest.fixture
def make_settings(tmp_path):
    def factory(**overrides) -> Settings:
        values = dict(
            database_path=tmp_path / "test.db",
            local_media_dir=tmp_path / "media",
            log_file=tmp_path / "logs" / "test.log",
            pipelines_config_path=ROOT / "config" / "pipelines.yaml",
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
