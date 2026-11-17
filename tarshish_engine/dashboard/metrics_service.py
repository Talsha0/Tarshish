"""KPI data-access layer.

A UI-agnostic service: every method returns plain ``dict`` / ``list[dict]``
values that Streamlit can render today and a JSON API (for a React front end)
could return tomorrow. No Streamlit import lives here.

Metric semantics
----------------
* ``metrics_snapshots`` rows are *cumulative* counters for a publication at
  ``recorded_at``; KPIs therefore use the **latest** snapshot per publication
  (summing every snapshot would double count).
* AI spend comes from the append-only ``ai_usage`` ledger — the same figure
  the budget guardrail enforces. The per-platform view uses the cost share
  attributed to each publication row instead (a platform has no ledger entry).
* Every query runs on SQLite and on PostgreSQL: counts use ``SUM(CASE WHEN … THEN 1 ELSE 0 END)``
  rather than summing booleans, which PostgreSQL does not allow.
* Storefront clicks are a separate, exact count: one ``click_events`` row per
  confirmed outbound click on the public storefront. They are not mixed into the
  publication KPIs above, which describe posts on social platforms.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from core.config import Settings
from core.database import Database, from_iso, to_iso
from core.models import utcnow

# Latest snapshot per publication, shared by every aggregation below.
_LATEST_SNAPSHOT_CTE = """
WITH latest AS (
    SELECT publication_id, views, clicks, conversions, revenue_usd
    FROM (
        SELECT ms.*,
               ROW_NUMBER() OVER (PARTITION BY ms.publication_id ORDER BY ms.recorded_at DESC, ms.id DESC) AS rn
        FROM metrics_snapshots ms
    ) AS ranked
    WHERE rn = 1
)
"""

_ACTIVITY_COLUMNS = """
    COUNT(p.id)                                                              AS publications,
    COALESCE(SUM(CASE WHEN p.status = 'PUBLISHED' THEN 1 ELSE 0 END), 0)    AS published,
    COALESCE(SUM(CASE WHEN p.status IN ('PENDING', 'QUEUED') THEN 1 ELSE 0 END), 0) AS queued,
    COALESCE(SUM(CASE WHEN p.status = 'FAILED' THEN 1 ELSE 0 END), 0)       AS failed,
    COALESCE(SUM(CASE WHEN p.status = 'PUBLISHED' AND p.content_type = 'text' THEN 1 ELSE 0 END), 0)  AS text_count,
    COALESCE(SUM(CASE WHEN p.status = 'PUBLISHED' AND p.content_type = 'video' THEN 1 ELSE 0 END), 0) AS video_count,
    COALESCE(SUM(l.views), 0)                                                AS views,
    COALESCE(SUM(l.clicks), 0)                                               AS clicks,
    COALESCE(SUM(l.conversions), 0)                                          AS conversions,
    COALESCE(SUM(l.revenue_usd), 0)                                          AS revenue_usd
