"""AliExpress product source backed by a failover chain of RapidAPI AliExpress endpoints.

RapidAPI free tiers for AliExpress meter searches and rate-limit hard (HTTP 429), and
any one endpoint can be out of quota, unsubscribed, down or simply empty. So the source
walks an ordered list of providers (``config/aliexpress_providers.yaml``; adapters in
``plugins/sources/aliexpress_providers/``) and **stops at the first one that yields
usable products**, which leaves the quota of the providers behind it untouched.

A provider is skipped — logged as a structured warning, never fatal — when

* its API key variable is not set (``api_key_env``, falling back to ``RAPIDAPI_KEY``),
* it answers HTTP 429 (quota used up; remembered for the rest of the run),
* it answers HTTP 401 / 403 (wrong key, or the key is not subscribed to that API),
* it stays unreachable or answers 5xx after two quick retries,
* it answers with no items, or with none that pass the local quality filter
  (``min_rating``, ``min_orders``, de-duplication by ``canonical_hash``).

When every provider fails the source logs an error summary and returns ``[]``.

Environment variables (see ``.env.example``)::

    RAPIDAPI_KEY_ALI_1 … _3                one key per provider (names set in aliexpress_providers.yaml)
    RAPIDAPI_KEY                           used by every provider whose own variable is empty
    ALIEXPRESS_PROVIDERS_CONFIG_PATH       default config/aliexpress_providers.yaml
    ALIEXPRESS_TRACKING_ID                 your affiliate tracking id; empty = plain product links
    ALIEXPRESS_AFFILIATE_URL_TEMPLATE      default {url}?aff_fcid={tracking_id}&aff_platform=portals-tool
    ALIEXPRESS_MAX_ITEMS                   batch size per search, default 20
    ALIEXPRESS_CURRENCY                    currency of prices when the API does not say, default USD

A promotion link the API already returns (``promotion_link``) is kept as the product's
affiliate link; the template is applied only to plain product URLs.

Search terms are a category's ``aliexpress_search`` rules (``config/categories.yaml`` ➔
``categories.aliexpress_search_json``), handed over by the harvester as ``options.search_config``::

    aliexpress_search:
      keyword: "smart gadgets"       # default: the category id
      priceMin: 5
      priceMax: 80
      sort: "salesDesc"              # passed to the providers that support sorting
      categoryId: "200000343"        # passed to generic providers that map {category_id}

A ``--dry-run`` never spends quota: it previews the AliExpress products already stored
in SQLite for the category (``python main.py --harvest`` puts them there), or the
``aliexpress_mock`` catalogue when nothing is stored yet.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from core.config import DEFAULT_ALIEXPRESS_AFFILIATE_URL_TEMPLATE, validate_affiliate_template
from core.database import Database, category_slug
from core.interfaces import BaseSource, PluginContext
from core.models import Product
from core.resilience import (
    ConfigError,
    NonRetryableError,
    TarshishError,
    get_logger,
    redact,
    register_secret,
    with_retry,
)
from plugins import placeholder_image
from plugins.sources import aliexpress_providers
from plugins.sources._rapidapi_common import config_error
from plugins.sources.aliexpress_providers import AliExpressProviderError, BaseAliExpressProvider, resolve_key

logger = get_logger("plugins.aliexpress_rapidapi")

DEFAULT_MAX_ITEMS = 20
MOCK_SOURCE = "aliexpress_mock"

# Outcome of asking one provider (ProviderAttempt.outcome) — the same words as the Temu chain.
SUCCESS = "success"
MISSING_KEY = "missing_key"            # the provider's API key variable is not set
QUOTA_EXHAUSTED = "quota_exhausted"    # HTTP 429
AUTH_FAILED = "auth_failed"            # HTTP 401 / 403
UNAVAILABLE = "unavailable"            # timeout / 5xx after the retries
EMPTY = "empty"                        # HTTP 200 with 0 items
FILTERED_OUT = "filtered_out"          # items came back, none passed the quality filter
ERROR = "error"                        # any other unusable reply

_warned: set[str] = set()


class AliExpressRetryableError(TarshishError):
    """Timeout or server-side failure; ``retry_after`` is honoured by ``with_retry``."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class AliExpressQuotaError(NonRetryableError):
    """HTTP 429: the plan's quota (or rate limit) for this key is used up."""


