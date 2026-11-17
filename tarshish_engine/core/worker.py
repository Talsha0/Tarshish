"""Queue consumer with human-like pacing.

Two pieces live here:

* ``JitterScheduler`` decides *when* a queued post may go out: randomised gaps
  (not a fixed cadence) and only inside the configured active-hours window.
  The orchestrator uses it to stamp ``scheduled_for`` when content is queued.
* ``QueueWorker`` publishes rows whose slot has arrived. It re-checks the
  active window and refuses to post to the same page in a burst, so a backlog
  that built up while the worker was down is spread out again instead of being
  dumped all at once.
"""

from __future__ import annotations

import json
import random
import threading
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone, tzinfo
from typing import Any, Callable

from core.config import Settings, SocialPage
from core.database import Database
from core.interfaces import BasePublisher, PluginContext
from core.models import GeneratedContent, PublishReceipt, utcnow
from core.registry import ConfigRegistry
from core.resilience import get_logger, redact

logger = get_logger("worker")

STALE_PENDING_AFTER = timedelta(minutes=15)


def deliver_publication(
    db: Database, publication_id: int, publisher: BasePublisher, content: GeneratedContent
) -> PublishReceipt:
    """Publish one row and record the outcome. Never raises on publisher failure."""
    try:
        receipt = publisher.publish(content)
    except Exception as exc:
        logger.error("Publication #%d → %s failed: %s: %s", publication_id, publisher.platform_name, type(exc).__name__, exc)
        receipt = PublishReceipt(
            platform=publisher.platform_name, success=False, error_message=f"{type(exc).__name__}: {exc}"
        )
    if receipt.success:
        db.mark_published(publication_id, receipt.platform_post_id, receipt.published_at)
    else:
        # Error text may quote a request URL or header — scrub it before it reaches the DB.
        receipt.error_message = redact(receipt.error_message or "publisher reported failure without details")
        db.mark_failed(publication_id, receipt.error_message)
    return receipt


class JitterScheduler:
    """Human-like publication timing: random gaps inside an active-hours window."""

    def __init__(
        self,
        *,
        tz: tzinfo,
        start_hour: int,
        end_hour: int,
        min_delay: float,
        max_delay: float,
        rng: random.Random | None = None,
    ) -> None:
        self.tz = tz
        self.start_hour = start_hour
        self.end_hour = end_hour
        self.min_delay = float(min_delay)
        self.max_delay = float(max(max_delay, min_delay))
        self._rng = rng or random.Random()

    @classmethod
    def from_settings(cls, settings: Settings, rng: random.Random | None = None) -> "JitterScheduler":
        return cls(
            tz=settings.tzinfo,
            start_hour=settings.worker_active_hours_start,
            end_hour=settings.worker_active_hours_end,
            min_delay=settings.worker_min_delay_seconds,
            max_delay=settings.worker_max_delay_seconds,
            rng=rng,
        )

    @property
    def always_active(self) -> bool:
        return (self.end_hour - self.start_hour) % 24 == 0

    def is_active(self, moment: datetime) -> bool:
        """True when ``moment`` falls inside the active window (may wrap past midnight)."""
        if self.always_active:
            return True
        local = moment.astimezone(self.tz)
        hour = local.hour + local.minute / 60 + local.second / 3600
        if self.start_hour < self.end_hour:
            return self.start_hour <= hour < self.end_hour
        return hour >= self.start_hour or hour < self.end_hour

    def next_window_start(self, moment: datetime) -> datetime:
        local = moment.astimezone(self.tz)
        opening = datetime.combine(local.date(), time(self.start_hour % 24), tzinfo=self.tz)
        if opening <= local:
            opening = datetime.combine(local.date() + timedelta(days=1), time(self.start_hour % 24), tzinfo=self.tz)
        return opening.astimezone(timezone.utc)

    def human_delay(self) -> float:
        """Seconds between two posts: skewed towards the short end, never a round number."""
        if self.max_delay <= self.min_delay:
            return self.min_delay
        mode = self.min_delay + (self.max_delay - self.min_delay) * 0.35
        return self._rng.triangular(self.min_delay, self.max_delay, mode)

    def into_active_hours(self, moment: datetime) -> datetime:
        """Push a moment outside the window to shortly after the next opening."""
        if self.is_active(moment):
            return moment
        warm_up = self._rng.uniform(0.0, min(self.max_delay, 1800.0))
        return self.next_window_start(moment) + timedelta(seconds=warm_up)

    def next_slot(self, after: datetime) -> datetime:
        """The slot following a post at ``after``."""
        return self.into_active_hours(after + timedelta(seconds=self.human_delay()))

    def first_slot(self, now: datetime) -> datetime:
        """Slot for a page that has been quiet: soon, but not on the dot."""
        lead_in = self._rng.uniform(0.05, 0.3) * self.min_delay
        return self.into_active_hours(now + timedelta(seconds=lead_in))

    def slot_for(self, now: datetime, last_activity: datetime | None) -> datetime:
        """Next slot for a page given its latest published/scheduled moment."""
        if last_activity is None or last_activity < now - timedelta(seconds=self.max_delay):
            return self.first_slot(now)
        return self.next_slot(max(last_activity, now))


