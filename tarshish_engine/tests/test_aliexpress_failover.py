"""The AliExpress provider chain: adapters, YAML configuration, failover and affiliate links.

No network: every RapidAPI host is served by one ``httpx.MockTransport`` handler that
records the requests it gets and answers per host.
"""

import logging

import httpx
import pytest

from storefront_api.seed import seed_aliexpress
from core.config import (
    DEFAULT_ALIEXPRESS_AFFILIATE_URL_TEMPLATE,
    Settings,
    masked,
    register_rapidapi_secrets,
    resolve_rapidapi_key,
)
from core.interfaces import PluginContext
from core.resilience import ConfigError, redact
from plugins.sources import aliexpress_providers
from plugins.sources.aliexpress_providers import (
    NORMALIZED_FIELDS,
    AliExpressDataHubAdapter,
    AliExpressTrueApiAdapter,
    AliExpressUnofficialAdapter,
    GenericConfigurableAliExpressAdapter,
    build_chain,
    chain_status,
    read_last_run,
)
from plugins.sources.aliexpress_rapidapi import AliExpressRapidApiSource

P1 = "aliexpress-datahub.p.rapidapi.com"
P2 = "aliexpress-true-api.p.rapidapi.com"
P3 = "aliexpress-unofficial.p.rapidapi.com"
KEYS = {
    "RAPIDAPI_KEY_ALI_1": "alikey-one-0123456789abcdef0123456789",
    "RAPIDAPI_KEY_ALI_2": "alikey-two-0123456789abcdef0123456789",
    "RAPIDAPI_KEY_ALI_3": "alikey-three-0123456789abcdef01234567",
}
SHARED_KEY = "alikey-shared-0123456789abcdef012345"

QUOTA = (429, {"message": "You have exceeded the MONTHLY quota for Requests on your current plan, BASIC."})

DATAHUB_REPLY = {"result": {
    "status": {"data": "success", "code": 200},
    "resultList": [
        {"item": {"itemId": "1005001", "title": "Wireless Earbuds Bluetooth 5.3", "sales": 5400,
                  "averageStarRate": 4.8, "itemUrl": "//www.aliexpress.com/item/1005001.html",
                  "image": "//ae01.alicdn.com/kf/earbuds.jpg",
                  "sku": {"def": {"price": "25.98", "promotionPrice": "12.99"}}}},
        {"item": {"itemId": "1005002", "title": "Cheap Cable Organizer", "sales": 90,
                  "averageStarRate": 4.9, "sku": {"def": {"promotionPrice": 1.2}}}},
    ],
}}
TRUEAPI_PRODUCTS = [
    {"product_id": 2005001, "product_title": "Mini Projector 1080P Portable", "target_sale_price": "45.20",
     "target_original_price": "90.40", "target_sale_price_currency": "EUR", "evaluate_rate": "96.0%",
     "lastest_volume": 870, "promotion_link": "https://s.click.aliexpress.com/e/_DkPro01",
     "product_detail_url": "https://www.aliexpress.com/item/2005001.html?spm=a2g0o.tracking",
     "product_main_image_url": "https://ae01.alicdn.com/kf/projector.jpg",
     "product_small_image_urls": {"string": ["https://ae01.alicdn.com/kf/projector-2.jpg"]},
     "second_level_category_name": "Projectors"},
]
UNOFFICIAL_ITEMS = [
    {"id": "3005001", "title": "Smart Watch Fitness Tracker", "price": "$19.90", "originalPrice": "$39.80",
     "rating": 4.7, "orders": "2.3K+ sold", "url": "https://www.aliexpress.com/item/3005001.html",
     "images": ["https://ae01.alicdn.com/kf/watch-1.jpg", "//ae01.alicdn.com/kf/watch-2.jpg"],
     "category": "Smart Watches"},
    {"id": "3005002", "title": "Fitness Tracker Smart Watch", "price": 21.5, "rating": 4.6, "orders": 800},
    {"id": "3005003", "title": "USB Desk Lamp LED", "price": 7.99, "rating": 4.9, "orders": 12000},
    {"id": "3005004", "title": "Poorly Rated Phone Holder", "price": 3.5, "rating": 3.9, "orders": 50000},
]