class AliExpressAuthError(NonRetryableError):
    """HTTP 401 / 403: the key is wrong or not subscribed to this API."""


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
class AliExpressSettings(BaseModel):
    """The plugin's environment variables, validated (field name = variable name in lower case)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    aliexpress_tracking_id: str | None = None
    aliexpress_affiliate_url_template: str = DEFAULT_ALIEXPRESS_AFFILIATE_URL_TEMPLATE
    aliexpress_max_items: int = Field(default=DEFAULT_MAX_ITEMS, ge=1, le=100)
    aliexpress_currency: str = "USD"

    @field_validator("aliexpress_affiliate_url_template", mode="after")
    @classmethod
    def _known_placeholders(cls, value: str) -> str:
        return validate_affiliate_template(value)

    @field_validator("aliexpress_currency", mode="after")
    @classmethod
    def _currency_code(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z]{3}", value):
            raise ValueError("must be a 3-letter ISO 4217 code such as 'USD'")
        return value.upper()

    @classmethod
    def from_env(cls, get_env: Callable[[str], str | None], defaults: Any = None) -> "AliExpressSettings":
        """Live environment first (so a changed ``.env`` applies without a restart), then ``core.config.Settings``."""
        values: dict[str, Any] = {}
        for name in cls.model_fields:
            value = get_env(name.upper())
            if value is None and defaults is not None:
                value = getattr(defaults, name, None)
            if value is not None:
                values[name] = value
        try:
            return cls(**values)
        except ValidationError as exc:
            raise config_error(
                "Invalid AliExpress source configuration (.env / environment variables)", exc, str.upper
            ) from None

    def affiliate_url(self, raw_url: str, api_link: str | None = None) -> str:
        """The API's own promotion link when it gave one; else the template; else the plain URL."""
        if api_link:
            return api_link
        if not self.aliexpress_tracking_id:
            return raw_url
        link = self.aliexpress_affiliate_url_template.format(
            url=raw_url, tracking_id=quote(self.aliexpress_tracking_id, safe="")
        )
        head, _, tail = link.partition("?")  # a product URL that already has a query keeps a single '?'
        return f"{head}?{tail.replace('?', '&')}" if tail else link


