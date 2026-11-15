"""Proof-of-concept product source: a tiny, realistic AliExpress-style catalogue.

It behaves like a real source plugin (filters, ordering, limit, retry wrapper)
without any network access, so the whole pipeline can be exercised offline.
Swap it for a real implementation by adding e.g. ``plugins/sources/aliexpress.py``
and pointing the pipeline's ``source`` at it.
"""

from __future__ import annotations

from core.interfaces import BaseSource
from core.models import Product
from core.resilience import get_logger, with_retry

logger = get_logger("plugins.aliexpress_mock")

# Prices in ILS. Image URLs are placeholders that resolve to real images so that
# photo publishing (e.g. Telegram sendPhoto) works end to end; affiliate links are fake.
_CATALOGUE: tuple[dict, ...] = (
    {
        "external_id": "1005006482913507",
        "title": "Men's Lightweight Running Shoes - Breathable Mesh Cushioned Road Sneakers",
        "price": 189.90,
        "original_price": 329.00,
        "rating": 4.8,
        "orders_count": 3412,
        "raw_url": "https://www.aliexpress.com/item/1005006482913507.html",
        "affiliate_url": "https://s.click.aliexpress.com/e/_mockRunShoes01",
        "image_urls": ["https://placehold.co/1080x1080.jpg?text=Running+Shoes"],
    },
    {
        "external_id": "1005005937421186",
        "title": "5L Running Hydration Vest Backpack with 2 x 500ml Soft Flasks",
        "price": 129.00,
        "original_price": 199.00,
        "rating": 4.7,
        "orders_count": 1876,
        "raw_url": "https://www.aliexpress.com/item/1005005937421186.html",
        "affiliate_url": "https://s.click.aliexpress.com/e/_mockHydraVest02",
        "image_urls": ["https://placehold.co/1080x1080.jpg?text=Hydration+Vest"],
    },
    {
        "external_id": "1005007120854332",
        "title": "GPS Running Smartwatch - AMOLED Display, Heart Rate and SpO2, 5ATM Waterproof",
        "price": 249.00,
        "original_price": 449.00,
        "rating": 4.6,
        "orders_count": 5230,
        "raw_url": "https://www.aliexpress.com/item/1005007120854332.html",
        "affiliate_url": "https://s.click.aliexpress.com/e/_mockGpsWatch03",
        "image_urls": ["https://placehold.co/1080x1080.jpg?text=GPS+Sports+Watch"],
    },
)


class AliExpressMockSource(BaseSource):
    """Returns the same three sports/running products on every call."""

    @property
    def source_name(self) -> str:
        return "aliexpress_mock"

    @with_retry(max_attempts=3, base_delay=2.0)
    def fetch_products(self, niche: str, min_rating: float, min_orders: int, limit: int) -> list[Product]:
        # A real source would call its API here; the retry decorator already guards the call.
        products = [
            Product(source=self.source_name, niche=niche, currency="ILS", **item)
            for item in _CATALOGUE
            if item["rating"] >= min_rating and item["orders_count"] >= min_orders
        ]
        products.sort(key=lambda product: product.orders_count, reverse=True)
        logger.debug("Mock catalogue: %d of %d products pass the filters.", len(products), len(_CATALOGUE))
        return products[: max(limit, 0)]