class Hub:
    """Every RapidAPI host in one handler: queued replies per host, the last one repeating."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.replies: dict[str, list] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        queue = self.replies.get(request.url.host) or [(404, {"message": "API doesn't exists"})]
        reply = queue.pop(0) if len(queue) > 1 else queue[0]
        if reply == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(reply[0], json=reply[1])

    def hosts(self) -> list[str]:
        return [request.url.host for request in self.requests]


@pytest.fixture
def hub(monkeypatch):
    hub = Hub()
    monkeypatch.setattr(AliExpressRapidApiSource, "_transport", httpx.MockTransport(hub))
    return hub


@pytest.fixture
def chain(hub, monkeypatch):
    """The shipped three-provider chain (config/aliexpress_providers.yaml), each provider with its own key."""
    monkeypatch.delenv("ALIEXPRESS_PROVIDERS_CONFIG_PATH")
    for name, value in KEYS.items():
        monkeypatch.setenv(name, value)
    return hub


def ali(engine, **context) -> AliExpressRapidApiSource:
    return AliExpressRapidApiSource(PluginContext(settings=engine.settings, **context))


def ids(products) -> list[str]:
    return [product.external_id for product in products]


def outcomes(batch) -> list[str]:
    return [attempt.outcome for attempt in batch.attempts]


# -- the failover sequence of the specification ------------------------------------------- #
def test_429_then_empty_then_valid_products(engine, chain, monkeypatch, caplog):
    """Provider 1 answers 429 -> provider 2 answers 200 with [] -> provider 3 delivers."""
    monkeypatch.setenv("ALIEXPRESS_TRACKING_ID", "trk42")
    caplog.set_level(logging.DEBUG, logger="tarshish")
    chain.replies = {P1: [QUOTA], P2: [(200, {"products": []})], P3: [(200, {"items": UNOFFICIAL_ITEMS})]}

    products = ali(engine, pipeline_id="gadgets_he").fetch_products("gadgets", 4.5, 500, 5)

    # One request per provider, in order, each with its own host header and its own key.
    assert chain.hosts() == [P1, P2, P3]
    assert [request.url.path for request in chain.requests] == ["/item_search", "/api/v3/products", "/search"]
    assert [request.headers["x-rapidapi-host"] for request in chain.requests] == [P1, P2, P3]
    assert [request.headers["x-rapidapi-key"] for request in chain.requests] == list(KEYS.values())
    assert dict(chain.requests[0].url.params) == {"q": "gadgets", "query": "gadgets", "page": "1"}
    assert dict(chain.requests[1].url.params) == {"keywords": "gadgets", "page_no": "1", "page_size": "20"}
    assert dict(chain.requests[2].url.params) == {"query": "gadgets", "page": "1"}

    # Provider 3's items: normalised, filtered (3005004 is rated 3.9), de-duplicated by canonical
    # hash (3005002 is the same watch under a reordered title) and ranked best first.
    assert ids(products) == ["3005003", "3005001"]
    watch = products[1]
    assert (watch.source, watch.category, watch.title) == ("aliexpress", "gadgets", "Smart Watch Fitness Tracker")
    assert (watch.price, watch.original_price, watch.currency) == (19.90, 39.80, "USD")
    assert (watch.rating, watch.orders_count, watch.discount_percent) == (4.7, 2300, 50)
    assert watch.image_urls == ["https://ae01.alicdn.com/kf/watch-1.jpg", "https://ae01.alicdn.com/kf/watch-2.jpg"]
    assert watch.raw_url == "https://www.aliexpress.com/item/3005001.html"
    # ALIEXPRESS_TRACKING_ID is injected with the default template.
    assert watch.affiliate_url == "https://www.aliexpress.com/item/3005001.html?aff_fcid=trk42&aff_platform=portals-tool"
    lamp = products[0]  # no URL from the API: built from the item id
    assert lamp.affiliate_url == "https://www.aliexpress.com/item/3005003.html?aff_fcid=trk42&aff_platform=portals-tool"
    assert lamp.image_urls and lamp.image_urls[0].startswith("https://placehold.co/")

    # Each failover is one structured warning; no key reaches the log.
    warnings = [r.getMessage() for r in caplog.records if "AliExpress provider failover" in r.getMessage()]
    assert len(warnings) == 2
    assert "provider=ali_primary_datahub" in warnings[0] and "outcome=quota_exhausted http=429" in warnings[0]
    assert "key_env=RAPIDAPI_KEY_ALI_1" in warnings[0]
    assert "provider=ali_secondary_trueapi" in warnings[1] and "outcome=empty http=200" in warnings[1]
    assert "provider ali_backup_unofficial delivered 2 usable product(s)" in caplog.text
    assert not any(key in caplog.text for key in KEYS.values())

    # The run is recorded for the dashboard — names and outcomes only.
    run = read_last_run(engine.settings.database_path)
    assert (run["succeeded"], run["usable"], run["pipeline_id"]) == ("ali_backup_unofficial", 2, "gadgets_he")
    assert [attempt["outcome"] for attempt in run["attempts"]] == ["quota_exhausted", "empty", "success"]
    assert [attempt["key_env"] for attempt in run["attempts"]] == list(KEYS)
    assert not any(key in str(run) for key in KEYS.values())


def test_the_walk_stops_at_the_first_provider_that_delivers(engine, chain):
    chain.replies = {P1: [(200, DATAHUB_REPLY)]}
    batch = ali(engine).fetch_batch("gadgets", 4.5, 500)

    assert chain.hosts() == [P1]  # the quota of providers 2 and 3 is untouched
    assert (batch.provider, ids(batch.products), outcomes(batch)) == ("ali_primary_datahub", ["1005001"], ["success"])
    assert batch.rejected == [("1005002 Cheap Cable Organizer", "90 orders is below 500")]


def test_a_quota_exhausted_key_is_not_asked_again_in_the_same_run(engine, chain):
    chain.replies = {P1: [QUOTA], P2: [(200, {"products": TRUEAPI_PRODUCTS})]}
    source = ali(engine)
    source.fetch_batch("gadgets", 4.5, 100)
    second = source.fetch_batch("gadgets", 4.5, 100)

    assert chain.hosts() == [P1, P2, P2]
    assert second.attempts[0].outcome == "quota_exhausted" and "already ran out" in second.attempts[0].detail


@pytest.mark.parametrize("status", [401, 403])
def test_rejected_keys_fail_over(engine, chain, status):
    chain.replies = {P1: [(status, {"message": "You are not subscribed to this API."})],
                     P2: [(200, {"data": {"products": TRUEAPI_PRODUCTS}})]}
    batch = ali(engine).fetch_batch("gadgets", 4.5, 100)

    assert outcomes(batch) == ["auth_failed", "success"]
    assert batch.attempts[0].http_status == status and "subscribed" in batch.attempts[0].detail


def test_timeouts_and_5xx_get_two_quick_retries_then_fail_over(engine, chain, clean_environment):
    chain.replies = {P1: ["timeout", (502, {}), (503, {})], P2: [(200, {"products": TRUEAPI_PRODUCTS})]}
    batch = ali(engine).fetch_batch("gadgets", 4.5, 100)

    assert chain.hosts() == [P1, P1, P1, P2]  # 1 try + 2 retries, then the next provider
    assert outcomes(batch) == ["unavailable", "success"]
    assert batch.attempts[0].http_status == 503 and "after 2 retries" in batch.attempts[0].detail
    assert len(clean_environment) == 2  # two backoff sleeps (patched out)


def test_a_missing_key_skips_the_provider_and_rapidapi_key_is_the_fallback(engine, hub, monkeypatch):
    monkeypatch.setenv("RAPIDAPI_KEY_ALI_2", KEYS["RAPIDAPI_KEY_ALI_2"])  # only provider 2 has a key
    hub.replies = {P2: [(200, {"products": TRUEAPI_PRODUCTS})]}
    batch = ali(engine).fetch_batch("gadgets", 4.5, 100)

    assert hub.hosts() == [P2]
    assert outcomes(batch) == ["missing_key", "success"]
    assert "RAPIDAPI_KEY_ALI_1 or RAPIDAPI_KEY is not set" in batch.attempts[0].detail

    monkeypatch.setenv("RAPIDAPI_KEY", SHARED_KEY)  # now every provider without its own key uses this one
    hub.replies = {P1: [(200, DATAHUB_REPLY)]}
    batch = ali(engine).fetch_batch("gadgets", 4.5, 100)
    assert hub.requests[-1].headers["x-rapidapi-key"] == SHARED_KEY
    assert (batch.provider, batch.attempts[0].key_env) == ("ali_primary_datahub", "RAPIDAPI_KEY")


def test_items_that_all_fail_the_quality_filter_count_as_a_failover(engine, chain):
    chain.replies = {P1: [(200, DATAHUB_REPLY)], P2: [(200, {"products": TRUEAPI_PRODUCTS})]}
    batch = ali(engine).fetch_batch("gadgets", 4.9, 1000)  # nothing in DataHub's reply is that good

    assert outcomes(batch)[:2] == ["filtered_out", "filtered_out"]
    assert "none passed the quality filter" in batch.attempts[0].detail


def test_when_every_provider_fails_the_source_returns_nothing(engine, chain, caplog):
    chain.replies = {P1: [QUOTA], P2: [(200, {"products": []})], P3: [(500, {})]}
    assert ali(engine).fetch_products("gadgets", 4.5, 100, 5) == []
    assert "no provider delivered products (ali_primary_datahub: quota_exhausted; " in caplog.text
    assert read_last_run(engine.settings.database_path)["succeeded"] is None


def test_datahub_error_envelope_is_a_failover(engine, chain):
    envelope = {"result": {"status": {"data": "error", "code": 5008, "msg": {"data-error": "keyword blocked"}}}}
    chain.replies = {P1: [(200, envelope)], P2: [(200, {"products": TRUEAPI_PRODUCTS})]}
    batch = ali(engine).fetch_batch("gadgets", 4.5, 100)

    assert outcomes(batch) == ["error", "success"]
    assert "error 5008" in batch.attempts[0].detail and "keyword blocked" in batch.attempts[0].detail


# -- adapters ------------------------------------------------------------------------------ #
def test_datahub_adapter_normalises_nested_items(engine):
    (adapter,) = [p for p in build_chain(engine.settings.get_env) if isinstance(p, AliExpressDataHubAdapter)]
    url, headers, params = adapter.build_request("earbuds", 20, {"price_min": 5.0, "price_max": 50, "sort": "salesDesc"})
    assert (url, headers) == (f"https://{P1}/item_search", {"x-rapidapi-host": P1})
    assert params == {"q": "earbuds", "query": "earbuds", "page": "1", "sort": "salesDesc",
                      "startPrice": "5", "endPrice": "50"}

    first, second = adapter.parse_response(DATAHUB_REPLY)
    assert set(NORMALIZED_FIELDS) <= set(first)
    assert first["goods_id"] == "1005001" and first["title"] == "Wireless Earbuds Bluetooth 5.3"
    assert (first["price"], first["original_price"], first["rating"], first["sales_volume"]) == (12.99, 25.98, 4.8, 5400)
    assert first["raw_url"] == "https://www.aliexpress.com/item/1005001.html"
    assert first["image_urls"] == ["https://ae01.alicdn.com/kf/earbuds.jpg"]
    assert second["raw_url"] == "https://www.aliexpress.com/item/1005002.html"


def test_true_api_adapter_converts_feedback_percent_and_keeps_promotion_links(engine, chain, monkeypatch):
    monkeypatch.setenv("ALIEXPRESS_TRACKING_ID", "trk42")
    (adapter,) = [p for p in build_chain(engine.settings.get_env) if isinstance(p, AliExpressTrueApiAdapter)]
    assert adapter.build_request("projector", 10, {"price_min": 20, "price_max": None})[2] == {
        "keywords": "projector", "page_no": "1", "page_size": "10", "min_sale_price": "20",
    }
    (item,) = adapter.parse_response({"products": {"product": TRUEAPI_PRODUCTS}})  # the wrapped shape too
    assert (item["goods_id"], item["rating"], item["sales_volume"], item["currency"]) == ("2005001", 4.8, 870, "EUR")
    assert item["raw_url"] == "https://www.aliexpress.com/item/2005001.html"  # tracking query dropped
    assert item["category_hint"] == "Projectors"
    assert item["image_urls"] == ["https://ae01.alicdn.com/kf/projector.jpg", "https://ae01.alicdn.com/kf/projector-2.jpg"]

    chain.replies = {P1: [QUOTA], P2: [(200, {"products": TRUEAPI_PRODUCTS})]}
    (product,) = ali(engine).fetch_batch("projector", 4.5, 100).products
    assert product.affiliate_url == "https://s.click.aliexpress.com/e/_DkPro01"  # the API's own link is preserved
    assert (product.price, product.original_price, product.currency) == (45.2, 90.4, "EUR")


def test_unofficial_adapter_reads_items_or_results(engine):
    (adapter,) = [p for p in build_chain(engine.settings.get_env) if isinstance(p, AliExpressUnofficialAdapter)]
    assert len(adapter.parse_response({"results": UNOFFICIAL_ITEMS})) == 4
    assert adapter.parse_response({"items": []}) == []


def test_the_generic_adapter_is_driven_by_yaml(engine, hub, tmp_path, monkeypatch):
    host = "ali-search-hub.p.rapidapi.com"
    path = tmp_path / "aliexpress_providers.yaml"
    path.write_text(f"""
