"""Stage 1 — language-agnostic product harvesting.

    python main.py --harvest                                  # every enabled category, every store it has rules for
    python main.py --harvest --category gadgets               # one category
    python main.py --harvest --source temu_rapidapi           # one store, every category
    python main.py --harvest --source temu_mock --category gadgets   # offline catalogue, no API quota

A harvest runs once per ``(category, source)`` — never once per language. The search
terms are the category's English rules for that store (``categories.temu_search_json`` /
``aliexpress_search_json``), handed to the source plugin as ``options["search_config"]``.
Every usable product is upserted into the central ``products`` table under the category
id, from where every language's publishing pipeline (Stage 2) reads it.

No LLM call is made here: harvesting costs API quota only, never AI budget.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Sequence

from core.config import CategoryConfig, store_of
from core.models import Product
from core.resilience import PipelineError, TarshishError, get_logger, redact

if TYPE_CHECKING:
    from core.config import Settings
    from core.database import Database
    from core.registry import ConfigRegistry, PluginRegistry

logger = get_logger("harvester")


@dataclass
class HarvestReport:
    """Outcome of harvesting one ``(category, source)`` pair."""

    category: str
    keyword: str
    source: str = ""
    received: int = 0
    min_rating: float = 0.0
    min_orders: int = 0
    inserted: list[Product] = field(default_factory=list)        # new rows in ``products``
    duplicates: list[Product] = field(default_factory=list)      # already stored (same-store rows refreshed)
    over_limit: list[Product] = field(default_factory=list)      # usable, but beyond the limit
    rejected: list[tuple[str, str]] = field(default_factory=list)  # (item, reason)
    notice: str | None = None
    provider: str | None = None                                  # who delivered; None = nothing did
    attempts: list[Any] = field(default_factory=list)            # ProviderAttempt per provider asked (RapidAPI)
    dry_run: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.provider is not None


class Harvester:
    """Fetches products per ``(category, source)`` and upserts them into ``products``."""

    def __init__(self, settings: "Settings", db: "Database", plugins: "PluginRegistry", registry: "ConfigRegistry") -> None:
        self.settings = settings
        self.db = db
        self.plugins = plugins
        self.registry = registry

    # -- planning -------------------------------------------------------------- #
    def plan(self, category: str | None = None, source: str | None = None) -> list[tuple[CategoryConfig, str]]:
        """The ``(category, source)`` pairs a harvest run covers."""
        if source is not None and not self.plugins.has("source", source):
            self.plugins.get("source", source)  # raises PluginError listing the available sources
        categories = [self.registry.get_category(category)] if category else self.registry.list_categories(enabled_only=True)
        pairs: list[tuple[CategoryConfig, str]] = []
        for config in categories:
            if source is not None:
                pairs.append((config, source))
            else:
                pairs.extend((config, plugin) for plugin in config.default_sources())
        return pairs

    # -- running ---------------------------------------------------------------- #
    def harvest(
        self, category: str | None = None, source: str | None = None, *, limit: int | None = None,
        dry_run: bool = False,
    ) -> list[HarvestReport]:
        """Harvest every planned pair; one failing pair never stops the others."""
        pairs = self.plan(category, source)
        if not pairs:
            raise PipelineError(
                "Nothing to harvest: no enabled category has search rules for a store"
                + (f" (category '{category}')" if category else "")
                + ". Add temu/aliexpress search rules in the dashboard or config/categories.yaml."
            )
        return [self.harvest_one(config, plugin, limit=limit, dry_run=dry_run) for config, plugin in pairs]

    def harvest_one(
        self, category: CategoryConfig, source_plugin: str, *, limit: int | None = None, keyword: str | None = None,
        job_id: str | None = None, dry_run: bool = False,
    ) -> HarvestReport:
        """Harvest one ``(category, source)`` pair. Never raises for a fetch problem: it is reported."""
        limit = limit or self.settings.harvest_limit
        search = category.search_for(source_plugin)
        report = HarvestReport(
            category=category.category_id, keyword=keyword or (search or {}).get("keyword") or category.category_id,
            source=source_plugin, min_rating=category.min_rating, min_orders=category.min_orders, dry_run=dry_run,
        )
        if search is None and keyword is None:
            report.error = (
                f"category '{category.category_id}' has no search rules for the {store_of(source_plugin)} store "
                f"({source_plugin}); add them in the dashboard (Categories tab) or config/categories.yaml"
            )
            logger.warning("Harvest skipped: %s.", report.error)
            return report
        search = {**(search or {}), **({"keyword": keyword} if keyword else {})}

        context = self.registry.context_for(options={"search_config": search}, dry_run=dry_run)
        try:
            source = self.plugins.get("source", source_plugin)(context)
            products = self._fetch(source, category, report, limit, job_id, dry_run)
        except TarshishError as exc:
            report.error = redact(str(exc))
            logger.error("Harvest %s ➔ %s failed: %s", source_plugin, category.category_id, report.error)
            return report
        except Exception as exc:  # an untrusted plugin must not take the harvest run down
            report.error = redact(f"{type(exc).__name__}: {exc}")
            logger.exception("Harvest %s ➔ %s failed.", source_plugin, category.category_id)
            return report

        self._store(products, category, report, limit, dry_run)
        logger.info(
            "Harvest %s ➔ %s%s: received %d, inserted %d, already stored %d, beyond limit %d, rejected %d.",
            source_plugin, category.category_id, " (dry-run)" if dry_run else "", report.received,
            len(report.inserted), len(report.duplicates), len(report.over_limit), len(report.rejected),
        )
        return report

    def _fetch(
        self, source: Any, category: CategoryConfig, report: HarvestReport, limit: int, job_id: str | None,
        dry_run: bool,
    ) -> list[Product]:
        min_rating, min_orders = category.min_rating, category.min_orders
        if hasattr(source, "fetch_batch") and not dry_run:
            # RapidAPI chain sources: one walk down the provider chain, with per-provider diagnostics.
            extra = {"job_id": job_id} if job_id is not None else {}
            batch = source.fetch_batch(category.category_id, min_rating, min_orders, max_items=limit, **extra)
            report.keyword, report.received, report.notice = batch.keyword, batch.received, batch.notice
            report.provider, report.attempts = batch.provider, list(batch.attempts)
            report.rejected = list(batch.rejected)
            products = list(batch.products)
        else:
            # Any other source (mock catalogues, a dry-run preview): the plain BaseSource contract.
            products = source.fetch_products(category.category_id, min_rating, min_orders, limit * 3)
            report.received = len(products)
            report.provider = source.source_name if products else None
        # Plugins are untrusted: re-apply the category's standard and drop in-batch duplicates.
        unique: dict[str, Product] = {}
        for product in products:
            if product.rating < min_rating or product.orders_count < min_orders:
                report.rejected.append((product.ref, f"below the category standard (rating ≥ {min_rating:g}, "
                                                     f"orders ≥ {min_orders:,})"))
                continue
            if product.category != category.category_id:
                product = product.model_copy(update={"category": category.category_id})
            unique.setdefault(product.canonical_hash, product)
        return list(unique.values())

    def _store(
        self, products: Sequence[Product], category: CategoryConfig, report: HarvestReport, limit: int, dry_run: bool
    ) -> None:
        same_store, other_store = self._known(products)
        for product in products:
            if product.ref in same_store:
                report.duplicates.append(product)
                if not dry_run:  # refresh price, rating and stock of the row we already hold
                    self.db.upsert_product(product, {"category": category.category_id})
            elif product.canonical_hash in other_store:
                report.duplicates.append(product)  # the same item from another store: keep one listing
            elif len(report.inserted) >= limit:
                report.over_limit.append(product)
            else:
                if not dry_run:
                    self.db.upsert_product(product, {"category": category.category_id})
                report.inserted.append(product)

    def _known(self, products: Sequence[Product]) -> tuple[set[str], set[str]]:
        """``(refs stored under the same store id, canonical hashes stored by another store)``."""
        if not products:
            return set(), set()
        refs = {(p.source, p.external_id) for p in products}
        hashes = list({p.canonical_hash for p in products})
        rows = self.db.query(
            f"SELECT source, external_id, canonical_hash FROM products "
            f"WHERE canonical_hash IN ({', '.join('?' for _ in hashes)}) "
            f"OR external_id IN ({', '.join('?' for _ in refs)})",
            [*hashes, *(external for _, external in refs)],
        )
        same = {f"{row['source']}:{row['external_id']}" for row in rows if (row["source"], row["external_id"]) in refs}
        other = {row["canonical_hash"] for row in rows if (row["source"], row["external_id"]) not in refs}
        return same, other

