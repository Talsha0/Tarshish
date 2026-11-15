import random
from datetime import datetime, timedelta, timezone

import pytest

from core.database import from_iso, to_iso
from core.models import utcnow
from core.resilience import BudgetExceededError
from core.worker import JitterScheduler, QueueWorker
from dashboard.metrics_service import MetricsService

FAKE_TOKEN = "7123456789:" + "A" * 35


def counts(engine):
    return engine.db.get_status_counts()


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
def test_dry_run_prints_previews_and_has_no_side_effects(engine, capsys):
    report = engine.orchestrator.run("sports_he", mode="immediate", dry_run=True)

    assert report.ok and report.fetched == 3 and report.count("preview") == 3
    assert sum(counts(engine).values()) == 0
    assert engine.db.query_one("SELECT COUNT(*) AS n FROM products")["n"] == 0
    assert engine.db.get_monthly_ai_spend() == 0
    printed = capsys.readouterr().out
    assert printed.count("DRY-RUN PREVIEW") == 3 and "לרכישה" in printed
    # The Telegram target has no credentials: warned about and skipped, not fatal.
    assert any("TG_SPORTS_BOT_TOKEN" in warning for warning in report.warnings)


def test_immediate_mode_publishes_and_skips_only_the_unconfigured_target(engine):
    report = engine.orchestrator.run("sports_he", mode="immediate")

    assert report.ok and report.count("published") == 3
    rows = engine.db.list_publications()
    assert {row["platform"] for row in rows} == {"console_pub"}
    assert all(row["status"] == "PUBLISHED" and row["platform_post_id"] for row in rows)


def test_reruns_are_deduplicated_and_force_overrides(engine):
    engine.orchestrator.run("sports_he")
    second = engine.orchestrator.run("sports_he")
    assert second.count("skipped") == 3 and counts(engine)["PUBLISHED"] == 3

    forced = engine.orchestrator.run("sports_he", force=True, limit=1)
    assert forced.count("published") == 1 and counts(engine)["PUBLISHED"] == 4


def test_limit_walks_past_already_published_products(engine):
    titles = set()
    for _ in range(3):
        report = engine.orchestrator.run("sports_he", limit=1)
        titles |= {item.title for item in report.items if item.status == "published"}
    assert len(titles) == 3  # one new product per run, never the same one twice


