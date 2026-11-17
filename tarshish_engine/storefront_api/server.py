"""FindHub storefront API: products, categories and outbound click tracking.

Run from the project root (the folder that holds ``main.py``):

    python -m uvicorn storefront_api.server:app --reload --port 8000

Endpoints
---------
    GET  /api/products                   search, filter, sort and page through listed products
    GET  /api/products/{product_id}      one product (deep links and Quick View)
    GET  /api/categories                 active categories with product counts
    POST /api/track-click/{product_id}   log a confirmed outbound click, return the affiliate link
    GET  /api/health                     liveness + number of listed products
    *    /api/admin/...                   the admin control plane (X-Admin-Key; see storefront_api/admin.py)

The API reads and writes the same database as the pipelines and the dashboard: Supabase
PostgreSQL in production (``DATABASE_URL``), a local SQLite file otherwise (``DATABASE_PATH``).
On Vercel it is served by ``api/index.py``. It only ever lists products that have a category and
meet the storefront standard (``STOREFRONT_MIN_RATING`` / ``STOREFRONT_MIN_ORDERS``),
and it never puts an affiliate link in a listing: the link is handed out by
``track-click`` alone, so every outbound visit is counted.

Interactive documentation: http://localhost:8000/api/docs
"""

from __future__ import annotations

import json
import math
import os
import re
from datetime import timedelta
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Path as PathParam, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from storefront_api import search
from storefront_api.admin import mount_admin
from core.config import Settings, default_env_file, load_settings
from core.database import Database
from core.models import utcnow
from core.resilience import get_logger, register_env_secrets, setup_logging

logger = get_logger("api")

SortBy = Literal["rating_desc", "price_asc", "price_desc", "orders_desc", "discount_desc"]

DEFAULT_ORIGINS = "http://localhost:3000,http://127.0.0.1:3000"
CROWD_FAVORITE_MIN_ORDERS = 1000
CROWD_FAVORITE_MIN_RATING = 4.8
HIGH_DEMAND_WINDOW_DAYS = 7
HIGH_DEMAND_MIN_CLICKS = 5       # outbound clicks inside the window that earn "high demand this week"

_STORE_NAMES = {"aliexpress": "AliExpress", "temu": "Temu", "amazon": "Amazon", "ebay": "eBay"}
# Words a shopper may type for a category, searched alongside the product titles.
_CATEGORY_WORDS = {
    "sports": "sports sport fitness running ספורט כושר ריצה",
    "gadgets": "gadgets gadget tech electronics גאדג'טים גאדג'ט טכנולוגיה אלקטרוניקה",
    "home": "home kitchen household בית מטבח לבית",
}
_LANGUAGE_RE = re.compile(r"[a-z]{2,3}(-[a-z0-9]{2,8})?")

# The same orderings as core.database.STOREFRONT_ORDER, for results ranked in Python (search).
_SORT_KEYS = {
    "rating_desc": lambda row: (-row["rating"], -row["orders_count"], row["id"]),
    "price_asc": lambda row: (row["price"], row["id"]),
    "price_desc": lambda row: (-row["price"], row["id"]),
    "orders_desc": lambda row: (-row["orders_count"], -row["rating"], row["id"]),
    "discount_desc": lambda row: (-(row["discount_percent"] or 0), -row["orders_count"], row["id"]),
}


# --------------------------------------------------------------------------- #
# Response models
# --------------------------------------------------------------------------- #
class Store(BaseModel):
    key: str = Field(description="Stable store id used by the `source` filter, e.g. 'aliexpress'.")
    name: str = Field(description="Display name, e.g. 'AliExpress'.")


class ProductOut(BaseModel):
    id: int
    title: str
    title_he: str | None
    category: str
    source: str
    store: Store
    price: float
    original_price: float | None
    discount_percent: int | None
    currency: str
    rating: float
    orders_count: int
    image_urls: list[str]
    coupon_code: str | None
    is_flash_sale: bool
    stock_urgency_label: str | None
    badges: list[Literal["crowd_favorite", "flash_deal", "verified"]]
    social_proof: Literal["high_demand", "top_rated", "most_ordered"] | None


class StoreFacet(Store):
    products: int


class Facets(BaseModel):
    stores: list[StoreFacet]
    price_min: float | None
    price_max: float | None


