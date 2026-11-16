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
* Storefront clicks are a separate, exact count: one ``click_events`` row per
  confirmed outbound click on the public storefront. They are not mixed into the
  publication KPIs above, which describe posts on social platforms.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from core.config import Settings
from core.database import Database, to_iso
from core.models import utcnow

# Latest snapshot per publication, shared by every aggregation below.
_LATEST_SNAPSHOT_CTE = """
WITH latest AS (
    SELECT publication_id, views, clicks, conversions, revenue_usd
    FROM (
        SELECT ms.*,
               ROW_NUMBER() OVER (PARTITION BY ms.publication_id ORDER BY ms.recorded_at DESC, ms.id DESC) AS rn
        FROM metrics_snapshots ms
    )
    WHERE rn = 1
)
"""

_ACTIVITY_COLUMNS = """
    COUNT(p.id)                                                              AS publications,
    COALESCE(SUM(p.status = 'PUBLISHED'), 0)                                 AS published,
    COALESCE(SUM(p.status IN ('PENDING', 'QUEUED')), 0)                      AS queued,
    COALESCE(SUM(p.status = 'FAILED'), 0)                                    AS failed,
    COALESCE(SUM(p.status = 'PUBLISHED' AND p.content_type = 'text'), 0)     AS text_count,
    COALESCE(SUM(p.status = 'PUBLISHED' AND p.content_type = 'video'), 0)    AS video_count,
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
        """One row per pipeline, including pipelines that have not produced anything yet."""
        rows = self.db.query(
            f"""
            {_LATEST_SNAPSHOT_CTE},
            activity AS (
                SELECT p.pipeline_id AS pipeline_id, {_ACTIVITY_COLUMNS}
                FROM publications p LEFT JOIN latest l ON l.publication_id = p.id
                GROUP BY p.pipeline_id
            ),
            spend AS (
                SELECT pipeline_id, SUM(generation_cost_usd) AS ai_spend_usd FROM ai_usage GROUP BY pipeline_id
            )
            SELECT pl.id AS pipeline_id, pl.type AS niche, pl.language AS language, pl.enabled AS enabled,
                   COALESCE(a.publications, 0) AS publications,
                   COALESCE(a.published, 0)    AS published,
                   COALESCE(a.queued, 0)       AS queued,
                   COALESCE(a.failed, 0)       AS failed,
                   COALESCE(a.text_count, 0)   AS text_count,
                   COALESCE(a.video_count, 0)  AS video_count,
                   COALESCE(a.views, 0)        AS views,
                   COALESCE(a.clicks, 0)       AS clicks,
                   COALESCE(a.conversions, 0)  AS conversions,
                   COALESCE(a.revenue_usd, 0)  AS revenue_usd,
                   COALESCE(s.ai_spend_usd, 0) AS ai_spend_usd
            FROM pipelines pl
            LEFT JOIN activity a ON a.pipeline_id = pl.id
            LEFT JOIN spend s    ON s.pipeline_id = pl.id
            ORDER BY revenue_usd DESC, published DESC, pl.id
            """
        )
        return [_with_ratios(row) for row in rows]

    # -- storefront click-throughs ----------------------------------------------- #
    # ``click_events`` holds one row per confirmed outbound click on the public storefront,
    # written by the storefront API the moment a shopper continues to the store.
    def get_storefront_click_summary(self) -> dict[str, Any]:
        """Outbound storefront clicks: all time, last 7 days, last 24 hours."""
        now = utcnow()
        row = self.db.query_one(
            """
            SELECT COUNT(*)                                AS total_clicks,
                   COALESCE(SUM(clicked_at >= :week), 0)   AS clicks_7d,
                   COALESCE(SUM(clicked_at >= :day), 0)    AS clicks_24h,
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
                   COALESCE(SUM(c.clicked_at >= :week), 0)   AS clicks_7d,
                   COALESCE(SUM(c.clicked_at >= :day), 0)    AS clicks_24h,
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
