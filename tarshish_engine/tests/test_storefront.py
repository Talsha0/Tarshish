"""The FindHub storefront backend: schema migration, seeding, search, the REST API and click metrics."""

import json
import re
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import search
from api.seed import seed_storefront
from api.server import create_app
from core.database import PRODUCT_STOREFRONT_COLUMNS, Database, category_slug
from core.models import Product, utcnow
from dashboard.metrics_service import MetricsService

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def api(make_settings):
    """The API over a temp database that holds the demo catalogue."""
    settings = make_settings()
    app = create_app(settings)
    seed_storefront(app.state.catalogue.db)
    with TestClient(app) as client:
        client.db = app.state.catalogue.db
        client.settings = settings
        yield client


def ids(response) -> list[int]:
    return [item["id"] for item in response.json()["items"]]


def titles(response) -> list[str]:
    return [item["title"] for item in response.json()["items"]]


# -- database ------------------------------------------------------------------------- #
LEGACY = "ni" + "che"  # the pre-v3 name of products.category
V1_PRODUCTS = """
CREATE TABLE products (
    id INTEGER PRIMARY KEY AUTOINCREMENT, external_id TEXT NOT NULL, source TEXT NOT NULL,
    canonical_hash TEXT NOT NULL, {legacy} TEXT NOT NULL, title TEXT NOT NULL, price REAL NOT NULL,
    currency TEXT NOT NULL DEFAULT 'ILS', affiliate_url TEXT NOT NULL, created_at TEXT NOT NULL,
    UNIQUE (source, external_id)
);
INSERT INTO products (external_id, source, canonical_hash, {legacy}, title, price, affiliate_url, created_at)
VALUES ('1005006482913507', 'aliexpress_mock', 'abc', 'running_and_sports', 'Old row', 10, 'https://a.example/1',
        '2026-11-01T00:00:00+00:00');
""".replace("{legacy}", LEGACY)


