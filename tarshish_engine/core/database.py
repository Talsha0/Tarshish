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
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from core.models import GeneratedContent, Product, PublicationStatus, utcnow
from core.resilience import PipelineError, get_logger

logger = get_logger("database")

SCHEMA_VERSION = 1

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

_ACTIVE = (PublicationStatus.PENDING.value, PublicationStatus.QUEUED.value)


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
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION};")

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
    def upsert_product(self, product: Product) -> int:
        with self.connect() as conn:
            row = conn.execute(
                """
                INSERT INTO products (external_id, source, canonical_hash, niche, title, price, currency,
                                      affiliate_url, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (source, external_id) DO UPDATE SET
                    canonical_hash = excluded.canonical_hash,
                    niche = excluded.niche,
                    title = excluded.title,
                    price = excluded.price,
                    currency = excluded.currency,
                    affiliate_url = excluded.affiliate_url
                RETURNING id
                """,
                (
                    product.external_id, product.source, product.canonical_hash, product.niche, product.title,
                    product.price, product.currency, product.affiliate_url, to_iso(utcnow()),
                ),
            ).fetchone()
            return int(row["id"])

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
