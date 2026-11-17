"""Load products into the storefront without running a content pipeline.

    python main.py --seed-storefront        (or: python -m api.seed)

Writes the 14 mock products (two stores, three categories) into ``products``
together with their storefront listing details. It is an upsert keyed on
``(source, external_id)``: running it again refreshes the same rows, and products
a pipeline already stored keep their ids, publications and click history.

    python main.py --seed-temu --category gadgets --keyword "smart gadgets" --limit 10

Loads real Temu products through the ``temu_rapidapi`` source and its provider chain
(see ``seed_temu``). Both are ad-hoc aliases of the Stage 1 harvester —
``python main.py --harvest --source temu_rapidapi --category gadgets`` — that let you
override the category's keyword for one run.

    python main.py --seed-aliexpress --category gadgets --keyword "smart watch" --limit 10

The same for AliExpress through the ``aliexpress_rapidapi`` failover chain (see ``seed_aliexpress``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.config import CategoryConfig
from core.database import Database
from core.harvester import HarvestReport
from core.models import category_slug
from core.resilience import PipelineError
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


SeedReport = HarvestReport      # the names used before the harvester existed
TemuSeedReport = HarvestReport


def seed_source(
    engine: "Engine", source_plugin: str, *, category: str, keyword: str | None = None, limit: int = 10,
    job_id: str | None = None,
) -> HarvestReport:
    """Harvest one RapidAPI chain source into ``products`` with an ad-hoc keyword: no LLM call.

    A thin alias of the Stage 1 harvester (``python main.py --harvest``). The category's stored
    search rules are used — with ``keyword`` replacing their keyword when given; a category that
    is not configured yet is harvested with its name as the keyword. Products already stored are
    reported as duplicates; ``job_id`` (Temu only) collects an earlier background search.
    """
    if source_plugin not in SEED_SOURCES:
        raise ValueError(f"cannot seed from '{source_plugin}' (choose one of: {', '.join(SEED_SOURCES)})")
    try:
        config = engine.registry.get_category(category_slug(category))
    except PipelineError:
        config = CategoryConfig(category_id=category, display_name_en=category)
    standard = {
        "min_rating": _standard(engine, "STOREFRONT_MIN_RATING", 4.5),
        "min_orders": int(_standard(engine, "STOREFRONT_MIN_ORDERS", 200)),
    }
    config = config.model_copy(update=standard)
    return engine.harvester.harvest_one(
        config, source_plugin, limit=limit, keyword=keyword or (None if config.search_for(source_plugin) else category),
        job_id=job_id,
    )


def _standard(engine: "Engine", name: str, default: float) -> float:
    try:
        return float(engine.settings.get_env(name) or default)
    except ValueError:
        return default


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
