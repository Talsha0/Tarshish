"""Stage 2 — publishing pipelines over harvested products, the queue worker and KPIs."""

import random
from datetime import datetime, timedelta, timezone

import pytest

from core.config import SocialAccount
from core.database import from_iso, to_iso
from core.models import utcnow
from core.resilience import BudgetExceededError
from core.worker import JitterScheduler, QueueWorker
from dashboard.metrics_service import MetricsService

FAKE_TOKEN = "7123456789:" + "A" * 35
CONSOLE = ["console_post"]
CONSOLE_AND_TELEGRAM = ["console_post", "telegram_post"]


@pytest.fixture
def ready(harvested, console_route):
    """Products harvested and a credential-free console account for every category × language."""
    return harvested


def counts(engine):
    return engine.db.get_status_counts()


def run(engine, content_types=CONSOLE, category="sports", language="he", **kwargs):
    kwargs.setdefault("mode", "immediate")
    return engine.orchestrator.run(category, language, content_types, **kwargs)


def make_paid(engine, monkeypatch, cost: float):
    """Make the text generator behave like a paid LLM: budget check first, then a cost."""
    cls = engine.plugins.get("generator", "llm_text_gen")
    original = cls.generate

    def paid(self, product, language, prompt_template):
        self.context.budget_guard.ensure_within_budget()
        content = original(self, product, language, prompt_template)
        return content.model_copy(
            update={"generation_cost_usd": cost, "tokens_input": 100, "tokens_output": 50, "model_used": "claude-opus-5-5"}
        )

    monkeypatch.setattr(cls, "generate", paid)


def make_due(engine):
    engine.db._execute("UPDATE publications SET scheduled_for = ?", (to_iso(utcnow() - timedelta(minutes=5)),))


# -- orchestration -------------------------------------------------------------- #
def test_publishing_reads_only_harvested_products(engine, console_route):
    report = run(engine)  # nothing harvested yet: no store is ever asked from Stage 2
    assert report.ok and report.items == [] and report.routes[0].candidates == 0
    assert any("harvest first: python main.py --harvest --category sports" in w for w in report.warnings)


def test_dry_run_prints_previews_and_has_no_side_effects(ready, capsys):
    products_before = ready.db.query_one("SELECT COUNT(*) AS n FROM products")["n"]
    report = run(ready, CONSOLE_AND_TELEGRAM, dry_run=True)

    assert report.ok and report.count("preview") == 6  # 3 products × 2 content types
    assert sum(counts(ready).values()) == 0
    assert ready.db.query_one("SELECT COUNT(*) AS n FROM products")["n"] == products_before
    assert ready.db.get_monthly_ai_spend() == 0
    printed = capsys.readouterr().out
    assert printed.count("DRY-RUN PREVIEW") == 6 and "לרכישה" in printed
    # The Telegram account has no credentials: warned about, previewed, not fatal.
    assert any("TG_SPORTS_BOT_TOKEN" in warning for warning in report.warnings)
    telegram = next(route for route in report.routes if route.content_type == "telegram_post")
    assert not telegram.ready and telegram.account == "@my_sports_channel" and telegram.llm_model == "claude-haiku-5-5"


def test_immediate_mode_publishes_and_skips_only_the_unconfigured_account(ready):
    report = run(ready, CONSOLE_AND_TELEGRAM)

    assert report.ok and report.count("published") == 3
    rows = ready.db.list_publications()
    assert {row["platform"] for row in rows} == {"console_pub"}
    assert all(row["status"] == "PUBLISHED" and row["platform_post_id"] for row in rows)
    assert {(row["pipeline_id"], row["category"], row["language"], row["content_format"], row["account_platform"])
            for row in rows} == {("sports_he", "sports", "he", "console_post", "console")}


def test_reruns_are_deduplicated_and_force_overrides(ready):
    run(ready)
    second = run(ready)
    assert second.items == [] and counts(ready)["PUBLISHED"] == 3
    assert any("no harvested 'sports' product is waiting" in w for w in second.warnings)

    forced = run(ready, force=True, limit=1)
    assert forced.count("published") == 1 and counts(ready)["PUBLISHED"] == 4


def test_limit_walks_past_already_published_products(ready):
    titles = set()
    for _ in range(3):
        report = run(ready, limit=1)
        titles |= {item.title for item in report.items if item.status == "published"}
    assert len(titles) == 3  # one new product per run, never the same one twice


def test_languages_publish_the_same_products_to_their_own_accounts(ready):
    he, en = run(ready, language="he"), run(ready, language="en")

    assert he.count("published") == en.count("published") == 3
    rows = ready.db.list_publications()
    assert {row["target_page"] for row in rows if row["language"] == "he"} == {"sports_he_console"}
    assert {row["target_page"] for row in rows if row["language"] == "en"} == {"sports_en_console"}
    # One harvested row per product serves both languages: no language duplication in `products`.
    assert len({row["product_title"] for row in rows}) == 3
    assert ready.db.query_one("SELECT COUNT(*) AS n FROM products WHERE category = 'sports'")["n"] == 3


