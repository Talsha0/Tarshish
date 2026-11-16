"""SQLite persistence: schema, WAL-mode connections and the engine's queries.

Design notes
------------
* Every connection enforces ``PRAGMA journal_mode=WAL`` so the CLI, the queue
  worker and the dashboard can read and write concurrently without lock errors.
* One short-lived connection per operation keeps the class safe to share
  between threads (Streamlit reruns, worker loop) and processes.
* Timestamps are stored as UTC ISO-8601 text, which sorts chronologically.
* ``ai_usage`` is an append-only ledger of LLM spend. The budget guardrail sums
  it instead of ``publications.generation_cost_usd`` so that flushing the queue
  (or generating content that never gets a publication row) cannot erase money
  that was really spent.
* The same file serves the public storefront. ``products`` carries the listing
  details the storefront shows (category, discount, coupon, flash-sale flag…),
  added by ``_migrate`` so that databases created before the storefront existed
  are upgraded in place on the next start. ``click_events`` records every
  confirmed outbound click.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from core.models import GeneratedContent, Product, PublicationStatus, utcnow
from core.resilience import PipelineError, get_logger

logger = get_logger("database")

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS pipelines (
    id                TEXT PRIMARY KEY,
    type              TEXT NOT NULL,                      -- niche / category
    language          TEXT NOT NULL,
    source_plugin     TEXT NOT NULL,
    generator_plugin  TEXT NOT NULL,
    llm_model         TEXT NOT NULL DEFAULT 'mock',
    publishing_mode   TEXT NOT NULL DEFAULT 'immediate' CHECK (publishing_mode IN ('immediate', 'queued')),
    social_pages_json TEXT NOT NULL DEFAULT '[]',         -- env-var NAMES only, never secrets
    filters_json      TEXT NOT NULL DEFAULT '{}',
    options_json      TEXT NOT NULL DEFAULT '{}',         -- prompt template, per-run limit, plugin options
    enabled           INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    external_id    TEXT NOT NULL,
    source         TEXT NOT NULL,
    canonical_hash TEXT NOT NULL,
    niche          TEXT NOT NULL,
    title          TEXT NOT NULL,
    price          REAL NOT NULL,
    currency       TEXT NOT NULL DEFAULT 'ILS',
    affiliate_url  TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    UNIQUE (source, external_id)
);
CREATE INDEX IF NOT EXISTS idx_products_canonical_hash ON products (canonical_hash);

CREATE TABLE IF NOT EXISTS publications (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    pipeline_id         TEXT NOT NULL REFERENCES pipelines (id),
    product_id          INTEGER NOT NULL REFERENCES products (id),
    platform            TEXT NOT NULL,
    target_page         TEXT NOT NULL DEFAULT 'default',
    content_type        TEXT NOT NULL CHECK (content_type IN ('text', 'video')),
    status              TEXT NOT NULL CHECK (status IN ('PENDING', 'QUEUED', 'PUBLISHED', 'FAILED')),
    caption             TEXT NOT NULL,
    media_uri           TEXT,
    model_used          TEXT NOT NULL,
    generation_cost_usd REAL NOT NULL DEFAULT 0,          -- this row's share of the generation cost
    platform_post_id    TEXT,
    scheduled_for       TEXT,
    published_at        TEXT,
    error_message       TEXT,
    content_json        TEXT NOT NULL,                    -- full GeneratedContent snapshot for replay
    target_json         TEXT NOT NULL DEFAULT '{}',       -- social page (env-var names only)
    attempts            INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_publications_queue    ON publications (status, scheduled_for);
CREATE INDEX IF NOT EXISTS idx_publications_pipeline ON publications (pipeline_id, product_id);
CREATE INDEX IF NOT EXISTS idx_publications_target   ON publications (platform, target_page, status);

CREATE TABLE IF NOT EXISTS metrics_snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    publication_id INTEGER NOT NULL REFERENCES publications (id) ON DELETE CASCADE,
    views          INTEGER NOT NULL DEFAULT 0,            -- cumulative counters at recorded_at
    clicks         INTEGER NOT NULL DEFAULT 0,
    conversions    INTEGER NOT NULL DEFAULT 0,
    revenue_usd    REAL NOT NULL DEFAULT 0,
    recorded_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_metrics_publication ON metrics_snapshots (publication_id, recorded_at);

CREATE TABLE IF NOT EXISTS ai_usage (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    pipeline_id         TEXT NOT NULL,
    product_ref         TEXT NOT NULL,                    -- "<source>:<external_id>"
    model_used          TEXT NOT NULL,
    tokens_input        INTEGER NOT NULL DEFAULT 0,
    tokens_output       INTEGER NOT NULL DEFAULT 0,
    generation_cost_usd REAL NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_usage_created ON ai_usage (created_at);
"""

