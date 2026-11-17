"""Concrete adapters for RapidAPI AliExpress schemas.

Three adapters cover fixed schemas; ``GenericConfigurableAliExpressAdapter`` reads its
request parameters and response paths from ``config/aliexpress_providers.yaml``, so a
new RapidAPI AliExpress endpoint is one YAML entry, not a Python change.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from core.resilience import redact
from plugins.sources._rapidapi_common import dig
from plugins.sources.aliexpress_providers.base import (
    COMMON_FIELDS,
    BaseAliExpressProvider,
    MappedAliExpressProvider,
)


class AliExpressDataHubAdapter(MappedAliExpressProvider):
    """"AliExpress DataHub" — ``GET /item_search``; items under ``result.resultList[].item``.

    Errors arrive as HTTP 200 with ``result.status.data == "error"`` (and a code/message),
    which is reported as a provider error so the chain moves on.
    """

    PARAMS = {
        "q": "{keyword}", "query": "{keyword}", "page": "{page}", "sort": "{sort}",
        "startPrice": "{price_min}", "endPrice": "{price_max}",
    }
    ITEM_PATHS = ("result.resultList",)
    ITEM_ROOT = "item"
    FIELDS = {
        **COMMON_FIELDS,
        "goods_id": ("itemId",),
        "price": ("sku.def.promotionPrice", "sku.def.price", "promotionPrice", "price"),
        "original_price": ("sku.def.price", "originalPrice"),
        "rating": ("averageStarRate", "averageStar"),
        "sales_volume": ("sales", "orders"),
        "raw_url": ("itemUrl",),
        "image_urls": ("image", "images"),
    }
    NOTICE_PATHS = ("result.status.msg", "message")

    def payload_error(self, payload: dict[str, Any]) -> str | None:
        status = dig(payload, "result.status")
        if not isinstance(status, Mapping) or str(status.get("data") or "").lower() != "error":
            return None
        message = status.get("msg")
        if isinstance(message, Mapping):
            message = "; ".join(f"{key}: {value}" for key, value in message.items())
        return redact(f"the API reported error {status.get('code', '?')}: {message or 'no details'}")[:240]


class AliExpressTrueApiAdapter(MappedAliExpressProvider):
    """"AliExpress True API" — ``GET /api/v3/products``; items under ``products[]`` / ``data.products[]``.

    ``evaluate_rate`` is a positive-feedback percentage (``"97.5%"``) and is converted to stars;
    ``promotion_link`` is a ready-made affiliate link and is kept as the product's link.
    """

    PARAMS = {
        "keywords": "{keyword}", "page_no": "{page}", "page_size": "{max_items}",
        "min_sale_price": "{price_min}", "max_sale_price": "{price_max}", "sort": "{sort}",
    }
    ITEM_PATHS = ("products", "data.products", "products.product", "data.products.product")
    FIELDS = {
        **COMMON_FIELDS,
        "goods_id": ("product_id",),
        "title": ("product_title",),
        "price": ("target_sale_price", "sale_price", "target_app_sale_price"),
        "original_price": ("target_original_price", "original_price"),
        "currency": ("target_sale_price_currency", "sale_price_currency"),
        "rating": ("evaluate_rate",),
        "sales_volume": ("lastest_volume", "latest_volume"),
        "raw_url": ("product_detail_url",),
        "affiliate_url": ("promotion_link",),
        "image_urls": ("product_main_image_url", "product_small_image_urls.string", "product_small_image_urls"),
        "category_hint": ("second_level_category_name", "first_level_category_name"),
    }


class AliExpressUnofficialAdapter(MappedAliExpressProvider):
    """"AliExpress Unofficial" — ``GET /search``; items under ``items[]`` / ``results[]``."""

    PARAMS = {"query": "{keyword}", "page": "{page}", "minPrice": "{price_min}", "maxPrice": "{price_max}"}
    ITEM_PATHS = ("items", "results", "data.items", "data.results")


class GenericConfigurableAliExpressAdapter(MappedAliExpressProvider):
    """An adapter driven entirely by the provider's ``request:`` and ``response:`` YAML blocks.

    ``request.params`` maps query-parameter names to literals or templates
    (``"{keyword}"``, ``"{max_items}"``, ``"{min_stars}"`` …; a parameter whose
    placeholder has no value is left out). ``response.items`` lists the dotted paths
    that may hold the item list, ``response.item_root`` an optional wrapper key inside
    each entry, and ``response.fields`` the paths of each normalised field; fields it
    does not name fall back to the common AliExpress spellings.
    """

    DEFAULT_PARAMS = {"query": "{keyword}"}

    def params_template(self) -> Mapping[str, Any]:
        request = self.config.request
        return request.params if request and request.params else self.DEFAULT_PARAMS

    def item_paths(self) -> Sequence[str]:
        return self.config.response.items if self.config.response else ()

    def item_root(self) -> str | None:
        return self.config.response.item_root if self.config.response else None

    def field_paths(self) -> Mapping[str, Sequence[str]]:
        return {**COMMON_FIELDS, **(self.config.response.fields if self.config.response else {})}

    def notice_paths(self) -> Sequence[str]:
        return (self.config.response.notice if self.config.response else None) or self.NOTICE_PATHS

    def price_divisor(self) -> float:
        return self.config.response.price_divisor if self.config.response else 1.0


# The names an ``adapter:`` line may use: the class name or a short alias.
ADAPTERS: dict[str, type[BaseAliExpressProvider]] = {
    "AliExpressDataHubAdapter": AliExpressDataHubAdapter, "datahub": AliExpressDataHubAdapter,
    "AliExpressTrueApiAdapter": AliExpressTrueApiAdapter, "true_api": AliExpressTrueApiAdapter,
    "AliExpressUnofficialAdapter": AliExpressUnofficialAdapter, "unofficial": AliExpressUnofficialAdapter,
    "GenericConfigurableAliExpressAdapter": GenericConfigurableAliExpressAdapter,
    "generic": GenericConfigurableAliExpressAdapter,
}