class ProductPage(BaseModel):
    items: list[ProductOut]
    total: int
    page: int
    page_size: int
    pages: int
    sort_by: str
    facets: Facets


class CategoryOut(BaseModel):
    key: str
    products: int


class Standards(BaseModel):
    min_rating: float
    min_orders: int


class CategoriesOut(BaseModel):
    categories: list[CategoryOut]
    total: int
    standards: Standards


class ClickIn(BaseModel):
    language: str = Field(default="", max_length=16, description="Storefront language, e.g. 'he'.")
    referrer_path: str = Field(default="", max_length=2000, description="Storefront path the click came from.")


class ClickOut(BaseModel):
    product_id: int
    affiliate_url: str
    store: Store


# --------------------------------------------------------------------------- #
# Catalogue
# --------------------------------------------------------------------------- #
def store_for(source: str) -> dict[str, str]:
    """'aliexpress_mock' -> {'key': 'aliexpress', 'name': 'AliExpress'}."""
    key = source.casefold().removesuffix("_mock")
    return {"key": key, "name": _STORE_NAMES.get(key, key.replace("_", " ").title())}


def _search_texts(row: dict[str, Any]) -> tuple[str, ...]:
    store = store_for(row["source"])
    return (
        row["title"], row["title_he"] or "",
        f"{row['category']} {_CATEGORY_WORDS.get(row['category'], '')}", store["name"],
    )