# Storefront columns of ``products``, added with ALTER TABLE when missing (name -> definition).
# SQLite can only add a NOT NULL column when it has a default, hence the defaults below.
PRODUCT_STOREFRONT_COLUMNS: dict[str, str] = {
    "category": "TEXT",                             # storefront shelf, e.g. 'sports'; NULL = not listed
    "title_he": "TEXT",                             # Hebrew title for the RTL storefront
    "original_price": "REAL",
    "discount_percent": "INTEGER",
    "rating": "REAL",
    "orders_count": "INTEGER",
    "image_urls_json": "TEXT NOT NULL DEFAULT '[]'",
    "coupon_code": "TEXT",
    "is_flash_sale": "INTEGER NOT NULL DEFAULT 0",
    "stock_urgency_label": "TEXT",                  # e.g. 'Limited Stock', 'Top 1% Deal'
}

# Listing details a caller may set explicitly through ``upsert_product(listing=...)``.
LISTING_COLUMNS = ("category", "title_he", "coupon_code", "is_flash_sale", "stock_urgency_label")

# Runs after the column migration: it indexes columns that older databases only just gained.
STOREFRONT_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_products_category ON products (category);

CREATE TABLE IF NOT EXISTS click_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id    INTEGER NOT NULL REFERENCES products (id),
    category      TEXT NOT NULL,
    source        TEXT NOT NULL,
    language      TEXT NOT NULL,
    referrer_path TEXT NOT NULL DEFAULT '',          -- storefront page the click came from
    clicked_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_click_events_time     ON click_events (clicked_at);
