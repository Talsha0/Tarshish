"""Stage 1 — language-agnostic harvesting, and the in-place upgrade of pre-v3 databases."""

import json
import sqlite3

import pytest

from core.database import Database
from core.resilience import PipelineError


def stored(engine, category=None):
    where, params = ("WHERE category = ?", (category,)) if category else ("", ())
    return engine.db.query(f"SELECT source, external_id, category FROM products {where} ORDER BY id", params)


def test_a_category_is_harvested_once_for_every_language(engine):
    [report] = engine.harvester.harvest("gadgets", "temu_mock")

    assert report.ok and report.keyword == "smart tech gadgets" and len(report.inserted) == 3
    rows = stored(engine, "gadgets")
    assert len(rows) == 3 and {row["source"] for row in rows} == {"temu_mock"}
    # Nothing in `products` is per language: the row has no language at all.
    columns = {row["name"] for row in engine.db.table_columns("products")}
    assert "language" not in columns

    again = engine.harvester.harvest("gadgets", "temu_mock")[0]
    assert again.inserted == [] and len(again.duplicates) == 3 and len(stored(engine, "gadgets")) == 3


def test_harvest_refreshes_stored_rows_and_keeps_their_ids(engine):
    engine.harvester.harvest("gadgets", "temu_mock")
    before = engine.db.query_one("SELECT id, price FROM products WHERE external_id = '601099480276615'")
    engine.db._execute("UPDATE products SET price = 1 WHERE id = ?", (before["id"],))

    engine.harvester.harvest("gadgets", "temu_mock")

    after = engine.db.query_one("SELECT id, price FROM products WHERE external_id = '601099480276615'")
    assert after == before  # the store's current price is back, same row


def test_the_category_quality_bar_is_reapplied_to_untrusted_sources(engine):
    sports = engine.registry.get_category("sports")
    engine.registry.save_category(sports.model_copy(update={"min_rating": 4.75}))
    report = engine.harvester.harvest("sports", "aliexpress_mock")[0]
    assert len(report.inserted) == 1 and all(p.rating >= 4.75 for p in report.inserted)


def test_planning_uses_the_stores_a_category_has_rules_for(engine):
    plan = {(c.category_id, source) for c, source in engine.harvester.plan()}
    # gadgets has Temu + AliExpress rules, sports only AliExpress; home is disabled.
    assert plan == {("gadgets", "temu_rapidapi"), ("gadgets", "aliexpress_rapidapi"), ("sports", "aliexpress_rapidapi")}
    assert {(c.category_id, s) for c, s in engine.harvester.plan(source="temu_mock")} == {
        ("gadgets", "temu_mock"), ("sports", "temu_mock")}

    report = engine.harvester.harvest("sports", "temu_mock")[0]  # sports has no Temu rules
    assert not report.ok and "no search rules for the temu store" in report.error and not stored(engine)
    with pytest.raises(PipelineError, match="Unknown category 'nope'"):
        engine.harvester.harvest("nope")


def test_harvest_dry_run_writes_nothing(engine):
    report = engine.harvester.harvest("gadgets", "temu_mock", dry_run=True)[0]
    assert report.dry_run and len(report.inserted) == 3 and stored(engine) == []


def test_live_sources_get_the_category_rules_as_search_config(engine, monkeypatch):
    seen = {}
    cls = engine.plugins.get("source", "temu_rapidapi")

    def fake_batch(self, category, min_rating, min_orders, *, max_items=None, **_):
        seen.update(category=category, search=dict(self.context.options["search_config"]), max_items=max_items,
                    min_orders=min_orders)
        from plugins.sources.temu_rapidapi import TemuBatch

        return TemuBatch(keyword=self.search().keyword or "", provider="fake")

    monkeypatch.setattr(cls, "fetch_batch", fake_batch)
    report = engine.harvester.harvest("gadgets", "temu_rapidapi", limit=7)[0]
    assert report.provider == "fake" and seen == {
        "category": "gadgets", "max_items": 7, "min_orders": 500,
        "search": {"keyword": "smart tech gadgets", "l2Category": "Electronics & Appliances", "priceMin": 1, "priceMax": 100},
    }


def test_cli_harvest_then_publish_in_two_languages(make_settings, monkeypatch, capsys):
    import main

    settings = make_settings()
    for name, value in {
        "DATABASE_PATH": settings.database_path, "LOG_FILE": settings.log_file, "LOCAL_MEDIA_DIR": settings.local_media_dir,
    }.items():
        monkeypatch.setenv(name, str(value))

    assert main.main(["--harvest", "--source", "temu_mock", "--category", "gadgets"]) == 0
    assert "inserted 3" in capsys.readouterr().out

    for language, account in (("he", "instagram_pub → gadgets"), ("en", "instagram_pub → gadgets_en")):
        assert main.main(["--publish", "--category", "gadgets", "--lang", language,
                          "--content-types", "instagram_post,instagram_story", "--dry-run"]) == 0
        out = capsys.readouterr().out
        assert f"Publishing pipeline gadgets_{language}" in out
        assert f"instagram_post   llm_text_gen [claude-sonnet-5-5] ➔ {account} · 3 product(s)" in out
        assert f"instagram_story  llm_story_gen [claude-haiku-5-5] ➔ {account} · 3 product(s)" in out
        assert "previewed 6" in out

    with pytest.raises(SystemExit):
        main.main(["--harvest", "--publish"])
    with pytest.raises(SystemExit):
        main.main(["--publish", "--source", "temu_mock"])
    assert main.main([]) == 2  # no stage chosen
    assert "Choose a stage" in capsys.readouterr().err


