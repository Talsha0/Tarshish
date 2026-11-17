"""Proof-of-concept product source: a small, realistic Temu-style catalogue.

The offline twin of ``aliexpress_mock``: same contract, no network access, its
own slice of ``_mock_catalogue.py``. It exists so that pipelines, the storefront
and the dashboard can be exercised with two stores before a real Temu feed is
written (``plugins/sources/temu.py``).
"""

from __future__ import annotations

from core.interfaces import BaseSource
from core.models import Product
from core.resilience import get_logger, with_retry
from plugins.sources._mock_catalogue import product_fields

logger = get_logger("plugins.temu_mock")


class TemuMockSource(BaseSource):
    """Serves the Temu slice of the offline catalogue."""

    @property
    def source_name(self) -> str:
        return "temu_mock"

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