CREATE INDEX IF NOT EXISTS idx_click_events_product  ON click_events (product_id, clicked_at);
CREATE INDEX IF NOT EXISTS idx_click_events_category ON click_events (category, source);
"""

# ``sort_by`` values of the storefront API -> ORDER BY (a fixed whitelist, never user text).
STOREFRONT_ORDER: dict[str, str] = {
    "recommended": "is_flash_sale DESC, COALESCE(discount_percent, 0) DESC, orders_count DESC, id",
    "rating_desc": "rating DESC, orders_count DESC, id",
    "price_asc": "price ASC, id",
    "price_desc": "price DESC, id",
    "orders_desc": "orders_count DESC, rating DESC, id",
    "discount_desc": "COALESCE(discount_percent, 0) DESC, orders_count DESC, id",
}

_STOREFRONT_COLUMNS = (
    "id, external_id, source, category, title, title_he, price, original_price, discount_percent, currency, "
    "rating, orders_count, image_urls_json, coupon_code, is_flash_sale, stock_urgency_label, created_at"
)

_ACTIVE = (PublicationStatus.PENDING.value, PublicationStatus.QUEUED.value)


def category_slug(value: str) -> str:
    """URL-safe storefront category: 'Running & Sports' -> 'running-sports'."""
    return re.sub(r"[\W_]+", "-", value.casefold()).strip("-")


def to_iso(moment: datetime) -> str:
    """UTC ISO-8601 with second precision — the single on-disk timestamp format."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def from_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class Database:
    """Thin, explicit data-access layer over the engine's SQLite file."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    # -- connections ------------------------------------------------------- #
    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Open a WAL-mode connection; commit on success, roll back on error."""
        conn = sqlite3.connect(str(self.path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            conn.execute("PRAGMA busy_timeout=30000;")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        """Create the schema (idempotent) and verify WAL mode is active."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
            if str(mode).lower() != "wal":
                logger.warning("SQLite journal mode is %r, expected 'wal' — concurrent writers may block.", mode)
            conn.executescript(SCHEMA)
            self._migrate(conn)
            conn.executescript(STOREFRONT_SCHEMA)
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION};")

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Add the storefront columns to a ``products`` table that predates them.

        Additive and idempotent: existing rows and columns are never rewritten. Rows that
        were stored before the migration keep ``category`` NULL, so they stay off the
        storefront until a pipeline run or the seeder upserts them again.
        """
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(products)")}
        for name, definition in PRODUCT_STOREFRONT_COLUMNS.items():
            if name in existing:
                continue
            try:
                conn.execute(f"ALTER TABLE products ADD COLUMN {name} {definition}")
            except sqlite3.OperationalError as exc:
                # Another process (API, dashboard, CLI) started at the same moment and won the race.
                if "duplicate column name" not in str(exc).lower():
                    raise
            else:
                logger.info("Database migration: added products.%s.", name)

    def query(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> list[dict[str, Any]]:
        """Run a read query and return plain dict rows (used by the dashboard layer)."""
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def query_one(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def _execute(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> int:
        with self.connect() as conn:
            return conn.execute(sql, params).rowcount

    # -- pipelines ---------------------------------------------------------- #
    def count_pipelines(self) -> int:
        return int(self.query_one("SELECT COUNT(*) AS n FROM pipelines")["n"])  # type: ignore[index]

    def list_pipelines(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        where = "WHERE enabled = 1" if enabled_only else ""
        return self.query(f"SELECT * FROM pipelines {where} ORDER BY created_at, id")

    def get_pipeline(self, pipeline_id: str) -> dict[str, Any] | None:
        return self.query_one("SELECT * FROM pipelines WHERE id = ?", (pipeline_id,))

    def upsert_pipeline(self, record: Mapping[str, Any]) -> None:
        """Insert or update a pipeline row (``created_at`` is preserved on update)."""
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO pipelines (id, type, language, source_plugin, generator_plugin, llm_model,
                                       publishing_mode, social_pages_json, filters_json, options_json,
                                       enabled, created_at)
                VALUES (:id, :type, :language, :source_plugin, :generator_plugin, :llm_model,
                        :publishing_mode, :social_pages_json, :filters_json, :options_json,
                        :enabled, :created_at)
                ON CONFLICT (id) DO UPDATE SET
                    type = excluded.type,
                    language = excluded.language,
                    source_plugin = excluded.source_plugin,
                    generator_plugin = excluded.generator_plugin,
                    llm_model = excluded.llm_model,
                    publishing_mode = excluded.publishing_mode,
                    social_pages_json = excluded.social_pages_json,
                    filters_json = excluded.filters_json,
                    options_json = excluded.options_json,
                    enabled = excluded.enabled
                """,
                {**record, "created_at": to_iso(utcnow())},
            )

    def set_pipeline_enabled(self, pipeline_id: str, enabled: bool) -> bool:
        return self._execute("UPDATE pipelines SET enabled = ? WHERE id = ?", (int(enabled), pipeline_id)) > 0

    def delete_pipeline(self, pipeline_id: str) -> bool:
        """Delete a pipeline that has no publication history (history is never orphaned)."""
        try:
            return self._execute("DELETE FROM pipelines WHERE id = ?", (pipeline_id,)) > 0
        except sqlite3.IntegrityError as exc:
            raise PipelineError(
                f"Pipeline '{pipeline_id}' has publication history and cannot be deleted — disable it instead."
            ) from exc

    # -- products ----------------------------------------------------------- #
    def upsert_product(self, product: Product, listing: Mapping[str, Any] | None = None) -> int:
        """Insert or refresh a product and return its id.

        Everything a source reports (price, rating, orders, images…) is refreshed on every
        call. ``listing`` carries the storefront details a source does not know (see
        ``LISTING_COLUMNS``); only the keys it names are written, so a plain pipeline upsert
        never wipes a coupon or a category that was set earlier. A new product without an
        explicit category is filed under its pipeline's product type.
        """
        listing = dict(listing or {})
        unknown = sorted(set(listing) - set(LISTING_COLUMNS))
        if unknown:
            raise ValueError(f"unknown listing field(s): {', '.join(unknown)}")
        values = {
            "external_id": product.external_id, "source": product.source,
            "canonical_hash": product.canonical_hash, "niche": product.niche, "title": product.title,
            "price": product.price, "currency": product.currency, "affiliate_url": product.affiliate_url,
            "original_price": product.original_price, "discount_percent": product.discount_percent,
            "rating": product.rating, "orders_count": product.orders_count,
            "image_urls_json": json.dumps(product.image_urls),
            "category": category_slug(str(listing.get("category") or product.niche)),
            "title_he": listing.get("title_he"), "coupon_code": listing.get("coupon_code"),
            "is_flash_sale": int(bool(listing.get("is_flash_sale", False))),
            "stock_urgency_label": listing.get("stock_urgency_label"),
            "created_at": to_iso(utcnow()),
        }
        refreshed = [
            "canonical_hash", "niche", "title", "price", "currency", "affiliate_url", "original_price",
            "discount_percent", "rating", "orders_count", "image_urls_json", *listing,
        ]
        updates = [f"{name} = excluded.{name}" for name in refreshed]
        if "category" not in listing:
            updates.append("category = COALESCE(NULLIF(products.category, ''), excluded.category)")
        with self.connect() as conn:
            row = conn.execute(
                f"""
                INSERT INTO products ({', '.join(values)})
                VALUES ({', '.join(f':{name}' for name in values)})
                ON CONFLICT (source, external_id) DO UPDATE SET {', '.join(updates)}
                RETURNING id
                """,
                values,
            ).fetchone()
            return int(row["id"])

    # -- storefront ----------------------------------------------------------- #
    @staticmethod
    def _storefront_filter(
        *, category: str | None = None, store: str | None = None, min_rating: float = 0.0,
        min_orders: int = 0, min_price: float | None = None, max_price: float | None = None,
    ) -> tuple[str, list[Any]]:
        """WHERE clause + parameters selecting listed products that pass the given filters."""
        clauses = ["category IS NOT NULL", "category != ''", "rating >= ?", "orders_count >= ?"]
        params: list[Any] = [min_rating, min_orders]
        if category:
            clauses.append("category = ?")
            params.append(category)
        if store:
            # 'aliexpress' matches the real plugin and its variants ('aliexpress_mock').
            escaped = store.replace("!", "!!").replace("%", "!%").replace("_", "!_")
            clauses.append("(source = ? OR source LIKE ? ESCAPE '!')")
            params.extend([store, f"{escaped}!_%"])
        if min_price is not None:
            clauses.append("price >= ?")
            params.append(min_price)
        if max_price is not None:
            clauses.append("price <= ?")
            params.append(max_price)
        return " AND ".join(clauses), params

    def list_storefront_products(
        self, *, sort_by: str = "recommended", limit: int | None = None, offset: int = 0, **filters: Any,
    ) -> tuple[list[dict[str, Any]], int]:
        """One page of listed products plus the number of products that match the filters."""
        where, params = self._storefront_filter(**filters)
        order = STOREFRONT_ORDER[sort_by]
        page = "" if limit is None else " LIMIT ? OFFSET ?"
        page_params = [] if limit is None else [limit, offset]
        with self.connect() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM products WHERE {where}", params).fetchone()[0]
            rows = conn.execute(
                f"SELECT {_STOREFRONT_COLUMNS} FROM products WHERE {where} ORDER BY {order}{page}",
                [*params, *page_params],
            ).fetchall()
        return [dict(row) for row in rows], int(total)

    def get_storefront_product(
        self, product_id: int, *, min_rating: float = 0.0, min_orders: int = 0
    ) -> dict[str, Any] | None:
        """A listed product including its ``affiliate_url``; ``None`` when missing or not listed."""
        where, params = self._storefront_filter(min_rating=min_rating, min_orders=min_orders)
        return self.query_one(
            f"SELECT {_STOREFRONT_COLUMNS}, affiliate_url FROM products WHERE id = ? AND {where}",
            [product_id, *params],
        )

    def storefront_category_stats(self, *, min_rating: float = 0.0, min_orders: int = 0) -> list[dict[str, Any]]:
        """Per category: product count, best rating and highest order count (largest category first)."""
        where, params = self._storefront_filter(min_rating=min_rating, min_orders=min_orders)
        return self.query(
            f"""
            SELECT category, COUNT(*) AS products, MAX(rating) AS top_rating, MAX(orders_count) AS top_orders
            FROM products WHERE {where}
            GROUP BY category ORDER BY products DESC, category
            """,
            params,
        )

    def storefront_facets(self, **filters: Any) -> dict[str, Any]:
        """Stores and price bounds available inside a scope (used to draw the filter controls)."""
        where, params = self._storefront_filter(**filters)
        sources = self.query(
            f"SELECT source, COUNT(*) AS products FROM products WHERE {where} GROUP BY source ORDER BY source", params
        )
        bounds = self.query_one(f"SELECT MIN(price) AS low, MAX(price) AS high FROM products WHERE {where}", params)
        return {"sources": sources, "price_min": (bounds or {}).get("low"), "price_max": (bounds or {}).get("high")}

    def record_click_event(
        self, product_id: int, *, category: str, source: str, language: str, referrer_path: str = "",
        clicked_at: datetime | None = None,
    ) -> int:
        """Log one confirmed outbound click from the storefront."""
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO click_events (product_id, category, source, language, referrer_path, clicked_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (product_id, category, source, language, referrer_path, to_iso(clicked_at or utcnow())),
            )
            return int(cursor.lastrowid)  # type: ignore[arg-type]

    def click_counts_since(self, since: datetime) -> dict[int, int]:
        """Outbound clicks per product from ``since`` until now."""
        rows = self.query(
            "SELECT product_id, COUNT(*) AS clicks FROM click_events WHERE clicked_at >= ? GROUP BY product_id",
            (to_iso(since),),
        )
        return {int(row["product_id"]): int(row["clicks"]) for row in rows}

    # -- publications ------------------------------------------------------- #
    def handled_targets(self, pipeline_id: str, canonical_hash: str) -> set[tuple[str, str]]:
        """(platform, page) pairs that already hold this product for this pipeline.

        Matching on ``canonical_hash`` de-duplicates the same item across sources.
        """
        rows = self.query(
            """
            SELECT DISTINCT pub.platform, pub.target_page
            FROM publications pub JOIN products p ON p.id = pub.product_id
            WHERE pub.pipeline_id = ? AND p.canonical_hash = ?
            """,
            (pipeline_id, canonical_hash),
        )
        return {(row["platform"], row["target_page"]) for row in rows}

    def insert_publication(
        self,
        *,
        pipeline_id: str,
        product_id: int,
        platform: str,
        target_page: str,
        target_json: str,
        content: GeneratedContent,
        status: PublicationStatus,
        cost_share_usd: float,
        scheduled_for: datetime | None = None,
    ) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO publications (pipeline_id, product_id, platform, target_page, content_type, status,
                                          caption, media_uri, model_used, generation_cost_usd, scheduled_for,
                                          content_json, target_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pipeline_id, product_id, platform, target_page, content.content_type, status.value,
                    content.caption, content.media_public_url or content.media_local_path, content.model_used,
                    cost_share_usd, to_iso(scheduled_for) if scheduled_for else None,
                    content.model_dump_json(), target_json, to_iso(utcnow()),
                ),
            )
            return int(cursor.lastrowid)  # type: ignore[arg-type]

    def mark_published(self, publication_id: int, platform_post_id: str, published_at: datetime) -> None:
        self._execute(
            "UPDATE publications SET status = 'PUBLISHED', platform_post_id = ?, published_at = ?, "
            "error_message = NULL WHERE id = ?",
            (platform_post_id, to_iso(published_at), publication_id),
        )

    def mark_failed(self, publication_id: int, error_message: str) -> None:
        self._execute(
            "UPDATE publications SET status = 'FAILED', error_message = ? WHERE id = ?",
            (error_message[:2000], publication_id),
        )

    def claim_publication(self, publication_id: int) -> bool:
        """Atomically move QUEUED -> PENDING; False when another worker got there first."""
        return (
            self._execute(
                "UPDATE publications SET status = 'PENDING', attempts = attempts + 1 "
                "WHERE id = ? AND status = 'QUEUED'",
                (publication_id,),
            )
            == 1
        )

    def reschedule_publication(self, publication_id: int, when: datetime) -> None:
        self._execute("UPDATE publications SET scheduled_for = ? WHERE id = ?", (to_iso(when), publication_id))

    def fetch_due_publications(self, now: datetime, limit: int = 20) -> list[dict[str, Any]]:
        """QUEUED rows whose slot has arrived, oldest slot first (disabled pipelines are paused)."""
        return self.query(
            """
            SELECT pub.* FROM publications pub JOIN pipelines pl ON pl.id = pub.pipeline_id
            WHERE pub.status = 'QUEUED' AND pl.enabled = 1
              AND (pub.scheduled_for IS NULL OR pub.scheduled_for <= ?)
            ORDER BY COALESCE(pub.scheduled_for, pub.created_at), pub.id
            LIMIT ?
            """,
            (to_iso(now), limit),
        )

    def last_published_at(self, platform: str, target_page: str) -> datetime | None:
        row = self.query_one(
            "SELECT MAX(published_at) AS ts FROM publications "
            "WHERE platform = ? AND target_page = ? AND status = 'PUBLISHED'",
            (platform, target_page),
        )
        return from_iso(row["ts"]) if row else None

    def last_activity_at(self, platform: str, target_page: str) -> datetime | None:
        """Latest moment a page was (or is scheduled to be) posted to."""
        row = self.query_one(
            """
            SELECT MAX(ts) AS ts FROM (
                SELECT MAX(published_at) AS ts FROM publications
                 WHERE platform = ? AND target_page = ? AND status = 'PUBLISHED'
                UNION ALL
                SELECT MAX(scheduled_for) FROM publications
                 WHERE platform = ? AND target_page = ? AND status IN ('PENDING', 'QUEUED')
            )
            """,
            (platform, target_page, platform, target_page),
        )
        return from_iso(row["ts"]) if row else None

    def list_publications(
        self,
        statuses: Sequence[str] | None = None,
        pipeline_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if statuses:
            clauses.append(f"pub.status IN ({', '.join('?' for _ in statuses)})")
            params.extend(statuses)
        if pipeline_id:
            clauses.append("pub.pipeline_id = ?")
            params.append(pipeline_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return self.query(
            f"""
            SELECT pub.id, pub.pipeline_id, pub.platform, pub.target_page, pub.content_type, pub.status,
                   p.title AS product_title, pub.caption, pub.media_uri, pub.model_used, pub.generation_cost_usd,
                   pub.platform_post_id, pub.scheduled_for, pub.published_at, pub.error_message, pub.attempts,
                   pub.created_at
            FROM publications pub JOIN products p ON p.id = pub.product_id
            {where}
            ORDER BY pub.id DESC
            LIMIT ?
            """,
            (*params, limit),
        )

    def retry_failed(self, publication_ids: Sequence[int] | None = None, pipeline_id: str | None = None) -> int:
        """Put FAILED rows back in the queue (due immediately; the worker paces them)."""
        clauses = ["status = 'FAILED'"]
        params: list[Any] = [to_iso(utcnow())]
        if publication_ids is not None:
            if not publication_ids:
                return 0
            clauses.append(f"id IN ({', '.join('?' for _ in publication_ids)})")
            params.extend(publication_ids)
        if pipeline_id:
            clauses.append("pipeline_id = ?")
            params.append(pipeline_id)
        return self._execute(
            f"UPDATE publications SET status = 'QUEUED', error_message = NULL, scheduled_for = ? "
            f"WHERE {' AND '.join(clauses)}",
            params,
        )

    def flush_queue(self, pipeline_id: str | None = None) -> int:
        """Delete PENDING/QUEUED rows. AI spend already incurred stays in ``ai_usage``."""
        sql = "DELETE FROM publications WHERE status IN ('PENDING', 'QUEUED')"
        params: tuple[Any, ...] = ()
        if pipeline_id:
            sql += " AND pipeline_id = ?"
            params = (pipeline_id,)
        return self._execute(sql, params)

    def fail_stale_pending(self, older_than: datetime) -> int:
        """Rows stuck in PENDING (process died mid-publish) become FAILED for human review."""
        return self._execute(
            "UPDATE publications SET status = 'FAILED', error_message = ? "
            "WHERE status = 'PENDING' AND created_at < ? AND (scheduled_for IS NULL OR scheduled_for < ?)",
            (
                "Interrupted mid-publish — check the platform for a duplicate before retrying.",
                to_iso(older_than), to_iso(older_than),
            ),
        )

    def active_media_uris(self) -> set[str]:
        """Media still needed by unpublished rows (protected from storage cleanup)."""
        rows = self.query(
            "SELECT DISTINCT media_uri FROM publications WHERE media_uri IS NOT NULL AND status IN (?, ?)", _ACTIVE
        )
        return {row["media_uri"] for row in rows}

    def get_status_counts(self, pipeline_id: str | None = None) -> dict[str, int]:
        counts = {status.value: 0 for status in PublicationStatus}
        where, params = ("WHERE pipeline_id = ?", (pipeline_id,)) if pipeline_id else ("", ())
        for row in self.query(f"SELECT status, COUNT(*) AS n FROM publications {where} GROUP BY status", params):
            counts[row["status"]] = int(row["n"])
        return counts

    # -- AI spend ledger ------------------------------------------------------ #
    def record_ai_usage(
        self, *, pipeline_id: str, product_ref: str, model_used: str,
        tokens_input: int, tokens_output: int, cost_usd: float,
    ) -> None:
        self._execute(
            "INSERT INTO ai_usage (pipeline_id, product_ref, model_used, tokens_input, tokens_output, "
            "generation_cost_usd, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (pipeline_id, product_ref, model_used, tokens_input, tokens_output, cost_usd, to_iso(utcnow())),
        )

    def get_monthly_ai_spend(self, now: datetime | None = None) -> float:
        """Sum of ``generation_cost_usd`` for the current calendar month (UTC)."""
        now = (now or utcnow()).astimezone(timezone.utc)
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
        row = self.query_one(
            "SELECT COALESCE(SUM(generation_cost_usd), 0) AS total FROM ai_usage "
            "WHERE created_at >= ? AND created_at < ?",
            (to_iso(start), to_iso(end)),
        )
        return float(row["total"]) if row else 0.0

    # -- metrics -------------------------------------------------------------- #
    def record_metrics_snapshot(
        self, publication_id: int, *, views: int = 0, clicks: int = 0, conversions: int = 0,
        revenue_usd: float = 0.0, recorded_at: datetime | None = None,
    ) -> int:
        """Store cumulative counters for a publication (the latest snapshot wins in KPIs)."""
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO metrics_snapshots (publication_id, views, clicks, conversions, revenue_usd, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (publication_id, views, clicks, conversions, revenue_usd, to_iso(recorded_at or utcnow())),
            )
            return int(cursor.lastrowid)  # type: ignore[arg-type]
