"""The Streamlit dashboard, driven through Streamlit's own test harness (no browser, no server)."""

import json
from pathlib import Path

import pytest

AppTest = pytest.importorskip("streamlit.testing.v1").AppTest

from core.database import Database  # noqa: E402

APP = Path(__file__).resolve().parents[1] / "dashboard" / "app.py"
SAVE_CATEGORY = "FormSubmitter:category_form_new-Save category"
RUN = "FormSubmitter:run_form-▶ Run pipeline"
TOKEN = "7123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw1"


@pytest.fixture
def dashboard(tmp_path, monkeypatch):
    """A dashboard bound to its own empty database (seeded from config/*.yaml on first run)."""
    import streamlit as st

    st.cache_resource.clear()  # the app caches its engine per process; each test needs a fresh one
    database_path = tmp_path / "dashboard.db"
    monkeypatch.setenv("DATABASE_PATH", str(database_path))
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "logs" / "dashboard.log"))
    monkeypatch.setenv("LOCAL_MEDIA_DIR", str(tmp_path / "media"))
    monkeypatch.setenv("WORKER_ACTIVE_HOURS_START", "0")
    monkeypatch.setenv("WORKER_ACTIVE_HOURS_END", "24")
    app = AppTest.from_file(str(APP), default_timeout=120).run()
    yield app, Database(database_path)
    st.cache_resource.clear()


def queue_console_posts(db, capsys):
    """Through the CLI, on the dashboard's database: harvest sports, queue 3 posts to a console account."""
    import main

    db.upsert_config("content_profiles", {"content_type": "console_post", "platform": "console",
                                          "generator": "llm_text_gen", "llm_model": "mock", "enabled": 1})
    db.upsert_config("social_accounts", {"category_id": "sports", "language": "he", "platform": "console",
                                         "account_handle": "default", "enabled": 1})
    assert main.main(["--harvest", "--source", "aliexpress_mock", "--category", "sports"]) == 0
    assert main.main(["--publish", "--category", "sports", "--lang", "he", "--content-types", "console_post",
                      "--mode", "queued"]) == 0
    capsys.readouterr()


def test_dashboard_renders_every_tab_harvests_and_runs_a_dry_run(dashboard):
    app, db = dashboard

    assert not app.exception
    top_level = ["Categories & Search", "Social Accounts", "Content Types & AI Models", "Run Pipelines & KPIs",
                 "Content Review"]
    labels = [tab.label for tab in app.tabs]
    assert labels[:4] == top_level[:4] and [label for label in labels if label in top_level] == top_level
    assert any(metric.label == "Net profit (USD)" for metric in app.metric)
    assert any("Monthly AI spend" in header.value for header in app.subheader)
    assert {metric.label: metric.value for metric in app.metric}["Storefront clicks"] == "0"
    rules = next(block.value for block in app.markdown if "smart tech gadgets" in block.value)
    assert "Electronics &amp; Appliances" in rules and "price 1–100" in rules

    app.selectbox(key="harvest_source_gadgets").set_value("temu_mock").run()
    app.button(key="harvest_gadgets").click().run()
    assert not app.exception and any("inserted 3" in message.value for message in app.success)
    assert db.query_one("SELECT COUNT(*) AS n FROM products WHERE category = 'gadgets'")["n"] == 3

    app.button(key=RUN).click().run()  # defaults: gadgets · he · instagram_post + instagram_story · dry-run on

    assert not app.exception
    assert any("Dry-run result" in expander.label and "gadgets_he" in expander.label for expander in app.expander)
    routes = next(block.value for block in app.markdown if "llm_story_gen" in block.value and ">Account<" in block.value)
    assert "claude-sonnet-5-5" in routes and "claude-haiku-5-5" in routes and "instagram_pub → gadgets" in routes
    assert any("הקישור בביו" in block.value for block in app.markdown)  # the Hebrew story hook
    assert sum(db.get_status_counts().values()) == 0  # a dry-run writes nothing


def test_category_form_validates_then_creates_a_category_that_can_be_harvested(dashboard):
    app, db = dashboard

    app.text_input(key="cf_id_new").set_value("!!")
    app.text_input(key="cf_en_new").set_value("Yoga")
    app.button(key=SAVE_CATEGORY).click().run()
    assert any("not valid" in error.value for error in app.error)
    assert db.get_config("categories", "yoga") is None

    app.text_input(key="cf_id_new").set_value("Yoga Gear")
    app.text_input(key="cf_he_new").set_value("יוגה")
    app.text_input(key="cf_temu_kw_new").set_value("yoga mat")
    app.number_input(key="cf_temu_max_new").set_value(60.0)
    app.checkbox(key="cf_ali_on_new").uncheck()
    app.button(key=SAVE_CATEGORY).click().run()

    assert not app.exception and not app.error
    row = db.get_config("categories", "yoga-gear")
    assert (row["display_name_en"], row["display_name_he"], row["aliexpress_search_json"]) == ("Yoga", "יוגה", None)
    assert json.loads(row["temu_search_json"]) == {"keyword": "yoga mat", "priceMax": 60.0}
    assert any(button.key == "harvest_yoga-gear" for button in app.button)  # live at once, no restart


