"""Pins the public contract of the engine, section by section of the original specification.

A failure here means the project no longer matches what was specified (or what
teammates rely on). Change these assertions deliberately, never just to get green.
"""

import ast
import inspect
import os
import re
import sys
import time
import typing
from pathlib import Path

import pytest

from core.config import ContentProfile, SocialAccount
from core.interfaces import BaseGenerator, BasePublisher, BaseSource, BaseStorage, PluginContext
from core.models import GeneratedContent, Product, PublishReceipt
from dashboard.metrics_service import MetricsService

ROOT = Path(__file__).resolve().parents[1]


# -- §1 directory & module structure ---------------------------------------------- #
def test_specified_files_exist():
    expected = [
        ".env.example", "requirements.txt", "README.md", "main.py",
        "config/categories.yaml", "config/content_profiles.yaml", "config/social_accounts.yaml",
        "core/__init__.py", "core/config.py", "core/models.py", "core/interfaces.py", "core/registry.py",
        "core/database.py", "core/harvester.py", "core/orchestrator.py", "core/worker.py", "core/resilience.py",
        "storage/__init__.py", "storage/local_storage.py", "storage/cloud_storage_stub.py",
        "plugins/sources/__init__.py", "plugins/sources/aliexpress_mock.py",
        "plugins/generators/__init__.py", "plugins/generators/llm_text_gen.py",
        "plugins/generators/llm_story_gen.py", "plugins/generators/ffmpeg_video_gen.py",
        "plugins/publishers/__init__.py", "plugins/publishers/console_pub.py", "plugins/publishers/telegram_pub.py",
        "dashboard/metrics_service.py", "dashboard/app.py",
    ]
    missing = [path for path in expected if not (ROOT / path).is_file()]
    assert not missing
    assert (ROOT / "output").is_dir()
    # Zero duplication: the per-language pipeline definitions are gone for good.
    assert not (ROOT / "config" / "pipelines.yaml").exists() and not (ROOT / "core" / "pipeline.py").exists()


# -- §2 core data models -------------------------------------------------------------- #
def test_domain_models_expose_exactly_the_specified_fields():
    assert list(Product.model_fields) == [
        "external_id", "source", "canonical_hash", "category", "title", "price", "original_price", "currency",
        "rating", "orders_count", "raw_url", "affiliate_url", "image_urls", "video_url",
    ]
    assert Product.model_fields["currency"].default == "ILS"
    assert list(GeneratedContent.model_fields) == [
        "product", "content_type", "content_format", "language", "caption", "hashtags", "media_local_path", "media_public_url",
        "model_used", "tokens_input", "tokens_output", "generation_cost_usd",
    ]
    assert typing.get_args(GeneratedContent.model_fields["content_type"].annotation) == ("text", "video")
    assert list(PublishReceipt.model_fields) == [
        "platform", "platform_post_id", "published_at", "success", "error_message",
    ]


# -- §3 plugin contracts ----------------------------------------------------------------- #
def test_plugin_contracts_have_the_specified_members_and_signatures():
    contracts = {
        BaseSource: ("source_name", "fetch_products", ["category", "min_rating", "min_orders", "limit"]),
        BaseGenerator: ("generator_name", "generate", ["product", "language", "prompt_template"]),
        BasePublisher: ("platform_name", "publish", ["content"]),
    }
    for base, (name_property, method, parameters) in contracts.items():
        assert inspect.isabstract(base)
        assert {name_property, method} <= base.__abstractmethods__
        assert isinstance(getattr(base, name_property), property)
        assert list(inspect.signature(getattr(base, method)).parameters) == ["self", *parameters]
    assert {"save", "delete"} <= BaseStorage.__abstractmethods__
    assert list(inspect.signature(BaseStorage.save).parameters) == ["self", "local_path", "remote_key"]
    assert list(inspect.signature(BaseStorage.delete).parameters) == ["self", "remote_key"]


def test_every_shipped_plugin_is_discovered_without_touching_core(engine):
    # _mock_catalogue is data, not a plugin
    assert engine.plugins.names("source") == ["aliexpress_mock", "aliexpress_rapidapi", "temu_mock", "temu_rapidapi"]
    assert engine.plugins.names("generator") == ["ffmpeg_video_gen", "llm_story_gen", "llm_text_gen"]
    assert engine.plugins.names("publisher") == ["console_pub", "instagram_pub", "telegram_pub"]
    assert engine.plugins.names("storage") == ["cloud", "local"]
    assert engine.plugins.load_errors == {}
    # core/ must not name a concrete plugin module (the dry-run publisher is a setting, not an import).
    for path in (ROOT / "core").glob("*.py"):
        assert not re.search(r"^\s*(from|import)\s+(plugins|storage)\b", path.read_text(encoding="utf-8"), re.M), path


