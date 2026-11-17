"""The Streamlit dashboard, driven through Streamlit's own test harness (no browser, no server)."""

import json
from pathlib import Path

import pytest

AppTest = pytest.importorskip("streamlit.testing.v1").AppTest

from core.database import Database  # noqa: E402

APP = Path(__file__).resolve().parents[1] / "dashboard" / "app.py"
SAVE = "FormSubmitter:pipeline_form_new-Save pipeline"


@pytest.fixture
def dashboard(tmp_path, monkeypatch):
    """A dashboard bound to its own empty database (seeded from pipelines.yaml on first run)."""
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


def test_dashboard_renders_every_tab_and_runs_a_dry_run(dashboard):
    app, db = dashboard

    assert not app.exception
    assert [tab.label for tab in app.tabs][:3] == ["Pipelines", "KPIs & Budget", "Queue & Logs"]
    assert any(metric.label == "Net profit (USD)" for metric in app.metric)
    assert any("Monthly AI spend" in header.value for header in app.subheader)
    assert any("Storefront click-throughs" in header.value for header in app.subheader)
    assert {metric.label: metric.value for metric in app.metric}["Storefront clicks"] == "0"
    # Tab 1 flow card: [Input: source] ➔ [Generate: generator + model] ➔ [Output: publishers & pages]
    cards = [block.value for block in app.markdown if 'class="tsh-flow"' in block.value]
    card = next(value for value in cards if "aliexpress_mock" in value)
    for expected in ("aliexpress_mock", "llm_text_gen", "model anthropic", "console_pub", "telegram_pub", "TG_SPORTS_BOT_TOKEN"):
        assert expected in card

    app.button(key="run_sports_he").click().run()

    assert not app.exception
    assert any("Dry-run result" in expander.label for expander in app.expander)
    assert any("לרכישה" in block.value or "הזמנות" in block.value for block in app.markdown)
    assert sum(db.get_status_counts().values()) == 0  # a dry-run writes nothing


def test_pipeline_form_validates_then_creates_a_pipeline_that_is_live_immediately(dashboard):
    app, db = dashboard

    app.text_input(key="f_id_new").set_value("Not A Valid ID!")
    app.text_input(key="f_niche_new").set_value("smart_gadgets")
    app.button(key=SAVE).click().run()
    assert any("not valid" in error.value for error in app.error)
    assert len(db.list_pipelines()) == 2  # nothing was saved (the two seeded pipelines only)

    app.text_input(key="f_id_new").set_value("gadgets_en")
    app.text_input(key="f_niche_new").set_value("smart_gadgets")
    app.button(key=SAVE).click().run()

    assert not app.exception and not app.error
    row = db.get_pipeline("gadgets_en")
    assert row["type"] == "smart_gadgets" and row["source_plugin"] == "aliexpress_mock" and row["enabled"] == 1
    assert json.loads(row["social_pages_json"]) == [{"platform": "console_pub", "page_name": "default"}]
    # No restart: the new pipeline already has its card and its own dry-run button.
    assert any(button.key == "run_gadgets_en" for button in app.button)
    app.button(key="run_gadgets_en").click().run()
    assert not app.exception and any("Dry-run result" in expander.label for expander in app.expander)


def test_editing_a_pipeline_keeps_its_social_pages_and_env_var_names(dashboard):
    app, db = dashboard
    before = json.loads(db.get_pipeline("sports_he")["social_pages_json"])
    assert len(before) == 2

    app.button(key="edit_sports_he").click().run()
    assert app.selectbox(key="form_pipeline").value == "sports_he"  # the form switched to that pipeline
    app.radio(key="f_mode_sports_he").set_value("queued")
    app.button(key="FormSubmitter:pipeline_form_sports_he-Save pipeline").click().run()

    assert not app.exception and not app.error
    row = db.get_pipeline("sports_he")
    assert row["publishing_mode"] == "queued"
    assert json.loads(row["social_pages_json"]) == before  # incl. token_env_var / chat_id_env_var names


def test_enable_toggle_updates_the_database(dashboard):
    app, db = dashboard
    assert db.get_pipeline("sports_he")["enabled"] == 1

    app.toggle(key="enabled_sports_he").set_value(False).run()

    assert not app.exception
    assert db.get_pipeline("sports_he")["enabled"] == 0


