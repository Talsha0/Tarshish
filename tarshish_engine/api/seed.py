"""Load products into the storefront without running a content pipeline.

    python main.py --seed-storefront        (or: python -m api.seed)

Writes the 14 mock products (two stores, three categories) into ``products``
together with their storefront listing details. It is an upsert keyed on
``(source, external_id)``: running it again refreshes the same rows, and products
a pipeline already stored keep their ids, publications and click history.

    python main.py --seed-temu --category gadgets --keyword "smart gadgets" --limit 10

Loads real Temu products through the ``temu_rapidapi`` source and its provider chain
(see ``seed_temu``).

    python main.py --seed-aliexpress --category gadgets --keyword "smart watch" --limit 10

The same for AliExpress through the ``aliexpress_rapidapi`` failover chain (see ``seed_aliexpress``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from core.database import Database
from core.interfaces import PluginContext
from core.models import Product
from plugins.sources._mock_catalogue import seed_entries

if TYPE_CHECKING:
    from core import Engine

TEMU_SOURCE = "temu_rapidapi"
ALIEXPRESS_SOURCE = "aliexpress_rapidapi"
SEED_SOURCES = (TEMU_SOURCE, ALIEXPRESS_SOURCE)


def seed_storefront(db: Database) -> int:
    """Upsert every demo product; return how many were written."""
    count = 0
    for product, listing in seed_entries():
        db.upsert_product(product, listing)
        count += 1
    return count


@dataclass
class SeedReport:
    """Outcome of one ``--seed-temu`` / ``--seed-aliexpress`` run (one search per provider asked)."""

    category: str
    keyword: str
    received: int = 0
    min_rating: float = 0.0
    min_orders: int = 0
    inserted: list[Product] = field(default_factory=list)
    duplicates: list[Product] = field(default_factory=list)      # already in SQLite: left untouched
    over_limit: list[Product] = field(default_factory=list)      # usable, but beyond --limit
    rejected: list[tuple[str, str]] = field(default_factory=list)  # (item, reason)
    notice: str | None = None
    provider: str | None = None                                  # the provider that delivered; None = all failed
    attempts: list[Any] = field(default_factory=list)            # ProviderAttempt per provider asked, in order
    source: str = TEMU_SOURCE                                    # the source plugin that ran the chain


TemuSeedReport = SeedReport  # the original name


def _standard(engine: "Engine", name: str, default: float) -> float:
    try:
        return float(engine.settings.get_env(name) or default)
    except ValueError:
        return default


def seed_source(
    engine: "Engine", source_plugin: str, *, category: str, keyword: str | None = None, limit: int = 10,
    job_id: str | None = None,
) -> SeedReport:
    """Load real products of one RapidAPI chain source into the storefront: no LLM call.

    The source walks its provider chain and stops at the first provider that delivers usable
    products. That batch is filtered against the storefront's listing standard, ranked by rating and
    sales, and the best ``limit`` products that SQLite does not hold yet are inserted
    under ``category``. Products already stored (same store id, or the same item from
    another source) are reported as duplicates and left as they are. ``job_id`` (Temu only)
    collects the results of a search that is already running or finished instead of starting one.
    """
    if source_plugin not in SEED_SOURCES:
        raise ValueError(f"cannot seed from '{source_plugin}' (choose one of: {', '.join(SEED_SOURCES)})")
    source: Any = engine.plugins.get("source", source_plugin)(PluginContext(settings=engine.settings))
    min_rating = _standard(engine, "STOREFRONT_MIN_RATING", 4.5)
    min_orders = int(_standard(engine, "STOREFRONT_MIN_ORDERS", 200))
    extra = {"job_id": job_id} if job_id is not None else {}
    batch = source.fetch_batch(category, min_rating, min_orders, keyword=keyword, max_items=limit, **extra)

    report = SeedReport(
        category=category, keyword=batch.keyword, received=batch.received, min_rating=min_rating,
        min_orders=min_orders, rejected=list(batch.rejected), notice=batch.notice,
        provider=batch.provider, attempts=list(batch.attempts), source=source_plugin,
    )
    known = source.stored_refs(batch.products)
    for product in batch.products:
        if product.ref in known:
            report.duplicates.append(product)
        elif len(report.inserted) >= limit:
            report.over_limit.append(product)
        else:
            engine.db.upsert_product(product, {"category": category})
            report.inserted.append(product)
    return report


def seed_temu(
    engine: "Engine", *, category: str, keyword: str | None = None, limit: int = 10, job_id: str | None = None,
) -> SeedReport:
    """Load real Temu products into the storefront: no LLM call, one search per provider asked.

        python main.py --seed-temu --category gadgets --keyword "smart gadgets" --limit 10
    """
    return seed_source(engine, TEMU_SOURCE, category=category, keyword=keyword, limit=limit, job_id=job_id)


def seed_aliexpress(engine: "Engine", *, category: str, keyword: str | None = None, limit: int = 10) -> SeedReport:
    """Load real AliExpress products into the storefront through the RapidAPI failover chain: no LLM call.

        python main.py --seed-aliexpress --category gadgets --keyword "smart watch" --limit 10
    """
    return seed_source(engine, ALIEXPRESS_SOURCE, category=category, keyword=keyword, limit=limit)


def main() -> int:
    from core.config import default_env_file, load_settings

    settings = load_settings(default_env_file())
    db = Database(settings.database_path)
    db.initialize()
    print(f"Seeded {seed_storefront(db)} demo product(s) into {settings.database_path.name}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