providers:
  - name: ali_generic
    adapter: generic
    host: {host}
    url: https://{host}/v2/find
    api_key_env: RAPIDAPI_KEY_ALI_9
    request:
      params: {{term: "{{keyword}}", size: "{{max_items}}", stars: "{{min_stars}}", lang: en}}
    response:
      items: [payload.list]
      item_root: product
      price_divisor: 100
      fields:
        goods_id: pid
        price: cents
        rating: feedback
        sales_volume: orders
        image_urls: pics
""", encoding="utf-8")
    monkeypatch.setenv("ALIEXPRESS_PROVIDERS_CONFIG_PATH", str(path))
    monkeypatch.setenv("RAPIDAPI_KEY", SHARED_KEY)
    hub.replies = {host: [(200, {"payload": {"list": [
        {"product": {"pid": "77", "title": "Magnetic Charging Cable 3-in-1", "cents": 1299, "feedback": "93.4%",
                     "orders": "1,204", "pics": [{"url": "https://ae01.alicdn.com/kf/cable.jpg"}]}},
    ]}})]}

    (provider,) = build_chain(engine.settings.get_env)
    assert isinstance(provider, GenericConfigurableAliExpressAdapter)
    (product,) = ali(engine).fetch_batch("cables", 4.5, 1000).products

    assert dict(hub.requests[0].url.params) == {"term": "cables", "size": "20", "stars": "4", "lang": "en"}
    assert (product.external_id, product.price, product.rating, product.orders_count) == ("77", 12.99, 4.67, 1204)
    assert product.raw_url == "https://www.aliexpress.com/item/77.html"
    assert product.image_urls == ["https://ae01.alicdn.com/kf/cable.jpg"]


@pytest.mark.parametrize("entry, problem", [
    ("adapter: nope", "unknown adapter 'nope'"),
    ("adapter: generic", "generic adapter needs a 'response:' block"),
    ("adapter: datahub\n    response: {items: x}", "read by the generic adapter only"),
])
def test_an_invalid_chain_file_is_a_config_error(engine, tmp_path, monkeypatch, entry, problem):
    path = tmp_path / "chain.yaml"
    path.write_text(f"providers:\n  - name: broken\n    {entry}\n    host: {P1}\n    url: https://{P1}/x\n",
                    encoding="utf-8")
    monkeypatch.setenv("ALIEXPRESS_PROVIDERS_CONFIG_PATH", str(path))
    with pytest.raises(ConfigError, match=problem):
        build_chain(engine.settings.get_env)


def test_a_raw_key_or_foreign_host_is_refused_in_the_chain_file(engine, tmp_path, monkeypatch):
    path = tmp_path / "chain.yaml"
    path.write_text("providers:\n  - name: leak\n    adapter: datahub\n    host: evil.example.com\n"
                    "    url: https://evil.example.com/x\n    api_key_env: sk-ant-abcdefghijklmnop\n", encoding="utf-8")
    monkeypatch.setenv("ALIEXPRESS_PROVIDERS_CONFIG_PATH", str(path))
    with pytest.raises(ConfigError) as caught:
        build_chain(engine.settings.get_env)
    assert "RapidAPI host name" in str(caught.value) and "NAME of an environment variable" in str(caught.value)


# -- core configuration ----------------------------------------------------------------------- #
def test_core_config_resolves_masks_and_falls_back(monkeypatch):
    monkeypatch.setenv("RAPIDAPI_KEY", SHARED_KEY)
    assert resolve_rapidapi_key("RAPIDAPI_KEY_ALI_1") == (SHARED_KEY, "RAPIDAPI_KEY")
    monkeypatch.setenv("RAPIDAPI_KEY_ALI_1", KEYS["RAPIDAPI_KEY_ALI_1"])
    assert resolve_rapidapi_key("RAPIDAPI_KEY_ALI_1") == (KEYS["RAPIDAPI_KEY_ALI_1"], "RAPIDAPI_KEY_ALI_1")
    assert redact(f"key={KEYS['RAPIDAPI_KEY_ALI_1']} shared {SHARED_KEY}") == "key=[REDACTED] shared [REDACTED]"

    register_rapidapi_secrets({"RAPIDAPI_KEY_ALI_3": "alikey-only-in-env-0123456789"})
    assert "alikey-only-in-env" not in redact("sent alikey-only-in-env-0123456789")
    assert (masked("anything"), masked(None)) == ("[REDACTED]", "Missing")

    assert Settings().aliexpress_affiliate_url_template == DEFAULT_ALIEXPRESS_AFFILIATE_URL_TEMPLATE
    monkeypatch.setenv("ALIEXPRESS_AFFILIATE_URL_TEMPLATE", "https://example.com/{nope}")
    with pytest.raises(Exception, match="must contain"):
        Settings()


# -- registry, dry-run, seeding, dashboard status ---------------------------------------------- #
def test_the_source_is_registered_and_a_dry_run_spends_no_quota(engine, chain):
    assert engine.plugins.has("source", "aliexpress_rapidapi")
    products = ali(engine, dry_run=True).fetch_products("sports", 0, 0, 3)

    assert chain.requests == []  # no request, no quota
    assert products and {product.source for product in products} == {"aliexpress_mock"}


def test_seed_aliexpress_upserts_into_sqlite_without_llm(engine, chain, monkeypatch):
    monkeypatch.setenv("ALIEXPRESS_TRACKING_ID", "trk42")
    chain.replies = {P1: [QUOTA], P2: [(200, {"products": []})], P3: [(200, {"items": UNOFFICIAL_ITEMS})]}

    report = seed_aliexpress(engine, category="gadgets", keyword="smart watch", limit=10)
    assert (report.source, report.provider, report.keyword) == ("aliexpress_rapidapi", "ali_backup_unofficial", "smart watch")
    assert ids(report.inserted) == ["3005003", "3005001"]
    stored = engine.db.query("SELECT external_id, source, affiliate_url FROM products WHERE source = 'aliexpress'")
    assert sorted(row["external_id"] for row in stored) == ["3005001", "3005003"]
    assert all("aff_fcid=trk42" in row["affiliate_url"] for row in stored)

    # Seeding again finds the same products already stored, and the dry-run preview now uses them.
    again = seed_aliexpress(engine, category="gadgets", keyword="smart watch", limit=10)
    assert (again.inserted, ids(again.duplicates)) == ([], ["3005003", "3005001"])
    preview = ali(engine, dry_run=True).fetch_products("gadgets", 4.5, 500, 5)
    assert {product.source for product in preview} == {"aliexpress"}


def test_chain_status_reports_order_keys_and_the_last_run(engine, chain, monkeypatch):
    monkeypatch.delenv("RAPIDAPI_KEY_ALI_3")
    monkeypatch.setenv("RAPIDAPI_KEY", SHARED_KEY)
    chain.replies = {P1: [QUOTA], P2: [(200, {"products": TRUEAPI_PRODUCTS})]}
    ali(engine).fetch_batch("gadgets", 4.5, 100)

    status = chain_status(engine.settings)
    rows = status["providers"]
    assert [row["name"] for row in rows] == ["ali_primary_datahub", "ali_secondary_trueapi", "ali_backup_unofficial"]
    assert [row["key_state"] for row in rows] == ["Configured", "Configured", "Configured (via RAPIDAPI_KEY)"]
    assert [row["last_outcome"] for row in rows] == ["quota_exhausted", "success", None]
    assert status["last_run"]["succeeded"] == "ali_secondary_trueapi"
    assert not any(key in str(status) for key in [*KEYS.values(), SHARED_KEY])
    aliexpress_providers.reset_run_state()


def test_cli_seed_aliexpress_flags(engine):
    import main

    with pytest.raises(SystemExit):
        main.main(["--seed-aliexpress"])  # needs --category
    with pytest.raises(SystemExit):
        main.main(["--seed-aliexpress", "--category", "gadgets", "--job", "abcd1234"])  # --job is Temu-only
    with pytest.raises(SystemExit):
        main.main(["--seed-aliexpress", "--category", "gadgets", "--dry-run"])