def test_editing_a_category_keeps_its_other_search_keys_and_the_toggle_saves(dashboard):
    app, db = dashboard
    app.button(key="cat_edit_gadgets").click().run()
    assert app.selectbox(key="form_category").value == "gadgets"
    app.text_input(key="cf_temu_kw_gadgets").set_value("smart home gadgets")
    app.button(key="FormSubmitter:category_form_gadgets-Save category").click().run()

    assert not app.exception and not app.error
    assert json.loads(db.get_config("categories", "gadgets")["temu_search_json"]) == {
        "keyword": "smart home gadgets", "l2Category": "Electronics & Appliances", "priceMin": 1.0, "priceMax": 100.0,
    }

    app.toggle(key="cat_enabled_sports").set_value(False).run()
    assert not app.exception and db.get_config("categories", "sports")["enabled"] == 0


def test_accounts_matrix_shows_languages_side_by_side_and_refuses_raw_tokens(dashboard):
    app, db = dashboard
    matrix = next(block.value for block in app.markdown if "Hebrew (he)" in block.value and "gadgets_en" in block.value)
    assert matrix.index("Hebrew (he)") < matrix.index("English (en)")
    assert "INSTAGRAM_ACCESS_TOKEN_GADGETS_EN" in matrix and "no tiktok_pub plugin" not in matrix
    assert "⚠️ @gadgets.il · environment variable &#x27;TIKTOK_ACCESS_TOKEN_GADGETS_HE" in matrix  # plugin installed
    assert "⚠️ gadgets · environment variable" in matrix and "YOUTUBE_REFRESH_TOKEN" in matrix

    app.selectbox(key="acc_category").set_value("sports").run()
    app.selectbox(key="acc_language").set_value("en").run()
    app.selectbox(key="acc_platform").set_value("telegram").run()
    form = "FormSubmitter:account_form_sports_en_telegram-Save account"
    app.text_input(key="af_handle_sports_en_telegram").set_value("@sports_en")
    app.text_input(key="af_token_sports_en_telegram").set_value(TOKEN)  # a real-looking token
    app.button(key=form).click().run()
    assert any("NAME of an environment variable" in error.value for error in app.error)
    assert db.get_config("social_accounts", "sports", "en", "telegram") is None  # nothing was stored

    app.text_input(key="af_token_sports_en_telegram").set_value("TG_SPORTS_EN_TOKEN")
    app.text_input(key="af_id_sports_en_telegram").set_value("TG_SPORTS_EN_CHAT")
    app.button(key=form).click().run()
    assert not app.exception and not app.error
    row = db.get_config("social_accounts", "sports", "en", "telegram")
    assert (row["account_handle"], row["token_env_var"], row["account_id_env_var"]) == (
        "@sports_en", "TG_SPORTS_EN_TOKEN", "TG_SPORTS_EN_CHAT")
    assert TOKEN not in json.dumps(db.list_config("social_accounts"))

    app.button(key="unlink_sports_en_telegram").click().run()
    assert not app.exception and db.get_config("social_accounts", "sports", "en", "telegram") is None


def test_content_types_get_their_model_from_one_place(dashboard):
    app, db = dashboard
    app.selectbox(key="pf_model_instagram_story").set_value("gemini-3.5-flash-lite")
    app.text_input(key="pf_tone_instagram_story").set_value("bold and short")
    app.button(key="FormSubmitter:profile_form_instagram_story-Save content type").click().run()

    assert not app.exception and not app.error
    row = db.get_config("content_profiles", "instagram_story")
    assert (row["llm_model"], row["tone"], row["generator"], row["publish_format"]) == (
        "gemini-3.5-flash-lite", "bold and short", "llm_story_gen", "story")

    app.text_input(key="pf_type_new").set_value("pinterest_pin")
    app.text_input(key="pf_platform_new").set_value("Not a platform!")
    app.button(key="FormSubmitter:profile_form_new-Add content type").click().run()
    assert any("not valid" in error.value for error in app.error) and not db.get_config("content_profiles", "pinterest_pin")