def test_queue_tab_retries_failed_items_and_flushes_the_queue(dashboard, capsys):
    import main

    app, db = dashboard
    assert main.main(["--pipeline", "sports_he", "--mode", "queued"]) == 0  # same database, via the CLI
    queued = db.list_publications(statuses=["QUEUED"])
    assert len(queued) == 3
    db.mark_failed(queued[0]["id"], "Telegram sendMessage failed (403): bot is not a member of the channel")
    capsys.readouterr()

    app.run()
    assert any(tab.label == "Failed (1)" for tab in app.tabs) and any(tab.label == "Queued (2)" for tab in app.tabs)

    next(button for button in app.button if button.label == "Retry all failed").click().run()
    assert not app.exception
    assert db.get_status_counts()["FAILED"] == 0 and db.get_status_counts()["QUEUED"] == 3

    next(button for button in app.button if button.label == "Yes, flush the queue").click().run()
    assert not app.exception
    assert sum(db.get_status_counts().values()) == 0


def test_worker_pass_button_and_manual_metrics_entry(dashboard, monkeypatch, capsys):
    import main
    import streamlit as st

    app, db = dashboard
    monkeypatch.setenv("WORKER_MIN_DELAY_SECONDS", "0")  # no pacing: everything queued is due at once
    monkeypatch.setenv("WORKER_MAX_DELAY_SECONDS", "0")
    st.cache_resource.clear()
    assert main.main(["--pipeline", "sports_he", "--mode", "queued"]) == 0
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


def test_delete_is_allowed_only_for_pipelines_without_history(dashboard, capsys):
    import main

    app, db = dashboard
    app.text_input(key="f_id_new").set_value("scratch_en")
    app.text_input(key="f_niche_new").set_value("scratch")
    app.button(key=SAVE).click().run()
    assert db.get_pipeline("scratch_en") is not None

    app.button(key="delete_scratch_en").click().run()
    assert not app.exception and db.get_pipeline("scratch_en") is None

    assert main.main(["--pipeline", "sports_he", "--mode", "immediate", "--limit", "1"]) == 0
    capsys.readouterr()
    app.button(key="edit_sports_he").click().run()
    app.button(key="delete_sports_he").click().run()
    assert any("publication history" in error.value for error in app.error)
    assert db.get_pipeline("sports_he") is not None


def test_social_pages_are_added_through_plain_widgets_and_raw_tokens_are_refused(dashboard):
    app, db = dashboard
    app.button(key="edit_sports_he").click().run()
    app.number_input(key="f_rows_sports_he").set_value(3).run()  # one more row than the two existing pages

    app.selectbox(key="f_pp_sports_he_2").set_value("telegram_pub")
    app.text_input(key="f_pn_sports_he_2").set_value("@second_channel")
    app.text_input(key="f_pt_sports_he_2").set_value("7123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw1")  # a real-looking token
    app.button(key="FormSubmitter:pipeline_form_sports_he-Save pipeline").click().run()
    assert any("NAME of an environment variable" in error.value for error in app.error)
    assert len(json.loads(db.get_pipeline("sports_he")["social_pages_json"])) == 2  # nothing was stored

    app.text_input(key="f_pt_sports_he_2").set_value("TG_SECOND_BOT_TOKEN")
    app.text_input(key="f_pc_sports_he_2").set_value("TG_SECOND_CHAT_ID")
    app.button(key="FormSubmitter:pipeline_form_sports_he-Save pipeline").click().run()

    assert not app.exception and not app.error
    pages = json.loads(db.get_pipeline("sports_he")["social_pages_json"])
    assert pages[2] == {
        "platform": "telegram_pub", "page_name": "@second_channel",
        "token_env_var": "TG_SECOND_BOT_TOKEN", "chat_id_env_var": "TG_SECOND_CHAT_ID",
    }

    # Setting a row's platform to "— none —" removes that page.
    app.selectbox(key="f_pp_sports_he_2").set_value("— none —")
    app.button(key="FormSubmitter:pipeline_form_sports_he-Save pipeline").click().run()
    assert not app.exception and len(json.loads(db.get_pipeline("sports_he")["social_pages_json"])) == 2


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
    app.button(key="run_sports_he").click().run()

    assert not app.exception
    assert any(metric.label == "Net profit (USD)" for metric in app.metric)


def test_kpi_tab_shows_live_storefront_clicks_per_category_and_source(dashboard):
    from api.seed import seed_storefront

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
