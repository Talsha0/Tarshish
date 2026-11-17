"""Proof-of-concept product source: a small, realistic AliExpress-style catalogue.

It behaves like a real source plugin (filters, ordering, limit, retry wrapper)
without any network access, so harvesting and publishing can be exercised offline.
Swap it for a real implementation by adding e.g. ``plugins/sources/aliexpress.py``
and harvesting with ``--source aliexpress``.

The products live in ``_mock_catalogue.py`` (shared with ``temu_mock`` and the
storefront seeder). A category whose id names a shelf — sports,
gadgets, home — gets that shelf; ``running_and_sports`` returns the three
sports/running products.
"""

from __future__ import annotations

from core.interfaces import BaseSource
from core.models import Product
from core.resilience import get_logger, with_retry
from plugins.sources._mock_catalogue import product_fields

logger = get_logger("plugins.aliexpress_mock")


class AliExpressMockSource(BaseSource):
    """Serves the AliExpress slice of the offline catalogue."""

    @property
    def source_name(self) -> str:
        return "aliexpress_mock"

    @with_retry(max_attempts=3, base_delay=2.0)
    def fetch_products(self, category: str, min_rating: float, min_orders: int, limit: int) -> list[Product]:
        # A real source would call its API here; the retry decorator already guards the call.
        shelf = product_fields(self.source_name, category)
        products = [
            Product(source=self.source_name, category=category, currency="ILS", **item)
            for item in shelf
            if item["rating"] >= min_rating and item["orders_count"] >= min_orders
        ]
        products.sort(key=lambda product: product.orders_count, reverse=True)
        logger.debug("Mock catalogue: %d of %d products pass the filters.", len(products), len(shelf))
        return products[: max(limit, 0)]
