"""Admin control plane: every ``python main.py`` operation as a protected HTTP API.

Mounted under ``/api/admin`` by :func:`storefront_api.server.create_app`. Every request must
carry ``X-Admin-Key: <ADMIN_API_SECRET>``; without ``ADMIN_API_SECRET`` in the environment the
whole admin API answers 503. It is meant for scripts that call the backend directly: the public
storefront has no admin pages and never proxies ``/api/admin``, and operators use the Streamlit
dashboard (``dashboard/app.py``), which drives the engine in-process.

CLI parity
----------
Long-running commands become background jobs (see ``storefront_api/jobs.py``) that run the very
same code as the command line, ``main.dispatch``, and record its console output:

    POST /api/admin/actions/harvest          python main.py --harvest [--source S] [--category C] [--limit N] [--dry-run]
    POST /api/admin/actions/publish          python main.py --publish [--category C] [--lang L] [--content-types …] [--dry-run]
    POST /api/admin/actions/verify           python main.py --verify-instagram --verify-youtube --verify-tiktok --verify-telegram
    POST /api/admin/actions/seed             python main.py --seed-temu | --seed-aliexpress --category C [--keyword K]
    POST /api/admin/actions/seed-storefront  python main.py --seed-storefront
    POST /api/admin/actions/sync-config      python main.py --sync-config
    POST /api/admin/actions/worker           python main.py --mode worker --once
    POST /api/admin/actions/report           python main.py --stats | --list-config
    POST /api/admin/actions/import-sqlite    python main.py --import-sqlite FILE
    POST /api/admin/queue/flush              python main.py --flush-queue [--category C --lang L]
    POST /api/admin/queue/retry-failed       python main.py --retry-failed [--category C --lang L]
    POST /api/admin/assets/generate          python main.py --generate-assets --category C --lang L [--content-types …]
    POST /api/admin/assets/publish           python main.py --publish-assets ID[,ID…]

Generated FindHub assets (copy + branded image + links) are listed with ``GET /api/admin/assets`` and
moved between DRAFT and QUEUED, edited, marked as published by hand, or deleted with the routes below it.

Single unpublished items are reviewed with the routes below; the rules live in ``services.content_review``,
which the Streamlit dashboard's Content Review tab runs in-process:

    PATCH  /api/admin/assets/{id}                edit generated_text / Story link stickers / Reel caption
    DELETE /api/admin/assets/{id}                delete it and its image, unless another row uses the file
    PATCH  /api/admin/queue/{id}                 edit a queued publication's caption
    DELETE /api/admin/queue/{id}                 delete it and its local media, unless another row uses the file

Configuration (categories, content profiles, the social-accounts matrix) is edited directly,
with the same validation as ``config/*.yaml``. Credentials are never accepted: forms take the
NAMES of environment variables, and the API reports whether each one is set (``Configured`` /
``Missing``) without ever returning a value.
"""

from __future__ import annotations

import argparse
import hmac
import os
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query
from fastapi import Path as PathParam
from pydantic import BaseModel, Field, ValidationError

from core.config import (
    PUBLISH_FORMATS,
    CategoryConfig,
    ContentProfile,
    Settings,
    SocialAccount,
    describe_validation_error,
)
from core.database import Database, to_iso
from core.models import category_slug, utcnow
from core.resilience import TarshishError, get_logger, looks_like_secret, redact
from storefront_api.jobs import Job, JobRunner, present_job

logger = get_logger("admin")

ADMIN_SECRET_ENV = "ADMIN_API_SECRET"
PLACEHOLDER_SECRETS = {"change_me_to_a_strong_secret", "change_me", "changeme"}
MIN_SECRET_LENGTH = 16
SOCIAL_PLATFORMS = ("instagram", "tiktok", "youtube", "telegram")
HARVEST_SOURCES = ("temu_rapidapi", "aliexpress_rapidapi")
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
# What the admin forms accept as a variable NAME: UPPER_SNAKE_CASE, like every name in .env.example.
# A pasted token (mixed case, or one long run without underscores) fails this check.
_ADMIN_ENV_NAME_RE = re.compile(r"[A-Z_][A-Z0-9_]{1,127}")
_VERIFY_FLAGS = {
    "instagram": "--verify-instagram", "youtube": "--verify-youtube",
    "tiktok": "--verify-tiktok", "telegram": "--verify-telegram",
}
_VERIFY_MODULES = {
    "instagram": "plugins.publishers.instagram_pub", "youtube": "plugins.publishers.youtube_pub",
    "tiktok": "plugins.publishers.tiktok_pub", "telegram": "plugins.publishers.telegram_pub",
}
# What the onboarding wizard asks for per platform: platform_metadata keys and env-var roles.
PLATFORM_FIELDS: dict[str, dict[str, Any]] = {
    "instagram": {
        "label": "Instagram", "handle_label": "Page handle", "handle_placeholder": "@tarshish_gadgets_il",
        "metadata": [
            {"key": "app_id", "label": "App ID", "required": True},
            {"key": "business_account_id", "label": "Instagram Business Account ID", "required": True},
        ],
        "token_label": "Access token env var", "token_placeholder": "IG_GADGETS_HE_TOKEN",
    },
    "tiktok": {
        "label": "TikTok", "handle_label": "Page handle", "handle_placeholder": "@tarshish.gadgets",
        "metadata": [{"key": "client_key", "label": "Client Key", "required": True}],
        "token_label": "Access token env var", "token_placeholder": "TIKTOK_GADGETS_HE_TOKEN",
        "account_id_label": "Refresh token env var (optional)", "account_id_placeholder": "TIKTOK_GADGETS_HE_REFRESH",
    },
    "youtube": {
        "label": "YouTube Shorts", "handle_label": "Channel handle", "handle_placeholder": "@tarshish.gadgets",
        "metadata": [
            {"key": "client_id", "label": "OAuth Client ID", "required": True},
            {"key": "channel_id", "label": "Channel ID (optional)", "required": False},
        ],
        "token_label": "Refresh token env var", "token_placeholder": "YT_GADGETS_HE_TOKEN",
    },
    "telegram": {
        "label": "Telegram", "handle_label": "Channel username", "handle_placeholder": "@tarshish_deals",
        "metadata": [{"key": "chat_id", "label": "Chat ID", "required": True}],
        "token_label": "Bot token env var", "token_placeholder": "TG_GADGETS_HE_TOKEN",
    },
}
# Engine-wide variables the overview reports on (names only; values never leave the server).
CORE_ENV_VARS: dict[str, list[tuple[str, str]]] = {
    "AI models": [("ANTHROPIC_API_KEY", "Claude models"), ("GEMINI_API_KEY", "Gemini models · free chain #1"),
                  ("GROQ_API_KEY", "Free chain #2: Groq Llama 3.3 70B"),
                  ("OPENROUTER_API_KEY", "Free chain #3: OpenRouter free models")],
    "FindHub links & media": [("STOREFRONT_BASE_URL", "Storefront links in posts and Story stickers"),
                              ("MEDIA_PUBLIC_BASE_URL", "Public HTTPS origin of /media images (optional)")],
    "Affiliate tracking": [("TEMU_TRACKING_ID", "Temu affiliate links"),
                           ("ALIEXPRESS_TRACKING_ID", "AliExpress affiliate links")],
    "Infrastructure": [("DATABASE_URL", "Supabase PostgreSQL (else local SQLite)"),
                       ("SUPABASE_URL", "Media bucket for videos / Stories"),
                       (ADMIN_SECRET_ENV, "This admin API")],
}


