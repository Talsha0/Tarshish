"""The contract every RapidAPI Temu provider adapter implements, plus shared mapping tools.

An adapter knows one API's request shape and response shape and nothing else:
no HTTP, no retries, no API key, no quality filter. The failover loop in
``plugins/sources/temu_rapidapi.py`` does all of that and only asks the adapter

* ``build_request(keyword, max_items, filters)`` -> ``(url, headers, params)``
* ``parse_response(payload)`` -> a list of normalised items

A normalised item always has these keys::

    goods_id, title, price, rating, sales_volume, raw_url, image_urls, category_hint

and may add ``original_price`` and ``currency`` when the API reports them.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar, Mapping, Sequence
from urllib.parse import quote

from core.resilience import TarshishError
from plugins.sources._rapidapi_common import dig, first, image_urls, number, render_params  # noqa: F401 (re-exported)
from plugins.sources.temu_providers.config import ProviderConfig

NORMALIZED_FIELDS = (
    "goods_id", "title", "price", "rating", "sales_volume", "raw_url", "image_urls", "category_hint",
)
TEMU_ORIGIN = "https://www.temu.com"
JOB_ID_RE = re.compile(r"[A-Za-z0-9_\-]{4,64}")

# Field names seen across Temu scraper APIs; an adapter narrows or extends these.
COMMON_FIELDS: Mapping[str, tuple[str, ...]] = {
    "goods_id": ("goodsId", "goods_id", "id", "productId", "product_id", "itemId", "item_id"),
    "title": ("title", "name", "goodsName", "goods_name", "productName"),
    "price": ("price", "salePrice", "sale_price", "priceInfo.price", "price_info.price", "price_text"),
    "original_price": ("originalPrice", "original_price", "list_price", "marketPrice", "market_price"),
    "currency": ("currency", "priceInfo.currency", "price_info.currency"),
    "rating": ("rating", "goodsScore", "goods_score", "score", "comment.goods_score", "reviewRating"),
    "sales_volume": (
        "salesVolume", "sales_volume", "sold", "salesNum", "sales_num", "salesText", "sold_text", "sales_tip",
    ),
    "raw_url": ("url", "link", "goodsUrl", "goods_url", "productUrl", "product_url", "link_url", "seo_link_url"),
    "image_urls": (
        "imageUrl", "image", "image_url", "thumbnail", "thumbUrl", "thumb_url", "hd_thumb_url",
        "imageUrls", "images",
    ),
    "category_hint": (
        "l2Category", "category", "categoryName", "category_name", "l1Category", "category_id", "cat_id",
    ),
}


class TemuProviderError(TarshishError):
    """A reply the adapter cannot use (failed job, malformed payload); the chain moves on."""


@dataclass(frozen=True)
class PendingJob:
    """A search that runs in the background: poll ``url`` again after ``poll_after`` seconds."""

    job_id: str
    url: str
    poll_after: float = 10.0


# --------------------------------------------------------------------------- #
# Mapping tools (the generic ones live in plugins/sources/_rapidapi_common.py)
# --------------------------------------------------------------------------- #
def product_url(value: Any, goods_id: str) -> str:
    """An absolute product URL; built from the goods id when the API gives none."""
    url = str(value or "").strip()
    if url.startswith("//"):
        url = f"https:{url}"
    elif url.startswith("/"):
        url = f"{TEMU_ORIGIN}{url}"
    elif url and not re.match(r"https?://", url, re.I):
        url = f"{TEMU_ORIGIN}/{url}"
    if not url and goods_id:
        url = f"{TEMU_ORIGIN}/goods.html?goods_id={quote(goods_id, safe='')}"
    return url


def normalize_item(raw: Any, fields: Mapping[str, Sequence[str]], *, price_divisor: float = 1.0) -> dict[str, Any]:
    """One raw API item as a normalised dict. Missing values stay empty: the caller rejects them."""
    item = raw if isinstance(raw, Mapping) else {}

    def text(name: str) -> str:
        value = first(item, fields.get(name, ()))
        return "" if value is None or isinstance(value, (dict, list)) else str(value).strip()

    def amount(name: str) -> float | None:
        value = number(first(item, fields.get(name, ())))
        return None if value is None else value / price_divisor

    goods_id = text("goods_id")
    currency = text("currency")
    return {
        "goods_id": goods_id,
        "title": text("title"),
        "price": amount("price"),
        "rating": number(first(item, fields.get("rating", ()))) or 0.0,
        "sales_volume": round(number(first(item, fields.get("sales_volume", ()))) or 0),
        "raw_url": product_url(first(item, fields.get("raw_url", ())), goods_id),
        "image_urls": image_urls(item, fields.get("image_urls", ())),
        "category_hint": text("category_hint") or None,
        "original_price": amount("original_price"),
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
        "l1_category": filters.get("l1_category"),
        "l2_category": filters.get("l2_category"),
        "min_rating": min_rating or None,
        # Whole stars, capped at 4: a safe server-side pre-filter; the exact threshold is applied locally.
        "min_stars": min(4, int(min_rating)) if min_rating >= 1 else None,
        "min_orders": int(filters.get("min_orders") or 0) or None,
    }


# --------------------------------------------------------------------------- #
# Contract
# --------------------------------------------------------------------------- #
class BaseTemuProvider(ABC):
    """One RapidAPI Temu endpoint: how to ask it, and how to read its answer."""

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

        ``filters`` may hold ``price_min``, ``price_max``, ``l1_category``, ``l2_category``,
        ``min_rating`` and ``min_orders``; pass on what the API supports and ignore the rest.
        """

    @abstractmethod
    def parse_response(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """The payload's items as normalised dicts (see ``NORMALIZED_FIELDS``); ``[]`` when there are none."""

    # -- optional hooks ------------------------------------------------------------ #
    def pending_job(self, payload: dict[str, Any], previous: PendingJob | None = None) -> PendingJob | None:
        """For APIs that search in the background: the job to poll, or ``None`` when ``payload`` is final.

        Raise :class:`TemuProviderError` when the job failed or the payload is neither.
        """
        return None

    def job_request(self, job_id: str) -> tuple[str, dict[str, str], dict[str, Any]] | None:
        """The request that collects an earlier job by id; ``None`` when the API has no jobs."""
        return None

    def notice(self, payload: dict[str, Any]) -> str | None:
        """A human-readable remark the API attached to the payload (plan limits and the like)."""
        return None

    def searched_keyword(self, payload: dict[str, Any]) -> str | None:
        """The keyword the payload says it searched for (used when collecting a job by id)."""
        return None


class MappedTemuProvider(BaseTemuProvider):
    """An adapter described by three tables: request parameters, item paths and field paths."""

    PARAMS: ClassVar[Mapping[str, Any]] = {}
    ITEM_PATHS: ClassVar[tuple[str, ...]] = ()
    FIELDS: ClassVar[Mapping[str, tuple[str, ...]]] = COMMON_FIELDS
    NOTICE_PATHS: ClassVar[tuple[str, ...]] = ("notice",)
    PRICE_DIVISOR: ClassVar[float] = 1.0

    def params_template(self) -> Mapping[str, Any]:
        return self.PARAMS

    def item_paths(self) -> Sequence[str]:
        return self.ITEM_PATHS

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

    def raw_items(self, payload: dict[str, Any]) -> list[Any]:
        """The first non-empty list found at the item paths."""
        for path in self.item_paths():
            value = dig(payload, path)
            if isinstance(value, list) and value:
                return value
        return []

    def parse_response(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        fields, divisor = self.field_paths(), self.price_divisor()
        return [normalize_item(item, fields, price_divisor=divisor) for item in self.raw_items(payload)]

    def notice(self, payload: dict[str, Any]) -> str | None:
        value = first(payload, self.notice_paths())
        return str(value) if isinstance(value, (str, int, float)) and not isinstance(value, bool) else None
