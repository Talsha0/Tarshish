"""Pipeline orchestrator: fetch → generate → store → publish / queue.

Fault isolation is layered:

* one product failing never aborts the batch,
* one target (page) failing never blocks the other targets of that product,
* one pipeline failing never prevents the next pipeline from running.

The single deliberate exception is :class:`BudgetExceededError`, which halts
everything: once the monthly AI cap is hit no further LLM call may be made.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

from core.config import PipelineConfig, Settings, SocialPage
from core.database import Database
from core.interfaces import BasePublisher, BaseStorage
from core.models import GeneratedContent, Product, PublicationStatus, PublishTarget, utcnow
from core.registry import PipelineRegistry, WiredPipeline, WiredTarget
from core.resilience import (
    BudgetExceededError,
    BudgetGuard,
    PipelineError,
    TarshishError,
    get_logger,
    redact,
)
from core.worker import JitterScheduler, deliver_publication

logger = get_logger("pipeline")

PublishingMode = Literal["immediate", "queued"]

# Sources are asked for more candidates than needed so that products already
# published can be skipped without the run coming back empty-handed.
OVERFETCH_FACTOR = 3


@dataclass
class TargetOutcome:
    platform: str
    page_name: str
    status: str  # published | queued | failed | preview | unavailable
    detail: str = ""


@dataclass
class ItemOutcome:
    external_id: str
    title: str
    status: str  # published | queued | partial | failed | skipped | preview
    detail: str = ""
    model_used: str = ""
    cost_usd: float = 0.0
    caption: str = ""
    hashtags: list[str] = field(default_factory=list)
    targets: list[TargetOutcome] = field(default_factory=list)


@dataclass
class RunReport:
    pipeline_id: str
    mode: str
    dry_run: bool
    started_at: datetime = field(default_factory=utcnow)
    finished_at: datetime | None = None
    fetched: int = 0
    items: list[ItemOutcome] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    halted_by_budget: bool = False

    def count(self, *statuses: str) -> int:
        return sum(1 for item in self.items if item.status in statuses)

    @property
    def cost_usd(self) -> float:
        return sum(item.cost_usd for item in self.items)

    @property
    def ok(self) -> bool:
        """True when the run finished without any product- or target-level failure."""
        return self.error is None and self.count("failed", "partial") == 0


class PipelineOrchestrator:
    """Runs one pipeline end to end."""

    def __init__(
        self,
        settings: Settings,
        db: Database,
        registry: PipelineRegistry,
        budget: BudgetGuard,
        scheduler: JitterScheduler | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.registry = registry
        self.budget = budget
        self.scheduler = scheduler or JitterScheduler.from_settings(settings)

    # -- public API ------------------------------------------------------------ #
    def run(
        self,
        pipeline_id: str,
        *,
        mode: PublishingMode | None = None,
        limit: int | None = None,
        dry_run: bool = False,
        force: bool = False,
    ) -> RunReport:
        """Execute a pipeline and return what happened.

        ``mode`` overrides the pipeline's configured publishing mode. ``dry_run``
        simulates everything without paid LLM calls, publishing or database
        writes. ``force`` re-processes products that were already handled.
        Raises :class:`BudgetExceededError` (with ``.report`` attached) when the
        AI budget guardrail trips, and :class:`PipelineError` for an unknown id.
        """
        config = self.registry.get_pipeline(pipeline_id)
        effective_mode: str = mode or config.publishing_mode
        report = RunReport(pipeline_id=config.id, mode=effective_mode, dry_run=dry_run)
        logger.info(
            "Pipeline '%s' starting (mode=%s%s).", config.id, effective_mode, ", dry-run" if dry_run else ""
        )
        try:
            self._execute(config, report, effective_mode, limit, dry_run, force)
        except BudgetExceededError as exc:
            report.halted_by_budget = True
            report.error = str(exc)
            report.finished_at = utcnow()
            exc.report = report  # type: ignore[attr-defined]
            raise
        except Exception as exc:
            report.error = redact(f"{type(exc).__name__}: {exc}")
            logger.error(
                "Pipeline '%s' aborted: %s", config.id, report.error, exc_info=not isinstance(exc, TarshishError)
            )
        report.finished_at = utcnow()
        logger.info(
            "Pipeline '%s' finished: fetched=%d published=%d queued=%d previewed=%d skipped=%d failed=%d cost=$%.4f.",
            config.id, report.fetched, report.count("published"), report.count("queued"), report.count("preview"),
            report.count("skipped"), report.count("failed", "partial"), report.cost_usd,
        )
        return report

    def run_all(self, **kwargs) -> list[RunReport]:
        """Run every enabled pipeline; a failing pipeline does not stop the others."""
        return [self.run(pipeline.id, **kwargs) for pipeline in self.registry.list_pipelines(enabled_only=True)]

    # -- stages ------------------------------------------------------------------ #
    def _execute(
        self, config: PipelineConfig, report: RunReport, mode: str, limit: int | None, dry_run: bool, force: bool
    ) -> None:
        if mode not in ("immediate", "queued"):
            raise PipelineError(f"Unsupported publishing mode '{mode}' (use 'immediate' or 'queued').")
        if not config.enabled and not dry_run:
            raise PipelineError(f"Pipeline '{config.id}' is disabled — enable it or use --dry-run to preview it.")

        wired = self.registry.wire(config, dry_run=dry_run, budget_guard=self.budget)
        report.warnings += [f"Target {page.label} skipped: {reason}" for page, reason in wired.skipped]
        if not dry_run and not wired.targets:
            raise PipelineError("no publishable target — every social page was skipped (see warnings above)")
        if dry_run and self.budget.is_exceeded():
            report.warnings.append("The monthly AI budget is exhausted — a live run with a paid model would halt.")

        wanted = limit or config.limit
        candidates = self._fetch(wired, wanted)
        report.fetched = len(candidates)

        handled = 0
        for product in candidates:
            if handled >= wanted:
                break
            try:
                outcome = self._process_product(wired, product, mode, dry_run, force)
            except BudgetExceededError:
                raise  # guardrail: stop the whole run, never swallow
            except Exception as exc:
                # Fault isolation: log, record, move on to the next product.
                logger.error(
                    "Pipeline '%s': product %s failed and was skipped: %s: %s",
                    config.id, product.ref, type(exc).__name__, exc,
                    exc_info=not isinstance(exc, TarshishError),
                )
                outcome = ItemOutcome(
                    external_id=product.external_id, title=product.title, status="failed",
                    detail=redact(f"{type(exc).__name__}: {exc}"),
                )
            report.items.append(outcome)
            if outcome.status != "skipped":
                handled += 1

        if not dry_run:
            self._apply_retention(wired.storage)

    def _fetch(self, wired: WiredPipeline, wanted: int) -> list[Product]:
        config = wired.config
        filters = config.filters
        products = wired.source.fetch_products(
            config.niche, filters.min_rating, filters.min_orders, wanted * OVERFETCH_FACTOR
        )
        # Plugins are untrusted: re-apply the filters and drop in-batch duplicates.
        unique: dict[str, Product] = {}
        for product in products:
            if product.rating >= filters.min_rating and product.orders_count >= filters.min_orders:
                unique.setdefault(product.canonical_hash, product)
        logger.info(
            "Pipeline '%s': source '%s' returned %d product(s), %d usable.",
            config.id, wired.source.source_name, len(products), len(unique),
        )
        return list(unique.values())

    def _process_product(
        self, wired: WiredPipeline, product: Product, mode: str, dry_run: bool, force: bool
    ) -> ItemOutcome:
        config = wired.config
        already = self.db.handled_targets(config.id, product.canonical_hash)
        seen_before = bool(already)

        # Live runs act on the targets that could be wired; a dry-run previews every configured page.
        pending = [target for target in wired.targets if force or target.page.key not in already]
        if dry_run:
            pages = [page for page in config.social_pages if force or page.key not in already]
        else:
            pages = [target.page for target in pending]
        if not pages:
            return ItemOutcome(
                external_id=product.external_id, title=product.title, status="skipped",
                detail="duplicate — already published or queued for every target (use --force to redo)",
            )

        content = wired.generator.generate(product, config.language, config.prompt_template)
        outcome = ItemOutcome(
            external_id=product.external_id, title=product.title, status="preview",
            model_used=content.model_used, cost_usd=content.generation_cost_usd,
            caption=content.caption, hashtags=list(content.hashtags),
        )

        if dry_run:
            outcome.targets = self._preview(wired, content, pages, mode)
            if seen_before:
                outcome.detail = "already handled earlier — a live run would skip it unless forced"
            return outcome

        # Record spend first: once the provider has charged us, the ledger must know.
        if content.generation_cost_usd > 0 or content.tokens_input or content.tokens_output:
            self.db.record_ai_usage(
                pipeline_id=config.id, product_ref=product.ref, model_used=content.model_used,
                tokens_input=content.tokens_input, tokens_output=content.tokens_output,
                cost_usd=content.generation_cost_usd,
            )
        content = self._store_media(wired.storage, config, content)
        product_id = self.db.upsert_product(product)

        cost_share = content.generation_cost_usd / len(pending)
        outcome.targets = [self._dispatch(config, product_id, target, content, mode, cost_share) for target in pending]
        statuses = {target.status for target in outcome.targets}
        if statuses == {"failed"}:
            outcome.status = "failed"
        elif "failed" in statuses:
            outcome.status = "partial"
        else:
            outcome.status = "queued" if mode == "queued" else "published"
        if outcome.status in ("failed", "partial"):
            outcome.detail = "; ".join(
                f"{t.platform} → {t.page_name}: {t.detail}" for t in outcome.targets if t.status == "failed"
            )
        return outcome

    def _dispatch(
        self, config: PipelineConfig, product_id: int, target: WiredTarget, content: GeneratedContent,
        mode: str, cost_share: float,
    ) -> TargetOutcome:
        """Publish to (or queue for) one target. Isolated: never raises."""
        page = target.page
        try:
            common = dict(
                pipeline_id=config.id, product_id=product_id, platform=page.platform, target_page=page.page_name,
                target_json=page.model_dump_json(exclude_none=True), content=content, cost_share_usd=cost_share,
            )
            if mode == "queued":
                slot = self.scheduler.slot_for(utcnow(), self.db.last_activity_at(page.platform, page.page_name))
                self.db.insert_publication(status=PublicationStatus.QUEUED, scheduled_for=slot, **common)
                local = slot.astimezone(self.scheduler.tz).strftime("%Y-%m-%d %H:%M %Z")
                return TargetOutcome(page.platform, page.page_name, "queued", f"scheduled for {local}")

            publication_id = self.db.insert_publication(status=PublicationStatus.PENDING, **common)
            receipt = deliver_publication(self.db, publication_id, target.publisher, content)
            if receipt.success:
                return TargetOutcome(page.platform, page.page_name, "published", f"post id {receipt.platform_post_id}")
            return TargetOutcome(page.platform, page.page_name, "failed", receipt.error_message or "unknown error")
        except Exception as exc:
            logger.exception("Pipeline '%s': target %s failed.", config.id, page.label)
            return TargetOutcome(page.platform, page.page_name, "failed", redact(f"{type(exc).__name__}: {exc}"))

    # -- dry-run ------------------------------------------------------------------- #
    def _preview(
        self, wired: WiredPipeline, content: GeneratedContent, pages: list[SocialPage], mode: str
    ) -> list[TargetOutcome]:
        """Render the content through the dry-run publisher instead of the real targets."""
        ready = {target.page.key for target in wired.targets}
        reasons = {page.key: reason for page, reason in wired.skipped}
        outcomes = [
            TargetOutcome(page.platform, page.page_name, "preview", "ready")
            if page.key in ready
            else TargetOutcome(page.platform, page.page_name, "unavailable", reasons.get(page.key, "not available"))
            for page in pages
        ]
        intended = [
            f"{o.platform} → {o.page_name}" + ("" if o.status == "preview" else f"  (would be skipped: {o.detail})")
            for o in outcomes
        ]
        publisher = self._dry_run_publisher(wired.config, mode, intended)
        try:
            if publisher is None:
                raise PipelineError("no dry-run publisher available")
            publisher.publish(content)
        except Exception as exc:
            logger.debug("Dry-run publisher unavailable (%s); using plain output.", exc)
            print(f"\n[DRY-RUN] {content.product.title}\n{content.caption}\n")
        return outcomes

    def _dry_run_publisher(self, config: PipelineConfig, mode: str, intended: list[str]) -> BasePublisher | None:
        name = self.settings.dry_run_publisher
        if not self.registry.plugins.has("publisher", name):
            return None
        target = PublishTarget(
            platform=name,
            page_name="dry-run preview",
            options={"dry_run": True, "pipeline_id": config.id, "mode": mode, "intended_targets": intended},
        )
        context = self.registry.context_for(config, dry_run=True)
        return self.registry.plugins.get("publisher", name)(context, target)

    # -- storage --------------------------------------------------------------------- #
    def _store_media(self, storage: BaseStorage, config: PipelineConfig, content: GeneratedContent) -> GeneratedContent:
        """Hand generated media to the storage backend and point the content at the stored copy."""
        if not content.media_local_path:
            return content
        source = Path(content.media_local_path)
        if not source.is_file():
            logger.warning("Generated media %s does not exist; publishing without it.", source)
            content.media_local_path = None
            return content
        key = f"{config.id}/{utcnow():%Y%m%d}/{content.product.canonical_hash[:16]}{source.suffix.lower()}"
        uri = storage.save(str(source), key)
        if uri.lower().startswith(("http://", "https://")):
            content.media_public_url = uri
        else:
            content.media_local_path = uri
        return content

    def _apply_retention(self, storage: BaseStorage) -> None:
        try:
            removed = storage.cleanup(protected=self.db.active_media_uris())
            if removed:
                logger.info("Storage retention removed %d expired media file(s).", removed)
        except Exception as exc:
            logger.warning("Storage cleanup failed (ignored): %s", exc)
