"""Stage 2 — publishing pipelines: harvested products ➔ localized content ➔ social accounts.

A publishing pipeline is a ``(category, language)`` pair (id ``<category>_<language>``,
e.g. ``gadgets_he``) run for one or more content types::

    python main.py --publish --category gadgets --lang he --content-types instagram_post,instagram_story

For every content type the orchestrator resolves, from the normalized configuration:

* the **content profile** -> generator plugin + LLM model + tone + publish format,
* the **social account** for ``(category, language, profile.platform)`` -> publisher + page,

then reads products of the category from SQLite that this account has not received in
this format yet, generates the localized content and publishes (or queues) it.
Nothing here fetches from a store: products come only from Stage 1 (``--harvest``).

Fault isolation is layered:

* one product failing never aborts the batch,
* one content type failing (no account, missing credentials) never blocks the others,
* one pipeline failing never prevents the next pipeline from running.

The single deliberate exception is :class:`BudgetExceededError`, which halts
everything: once the monthly AI cap is hit no further LLM call may be made.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal, Sequence

from core.config import CategoryConfig, ContentProfile, Settings, SocialAccount, SocialPage
from core.database import Database
from core.interfaces import BaseGenerator, BasePublisher, BaseStorage
from core.models import GeneratedContent, Product, PublicationStatus, PublishTarget, utcnow
from core.registry import ConfigRegistry
from core.resilience import (
    BudgetExceededError,
    BudgetGuard,
    PipelineError,
    TarshishError,
    get_logger,
    redact,
)
from core.worker import JitterScheduler, deliver_publication

logger = get_logger("orchestrator")

#: Prefix of ``error_message`` on content stored without being sent (``store_unpublishable``).
NOT_SENT = "Not sent — stored for review:"

PublishingMode = Literal["immediate", "queued"]


def pipeline_id_for(category: str, language: str) -> str:
    """The publishing pipeline of a ``(category, language)`` pair: ``gadgets_he``."""
    return f"{category}_{language}"


@dataclass
class ItemOutcome:
    """One product × content type."""

    content_type: str
    external_id: str
    title: str
    status: str  # published | queued | stored | preview | failed
    target: str = ""            # "instagram_pub → @gadgets.il"
    detail: str = ""
    configured_model: str = ""  # the model the content profile routes to
    model_used: str = ""        # what actually wrote it (offline stand-in on a dry-run)
    cost_usd: float = 0.0
    caption: str = ""
    hashtags: list[str] = field(default_factory=list)


@dataclass
class ContentTypeRoute:
    """How one content type of a run was wired (shown even when nothing was published)."""

    content_type: str
    platform: str
    generator: str
    llm_model: str
    account: str = ""           # account handle, "" when none is configured
    publisher: str = ""
    ready: bool = False         # credentials resolved (always False for a missing account)
    reason: str = ""            # why it is not ready / was skipped
    candidates: int = 0         # unpublished products found


@dataclass
class RunReport:
    category: str
    language: str
    mode: str
    dry_run: bool
    started_at: datetime = field(default_factory=utcnow)
    finished_at: datetime | None = None
    routes: list[ContentTypeRoute] = field(default_factory=list)
    items: list[ItemOutcome] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    halted_by_budget: bool = False

    @property
    def pipeline_id(self) -> str:
        return pipeline_id_for(self.category, self.language)

    def count(self, *statuses: str) -> int:
        return sum(1 for item in self.items if item.status in statuses)

    @property
    def cost_usd(self) -> float:
        return sum(item.cost_usd for item in self.items)

    @property
    def ok(self) -> bool:
        """True when the run finished without any product-level failure."""
        return self.error is None and self.count("failed") == 0


class PublishingOrchestrator:
    """Runs publishing pipelines over harvested products."""

    def __init__(
        self,
        settings: Settings,
        db: Database,
        registry: ConfigRegistry,
        budget: BudgetGuard,
        scheduler: JitterScheduler | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.registry = registry
        self.budget = budget
        self.scheduler = scheduler or JitterScheduler.from_settings(settings)

    # -- planning ---------------------------------------------------------------- #
    def plan(self, category: str | None = None, language: str | None = None) -> list[tuple[str, str]]:
        """``(category, language)`` pairs to run: the given ones, or every pair with an enabled account."""
        if category and language:
            return [(category, language)]
        categories = [category] if category else [c.category_id for c in self.registry.list_categories(enabled_only=True)]
        pairs: list[tuple[str, str]] = []
        for category_id in categories:
            languages = [language] if language else sorted({
                a.language for a in self.registry.list_social_accounts(category=category_id, enabled_only=True)
            })
            pairs.extend((category_id, lang) for lang in languages)
        return pairs

    def run_all(self, category: str | None = None, language: str | None = None, **kwargs) -> list[RunReport]:
        """Run every planned pipeline; a failing pipeline does not stop the others."""
        return [self.run(cat, lang, **kwargs) for cat, lang in self.plan(category, language)]

    # -- public API ---------------------------------------------------------------- #
    def run(
        self,
        category: str,
        language: str,
        content_types: Sequence[str] | None = None,
        *,
        mode: PublishingMode | None = None,
        limit: int | None = None,
        dry_run: bool = False,
        force: bool = False,
        store_unpublishable: bool = False,
    ) -> RunReport:
        """Generate and publish content for one ``(category, language)``.

        ``content_types`` restricts the run (default: every enabled content profile whose
        platform has an enabled account for this category and language). ``dry_run``
        simulates everything without paid LLM calls, publishing or database writes.
        ``force`` re-processes products the account already received. ``store_unpublishable``
        still generates and stores content for an account that cannot publish yet (e.g. its token
        is not set): the rows are kept as ``FAILED`` with a :data:`NOT_SENT` note, never sent, so
        they can be reviewed (Content Review) and published once the credentials exist. Raises
        :class:`BudgetExceededError` (with ``.report`` attached) when the AI budget
        guardrail trips; any other problem is recorded on the report.
        """
        effective_mode = mode or self.settings.publishing_mode
        report = RunReport(category=category, language=language, mode=effective_mode, dry_run=dry_run)
        logger.info(
            "Publishing pipeline '%s' starting (content types=%s, mode=%s%s).", report.pipeline_id,
            ",".join(content_types) if content_types else "all", effective_mode, ", dry-run" if dry_run else "",
        )
        try:
            self._execute(report, content_types, effective_mode, limit, dry_run, force, store_unpublishable)
        except BudgetExceededError as exc:
            report.halted_by_budget = True
            report.error = str(exc)
            report.finished_at = utcnow()
            exc.report = report  # type: ignore[attr-defined]
            raise
        except Exception as exc:
            report.error = redact(f"{type(exc).__name__}: {exc}" if not isinstance(exc, TarshishError) else str(exc))
            logger.error(
                "Publishing pipeline '%s' aborted: %s", report.pipeline_id, report.error,
                exc_info=not isinstance(exc, TarshishError),
            )
        report.finished_at = utcnow()
        logger.info(
            "Publishing pipeline '%s' finished: published=%d queued=%d previewed=%d failed=%d cost=$%.4f.",
            report.pipeline_id, report.count("published"), report.count("queued"), report.count("preview"),
            report.count("failed"), report.cost_usd,
        )
        return report

    # -- stages ------------------------------------------------------------------------ #
    def _execute(
        self, report: RunReport, content_types: Sequence[str] | None, mode: str, limit: int | None,
        dry_run: bool, force: bool, store_unpublishable: bool = False,
    ) -> None:
        if mode not in ("immediate", "queued"):
            raise PipelineError(f"Unsupported publishing mode '{mode}' (use 'immediate' or 'queued').")
        category = self.registry.get_category(report.category)
        if not category.enabled and not dry_run:
            raise PipelineError(f"Category '{category.category_id}' is disabled — enable it or use --dry-run.")
        if dry_run and self.budget.is_exceeded():
            report.warnings.append("The monthly AI budget is exhausted — a live run with a paid model would halt.")

        for profile in self._profiles(report, content_types):
            self._run_content_type(report, category, profile, mode, limit or self.settings.publish_batch_limit,
                                   dry_run, force, store_unpublishable)

        if not dry_run:
            self._apply_retention(self.registry.storage())

    def _profiles(self, report: RunReport, content_types: Sequence[str] | None) -> list[ContentProfile]:
        if content_types:
            profiles = [self.registry.get_content_profile(name) for name in dict.fromkeys(content_types)]
            for profile in profiles:
                if not profile.enabled:
                    report.warnings.append(f"Content type '{profile.content_type}' is disabled in its profile; "
                                           "running it because it was asked for explicitly.")
            return profiles
        # Default: every enabled content type that has an enabled account for this category + language.
        platforms = {
            account.platform for account in self.registry.list_social_accounts(
                category=report.category, language=report.language, enabled_only=True)
        }
        profiles = [p for p in self.registry.list_content_profiles(enabled_only=True) if p.platform in platforms]
        if not profiles:
            raise PipelineError(
                f"No enabled social account for category '{report.category}' in language '{report.language}'. "
                "Link one in the dashboard (Social Accounts tab) or config/social_accounts.yaml."
            )
        return profiles

    def _run_content_type(
        self, report: RunReport, category: CategoryConfig, profile: ContentProfile, mode: str, limit: int,
        dry_run: bool, force: bool, store_unpublishable: bool = False,
    ) -> None:
        route = ContentTypeRoute(
            content_type=profile.content_type, platform=profile.platform, generator=profile.generator,
            llm_model=profile.llm_model,
        )
        report.routes.append(route)
        account = self.registry.get_social_account(category.category_id, report.language, profile.platform)
        if account is None or not account.enabled:
            route.reason = (
                f"no {'enabled ' if account else ''}{profile.platform} account for "
                f"({category.category_id}, {report.language}, {profile.platform})"
            )
            report.warnings.append(f"{profile.content_type} skipped: {route.reason}.")
            return
        page = self.registry.social_page(account, profile)
        route.account, route.publisher = account.account_handle, page.platform

        context = self.registry.context_for(
            pipeline_id=report.pipeline_id, llm_model=profile.llm_model, dry_run=dry_run, budget_guard=self.budget,
            options={"content_type": profile.content_type, "tone": profile.tone, "publish_format": profile.publish_format,
                     "category_name": category.display_name(report.language)},
        )
        publisher, reason = self.registry.build_publisher(page, context)
        route.ready, route.reason = publisher is not None, reason or ""
        unavailable = None
        if publisher is None:
            unavailable = reason or "the publisher could not be built"
            if store_unpublishable and not dry_run:
                report.warnings.append(f"{profile.content_type} → {page.label}: {unavailable}. Storing the content "
                                       "for review without sending it.")
            else:
                report.warnings.append(f"{profile.content_type} → {page.label}: {reason}.")
                if not dry_run:
                    return  # nothing could be delivered: do not spend AI budget on it
        generator: BaseGenerator = self.registry.plugins.get("generator", profile.generator)(context)

        products = self.db.unpublished_products(
            category=category.category_id, language=report.language, account_platform=profile.platform,
            content_format=profile.content_type, min_rating=category.min_rating, min_orders=category.min_orders,
            limit=limit, include_handled=force,
        )
        route.candidates = len(products)
        if not products:
            report.warnings.append(
                f"{profile.content_type}: no harvested '{category.category_id}' product is waiting for "
                f"{account.account_handle} — harvest first: python main.py --harvest --category {category.category_id}"
            )
            return

        for product in products:
            try:
                outcome = self._process(report, account, profile, page, generator, publisher, product, mode, dry_run,
                                        unavailable)
            except BudgetExceededError:
                raise  # guardrail: stop the whole run, never swallow
            except Exception as exc:
                logger.error(
                    "Pipeline '%s' (%s): product %s failed and was skipped: %s: %s",
                    report.pipeline_id, profile.content_type, product.ref, type(exc).__name__, exc,
                    exc_info=not isinstance(exc, TarshishError),
                )
                outcome = ItemOutcome(
                    content_type=profile.content_type, external_id=product.external_id, title=product.title,
                    status="failed", target=page.label, configured_model=profile.llm_model,
                    detail=redact(f"{type(exc).__name__}: {exc}"),
                )
            report.items.append(outcome)

    def _process(
        self, report: RunReport, account: SocialAccount, profile: ContentProfile, page: SocialPage,
        generator: BaseGenerator, publisher: BasePublisher | None, product: Product, mode: str, dry_run: bool,
        unavailable: str | None = None,
    ) -> ItemOutcome:
        content = generator.generate(product, report.language, profile.prompt_template)
        content.content_format = profile.content_type
        outcome = ItemOutcome(
            content_type=profile.content_type, external_id=product.external_id, title=product.title,
            status="preview", target=page.label, configured_model=profile.llm_model, model_used=content.model_used,
            cost_usd=content.generation_cost_usd, caption=content.caption, hashtags=list(content.hashtags),
        )
        if dry_run:
            outcome.detail = "ready" if publisher is not None else "a live run would skip this account (see warnings)"
            self._preview(report, profile, page, content)
            return outcome

        # Record spend first: once the provider has charged us, the ledger must know.
        if content.generation_cost_usd > 0 or content.tokens_input or content.tokens_output:
            self.db.record_ai_usage(
                pipeline_id=report.pipeline_id, product_ref=product.ref, model_used=content.model_used,
                tokens_input=content.tokens_input, tokens_output=content.tokens_output,
                cost_usd=content.generation_cost_usd,
            )
        content = self._store_media(self.registry.storage(), report.pipeline_id, content)
        product_id = self.db.product_id(product.source, product.external_id)
        if product_id is None:  # harvested rows are the only input, but a concurrent delete is possible
            product_id = self.db.upsert_product(product)
        if publisher is None:  # store_unpublishable: keep it for review, never send it
            status, detail = self._store_unsent(report, account, page, product_id, content,
                                                unavailable or "the publisher could not be built")
        else:
            status, detail = self._dispatch(report, account, page, publisher, product_id, content, mode)
        outcome.status, outcome.detail = status, detail
        return outcome

    def _dispatch(
        self, report: RunReport, account: SocialAccount, page: SocialPage, publisher: BasePublisher,
        product_id: int, content: GeneratedContent, mode: str,
    ) -> tuple[str, str]:
        """Publish to (or queue for) the account. Isolated: never raises."""
        try:
            common = dict(
                pipeline_id=report.pipeline_id, product_id=product_id, category=report.category,
                language=report.language, account_platform=account.platform, platform=page.platform,
                target_page=page.page_name, target_json=page.model_dump_json(exclude_none=True), content=content,
                cost_share_usd=content.generation_cost_usd,
            )
            if mode == "queued":
                slot = self.scheduler.slot_for(utcnow(), self.db.last_activity_at(page.platform, page.page_name))
                self.db.insert_publication(status=PublicationStatus.QUEUED, scheduled_for=slot, **common)
                local = slot.astimezone(self.scheduler.tz).strftime("%Y-%m-%d %H:%M %Z")
                return "queued", f"scheduled for {local}"
            publication_id = self.db.insert_publication(status=PublicationStatus.PENDING, **common)
            receipt = deliver_publication(self.db, publication_id, publisher, content)
            if receipt.success:
                return "published", f"post id {receipt.platform_post_id}"
            return "failed", receipt.error_message or "unknown error"
        except Exception as exc:
            logger.exception("Pipeline '%s': target %s failed.", report.pipeline_id, page.label)
            return "failed", redact(f"{type(exc).__name__}: {exc}")

    def _store_unsent(
        self, report: RunReport, account: SocialAccount, page: SocialPage, product_id: int,
        content: GeneratedContent, reason: str,
    ) -> tuple[str, str]:
        """Keep content for an account that cannot publish yet: ``FAILED`` + a :data:`NOT_SENT` note.

        ``FAILED`` rows are never picked up by the worker, show up in Content Review, and are published
        there (or re-queued with ``--retry-failed``) once the credentials exist.
        """
        publication_id = self.db.insert_publication(
            pipeline_id=report.pipeline_id, product_id=product_id, category=report.category, language=report.language,
            account_platform=account.platform, platform=page.platform, target_page=page.page_name,
            target_json=page.model_dump_json(exclude_none=True), content=content, status=PublicationStatus.FAILED,
            cost_share_usd=content.generation_cost_usd,
        )
        self.db.mark_failed(publication_id, redact(f"{NOT_SENT} {reason}"))
        return "stored", f"not sent ({reason}); stored for review as #{publication_id}"

    # -- dry-run --------------------------------------------------------------------------- #
    def _preview(self, report: RunReport, profile: ContentProfile, page: SocialPage, content: GeneratedContent) -> None:
        """Render the content through the dry-run publisher instead of the real account."""
        name = self.settings.dry_run_publisher
        try:
            if not self.registry.plugins.has("publisher", name):
                raise PipelineError("no dry-run publisher available")
            target = PublishTarget(
                platform=name, page_name="dry-run preview",
                options={"dry_run": True, "pipeline_id": report.pipeline_id, "mode": report.mode,
                         "content_type": profile.publish_format, "intended_targets": [
                             f"{page.label}  [{profile.content_type} · {profile.generator} · {profile.llm_model}]"]},
            )
            context = self.registry.context_for(pipeline_id=report.pipeline_id, dry_run=True)
            self.registry.plugins.get("publisher", name)(context, target).publish(content)
        except Exception as exc:
            logger.debug("Dry-run publisher unavailable (%s); using plain output.", exc)
            print(f"\n[DRY-RUN] {content.product.title}\n{content.caption}\n")

    # -- storage --------------------------------------------------------------------- #
    def _store_media(self, storage: BaseStorage, pipeline_id: str, content: GeneratedContent) -> GeneratedContent:
        """Hand generated media to the storage backend and point the content at the stored copy."""
        if not content.media_local_path:
            return content
        source = Path(content.media_local_path)
        if not source.is_file():
            logger.warning("Generated media %s does not exist; publishing without it.", source)
            content.media_local_path = None
            return content
        key = (f"{pipeline_id}/{content.content_format or 'content'}/{utcnow():%Y%m%d}/"
               f"{content.product.canonical_hash[:16]}{source.suffix.lower()}")
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