def test_a_database_from_before_the_storefront_is_upgraded_in_place(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(V1_PRODUCTS)

    db = Database(path)
    db.initialize()
    db.initialize()  # idempotent

    with db.connect() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(products)")}
        tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        clicks = {row["name"] for row in conn.execute("PRAGMA table_info(click_events)")}
        assert conn.execute("PRAGMA journal_mode;").fetchone()[0] == "wal"
    assert set(PRODUCT_STOREFRONT_COLUMNS) <= columns
    assert {"category", "original_price", "discount_percent", "coupon_code", "is_flash_sale",
            "stock_urgency_label"} <= columns
    assert "click_events" in tables
    assert clicks == {"id", "product_id", "category", "source", "language", "referrer_path", "clicked_at"}

    # The old row survives; its legacy category column is folded into `category` (rating unknown: not listed yet).
    assert LEGACY not in columns
    assert db.query_one("SELECT title, category, is_flash_sale FROM products WHERE id = 1") == {
        "title": "Old row", "category": "running-and-sports", "is_flash_sale": 0,
    }
    assert db.list_storefront_products() == ([], 0)
    seed_storefront(db)
    row = db.query_one("SELECT id, title, category FROM products WHERE external_id = '1005006482913507'")
    assert row["id"] == 1 and row["category"] == "sports" and row["title"].startswith("Men's Lightweight")


def test_seeder_loads_a_rich_catalogue_and_is_repeatable(engine):
    assert seed_storefront(engine.db) == 14
    assert seed_storefront(engine.db) == 14  # an upsert: no duplicates
    rows, total = engine.db.list_storefront_products()
    assert total == 14 >= 12
    assert {row["category"] for row in rows} == {"sports", "gadgets", "home"}
    assert {row["source"] for row in rows} == {"aliexpress_mock", "temu_mock"}
    for row in rows:
        assert 4.6 <= row["rating"] <= 4.9 and 350 <= row["orders_count"] <= 5300
        assert row["original_price"] > row["price"] and 1 <= row["discount_percent"] <= 90
        assert row["title_he"] and re.search(r"[֐-׿]", row["title_he"])
        images = json.loads(row["image_urls_json"])
        assert len(images) == 3 and all(url.startswith("https://") and "1080x1080" in url for url in images)
    assert sum(1 for row in rows if row["coupon_code"]) >= 5
    assert sum(1 for row in rows if row["is_flash_sale"]) >= 3
    assert {row["stock_urgency_label"] for row in rows} >= {"Limited Stock", "Top 1% Deal"}


def test_a_harvest_upsert_refreshes_source_data_but_keeps_the_listing(engine):
    seed_storefront(engine.db)
    before = engine.db.query_one("SELECT * FROM products WHERE external_id = '1005006482913507'")
    assert before["coupon_code"] == "RUN15" and before["category"] == "sports"

    # What a source reports: the product, no listing details.
    shoe = Product(
        external_id="1005006482913507", source="aliexpress_mock", category="sports",
        title=before["title"], price=179.90, original_price=329.00, rating=4.9, orders_count=3500,
        raw_url="https://www.aliexpress.com/item/1005006482913507.html", affiliate_url=before["affiliate_url"],
    )
    assert engine.db.upsert_product(shoe) == before["id"]

    after = engine.db.query_one("SELECT * FROM products WHERE id = ?", (before["id"],))
    assert (after["price"], after["rating"], after["orders_count"], after["discount_percent"]) == (179.90, 4.9, 3500, 45)
    assert (after["category"], after["coupon_code"], after["title_he"]) == ("sports", "RUN15", before["title_he"])

    # A product the storefront has never seen is filed under its category.
    mat = Product(external_id="d1", source="demo_feed", category="Yoga & Pilates", title="Demo Yoga Mat 6mm", price=59.9,
                  rating=4.9, orders_count=900, raw_url="https://shop.example/d1", affiliate_url="https://shop.example/aff/d1")
    new_id = engine.db.upsert_product(mat)
    assert engine.db.get_storefront_product(new_id)["category"] == "yoga-pilates" == category_slug("Yoga & Pilates")
    with pytest.raises(ValueError, match="unknown listing field"):
        engine.db.upsert_product(mat, {"price": 1})


def test_harvesting_feeds_the_storefront(engine):
    """Single source of truth: what Stage 1 harvests is what the storefront lists (and what gets published)."""
    report = engine.harvester.harvest("sports", "aliexpress_mock", limit=1)[0]
    assert report.ok and len(report.inserted) == 1
    rows, total = engine.db.list_storefront_products()
    assert total == 1 and rows[0]["category"] == "sports" and rows[0]["rating"] >= 4.5
    assert json.loads(rows[0]["image_urls_json"])


def test_mock_sources_serve_their_own_shelves(engine):
    from core.interfaces import PluginContext

    context = PluginContext(settings=engine.settings)
    ali = engine.plugins.get("source", "aliexpress_mock")(context)
    temu = engine.plugins.get("source", "temu_mock")(context)
    assert [p.source for p in temu.fetch_products("running_and_sports", 0, 0, 10)] == ["temu_mock"] * 2
    assert len(ali.fetch_products("smart_gadgets", 0, 0, 10)) == 2
    assert len(temu.fetch_products("home", 4.85, 0, 10)) == 1
    assert len(ali.fetch_products("anything_else", 0, 0, 20)) == 7  # no shelf named: the whole range


# -- search --------------------------------------------------------------------------- #
@pytest.mark.parametrize("engine_name", ["rapidfuzz", "python"])
def test_search_forgives_typos_and_partial_words_in_both_languages(engine_name, monkeypatch):
    if engine_name == "python":
        monkeypatch.setattr(search, "edit_distance", search._py_osa_distance)
    elif search.ENGINE != "rapidfuzz":
        pytest.skip("rapidfuzz is not available on this machine")
    watch = ("GPS Running Smartwatch - AMOLED Display", "שעון ריצה חכם עם GPS - מסך AMOLED")
    earbuds = ("Wireless Earbuds with Active Noise Cancelling", "אוזניות אלחוטיות עם ביטול רעשים אקטיבי")

    for query in ("smartwatch", "smartwacth", "smartwach", "smartw", "SMART-WATCH gps", "שעון", "שעונים", "שעון חכמ",
                  "לריצה", "runing smartwatch"):
        assert search.match_score(query, watch) > 0, query
        assert search.match_score(query, earbuds) == 0, query
    for query in ("earbuds", "earbds", "erbuds", "wireles earbud", "אוזניות", "אוזניו", "אוזנית", "ביטול רעש"):
        assert search.match_score(query, earbuds) > 0, query
        assert search.match_score(query, watch) == 0, query

    assert search.match_score("watch", watch) > search.match_score("wacth", watch) > 0   # exact beats typo
    assert search.match_score("gps", watch) > 0 and search.match_score("gpx", watch) == 0  # short words: no typos
    assert search.match_score("", watch) == 0 and search.match_score("   ", watch) == 0
    ranked = search.rank("earbuds", ["a", "b"], lambda item: watch if item == "a" else earbuds)
    assert [item for item, _ in ranked] == ["b"]


def test_pure_python_distance_agrees_with_known_values():
    distance = search._py_osa_distance
    assert distance("watch", "watch", 2) == 0
    assert distance("wacth", "watch", 2) == 1      # adjacent swap
    assert distance("wach", "watch", 2) == 1       # missing letter
    assert distance("watchh", "watch", 2) == 1     # extra letter
    assert distance("kitten", "sitting", 2) == 3   # above the cutoff -> cutoff + 1
    assert distance("abc", "abcdefg", 2) == 3


# -- API ------------------------------------------------------------------------------ #
def test_products_endpoint_lists_filters_sorts_and_pages(api):
    body = api.get("/api/products").json()
    assert body["total"] == 14 and body["page"] == 1 and body["pages"] == 1 and body["sort_by"] == "recommended"
    first = body["items"][0]
    assert first["is_flash_sale"] and "affiliate_url" not in first
    assert set(first) == {
        "id", "title", "title_he", "category", "source", "store", "price", "original_price", "discount_percent",
        "currency", "rating", "orders_count", "image_urls", "coupon_code", "is_flash_sale", "stock_urgency_label",
        "badges", "social_proof",
    }
    assert body["facets"]["stores"] == [
        {"key": "aliexpress", "name": "AliExpress", "products": 7}, {"key": "temu", "name": "Temu", "products": 7},
    ]
    assert body["facets"]["price_min"] == 49.9 and body["facets"]["price_max"] == 549.0

    sports = api.get("/api/products", params={"category": "sports"}).json()
    assert sports["total"] == 5 and {item["category"] for item in sports["items"]} == {"sports"}
    assert [store["products"] for store in sports["facets"]["stores"]] == [3, 2]

    temu = api.get("/api/products", params={"source": "temu"}).json()
    assert temu["total"] == 7 and {item["store"]["name"] for item in temu["items"]} == {"Temu"}
    assert api.get("/api/products", params={"source": "temu_mock"}).json()["total"] == 7

    assert api.get("/api/products", params={"min_rating": 4.9}).json()["total"] == 3
    assert api.get("/api/products", params={"min_orders": 3000}).json()["total"] == 5
    priced = api.get("/api/products", params={"min_price": 100, "max_price": 200}).json()
    assert priced["total"] == 4 and all(100 <= item["price"] <= 200 for item in priced["items"])

    by = lambda sort, key: [item[key] for item in api.get("/api/products", params={"sort_by": sort}).json()["items"]]  # noqa: E731
    assert by("price_asc", "price") == sorted(by("price_asc", "price"))
    assert by("price_desc", "price") == sorted(by("price_desc", "price"), reverse=True)
    assert by("rating_desc", "rating") == sorted(by("rating_desc", "rating"), reverse=True)
    assert by("orders_desc", "orders_count") == sorted(by("orders_desc", "orders_count"), reverse=True)
    assert by("discount_desc", "discount_percent") == sorted(by("discount_desc", "discount_percent"), reverse=True)

    page_two = api.get("/api/products", params={"page": 2, "page_size": 5, "sort_by": "price_asc"}).json()
    assert page_two["pages"] == 3 and len(page_two["items"]) == 5
    assert [item["price"] for item in page_two["items"]] == by("price_asc", "price")[5:10]

    for bad in ({"sort_by": "random"}, {"min_rating": 9}, {"page": 0}, {"page_size": 500}, {"q": "x" * 81}):
        assert api.get("/api/products", params=bad).status_code == 422


def test_products_endpoint_searches_with_typos_in_hebrew_and_english(api):
    assert "Earbuds" in titles(api.get("/api/products", params={"q": "earbds"}))[0]
    assert "Smartwatch" in titles(api.get("/api/products", params={"q": "smartwacth"}))[0]
    assert "Air Fryer" in titles(api.get("/api/products", params={"q": "air fry"}))[0]
    hebrew = api.get("/api/products", params={"q": "אוזניות"}).json()
    assert hebrew["sort_by"] == "relevance" and "Earbuds" in hebrew["items"][0]["title"]
    assert "Yoga" in titles(api.get("/api/products", params={"q": "מזרן יוגה"}))[0]
    assert "Power Bank" in titles(api.get("/api/products", params={"q": "סוללה נידת"}))[0]  # missing letter

    # Category and store names are searchable; a category scope narrows the search.
    assert api.get("/api/products", params={"q": "temu"}).json()["total"] == 7
    assert api.get("/api/products", params={"q": "ספורט"}).json()["total"] == 5
    assert api.get("/api/products", params={"q": "charger"}).json()["total"] >= 1
    assert api.get("/api/products", params={"q": "charger", "category": "sports"}).json()["total"] == 0
    assert api.get("/api/products", params={"q": "zzzzqqq"}).json() ["items"] == []

    sorted_search = api.get("/api/products", params={"q": "wireless", "sort_by": "price_asc"}).json()
    prices = [item["price"] for item in sorted_search["items"]]
    assert sorted_search["sort_by"] == "price_asc" and len(prices) >= 3 and prices == sorted(prices)


def test_badges_and_social_proof_are_derived_from_real_numbers(api):
    items = {item["title"].split(" - ")[0]: item for item in api.get("/api/products").json()["items"]}
    shoes = items["Men's Lightweight Running Shoes"]
    assert shoes["badges"] == ["crowd_favorite", "verified"]              # 3,412 orders, 4.8
    assert "crowd_favorite" not in items["Vacuum Insulated Stainless Steel Bottle 1L"]["badges"]  # 4.9 but 350 orders
    assert items["Digital Air Fryer 5.5L"]["badges"] == ["crowd_favorite", "flash_deal", "verified"]
    assert all("verified" in item["badges"] for item in items.values())
    assert items["65W GaN Fast Charger"]["social_proof"] == "top_rated"   # 4.9, best in gadgets
    assert items["GPS Running Smartwatch"]["social_proof"] == "most_ordered"
    assert items["5L Running Hydration Vest Backpack with 2 x 500ml Soft Flasks"]["social_proof"] is None

    vest = items["5L Running Hydration Vest Backpack with 2 x 500ml Soft Flasks"]["id"]
    for _ in range(5):
        assert api.post(f"/api/track-click/{vest}", json={"language": "he", "referrer_path": "/he"}).status_code == 200
    assert api.get(f"/api/products/{vest}").json()["social_proof"] == "high_demand"


def test_product_and_category_endpoints(api):
    product = api.get("/api/products/1").json()
    assert product["id"] == 1 and product["store"] == {"key": "aliexpress", "name": "AliExpress"}
    assert product["coupon_code"] == "RUN15" and product["discount_percent"] == 42 and len(product["image_urls"]) == 3
    assert "affiliate_url" not in product
    assert api.get("/api/products/9999").status_code == 404
    assert api.get("/api/products/abc").status_code == 422

    assert api.get("/api/categories").json() == {
        "categories": [{"key": "gadgets", "products": 5}, {"key": "sports", "products": 5}, {"key": "home", "products": 4}],
        "total": 14,
        "standards": {"min_rating": 4.5, "min_orders": 200},
    }
    assert api.get("/api/health").json()["products"] == 14


def test_only_products_that_meet_the_standard_are_listed(api, monkeypatch):
    weak = Product(external_id="w1", source="temu_mock", category="home", title="Barely Reviewed Kettle", price=30,
                   rating=4.1, orders_count=12, raw_url="https://t.example/w1", affiliate_url="https://t.example/aff/w1")
    weak_id = api.db.upsert_product(weak, {"category": "home"})
    assert api.get("/api/products").json()["total"] == 14
    assert api.get(f"/api/products/{weak_id}").status_code == 404
    assert api.post(f"/api/track-click/{weak_id}").status_code == 404

    monkeypatch.setenv("STOREFRONT_MIN_RATING", "4.8")
    monkeypatch.setenv("STOREFRONT_MIN_ORDERS", "1000")
    with TestClient(create_app(api.settings)) as strict:
        body = strict.get("/api/categories").json()
        assert body["total"] == 6 and body["standards"] == {"min_rating": 4.8, "min_orders": 1000}


def test_track_click_logs_the_event_and_returns_the_affiliate_link(api):
    response = api.post("/api/track-click/1", json={"language": "he", "referrer_path": "/he/c/sports?utm=ig#top"})
    assert response.status_code == 200
    assert response.json() == {
        "product_id": 1, "affiliate_url": "https://s.click.aliexpress.com/e/_mockRunShoes01",
        "store": {"key": "aliexpress", "name": "AliExpress"},
    }
    assert api.post("/api/track-click/8", json={"language": "EN"}).status_code == 200
    assert api.post("/api/track-click/8").status_code == 200  # the body is optional
    assert api.post("/api/track-click/8", json={"language": "<script>", "referrer_path": "https://evil.example/x"}).status_code == 200
    assert api.post("/api/track-click/9999", json={"language": "he"}).status_code == 404

    events = api.db.query("SELECT product_id, category, source, language, referrer_path, clicked_at FROM click_events ORDER BY id")
    assert [(e["product_id"], e["category"], e["source"], e["language"], e["referrer_path"]) for e in events] == [
        (1, "sports", "aliexpress_mock", "he", "/he/c/sports"),
        (8, "sports", "temu_mock", "en", ""),
        (8, "sports", "temu_mock", "unknown", ""),
        (8, "sports", "temu_mock", "unknown", ""),
    ]
    assert all(event["clicked_at"].endswith("+00:00") for event in events)


def test_cors_allows_the_storefront_origin_only(api):
    allowed = api.get("/api/categories", headers={"Origin": "http://localhost:3000"})
    assert allowed.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert "access-control-allow-origin" not in api.get("/api/categories", headers={"Origin": "https://evil.example"}).headers


def test_importing_the_server_module_opens_nothing(monkeypatch, tmp_path):
    """`api.server:app` is built on first access, so importing the module never reads .env or a database."""
    import api.server as server

    monkeypatch.setattr(server, "_app", None)
    database = tmp_path / "lazy.db"
    monkeypatch.setenv("DATABASE_PATH", str(database))
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "logs" / "lazy.log"))
    assert not database.exists()
    assert server.app is server.app and database.exists()
    with pytest.raises(AttributeError):
        server.no_such_attribute


