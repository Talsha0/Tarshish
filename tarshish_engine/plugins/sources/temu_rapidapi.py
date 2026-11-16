"""Temu product source backed by the "Temu Product Scraper" API on RapidAPI.

RapidAPI meters searches (the free plan allows a handful per month), so the plugin
is built around one rule: **one search per fetch**. It asks for a broad batch
(``maxItems``, 20 by default) and does everything else locally — thresholds,
ranking, de-duplication.

The API is asynchronous. ``GET /temu`` starts a scraping job and answers with
``{"status": "running", "jobId": ..., "resultUrl": "/jobs/<id>", "pollAfterSeconds": 10}``;
the products arrive from ``GET /jobs/<id>`` once its status is ``done``. The plugin
waits and polls (sparingly: the gap doubles up to a minute). A job that outlives
``TEMU_JOB_TIMEOUT`` is not lost — its id is reported and ``fetch_batch(job_id=...)``
/ ``--seed-temu --job <id>`` collects it later without starting a new search.

Environment variables (see ``.env.example``)::

    RAPIDAPI_KEY                   required for live fetches; masked in every log line
    RAPIDAPI_TEMU_HOST             default temu-product-scraper.p.rapidapi.com
    TEMU_TRACKING_ID               your affiliate tracking value; empty = plain product links
    TEMU_AFFILIATE_URL_TEMPLATE    default {url}?_x_sessn_id={tracking_id}&refer_page_name=affiliate
    TEMU_MAX_ITEMS                 batch size per search, default 20
    TEMU_JOB_TIMEOUT               seconds to wait for a search job to finish, default 300
    TEMU_CURRENCY                  currency of the prices the API reports, default USD

Per-pipeline search terms live in the pipeline's ``options``::

    options:
      search_config:
        keyword: "tech gadgets"        # default: the pipeline's category
        l2Category: "..."              # optional API filters
        priceMin: 1
        priceMax: 100
        skip_published: false          # true: drop (not just rank last) products this pipeline already handled

A ``--dry-run`` never spends quota: it previews the Temu products already stored
in SQLite for the category (``python main.py --seed-temu`` puts them there).
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from dataclasses import dataclass, field
from string import Formatter
from typing import Any, Callable, Sequence
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator

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

logger = get_logger("plugins.temu_rapidapi")

DEFAULT_HOST = "temu-product-scraper.p.rapidapi.com"
DEFAULT_AFFILIATE_URL_TEMPLATE = "{url}?_x_sessn_id={tracking_id}&refer_page_name=affiliate"
ENDPOINT_PATH = "/temu"
JOB_PATH = "/jobs/{job_id}"
DEFAULT_MAX_ITEMS = 20
MAX_POLL_GAP = 60.0

_JOB_ID_RE = re.compile(r"[A-Za-z0-9_\-]{4,64}")
_JOB_PATH_RE = re.compile(r"/[A-Za-z0-9_\-./]{1,200}")
_RUNNING = {"running", "queued", "pending", "processing", "started", "in_progress"}
_DONE = {"done", "completed", "complete", "succeeded", "success", "finished"}
_FAILED = {"failed", "error", "cancelled", "canceled", "timeout", "timed_out", "expired"}

_HOST_RE = re.compile(r"(?=.{1,253}$)([a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", re.I)
_IMAGE_KEYS = ("imageUrl", "image", "image_url", "thumbnail", "thumbUrl", "imageUrls", "images")
_warned: set[str] = set()


class TemuRetryableError(TarshishError):
    """Rate limit, timeout or server-side failure; ``retry_after`` is honoured by ``with_retry``."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class TemuJobPending(TarshishError):
    """The search job was still running when the wait ran out; it can be collected later by id."""

    def __init__(self, job_id: str, waited: float) -> None:
        self.job_id = job_id
        super().__init__(
            f"Temu search job {job_id} was still running after {waited:.0f}s. Nothing is lost: collect it later "
            f"with `python main.py --seed-temu --category <name> --job {job_id}` (this starts no new search), "
            "or raise TEMU_JOB_TIMEOUT."
        )


