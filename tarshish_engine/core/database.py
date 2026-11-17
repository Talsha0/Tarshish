"""Persistence: schema, connections and the engine's queries, on SQLite or PostgreSQL.

Design notes
------------
* Two backends share every query. ``Database("path/to/file.db")`` is SQLite (local
  development, tests); ``Database("postgresql://…")`` is PostgreSQL — Supabase in
  production. Queries are written once with ``?`` / ``:name`` placeholders and portable
  SQL; on PostgreSQL the placeholders are translated to psycopg's ``%s`` / ``%(name)s``.
* PostgreSQL connections come from a small per-process pool, so a serverless function
  (the storefront API on Vercel) reuses its connections across warm invocations. Server-side
  prepared statements are off, which Supabase's transaction pooler (port 6543) requires.
  Its schema lives in ``core/schema_postgres.sql`` and is applied on the first connect.
* On SQLite, every connection enforces ``PRAGMA journal_mode=WAL`` so the CLI, the queue
  worker and the dashboard can read and write concurrently without lock errors.
* One short-lived connection per operation keeps the class safe to share
  between threads (Streamlit reruns, worker loop) and processes.
* Timestamps are stored as UTC ISO-8601 text, which sorts chronologically.
* Configuration is normalized into three tables seeded from ``config/*.yaml``:
  ``categories`` (language-agnostic search rules), ``content_profiles`` (generator +
  LLM model per content type) and ``social_accounts`` (the category × language ×
  platform matrix). Products are harvested once per (category, source) into
  ``products``; publishing pipelines — a (category, language) pair, id
  ``<category>_<language>`` — read them back and record one ``publications`` row per
  (product, account, content type).
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

import atexit
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from core.models import GeneratedContent, Product, PublicationStatus, category_slug, utcnow
from core.resilience import get_logger

logger = get_logger("database")

SCHEMA_VERSION = 4

SCHEMA = """
CREATE TABLE IF NOT EXISTS categories (
    category_id            TEXT PRIMARY KEY,               -- URL slug, e.g. 'gadgets'
    display_name_en        TEXT NOT NULL,
    display_name_he        TEXT NOT NULL DEFAULT '',
    temu_search_json       TEXT,                           -- {"keyword": ..., "l2Category": ..., "priceMin": ...}
    aliexpress_search_json TEXT,
    min_rating             REAL NOT NULL DEFAULT 0,
    min_orders             INTEGER NOT NULL DEFAULT 0,
    enabled                INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS content_profiles (
    content_type    TEXT PRIMARY KEY,                      -- instagram_post | instagram_story | tiktok_video ...
    platform        TEXT NOT NULL,                         -- instagram | tiktok | telegram ...
    generator       TEXT NOT NULL,                         -- generator plugin
    llm_model       TEXT NOT NULL DEFAULT 'mock',
    publish_format  TEXT,                                  -- image | reel | story ... (publisher option)
    tone            TEXT,
    prompt_template TEXT,
    enabled         INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS social_accounts (
    category_id        TEXT NOT NULL,
    language           TEXT NOT NULL,
    platform           TEXT NOT NULL,
    account_handle     TEXT NOT NULL,
    token_env_var      TEXT,                               -- env-var NAMES only, never secrets
    account_id_env_var TEXT,
    platform_metadata_json TEXT,                           -- non-secret platform ids (app_id, chat_id …)
    enabled            INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at         TEXT NOT NULL,
    PRIMARY KEY (category_id, language, platform)
);

CREATE TABLE IF NOT EXISTS products (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    external_id    TEXT NOT NULL,
    source         TEXT NOT NULL,
    canonical_hash TEXT NOT NULL,
    category       TEXT,                                   -- harvesting category id; NULL = not listed
    title          TEXT NOT NULL,
    price          REAL NOT NULL,
    currency       TEXT NOT NULL DEFAULT 'ILS',
    affiliate_url  TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    UNIQUE (source, external_id)
);
CREATE INDEX IF NOT EXISTS idx_products_canonical_hash ON products (canonical_hash);
"""

PUBLICATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS publications (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    pipeline_id         TEXT NOT NULL,                    -- publishing pipeline '<category>_<language>'
    product_id          INTEGER NOT NULL REFERENCES products (id),
    category            TEXT,                             -- NULL only on rows from before schema v3
    language            TEXT,
    content_format      TEXT,                             -- content profile: instagram_post, instagram_story ...
    account_platform    TEXT,                             -- social_accounts.platform (instagram ...)
    platform            TEXT NOT NULL,                    -- publisher plugin (instagram_pub ...)
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
"""

PUBLICATION_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_publications_queue    ON publications (status, scheduled_for);
CREATE INDEX IF NOT EXISTS idx_publications_pipeline ON publications (pipeline_id, product_id);
CREATE INDEX IF NOT EXISTS idx_publications_target   ON publications (platform, target_page, status);
CREATE INDEX IF NOT EXISTS idx_publications_account
    ON publications (category, language, account_platform, content_format, product_id);
"""

LEDGER_SCHEMA = """
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
    pipeline_id         TEXT NOT NULL,                    -- '<category>_<language>' (or 'harvest')
    product_ref         TEXT NOT NULL,                    -- "<source>:<external_id>"
    model_used          TEXT NOT NULL,
    tokens_input        INTEGER NOT NULL DEFAULT 0,
    tokens_output       INTEGER NOT NULL DEFAULT 0,
    generation_cost_usd REAL NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_usage_created ON ai_usage (created_at);
"""

# Columns added to ``publications`` by schema v3 (copied as NULL from older rows).
PUBLICATION_V3_COLUMNS = ("category", "language", "content_format", "account_platform")

# Storefront columns of ``products``, added with ALTER TABLE when missing (name -> definition).
# SQLite can only add a NOT NULL column when it has a default, hence the defaults below.
PRODUCT_STOREFRONT_COLUMNS: dict[str, str] = {
    "category": "TEXT",                             # harvesting category / storefront shelf; NULL = not listed
    "raw_url": "TEXT",                              # plain product URL (affiliate_url carries the tracking)
    "video_url": "TEXT",
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

# Background jobs started from the web admin (harvest, publish, verify …) and their console output.
JOBS_SCHEMA = """
CREATE TABLE IF NOT EXISTS job_runs (
    id           TEXT PRIMARY KEY,                    -- random hex id handed to the caller
    kind         TEXT NOT NULL,                       -- harvest | publish | verify | seed | queue_worker ...
    params_json  TEXT NOT NULL DEFAULT '{}',
    argv_json    TEXT NOT NULL DEFAULT '[]',          -- the equivalent `python main.py ...` arguments
    status       TEXT NOT NULL CHECK (status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED')),
    exit_code    INTEGER,
    log_text     TEXT NOT NULL DEFAULT '',            -- console output, already redacted
    error        TEXT,
    created_at   TEXT NOT NULL,
    started_at   TEXT,
    finished_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_job_runs_created ON job_runs (created_at);
"""

# Columns added to configuration tables after their first release (table -> {name: definition}).
CONFIG_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "social_accounts": {"platform_metadata_json": "TEXT"},
}

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

POSTGRES_SCHEMA_FILE = Path(__file__).with_name("schema_postgres.sql")
_POSTGRES_SCHEMAS = ("postgres://", "postgresql://")
# Query-string parameters some providers append to their connection strings that libpq rejects
# (Supabase's Vercel integration adds `supa=`, Prisma-style strings add `pgbouncer=`).
_NON_LIBPQ_PARAMS = {"supa", "pgbouncer", "connection_limit", "pool_timeout"}
# Serializes concurrent first-time schema setup across processes (any constant works).
_SCHEMA_LOCK_KEY = 7_405_031_117

# Every table, parents before children (used to copy a database).
TABLES = (
    "categories", "content_profiles", "social_accounts", "products", "publications",
    "metrics_snapshots", "ai_usage", "click_events",
)
_IDENTITY_TABLES = ("products", "publications", "metrics_snapshots", "ai_usage", "click_events")


def is_postgres_url(target: str | os.PathLike[str] | None) -> bool:
    return isinstance(target, str) and target.strip().lower().startswith(_POSTGRES_SCHEMAS)


def postgres_conninfo(url: str) -> str:
    """The connection string with provider-specific, non-libpq query parameters removed."""
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() not in _NON_LIBPQ_PARAMS]
    return urlunsplit(parts._replace(query=urlencode(query)))


def database_errors() -> tuple[type[BaseException], ...]:
    """Exception types a database call can raise, for callers that degrade gracefully."""
    try:
        import psycopg
    except ImportError:  # pragma: no cover - psycopg is in requirements.txt
        return (sqlite3.Error,)
    return (sqlite3.Error, psycopg.Error)


@lru_cache(maxsize=1024)
def to_pyformat(sql: str) -> str:
    """Translate ``?`` and ``:name`` placeholders to psycopg's ``%s`` and ``%(name)s``.

    String literals, quoted identifiers and ``--`` comments are copied unchanged, ``::`` casts
    are kept, and literal ``%`` characters are doubled so psycopg does not read them as
    placeholders.
    """
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        char = sql[i]
        if char in ("'", '"'):
            end = i + 1
            while end < n:
                if sql[end] == char:
                    if end + 1 < n and sql[end + 1] == char:  # '' / "" escape
                        end += 2
                        continue
                    break
                end += 1
            out.append(sql[i:end + 1].replace("%", "%%"))
            i = end + 1
        elif char == "-" and sql.startswith("--", i):
            end = sql.find("\n", i)
            end = n if end == -1 else end
            out.append(sql[i:end].replace("%", "%%"))
            i = end
        elif char == "?":
            out.append("%s")
            i += 1
        elif char == "%":
            out.append("%%")
            i += 1
        elif char == ":" and sql.startswith("::", i):
            out.append("::")
            i += 2
        elif char == ":" and i + 1 < n and (sql[i + 1].isalpha() or sql[i + 1] == "_"):
            end = i + 1
            while end < n and (sql[end].isalnum() or sql[end] == "_"):
                end += 1
            out.append(f"%({sql[i + 1:end]})s")
            i = end
        else:
            out.append(char)
            i += 1
    return "".join(out)


def _plain(value: Any) -> Any:
    """PostgreSQL ``numeric`` results (``SUM`` of integers…) as the int/float SQLite would return."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


def _pg_row_factory(cursor: Any) -> Any:
    names = [column.name for column in cursor.description or ()]

    def make_row(values: Sequence[Any]) -> dict[str, Any]:
        return {name: _plain(value) for name, value in zip(names, values)}

    return make_row


class _PgConnection:
    """A psycopg connection that accepts the engine's ``?`` / ``:name`` SQL, like ``sqlite3``."""

    def __init__(self, raw: Any) -> None:
        self.raw = raw

    def execute(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> Any:
        cursor = self.raw.cursor()
        if params:
            cursor.execute(to_pyformat(sql), params if isinstance(params, Mapping) else list(params))
        else:
            cursor.execute(to_pyformat(sql))
        return cursor


_pools: dict[str, Any] = {}
_pools_lock = threading.Lock()


def _pool_for(url: str) -> Any:
    """One connection pool per database URL and process."""
    with _pools_lock:
        pool = _pools.get(url)
        if pool is None:
            from psycopg_pool import ConnectionPool

            pool = ConnectionPool(
                postgres_conninfo(url),
                min_size=1,
                max_size=max(1, int(os.environ.get("DATABASE_POOL_SIZE", "4") or 4)),
                # Supabase's pooler closes idle client connections; check before handing one out.
                check=ConnectionPool.check_connection,
                max_idle=120,
                timeout=15,
                kwargs={
                    "prepare_threshold": None,  # no server-side prepared statements (transaction pooler)
                    "row_factory": _pg_row_factory,
                    "connect_timeout": 10,
                    "application_name": "tarshish",
                },
                open=True,
            )
            try:
                pool.wait(timeout=15)
            except Exception:
                pool.close()
                _raise_connection_error(url)
                raise
            _pools[url] = pool
        return pool


def _raise_connection_error(url: str) -> None:
    """Surface the real reason a pool could not connect (bad password, unknown host…)."""
    import psycopg

    try:
        psycopg.connect(postgres_conninfo(url), connect_timeout=10).close()
    except psycopg.OperationalError as exc:
        raise psycopg.OperationalError(f"Cannot connect to PostgreSQL: {str(exc).strip()}") from None


def close_pools() -> None:
    """Close every PostgreSQL connection pool of this process (tests, clean shutdown)."""
    with _pools_lock:
        pools = list(_pools.values())
        _pools.clear()
    for pool in pools:
        pool.close()


atexit.register(close_pools)  # a CLI run exits at once instead of waiting on idle pool threads


def to_iso(moment: datetime) -> str:
    """UTC ISO-8601 with second precision — the single on-disk timestamp format."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def from_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def product_from_row(row: Mapping[str, Any]) -> Product:
    """Rebuild a :class:`Product` from a ``products`` row (raises ``ValueError`` when unusable)."""
    affiliate = row["affiliate_url"]
    raw_url = row.get("raw_url") or affiliate.split("?", 1)[0]  # rows harvested before raw_url existed
    return Product(
        external_id=row["external_id"], source=row["source"], canonical_hash=row["canonical_hash"],
        category=row["category"], title=row["title"], price=row["price"],
        original_price=row.get("original_price"), currency=row["currency"],
        rating=row.get("rating") or 0.0, orders_count=row.get("orders_count") or 0,
        raw_url=raw_url, affiliate_url=affiliate,
        image_urls=json.loads(row.get("image_urls_json") or "[]"), video_url=row.get("video_url"),
    )


class Database:
    """Thin, explicit data-access layer over SQLite (a file path) or PostgreSQL (a URL)."""

    def __init__(self, target: Path | str) -> None:
        self.url: str | None = target.strip() if is_postgres_url(target) else None
        self.dialect = "postgresql" if self.url else "sqlite"
        # SQLite file; for PostgreSQL a nominal path, never opened.
        self.path = Path("postgresql") if self.url else Path(target)

    @classmethod
    def from_settings(cls, settings: Any) -> "Database":
        """``DATABASE_URL`` (PostgreSQL / Supabase) when set, otherwise the ``DATABASE_PATH`` SQLite file."""
        url = getattr(settings, "database_url", None)
        if url is not None:
            return cls(url.get_secret_value() if hasattr(url, "get_secret_value") else str(url))
        return cls(settings.database_path)

    @property
    def is_postgres(self) -> bool:
        return self.dialect == "postgresql"

    def describe(self) -> str:
        """Where the data lives, safe to print (no password)."""
        if self.url:
            parts = urlsplit(self.url)
            return f"PostgreSQL at {parts.hostname or 'localhost'}{f':{parts.port}' if parts.port else ''}"
        return f"SQLite file {self.path.name}"

    def string_agg_distinct(self, expression: str) -> str:
        """SQL for a comma-separated list of the distinct values of ``expression``."""
        if self.is_postgres:
            return f"STRING_AGG(DISTINCT {expression}, ',')"
        return f"GROUP_CONCAT(DISTINCT {expression})"

    # -- connections ------------------------------------------------------- #
    @contextmanager
    def connect(self) -> Iterator[Any]:
        """A connection with ``execute(sql, params)``; commit on success, roll back on error."""
        if self.url:
            with _pool_for(self.url).connection() as raw:  # the pool commits or rolls back
                yield _PgConnection(raw)
            return
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
        """Create the schema (idempotent), upgrade older databases in place and verify WAL mode."""
        if self.is_postgres:
            self._initialize_postgres()
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._upgrade_publications()
        with self.connect() as conn:
            mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
            if str(mode).lower() != "wal":
                logger.warning("SQLite journal mode is %r, expected 'wal' — concurrent writers may block.", mode)
            conn.executescript(SCHEMA)
            conn.executescript(PUBLICATIONS_TABLE)
            conn.executescript(PUBLICATION_INDEXES)
            conn.executescript(LEDGER_SCHEMA)
            self._migrate(conn)
            conn.executescript(STOREFRONT_SCHEMA)
            conn.executescript(JOBS_SCHEMA)
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION};")

    def _postgres_schema_version(self, conn: Any) -> int:
        if not conn.execute("SELECT to_regclass('tarshish_meta') IS NOT NULL AS present").fetchone()["present"]:
            return 0
        row = conn.execute("SELECT value FROM tarshish_meta WHERE key = 'schema_version'").fetchone()
        return int(row["value"]) if row else 0

    def _initialize_postgres(self) -> None:
        """Apply ``schema_postgres.sql`` once per schema version.

        The fast path is a single query, so the serverless API can call this on every cold
        start. The first process to find an older version takes an advisory lock and applies
        the (idempotent) schema; any process that waited on the lock re-checks and skips.
        """
        with self.connect() as conn:
            if self._postgres_schema_version(conn) >= SCHEMA_VERSION:
                return
        with self.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(?)", (_SCHEMA_LOCK_KEY,))
            if self._postgres_schema_version(conn) >= SCHEMA_VERSION:
                return
            conn.raw.execute(POSTGRES_SCHEMA_FILE.read_text(encoding="utf-8"))
            conn.execute(
                "INSERT INTO tarshish_meta (key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (str(SCHEMA_VERSION),),
            )
        logger.info("Database: PostgreSQL schema v%d applied.", SCHEMA_VERSION)

    def _upgrade_publications(self) -> None:
        """Schema v2 -> v3: rebuild ``publications`` without its foreign key to the legacy ``pipelines`` table.

        Publications used to belong to a row of ``pipelines``; they now name their publishing
        pipeline (``<category>_<language>``), category, language, content profile and account
        directly. SQLite cannot drop a foreign key in place, so the table is rebuilt (the
        documented 12-step procedure) inside one transaction. Existing rows keep their ids —
        ``metrics_snapshots`` still points at them — and gain category/language from the
        legacy pipeline they belonged to. The legacy ``pipelines`` table itself is left
        untouched (and unused) so no hand-made configuration is ever lost.
        """
        conn = sqlite3.connect(str(self.path), timeout=30.0, isolation_level=None)
        try:
            conn.execute("PRAGMA busy_timeout=30000;")
            conn.create_function("category_slug", 1, lambda value: category_slug(value) if value else value)
            fks = conn.execute("PRAGMA foreign_key_list(publications)").fetchall()
            if not any(row[2] == "pipelines" for row in fks):
                return
            old_columns = [row[1] for row in conn.execute("PRAGMA table_info(publications)")]
            conn.execute("PRAGMA foreign_keys=OFF;")
            conn.execute("BEGIN IMMEDIATE;")
            try:
                # Create-copy-drop-rename (https://sqlite.org/lang_altertable.html#otheralter): renaming the
                # *old* table first would make SQLite repoint metrics_snapshots' foreign key at the copy.
                conn.execute(PUBLICATIONS_TABLE.replace("IF NOT EXISTS publications", "publications_v3"))
                shared = ", ".join(name for name in old_columns if name not in PUBLICATION_V3_COLUMNS)
                conn.execute(f"INSERT INTO publications_v3 ({shared}) SELECT {shared} FROM publications;")
                legacy = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'pipelines'"
                ).fetchone()
                if legacy:
                    conn.execute(
                        "UPDATE publications_v3 SET "
                        "category = (SELECT category_slug(type) FROM pipelines pl WHERE pl.id = publications_v3.pipeline_id), "
                        "language = (SELECT language FROM pipelines pl WHERE pl.id = publications_v3.pipeline_id)"
                    )
                conn.execute("DROP TABLE publications;")
                conn.execute("ALTER TABLE publications_v3 RENAME TO publications;")
                problems = conn.execute("PRAGMA foreign_key_check;").fetchall()
                if problems:
                    raise sqlite3.IntegrityError(f"foreign key check failed after the rebuild: {problems[:3]}")
                conn.execute("COMMIT;")
            except BaseException:
                conn.execute("ROLLBACK;")
                raise
            finally:
                conn.execute("PRAGMA foreign_keys=ON;")
            logger.info("Database migration: publications upgraded to schema v3 (decoupled from legacy pipelines).")
        finally:
            conn.close()

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Additive, idempotent column migrations of ``products``.

        Adds the storefront columns to tables that predate them, and folds the legacy
        pre-v3 category column (renamed in v3) into ``category`` before dropping it.
        """
        for table, columns in CONFIG_ADDED_COLUMNS.items():
            present = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            for name, definition in columns.items():
                if name in present:
                    continue
                try:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
                except sqlite3.OperationalError as exc:
                    if "duplicate column name" not in str(exc).lower():
                        raise
                else:
                    logger.info("Database migration: added %s.%s.", table, name)
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
        legacy = "ni" + "che"  # the pre-v3 name of ``category``
        if legacy in existing:
            rows = conn.execute(f"SELECT id, {legacy} FROM products WHERE category IS NULL OR category = ''").fetchall()
            for row in rows:
                conn.execute("UPDATE products SET category = ? WHERE id = ?", (category_slug(row[legacy]), row["id"]))
            try:
                conn.execute(f"ALTER TABLE products DROP COLUMN {legacy}")
                logger.info("Database migration: products.%s folded into products.category.", legacy)
            except sqlite3.OperationalError as exc:
                if "no such column" not in str(exc).lower():
                    raise

    def table_columns(self, table: str) -> list[dict[str, Any]]:
        """``[{'name': column}, …]`` of a table, on either database."""
        if self.is_postgres:
            return self.query(
                "SELECT column_name AS name FROM information_schema.columns "
                "WHERE table_name = ? AND table_schema = current_schema() ORDER BY ordinal_position",
                (table,),
            )
        return self.query(f"SELECT name FROM pragma_table_info('{table}')")

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

    # -- configuration: categories, content profiles, social accounts ----------- #
    # Table -> (primary-key columns, writable columns). Rows are validated by core.config models
    # before they get here; this layer only persists them.
    CONFIG_TABLES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
        "categories": (
            ("category_id",),
            ("display_name_en", "display_name_he", "temu_search_json", "aliexpress_search_json",
             "min_rating", "min_orders", "enabled"),
        ),
        "content_profiles": (
            ("content_type",),
            ("platform", "generator", "llm_model", "publish_format", "tone", "prompt_template", "enabled"),
        ),
        "social_accounts": (
            ("category_id", "language", "platform"),
            ("account_handle", "token_env_var", "account_id_env_var", "platform_metadata_json", "enabled"),
        ),
    }
    _CONFIG_ORDER = {
        "categories": "category_id",
        "content_profiles": "content_type",
        "social_accounts": "category_id, platform, language",
    }

    def _config_table(self, table: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
        try:
            return self.CONFIG_TABLES[table]
        except KeyError:
            raise ValueError(f"unknown configuration table '{table}'") from None

    def count_rows(self, table: str) -> int:
        self._config_table(table)
        return int(self.query_one(f"SELECT COUNT(*) AS n FROM {table}")["n"])  # type: ignore[index]

    def list_config(self, table: str, enabled_only: bool = False) -> list[dict[str, Any]]:
        self._config_table(table)
        where = "WHERE enabled = 1" if enabled_only else ""
        return self.query(f"SELECT * FROM {table} {where} ORDER BY {self._CONFIG_ORDER[table]}")

    def get_config(self, table: str, *key: Any) -> dict[str, Any] | None:
        keys, _ = self._config_table(table)
        where = " AND ".join(f"{name} = ?" for name in keys)
        return self.query_one(f"SELECT * FROM {table} WHERE {where}", key)

    def upsert_config(self, table: str, record: Mapping[str, Any]) -> None:
        """Insert or update one configuration row (``created_at`` is preserved on update)."""
        keys, columns = self._config_table(table)
        names = [*keys, *columns, "created_at"]
        updates = ", ".join(f"{name} = excluded.{name}" for name in columns)
        with self.connect() as conn:
            conn.execute(
                f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join(f':{n}' for n in names)}) "
                f"ON CONFLICT ({', '.join(keys)}) DO UPDATE SET {updates}",
                {**{name: record.get(name) for name in (*keys, *columns)}, "created_at": to_iso(utcnow())},
            )

    def set_config_enabled(self, table: str, key: Sequence[Any], enabled: bool) -> bool:
        keys, _ = self._config_table(table)
        where = " AND ".join(f"{name} = ?" for name in keys)
        return self._execute(f"UPDATE {table} SET enabled = ? WHERE {where}", (int(enabled), *key)) > 0

    def delete_config(self, table: str, *key: Any) -> bool:
        keys, _ = self._config_table(table)
        where = " AND ".join(f"{name} = ?" for name in keys)
        return self._execute(f"DELETE FROM {table} WHERE {where}", key) > 0

    # -- products ----------------------------------------------------------- #
    def upsert_product(self, product: Product, listing: Mapping[str, Any] | None = None) -> int:
        """Insert or refresh a product and return its id.

        Everything a source reports (price, rating, orders, images…) is refreshed on every
        call. ``listing`` carries the storefront details a source does not know (see
        ``LISTING_COLUMNS``); only the keys it names are written, so a plain pipeline upsert
        never wipes a coupon or a category that was set earlier. A new product without an
        explicit category is filed under ``product.category``.
        """
        listing = dict(listing or {})
        unknown = sorted(set(listing) - set(LISTING_COLUMNS))
        if unknown:
            raise ValueError(f"unknown listing field(s): {', '.join(unknown)}")
        values = {
            "external_id": product.external_id, "source": product.source,
            "canonical_hash": product.canonical_hash, "title": product.title,
            "raw_url": product.raw_url, "video_url": product.video_url,
            "price": product.price, "currency": product.currency, "affiliate_url": product.affiliate_url,
            "original_price": product.original_price, "discount_percent": product.discount_percent,
            "rating": product.rating, "orders_count": product.orders_count,
            "image_urls_json": json.dumps(product.image_urls),
            "category": category_slug(str(listing.get("category") or product.category)),
            "title_he": listing.get("title_he"), "coupon_code": listing.get("coupon_code"),
            "is_flash_sale": int(bool(listing.get("is_flash_sale", False))),
            "stock_urgency_label": listing.get("stock_urgency_label"),
            "created_at": to_iso(utcnow()),
        }
        refreshed = [
            "canonical_hash", "raw_url", "video_url", "title", "price", "currency", "affiliate_url", "original_price",
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

    def product_id(self, source: str, external_id: str) -> int | None:
        row = self.query_one("SELECT id FROM products WHERE source = ? AND external_id = ?", (source, external_id))
        return int(row["id"]) if row else None

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
            total = conn.execute(f"SELECT COUNT(*) AS n FROM products WHERE {where}", params).fetchone()["n"]
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
                "VALUES (?, ?, ?, ?, ?, ?) RETURNING id",
                (product_id, category, source, language, referrer_path, to_iso(clicked_at or utcnow())),
            )
            return int(cursor.fetchone()["id"])

    def click_counts_since(self, since: datetime) -> dict[int, int]:
        """Outbound clicks per product from ``since`` until now."""
        rows = self.query(
            "SELECT product_id, COUNT(*) AS clicks FROM click_events WHERE clicked_at >= ? GROUP BY product_id",
            (to_iso(since),),
        )
        return {int(row["product_id"]): int(row["clicks"]) for row in rows}

    # -- publications ------------------------------------------------------- #
    def unpublished_products(
        self, *, category: str, language: str, account_platform: str, content_format: str,
        min_rating: float = 0.0, min_orders: int = 0, limit: int = 5, include_handled: bool = False,
    ) -> list[Product]:
        """Harvested products of ``category`` that this account has not received in this format yet.

        A product counts as handled once ``publications`` holds a row for the same
        ``(language, account, content format)`` — PUBLISHED, or QUEUED/PENDING (in flight),
        or FAILED (recovered with ``--retry-failed``, never silently re-generated). The match
        is on ``canonical_hash``, so the same item harvested from two stores is offered once.
        Best sellers first. ``include_handled`` (``--force``) drops the exclusion.
        """
        exclusion = "" if include_handled else """
              AND NOT EXISTS (
                SELECT 1 FROM publications pub JOIN products seen ON seen.id = pub.product_id
                WHERE seen.canonical_hash = p.canonical_hash
                  AND pub.category = :category AND pub.language = :language
                  AND pub.account_platform = :platform AND pub.content_format = :format
              )"""
        rows = self.query(
            f"""
            SELECT p.* FROM products p
            WHERE p.category = :category AND COALESCE(p.rating, 0) >= :min_rating
              AND COALESCE(p.orders_count, 0) >= :min_orders {exclusion}
            ORDER BY COALESCE(p.orders_count, 0) DESC, COALESCE(p.rating, 0) DESC, p.id
            """,
            {"category": category_slug(category), "language": language, "platform": account_platform,
             "format": content_format, "min_rating": min_rating, "min_orders": min_orders},
        )
        products: list[Product] = []
        seen_hashes: set[str] = set()
        for row in rows:
            if row["canonical_hash"] in seen_hashes:
                continue
            try:
                products.append(product_from_row(row))
            except ValueError as exc:  # a corrupt or legacy row must not hide the healthy ones
                logger.warning("Stored product %s:%s is not usable (%s).", row["source"], row["external_id"], exc)
                continue
            seen_hashes.add(row["canonical_hash"])
            if len(products) >= limit:
                break
        return products

    def count_products(self, category: str | None = None) -> list[dict[str, Any]]:
        """Harvested products per (category, source)."""
        where, params = ("WHERE category = ?", (category_slug(category),)) if category else ("", ())
        return self.query(
            f"SELECT category, source, COUNT(*) AS products, MAX(created_at) AS last_harvest FROM products {where} "
            "GROUP BY category, source ORDER BY category, source",
            params,
        )

    def insert_publication(
        self,
        *,
        pipeline_id: str,
        product_id: int,
        platform: str,
        category: str | None = None,
        language: str | None = None,
        account_platform: str | None = None,
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
                INSERT INTO publications (pipeline_id, product_id, category, language, content_format,
                                          account_platform, platform, target_page, content_type, status,
                                          caption, media_uri, model_used, generation_cost_usd, scheduled_for,
                                          content_json, target_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                RETURNING id
                """,
                (
                    pipeline_id, product_id, category, language or content.language, content.content_format,
                    account_platform, platform, target_page, content.content_type, status.value,
                    content.caption, content.media_public_url or content.media_local_path, content.model_used,
                    cost_share_usd, to_iso(scheduled_for) if scheduled_for else None,
                    content.model_dump_json(), target_json, to_iso(utcnow()),
                ),
            )
            return int(cursor.fetchone()["id"])

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
        """QUEUED rows whose slot has arrived, oldest slot first.

        Rows of a disabled social account or category are paused (kept, not sent).
        """
        return self.query(
            """
            SELECT pub.* FROM publications pub
            LEFT JOIN social_accounts sa ON sa.category_id = pub.category AND sa.language = pub.language
                                        AND sa.platform = pub.account_platform
            LEFT JOIN categories c ON c.category_id = pub.category
            WHERE pub.status = 'QUEUED' AND COALESCE(sa.enabled, 1) = 1 AND COALESCE(c.enabled, 1) = 1
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
            ) AS activity
            """,
            (platform, target_page, platform, target_page),
        )
        return from_iso(row["ts"]) if row else None

    def list_publications(
        self,
        statuses: Sequence[str] | None = None,
        pipeline_id: str | None = None,
        limit: int = 200,
        *,
        category: str | None = None,
        language: str | None = None,
        account_platform: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (("category", category), ("language", language), ("account_platform", account_platform)):
            if value:
                clauses.append(f"pub.{column} = ?")
                params.append(value)
        if statuses:
            clauses.append(f"pub.status IN ({', '.join('?' for _ in statuses)})")
            params.extend(statuses)
        if pipeline_id:
            clauses.append("pub.pipeline_id = ?")
            params.append(pipeline_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return self.query(
            f"""
            SELECT pub.id, pub.pipeline_id, pub.category, pub.language, pub.content_format, pub.account_platform,
                   pub.platform, pub.target_page, pub.content_type, pub.status,
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

    def get_publication(self, publication_id: int) -> dict[str, Any] | None:
        return self.query_one("SELECT * FROM publications WHERE id = ?", (publication_id,))

    def delete_publication(self, publication_id: int) -> bool:
        """Delete one publication row (its metrics snapshots go with it; AI spend stays in ``ai_usage``)."""
        with self.connect() as conn:
            conn.execute("DELETE FROM metrics_snapshots WHERE publication_id = ?", (publication_id,))
            return conn.execute("DELETE FROM publications WHERE id = ?", (publication_id,)).rowcount > 0

    def requeue_now(self, publication_id: int) -> bool:
        """Make a QUEUED or FAILED row due immediately (``Publish now`` in the web admin)."""
        return self._execute(
            "UPDATE publications SET status = 'QUEUED', error_message = NULL, scheduled_for = ? "
            "WHERE id = ? AND status IN ('QUEUED', 'FAILED')",
            (to_iso(utcnow()), publication_id),
        ) > 0

    # -- background jobs (web admin) ------------------------------------------- #
    def create_job_run(self, job_id: str, kind: str, params: Mapping[str, Any], argv: Sequence[str]) -> None:
        self._execute(
            "INSERT INTO job_runs (id, kind, params_json, argv_json, status, created_at) VALUES (?, ?, ?, ?, 'QUEUED', ?)",
            (job_id, kind, json.dumps(dict(params), ensure_ascii=False, default=str),
             json.dumps(list(argv), ensure_ascii=False), to_iso(utcnow())),
        )

    def start_job_run(self, job_id: str) -> None:
        self._execute("UPDATE job_runs SET status = 'RUNNING', started_at = ? WHERE id = ?", (to_iso(utcnow()), job_id))

    def write_job_log(self, job_id: str, log_text: str) -> None:
        self._execute("UPDATE job_runs SET log_text = ? WHERE id = ?", (log_text, job_id))

    def finish_job_run(
        self, job_id: str, *, status: str, exit_code: int | None, log_text: str, error: str | None = None
    ) -> None:
        self._execute(
            "UPDATE job_runs SET status = ?, exit_code = ?, log_text = ?, error = ?, finished_at = ? WHERE id = ?",
            (status, exit_code, log_text, error, to_iso(utcnow()), job_id),
        )

    def get_job_run(self, job_id: str) -> dict[str, Any] | None:
        return self.query_one("SELECT * FROM job_runs WHERE id = ?", (job_id,))

    def list_job_runs(self, limit: int = 30, kind: str | None = None) -> list[dict[str, Any]]:
        where, params = ("WHERE kind = ?", (kind,)) if kind else ("", ())
        return self.query(
            "SELECT id, kind, params_json, argv_json, status, exit_code, error, created_at, started_at, finished_at "
            f"FROM job_runs {where} ORDER BY created_at DESC, id LIMIT ?",
            (*params, limit),
        )

    def fail_interrupted_jobs(self, older_than: datetime) -> int:
        """Jobs left QUEUED/RUNNING by a process that stopped (restart, deploy) are marked FAILED."""
        return self._execute(
            "UPDATE job_runs SET status = 'FAILED', error = ?, finished_at = ? "
            "WHERE status IN ('QUEUED', 'RUNNING') AND created_at < ?",
            ("Interrupted: the backend process stopped before the job finished.", to_iso(utcnow()), to_iso(older_than)),
        )

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
                "VALUES (?, ?, ?, ?, ?, ?) RETURNING id",
                (publication_id, views, clicks, conversions, revenue_usd, to_iso(recorded_at or utcnow())),
            )
            return int(cursor.fetchone()["id"])


def copy_database(source: Database, target: Database, *, batch_size: int = 500) -> dict[str, int]:
    """Copy every row of ``source`` into ``target`` (e.g. a local SQLite file into Supabase).

    Both databases are initialized first. Ids are preserved, so publications keep pointing at
    their products and metrics at their publications. On PostgreSQL the id sequences are then
    moved past the copied ids. Configuration rows (categories, content profiles, social accounts) of the source
    replace the target's; other rows whose key already exists in the target are skipped, so
    copy into a database that has no products or publications yet. Returns the number of rows
    read per table.
    """
    source.initialize()
    target.initialize()
    copied: dict[str, int] = {}
    with target.connect() as conn:
        for table in TABLES:
            target_columns = [row["name"] for row in target.table_columns(table)]
            rows = source.query(f"SELECT * FROM {table}")
            copied[table] = len(rows)
            if not rows:
                continue
            columns = [name for name in rows[0] if name in target_columns]
            conflict = "ON CONFLICT DO NOTHING"
            if table in Database.CONFIG_TABLES:
                # Configuration edited in the source wins over the YAML seed the target started with.
                keys, _ = Database.CONFIG_TABLES[table]
                updates = ", ".join(f"{name} = excluded.{name}" for name in columns if name not in keys)
                conflict = f"ON CONFLICT ({', '.join(keys)}) DO UPDATE SET {updates}"
            sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)}) {conflict}"
            for start in range(0, len(rows), batch_size):
                for row in rows[start:start + batch_size]:
                    conn.execute(sql, [row[name] for name in columns])
        if target.is_postgres:
            for table in _IDENTITY_TABLES:
                conn.execute(
                    f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), COALESCE(MAX(id), 1), MAX(id) IS NOT NULL) "
                    f"FROM {table}"
                )
    return copied