# -- dashboard metrics ------------------------------------------------------------------ #
def test_metrics_service_counts_storefront_clicks_per_category_and_source(engine):
    metrics = MetricsService(engine.db, engine.settings)
    assert metrics.get_storefront_click_summary()["total_clicks"] == 0
    assert metrics.get_storefront_clicks_by_category() == [] == metrics.get_storefront_clicks_by_source()

    seed_storefront(engine.db)
    now = utcnow()
    clicks = [  # (product id, category, source, age)
        (1, "sports", "aliexpress_mock", timedelta(minutes=5)),
        (1, "sports", "aliexpress_mock", timedelta(days=2)),
        (8, "sports", "temu_mock", timedelta(days=30)),
        (4, "gadgets", "aliexpress_mock", timedelta(hours=3)),
    ]
    for product_id, category, source, age in clicks:
        engine.db.record_click_event(product_id, category=category, source=source, language="he",
                                     referrer_path="/he", clicked_at=now - age)

    summary = metrics.get_storefront_click_summary()
    assert (summary["total_clicks"], summary["clicks_7d"], summary["clicks_24h"], summary["products_clicked"]) == (4, 3, 2, 3)
    by_category = {row["category"]: row for row in metrics.get_storefront_clicks_by_category()}
    assert [(name, row["clicks"], row["clicks_7d"], row["clicks_24h"]) for name, row in by_category.items()] == [
        ("sports", 3, 2, 1), ("gadgets", 1, 1, 1), ("home", 0, 0, 0),
    ]
    by_source = {row["source"]: row["clicks"] for row in metrics.get_storefront_clicks_by_source()}
    assert by_source == {"aliexpress_mock": 3, "temu_mock": 1}
    assert engine.db.click_counts_since(now - timedelta(days=7)) == {1: 2, 4: 1}


