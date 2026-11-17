"""Temu product source backed by a failover chain of RapidAPI Temu endpoints.

RapidAPI plans meter searches (free tiers allow a handful per month) and any one
endpoint can be out of quota, unsubscribed, down or simply empty. So the source
walks an ordered list of providers (``config/temu_providers.yaml``; adapters in
``plugins/sources/temu_providers/``) and **stops at the first one that yields
usable products**, which leaves the quota of the providers behind it untouched.

A provider is skipped — logged, never fatal — when

* its API key variable is not set (``api_key_env``, falling back to ``RAPIDAPI_KEY``),
* it answers HTTP 429 (quota used up; remembered for the rest of the run),
* it answers HTTP 401 / 403 (wrong key, or the key is not subscribed to that API),
* it stays unreachable or answers 5xx after two quick retries,
* it answers with no items, or with none that pass the local quality filter.

Each provider gets one search per fetch: a broad batch (``TEMU_MAX_ITEMS``, 20 by
default) that is filtered, ranked and de-duplicated locally. When every provider
fails the source logs a summary and returns nothing.

Some endpoints search in the background and answer with a running job; the source
waits and polls (the gap doubles up to a minute). A job that outlives
``TEMU_JOB_TIMEOUT`` is not lost — ``--seed-temu --job <id>`` collects it later
without starting a new search — and the chain moves on meanwhile.

Environment variables (see ``.env.example``)::

    RAPIDAPI_KEY_TEMU_1 … _N       one key per provider (the names are set in temu_providers.yaml)
    RAPIDAPI_KEY                   used by every provider whose own variable is empty
    TEMU_PROVIDERS_CONFIG_PATH     default config/temu_providers.yaml
    RAPIDAPI_TEMU_HOST             moves the first TemuProductScraperAdapter entry to another host
    TEMU_TRACKING_ID               your affiliate tracking value; empty = plain product links
    TEMU_AFFILIATE_URL_TEMPLATE    default {url}?_x_sessn_id={tracking_id}&refer_page_name=affiliate
    TEMU_MAX_ITEMS                 batch size per search, default 20
    TEMU_JOB_TIMEOUT               seconds to wait for a background search, default 300
    TEMU_CURRENCY                  currency of prices when the API does not say, default USD

Search terms are a category's ``temu_search`` rules (``config/categories.yaml`` ➔
``categories.temu_search_json``), handed over by the harvester as ``options.search_config``::

    temu_search:
      keyword: "smart tech gadgets"  # default: the category id
      l1Category: "..."              # optional filters, passed to the providers that support them
      l2Category: "..."
      priceMin: 1
      priceMax: 100

A ``--dry-run`` never spends quota: it previews the Temu products already stored
in SQLite for the category (``python main.py --harvest`` puts them there), or
mock stand-ins when nothing is stored yet.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from string import Formatter
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from core import resilience
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
from plugins.sources import temu_providers
from plugins.sources.temu_providers import JOB_ID_RE, BaseTemuProvider, TemuProviderError, resolve_key
from plugins.sources.temu_providers.config import PRIMARY_HOST, config_error

logger = get_logger("plugins.temu_rapidapi")

DEFAULT_HOST = PRIMARY_HOST
DEFAULT_AFFILIATE_URL_TEMPLATE = "{url}?_x_sessn_id={tracking_id}&refer_page_name=affiliate"
DEFAULT_MAX_ITEMS = 20
MAX_POLL_GAP = 60.0

# Outcome of asking one provider (ProviderAttempt.outcome).
SUCCESS = "success"
MISSING_KEY = "missing_key"            # the provider's API key variable is not set
QUOTA_EXHAUSTED = "quota_exhausted"    # HTTP 429
AUTH_FAILED = "auth_failed"            # HTTP 401 / 403
UNAVAILABLE = "unavailable"            # timeout / 5xx after the retries
EMPTY = "empty"                        # HTTP 200 with 0 items
FILTERED_OUT = "filtered_out"          # items came back, none passed the quality filter
JOB_PENDING = "job_pending"            # the background search outlived TEMU_JOB_TIMEOUT
ERROR = "error"                        # any other unusable reply

_HOST_RE = re.compile(r"(?=.{1,253}$)([a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", re.I)
_warned: set[str] = set()


class TemuRetryableError(TarshishError):
    """Timeout or server-side failure; ``retry_after`` is honoured by ``with_retry``."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class TemuQuotaError(NonRetryableError):
    """HTTP 429: the plan's quota (or rate limit) for this key is used up."""