# -- schema v2 -> v3 ---------------------------------------------------------------- #
V2_SCHEMA = """
CREATE TABLE pipelines (
    id TEXT PRIMARY KEY, type TEXT NOT NULL, language TEXT NOT NULL, source_plugin TEXT NOT NULL,
    generator_plugin TEXT NOT NULL, llm_model TEXT NOT NULL DEFAULT 'mock', publishing_mode TEXT NOT NULL,
    social_pages_json TEXT NOT NULL DEFAULT '[]', filters_json TEXT NOT NULL DEFAULT '{}',
    options_json TEXT NOT NULL DEFAULT '{}', enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
);
CREATE TABLE products (
    id INTEGER PRIMARY KEY AUTOINCREMENT, external_id TEXT NOT NULL, source TEXT NOT NULL,
    canonical_hash TEXT NOT NULL, {legacy} TEXT NOT NULL, title TEXT NOT NULL, price REAL NOT NULL,
    currency TEXT NOT NULL DEFAULT 'ILS', affiliate_url TEXT NOT NULL, created_at TEXT NOT NULL,
    category TEXT, rating REAL, orders_count INTEGER, image_urls_json TEXT NOT NULL DEFAULT '[]',
    UNIQUE (source, external_id)
);
CREATE TABLE publications (
    id INTEGER PRIMARY KEY AUTOINCREMENT, pipeline_id TEXT NOT NULL REFERENCES pipelines (id),
    product_id INTEGER NOT NULL REFERENCES products (id), platform TEXT NOT NULL,
    target_page TEXT NOT NULL DEFAULT 'default', content_type TEXT NOT NULL, status TEXT NOT NULL,
    caption TEXT NOT NULL, media_uri TEXT, model_used TEXT NOT NULL, generation_cost_usd REAL NOT NULL DEFAULT 0,
    platform_post_id TEXT, scheduled_for TEXT, published_at TEXT, error_message TEXT, content_json TEXT NOT NULL,
    target_json TEXT NOT NULL DEFAULT '{}', attempts INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
);
CREATE TABLE metrics_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    publication_id INTEGER NOT NULL REFERENCES publications (id) ON DELETE CASCADE,
    views INTEGER NOT NULL DEFAULT 0, clicks INTEGER NOT NULL DEFAULT 0, conversions INTEGER NOT NULL DEFAULT 0,
    revenue_usd REAL NOT NULL DEFAULT 0, recorded_at TEXT NOT NULL
);
INSERT INTO pipelines VALUES ('sports_he', 'running_and_sports', 'he', 'aliexpress_mock', 'llm_text_gen', 'mock',
    'immediate', '[]', '{}', '{}', 1, '2026-11-01T00:00:00+00:00');
INSERT INTO products (external_id, source, canonical_hash, {legacy}, title, price, affiliate_url, created_at,
    rating, orders_count) VALUES ('p1', 'aliexpress_mock', 'h1', 'running_and_sports', 'Old Shoe', 10,
    'https://a.example/aff/p1?x=1', '2026-11-01T00:00:00+00:00', 4.8, 900);
INSERT INTO publications (pipeline_id, product_id, platform, target_page, content_type, status, caption, model_used,
    content_json, created_at) VALUES ('sports_he', 1, 'console_pub', 'default', 'text', 'PUBLISHED', 'hi', 'mock',
    '{}', '2026-11-01T00:00:00+00:00');
INSERT INTO metrics_snapshots (publication_id, views, recorded_at) VALUES (1, 42, '2026-11-02T00:00:00+00:00');
""".replace("{legacy}", "ni" + "che")


def test_a_v2_database_is_upgraded_in_place_without_losing_history(tmp_path):
    path = tmp_path / "v2.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(V2_SCHEMA)

    db = Database(path)
    db.initialize()
    db.initialize()  # idempotent

    with db.connect() as conn:
        assert not [fk for fk in conn.execute("PRAGMA foreign_key_list(publications)") if fk["table"] == "pipelines"]
        assert {fk["table"] for fk in conn.execute("PRAGMA foreign_key_list(metrics_snapshots)")} == {"publications"}
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    columns = {row["name"] for row in db.query("PRAGMA table_info(products)")}
    assert "ni" + "che" not in columns and {"category", "raw_url", "video_url"} <= columns
    assert db.query_one("SELECT category FROM products WHERE id = 1") == {"category": "running-and-sports"}
    publication = db.query_one("SELECT id, pipeline_id, category, language, status FROM publications")
    assert publication == {"id": 1, "pipeline_id": "sports_he", "category": "running-and-sports", "language": "he",
                           "status": "PUBLISHED"}
    assert db.query_one("SELECT views FROM metrics_snapshots WHERE publication_id = 1") == {"views": 42}
    assert db.query_one("SELECT COUNT(*) AS n FROM pipelines")["n"] == 1  # legacy config kept, unused
    # Rows harvested before raw_url existed still load as products.
    [product] = db.unpublished_products(category="running-and-sports", language="en", account_platform="console",
                                        content_format="console_post")
    assert product.raw_url == "https://a.example/aff/p1" and json.loads("[]") == product.image_urls