def test_queue_tab_retries_failed_items_and_flushes_the_queue(dashboard, capsys):
    app, db = dashboard
    queue_console_posts(db, capsys)  # same database, via the CLI
    queued = db.list_publications(statuses=["QUEUED"])
    assert len(queued) == 3
    db.mark_failed(queued[0]["id"], "Telegram sendMessage failed (403): bot is not a member of the channel")

    app.run()
    assert any(tab.label == "Failed (1)" for tab in app.tabs) and any(tab.label == "Queued (2)" for tab in app.tabs)

    next(button for button in app.button if button.label == "Retry all failed").click().run()
    assert not app.exception
    assert db.get_status_counts()["FAILED"] == 0 and db.get_status_counts()["QUEUED"] == 3

    next(button for button in app.button if button.label == "Yes, flush the queue").click().run()
    assert not app.exception
    assert sum(db.get_status_counts().values()) == 0


def test_worker_pass_button_and_manual_metrics_entry(dashboard, monkeypatch, capsys):
    import streamlit as st

    app, db = dashboard
    monkeypatch.setenv("WORKER_MIN_DELAY_SECONDS", "0")  # no pacing: everything queued is due at once
    monkeypatch.setenv("WORKER_MAX_DELAY_SECONDS", "0")
    st.cache_resource.clear()
    queue_console_posts(db, capsys)
    app.run()

    next(button for button in app.button if button.label == "Run a worker pass now").click().run()
    capsys.readouterr()
    assert not app.exception
    assert db.get_status_counts()["PUBLISHED"] == 3 and db.get_status_counts()["QUEUED"] == 0

    for label, value in (("Views", 1200), ("Clicks", 90), ("Conversions", 4), ("Revenue (USD)", 12.5)):
        next(field for field in app.number_input if field.label == label).set_value(value)
    app.button(key="FormSubmitter:metrics_snapshot-Save snapshot").click().run()

    assert not app.exception
    tiles = {metric.label: metric.value for metric in app.metric}
    assert tiles["Total views"] == "1,200" and tiles["Clicks"] == "90" and tiles["Revenue (USD)"] == "$12.50"
    by_type = next(block.value for block in app.markdown if "console_post" in block.value and ">Net profit<" in block.value)
    assert '<td class="num">3</td>' in by_type  # the KPI breakdown per content type: 3 published


def test_dashboard_does_not_need_pyarrow(dashboard, monkeypatch):
    """Windows Smart App Control can refuse to load pyarrow; the dashboard must render without it."""
    import builtins
    import sys

    app, _ = dashboard
    for name in [module for module in sys.modules if module == "pyarrow" or module.startswith("pyarrow.")]:
        monkeypatch.delitem(sys.modules, name)
    real_import = builtins.__import__

    def refuse_pyarrow(name, *args, **kwargs):
        if name == "pyarrow" or name.startswith("pyarrow."):
            raise ImportError("DLL load failed while importing lib: An Application Control policy has blocked this file.")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse_pyarrow)
    app.run()
    app.button(key=RUN).click().run()

    assert not app.exception
    assert any(metric.label == "Net profit (USD)" for metric in app.metric)


def test_kpi_tab_shows_live_storefront_clicks_per_category_and_source(dashboard):
    from storefront_api.seed import seed_storefront

    app, db = dashboard
    seed_storefront(db)
    db.record_click_event(1, category="sports", source="aliexpress_mock", language="he", referrer_path="/he")
    db.record_click_event(8, category="sports", source="temu_mock", language="en", referrer_path="/en/c/sports")
    db.record_click_event(4, category="gadgets", source="aliexpress_mock", language="he")

    app.button(key="refresh_clicks").click().run()

    assert not app.exception
    tiles = {metric.label: metric.value for metric in app.metric}
    assert tiles["Storefront clicks"] == "3" and tiles["Last 24 hours"] == "3" and tiles["Products clicked"] == "3"
    bars = [block.value for block in app.markdown if 'class="tsh-bars"' in block.value]
    by_category = next(block for block in bars if "Clicks by category" in block)
    assert 'title="sports: 2"' in by_category and 'title="gadgets: 1"' in by_category
    by_source = next(block for block in bars if "Clicks by source" in block)
    assert 'title="aliexpress_mock: 2"' in by_source and 'title="temu_mock: 1"' in by_source