def test_a_new_plugin_file_is_picked_up_with_zero_core_changes(engine, tmp_path, monkeypatch):
    """Drop a module into plugins/sources and it can be harvested, then published from."""
    import plugins.sources as sources_package
    from core.registry import PluginRegistry

    extra = tmp_path / "extra_sources"
    extra.mkdir()
    (extra / "demo_feed.py").write_text(
        "from core.interfaces import BaseSource\n"
        "from core.models import Product\n\n"
        "class DemoFeed(BaseSource):\n"
        "    @property\n"
        "    def source_name(self):\n"
        "        return 'demo_feed'\n\n"
        "    def fetch_products(self, category, min_rating, min_orders, limit):\n"
        "        return [Product(external_id='d1', source='demo_feed', category=category, title='Demo Yoga Mat 6mm',\n"
        "                        price=59.9, rating=4.9, orders_count=900, raw_url='https://shop.example/d1',\n"
        "                        affiliate_url='https://shop.example/aff/d1')]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sources_package, "__path__", [*sources_package.__path__, str(extra)])
    monkeypatch.setattr(engine.registry, "plugins", PluginRegistry().discover())
    assert "demo_feed" in engine.registry.plugins.names("source")

    monkeypatch.setattr(engine.harvester, "plugins", engine.registry.plugins)
    assert engine.harvester.harvest("gadgets", "demo_feed")[0].ok  # a new store needs no schema change
    console_profile(engine)
    report = engine.orchestrator.run("gadgets", "en", ["console_post"], mode="immediate")
    assert report.ok and report.count("published") == 1
    sys.modules.pop("plugins.sources.demo_feed", None)


# -- §5 / §9 database ---------------------------------------------------------------------- #
def test_database_uses_wal_and_has_the_specified_schema(engine):
    spec = {
        "categories": {"category_id", "display_name_en", "display_name_he", "temu_search_json",
                       "aliexpress_search_json", "min_rating", "min_orders", "enabled"},
        "content_profiles": {"content_type", "platform", "generator", "llm_model", "publish_format", "tone",
                             "prompt_template", "enabled"},
        "social_accounts": {"category_id", "language", "platform", "account_handle", "token_env_var",
                            "account_id_env_var", "enabled"},
        "products": {"id", "external_id", "source", "canonical_hash", "category", "title", "price",
                     "affiliate_url", "created_at"},
        "publications": {"id", "pipeline_id", "product_id", "category", "language", "content_format",
                         "account_platform", "platform", "content_type", "status", "caption",
                         "media_uri", "model_used", "generation_cost_usd", "platform_post_id", "scheduled_for",
                         "published_at", "error_message"},
        "metrics_snapshots": {"id", "publication_id", "views", "clicks", "conversions", "revenue_usd", "recorded_at"},
    }
    with engine.db.connect() as conn:
        assert conn.execute("PRAGMA journal_mode;").fetchone()[0] == "wal"
        for table, columns in spec.items():
            actual = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            assert columns <= actual, f"{table} is missing {columns - actual}"

        indexed = {}
        for index in conn.execute("PRAGMA index_list(products)").fetchall():
            columns = tuple(row["name"] for row in conn.execute(f"PRAGMA index_info({index['name']})"))
            indexed[columns] = bool(index["unique"])
        assert indexed.get(("source", "external_id")) is True  # UNIQUE (source, external_id)
        assert ("canonical_hash",) in indexed                    # index on canonical_hash
        tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert "pipelines" not in tables                          # a fresh database has no per-language pipelines
        keys = [row["name"] for row in conn.execute("PRAGMA table_info(social_accounts)") if row["pk"]]
        assert keys == ["category_id", "language", "platform"]

        publications_sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'publications'").fetchone()[0]
        for status in ("PENDING", "QUEUED", "PUBLISHED", "FAILED"):
            assert f"'{status}'" in publications_sql


def test_metrics_service_exposes_the_specified_aggregations(engine):
    metrics = MetricsService(engine.db, engine.settings)
    summary = metrics.get_overall_summary()
    assert {"total_views", "total_clicks", "total_conversions", "revenue_usd", "ai_spend_usd", "net_profit_usd",
            "total_text", "total_videos"} <= set(summary)
    assert metrics.get_metrics_by_platform() == []
    assert metrics.get_metrics_by_pipeline() == [] and metrics.get_metrics_by_content_type() == []


# -- §6 resilience ------------------------------------------------------------------------------ #
def test_retry_decorator_wraps_every_external_call():
    from plugins.generators.llm_text_gen import LLMTextGenerator
    from plugins.publishers.instagram_pub import InstagramPublisher
    from plugins.publishers.telegram_pub import TelegramPublisher
    from plugins.sources.aliexpress_mock import AliExpressMockSource
    from plugins.sources.temu_rapidapi import TemuRapidApiSource

    wrapped = [
        AliExpressMockSource.fetch_products,   # source fetching
        TemuRapidApiSource._request,
        LLMTextGenerator._call_anthropic,      # LLM calls
        LLMTextGenerator._call_gemini,
        TelegramPublisher._send,               # publishers
        TelegramPublisher._asend,
        InstagramPublisher._call,
        InstagramPublisher._await_container,
    ]
    assert all(hasattr(function, "__wrapped__") for function in wrapped)


# -- §8 CLI ------------------------------------------------------------------------------------------ #
def test_cli_exposes_the_specified_flags():
    import main

    actions = main.build_parser()._actions
    options = {option for action in actions for option in action.option_strings}
    assert {"--harvest", "--publish", "--source", "--category", "--lang", "--content-types", "--mode", "--limit",
            "--dry-run", "--stats", "--flush-queue", "--sync-config", "--list-config", "--seed-storefront",
            "--seed-temu", "--keyword", "--job"} <= options
    assert "--pipeline" not in options
    mode = next(action for action in actions if "--mode" in action.option_strings)
    assert list(mode.choices) == ["immediate", "queued", "worker"]


def test_cli_dry_run_exits_cleanly(make_settings, monkeypatch, capsys):
    import main

    settings = make_settings()
    for name, value in {
        "DATABASE_PATH": settings.database_path, "LOG_FILE": settings.log_file, "LOCAL_MEDIA_DIR": settings.local_media_dir,
    }.items():
        monkeypatch.setenv(name, str(value))

    assert main.main(["--harvest", "--source", "aliexpress_mock", "--category", "sports"]) == 0
    capsys.readouterr()
    assert main.main(["--publish", "--category", "sports", "--lang", "he", "--mode", "immediate", "--dry-run"]) == 0
    assert capsys.readouterr().out.count("DRY-RUN PREVIEW") == 3
    assert main.main(["--stats"]) == 0
    out = capsys.readouterr().out
    assert "Monthly AI spend" in out and "sports/aliexpress_mock 3" in out
    assert main.main(["--list-config"]) == 0
    assert "instagram_story  enabled  instagram  llm_story_gen [claude-haiku-5-5]" in capsys.readouterr().out
    assert main.main(["--publish", "--category", "does_not_exist", "--lang", "he", "--dry-run"]) == 1


# -- stubs & storage --------------------------------------------------------------------------------------- #
def console_profile(engine, content_type="console_post", generator="llm_text_gen"):
    """A credential-free route for one content type: console_pub accounts for gadgets/sports in he/en."""
    engine.registry.save_content_profile(ContentProfile(
        content_type=content_type, platform="console", generator=generator, llm_model="mock",
    ))
    for category in ("gadgets", "sports"):
        for language in ("he", "en"):
            engine.registry.save_social_account(SocialAccount(
                category_id=category, language=language, platform="console", account_handle="default",
            ))


def test_video_generator_runs_end_to_end(engine):
    engine.harvester.harvest("sports", "aliexpress_mock")
    console_profile(engine, "console_video", "ffmpeg_video_gen")

    preview = engine.orchestrator.run("sports", "en", ["console_video"], limit=1, dry_run=True)
    jobs_dir = engine.settings.local_media_dir / "render_jobs"
    assert preview.count("preview") == 1 and not jobs_dir.exists()  # dry-run writes nothing

    report = engine.orchestrator.run("sports", "en", ["console_video"], limit=1, mode="immediate")
    assert report.ok and report.count("published") == 1
    assert engine.db.list_publications(pipeline_id="sports_en")[0]["content_type"] == "video"
    assert len(list(jobs_dir.glob("*.json"))) == 1  # rendering is off by default: the render job is written


def test_rendered_media_is_handed_to_the_storage_backend(engine, tmp_path, monkeypatch):
    video = engine.plugins.get("generator", "ffmpeg_video_gen")

    def render(self, product, language):
        path = tmp_path / "render.mp4"
        path.write_bytes(b"not really a video")
        return str(path)

    monkeypatch.setattr(video, "_render_enabled", lambda self: True)
    monkeypatch.setattr(video, "_render", render)
    engine.harvester.harvest("sports", "aliexpress_mock")
    console_profile(engine, "console_video", "ffmpeg_video_gen")
    engine.orchestrator.run("sports", "en", ["console_video"], limit=1, mode="immediate")

    stored = Path(engine.db.list_publications(pipeline_id="sports_en")[0]["media_uri"])
    media_root = engine.settings.local_media_dir.resolve()
    assert stored.is_file() and media_root in stored.resolve().parents and stored.suffix == ".mp4"


def test_local_storage_round_trip_retention_and_path_safety(make_settings, tmp_path):
    from storage.local_storage import LocalStorage

    storage = LocalStorage(make_settings(media_retention_days=1))
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"x")
    stored = Path(storage.save(str(source), "sports_he/20261116/clip.mp4"))
    assert stored.is_file() and stored.parent.parent.name == "sports_he"
    with pytest.raises(ValueError):
        storage.save(str(source), "../escape.mp4")

    three_days_ago = time.time() - 3 * 86_400
    os.utime(stored, (three_days_ago, three_days_ago))
    assert storage.cleanup(protected=[str(stored)]) == 0   # still needed by an unpublished post
    assert storage.cleanup() == 1 and not stored.exists()  # expired and unreferenced
    assert storage.delete("sports_he/20261116/clip.mp4") is False


