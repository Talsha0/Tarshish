"""Concrete adapters for RapidAPI Temu scraper schemas.

Three adapters cover fixed schemas; ``GenericConfigurableTemuAdapter`` reads its
request parameters and response paths from ``config/temu_providers.yaml``, so a
new RapidAPI Temu endpoint is one YAML entry, not a Python change.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from core.resilience import redact
from plugins.sources.temu_providers.base import (
    COMMON_FIELDS,
    JOB_ID_RE,
    BaseTemuProvider,
    MappedTemuProvider,
    PendingJob,
    TemuProviderError,
)

_JOB_PATH_RE = re.compile(r"/[A-Za-z0-9_\-./]{1,200}")
_RUNNING = {"running", "queued", "pending", "processing", "started", "in_progress"}
_DONE = {"done", "completed", "complete", "succeeded", "success", "finished"}
_FAILED = {"failed", "error", "cancelled", "canceled", "timeout", "timed_out", "expired"}


class TemuProductScraperAdapter(MappedTemuProvider):
    """"Temu Product Scraper" — ``GET /temu``, items under ``results[]``.

    The search may run as a background job: the reply is then
    ``{"status": "running", "jobId": ..., "resultUrl": "/jobs/<id>", "pollAfterSeconds": 10}``
    and the items arrive from ``GET /jobs/<id>`` once its status is ``done``.
    """

    JOB_PATH = "/jobs/{job_id}"
    PARAMS = {
        "keyword": "{keyword}", "maxItems": "{max_items}", "l1Category": "{l1_category}",
        "l2Category": "{l2_category}", "priceMin": "{price_min}", "priceMax": "{price_max}",
    }
    ITEM_PATHS = ("results",)
    FIELDS = {
        **COMMON_FIELDS,
        "goods_id": ("goodsId",),
        "sales_volume": ("salesVolume", "salesText"),
        "raw_url": ("url",),
        "category_hint": ("l2Category", "l1Category"),
    }

    def pending_job(self, payload: dict[str, Any], previous: PendingJob | None = None) -> PendingJob | None:
        status = str(payload.get("status") or "").strip().lower()
        if isinstance(payload.get("results"), list) and status not in _RUNNING:
            return None
        job_id = str(payload.get("jobId") or (previous.job_id if previous else "")).strip()
        if status in _FAILED:
            detail = redact(str(payload.get("message") or payload.get("error") or "no details"))
            raise TemuProviderError(f"search job {job_id or '?'} ended as '{status}': {detail}")
        path = str(payload.get("resultUrl") or "")
        if not _JOB_PATH_RE.fullmatch(path):
            path = self.JOB_PATH.format(job_id=job_id) if JOB_ID_RE.fullmatch(job_id) else ""
        if status in _DONE or not path:
            raise TemuProviderError(
                "the API returned neither a 'results' list nor a running job "
                f"(status {status or 'missing'!r}, fields: {', '.join(sorted(map(str, payload))) or 'none'})"
            )
        hint = payload.get("pollAfterSeconds")
        poll_after = float(hint) if isinstance(hint, (int, float)) and not isinstance(hint, bool) else 10.0
        return PendingJob(job_id=job_id, url=f"{self.config.origin}{path}", poll_after=max(0.0, poll_after))

    def job_request(self, job_id: str) -> tuple[str, dict[str, str], dict[str, Any]] | None:
        return f"{self.config.origin}{self.JOB_PATH.format(job_id=job_id)}", self.headers(), {}

    def searched_keyword(self, payload: dict[str, Any]) -> str | None:
        query = payload.get("query")
        keyword = query.get("keyword") if isinstance(query, dict) else None
        return str(keyword) if keyword else None


class TemuScraper2Adapter(MappedTemuProvider):
    """``GET /search`` with ``query``/``keyword``, ``page``, ``limit``; items under ``data.products[]`` or ``items[]``."""

    PARAMS = {"query": "{keyword}", "keyword": "{keyword}", "page": "{page}", "limit": "{max_items}"}
    ITEM_PATHS = ("data.products", "items")
    NOTICE_PATHS = ("notice", "message")


class TemuApiRealtimeAdapter(MappedTemuProvider):
    """``GET /api/search`` with ``keywords``, ``page``, ``pageSize``; items under ``result.goodsList[]`` or ``products[]``."""

    PARAMS = {"keywords": "{keyword}", "page": "{page}", "pageSize": "{max_items}"}
    ITEM_PATHS = ("result.goodsList", "products")
    NOTICE_PATHS = ("notice", "message")


class GenericConfigurableTemuAdapter(MappedTemuProvider):
    """An adapter driven entirely by the provider's ``request:`` and ``response:`` YAML blocks.

    ``request.params`` maps query-parameter names to literals or templates
    (``"{keyword}"``, ``"{max_items}"``, ``"{min_stars}"`` …; a parameter whose
    placeholder has no value is left out). ``response.items`` lists the dotted
    paths that may hold the item list, and ``response.fields`` the paths of each
    normalised field; fields it does not name fall back to the common spellings.
    """

    DEFAULT_PARAMS = {"keyword": "{keyword}"}

    def params_template(self) -> Mapping[str, Any]:
        request = self.config.request
        return request.params if request and request.params else self.DEFAULT_PARAMS

    def item_paths(self) -> Sequence[str]:
        return self.config.response.items if self.config.response else ()

    def field_paths(self) -> Mapping[str, Sequence[str]]:
        return {**COMMON_FIELDS, **(self.config.response.fields if self.config.response else {})}

    def notice_paths(self) -> Sequence[str]:
        return (self.config.response.notice if self.config.response else None) or self.NOTICE_PATHS

    def price_divisor(self) -> float:
        return self.config.response.price_divisor if self.config.response else 1.0


# The names an ``adapter:`` line may use: the class name or a short alias.
ADAPTERS: dict[str, type[BaseTemuProvider]] = {
    "TemuProductScraperAdapter": TemuProductScraperAdapter, "temu_product_scraper": TemuProductScraperAdapter,
    "TemuScraper2Adapter": TemuScraper2Adapter, "temu_scraper2": TemuScraper2Adapter,
    "TemuApiRealtimeAdapter": TemuApiRealtimeAdapter, "temu_api_realtime": TemuApiRealtimeAdapter,
    "GenericConfigurableTemuAdapter": GenericConfigurableTemuAdapter, "generic": GenericConfigurableTemuAdapter,
}