def _config_error(title: str, exc: ValidationError, rename: Callable[[str], str] = str) -> ConfigError:
    lines = [
        f"  - {rename('.'.join(str(part) for part in err['loc']))}: {err['msg'].removeprefix('Value error, ')}"
        for err in exc.errors()
    ]
    return ConfigError(f"{title}:\n" + "\n".join(lines))


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
class TemuSettings(BaseModel):
    """The plugin's environment variables, validated (field name = variable name in lower case)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    rapidapi_key: SecretStr | None = None
    rapidapi_temu_host: str = DEFAULT_HOST
    temu_tracking_id: str | None = None
    temu_affiliate_url_template: str = DEFAULT_AFFILIATE_URL_TEMPLATE
    temu_max_items: int = Field(default=DEFAULT_MAX_ITEMS, ge=1, le=100)
    temu_job_timeout: float = Field(default=300.0, ge=0, le=3600)
    temu_currency: str = "USD"

    @field_validator("rapidapi_temu_host", mode="after")
    @classmethod
    def _bare_host(cls, value: str) -> str:
        # The API key is sent to this host, so it must be a host name and nothing else.
        if not _HOST_RE.fullmatch(value):
            raise ValueError("must be a bare host name such as 'temu-product-scraper.p.rapidapi.com' (no https://, no path)")
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
            raise _config_error(
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
    """``options.search_config`` of a pipeline (keys as the API spells them)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, str_strip_whitespace=True)

    keyword: str | None = Field(default=None, min_length=1)
    l2_category: str | None = Field(default=None, alias="l2Category", min_length=1)
    price_min: float | None = Field(default=None, alias="priceMin", ge=0)
    price_max: float | None = Field(default=None, alias="priceMax", ge=0)
    max_items: int | None = Field(default=None, alias="maxItems", ge=1, le=100)
    skip_published: bool = False


@dataclass
class TemuBatch:
    """What one API request yielded after local filtering."""

    keyword: str
    received: int = 0
    products: list[Product] = field(default_factory=list)            # usable, best first
    rejected: list[tuple[str, str]] = field(default_factory=list)    # (item label, reason)
    notice: str | None = None