# -- project contract ------------------------------------------------------------------- #
def test_storefront_files_exist_and_avoid_the_retired_term():
    expected = [
        "api/__init__.py", "api/server.py", "api/search.py",
        "storefront/package.json", "storefront/next.config.mjs", "storefront/tailwind.config.ts",
        "storefront/src/app/[lang]/page.tsx", "storefront/src/app/[lang]/c/[category]/page.tsx",
        "storefront/src/app/[lang]/p/[productId]/page.tsx",
        "storefront/src/components/Navbar.tsx", "storefront/src/components/ProductCard.tsx",
        "storefront/src/components/QuickViewModal.tsx", "storefront/src/components/RedirectConfirmModal.tsx",
        "storefront/src/components/FilterSidebar.tsx", "storefront/src/components/SkeletonCard.tsx",
        "storefront/src/components/SavedDealsDrawer.tsx", "storefront/src/lib/api.ts", "storefront/src/lib/i18n.ts",
    ]
    assert not [path for path in expected if not (ROOT / path).is_file()]

    # The storefront speaks of categories, collections and hubs. The older word survives only in the
    # engine's own plugin contract, never in the API, the storefront or its texts.
    retired = re.compile("ni" + "che", re.IGNORECASE)
    sources = [*(ROOT / "api").glob("*.py")]
    for folder in ("src",):
        sources += [path for path in (ROOT / "storefront" / folder).rglob("*") if path.is_file()]
    sources += [ROOT / "storefront" / name for name in ("package.json", "next.config.mjs", "tailwind.config.ts")]
    offenders = [str(path.relative_to(ROOT)) for path in sources if retired.search(path.read_text(encoding="utf-8"))]
    assert not offenders