class Catalogue:
    """Read side of the storefront: turns database rows into what the API returns."""

    def __init__(self, db: Database, *, min_rating: float, min_orders: int) -> None:
        self.db = db
        self.min_rating = min_rating
        self.min_orders = min_orders

    def _context(self) -> dict[str, Any]:
        """Facts needed to badge a product honestly: category leaders and this week's clicks."""
        stats = self.db.storefront_category_stats(min_rating=self.min_rating, min_orders=self.min_orders)
        return {
            "leaders": {row["category"]: row for row in stats},
            "clicks": self.db.click_counts_since(utcnow() - timedelta(days=HIGH_DEMAND_WINDOW_DAYS)),
        }

    def _present(self, row: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        badges = []
        if row["orders_count"] >= CROWD_FAVORITE_MIN_ORDERS and row["rating"] >= CROWD_FAVORITE_MIN_RATING:
            badges.append("crowd_favorite")
        if row["is_flash_sale"]:
            badges.append("flash_deal")
        badges.append("verified")  # only products that pass the storefront standard are ever listed

        leader = context["leaders"].get(row["category"], {})
        if context["clicks"].get(row["id"], 0) >= HIGH_DEMAND_MIN_CLICKS:
            social_proof = "high_demand"
        elif leader.get("products", 0) > 1 and row["rating"] == leader.get("top_rating"):
            social_proof = "top_rated"
        elif leader.get("products", 0) > 1 and row["orders_count"] == leader.get("top_orders"):
            social_proof = "most_ordered"
        else:
            social_proof = None

        discounted = bool(row["original_price"] and row["original_price"] > row["price"])
        try:
            images = [url for url in json.loads(row["image_urls_json"] or "[]") if isinstance(url, str)]
        except json.JSONDecodeError:
            images = []
        return {
            "id": row["id"], "title": row["title"], "title_he": row["title_he"], "category": row["category"],
            "source": row["source"], "store": store_for(row["source"]),
            "price": row["price"],
            "original_price": row["original_price"] if discounted else None,
            "discount_percent": row["discount_percent"] if discounted else None,
            "currency": row["currency"], "rating": row["rating"], "orders_count": row["orders_count"],
            "image_urls": images, "coupon_code": row["coupon_code"] or None,
            "is_flash_sale": bool(row["is_flash_sale"]),
            "stock_urgency_label": row["stock_urgency_label"] or None,
            "badges": badges, "social_proof": social_proof,
        }

    def list_products(
        self, *, q: str | None, category: str | None, source: str | None, min_rating: float | None,
        min_orders: int | None, min_price: float | None, max_price: float | None, sort_by: str | None,
        page: int, page_size: int,
    ) -> dict[str, Any]:
        filters = {
            "category": category or None,
            "store": store_for(source)["key"] if source else None,
            "min_rating": max(self.min_rating, min_rating or 0.0),
            "min_orders": max(self.min_orders, min_orders or 0),
            "min_price": min_price,
            "max_price": max_price,
        }
        offset = (page - 1) * page_size
        query = (q or "").strip()
        if query:
            # Structured filters run in SQL; relevance is ranked here, over the rows that survive them.
            rows, _ = self.db.list_storefront_products(**filters)
            matches = [row for row, _score in search.rank(query, rows, _search_texts)]
            if sort_by:
                matches.sort(key=_SORT_KEYS[sort_by])
            total, rows, applied = len(matches), matches[offset: offset + page_size], sort_by or "relevance"
        else:
            applied = sort_by or "recommended"
            rows, total = self.db.list_storefront_products(sort_by=applied, limit=page_size, offset=offset, **filters)

        context = self._context()
        scope = self.db.storefront_facets(
            category=category or None, min_rating=self.min_rating, min_orders=self.min_orders
        )
        stores: dict[str, dict[str, Any]] = {}
        for entry in scope["sources"]:
            store = store_for(entry["source"])
            stores.setdefault(store["key"], {**store, "products": 0})["products"] += entry["products"]
        return {
            "items": [self._present(row, context) for row in rows],
            "total": total, "page": page, "page_size": page_size,
            "pages": max(1, math.ceil(total / page_size)),
            "sort_by": applied,
            "facets": {
                "stores": sorted(stores.values(), key=lambda store: store["name"]),
                "price_min": scope["price_min"], "price_max": scope["price_max"],
            },
        }

    def get_row(self, product_id: int) -> dict[str, Any] | None:
        return self.db.get_storefront_product(product_id, min_rating=self.min_rating, min_orders=self.min_orders)

    def get_product(self, product_id: int) -> dict[str, Any] | None:
        row = self.get_row(product_id)
        return self._present(row, self._context()) if row else None

    def categories(self) -> dict[str, Any]:
        stats = self.db.storefront_category_stats(min_rating=self.min_rating, min_orders=self.min_orders)
        return {
            "categories": [{"key": row["category"], "products": row["products"]} for row in stats],
            "total": sum(row["products"] for row in stats),
            "standards": {"min_rating": self.min_rating, "min_orders": self.min_orders},
        }


def _clean_referrer(value: str) -> str:
    """Keep a same-site path such as '/he/c/sports'; drop anything else (full URLs, query strings)."""
    path = value.strip().split("?", 1)[0].split("#", 1)[0]
    if not path.startswith("/") or path.startswith("//"):
        return ""
    return path[:200]


# --------------------------------------------------------------------------- #
# Application
# --------------------------------------------------------------------------- #
def _number(settings: Settings, name: str, default: float) -> float:
    raw = settings.get_env(name)
    try:
        return float(raw) if raw is not None else default
    except ValueError:
        logger.warning("%s=%r is not a number; using %s.", name, raw, default)
        return default


def allowed_origins(settings: Settings) -> list[str]:
    """Browser origins allowed to call the API: ``ALLOWED_ORIGINS`` plus the older ``STOREFRONT_ORIGINS``.

    The Next.js storefront and admin portal proxy every call server-side, so a browser normally
    never calls the API cross-origin; list your Vercel domain(s) here for any other front end.
    """
    configured = [settings.get_env(name) for name in ("ALLOWED_ORIGINS", "STOREFRONT_ORIGINS")]
    raw = ",".join(value for value in configured if value) or DEFAULT_ORIGINS
    origins = [origin.strip().rstrip("/") for origin in raw.split(",")]
    return list(dict.fromkeys(origin for origin in origins if origin))


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the API on top of the engine's settings and database."""
    if settings is None:
        settings = load_settings(default_env_file())
    # Serverless hosts (Vercel) have a read-only filesystem and collect stdout/stderr as the log.
    setup_logging(settings.log_level, None if os.environ.get("VERCEL") else settings.log_file, console=True)
    register_env_secrets(os.environ)  # the same redaction the CLI and the dashboard log with

    db = Database.from_settings(settings)
    db.initialize()
    catalogue = Catalogue(
        db,
        min_rating=_number(settings, "STOREFRONT_MIN_RATING", 4.5),
        min_orders=int(_number(settings, "STOREFRONT_MIN_ORDERS", 200)),
    )
    listed = catalogue.categories()["total"]
    if listed == 0:
        logger.warning("The storefront has no products yet. Load the demo catalogue: python main.py --seed-storefront")
    else:
        logger.info("Storefront API ready: %d listed product(s), search engine '%s'.", listed, search.ENGINE)

    app = FastAPI(
        title="FindHub Storefront API",
        description="Public catalogue, search and click tracking for the FindHub storefront.",
        version="1.0.0",
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins(settings),
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Content-Type", "X-Admin-Key"],
    )
    app.state.catalogue = catalogue
    mount_admin(app, settings)

    # Public catalogue reads may be cached by the CDN in front of the API (Vercel's edge) for a
    # short while; clicks and health checks never are. STOREFRONT_CACHE_SECONDS=0 turns it off.
    cache_seconds = int(_number(settings, "STOREFRONT_CACHE_SECONDS", 60))

    @app.middleware("http")
    async def cache_headers(request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        path = request.url.path
        cacheable = (
            request.method == "GET" and response.status_code == 200 and cache_seconds > 0
            and path.startswith(("/api/products", "/api/categories"))
        )
        if cacheable:
            response.headers.setdefault(
                "Cache-Control", f"public, max-age=0, s-maxage={cache_seconds}, stale-while-revalidate={cache_seconds * 5}"
            )
        else:
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.get("/api/health", tags=["status"])
    def health() -> dict[str, Any]:
        return {"status": "ok", "products": catalogue.categories()["total"], "search_engine": search.ENGINE}

    @app.get("/api/products", response_model=ProductPage, tags=["catalogue"])
    def list_products(
        q: str | None = Query(None, max_length=80, description="Search text, Hebrew or English; typos are forgiven."),
        category: str | None = Query(None, max_length=64, description="Category key, e.g. 'sports'."),
        source: str | None = Query(None, max_length=40, description="Store, e.g. 'aliexpress' or 'temu'."),
        min_rating: float | None = Query(None, ge=0, le=5),
        min_orders: int | None = Query(None, ge=0),
        min_price: float | None = Query(None, ge=0),
        max_price: float | None = Query(None, ge=0),
        sort_by: SortBy | None = Query(
            None, description="Default: best match when searching, otherwise the recommended order."
        ),
        page: int = Query(1, ge=1, le=10_000),
        page_size: int = Query(24, ge=1, le=60),
    ) -> dict[str, Any]:
        return catalogue.list_products(
            q=q, category=category, source=source, min_rating=min_rating, min_orders=min_orders,
            min_price=min_price, max_price=max_price, sort_by=sort_by, page=page, page_size=page_size,
        )

    @app.get("/api/products/{product_id}", response_model=ProductOut, tags=["catalogue"])
    def get_product(product_id: int = PathParam(ge=1)) -> dict[str, Any]:
        product = catalogue.get_product(product_id)
        if product is None:
            raise HTTPException(status_code=404, detail="Product not found")
        return product

    @app.get("/api/categories", response_model=CategoriesOut, tags=["catalogue"])
    def list_categories() -> dict[str, Any]:
        return catalogue.categories()

    @app.post("/api/track-click/{product_id}", response_model=ClickOut, tags=["tracking"])
    def track_click(product_id: int = PathParam(ge=1), click: ClickIn | None = None) -> dict[str, Any]:
        row = catalogue.get_row(product_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Product not found")
        affiliate_url = str(row["affiliate_url"] or "")
        if not re.match(r"https?://\S+$", affiliate_url):
            logger.error("Product %s has no usable affiliate link; click not recorded.", product_id)
            raise HTTPException(status_code=409, detail="This deal has no working link right now")
        click = click or ClickIn()
        language = click.language.strip().lower()
        db.record_click_event(
            product_id, category=row["category"], source=row["source"],
            language=language if _LANGUAGE_RE.fullmatch(language) else "unknown",
            referrer_path=_clean_referrer(click.referrer_path),
        )
        return {"product_id": product_id, "affiliate_url": affiliate_url, "store": store_for(row["source"])}

    return app


_app: FastAPI | None = None


def __getattr__(name: str) -> Any:
    """Build ``app`` the first time it is asked for (``uvicorn storefront_api.server:app``).

    Importing this module therefore has no side effects: no ``.env`` is read and no
    database is opened until a server, or a test calling ``create_app``, needs one.
    """
    global _app
    if name == "app":
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