def test_cloud_storage_stub_is_wired_but_unimplemented(make_settings):
    from core import bootstrap

    engine = bootstrap(settings=make_settings(storage_mode="cloud"), console_logging=False)
    storage = engine.registry.storage()
    assert type(storage).__name__ == "CloudStorageStub"
    with pytest.raises(NotImplementedError, match="STORAGE_MODE=local"):
        storage.save("clip.mp4", "key.mp4")


# -- dependencies --------------------------------------------------------------------------------------------- #
def test_every_third_party_import_is_declared_in_requirements():
    """A teammate's fresh `pip install -r requirements.txt` must cover everything the code imports."""
    distribution_for = {
        "pydantic": "pydantic", "pydantic_settings": "pydantic-settings", "ruamel": "ruamel.yaml",
        "dotenv": "python-dotenv", "httpx": "httpx", "anthropic": "anthropic", "google": "google-genai",
        "streamlit": "streamlit", "pytest": "pytest", "fastapi": "fastapi", "rapidfuzz": "rapidfuzz",
    }
    local = {"core", "plugins", "storage", "dashboard", "api", "main", "tests", "conftest"}
    imported: set[str] = set()
    for path in ROOT.rglob("*.py"):
        if {".venv", "node_modules", ".next"} & set(path.parts):  # node_modules ships Python build scripts
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                imported |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
    third_party = imported - set(sys.stdlib_module_names) - local

    undeclared = third_party - set(distribution_for)
    assert not undeclared, f"imports with no known requirement: {sorted(undeclared)}"

    declared = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    declared += (ROOT / "requirements-dev.txt").read_text(encoding="utf-8").lower()
    missing = [distribution_for[name] for name in sorted(third_party) if distribution_for[name].lower() not in declared]
    assert not missing, f"add to requirements.txt: {missing}"