def test_temu_provider_chain_card_shows_order_keys_and_the_last_run(dashboard, tmp_path, monkeypatch):
    from plugins.sources.temu_providers import record_run

    def chain_table(app) -> str:
        assert any("Temu RapidAPI Provider Chain" in block.value for block in app.markdown)
        return next(block.value for block in app.markdown if "temu_primary_scraper" in block.value)

    app, _ = dashboard
    assert not app.exception
    table = chain_table(app)  # the built-in chain: one provider, no key in the environment
    assert "TemuProductScraperAdapter" in table and "RAPIDAPI_KEY_TEMU_1" in table and "Missing" in table
    assert any("No live fetch has run yet" in caption.value for caption in app.caption)

    key = "rapidkey-dashboard-0123456789abcdef"
    monkeypatch.delenv("TEMU_PROVIDERS_CONFIG_PATH")  # the shipped config/temu_providers.yaml
    monkeypatch.setenv("RAPIDAPI_KEY", key)
    record_run(tmp_path / "dashboard.db", {
        "finished_at": "2026-11-17T08:30:00+00:00", "category": "gadgets", "keyword": "tech gadgets",
        "succeeded": "temuscout", "usable": 7,
        "attempts": [
            {"provider": "temu_primary_scraper", "outcome": "quota_exhausted", "detail": "HTTP 429: quota used up"},
            {"provider": "temuscout", "outcome": "success", "detail": "24 item(s) received, 7 usable"},
        ],
    })
    app.run()

    assert not app.exception
    table = chain_table(app)
    order = [table.index(name) for name in
             ("temu_primary_scraper", "temu_secondary_scraper", "temu_realtime_backup", "temuscout")]
    assert order == sorted(order)  # the failover order, top to bottom
    assert "Configured (via RAPIDAPI_KEY)" in table and "Missing" not in table
    assert "quota used up (429)" in table and "✓ delivered" in table and "switched off" in table
    assert any("temuscout" in caption.value and "7 usable product(s)" in caption.value for caption in app.caption)
    assert not any(key in block.value for block in app.markdown)  # variable names only, never the key


def test_aliexpress_provider_chain_card_shows_order_keys_and_the_last_run(dashboard, tmp_path, monkeypatch):
    from plugins.sources.aliexpress_providers import record_run

    def chain_table(app) -> str:
        assert any("AliExpress RapidAPI Provider Chain" in block.value for block in app.markdown)
        return next(block.value for block in app.markdown if "ali_primary_datahub" in block.value)

    app, _ = dashboard
    assert not app.exception
    table = chain_table(app)  # shown next to the Temu card; no key in the environment yet
    order = [table.index(name) for name in ("ali_primary_datahub", "ali_secondary_trueapi", "ali_backup_unofficial")]
    assert order == sorted(order)  # the failover order, top to bottom
    assert "AliExpressDataHubAdapter" in table and "RAPIDAPI_KEY_ALI_1" in table and "Missing" in table

    key = "alikey-dashboard-0123456789abcdef"
    monkeypatch.setenv("RAPIDAPI_KEY_ALI_1", key)
    monkeypatch.setenv("RAPIDAPI_KEY", key)
    record_run(tmp_path / "dashboard.db", {
        "finished_at": "2026-11-17T08:30:00+00:00", "category": "gadgets", "keyword": "smart watch",
        "succeeded": "ali_backup_unofficial", "usable": 4,
        "attempts": [
            {"provider": "ali_primary_datahub", "outcome": "quota_exhausted", "detail": "HTTP 429: quota used up"},
            {"provider": "ali_secondary_trueapi", "outcome": "empty", "detail": "HTTP 200 with 0 items"},
            {"provider": "ali_backup_unofficial", "outcome": "success", "detail": "20 item(s) received, 4 usable"},
        ],
    })
    app.run()

    assert not app.exception
    table = chain_table(app)
    assert "Configured" in table and "Configured (via RAPIDAPI_KEY)" in table and "Missing" not in table
    assert "quota used up (429)" in table and "✗ no items" in table and "✓ delivered" in table
    assert any("ali_backup_unofficial" in caption.value and "4 usable product(s)" in caption.value
               for caption in app.caption)
    assert not any(key in block.value for block in app.markdown)  # variable names only, never the key


def test_a_password_locks_the_dashboard(tmp_path, monkeypatch):
    import streamlit as st

    st.cache_resource.clear()
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "locked.db"))
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "logs" / "dashboard.log"))
    monkeypatch.setenv("DASHBOARD_PASSWORD", "correct horse")
    app = AppTest.from_file(str(APP), default_timeout=120).run()
    assert not app.exception and not app.tabs  # nothing but the sign-in form
    app.text_input[0].input("wrong")  # a form field and its submit button go in the same run
    app.button[0].click().run()
    assert any("Wrong password" in error.value for error in app.error) and not app.tabs
    app.text_input[0].input("correct horse")
    app.button[0].click().run()
    assert not app.exception and [tab.label for tab in app.tabs][:1] == ["Categories & Search"]
    st.cache_resource.clear()


def test_a_production_database_needs_a_password(tmp_path, monkeypatch):
    import streamlit as st

    st.cache_resource.clear()
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pw@db.invalid:6543/postgres")
    app = AppTest.from_file(str(APP), default_timeout=120).run()
    assert any("DASHBOARD_PASSWORD" in error.value for error in app.error) and not app.tabs
    st.cache_resource.clear()
