"""The contract every RapidAPI AliExpress provider adapter implements, plus AliExpress mapping tools.

An adapter knows one API's request shape and response shape and nothing else: no
HTTP, no retries, no API key, no quality filter. The failover loop in
``plugins/sources/aliexpress_rapidapi.py`` does all of that and only asks the adapter

* ``build_request(keyword, max_items, filters)`` -> ``(url, headers, params)``
* ``parse_response(payload)`` -> a list of normalised items

A normalised item always has these keys::

    goods_id, title, price, original_price, rating, sales_volume, raw_url, image_urls, category_hint

and may add ``currency`` and ``affiliate_url`` (a ready-made promotion link the API
returned, which the source keeps instead of building its own).
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Any, ClassVar, Mapping, Sequence
from urllib.parse import quote, urlsplit, urlunsplit

from core.resilience import TarshishError
from plugins.sources._rapidapi_common import dig, first, image_urls, number, render_params
from plugins.sources.aliexpress_providers.config import ProviderConfig

NORMALIZED_FIELDS = (
    "goods_id", "title", "price", "original_price", "rating", "sales_volume", "raw_url", "image_urls",
    "category_hint",
)
ALIEXPRESS_ORIGIN = "https://www.aliexpress.com"

# Field names seen across AliExpress RapidAPI schemas; an adapter narrows or extends these.
COMMON_FIELDS: Mapping[str, tuple[str, ...]] = {
    "goods_id": ("itemId", "item_id", "productId", "product_id", "id", "goodsId"),
    "title": ("title", "product_title", "productTitle", "name", "subject"),
    "price": (
        "sku.def.promotionPrice", "promotionPrice", "salePrice", "sale_price", "target_sale_price", "price",
        "sku.def.price", "prices.salePrice.minPrice", "price.value",
    ),
    "original_price": (
        "sku.def.price", "originalPrice", "original_price", "target_original_price", "listPrice",
        "prices.originalPrice.minPrice",
    ),
    "currency": ("currency", "target_sale_price_currency", "sale_price_currency", "price.currency"),
    "rating": ("averageStarRate", "averageStar", "rating", "starRating", "evaluate_rate", "evaluateRate", "score"),
    "sales_volume": ("sales", "orders", "lastest_volume", "latest_volume", "sold", "tradeCount", "salesCount"),
    "raw_url": ("itemUrl", "product_detail_url", "productUrl", "product_url", "url", "link", "detailUrl"),
    "affiliate_url": ("promotion_link", "promotionLink", "affiliateLink", "affiliate_link"),
    "image_urls": (
        "image", "product_main_image_url", "imageUrl", "image_url", "mainImage", "images",
        "product_small_image_urls.string", "imageUrls", "thumbnail",
    ),
    "category_hint": (
        "second_level_category_name", "first_level_category_name", "categoryName", "category_name",
        "category", "categoryId", "category_id",
    ),
}


class AliExpressProviderError(TarshishError):
    """A reply the adapter cannot use (an error envelope, malformed payload); the chain moves on."""


# --------------------------------------------------------------------------- #
# AliExpress mapping tools
# --------------------------------------------------------------------------- #
def _absolute(url: str) -> str:
    url = url.strip()
    if url.startswith("//"):
        return f"https:{url}"
    if url.startswith("/"):
        return f"{ALIEXPRESS_ORIGIN}{url}"
    if url and not re.match(r"https?://", url, re.I):
        return f"{ALIEXPRESS_ORIGIN}/{url}"
    return url


def product_url(value: Any, goods_id: str) -> str:
    """The plain product page (no tracking query); built from the item id when the API gives none."""
    url = _absolute(str(value or "")) if isinstance(value, (str, int)) else ""
    if url:
        parts = urlsplit(url)
        url = urlunsplit((parts.scheme or "https", parts.netloc, parts.path, "", ""))
    if not url and goods_id:
        url = f"{ALIEXPRESS_ORIGIN}/item/{quote(goods_id, safe='')}.html"
    return url


def affiliate_link(value: Any) -> str | None:
    """A ready-made promotion link from the API, kept as-is (absolute http(s) only)."""
    if not isinstance(value, str):
        return None
    url = _absolute(value)
    return url if re.match(r"https?://\S+$", url) else None


def star_rating(value: Any) -> float:
    """Stars out of 5. ``"97.5%"`` / ``97.5`` (positive-feedback percent) become ``4.88``."""
    stars = number(value)
    if stars is None:
        return 0.0
    if (isinstance(value, str) and "%" in value) or 5 < stars <= 100:
        stars = stars / 20
    return round(min(max(stars, 0.0), 5.0), 2)


def normalize_item(
    raw: Any, fields: Mapping[str, Sequence[str]], *, price_divisor: float = 1.0, item_root: str | None = None,
) -> dict[str, Any]:
    """One raw API item as a normalised dict. Missing values stay empty: the caller rejects them."""
    item = raw if isinstance(raw, Mapping) else {}
    if item_root:
        nested = dig(item, item_root)
        item = nested if isinstance(nested, Mapping) else item

    def text(name: str) -> str:
        value = first(item, fields.get(name, ()))
        return "" if value is None or isinstance(value, (dict, list)) else str(value).strip()

    def amount(name: str) -> float | None:
        value = number(first(item, fields.get(name, ())))
        return None if value is None else round(value / price_divisor, 2)

    goods_id = text("goods_id")
    currency = text("currency")
    return {
        "goods_id": goods_id,
        "title": text("title"),
        "price": amount("price"),
        "original_price": amount("original_price"),
        "rating": star_rating(first(item, fields.get("rating", ()))),
        "sales_volume": round(number(first(item, fields.get("sales_volume", ()))) or 0),
        "raw_url": product_url(first(item, fields.get("raw_url", ())), goods_id),
        "affiliate_url": affiliate_link(first(item, fields.get("affiliate_url", ()))),
        "image_urls": image_urls(item, fields.get("image_urls", ())),
        "category_hint": text("category_hint") or None,
        "currency": currency.upper() if re.fullmatch(r"[A-Za-z]{3}", currency) else None,
    }


def request_values(keyword: str, max_items: int, filters: Mapping[str, Any]) -> dict[str, Any]:
    """Everything a request template may refer to; ``None`` means "leave the parameter out"."""
    min_rating = float(filters.get("min_rating") or 0)
    return {
        "keyword": keyword,
        "max_items": max_items,
        "page": 1,
        "price_min": filters.get("price_min"),
        "price_max": filters.get("price_max"),
        "sort": filters.get("sort"),
        "category_id": filters.get("category_id"),
        "min_rating": min_rating or None,
        # Whole stars, capped at 4: a safe server-side pre-filter; the exact threshold is applied locally.
        "min_stars": min(4, int(min_rating)) if min_rating >= 1 else None,
        "min_orders": int(filters.get("min_orders") or 0) or None,
    }


# --------------------------------------------------------------------------- #
# Contract
# --------------------------------------------------------------------------- #
class BaseAliExpressProvider(ABC):
    """One RapidAPI AliExpress endpoint: how to ask it, and how to read its answer."""

    def __init__(self, config: ProviderConfig) -> None:
        self.config = config

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def host(self) -> str:
        return self.config.host

    def headers(self) -> dict[str, str]:
        """Request headers without the API key — the failover loop adds ``x-rapidapi-key``."""
        return {"x-rapidapi-host": self.config.host}

    @abstractmethod
    def build_request(
        self, keyword: str, max_items: int, filters: Mapping[str, Any]
    ) -> tuple[str, dict[str, str], dict[str, Any]]:
        """``(url, headers, query parameters)`` of one search.

        ``filters`` may hold ``price_min``, ``price_max``, ``sort``, ``category_id``,
        ``min_rating`` and ``min_orders``; pass on what the API supports and ignore the rest.
        """

    @abstractmethod
    def parse_response(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """The payload's items as normalised dicts (see ``NORMALIZED_FIELDS``); ``[]`` when there are none.

        Raise :class:`AliExpressProviderError` when the payload is an error envelope.
        """

    def notice(self, payload: dict[str, Any]) -> str | None:
        """A human-readable remark the API attached to the payload (plan limits and the like)."""
        return None


class MappedAliExpressProvider(BaseAliExpressProvider):
    """An adapter described by tables: request parameters, item paths and field paths."""

    PARAMS: ClassVar[Mapping[str, Any]] = {}
    ITEM_PATHS: ClassVar[tuple[str, ...]] = ()
    ITEM_ROOT: ClassVar[str | None] = None
    FIELDS: ClassVar[Mapping[str, tuple[str, ...]]] = COMMON_FIELDS
    NOTICE_PATHS: ClassVar[tuple[str, ...]] = ("notice", "message")
    PRICE_DIVISOR: ClassVar[float] = 1.0

    def params_template(self) -> Mapping[str, Any]:
        return self.PARAMS

    def item_paths(self) -> Sequence[str]:
        return self.ITEM_PATHS

    def item_root(self) -> str | None:
        return self.ITEM_ROOT

    def field_paths(self) -> Mapping[str, Sequence[str]]:
        return self.FIELDS

    def notice_paths(self) -> Sequence[str]:
        return self.NOTICE_PATHS

    def price_divisor(self) -> float:
        return self.PRICE_DIVISOR

    def build_request(
        self, keyword: str, max_items: int, filters: Mapping[str, Any]
    ) -> tuple[str, dict[str, str], dict[str, Any]]:
        params = render_params(self.params_template(), request_values(keyword, max_items, filters))
        return self.config.url, self.headers(), params

    def payload_error(self, payload: dict[str, Any]) -> str | None:
        """An error the API wrapped in an HTTP 200 reply; ``None`` when there is none."""
        return None

    def raw_items(self, payload: dict[str, Any]) -> list[Any]:
        """The first non-empty list found at the item paths."""
        for path in self.item_paths():
            value = dig(payload, path)
            if isinstance(value, list) and value:
                return value
        return []

    def parse_response(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        problem = self.payload_error(payload)
        if problem:
            raise AliExpressProviderError(problem)
        fields, divisor, root = self.field_paths(), self.price_divisor(), self.item_root()
        return [normalize_item(item, fields, price_divisor=divisor, item_root=root) for item in self.raw_items(payload)]

    def notice(self, payload: dict[str, Any]) -> str | None:
        value = first(payload, self.notice_paths())
        return str(value) if isinstance(value, (str, int, float)) and not isinstance(value, bool) else None