class AliExpressSearch(BaseModel):
    """A category's AliExpress search rules, ``options.search_config``."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, str_strip_whitespace=True)

    keyword: str | None = Field(default=None, min_length=1)
    price_min: float | None = Field(default=None, alias="priceMin", ge=0)
    price_max: float | None = Field(default=None, alias="priceMax", ge=0)
    sort: str | None = Field(default=None, min_length=1, max_length=40)
    category_id: str | None = Field(default=None, alias="categoryId", min_length=1, max_length=40)
    max_items: int | None = Field(default=None, alias="maxItems", ge=1, le=100)
    skip_published: bool = False


@dataclass
class ProviderAttempt:
    """What happened when one provider of the chain was asked."""

    provider: str
    adapter: str
    host: str
    outcome: str
    detail: str = ""
    http_status: int | None = None
    received: int = 0
    usable: int = 0
    key_env: str | None = None      # the variable the key came from — a name, never the key

    @property
    def ok(self) -> bool:
        return self.outcome == SUCCESS


@dataclass
class AliExpressBatch:
    """What one walk down the provider chain yielded after local filtering."""

    keyword: str
    received: int = 0
    products: list[Product] = field(default_factory=list)            # usable, best first
    rejected: list[tuple[str, str]] = field(default_factory=list)    # (item label, reason)
    notice: str | None = None
    provider: str | None = None                                      # the provider that delivered; None = all failed
    attempts: list[ProviderAttempt] = field(default_factory=list)    # in chain order, up to the one that delivered


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _rank(product: Product) -> tuple[float, int]:
    """Best first: rating weighted by the order of magnitude of the sales volume."""
    return (product.rating * math.log10(product.orders_count + 1), product.orders_count)


def _label(item: Mapping[str, Any]) -> str:
    return f"{item.get('goods_id') or '?'} {str(item.get('title') or '')[:48]}".strip()


def _payload_hint(payload: Mapping[str, Any]) -> str:
    """Why a 200 reply held no items, as far as the payload says."""
    for key in ("message", "description", "error", "msg"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return f"{key}: {redact(value.strip())[:160]}"
    return f"response fields: {', '.join(sorted(map(str, payload))) or 'none'}"


class AliExpressRapidApiSource(BaseSource):
    """Real AliExpress listings through RapidAPI — an ordered provider chain with failover."""

    _transport: Any = None  # test hook: an httpx.MockTransport instead of the network

    def __init__(self, context: PluginContext) -> None:
        super().__init__(context)
        self._settings: AliExpressSettings | None = None
        self._providers: list[BaseAliExpressProvider] | None = None
        try:
            settings = self.settings()
            for provider in self.providers():
                register_secret(resolve_key(provider.config, self.context.settings.get_env)[0])
        except ConfigError:
            return  # reported, with the variable or file name, when a fetch is attempted
        if not settings.aliexpress_tracking_id and "tracking" not in _warned:
            _warned.add("tracking")
            logger.warning(
                "ALIEXPRESS_TRACKING_ID is not set: AliExpress links fall back to the API's promotion link or "
                "the plain product URL, and clicks may not be attributed to you."
            )

    @property
    def source_name(self) -> str:
        return "aliexpress"

    # -- configuration ------------------------------------------------------------ #
    def settings(self) -> AliExpressSettings:
        if self._settings is None:
            self._settings = AliExpressSettings.from_env(self.context.settings.get_env, self.context.settings)
        return self._settings

    def providers(self) -> list[BaseAliExpressProvider]:
        """The configured chain, in order (disabled providers included)."""
        if self._providers is None:
            self._providers = aliexpress_providers.build_chain(self.context.settings.get_env)
        return self._providers

    def search(self) -> AliExpressSearch:
        raw = self.context.options.get("search_config") or {}
        try:
            return AliExpressSearch.model_validate(raw)
        except ValidationError as exc:
            raise config_error("Invalid aliexpress_search rules of the category", exc) from None

    # -- contract ------------------------------------------------------------------- #
    def fetch_products(self, category: str, min_rating: float, min_orders: int, limit: int) -> list[Product]:
        if limit <= 0:
            return []
        if self.context.dry_run:
            return self._preview_products(category, min_rating, min_orders, limit)

        search = self.search()
        batch = self.fetch_batch(category, min_rating, min_orders, max_items=limit)
        pipeline_id = self.context.pipeline_id
        handled = self.stored_refs(batch.products, published_by=pipeline_id) if pipeline_id else set()
        fresh = [product for product in batch.products if product.ref not in handled]
        # Products this pipeline already turned into posts go last (or are dropped with skip_published).
        seen = [] if search.skip_published else [product for product in batch.products if product.ref in handled]
        if batch.provider:
            logger.info(
                "AliExpress '%s' via %s: %d received, %d usable, %d not yet handled by this pipeline.",
                batch.keyword, batch.provider, batch.received, len(batch.products), len(fresh),
            )
        return (fresh + seen)[:limit]

    # -- the failover chain ------------------------------------------------------------ #
    def fetch_batch(
        self, category: str, min_rating: float = 0.0, min_orders: int = 0, *,
        keyword: str | None = None, max_items: int | None = None,
    ) -> AliExpressBatch:
        """Ask the providers in order and return the first usable batch, best products first.

        Every provider that is asked spends one of its searches, so the walk stops at the first
        one that delivers. When none does, the batch comes back empty with ``provider`` unset and
        one ``attempts`` entry per provider saying why.
        """
        settings = self.settings()
        search = self.search()
        chain = [provider for provider in self.providers() if provider.config.enabled]

        keyword = (keyword or search.keyword or re.sub(r"[\W_]+", " ", category)).strip()
        want = min(100, max(search.max_items or settings.aliexpress_max_items, max_items or 0))
        filters = {
            "price_min": search.price_min, "price_max": search.price_max, "sort": search.sort,
            "category_id": search.category_id, "min_rating": min_rating, "min_orders": min_orders,
        }

        batch = AliExpressBatch(keyword=keyword)
        for position, provider in enumerate(chain, start=1):
            attempt, found = self._ask(provider, settings, keyword, want, filters, category, min_rating, min_orders)
            batch.attempts.append(attempt)
            if attempt.ok and found is not None:
                batch.provider = provider.name
                batch.received, batch.notice = found.received, found.notice
                batch.products, batch.rejected = found.products, found.rejected
                break
            if found is not None:
                batch.received += found.received
                batch.rejected.extend(found.rejected)
                batch.notice = batch.notice or found.notice
            logger.warning(
                "AliExpress provider failover | provider=%s adapter=%s host=%s outcome=%s http=%s received=%d "
                "usable=%d key_env=%s detail=%s | %s",
                attempt.provider, attempt.adapter, attempt.host, attempt.outcome, attempt.http_status or "-",
                attempt.received, attempt.usable, attempt.key_env or "-", attempt.detail,
                "trying the next provider" if position < len(chain) else "no provider left",
            )

        if batch.provider:
            logger.info(
                "AliExpress '%s': provider %s delivered %d usable product(s) of %d received (%d provider(s) asked).",
                batch.keyword, batch.provider, len(batch.products), batch.received, len(batch.attempts),
            )
            if batch.notice:
                logger.debug("AliExpress API notice for '%s': %s", batch.keyword, batch.notice)
        else:
            logger.error(
                "AliExpress '%s': no provider delivered products (%s).", keyword,
                "; ".join(f"{attempt.provider}: {attempt.outcome}" for attempt in batch.attempts)
                or "no provider is enabled in the chain",
            )
        aliexpress_providers.record_run(self.context.settings.database_path, {
            "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "pipeline_id": self.context.pipeline_id, "category": category, "keyword": batch.keyword,
            "succeeded": batch.provider, "usable": len(batch.products),
            "attempts": [asdict(attempt) for attempt in batch.attempts],
        })
        return batch

    def _ask(
        self, provider: BaseAliExpressProvider, settings: AliExpressSettings, keyword: str, want: int,
        filters: Mapping[str, Any], category: str, min_rating: float, min_orders: int,
    ) -> tuple[ProviderAttempt, AliExpressBatch | None]:
        """One provider, one search. Every failure becomes an outcome; nothing escapes to the caller."""
        config = provider.config
        key, key_env = resolve_key(config, self.context.settings.get_env)
        attempt = ProviderAttempt(
            provider=config.name, adapter=type(provider).__name__, host=config.host, outcome=ERROR, key_env=key_env,
        )

        def failed(outcome: str, detail: str, status: int | None = None) -> tuple[ProviderAttempt, None]:
            attempt.outcome, attempt.detail, attempt.http_status = outcome, redact(detail), status
            return attempt, None

        if key is None:
            names = " or ".join(dict.fromkeys((config.api_key_env, aliexpress_providers.FALLBACK_KEY_ENV)))
            return failed(MISSING_KEY, f"{names} is not set in .env")
        if aliexpress_providers.is_exhausted(config.host, key):
            return failed(QUOTA_EXHAUSTED, "skipped: this key already ran out of quota on this API during this run")

        try:
            payload = self._request(key, *provider.build_request(keyword, want, filters))
            items = provider.parse_response(payload)
        except AliExpressQuotaError as exc:
            aliexpress_providers.mark_exhausted(config.host, key)
            return failed(QUOTA_EXHAUSTED, str(exc), 429)
        except AliExpressAuthError as exc:
            return failed(AUTH_FAILED, str(exc), getattr(exc, "status", None))
        except AliExpressRetryableError as exc:
            return failed(UNAVAILABLE, f"{exc} (after 2 retries)", getattr(exc, "status", None))
        except (NonRetryableError, AliExpressProviderError) as exc:
            return failed(ERROR, str(exc), getattr(exc, "status", 200))
        except (ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:  # a mapping that does not fit
            return failed(ERROR, f"the response could not be read: {type(exc).__name__}: {exc}")

        attempt.http_status, attempt.received = 200, len(items)
        if not items:
            return failed(EMPTY, f"HTTP 200 with 0 items ({_payload_hint(payload)})", 200)
        found = self._parse(items, keyword, category, settings, min_rating, min_orders)
        found.notice = provider.notice(payload)
        attempt.usable = len(found.products)
        if not found.products:
            attempt.outcome = FILTERED_OUT
            attempt.detail = (
                f"{len(items)} item(s) received, none passed the quality filter "
                f"(rating ≥ {min_rating:g}, orders ≥ {min_orders:,}, no duplicates)"
            )
            return attempt, found
        attempt.outcome = SUCCESS
        attempt.detail = f"{len(items)} item(s) received, {len(found.products)} usable"
        return attempt, found

    @with_retry(max_attempts=3, base_delay=1.0)
    def _request(self, key: str, url: str, headers: Mapping[str, str], params: Mapping[str, Any]) -> dict[str, Any]:
        """One GET; 2 quick retries on a timeout or 5xx (``with_retry``), none on 4xx."""
        try:
            with httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0), transport=self._transport) as client:
                response = client.get(url, params=dict(params) or None, headers={**headers, "x-rapidapi-key": key})
        except httpx.HTTPError as exc:
            raise AliExpressRetryableError(redact(f"API unreachable: {type(exc).__name__}: {exc}")) from None
        return self._interpret(response)

    @staticmethod
    def _interpret(response: httpx.Response) -> dict[str, Any]:
        """Map a RapidAPI response onto retryable / fatal outcomes."""
        try:
            body = response.json()
        except ValueError:
            body = None
        status = response.status_code
        detail = redact(str(body.get("message") or body.get("error") or "")) if isinstance(body, dict) else ""
        message = f"HTTP {status}" + (f": {detail}" if detail else "")

        def error(kind: type[TarshishError], text: str, **extra: Any) -> TarshishError:
            exc = kind(text, **extra)
            exc.status = status  # type: ignore[attr-defined]
            return exc

        if status == 429:
            raise error(AliExpressQuotaError, f"{message} — the plan's quota for this key is used up")
        if status == 408 or status >= 500:
            retry_after = response.headers.get("retry-after", "")
            raise error(
                AliExpressRetryableError, message, retry_after=float(retry_after) if retry_after.isdigit() else None
            )
        if status in (401, 403):
            raise error(AliExpressAuthError, f"{message} — check the key and that it is subscribed to this API")
        if status >= 400:
            raise error(NonRetryableError, message)
        if isinstance(body, list):
            body = {"items": body}  # a bare JSON array is read as {"items": [...]}
        if not isinstance(body, dict):
            raise error(NonRetryableError, "unexpected payload (not a JSON object)")
        if body.get("success") is False:
            raise error(NonRetryableError, f"the API reported a failure: {detail or 'no details'}")
        return body

    def _parse(
        self, items: Sequence[Mapping[str, Any]], keyword: str, category: str, settings: AliExpressSettings,
        min_rating: float, min_orders: int,
    ) -> AliExpressBatch:
        """Normalised items -> products: validate, apply the thresholds, rank, drop duplicates."""
        batch = AliExpressBatch(keyword=keyword, received=len(items))
        candidates: list[Product] = []
        for item in items:
            try:
                product = self._product(item, category, settings)
            except (ValidationError, ValueError, TypeError, KeyError) as exc:
                reason = exc.errors()[0]["msg"] if isinstance(exc, ValidationError) else str(exc)
                batch.rejected.append((_label(item), f"invalid item: {reason}"))
                continue
            if product.rating < min_rating:
                batch.rejected.append((_label(item), f"rating {product.rating:g} is below {min_rating:g}"))
            elif product.orders_count < min_orders:
                batch.rejected.append((_label(item), f"{product.orders_count:,} orders is below {min_orders:,}"))
            else:
                candidates.append(product)

        ids: set[str] = set()
        hashes: set[str] = set()
        for product in sorted(candidates, key=_rank, reverse=True):
            if product.external_id in ids or product.canonical_hash in hashes:
                batch.rejected.append((f"{product.external_id} {product.title[:48]}", "duplicate within the batch"))
                continue
            ids.add(product.external_id)
            hashes.add(product.canonical_hash)
            batch.products.append(product)
        return batch

    def _product(self, item: Mapping[str, Any], category: str, settings: AliExpressSettings) -> Product:
        price = item.get("price")
        if price is None:
            raise ValueError("no price")
        original = item.get("original_price")
        raw_url = str(item.get("raw_url") or "")
        return Product(
            external_id=str(item.get("goods_id") or ""),
            source=self.source_name,
            category=category,
            title=str(item.get("title") or ""),
            price=price,
            original_price=original if original and original > price else None,
            currency=item.get("currency") or settings.aliexpress_currency,
            rating=item.get("rating") or 0.0,
            orders_count=int(item.get("sales_volume") or 0),
            raw_url=raw_url,
            affiliate_url=settings.affiliate_url(raw_url, item.get("affiliate_url")),
            image_urls=list(item.get("image_urls") or []) or [placeholder_image(category)],
        )

    # -- SQLite ----------------------------------------------------------------------- #
    def _db(self) -> Database:
        return Database(self.context.settings.database_path)

    def stored_refs(self, products: Sequence[Product], *, published_by: str | None = None) -> set[str]:
        """``Product.ref`` of the given products that SQLite already knows.

        A product matches on ``(source, external_id)`` or on ``canonical_hash`` (the same
        item listed by another source). With ``published_by`` (a pipeline id) only products
        that already have a publication of that pipeline count.
        """
        if not products:
            return set()
        ids = [product.external_id for product in products]
        hashes = [product.canonical_hash for product in products]
        sql = (
            "SELECT DISTINCT p.source, p.external_id, p.canonical_hash FROM products p "
            + ("JOIN publications pub ON pub.product_id = p.id AND pub.pipeline_id = ? " if published_by else "")
            + f"WHERE (p.source = ? AND p.external_id IN ({', '.join('?' for _ in ids)})) "
            f"OR p.canonical_hash IN ({', '.join('?' for _ in hashes)})"
        )
        params = [*([published_by] if published_by else []), self.source_name, *ids, *hashes]
        try:
            rows = self._db().query(sql, params)
        except sqlite3.Error as exc:
            logger.warning("Could not check stored products (%s); treating the batch as new.", exc)
            return set()
        stored_ids = {row["external_id"] for row in rows if row["source"] == self.source_name}
        stored_hashes = {row["canonical_hash"] for row in rows}
        return {p.ref for p in products if p.external_id in stored_ids or p.canonical_hash in stored_hashes}

    # -- dry-run ------------------------------------------------------------------------ #
    def _preview_products(self, category: str, min_rating: float, min_orders: int, limit: int) -> list[Product]:
        """Dry-run feed — no request, no quota: stored AliExpress products, else the ``aliexpress_mock`` source."""
        products = sorted(self._stored_products(category, min_rating, min_orders), key=_rank, reverse=True)
        if products:
            return products[:limit]
        from plugins.sources.aliexpress_mock import AliExpressMockSource

        products = AliExpressMockSource(self.context).fetch_products(category, min_rating, min_orders, limit)
        logger.warning(
            "Dry-run spends no RapidAPI quota and no AliExpress products are stored for category '%s' yet: "
            "previewing %d product(s) from %s. Load real ones with: python main.py --seed-aliexpress --category %s",
            category, len(products), MOCK_SOURCE, category,
        )
        return products

    def _stored_products(self, category: str, min_rating: float, min_orders: int) -> list[Product]:
        try:
            rows = self._db().query(
                "SELECT external_id, title, price, original_price, currency, rating, orders_count, "
                "affiliate_url, image_urls_json FROM products "
                "WHERE source = ? AND category = ? AND rating >= ? AND orders_count >= ?",
                (self.source_name, category_slug(category), min_rating, min_orders),
            )
        except sqlite3.Error as exc:
            logger.warning("Could not read stored AliExpress products (%s).", exc)
            rows = []
        products: list[Product] = []
        for row in rows:
            try:
                products.append(Product(
                    external_id=row["external_id"], source=self.source_name, category=category, title=row["title"],
                    price=row["price"], original_price=row["original_price"], currency=row["currency"],
                    rating=row["rating"], orders_count=row["orders_count"],
                    raw_url=row["affiliate_url"].split("?", 1)[0], affiliate_url=row["affiliate_url"],
                    image_urls=json.loads(row["image_urls_json"] or "[]"),
                ))
            except (ValidationError, ValueError, TypeError):
                continue
        return products
