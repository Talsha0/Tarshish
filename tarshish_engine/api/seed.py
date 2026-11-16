"""Load products into the storefront without running a content pipeline.

    python main.py --seed-storefront        (or: python -m api.seed)

Writes the 14 mock products (two stores, three categories) into ``products``
together with their storefront listing details. It is an upsert keyed on
``(source, external_id)``: running it again refreshes the same rows, and products
a pipeline already stored keep their ids, publications and click history.

    python main.py --seed-temu --category gadgets --keyword "smart gadgets" --limit 10

Loads real Temu products through the ``temu_rapidapi`` source (see ``seed_temu``).
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


def seed_storefront(db: Database) -> int:
    """Upsert every demo product; return how many were written."""
    count = 0
    for product, listing in seed_entries():
        db.upsert_product(product, listing)
        count += 1
    return count


@dataclass
class TemuSeedReport:
    """Outcome of one ``--seed-temu`` run (exactly one search)."""

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


def _standard(engine: "Engine", name: str, default: float) -> float:
    try:
        return float(engine.settings.get_env(name) or default)
    except ValueError:
        return default


def seed_temu(
    engine: "Engine", *, category: str, keyword: str | None = None, limit: int = 10, job_id: str | None = None,
) -> TemuSeedReport:
    """Load real Temu products into the storefront with one search and no LLM call.

        python main.py --seed-temu --category gadgets --keyword "smart gadgets" --limit 10

    The batch is filtered against the storefront's listing standard, ranked by rating and
    sales, and the best ``limit`` products that SQLite does not hold yet are inserted
    under ``category``. Products already stored (same store id, or the same item from
    another source) are reported as duplicates and left as they are. ``job_id`` collects
    the results of a search that is already running or finished instead of starting one.
    """
    source: Any = engine.plugins.get("source", TEMU_SOURCE)(PluginContext(settings=engine.settings))
    min_rating = _standard(engine, "STOREFRONT_MIN_RATING", 4.5)
    min_orders = int(_standard(engine, "STOREFRONT_MIN_ORDERS", 200))
    batch = source.fetch_batch(
        category, min_rating, min_orders, keyword=keyword, max_items=limit, job_id=job_id
    )

    report = TemuSeedReport(
        category=category, keyword=batch.keyword, received=batch.received, min_rating=min_rating,
        min_orders=min_orders, rejected=list(batch.rejected), notice=batch.notice,
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


def main() -> int:
    from core.config import default_env_file, load_settings

    settings = load_settings(default_env_file())
    db = Database(settings.database_path)
    db.initialize()
    print(f"Seeded {seed_storefront(db)} demo product(s) into {settings.database_path.name}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