class TemuAuthError(NonRetryableError):
    """HTTP 401 / 403: the key is wrong or not subscribed to this API."""


class TemuJobPending(TarshishError):
    """The search job was still running when the wait ran out; it can be collected later by id."""

    def __init__(self, job_id: str, waited: float) -> None:
        self.job_id = job_id
        super().__init__(
            f"search job {job_id} was still running after {waited:.0f}s. Nothing is lost: collect it later "
            f"with `python main.py --seed-temu --category <name> --job {job_id}` (this starts no new search), "
            "or raise TEMU_JOB_TIMEOUT."
        )


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
class TemuSettings(BaseModel):
    """The plugin's environment variables, validated (field name = variable name in lower case)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    rapidapi_temu_host: str | None = None
    temu_tracking_id: str | None = None
    temu_affiliate_url_template: str = DEFAULT_AFFILIATE_URL_TEMPLATE
    temu_max_items: int = Field(default=DEFAULT_MAX_ITEMS, ge=1, le=100)
    temu_job_timeout: float = Field(default=300.0, ge=0, le=3600)
    temu_currency: str = "USD"

    @field_validator("rapidapi_temu_host", mode="after")
    @classmethod
    def _bare_host(cls, value: str | None) -> str | None:
        # An API key is sent to this host, so it must be a host name and nothing else.
        if value is None:
            return None
        if not _HOST_RE.fullmatch(value):
            raise ValueError(f"must be a bare host name such as '{DEFAULT_HOST}' (no https://, no path)")
        return value.lower()

    @field_validator("temu_affiliate_url_template", mode="after")
    @classmethod
    def _known_placeholders(cls, value: str) -> str:
        try:
            names = {name for _, name, _, _ in Formatter().parse(value) if name is not None}
        except ValueError as exc:
            raise ValueError(f"is not a valid template ({exc})") from exc
        if "url" not in names or names - {"url", "tracking_id"}:
            raise ValueError("must contain {url} and may only use the placeholders {url} and {tracking_id}")
        return value

    @field_validator("temu_currency", mode="after")
    @classmethod
    def _currency_code(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z]{3}", value):
            raise ValueError("must be a 3-letter ISO 4217 code such as 'USD'")
        return value.upper()

    @classmethod
    def from_env(cls, get_env: Callable[[str], str | None]) -> "TemuSettings":
        values = {name: get_env(name.upper()) for name in cls.model_fields}
        try:
            return cls(**{name: value for name, value in values.items() if value is not None})
        except ValidationError as exc:
            raise config_error(
                "Invalid Temu source configuration (.env / environment variables)", exc, str.upper
            ) from None

    def affiliate_url(self, raw_url: str) -> str:
        """The tracking link for a product URL; the plain URL when no tracking id is configured."""
        if not self.temu_tracking_id:
            return raw_url
        link = self.temu_affiliate_url_template.format(url=raw_url, tracking_id=quote(self.temu_tracking_id, safe=""))
        head, _, tail = link.partition("?")  # a product URL that already has a query keeps a single '?'
        return f"{head}?{tail.replace('?', '&')}" if tail else link


class TemuSearch(BaseModel):
    """A category's Temu search rules, ``options.search_config`` (keys as the original API spells them)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, str_strip_whitespace=True)

    keyword: str | None = Field(default=None, min_length=1)
    l1_category: str | None = Field(default=None, alias="l1Category", min_length=1)
    l2_category: str | None = Field(default=None, alias="l2Category", min_length=1)
    price_min: float | None = Field(default=None, alias="priceMin", ge=0)
    price_max: float | None = Field(default=None, alias="priceMax", ge=0)
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
class TemuBatch:
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