# --------------------------------------------------------------------------- #
# Payload helpers
# --------------------------------------------------------------------------- #
def _number(value: Any) -> float:
    """``1.99``, ``"$1.99"``, ``"11,779"`` and ``"10K+"`` as a number; ``None`` counts as 0."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    match = re.search(r"(\d+(?:\.\d+)?)\s*([km])?", str(value).replace(",", ""), re.I)
    if not match:
        raise ValueError(f"not a number: {value!r}")
    return float(match[1]) * {"k": 1_000, "m": 1_000_000}.get((match[2] or "").lower(), 1)


def _image_urls(item: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for key in _IMAGE_KEYS:
        value = item.get(key)
        for candidate in value if isinstance(value, list) else [value]:
            if not isinstance(candidate, str):
                continue
            url = candidate.strip()
            url = f"https:{url}" if url.startswith("//") else url
            if re.match(r"https?://\S+$", url) and url not in urls:
                urls.append(url)
    return urls


def _rank(product: Product) -> tuple[float, int]:
    """Best first: rating weighted by the order of magnitude of the sales volume."""
    return (product.rating * math.log10(product.orders_count + 1), product.orders_count)


def _label(item: Any) -> str:
    if not isinstance(item, dict):
        return "?"
    title = str(item.get("title") or "").strip()
    return f"{item.get('goodsId') or '?'} {title[:48]}".strip()


class TemuRapidApiSource(BaseSource):
    """Real Temu listings through RapidAPI — one search per fetch."""

    _transport: Any = None  # test hook: an httpx.MockTransport instead of the network

    def __init__(self, context: PluginContext) -> None:
        super().__init__(context)
        self._settings: TemuSettings | None = None
        try:
            settings = self.settings()
        except ConfigError:
            return  # reported, with the variable name, when a fetch is attempted
        if settings.rapidapi_key is not None:
            register_secret(settings.rapidapi_key.get_secret_value())
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

    def search(self) -> TemuSearch:
        raw = self.context.options.get("search_config") or {}
        try:
            return TemuSearch.model_validate(raw)
        except ValidationError as exc:
            pipeline = self.context.pipeline_id or "?"
            raise _config_error(f"Pipeline '{pipeline}': invalid options.search_config", exc) from None

    # -- contract ------------------------------------------------------------------- #
    def fetch_products(self, category: str, min_rating: float, min_orders: int, limit: int) -> list[Product]:
        if limit <= 0:
            return []
        if self.context.dry_run:
            return self._stored_products(category, min_rating, min_orders, limit)

        search = self.search()
        batch = self.fetch_batch(category, min_rating, min_orders, max_items=limit)
        pipeline_id = self.context.pipeline_id
        handled = self.stored_refs(batch.products, published_by=pipeline_id) if pipeline_id else set()
        fresh = [product for product in batch.products if product.ref not in handled]
        # Products this pipeline already turned into posts go last: with enough fresh candidates they
        # fall off the end; otherwise the orchestrator skips them before any LLM call, and --force
        # and back-filling a newly added page keep working.
        seen = [] if search.skip_published else [product for product in batch.products if product.ref in handled]
        logger.info(
            "Temu '%s': %d received, %d usable, %d not yet handled by this pipeline.",
            batch.keyword, batch.received, len(batch.products), len(fresh),
        )
        return (fresh + seen)[:limit]

    # -- one search, then local work -------------------------------------------------- #
    def fetch_batch(
        self, category: str, min_rating: float = 0.0, min_orders: int = 0, *,
        keyword: str | None = None, max_items: int | None = None, job_id: str | None = None,
    ) -> TemuBatch:
        """Spend exactly one search and return its usable products, best first.

        With ``job_id`` no search is started: the results of that earlier job are collected.
        """
        settings = self.settings()
        if settings.rapidapi_key is None:
            raise ConfigError(
                "RAPIDAPI_KEY is not set — the Temu source needs it for live requests. Add it to .env "
                "(a --dry-run needs no key: it previews products already stored by --seed-temu)."
            )
        search = self.search()
        keyword = (keyword or search.keyword or re.sub(r"[\W_]+", " ", category)).strip()
        params: dict[str, Any] = {
            "keyword": keyword,
            "maxItems": min(100, max(search.max_items or settings.temu_max_items, max_items or 0)),
        }
        for name, value in (("l2Category", search.l2_category), ("priceMin", search.price_min),
                            ("priceMax", search.price_max)):
            if value is not None:
                params[name] = f"{value:g}" if isinstance(value, float) else value

        if job_id is not None:
            if not _JOB_ID_RE.fullmatch(job_id):
                raise ConfigError(f"'{job_id}' is not a Temu job id (expected letters, digits, '-' or '_').")
            started: dict[str, Any] = {"status": "running", "jobId": job_id, "pollAfterSeconds": 0}
        else:
            started = self._request(settings, ENDPOINT_PATH, params)
        payload = self._await_job(settings, started)

        if job_id is not None and isinstance(payload.get("query"), dict):
            keyword = str(payload["query"].get("keyword") or keyword)
        batch = self._parse(payload, keyword, category, settings, min_rating, min_orders)
        if batch.notice:
            logger.debug("Temu API notice for '%s': %s", keyword, batch.notice)
        logger.debug(
            "Temu '%s': 1 search, %d item(s) received, %d usable, %d rejected.",
            keyword, batch.received, len(batch.products), len(batch.rejected),
        )
        return batch

    def _await_job(self, settings: TemuSettings, job: dict[str, Any]) -> dict[str, Any]:
        """Follow a search job until it carries its results; polls are spaced out to stay cheap."""
        waited, gap = 0.0, None
        while True:
            status = str(job.get("status") or "").strip().lower()
            if isinstance(job.get("results"), list) and status not in _RUNNING:
                return job
            job_id = str(job.get("jobId") or "").strip()
            if status in _FAILED:
                detail = redact(str(job.get("message") or job.get("error") or "no details"))
                raise NonRetryableError(f"Temu search job {job_id or '?'} ended as '{status}': {detail}")
            path = str(job.get("resultUrl") or "")
            if not _JOB_PATH_RE.fullmatch(path):
                path = JOB_PATH.format(job_id=job_id) if _JOB_ID_RE.fullmatch(job_id) else ""
            if status in _DONE or not path:
                raise NonRetryableError(
                    "Temu API returned neither a 'results' list nor a running job "
                    f"(status {status or 'missing'!r}, fields: {', '.join(sorted(map(str, job))) or 'none'})"
                )
            if gap is None:
                logger.info("Temu search is running as job %s; waiting for its results…", job_id)
            elif waited >= settings.temu_job_timeout:
                raise TemuJobPending(job_id, waited)

            hint = job.get("pollAfterSeconds")
            hint = float(hint) if isinstance(hint, (int, float)) and not isinstance(hint, bool) else 10.0
            gap = min(MAX_POLL_GAP, max(0.0, hint) if gap is None else max(hint, gap * 2, 5.0))
            if gap > 0:
                resilience._sleep(gap)  # the engine's one sleeping seam
                waited += gap
            job = {"jobId": job_id, "resultUrl": path, **self._request(settings, path, None)}

    @with_retry(max_attempts=3, base_delay=2.0)
    def _request(self, settings: TemuSettings, path: str, params: dict[str, Any] | None) -> dict[str, Any]:
        headers = {
            "x-rapidapi-host": settings.rapidapi_temu_host,
            "x-rapidapi-key": settings.rapidapi_key.get_secret_value() if settings.rapidapi_key else "",
        }
        url = f"https://{settings.rapidapi_temu_host}{path}"
        try:
            with httpx.Client(timeout=httpx.Timeout(60.0, connect=10.0), transport=self._transport) as client:
                response = client.get(url, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise TemuRetryableError(redact(f"Temu API unreachable: {type(exc).__name__}: {exc}")) from None
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
        message = f"Temu API request failed (HTTP {status})" + (f": {detail}" if detail else "")

        if status == 429 and "quota" in detail.lower():
            raise NonRetryableError(f"{message} — the RapidAPI plan's quota is used up")
        if status in (408, 429) or status >= 500:
            retry_after = response.headers.get("retry-after", "")
            raise TemuRetryableError(message, retry_after=float(retry_after) if retry_after.isdigit() else None)
        if status in (401, 403):
            raise NonRetryableError(f"{message} — check RAPIDAPI_KEY and that the key is subscribed to this API")
        if status >= 400:
            raise NonRetryableError(message)
        if not isinstance(body, dict):
            raise NonRetryableError("Temu API returned an unexpected payload (not a JSON object)")
        if body.get("success") is False:
            raise NonRetryableError(f"Temu API reported a failure: {detail or 'no details'}")
        return body

    def _parse(
        self, payload: dict[str, Any], keyword: str, category: str, settings: TemuSettings,
        min_rating: float, min_orders: int,
    ) -> TemuBatch:
        results = payload.get("results") or []
        notice = payload.get("notice")
        batch = TemuBatch(keyword=keyword, received=len(results), notice=str(notice) if notice else None)

        candidates: list[Product] = []
        for item in results:
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

    def _product(self, item: Any, category: str, settings: TemuSettings) -> Product:
        if not isinstance(item, dict):
            raise ValueError("not an object")
        raw_url = str(item.get("url") or "").strip()
        return Product(
            external_id=str(item.get("goodsId") or "").strip(),
            source=self.source_name,
            category=category,
            title=str(item.get("title") or ""),
            price=_number(item.get("price")),
            currency=settings.temu_currency,
            rating=_number(item.get("rating")),
            orders_count=int(_number(item.get("salesVolume"))),
            raw_url=raw_url,
            affiliate_url=settings.affiliate_url(raw_url),
            image_urls=_image_urls(item) or [placeholder_image(category)],
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

    def _stored_products(self, category: str, min_rating: float, min_orders: int, limit: int) -> list[Product]:
        """Dry-run feed: this source's products already in SQLite — no request, no quota."""
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
                    raw_url=row["affiliate_url"].split("?", 1)[0], affiliate_url=row["affiliate_url"],
                    image_urls=json.loads(row["image_urls_json"] or "[]"),
                ))
            except (ValidationError, ValueError, TypeError):
                continue
        if not products:
            logger.warning(
                "Dry-run spends no RapidAPI quota and no Temu products are stored for category '%s' yet. "
                "Load some with: python main.py --seed-temu --category %s", category, category,
            )
        return sorted(products, key=_rank, reverse=True)[:limit]