def test_an_account_added_later_is_backfilled_and_no_secret_is_persisted(ready, monkeypatch):
    run(ready, CONSOLE_AND_TELEGRAM)  # console only: Telegram has no credentials yet
    monkeypatch.setenv("TG_SPORTS_BOT_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("TG_SPORTS_CHAT_ID", "@sports")

    report = run(ready, CONSOLE_AND_TELEGRAM, mode="queued")

    queued = ready.db.list_publications(statuses=["QUEUED"])
    assert report.count("queued") == 3 and {row["platform"] for row in queued} == {"telegram_pub"}
    assert counts(ready)["PUBLISHED"] == 3  # console posts were not repeated
    stored = str(ready.db.query("SELECT * FROM publications")) + str(ready.db.query("SELECT * FROM social_accounts"))
    assert FAKE_TOKEN not in stored and "TG_SPORTS_BOT_TOKEN" in stored


def test_default_content_types_follow_the_enabled_accounts(ready):
    report = ready.orchestrator.run("sports", "he", dry_run=True)
    assert {route.content_type for route in report.routes} == {"console_post", "telegram_post"}

    ready.registry.save_social_account(SocialAccount(
        category_id="sports", language="he", platform="console", account_handle="sports_he_console", enabled=False,
    ))
    report = ready.orchestrator.run("sports", "he", dry_run=True)
    assert {route.content_type for route in report.routes} == {"telegram_post"}
    assert "No enabled social account" in ready.orchestrator.run("gadgets", "ar").error


def test_one_failing_product_does_not_stop_the_batch(ready, monkeypatch):
    cls = ready.plugins.get("generator", "llm_text_gen")
    original = cls.generate

    def flaky(self, product, language, prompt_template):
        if "Hydration" in product.title:
            raise RuntimeError("generator crashed")
        return original(self, product, language, prompt_template)

    monkeypatch.setattr(cls, "generate", flaky)
    report = run(ready)

    assert report.count("failed") == 1 and report.count("published") == 2
    assert not report.ok and report.error is None
    assert "generator crashed" in next(item.detail for item in report.items if item.status == "failed")


def test_a_disabled_category_only_runs_as_dry_run(ready):
    category = ready.registry.get_category("sports")
    ready.registry.save_category(category.model_copy(update={"enabled": False}))
    assert "disabled" in run(ready).error
    assert run(ready, dry_run=True).count("preview") == 3


# -- budget guardrail ------------------------------------------------------------- #
def test_budget_guardrail_halts_the_run_once_the_cap_is_reached(ready, monkeypatch):
    make_paid(ready, monkeypatch, cost=3.0)  # cap is $5: two products fit, the third must not start

    with pytest.raises(BudgetExceededError) as excinfo:
        run(ready)

    report = excinfo.value.report
    assert report.halted_by_budget and report.count("published") == 2
    assert ready.db.get_monthly_ai_spend() == pytest.approx(6.0)
    assert {row["pipeline_id"] for row in ready.db.query("SELECT pipeline_id FROM ai_usage")} == {"sports_he"}
    with pytest.raises(BudgetExceededError):  # and it stays halted for every later run
        run(ready, force=True)


def test_flush_clears_the_queue_but_never_the_spend_ledger(ready, monkeypatch):
    make_paid(ready, monkeypatch, cost=0.5)
    run(ready, mode="queued")
    assert counts(ready)["QUEUED"] == 3 and ready.db.get_monthly_ai_spend() == pytest.approx(1.5)

    assert ready.db.flush_queue() == 3

    assert sum(counts(ready).values()) == 0
    assert ready.db.get_monthly_ai_spend() == pytest.approx(1.5)


# -- queue, jitter, worker ---------------------------------------------------------- #
def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, 16, hour, minute, tzinfo=timezone.utc)


def test_scheduler_respects_active_hours_and_jitter_bounds():
    day = JitterScheduler(tz=timezone.utc, start_hour=9, end_hour=22, min_delay=600, max_delay=2700, rng=random.Random(7))
    assert day.is_active(at(9)) and day.is_active(at(21, 59))
    assert not day.is_active(at(8, 59)) and not day.is_active(at(22))
    delays = [day.human_delay() for _ in range(300)]
    assert all(600 <= delay <= 2700 for delay in delays) and len({round(delay) for delay in delays}) > 100

    late = day.next_slot(at(21, 50))  # would land after closing time -> next morning, not on the dot
    assert late.date() == at(0).date() + timedelta(days=1) and at(9).time() <= late.time() <= at(9, 30).time()

    night = JitterScheduler(tz=timezone.utc, start_hour=20, end_hour=2, min_delay=1, max_delay=2)
    assert night.is_active(at(23)) and night.is_active(at(1)) and not night.is_active(at(12))