@dataclass
class WorkerReport:
    published: int = 0
    failed: int = 0
    deferred: int = 0
    recovered: int = 0
    outside_active_hours: bool = False

    @property
    def handled(self) -> int:
        return self.published + self.failed


class QueueWorker:
    """Publishes QUEUED publications when their jittered slot arrives."""

    def __init__(
        self,
        settings: Settings,
        db: Database,
        registry: ConfigRegistry,
        scheduler: JitterScheduler | None = None,
        *,
        clock: Callable[[], datetime] = utcnow,
        batch_size: int = 20,
    ) -> None:
        self.settings = settings
        self.db = db
        self.registry = registry
        self.scheduler = scheduler or JitterScheduler.from_settings(settings)
        self._clock = clock
        self._batch_size = batch_size
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    # -- one pass -------------------------------------------------------------- #
    def run_once(self) -> WorkerReport:
        """Publish every due item that pacing rules allow right now."""
        report = WorkerReport()
        now = self._clock()
        report.recovered = self.db.fail_stale_pending(now - STALE_PENDING_AFTER)
        if report.recovered:
            logger.warning("%d publication(s) were stuck in PENDING and are now FAILED for review.", report.recovered)

        if not self.scheduler.is_active(now):
            report.outside_active_hours = True
            return report

        # Posting twice to one page within half the minimum gap is a burst, not a human.
        burst_window = timedelta(seconds=self.scheduler.min_delay * 0.5)
        for row in self.db.fetch_due_publications(now, limit=self._batch_size):
            if self._stop.is_set():
                break
            now = self._clock()
            if not self.scheduler.is_active(now):
                report.outside_active_hours = True
                break
            last = self.db.last_published_at(row["platform"], row["target_page"])
            if last is not None and now - last < burst_window:
                self.db.reschedule_publication(row["id"], self.scheduler.next_slot(last))
                report.deferred += 1
                continue
            if not self.db.claim_publication(row["id"]):
                continue  # another worker took it
            receipt = self._publish_row(row)
            if receipt.success:
                report.published += 1
                logger.info(
                    "Published #%d → %s (%s), post id %s.",
                    row["id"], row["platform"], row["target_page"], receipt.platform_post_id,
                )
            else:
                report.failed += 1
        return report

    def _publish_row(self, row: dict[str, Any]) -> PublishReceipt:
        """Rebuild content + publisher from the stored snapshot and deliver it."""
        publication_id = int(row["id"])
        try:
            content = GeneratedContent.model_validate_json(row["content_json"])
            page = SocialPage.model_validate(json.loads(row["target_json"]))
            context = PluginContext(settings=self.settings, pipeline_id=row["pipeline_id"], storage=self.registry.storage())
            publisher, reason = self.registry.build_publisher(page, context)
        except Exception as exc:
            publisher, reason = None, f"stored publication could not be rebuilt: {type(exc).__name__}: {exc}"
        if publisher is None:
            message = redact(f"Target unavailable: {reason}")
            logger.warning("Publication #%d cannot be published — %s", publication_id, message)
            self.db.mark_failed(publication_id, message)
            return PublishReceipt(platform=row["platform"], success=False, error_message=message)
        return deliver_publication(self.db, publication_id, publisher, content)

    # -- long-running loop ------------------------------------------------------ #
    def run_forever(self) -> None:
        """Poll the queue until :meth:`stop` is called (Ctrl+C in the CLI)."""
        scheduler = self.scheduler
        logger.info(
            "Queue worker started — active hours %02d:00-%02d:00 %s, %.0f-%.0fs between posts.",
            scheduler.start_hour, scheduler.end_hour, self.settings.worker_timezone,
            scheduler.min_delay, scheduler.max_delay,
        )
        announced_sleep = False
        while not self._stop.is_set():
            now = self._clock()
            if not scheduler.is_active(now):
                opening = scheduler.next_window_start(now)
                if not announced_sleep:
                    logger.info(
                        "Outside active hours — sleeping until %s.",
                        opening.astimezone(scheduler.tz).strftime("%Y-%m-%d %H:%M %Z"),
                    )
                    announced_sleep = True
                self._stop.wait(min(max((opening - now).total_seconds(), 1.0), 300.0))
                continue
            announced_sleep = False
            try:
                self.run_once()
            except Exception:
                logger.exception("Worker pass failed; will retry on the next poll.")
            # Jitter the poll itself so wake-ups never form a fixed cadence.
            self._stop.wait(self.settings.worker_poll_interval_seconds * random.uniform(0.8, 1.3))
        logger.info("Queue worker stopped.")