class TemuRapidApiSource(BaseSource):
    """Real Temu listings through RapidAPI — an ordered provider chain with failover."""

    _transport: Any = None  # test hook: an httpx.MockTransport instead of the network

    def __init__(self, context: PluginContext) -> None:
        super().__init__(context)
        self._settings: TemuSettings | None = None
        self._providers: list[BaseTemuProvider] | None = None
        try:
            settings = self.settings()
            for provider in self.providers():
                register_secret(resolve_key(provider.config, self.context.settings.get_env)[0])
        except ConfigError:
            return  # reported, with the variable or file name, when a fetch is attempted
        if not settings.temu_tracking_id and "tracking" not in _warned:
            _warned.add("tracking")
            logger.warning(
                "TEMU_TRACKING_ID is not set: Temu affiliate links fall back to the plain product URL "
                "and clicks will not be attributed to you."
            )

    @property
    def source_name(self) -> str:
        return "temu"

    # -- configuration ------------------------------------------------------------ #
    def settings(self) -> TemuSettings:
        if self._settings is None:
            self._settings = TemuSettings.from_env(self.context.settings.get_env)
        return self._settings

    def providers(self) -> list[BaseTemuProvider]:
        """The configured chain, in order (disabled providers included)."""
        if self._providers is None:
            self._providers = temu_providers.build_chain(self.context.settings.get_env)
        return self._providers

    def search(self) -> TemuSearch:
        raw = self.context.options.get("search_config") or {}
        try:
            return TemuSearch.model_validate(raw)
        except ValidationError as exc:
            raise config_error("Invalid temu_search rules of the category", exc) from None

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
        # Products this pipeline already turned into posts go last: with enough fresh candidates they
        # fall off the end; otherwise the orchestrator skips them before any LLM call, and --force
        # and back-filling a newly added page keep working.
        seen = [] if search.skip_published else [product for product in batch.products if product.ref in handled]
        if batch.provider:
            logger.info(
                "Temu '%s' via %s: %d received, %d usable, %d not yet handled by this pipeline.",
                batch.keyword, batch.provider, batch.received, len(batch.products), len(fresh),
            )
        return (fresh + seen)[:limit]

    # -- the failover chain ------------------------------------------------------------ #
    def fetch_batch(
        self, category: str, min_rating: float = 0.0, min_orders: int = 0, *,
        keyword: str | None = None, max_items: int | None = None, job_id: str | None = None,
    ) -> TemuBatch:
        """Ask the providers in order and return the first usable batch, best products first.

        Every provider that is asked spends one of its searches, so the walk stops at the first
        one that delivers. When none does, the batch comes back empty with ``provider`` unset and
        one ``attempts`` entry per provider saying why. With ``job_id`` no search is started: the
        results of that earlier background job are collected.
        """
        settings = self.settings()
        search = self.search()
        chain = [provider for provider in self.providers() if provider.config.enabled]
        if job_id is not None:
            if not JOB_ID_RE.fullmatch(job_id):
                raise ConfigError(f"'{job_id}' is not a Temu job id (expected letters, digits, '-' or '_').")
            chain = [provider for provider in chain if provider.job_request(job_id) is not None]
            if not chain:
                raise ConfigError("No enabled Temu provider runs searches as jobs, so there is nothing to collect by id.")

        keyword = (keyword or search.keyword or re.sub(r"[\W_]+", " ", category)).strip()
        want = min(100, max(search.max_items or settings.temu_max_items, max_items or 0))
        filters = {
            "price_min": search.price_min, "price_max": search.price_max, "l1_category": search.l1_category,
            "l2_category": search.l2_category, "min_rating": min_rating, "min_orders": min_orders,
        }

        batch = TemuBatch(keyword=keyword)
        for position, provider in enumerate(chain, start=1):
            attempt, found = self._ask(
                provider, settings, keyword, want, filters, category, min_rating, min_orders, job_id
            )
            batch.attempts.append(attempt)
            if attempt.ok and found is not None:
                batch.provider = provider.name
                batch.keyword, batch.received, batch.notice = found.keyword, found.received, found.notice
                batch.products, batch.rejected = found.products, found.rejected
                break
            if found is not None:
                batch.received += found.received
                batch.rejected.extend(found.rejected)
                batch.notice = batch.notice or found.notice
            logger.warning(
                "Temu provider failover | provider=%s adapter=%s host=%s outcome=%s http=%s received=%d usable=%d "
                "detail=%s | %s",
                attempt.provider, attempt.adapter, attempt.host, attempt.outcome, attempt.http_status or "-",
                attempt.received, attempt.usable, attempt.detail,
                "trying the next provider" if position < len(chain) else "no provider left",
            )

        if batch.provider:
            logger.info(
                "Temu '%s': provider %s delivered %d usable product(s) of %d received (%d provider(s) asked).",
                batch.keyword, batch.provider, len(batch.products), batch.received, len(batch.attempts),
            )
            if batch.notice:
                logger.debug("Temu API notice for '%s': %s", batch.keyword, batch.notice)
        else:
            logger.error(
                "Temu '%s': no provider delivered products (%s).", keyword,
                "; ".join(f"{attempt.provider}: {attempt.outcome}" for attempt in batch.attempts)
                or "no provider is enabled in the chain",
            )
        temu_providers.record_run(self.context.settings.database_path, {
            "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "pipeline_id": self.context.pipeline_id, "category": category, "keyword": batch.keyword,
            "succeeded": batch.provider, "usable": len(batch.products),
            "attempts": [asdict(attempt) for attempt in batch.attempts],
        })
        return batch

    def _ask(
        self, provider: BaseTemuProvider, settings: TemuSettings, keyword: str, want: int,
        filters: Mapping[str, Any], category: str, min_rating: float, min_orders: int, job_id: str | None,
    ) -> tuple[ProviderAttempt, TemuBatch | None]:
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
            names = " or ".join(dict.fromkeys((config.api_key_env, temu_providers.FALLBACK_KEY_ENV)))
            return failed(MISSING_KEY, f"{names} is not set in .env")
        register_secret(key)
        if temu_providers.is_exhausted(config.host, key):
            return failed(QUOTA_EXHAUSTED, "skipped: this key already ran out of quota on this API during this run")

        try:
            request = provider.job_request(job_id) if job_id else provider.build_request(keyword, want, filters)
            payload = self._await_job(provider, key, self._request(key, *request), settings)
            items = provider.parse_response(payload)
        except TemuQuotaError as exc:
            temu_providers.mark_exhausted(config.host, key)
            return failed(QUOTA_EXHAUSTED, str(exc), 429)
        except TemuAuthError as exc:
            return failed(AUTH_FAILED, str(exc), getattr(exc, "status", None))
        except TemuJobPending as exc:
            return failed(JOB_PENDING, str(exc))
        except TemuRetryableError as exc:
            return failed(UNAVAILABLE, f"{exc} (after 2 retries)", getattr(exc, "status", None))
        except (NonRetryableError, TemuProviderError) as exc:
            return failed(ERROR, str(exc), getattr(exc, "status", None))
        except (ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:  # a mapping that does not fit
            return failed(ERROR, f"the response could not be read: {type(exc).__name__}: {exc}")

        attempt.http_status, attempt.received = 200, len(items)
        if not items:
            return failed(EMPTY, f"HTTP 200 with 0 items ({_payload_hint(payload)})", 200)
        found = self._parse(items, provider.searched_keyword(payload) or keyword, category, settings,
                            min_rating, min_orders)
        found.notice = provider.notice(payload)
        attempt.usable = len(found.products)
        if not found.products:
            attempt.outcome = FILTERED_OUT
            attempt.detail = (
                f"{len(items)} item(s) received, none passed the quality filter "
                f"(rating ≥ {min_rating:g}, sold ≥ {min_orders:,})"
            )
            return attempt, found
        attempt.outcome = SUCCESS
        attempt.detail = f"{len(items)} item(s) received, {len(found.products)} usable"
        return attempt, found

    def _await_job(
        self, provider: BaseTemuProvider, key: str, payload: dict[str, Any], settings: TemuSettings
    ) -> dict[str, Any]:
        """Follow a background search until its payload is final; polls are spaced out to stay cheap."""
        waited, gap, job = 0.0, None, None
        while True:
            job = provider.pending_job(payload, job)
            if job is None:
                return payload
            if gap is None:
                logger.info("Temu search on %s is running as job %s; waiting for its results…", provider.name, job.job_id)
            elif waited >= settings.temu_job_timeout:
                raise TemuJobPending(job.job_id, waited)
            gap = min(MAX_POLL_GAP, job.poll_after if gap is None else max(job.poll_after, gap * 2, 5.0))
            if gap > 0:
                resilience._sleep(gap)  # the engine's one sleeping seam
                waited += gap
            payload = self._request(key, job.url, provider.headers(), {})

    @with_retry(max_attempts=3, base_delay=2.0)
    def _request(self, key: str, url: str, headers: Mapping[str, str], params: Mapping[str, Any]) -> dict[str, Any]:
        try:
            with httpx.Client(timeout=httpx.Timeout(60.0, connect=10.0), transport=self._transport) as client:
                response = client.get(url, params=dict(params) or None, headers={**headers, "x-rapidapi-key": key})
        except httpx.HTTPError as exc:
            raise TemuRetryableError(redact(f"API unreachable: {type(exc).__name__}: {exc}")) from None
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
            raise error(TemuQuotaError, f"{message} — the plan's quota for this key is used up")
        if status == 408 or status >= 500:
            retry_after = response.headers.get("retry-after", "")
            raise error(TemuRetryableError, message, retry_after=float(retry_after) if retry_after.isdigit() else None)
        if status in (401, 403):
            raise error(TemuAuthError, f"{message} — check the key and that it is subscribed to this API")
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
        self, items: Sequence[Mapping[str, Any]], keyword: str, category: str, settings: TemuSettings,
        min_rating: float, min_orders: int,
    ) -> TemuBatch:
        """Normalised items -> products: validate, apply the thresholds, rank, drop duplicates."""
        batch = TemuBatch(keyword=keyword, received=len(items))
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
                batch.rejected.append((_label(item), f"{product.orders_count:,} sold is below {min_orders:,}"))
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

    def _product(self, item: Mapping[str, Any], category: str, settings: TemuSettings) -> Product:
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
            currency=item.get("currency") or settings.temu_currency,
            rating=item.get("rating") or 0.0,
            orders_count=int(item.get("sales_volume") or 0),
            raw_url=raw_url,
            affiliate_url=settings.affiliate_url(raw_url),
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
        """Dry-run feed — no request, no quota: stored Temu products, else mock stand-ins."""
        products = self._stored_products(category, min_rating, min_orders)
        if not products:
            products = self._mock_products(category, min_rating, min_orders)
            logger.warning(
                "Dry-run spends no RapidAPI quota and no Temu products are stored for category '%s' yet: "
                "previewing %d mock stand-in(s). Load real ones with: python main.py --seed-temu --category %s",
                category, len(products), category,
            )
        return sorted(products, key=_rank, reverse=True)[:limit]

    def _stored_products(self, category: str, min_rating: float, min_orders: int) -> list[Product]:
        try:
            rows = self._db().query(
                "SELECT external_id, title, price, original_price, currency, rating, orders_count, "
                "affiliate_url, image_urls_json FROM products "
                "WHERE source = ? AND category = ? AND rating >= ? AND orders_count >= ?",
                (self.source_name, category_slug(category), min_rating, min_orders),
            )
        except sqlite3.Error as exc:
            logger.warning("Could not read stored Temu products (%s).", exc)
            rows = []
        products: list[Product] = []
        for row in rows:
            try:
                products.append(Product(
                    external_id=row["external_id"], source=self.source_name, category=category, title=row["title"],
                    price=row["price"], original_price=row["original_price"], currency=row["currency"],
                    rating=row["rating"], orders_count=row["orders_count"],
                    raw_url=row["affiliate_url"].split("&" if "goods_id=" in row["affiliate_url"] else "?", 1)[0],
                    affiliate_url=row["affiliate_url"],
                    image_urls=json.loads(row["image_urls_json"] or "[]"),
                ))
            except (ValidationError, ValueError, TypeError):
                continue
        return products

    def _mock_products(self, category: str, min_rating: float, min_orders: int) -> list[Product]:
        """The mock Temu shelf, for previewing a pipeline before any real product is stored."""
        from plugins.sources._mock_catalogue import product_fields

        products: list[Product] = []
        for fields in product_fields("temu_mock", category):
            try:
                product = Product(source=self.source_name, category=category, **fields)
            except (ValidationError, ValueError, TypeError):
                continue
            if product.rating >= min_rating and product.orders_count >= min_orders:
                products.append(product)
        return products