def test_env_example_documents_every_setting():
    """Every variable the engine reads appears in .env.example, so nobody has to read the code."""
    from core.config import Settings

    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    names = {name.upper() for name in Settings.model_fields} - {"DRY_RUN_PUBLISHER"}
    names |= {"TG_SPORTS_BOT_TOKEN", "TG_SPORTS_CHAT_ID", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
              "ANTHROPIC_MODEL", "GEMINI_MODEL", "ANTHROPIC_EFFORT", "LLM_MAX_OUTPUT_TOKENS",
              "LLM_PRICE_OVERRIDES", "AFFILIATE_DISCLOSURE",
              "STOREFRONT_ORIGINS", "STOREFRONT_MIN_RATING", "STOREFRONT_MIN_ORDERS",
              "RAPIDAPI_KEY", "RAPIDAPI_KEY_TEMU_1", "RAPIDAPI_KEY_TEMU_2", "RAPIDAPI_KEY_TEMU_3",
              "RAPIDAPI_KEY_TEMU_4", "TEMU_PROVIDERS_CONFIG_PATH",
              "RAPIDAPI_TEMU_HOST", "TEMU_TRACKING_ID", "TEMU_AFFILIATE_URL_TEMPLATE",
              "TEMU_MAX_ITEMS", "TEMU_CURRENCY", "TEMU_JOB_TIMEOUT", "INSTAGRAM_ACCESS_TOKEN", "INSTAGRAM_BUSINESS_ACCOUNT_ID",
              "INSTAGRAM_GRAPH_API_VERSION", "VIDEO_RENDER_ENABLED",
              "INSTAGRAM_ACCESS_TOKEN_GADGETS_EN", "INSTAGRAM_BUSINESS_ACCOUNT_ID_GADGETS_EN"}
    assert not [name for name in sorted(names) if name not in example]


def test_plugin_context_is_the_only_thing_a_plugin_needs(engine):
    """Plugins are constructed from PluginContext alone — the seam that keeps core closed."""
    context = PluginContext(settings=engine.settings)
    for kind in ("source", "generator"):
        for name in engine.plugins.names(kind):
            assert engine.plugins.get(kind, name)(context) is not None