def test_queued_items_get_jittered_slots_and_the_worker_publishes_them_when_due(ready):
    run(ready, mode="queued")
    rows = sorted(ready.db.list_publications(statuses=["QUEUED"]), key=lambda row: row["scheduled_for"])
    slots = [from_iso(row["scheduled_for"]) for row in rows]
    gaps = [(later - earlier).total_seconds() for earlier, later in zip(slots, slots[1:])]
    assert len(rows) == 3 and all(59 <= gap <= 121 for gap in gaps)  # WORKER_MIN/MAX_DELAY of the test settings

    clock = {"now": utcnow()}
    worker = QueueWorker(ready.settings, ready.db, ready.registry, clock=lambda: clock["now"])
    assert worker.run_once().published == 0  # nothing is due yet
    clock["now"] = slots[0] + timedelta(seconds=1)
    assert worker.run_once().published == 1
    assert counts(ready)["PUBLISHED"] == 1 and counts(ready)["QUEUED"] == 2


def test_a_disabled_account_pauses_its_queue(ready):
    run(ready, mode="queued")
    make_due(ready)
    ready.registry.save_social_account(SocialAccount(
        category_id="sports", language="he", platform="console", account_handle="sports_he_console", enabled=False,
    ))
    assert ready.create_worker().run_once().published == 0 and counts(ready)["QUEUED"] == 3


def test_worker_spreads_a_backlog_instead_of_bursting(ready):
    run(ready, mode="queued")
    make_due(ready)  # e.g. the worker was down and everything is overdue

    report = ready.create_worker().run_once()

    assert report.published == 1 and report.deferred == 2
    remaining = ready.db.list_publications(statuses=["QUEUED"])
    assert all(from_iso(row["scheduled_for"]) > utcnow() + timedelta(seconds=30) for row in remaining)


def test_worker_stays_silent_outside_active_hours(ready):
    run(ready, mode="queued")
    make_due(ready)
    office_hours = JitterScheduler(tz=timezone.utc, start_hour=9, end_hour=17, min_delay=60, max_delay=120)
    worker = QueueWorker(ready.settings, ready.db, ready.registry, scheduler=office_hours, clock=lambda: at(3))

    report = worker.run_once()

    assert report.outside_active_hours and report.published == 0 and counts(ready)["QUEUED"] == 3


def test_missing_credentials_fail_the_item_cleanly_and_it_can_be_retried(ready, monkeypatch):
    monkeypatch.setenv("TG_SPORTS_BOT_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("TG_SPORTS_CHAT_ID", "@sports")
    run(ready, ["telegram_post"], mode="queued", limit=1)
    telegram_rows = ready.db.list_publications(statuses=["QUEUED"])
    assert len(telegram_rows) == 1 and telegram_rows[0]["platform"] == "telegram_pub"
    monkeypatch.delenv("TG_SPORTS_BOT_TOKEN")  # the secret disappears before the worker runs
    make_due(ready)

    ready.create_worker().run_once()

    failed = ready.db.list_publications(statuses=["FAILED"])
    assert [row["id"] for row in failed] == [telegram_rows[0]["id"]]
    assert "TG_SPORTS_BOT_TOKEN" in failed[0]["error_message"] and FAKE_TOKEN not in failed[0]["error_message"]
    assert ready.db.retry_failed() == 1 and counts(ready)["FAILED"] == 0


# -- KPIs ----------------------------------------------------------------------------- #
def test_metrics_use_the_latest_snapshot_and_the_spend_ledger(ready):
    run(ready)
    publication_id = ready.db.list_publications(statuses=["PUBLISHED"])[0]["id"]
    earlier = utcnow() - timedelta(hours=2)
    ready.db.record_metrics_snapshot(publication_id, views=100, clicks=10, conversions=1, revenue_usd=5.0, recorded_at=earlier)
    ready.db.record_metrics_snapshot(publication_id, views=400, clicks=40, conversions=3, revenue_usd=21.0)
    ready.db.record_ai_usage(
        pipeline_id="sports_he", product_ref="aliexpress_mock:1", model_used="claude-opus-5-5",
        tokens_input=1000, tokens_output=500, cost_usd=1.25,
    )
    metrics = MetricsService(ready.db, ready.settings)

    summary = metrics.get_overall_summary()
    assert (summary["total_views"], summary["total_clicks"], summary["total_conversions"]) == (400, 40, 3)
    assert summary["revenue_usd"] == 21.0 and summary["ai_spend_usd"] == 1.25 and summary["net_profit_usd"] == 19.75
    assert (summary["total_text"], summary["total_videos"]) == (3, 0)

    platform = metrics.get_metrics_by_platform()[0]
    assert platform["platform"] == "console_pub" and platform["published"] == 3 and platform["views"] == 400
    pipeline = metrics.get_metrics_by_pipeline()[0]
    assert (pipeline["pipeline_id"], pipeline["category"], pipeline["language"]) == ("sports_he", "sports", "he")
    assert pipeline["ai_spend_usd"] == 1.25 and pipeline["clicks"] == 40
    by_type = metrics.get_metrics_by_content_type()[0]
    assert by_type["content_format"] == "console_post" and by_type["published"] == 3
    assert {row["category"] for row in metrics.get_product_inventory()} == {"sports", "gadgets"}
    budget = metrics.get_budget_status()
    assert budget["state"] == "ok" and budget["ratio"] == pytest.approx(0.25)