def env_configured(name: str | None) -> bool:
    return bool(name) and bool(os.getenv(name or "", "").strip())


def env_state(name: str | None) -> str:
    return "Configured" if env_configured(name) else "Missing"


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #
def admin_secret_problem(secret: str | None) -> str | None:
    if not secret:
        return f"The admin API is disabled: set {ADMIN_SECRET_ENV} in the backend environment."
    if secret.strip().lower() in PLACEHOLDER_SECRETS:
        return f"The admin API is disabled: {ADMIN_SECRET_ENV} still holds the example value; choose a real secret."
    if len(secret.strip()) < MIN_SECRET_LENGTH:
        return f"The admin API is disabled: {ADMIN_SECRET_ENV} must be at least {MIN_SECRET_LENGTH} characters."
    return None


def require_admin(x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")) -> None:
    secret = Settings.get_env(ADMIN_SECRET_ENV)
    problem = admin_secret_problem(secret)
    if problem:
        raise HTTPException(status_code=503, detail=problem)
    supplied = (x_admin_key or "").encode()
    if not hmac.compare_digest(supplied, (secret or "").strip().encode()):
        raise HTTPException(status_code=401, detail="Missing or wrong X-Admin-Key header.")


# --------------------------------------------------------------------------- #
# Context: the engine and the job runner, built on first use
# --------------------------------------------------------------------------- #
class AdminContext:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._engine: Any = None
        self._runner: JobRunner | None = None
        self._lock = threading.Lock()

    @property
    def serverless(self) -> bool:
        return bool(os.environ.get("VERCEL")) and not env_configured("ADMIN_ALLOW_SERVERLESS_JOBS")

    def engine(self) -> Any:
        with self._lock:
            if self._engine is None:
                from core import bootstrap

                settings = self.settings
                if os.environ.get("VERCEL"):  # read-only filesystem: no log file
                    settings = settings.model_copy(update={"log_file": None})
                self._engine = bootstrap(settings=settings, console_logging=True)
            return self._engine

    def runner(self) -> JobRunner:
        engine = self.engine()
        with self._lock:
            if self._runner is None:
                self._runner = JobRunner(engine.db)
            return self._runner


# --------------------------------------------------------------------------- #
# Request bodies
# --------------------------------------------------------------------------- #
class CategoryIn(BaseModel):
    category_id: str | None = Field(default=None, description="URL slug; derived from the English name when empty.")
    display_name_en: str = Field(min_length=1, max_length=80)
    display_name_he: str = Field(default="", max_length=80)
    temu_search: dict[str, Any] | None = None
    aliexpress_search: dict[str, Any] | None = None
    temu_keyword: str | None = Field(default=None, max_length=120, description="Shorthand for temu_search.keyword")
    aliexpress_keyword: str | None = Field(default=None, max_length=120)
    price_min: float | None = Field(default=None, ge=0)
    price_max: float | None = Field(default=None, ge=0)
    min_rating: float = Field(default=4.5, ge=0, le=5)
    min_orders: int = Field(default=200, ge=0)
    enabled: bool = True


class CategoryPatch(BaseModel):
    display_name_en: str | None = Field(default=None, min_length=1, max_length=80)
    display_name_he: str | None = Field(default=None, max_length=80)
    temu_search: dict[str, Any] | None = None
    aliexpress_search: dict[str, Any] | None = None
    min_rating: float | None = Field(default=None, ge=0, le=5)
    min_orders: int | None = Field(default=None, ge=0)
    enabled: bool | None = None


class SocialAccountIn(BaseModel):
    page_handle: str = Field(min_length=1, max_length=120)
    platform: str = Field(description="instagram | tiktok | youtube | telegram")
    language: str = Field(min_length=2, max_length=12)
    category_id: str | None = Field(default=None, description="An existing category, or leave empty and send new_category.")
    new_category: CategoryIn | None = None
    token_env_var: str | None = Field(default=None, max_length=128)
    account_id_env_var: str | None = Field(default=None, max_length=128)
    platform_metadata: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class SocialAccountPatch(BaseModel):
    page_handle: str | None = Field(default=None, min_length=1, max_length=120)
    token_env_var: str | None = Field(default=None, max_length=128)
    account_id_env_var: str | None = Field(default=None, max_length=128)
    platform_metadata: dict[str, Any] | None = None
    enabled: bool | None = None


class ContentProfilePatch(BaseModel):
    llm_model: str | None = Field(default=None, min_length=1, max_length=80)
    generator: str | None = Field(default=None, min_length=1, max_length=80)
    publish_format: str | None = None
    tone: str | None = Field(default=None, max_length=500)
    prompt_template: str | None = Field(default=None, max_length=8000)
    enabled: bool | None = None


class HarvestIn(BaseModel):
    source: str = Field(default="all", description="temu_rapidapi | aliexpress_rapidapi | all (or a mock source)")
    category_id: str = Field(default="all")
    limit: int | None = Field(default=None, ge=1, le=100)
    dry_run: bool = False


class PublishIn(BaseModel):
    category_id: str = Field(default="all")
    language: str = Field(default="all")
    content_types: list[str] = Field(default_factory=list)
    dry_run: bool = False
    mode: Literal["immediate", "queued"] | None = None
    limit: int | None = Field(default=None, ge=1, le=100)
    force: bool = False


class VerifyIn(BaseModel):
    platforms: list[Literal["instagram", "youtube", "tiktok", "telegram"]] = Field(
        default_factory=lambda: list(SOCIAL_PLATFORMS)
    )
    category_id: str | None = None
    language: str | None = None


class SeedIn(BaseModel):
    store: Literal["temu", "aliexpress"]
    category_id: str
    keyword: str | None = Field(default=None, max_length=120)
    limit: int = Field(default=10, ge=1, le=100)
    job: str | None = Field(default=None, max_length=120, description="Temu only: collect an earlier search job")


class ReportIn(BaseModel):
    report: Literal["stats", "list-config"]
    category_id: str | None = None
    language: str | None = None


class ImportIn(BaseModel):
    path: str = Field(min_length=1, max_length=500)


class AssetGenerateIn(BaseModel):
    category_id: str = Field(min_length=1, max_length=64)
    language: Literal["he", "en"]
    content_types: list[Literal["telegram_post", "instagram_post", "instagram_story", "instagram_reel", "tiktok_post"]] = Field(
        default_factory=lambda: ["telegram_post", "instagram_post", "instagram_story", "instagram_reel", "tiktok_post"],
        min_length=1,
    )
    limit: int = Field(default=3, ge=1, le=20, description="Real products per content type")
    force: bool = Field(default=False, description="Also products that already have an asset of this type")


class AssetPublishIn(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=50)


class AssetStatusIn(BaseModel):
    status: Literal["DRAFT", "QUEUED"]


class StoryLinkIn(BaseModel):
    kind: Literal["storefront", "product", "affiliate"]
    label: str = Field(min_length=1, max_length=60)
    url: str = Field(min_length=8, max_length=2000, pattern=r"^https?://\S+$")


class AssetPatch(BaseModel):
    """Edits from the preview simulator; omitted fields are left unchanged."""

    model_config = {"extra": "forbid"}

    generated_text: str | None = Field(default=None, min_length=1, max_length=5000)
    reel_caption: str | None = Field(default=None, min_length=1, max_length=2200)
    story_links: list[StoryLinkIn] | None = Field(default=None, max_length=5)


class QueuePatch(BaseModel):
    model_config = {"extra": "forbid"}

    caption: str = Field(min_length=1, max_length=5000)


class QueueScope(BaseModel):
    category_id: str | None = None
    language: str | None = None


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _validation_error(exc: ValidationError) -> HTTPException:
    return HTTPException(status_code=422, detail="; ".join(describe_validation_error(exc)))


@contextmanager
def review_errors() -> Iterator[None]:
    """Turn a refused content-review action into the matching HTTP error."""
    from services.content_review import ReviewError

    try:
        yield
    except ReviewError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


def _all(value: str | None) -> str | None:
    value = (value or "").strip()
    return None if value.lower() in ("", "all", "*") else value


def parse_cli(argv: list[str]) -> argparse.Namespace:
    """Parse and validate arguments exactly like ``main.py`` does; 422 instead of exiting."""
    import main

    parser = main.build_parser()

    def refuse(message: str) -> None:
        raise HTTPException(status_code=422, detail=message)

    parser.error = refuse  # type: ignore[method-assign]
    args = parser.parse_args(argv)
    main.validate_args(parser, args)
    return args


def account_key(account: SocialAccount) -> str:
    return ":".join(account.key)


def split_account_key(account_id: str) -> tuple[str, str, str]:
    parts = account_id.split(":")
    if len(parts) != 3 or not all(parts):
        raise HTTPException(status_code=404, detail="Account ids look like 'gadgets:he:instagram'.")
    return parts[0], parts[1], parts[2]


def model_label(model: str) -> str:
    fixed = {
        "mock": "Mock (offline templates, free)",
        "anthropic": "Claude (ANTHROPIC_MODEL default)",
        "gemini": "Gemini (GEMINI_MODEL default)",
    }
    if model in fixed:
        return fixed[model]
    match = re.fullmatch(r"claude-([a-z]+)-(\d+)-(\d+)", model)
    if match:
        return f"Claude {match.group(1).title()} {match.group(2)}.{match.group(3)}"
    if model.startswith("gemini-"):
        return "Gemini " + model.removeprefix("gemini-").replace("-", " ").title()
    return model


def model_provider(model: str) -> str:
    if model.startswith(("claude", "anthropic")):
        return "anthropic"
    if model.startswith("gemini"):
        return "gemini"
    return "offline" if model == "mock" else "other"


class AdminService:
    """The admin operations, independent of HTTP (the router below is a thin layer)."""

    def __init__(self, ctx: AdminContext) -> None:
        self.ctx = ctx

    @property
    def engine(self) -> Any:
        return self.ctx.engine()

    @property
    def db(self) -> Database:
        return self.engine.db

    # -- jobs ------------------------------------------------------------------- #
    def start_cli_job(self, kind: str, argv: list[str], params: dict[str, Any]) -> dict[str, Any]:
        args = parse_cli(argv)
        self._jobs_allowed()
        engine = self.engine

        def action(_job: Job) -> int:
            import main

            return main.dispatch(engine, args)

        job = self.ctx.runner().submit(kind, params, argv, action)
        return self.ctx.runner().get(job.id) or {"id": job.id}

    def start_job(self, kind: str, params: dict[str, Any], action: Any) -> dict[str, Any]:
        self._jobs_allowed()
        job = self.ctx.runner().submit(kind, params, [], action)
        return self.ctx.runner().get(job.id) or {"id": job.id}

    def _jobs_allowed(self) -> None:
        if self.ctx.serverless:
            raise HTTPException(
                status_code=409,
                detail="Background jobs need the long-running backend (uvicorn on a server or behind a tunnel); "
                       "this API runs as a serverless function, which stops as soon as it answers. Point "
                       "BACKEND_API_URL at the long-running backend (see DEPLOYMENT.md).",
            )

    # -- overview --------------------------------------------------------------- #
    def chain(self, store: str) -> dict[str, Any]:
        module = {"temu": "plugins.sources.temu_providers", "aliexpress": "plugins.sources.aliexpress_providers"}[store]
        try:
            import importlib

            status = importlib.import_module(module).chain_status(self.engine.settings)
        except Exception as exc:  # a broken chain file must not take the overview down
            return {"store": store, "providers": [], "error": redact(f"{type(exc).__name__}: {exc}"), "last_run": None}
        last = status.get("last_run") or {}
        providers = [
            {
                "order": p["order"], "name": p["name"], "host": p["host"], "adapter": p["adapter"],
                "enabled": p["enabled"], "api_key_env": p["api_key_env"],
                "key_status": "Ready" if str(p["key_state"]).startswith("Configured") else "Missing",
                "key_detail": p["key_state"],
                "last_outcome": p.get("last_outcome"), "last_detail": redact(str(p.get("last_detail") or "")) or None,
            }
            for p in status.get("providers", [])
        ]
        return {
            "store": store,
            "config_path": os.path.basename(status.get("config_path") or "") or None,
            "from_file": status.get("from_file", False),
            "providers": providers,
            "ready": any(p["key_status"] == "Ready" and p["enabled"] for p in providers),
            "error": status.get("error"),
            "last_run": {k: last.get(k) for k in ("finished_at", "succeeded", "category", "keyword", "usable")}
            if last else None,
        }

    def platform_credentials(self) -> list[dict[str, Any]]:
        plugins = self.engine.plugins
        rows: list[dict[str, Any]] = []
        for platform in SOCIAL_PLATFORMS:
            publisher = f"{platform}_pub"
            if not plugins.has("publisher", publisher):
                rows.append({"platform": platform, "available": False, "env": []})
                continue
            cls = plugins.get("publisher", publisher)
            rows.append({
                "platform": platform, "available": True,
                "env": [
                    {"credential": credential, "env_var": name, "status": env_state(name),
                     "required": credential in cls.required_credentials}
                    for credential, name in cls.default_env_vars.items()
                ],
            })
        return rows

    def overview(self) -> dict[str, Any]:
        engine = self.engine
        db = engine.db
        counts = db.get_status_counts()
        spent = db.get_monthly_ai_spend()
        cap = engine.settings.max_monthly_ai_budget_usd
        now = utcnow()
        clicks = db.query_one(
            "SELECT COUNT(*) AS total, COALESCE(SUM(CASE WHEN clicked_at >= ? THEN 1 ELSE 0 END), 0) AS week "
            "FROM click_events",
            (to_iso(now - timedelta(days=7)),),
        ) or {}
        products = db.query_one(
            "SELECT COUNT(*) AS total, "
            "COALESCE(SUM(CASE WHEN category IS NOT NULL AND category != '' THEN 1 ELSE 0 END), 0) AS listed "
            "FROM products"
        ) or {}
        categories = engine.registry.list_categories()
        accounts = [self.present_account(a) for a in engine.registry.list_social_accounts()]
        ratio = spent / cap if cap > 0 else (1.0 if spent > 0 else 0.0)
        return {
            "kpis": {
                "storefront_clicks": int(clicks.get("total") or 0),
                "storefront_clicks_7d": int(clicks.get("week") or 0),
                "published_posts": counts["PUBLISHED"],
                "active_products": int(products.get("listed") or 0),
                "harvested_products": int(products.get("total") or 0),
            },
            "budget": {
                "spent_usd": round(spent, 4), "limit_usd": cap, "ratio": round(ratio, 4),
                "state": "exceeded" if spent > 0 and spent >= cap else
                         "critical" if ratio >= 0.9 else "warning" if ratio >= 0.7 else "ok",
            },
            "queue": {"QUEUED": counts["QUEUED"], "PENDING": counts["PENDING"],
                      "PUBLISHED": counts["PUBLISHED"], "FAILED": counts["FAILED"]},
            "categories": {"total": len(categories), "active": sum(1 for c in categories if c.enabled)},
            "social_accounts": {
                "total": len(accounts),
                "enabled": sum(1 for a in accounts if a["enabled"]),
                "ready": sum(1 for a in accounts if a["status"] == "ready"),
                "by_platform": {p: sum(1 for a in accounts if a["platform"] == p) for p in SOCIAL_PLATFORMS},
            },
            "sources": {"temu": self.chain("temu"), "aliexpress": self.chain("aliexpress")},
            "platform_credentials": self.platform_credentials(),
            "environment": [
                {"group": group, "vars": [{"name": name, "purpose": purpose, "status": env_state(name)}
                                          for name, purpose in names]}
                for group, names in CORE_ENV_VARS.items()
            ],
            "recent_jobs": [present_job(row) for row in db.list_job_runs(limit=8)],
            "runtime": {
                "database": db.describe(),
                "serverless": bool(os.environ.get("VERCEL")),
                "background_jobs": not self.ctx.serverless,
                "plugin_load_errors": sorted(engine.plugins.load_errors),
            },
        }

    # -- categories ------------------------------------------------------------- #
    def present_category(self, category: CategoryConfig, inventory: dict[str, dict[str, int]]) -> dict[str, Any]:
        return {
            **category.model_dump(),
            "sources": category.default_sources(),
            "products": inventory.get(category.category_id, {}),
            "accounts": sum(1 for a in self.engine.registry.list_social_accounts(category=category.category_id)),
        }

    def inventory(self) -> dict[str, dict[str, int]]:
        found: dict[str, dict[str, int]] = {}
        for row in self.db.count_products():
            found.setdefault(row["category"] or "", {})[row["source"]] = int(row["products"])
        return found

    def list_categories(self) -> list[dict[str, Any]]:
        inventory = self.inventory()
        return [self.present_category(c, inventory) for c in self.engine.registry.list_categories()]

    @staticmethod
    def category_from_input(body: CategoryIn) -> CategoryConfig:
        def rules(explicit: dict[str, Any] | None, keyword: str | None) -> dict[str, Any] | None:
            if explicit:
                return explicit
            if not (keyword or "").strip():
                return None
            found: dict[str, Any] = {"keyword": keyword.strip()}  # type: ignore[union-attr]
            if body.price_min is not None:
                found["priceMin"] = body.price_min
            if body.price_max is not None:
                found["priceMax"] = body.price_max
            return found

        category_id = (body.category_id or "").strip() or category_slug(body.display_name_en)
        try:
            return CategoryConfig.model_validate({
                "category_id": category_id,
                "display_name_en": body.display_name_en,
                "display_name_he": body.display_name_he,
                "temu_search": rules(body.temu_search, body.temu_keyword),
                "aliexpress_search": rules(body.aliexpress_search, body.aliexpress_keyword),
                "min_rating": body.min_rating,
                "min_orders": body.min_orders,
                "enabled": body.enabled,
            })
        except ValidationError as exc:
            raise _validation_error(exc) from None

    def create_category(self, body: CategoryIn) -> dict[str, Any]:
        category = self.category_from_input(body)
        if self.db.get_config("categories", category.category_id) is not None:
            raise HTTPException(status_code=409, detail=f"Category '{category.category_id}' already exists.")
        self.engine.registry.save_category(category)
        return self.present_category(category, self.inventory())

    def update_category(self, category_id: str, body: CategoryPatch) -> dict[str, Any]:
        current = self._category(category_id)
        changes = body.model_dump(exclude_unset=True)
        try:
            category = CategoryConfig.model_validate({**current.model_dump(), **changes})
        except ValidationError as exc:
            raise _validation_error(exc) from None
        self.engine.registry.save_category(category)
        return self.present_category(category, self.inventory())

    def delete_category(self, category_id: str) -> dict[str, Any]:
        self._category(category_id)
        linked = self.engine.registry.list_social_accounts(category=category_id)
        if linked:
            raise HTTPException(
                status_code=409,
                detail=f"Category '{category_id}' still has {len(linked)} social account(s); unlink them or disable "
                       "the category instead.",
            )
        self.db.delete_config("categories", category_id)
        return {"deleted": category_id}

    def _category(self, category_id: str) -> CategoryConfig:
        record = self.db.get_config("categories", category_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"Unknown category '{category_id}'.")
        return CategoryConfig.from_record(record)

    # -- social accounts ---------------------------------------------------------- #
    def present_account(self, account: SocialAccount) -> dict[str, Any]:
        registry = self.engine.registry
        plugins = self.engine.plugins
        base = {
            "id": account_key(account), "category_id": account.category_id, "language": account.language,
            "platform": account.platform, "page_handle": account.account_handle,
            "token_env_var": account.token_env_var, "account_id_env_var": account.account_id_env_var,
            "platform_metadata": account.platform_metadata, "enabled": account.enabled,
            "publisher": account.publisher,
        }
        if not plugins.has("publisher", account.publisher):
            return {**base, "status": "no_publisher", "reason": f"no publisher plugin '{account.publisher}'", "env": []}
        cls = plugins.get("publisher", account.publisher)
        page = registry.social_page(account)
        refs = {**cls.default_env_vars, **page.env_refs()}
        inline = {cred: key for key, cred in cls.inline_credentials.items() if account.platform_metadata.get(key)}
        env = []
        for credential in dict.fromkeys([*cls.required_credentials, *refs]):
            name = refs.get(credential)
            env.append({
                "credential": credential, "env_var": name, "status": env_state(name),
                "required": credential in cls.required_credentials,
                "inline_from": inline.get(credential),
                "explicit": credential in page.env_refs(),
            })
        _, reason = registry.resolve_target(page)
        status = "ready" if reason is None else "missing_credentials"
        if not account.enabled:
            status = "disabled"
        return {**base, "status": status, "reason": redact(reason) if reason else None, "env": env}

    def list_accounts(self) -> list[dict[str, Any]]:
        return [self.present_account(a) for a in self.engine.registry.list_social_accounts()]

    def _account(self, account_id: str) -> SocialAccount:
        category, language, platform = split_account_key(account_id)
        account = self.engine.registry.get_social_account(category, language, platform)
        if account is None:
            raise HTTPException(status_code=404, detail=f"No social account '{account_id}'.")
        return account

    def _check_env_names(self, *names: str | None) -> None:
        for name in names:
            if name and (
                not _ADMIN_ENV_NAME_RE.fullmatch(name) or looks_like_secret(name)
                or ("_" not in name and len(name) >= 24)
            ):
                raise HTTPException(
                    status_code=422,
                    detail=f"'{name[:6]}…' is not an environment variable NAME (UPPER_SNAKE_CASE, e.g. "
                           "IG_GADGETS_HE_TOKEN). "
                           "Never paste the secret itself: put it in the backend environment and enter its name.",
                )

    def create_account(self, body: SocialAccountIn) -> dict[str, Any]:
        registry = self.engine.registry
        platform = body.platform.strip().lower()
        if not self.engine.plugins.has("publisher", f"{platform}_pub"):
            raise HTTPException(status_code=422, detail=f"Unsupported platform '{platform}'. "
                                                        f"Choose one of: {', '.join(SOCIAL_PLATFORMS)}.")
        if bool(body.category_id) == bool(body.new_category):
            raise HTTPException(status_code=422, detail="Pick an existing category_id or send new_category, not both.")
        new_category = self.category_from_input(body.new_category) if body.new_category else None
        category_id = new_category.category_id if new_category else (body.category_id or "").strip()
        if new_category is None and self.db.get_config("categories", category_id) is None:
            raise HTTPException(status_code=404, detail=f"Unknown category '{category_id}'.")
        if new_category is not None and self.db.get_config("categories", category_id) is not None:
            raise HTTPException(status_code=409, detail=f"Category '{category_id}' already exists; select it instead.")
        self._check_env_names(body.token_env_var, body.account_id_env_var)
        try:  # validate the account before anything is written, so a bad form leaves no orphan category
            account = SocialAccount.model_validate({
                "category_id": category_id, "language": body.language, "platform": platform,
                "account_handle": body.page_handle, "token_env_var": body.token_env_var,
                "account_id_env_var": body.account_id_env_var, "platform_metadata": body.platform_metadata,
                "enabled": body.enabled,
            })
        except ValidationError as exc:
            raise _validation_error(exc) from None
        if registry.get_social_account(*account.key) is not None:
            raise HTTPException(
                status_code=409,
                detail=f"{account.category_id}/{account.language} already has a {platform} page "
                       "(one page per category × language × platform). Edit it instead.",
            )
        if new_category is not None:
            registry.save_category(new_category)
        registry.save_social_account(account)
        return {
            "account": self.present_account(account),
            "created_category": new_category.category_id if new_category else None,
        }

    def update_account(self, account_id: str, body: SocialAccountPatch) -> dict[str, Any]:
        current = self._account(account_id)
        changes = body.model_dump(exclude_unset=True)
        if "page_handle" in changes:
            changes["account_handle"] = changes.pop("page_handle")
        self._check_env_names(changes.get("token_env_var"), changes.get("account_id_env_var"))
        try:
            account = SocialAccount.model_validate({**current.model_dump(), **changes})
        except ValidationError as exc:
            raise _validation_error(exc) from None
        self.engine.registry.save_social_account(account)
        return self.present_account(account)

    def delete_account(self, account_id: str) -> dict[str, Any]:
        account = self._account(account_id)
        self.db.delete_config("social_accounts", *account.key)
        return {"deleted": account_id}

    def verify_account(self, account_id: str) -> dict[str, Any]:
        """Live, read-only handshake with the platform API for one page (nothing is posted)."""
        import importlib

        account = self._account(account_id)
        presented = self.present_account(account)
        module_name = _VERIFY_MODULES.get(account.platform)
        if module_name is None:
            return {"account": presented, "ok": presented["status"] == "ready", "summary": "No live check for this "
                    "platform; credentials resolved." if presented["status"] == "ready" else presented["reason"],
                    "lines": []}
        try:
            module = importlib.import_module(module_name)
            results = module.verify_pages(self.engine.registry, [self.engine.registry.social_page(account)])
        except Exception as exc:
            message = redact(f"{type(exc).__name__}: {exc}")
            return {"account": presented, "ok": False, "summary": f"Check failed: {message}", "lines": []}
        if not results:
            return {"account": presented, "ok": False, "summary": "Nothing to check.", "lines": []}
        result = results[0]
        return {
            "account": presented, "ok": bool(result.ok), "summary": redact(result.summary()),
            "lines": [redact(line) for line in result.lines()], "checked_at": utcnow().isoformat(),
        }

    # -- content profiles ------------------------------------------------------- #
    def content_profiles(self) -> dict[str, Any]:
        plugins = self.engine.plugins
        models = list(dict.fromkeys(
            m for name in plugins.names("generator") for m in plugins.get("generator", name).supported_models()
        ))
        profiles = self.engine.registry.list_content_profiles()
        for profile in profiles:
            if profile.llm_model not in models:
                models.append(profile.llm_model)
        return {
            "profiles": [
                {**p.model_dump(), "model_label": model_label(p.llm_model),
                 "accounts": sum(1 for a in self.engine.registry.list_social_accounts(platform=p.platform))}
                for p in profiles
            ],
            "models": [{"id": m, "label": model_label(m), "provider": model_provider(m)} for m in models],
            "generators": plugins.names("generator"),
            "formats": list(PUBLISH_FORMATS),
            "provider_keys": {"anthropic": env_state("ANTHROPIC_API_KEY"),
                              "gemini": "Configured" if env_configured("GEMINI_API_KEY")
                              or env_configured("GOOGLE_API_KEY") else "Missing"},
        }

    def update_profile(self, content_type: str, body: ContentProfilePatch) -> dict[str, Any]:
        record = self.db.get_config("content_profiles", content_type)
        if record is None:
            raise HTTPException(status_code=404, detail=f"Unknown content type '{content_type}'.")
        current = ContentProfile.from_record(record)
        try:
            profile = ContentProfile.model_validate({**current.model_dump(), **body.model_dump(exclude_unset=True)})
            self.engine.registry.save_content_profile(profile)
        except ValidationError as exc:
            raise _validation_error(exc) from None
        except TarshishError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        return {**profile.model_dump(), "model_label": model_label(profile.llm_model)}

    # -- actions ---------------------------------------------------------------- #
    def harvest(self, body: HarvestIn) -> dict[str, Any]:
        argv = ["--harvest"]
        source, category = _all(body.source), _all(body.category_id)
        if source:
            if not self.engine.plugins.has("source", source):
                raise HTTPException(status_code=422, detail=f"Unknown source '{source}'. Available: "
                                                            f"{', '.join(self.engine.plugins.names('source'))}.")
            argv += ["--source", source]
        if category:
            self._category(category)
            argv += ["--category", category]
        if body.limit:
            argv += ["--limit", str(body.limit)]
        if body.dry_run:
            argv.append("--dry-run")
        return self.start_cli_job("harvest", argv, body.model_dump())

    def publish(self, body: PublishIn) -> dict[str, Any]:
        argv = ["--publish"]
        category, language = _all(body.category_id), _all(body.language)
        if category:
            self._category(category)
            argv += ["--category", category]
        if language:
            argv += ["--lang", language]
        if body.content_types:
            known = {p.content_type for p in self.engine.registry.list_content_profiles()}
            unknown = sorted(set(body.content_types) - known)
            if unknown:
                raise HTTPException(status_code=422, detail=f"Unknown content type(s): {', '.join(unknown)}.")
            argv += ["--content-types", ",".join(body.content_types)]
        if body.mode:
            argv += ["--mode", body.mode]
        if body.limit:
            argv += ["--limit", str(body.limit)]
        if body.dry_run:
            argv.append("--dry-run")
        if body.force:
            argv.append("--force")
        return self.start_cli_job("publish", argv, body.model_dump())

    def verify(self, body: VerifyIn) -> dict[str, Any]:
        argv = [_VERIFY_FLAGS[p] for p in dict.fromkeys(body.platforms)] or list(_VERIFY_FLAGS.values())
        if body.category_id:
            argv += ["--category", body.category_id]
        if body.language:
            argv += ["--lang", body.language]
        return self.start_cli_job("verify", argv, body.model_dump())

    def seed(self, body: SeedIn) -> dict[str, Any]:
        self._category(body.category_id)
        argv = ["--seed-temu" if body.store == "temu" else "--seed-aliexpress", "--category", body.category_id,
                "--limit", str(body.limit)]
        if body.keyword:
            argv += ["--keyword", body.keyword]
        if body.job:
            argv += ["--job", body.job]
        return self.start_cli_job("seed", argv, body.model_dump())

    def report(self, body: ReportIn) -> dict[str, Any]:
        argv = ["--stats" if body.report == "stats" else "--list-config"]
        if body.report == "stats" and body.category_id and body.language:
            argv += ["--category", body.category_id, "--lang", body.language]
        return self.start_cli_job(body.report, argv, body.model_dump())

    def import_sqlite(self, body: ImportIn) -> dict[str, Any]:
        return self.start_cli_job("import-sqlite", ["--import-sqlite", body.path], body.model_dump())

    # -- generated FindHub assets ------------------------------------------------------- #
    @staticmethod
    def present_asset(row: dict[str, Any]) -> dict[str, Any]:
        import json

        try:
            links = json.loads(row.get("links_payload_json") or "{}")
        except ValueError:
            links = {}
        try:
            images = json.loads(row.get("product_image_urls_json") or "[]")
        except ValueError:
            images = []
        publishable, reason = True, None
        if row["content_type"] == "instagram_reel":
            publishable, reason = False, ("Instagram publishes Reels from video only; post this 9:16 cover from the "
                                          "app, or enable video rendering first.")
        elif not row.get("target_account_id"):
            publishable, reason = False, f"No {row['platform']} account for {row['category_id']}/{row['language']}."
        elif row["status"] not in ("QUEUED", "FAILED"):
            publishable, reason = False, f"Status is {row['status']}."
        item = {key: value for key, value in row.items()
                if key not in ("links_payload_json", "product_image_urls_json", "locked_at")}
        item.update({
            "links": links, "product_image_url": images[0] if images else None,
            "publishing": bool(row.get("locked_at")), "publishable": publishable and not row.get("locked_at"),
            "not_publishable_reason": reason,
        })
        return item

    def assets(self, status: str | None, category: str | None, language: str | None, platform: str | None,
               content_type: str | None, limit: int) -> dict[str, Any]:
        from core.database import GENERATED_ASSET_STATUSES
        from plugins.generators.free_llm_chain import FreeLLMChain

        statuses = [s.strip().upper() for s in (status or "").split(",") if s.strip()] or None
        if statuses and any(s not in GENERATED_ASSET_STATUSES for s in statuses):
            raise HTTPException(status_code=422, detail=f"status is one of {', '.join(GENERATED_ASSET_STATUSES)}.")
        rows = self.db.list_generated_assets(statuses=statuses, category=category, language=language,
                                             platform=platform, content_type=content_type, limit=limit)
        return {
            "items": [self.present_asset(row) for row in rows],
            "counts": self.db.generated_asset_counts(),
            "llm_chain": FreeLLMChain(self.engine.settings).status(),
            "storefront_base_url": self.engine.settings.storefront_base_url,
        }

    def _asset(self, asset_id: int) -> dict[str, Any]:
        row = self.db.get_generated_asset(asset_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"No generated asset #{asset_id}.")
        return row

    def generate_assets(self, body: AssetGenerateIn) -> dict[str, Any]:
        self._category(body.category_id)
        argv = ["--generate-assets", "--category", body.category_id, "--lang", body.language,
                "--content-types", ",".join(dict.fromkeys(body.content_types)), "--limit", str(body.limit)]
        if body.force:
            argv.append("--force")
        return self.start_cli_job("generate-assets", argv, body.model_dump())

    def publish_assets(self, body: AssetPublishIn) -> dict[str, Any]:
        ids = list(dict.fromkeys(body.ids))
        problems = []
        for asset_id in ids:
            item = self.present_asset(self._asset(asset_id))
            if not item["publishable"]:
                problems.append(f"#{asset_id}: {item['not_publishable_reason'] or 'a publish is already in flight'}")
        if problems:
            raise HTTPException(status_code=409, detail="; ".join(problems))
        return self.start_cli_job("publish-assets", ["--publish-assets", ",".join(map(str, ids))], {"ids": ids})

    def set_asset_status(self, asset_id: int, body: AssetStatusIn) -> dict[str, Any]:
        row = self._asset(asset_id)
        if body.status == "QUEUED" and not row.get("target_account_id"):
            raise HTTPException(status_code=409, detail=f"Asset #{asset_id} has no target account to queue for.")
        allowed = ("DRAFT", "FAILED") if body.status == "QUEUED" else ("QUEUED", "FAILED")
        if not self.db.set_generated_asset_status(asset_id, body.status, allowed_from=allowed):
            raise HTTPException(status_code=409, detail=f"Asset #{asset_id} is {row['status']}; it cannot become {body.status}.")
        return self.present_asset(self._asset(asset_id))

    def mark_asset_published(self, asset_id: int) -> dict[str, Any]:
        self._asset(asset_id)
        if not self.db.mark_generated_asset_published(asset_id, "manual"):
            raise HTTPException(status_code=409, detail=f"Asset #{asset_id} is already published or being published.")
        return self.present_asset(self._asset(asset_id))

    def _review(self):
        from services.content_review import ContentReview

        return ContentReview(self.engine)

    def update_asset(self, asset_id: int, body: AssetPatch) -> dict[str, Any]:
        stickers = [link.model_dump() for link in body.story_links] if body.story_links is not None else None
        with review_errors():
            row = self._review().edit_asset(asset_id, generated_text=body.generated_text,
                                            reel_caption=body.reel_caption, story_links=stickers)
        return self.present_asset(row)

    def delete_asset(self, asset_id: int) -> dict[str, Any]:
        with review_errors():
            return self._review().delete_asset(asset_id)

    # -- queue ------------------------------------------------------------------ #
    @staticmethod
    def _pipeline(scope: QueueScope) -> str | None:
        from core.orchestrator import pipeline_id_for

        if bool(scope.category_id) != bool(scope.language):
            raise HTTPException(status_code=422, detail="Narrow by both category and language, or by neither.")
        return pipeline_id_for(scope.category_id, scope.language) if scope.category_id else None

    def queue(self, status: str | None, category: str | None, language: str | None, platform: str | None,
              limit: int) -> dict[str, Any]:
        statuses = [s.strip().upper() for s in (status or "").split(",") if s.strip()] or None
        if statuses and any(s not in ("PENDING", "QUEUED", "PUBLISHED", "FAILED") for s in statuses):
            raise HTTPException(status_code=422, detail="status is one of QUEUED, PENDING, PUBLISHED, FAILED.")
        rows = self.db.list_publications(statuses, limit=limit, category=category, language=language,
                                         account_platform=platform)
        return {"items": rows, "counts": self.db.get_status_counts()}

    def _publication(self, publication_id: int) -> dict[str, Any]:
        row = self.db.get_publication(publication_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"No publication #{publication_id}.")
        return row

    def retry(self, publication_id: int) -> dict[str, Any]:
        row = self._publication(publication_id)
        if row["status"] != "FAILED":
            raise HTTPException(status_code=409, detail=f"Publication #{publication_id} is {row['status']}, not FAILED.")
        self.db.retry_failed([publication_id])
        return {"id": publication_id, "status": "QUEUED"}

    def publish_now(self, publication_id: int) -> dict[str, Any]:
        row = self._publication(publication_id)
        if row["status"] not in ("QUEUED", "FAILED"):
            raise HTTPException(status_code=409, detail=f"Publication #{publication_id} is {row['status']}.")
        engine = self.engine

        def action(_job: Job) -> int:
            from services.content_review import ContentReview

            print(f"Publishing #{publication_id} → {row['platform']} ({row['target_page']}) now, outside the pacing schedule.")
            result = ContentReview(engine).publish_publication(publication_id)
            if result.success:
                print(f"✓ Published. Post id {result.platform_post_id}.")
                return 0
            print(f"✗ Failed: {result.message}")
            return 1

        return self.start_job("publish-now", {"publication_id": publication_id}, action)

    def update_publication(self, publication_id: int, body: QueuePatch) -> dict[str, Any]:
        with review_errors():
            return self._review().edit_publication(publication_id, body.caption)

    def delete_publication(self, publication_id: int) -> dict[str, Any]:
        with review_errors():
            return self._review().delete_publication(publication_id)

    def flush(self, scope: QueueScope) -> dict[str, Any]:
        return {"flushed": self.db.flush_queue(self._pipeline(scope))}

    def retry_failed(self, scope: QueueScope) -> dict[str, Any]:
        return {"requeued": self.db.retry_failed(pipeline_id=self._pipeline(scope))}


# --------------------------------------------------------------------------- #
# Router
# --------------------------------------------------------------------------- #
def build_router(ctx: AdminContext) -> APIRouter:
    service = AdminService(ctx)
    router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])

    @router.get("/session")
    def session() -> dict[str, Any]:
        """Cheap authenticated ping: the portal uses it to check the backend accepts its key."""
        return {"ok": True, "database": service.db.describe(), "background_jobs": not ctx.serverless}

    @router.get("/overview")
    def overview() -> dict[str, Any]:
        return service.overview()

    @router.get("/env-check")
    def env_check(names: str = Query(..., max_length=2000, description="Comma-separated variable NAMES")) -> dict[str, Any]:
        found = {}
        for name in (n.strip() for n in names.split(",")):
            if name and _ENV_NAME_RE.fullmatch(name):
                found[name] = env_state(name)
        return {"vars": found}

    @router.get("/platforms")
    def platforms() -> dict[str, Any]:
        return {"platforms": PLATFORM_FIELDS}

    # categories
    @router.get("/categories")
    def list_categories() -> dict[str, Any]:
        return {"categories": service.list_categories()}

    @router.post("/categories", status_code=201)
    def create_category(body: CategoryIn) -> dict[str, Any]:
        return service.create_category(body)

    @router.put("/categories/{category_id}")
    def update_category(body: CategoryPatch, category_id: str = PathParam(max_length=64)) -> dict[str, Any]:
        return service.update_category(category_id, body)

    @router.delete("/categories/{category_id}")
    def delete_category(category_id: str = PathParam(max_length=64)) -> dict[str, Any]:
        return service.delete_category(category_id)

    # social accounts
    @router.get("/social-accounts")
    def list_accounts() -> dict[str, Any]:
        return {"accounts": service.list_accounts(), "platforms": PLATFORM_FIELDS}

    @router.post("/social-accounts", status_code=201)
    def create_account(body: SocialAccountIn) -> dict[str, Any]:
        return service.create_account(body)

    @router.put("/social-accounts/{account_id}")
    def update_account(body: SocialAccountPatch, account_id: str = PathParam(max_length=200)) -> dict[str, Any]:
        return service.update_account(account_id, body)

    @router.delete("/social-accounts/{account_id}")
    def delete_account(account_id: str = PathParam(max_length=200)) -> dict[str, Any]:
        return service.delete_account(account_id)

    @router.post("/actions/verify-account/{account_id}")
    def verify_account(account_id: str = PathParam(max_length=200)) -> dict[str, Any]:
        return service.verify_account(account_id)

    # content profiles
    @router.get("/content-profiles")
    def content_profiles() -> dict[str, Any]:
        return service.content_profiles()

    @router.put("/content-profiles/{content_type}")
    def update_profile(body: ContentProfilePatch, content_type: str = PathParam(max_length=64)) -> dict[str, Any]:
        return service.update_profile(content_type, body)

    # actions (background jobs)
    @router.post("/actions/harvest", status_code=202)
    def harvest(body: HarvestIn) -> dict[str, Any]:
        return service.harvest(body)

    @router.post("/actions/publish", status_code=202)
    def publish(body: PublishIn) -> dict[str, Any]:
        return service.publish(body)

    @router.post("/actions/verify", status_code=202)
    def verify(body: VerifyIn | None = None) -> dict[str, Any]:
        return service.verify(body or VerifyIn())

    @router.post("/actions/seed", status_code=202)
    def seed(body: SeedIn) -> dict[str, Any]:
        return service.seed(body)

    @router.post("/actions/seed-storefront", status_code=202)
    def seed_storefront() -> dict[str, Any]:
        return service.start_cli_job("seed-storefront", ["--seed-storefront"], {})

    @router.post("/actions/sync-config", status_code=202)
    def sync_config() -> dict[str, Any]:
        return service.start_cli_job("sync-config", ["--sync-config"], {})

    @router.post("/actions/worker", status_code=202)
    def worker_once() -> dict[str, Any]:
        return service.start_cli_job("worker", ["--mode", "worker", "--once"], {})

    @router.post("/actions/report", status_code=202)
    def report(body: ReportIn) -> dict[str, Any]:
        return service.report(body)

    @router.post("/actions/import-sqlite", status_code=202)
    def import_sqlite(body: ImportIn) -> dict[str, Any]:
        return service.import_sqlite(body)

    # jobs
    @router.get("/jobs")
    def list_jobs(limit: int = Query(30, ge=1, le=200), kind: str | None = Query(None, max_length=40)) -> dict[str, Any]:
        return {"jobs": [present_job(row) for row in service.db.list_job_runs(limit=limit, kind=kind)]}

    @router.get("/jobs/{job_id}")
    def get_job(job_id: str = PathParam(max_length=64), since: int = Query(0, ge=0)) -> dict[str, Any]:
        job = ctx.runner().get(job_id, since=since)
        if job is None:
            raise HTTPException(status_code=404, detail=f"No job '{job_id}'.")
        return job

    # generated FindHub assets
    @router.get("/assets")
    def list_assets(
        status: str | None = Query(None, max_length=60, description="DRAFT,QUEUED,PUBLISHED,FAILED (comma-separated)"),
        category: str | None = Query(None, max_length=64),
        language: str | None = Query(None, max_length=12),
        platform: str | None = Query(None, max_length=40),
        content_type: str | None = Query(None, max_length=40),
        limit: int = Query(100, ge=1, le=500),
    ) -> dict[str, Any]:
        return service.assets(status, category, language, platform, content_type, limit)

    @router.post("/assets/generate", status_code=202)
    def generate_assets(body: AssetGenerateIn) -> dict[str, Any]:
        return service.generate_assets(body)

    @router.post("/assets/publish", status_code=202)
    def publish_assets(body: AssetPublishIn) -> dict[str, Any]:
        return service.publish_assets(body)

    @router.post("/assets/{asset_id}/publish", status_code=202)
    def publish_asset(asset_id: int = PathParam(ge=1)) -> dict[str, Any]:
        return service.publish_assets(AssetPublishIn(ids=[asset_id]))

    @router.post("/assets/{asset_id}/status")
    def set_asset_status(body: AssetStatusIn, asset_id: int = PathParam(ge=1)) -> dict[str, Any]:
        return service.set_asset_status(asset_id, body)

    @router.post("/assets/{asset_id}/mark-published")
    def mark_asset_published(asset_id: int = PathParam(ge=1)) -> dict[str, Any]:
        return service.mark_asset_published(asset_id)

    @router.patch("/assets/{asset_id}")
    def update_asset(body: AssetPatch, asset_id: int = PathParam(ge=1)) -> dict[str, Any]:
        return service.update_asset(asset_id, body)

    @router.delete("/assets/{asset_id}")
    def delete_asset(asset_id: int = PathParam(ge=1)) -> dict[str, Any]:
        return service.delete_asset(asset_id)

    # publication queue
    @router.get("/queue")
    def queue(
        status: str | None = Query(None, max_length=60, description="QUEUED,PUBLISHED,FAILED,PENDING (comma-separated)"),
        category: str | None = Query(None, max_length=64),
        language: str | None = Query(None, max_length=12),
        platform: str | None = Query(None, max_length=40),
        limit: int = Query(100, ge=1, le=500),
    ) -> dict[str, Any]:
        return service.queue(status, category, language, platform, limit)

    @router.post("/queue/flush")
    def flush(scope: QueueScope | None = None) -> dict[str, Any]:
        return service.flush(scope or QueueScope())

    @router.post("/queue/retry-failed")
    def retry_failed(scope: QueueScope | None = None) -> dict[str, Any]:
        return service.retry_failed(scope or QueueScope())

    @router.post("/queue/{publication_id}/retry")
    def retry(publication_id: int = PathParam(ge=1)) -> dict[str, Any]:
        return service.retry(publication_id)

    @router.post("/queue/{publication_id}/publish-now", status_code=202)
    def publish_now(publication_id: int = PathParam(ge=1)) -> dict[str, Any]:
        return service.publish_now(publication_id)

    @router.patch("/queue/{publication_id}")
    def update_publication(body: QueuePatch, publication_id: int = PathParam(ge=1)) -> dict[str, Any]:
        return service.update_publication(publication_id, body)

    @router.delete("/queue/{publication_id}")
    def delete_publication(publication_id: int = PathParam(ge=1)) -> dict[str, Any]:
        return service.delete_publication(publication_id)

    return router


def mount_admin(app: FastAPI, settings: Settings) -> AdminContext:
    """Attach the admin API to ``app``; the engine is only built when an admin request arrives."""
    ctx = AdminContext(settings)
    app.include_router(build_router(ctx))
    app.state.admin = ctx
    if admin_secret_problem(Settings.get_env(ADMIN_SECRET_ENV)):
        logger.info("Admin API mounted but disabled: set %s (16+ characters) to enable it.", ADMIN_SECRET_ENV)
    return ctx