"""


def _with_ratios(row: dict[str, Any]) -> dict[str, Any]:
    """Add click-through rate, conversion rate and net profit to an aggregate row."""
    views, clicks = row.get("views", 0) or 0, row.get("clicks", 0) or 0
    row["ctr"] = clicks / views if views else 0.0
    row["conversion_rate"] = (row.get("conversions", 0) or 0) / clicks if clicks else 0.0
    row["net_profit_usd"] = round((row.get("revenue_usd", 0) or 0) - (row.get("ai_spend_usd", 0) or 0), 6)
    return row


class MetricsService:
    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings

    def get_overall_summary(self) -> dict[str, Any]:
        """Totals across everything: reach, revenue, AI spend, net profit, text vs video output."""
        row = self.db.query_one(
            f"""
            {_LATEST_SNAPSHOT_CTE}
            SELECT {_ACTIVITY_COLUMNS}
            FROM publications p LEFT JOIN latest l ON l.publication_id = p.id
            """
        ) or {}
        spend = self.db.query_one("SELECT COALESCE(SUM(generation_cost_usd), 0) AS total FROM ai_usage") or {}
        summary = _with_ratios({**row, "ai_spend_usd": float(spend.get("total", 0) or 0)})
        return {
            "total_views": int(summary["views"]),
            "total_clicks": int(summary["clicks"]),
            "total_conversions": int(summary["conversions"]),
            "revenue_usd": float(summary["revenue_usd"]),
            "ai_spend_usd": summary["ai_spend_usd"],
            "net_profit_usd": summary["net_profit_usd"],
            "total_text": int(summary["text_count"]),
            "total_videos": int(summary["video_count"]),
            "published": int(summary["published"]),
            "queued": int(summary["queued"]),
            "failed": int(summary["failed"]),
            "ctr": summary["ctr"],
            "conversion_rate": summary["conversion_rate"],
        }

    def get_metrics_by_platform(self) -> list[dict[str, Any]]:
        """One row per publishing platform."""
        rows = self.db.query(
            f"""
            {_LATEST_SNAPSHOT_CTE}
            SELECT p.platform AS platform,
                   {_ACTIVITY_COLUMNS},
                   COALESCE(SUM(p.generation_cost_usd), 0) AS ai_spend_usd
            FROM publications p LEFT JOIN latest l ON l.publication_id = p.id
            GROUP BY p.platform
            ORDER BY revenue_usd DESC, published DESC, p.platform
            """
        )
        return [_with_ratios(row) for row in rows]

    def get_metrics_by_pipeline(self) -> list[dict[str, Any]]:
        """One row per publishing pipeline ``<category>_<language>``, AI spend from the ledger."""
        rows = self.db.query(
            f"""
            {_LATEST_SNAPSHOT_CTE},
            activity AS (
                SELECT p.pipeline_id AS pipeline_id, MAX(p.category) AS category, MAX(p.language) AS language,
                       {_ACTIVITY_COLUMNS}
                FROM publications p LEFT JOIN latest l ON l.publication_id = p.id
                GROUP BY p.pipeline_id
            ),
            spend AS (
                SELECT pipeline_id, SUM(generation_cost_usd) AS ai_spend_usd FROM ai_usage GROUP BY pipeline_id
            )
            SELECT a.*, COALESCE(s.ai_spend_usd, 0) AS ai_spend_usd
            FROM activity a LEFT JOIN spend s ON s.pipeline_id = a.pipeline_id
            ORDER BY revenue_usd DESC, published DESC, a.pipeline_id
            """
        )
        return [_with_ratios(row) for row in rows]

    def get_metrics_by_content_type(self) -> list[dict[str, Any]]:
        """One row per content profile (``instagram_post``, ``instagram_story``…) and the models that wrote it."""
        rows = self.db.query(
            f"""
            {_LATEST_SNAPSHOT_CTE}
            SELECT COALESCE(p.content_format, 'legacy') AS content_format,
                   {self.db.string_agg_distinct("p.model_used")} AS models,
                   {_ACTIVITY_COLUMNS},
                   COALESCE(SUM(p.generation_cost_usd), 0) AS ai_spend_usd
            FROM publications p LEFT JOIN latest l ON l.publication_id = p.id
            GROUP BY COALESCE(p.content_format, 'legacy')
            ORDER BY published DESC, content_format
            """
        )
        return [_with_ratios(row) for row in rows]

    def get_product_inventory(self) -> list[dict[str, Any]]:
        """Harvested products per category and source (Stage 1 output)."""
        return self.db.count_products()

    # -- storefront click-throughs ----------------------------------------------- #
    # ``click_events`` holds one row per confirmed outbound click on the public storefront,
    # written by the storefront API the moment a shopper continues to the store.
    def get_storefront_click_summary(self) -> dict[str, Any]:
        """Outbound storefront clicks: all time, last 7 days, last 24 hours."""
        now = utcnow()
        row = self.db.query_one(
            """
            SELECT COUNT(*)                                AS total_clicks,
                   COALESCE(SUM(CASE WHEN clicked_at >= :week THEN 1 ELSE 0 END), 0) AS clicks_7d,
                   COALESCE(SUM(CASE WHEN clicked_at >= :day THEN 1 ELSE 0 END), 0)  AS clicks_24h,
                   COUNT(DISTINCT product_id)              AS products_clicked,
                   MAX(clicked_at)                         AS last_click_at
            FROM click_events
            """,
            {"week": to_iso(now - timedelta(days=7)), "day": to_iso(now - timedelta(days=1))},
        ) or {}
        return {
            "total_clicks": int(row.get("total_clicks") or 0),
            "clicks_7d": int(row.get("clicks_7d") or 0),
            "clicks_24h": int(row.get("clicks_24h") or 0),
            "products_clicked": int(row.get("products_clicked") or 0),
            "last_click_at": row.get("last_click_at"),
        }

    def _storefront_clicks_by(self, column: str) -> list[dict[str, Any]]:
        """Click counts grouped by ``category`` or ``source``, including listed groups with no clicks yet."""
        if column not in ("category", "source"):
            raise ValueError(f"unsupported click breakdown: {column}")
        now = utcnow()
        return self.db.query(
            f"""
            WITH scope AS (
                SELECT {column} AS name FROM products WHERE category IS NOT NULL AND category != ''
                UNION
                SELECT {column} FROM click_events
            )
            SELECT s.name                                    AS {column},
                   COUNT(c.id)                               AS clicks,
                   COALESCE(SUM(CASE WHEN c.clicked_at >= :week THEN 1 ELSE 0 END), 0) AS clicks_7d,
                   COALESCE(SUM(CASE WHEN c.clicked_at >= :day THEN 1 ELSE 0 END), 0)  AS clicks_24h,
                   COUNT(DISTINCT c.product_id)              AS products_clicked,
                   MAX(c.clicked_at)                         AS last_click_at
            FROM scope s LEFT JOIN click_events c ON c.{column} = s.name
            GROUP BY s.name
            ORDER BY clicks DESC, s.name
            """,
            {"week": to_iso(now - timedelta(days=7)), "day": to_iso(now - timedelta(days=1))},
        )

    def get_storefront_clicks_by_category(self) -> list[dict[str, Any]]:
        """One row per storefront category."""
        return self._storefront_clicks_by("category")

    def get_storefront_clicks_by_source(self) -> list[dict[str, Any]]:
        """One row per product source (store)."""
        return self._storefront_clicks_by("source")

    def get_upload_health(self, platform: str, recent: int = 20) -> dict[str, Any]:
        """Delivery outcomes of one publisher plugin (``youtube_pub``, ``tiktok_pub``…).

        ``last_run`` covers the attempts in the 24 hours before the most recent one (a publishing
        run plus the worker's paced deliveries); ``recent`` covers the latest ``recent`` attempts.
        Success rates are ``None`` when there is nothing to measure yet.
        """
        rows = self.db.query(
            """
            SELECT status, error_message, COALESCE(published_at, scheduled_for, created_at) AS attempted_at
            FROM publications
            WHERE platform = ? AND status IN ('PUBLISHED', 'FAILED')
            ORDER BY id DESC
            LIMIT ?
            """,
            (platform, max(1, recent)),
        )
        pending = self.db.query(
            "SELECT COUNT(*) AS n FROM publications WHERE platform = ? AND status IN ('PENDING', 'QUEUED')",
            (platform,),
        )

        def rate(items: list[dict[str, Any]]) -> float | None:
            return sum(1 for r in items if r["status"] == "PUBLISHED") / len(items) if items else None

        last_run: list[dict[str, Any]] = []
        if rows:
            newest = from_iso(rows[0]["attempted_at"])
            last_run = [r for r in rows if newest is None or (from_iso(r["attempted_at"]) or newest)
                        >= newest - timedelta(hours=24)]
        failures = [r for r in rows if r["status"] == "FAILED"]
        published = [r for r in rows if r["status"] == "PUBLISHED"]
        return {
            "platform": platform,
            "attempts": len(rows),
            "published": len(published),
            "failed": len(failures),
            "success_rate": rate(rows),
            "last_run_attempts": len(last_run),
            "last_run_published": sum(1 for r in last_run if r["status"] == "PUBLISHED"),
            "last_run_success_rate": rate(last_run),
            "last_attempt_at": rows[0]["attempted_at"] if rows else None,
            "last_published_at": published[0]["attempted_at"] if published else None,
            "last_error": failures[0]["error_message"] if failures else None,
            "queued": int((pending[0] if pending else {}).get("n") or 0),
        }

    def get_budget_status(self) -> dict[str, Any]:
        """Month-to-date AI spend against ``MAX_MONTHLY_AI_BUDGET_USD``."""
        spent = self.db.get_monthly_ai_spend()
        limit = self.settings.max_monthly_ai_budget_usd
        ratio = spent / limit if limit > 0 else 1.0
        if spent >= limit:
            state = "exceeded"
        elif ratio >= 0.9:
            state = "critical"
        elif ratio >= 0.7:
            state = "warning"
        else:
            state = "ok"
        return {
            "spent_usd": spent,
            "limit_usd": limit,
            "remaining_usd": max(0.0, limit - spent),
            "ratio": ratio,
            "state": state,
        }