def test_target_added_later_is_backfilled_and_no_secret_is_persisted(engine, monkeypatch):
    engine.orchestrator.run("sports_he")  # console only: Telegram has no credentials yet
    monkeypatch.setenv("TG_SPORTS_BOT_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("TG_SPORTS_CHAT_ID", "@sports")

    report = engine.orchestrator.run("sports_he", mode="queued")

    queued = engine.db.list_publications(statuses=["QUEUED"])
    assert report.count("queued") == 3 and {row["platform"] for row in queued} == {"telegram_pub"}
    assert counts(engine)["PUBLISHED"] == 3  # console posts were not repeated
    stored = str(engine.db.query("SELECT * FROM publications")) + str(engine.db.query("SELECT * FROM pipelines"))
    assert FAKE_TOKEN not in stored and "TG_SPORTS_BOT_TOKEN" in stored


def test_one_failing_product_does_not_stop_the_batch(engine, monkeypatch):
    cls = engine.plugins.get("generator", "llm_text_gen")
    original = cls.generate

    def flaky(self, product, language, prompt_template):
        if "Hydration" in product.title:
            raise RuntimeError("generator crashed")
        return original(self, product, language, prompt_template)

    monkeypatch.setattr(cls, "generate", flaky)
    report = engine.orchestrator.run("sports_he")

    assert report.count("failed") == 1 and report.count("published") == 2
    assert not report.ok and report.error is None
    assert "generator crashed" in next(item.detail for item in report.items if item.status == "failed")


def test_disabled_pipeline_only_runs_as_dry_run(engine):
    engine.registry.set_enabled("sports_he", False)
    assert "disabled" in engine.orchestrator.run("sports_he").error
    assert engine.orchestrator.run("sports_he", dry_run=True).count("preview") == 3


# -- budget guardrail ------------------------------------------------------------- #
def test_budget_guardrail_halts_the_run_once_the_cap_is_reached(engine, monkeypatch):
    make_paid(engine, monkeypatch, cost=3.0)  # cap is $5: two products fit, the third must not start

    with pytest.raises(BudgetExceededError) as excinfo:
        engine.orchestrator.run("sports_he")

    report = excinfo.value.report
    assert report.halted_by_budget and report.count("published") == 2
    assert engine.db.get_monthly_ai_spend() == pytest.approx(6.0)
    with pytest.raises(BudgetExceededError):  # and it stays halted for every later run
        engine.orchestrator.run("sports_he", force=True)


def test_flush_clears_the_queue_but_never_the_spend_ledger(engine, monkeypatch):
    make_paid(engine, monkeypatch, cost=0.5)
    engine.orchestrator.run("sports_he", mode="queued")
    assert counts(engine)["QUEUED"] == 3 and engine.db.get_monthly_ai_spend() == pytest.approx(1.5)

    assert engine.db.flush_queue() == 3

    assert sum(counts(engine).values()) == 0
    assert engine.db.get_monthly_ai_spend() == pytest.approx(1.5)


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


def test_queued_items_get_jittered_slots_and_the_worker_publishes_them_when_due(engine):
    engine.orchestrator.run("sports_he", mode="queued")
    rows = sorted(engine.db.list_publications(statuses=["QUEUED"]), key=lambda row: row["scheduled_for"])
    slots = [from_iso(row["scheduled_for"]) for row in rows]
    gaps = [(later - earlier).total_seconds() for earlier, later in zip(slots, slots[1:])]
    assert len(rows) == 3 and all(59 <= gap <= 121 for gap in gaps)  # WORKER_MIN/MAX_DELAY of the test settings

    clock = {"now": utcnow()}
    worker = QueueWorker(engine.settings, engine.db, engine.registry, clock=lambda: clock["now"])
    assert worker.run_once().published == 0  # nothing is due yet
    clock["now"] = slots[0] + timedelta(seconds=1)
    assert worker.run_once().published == 1
    assert counts(engine)["PUBLISHED"] == 1 and counts(engine)["QUEUED"] == 2


def test_worker_spreads_a_backlog_instead_of_bursting(engine):
    engine.orchestrator.run("sports_he", mode="queued")
    make_due(engine)  # e.g. the worker was down and everything is overdue

    report = engine.create_worker().run_once()

    assert report.published == 1 and report.deferred == 2
    remaining = engine.db.list_publications(statuses=["QUEUED"])
    assert all(from_iso(row["scheduled_for"]) > utcnow() + timedelta(seconds=30) for row in remaining)


def test_worker_stays_silent_outside_active_hours(engine):
    engine.orchestrator.run("sports_he", mode="queued")
    make_due(engine)
    office_hours = JitterScheduler(tz=timezone.utc, start_hour=9, end_hour=17, min_delay=60, max_delay=120)
    worker = QueueWorker(engine.settings, engine.db, engine.registry, scheduler=office_hours, clock=lambda: at(3))

    report = worker.run_once()

    assert report.outside_active_hours and report.published == 0 and counts(engine)["QUEUED"] == 3


def test_missing_credentials_fail_the_item_cleanly_and_it_can_be_retried(engine, monkeypatch):
    monkeypatch.setenv("TG_SPORTS_BOT_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("TG_SPORTS_CHAT_ID", "@sports")
    engine.orchestrator.run("sports_he", mode="queued", limit=1)
    telegram_rows = [row for row in engine.db.list_publications(statuses=["QUEUED"]) if row["platform"] == "telegram_pub"]
    assert len(telegram_rows) == 1
    monkeypatch.delenv("TG_SPORTS_BOT_TOKEN")  # the secret disappears before the worker runs
    make_due(engine)

    engine.create_worker().run_once()

    failed = engine.db.list_publications(statuses=["FAILED"])
    assert [row["id"] for row in failed] == [telegram_rows[0]["id"]]
    assert "TG_SPORTS_BOT_TOKEN" in failed[0]["error_message"] and FAKE_TOKEN not in failed[0]["error_message"]
    assert engine.db.retry_failed() == 1 and counts(engine)["FAILED"] == 0


# -- KPIs ----------------------------------------------------------------------------- #
def test_metrics_use_the_latest_snapshot_and_the_spend_ledger(engine):
    engine.orchestrator.run("sports_he")
    publication_id = engine.db.list_publications(statuses=["PUBLISHED"])[0]["id"]
    earlier = utcnow() - timedelta(hours=2)
    engine.db.record_metrics_snapshot(publication_id, views=100, clicks=10, conversions=1, revenue_usd=5.0, recorded_at=earlier)
    engine.db.record_metrics_snapshot(publication_id, views=400, clicks=40, conversions=3, revenue_usd=21.0)
    engine.db.record_ai_usage(
        pipeline_id="sports_he", product_ref="aliexpress_mock:1", model_used="claude-opus-5-5",
        tokens_input=1000, tokens_output=500, cost_usd=1.25,
    )
    metrics = MetricsService(engine.db, engine.settings)

    summary = metrics.get_overall_summary()
    assert (summary["total_views"], summary["total_clicks"], summary["total_conversions"]) == (400, 40, 3)
    assert summary["revenue_usd"] == 21.0 and summary["ai_spend_usd"] == 1.25 and summary["net_profit_usd"] == 19.75
    assert (summary["total_text"], summary["total_videos"]) == (3, 0)

    platform = metrics.get_metrics_by_platform()[0]
    assert platform["platform"] == "console_pub" and platform["published"] == 3 and platform["views"] == 400
    pipeline = metrics.get_metrics_by_pipeline()[0]
    assert pipeline["pipeline_id"] == "sports_he" and pipeline["ai_spend_usd"] == 1.25 and pipeline["clicks"] == 40
    budget = metrics.get_budget_status()
    assert budget["state"] == "ok" and budget["ratio"] == pytest.approx(0.25)
