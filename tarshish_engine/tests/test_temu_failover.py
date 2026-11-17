"""The Temu provider chain: adapters, YAML configuration and failover.

No network: every RapidAPI host is served by one ``httpx.MockTransport`` handler that
records the requests it gets and answers per host.
"""

import logging

import httpx
import pytest

from storefront_api.seed import seed_temu
from core import bootstrap
from core.interfaces import PluginContext
from core.resilience import ConfigError, redact
from plugins.sources import temu_providers
from plugins.sources.temu_providers import (
    NORMALIZED_FIELDS,
    GenericConfigurableTemuAdapter,
    ProviderConfig,
    TemuApiRealtimeAdapter,
    TemuProductScraperAdapter,
    TemuScraper2Adapter,
    build_chain,
    chain_status,
    read_last_run,
)
from plugins.sources.temu_rapidapi import TemuRapidApiSource

P1 = "temu-product-scraper.p.rapidapi.com"
P2 = "temu-scraper2.p.rapidapi.com"
P3 = "temu-api.p.rapidapi.com"
SCOUT = "temuscout-api-temu-data-api.p.rapidapi.com"
KEYS = {
    "RAPIDAPI_KEY_TEMU_1": "rapidkey-one-0123456789abcdef01234567",
    "RAPIDAPI_KEY_TEMU_2": "rapidkey-two-0123456789abcdef01234567",
    "RAPIDAPI_KEY_TEMU_3": "rapidkey-three-0123456789abcdef012345",
}
SHARED_KEY = "rapidkey-shared-0123456789abcdef0123"

CHAIN_YAML = f"""
providers:
  - name: temu_primary_scraper
    adapter: TemuProductScraperAdapter
    host: {P1}
    url: https://{P1}/temu
    api_key_env: RAPIDAPI_KEY_TEMU_1
  - name: temu_secondary_scraper
    adapter: TemuScraper2Adapter
    host: {P2}
    url: https://{P2}/search
    api_key_env: RAPIDAPI_KEY_TEMU_2
  - name: temu_realtime_backup
    adapter: TemuApiRealtimeAdapter
    host: {P3}
    url: https://{P3}/api/search
    api_key_env: RAPIDAPI_KEY_TEMU_3
"""

QUOTA = (429, {"message": "You have exceeded the MONTHLY quota for Searches on your current plan, BASIC."})

# The three schemas, each with products that pass (rating 4.5, 500 sold) and one that does not.
SCRAPER_ITEMS = [  # provider 1: results[]
    {"goodsId": "601", "title": "Magnetic Phone Case with Stand", "price": 1.99, "rating": 5, "salesVolume": 11779,
     "url": "https://www.temu.com/--g-601.html", "l2Category": "Phone Cases"},
    {"goodsId": "602", "title": "Barely Rated Cable 2m", "price": 2.5, "rating": 4.2, "salesVolume": 90000,
     "url": "https://www.temu.com/--g-602.html"},
]
SCRAPER2_ITEMS = [  # provider 2: data.products[]
    {"product_id": "651", "name": "Foldable Laptop Stand Aluminium", "sale_price": "$12.40", "score": 4.7,
     "sold": 2100, "product_url": "https://www.temu.com/--g-651.html", "images": ["https://img.kwcdn.com/p/stand.jpg"]},
]
REALTIME_ITEMS = [  # provider 3: result.goodsList[]
    {"goods_id": 701, "goods_name": "Magnetic Power Bank 10000mAh", "price": "19.90", "goods_score": 4.8,
     "sales_tip": "3.6K+ sold", "link_url": "/magnetic-power-bank-g-701.html",
     "thumb_url": "//img.kwcdn.com/product/bank.jpg", "cat_id": 55},
    {"goods_id": 702, "goods_name": "Smart Tracker Tag 4-Pack", "price": 8.75, "goods_score": 4.9,
     "sales_num": 12000, "thumb_url": "https://img.kwcdn.com/product/tag.jpg"},
    {"goods_id": 703, "goods_name": "Unloved Desk Fan", "price": 6.0, "goods_score": 4.0, "sales_num": 50000},
]
SCOUT_REPLY = {  # the example from TemuScout's own API description
    "description": "OK", "status": 200,
    "data": {"market": "us", "query": "backpack", "count": 2, "products": [
        {"id": "601099548785479", "title": "Large Capacity Travel Backpack for Men",
         "url": "https://www.temu.com/large-capacity-travel-backpack-g-601099548785479.html",
         "image": "https://img.kwcdn.com/product/fancy/c7e49ec5.jpg", "price": 10.33, "price_text": "$10.33",
         "list_price": 35.96, "currency": "USD", "sold": 350000, "sold_text": "350K+", "rating": 4.7,
         "reviews": 2282, "store_id": "145582942988", "category_id": "961", "local_warehouse": False},
        {"id": "601101943730548", "title": "Lightweight Outdoor Travel Backpack with Shoe Compartment",
         "url": "https://www.temu.com/lightweight-outdoor-travel-backpack-g-601101943730548.html",
         "image": "https://img.kwcdn.com/product/fancy/ea53f823.jpg", "price": 13.92, "list_price": 65.97,
         "currency": "USD", "sold": None, "sold_text": "4.1K+", "rating": 4.8, "category_id": "1526"},
    ]},
}


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
    monkeypatch.setattr(TemuRapidApiSource, "_transport", httpx.MockTransport(hub))
    return hub


@pytest.fixture
def chain(hub, tmp_path, monkeypatch):
    """The three-provider chain of the specification, each provider with its own key."""
    path = tmp_path / "temu_providers.yaml"
    path.write_text(CHAIN_YAML, encoding="utf-8")
    monkeypatch.setenv("TEMU_PROVIDERS_CONFIG_PATH", str(path))
    for name, value in KEYS.items():
        monkeypatch.setenv(name, value)
    return hub


def temu(engine, **context) -> TemuRapidApiSource:
    return TemuRapidApiSource(PluginContext(settings=engine.settings, **context))


def ids(products) -> list[str]:
    return [product.external_id for product in products]


def outcomes(batch) -> list[str]:
    return [attempt.outcome for attempt in batch.attempts]


# -- the failover sequence ------------------------------------------------------------- #
def test_failover_walks_the_chain_until_a_provider_delivers(engine, chain, monkeypatch, caplog):
    """429 on provider 1 -> empty result on provider 2 -> provider 3 delivers."""
    monkeypatch.setenv("TEMU_TRACKING_ID", "aff42")
    caplog.set_level(logging.DEBUG, logger="tarshish")
    chain.replies = {P1: [QUOTA], P2: [(200, {"results": []})], P3: [(200, {"result": {"goodsList": REALTIME_ITEMS}})]}

    products = temu(engine, pipeline_id="gadgets_he").fetch_products("gadgets", 4.5, 500, 5)

    # One request per provider, in order, each with its own host header and its own key.
    assert chain.hosts() == [P1, P2, P3]
    assert [request.url.path for request in chain.requests] == ["/temu", "/search", "/api/search"]
    assert [request.headers["x-rapidapi-host"] for request in chain.requests] == [P1, P2, P3]
    assert [request.headers["x-rapidapi-key"] for request in chain.requests] == list(KEYS.values())
    assert dict(chain.requests[0].url.params) == {"keyword": "gadgets", "maxItems": "20"}
    assert dict(chain.requests[1].url.params) == {"query": "gadgets", "keyword": "gadgets", "page": "1", "limit": "20"}
    assert dict(chain.requests[2].url.params) == {"keywords": "gadgets", "page": "1", "pageSize": "20"}

    # Provider 3's items are normalised, filtered (703 is rated 4.0) and ranked.
    assert ids(products) == ["702", "701"]
    bank = products[1]
    assert (bank.source, bank.category, bank.title) == ("temu", "gadgets", "Magnetic Power Bank 10000mAh")
    assert (bank.price, bank.rating, bank.orders_count, bank.currency) == (19.90, 4.8, 3600, "USD")
    assert bank.raw_url == "https://www.temu.com/magnetic-power-bank-g-701.html"
    assert bank.image_urls == ["https://img.kwcdn.com/product/bank.jpg"]
    # …with the affiliate tracking id attached; a product without a URL gets one from its goods id.
    assert bank.affiliate_url == f"{bank.raw_url}?_x_sessn_id=aff42&refer_page_name=affiliate"
    assert products[0].affiliate_url == (
        "https://www.temu.com/goods.html?goods_id=702&_x_sessn_id=aff42&refer_page_name=affiliate"
    )

    # Each failover is one structured warning; no key reaches the log.
    warnings = [record.getMessage() for record in caplog.records if "Temu provider failover" in record.getMessage()]
    assert len(warnings) == 2
    assert "provider=temu_primary_scraper" in warnings[0] and "outcome=quota_exhausted http=429" in warnings[0]
    assert "provider=temu_secondary_scraper" in warnings[1] and "outcome=empty http=200" in warnings[1]
    assert "provider temu_realtime_backup delivered 2 usable product(s)" in caplog.text
    assert not any(key in caplog.text for key in KEYS.values())

    # The run is recorded for the dashboard — names and outcomes only.
    run = read_last_run(engine.settings.database_path)
    assert (run["succeeded"], run["usable"], run["pipeline_id"]) == ("temu_realtime_backup", 2, "gadgets_he")
    assert [attempt["outcome"] for attempt in run["attempts"]] == ["quota_exhausted", "empty", "success"]
    assert [attempt["key_env"] for attempt in run["attempts"]] == list(KEYS)
    assert not any(key in str(run) for key in KEYS.values())


def test_the_walk_stops_at_the_first_provider_that_delivers(engine, chain):
    chain.replies = {P1: [(200, {"success": True, "status": "done", "results": SCRAPER_ITEMS})]}
    batch = temu(engine).fetch_batch("gadgets", 4.5, 500)

    assert chain.hosts() == [P1]  # the quota of providers 2 and 3 is untouched
    assert (batch.provider, ids(batch.products), outcomes(batch)) == ("temu_primary_scraper", ["601"], ["success"])
    assert batch.received == 2 and batch.rejected == [("602 Barely Rated Cable 2m", "rating 4.2 is below 4.5")]


def test_items_that_fail_the_quality_filter_count_as_a_failed_provider(engine, chain):
    weak = [{**SCRAPER_ITEMS[1]}, {**SCRAPER_ITEMS[0], "salesVolume": 12}]
    chain.replies = {P1: [(200, {"results": weak})], P2: [(200, {"data": {"products": SCRAPER2_ITEMS}})]}
    batch = temu(engine).fetch_batch("gadgets", 4.5, 500)

    assert chain.hosts() == [P1, P2] and outcomes(batch) == ["filtered_out", "success"]
    assert "none passed the quality filter" in batch.attempts[0].detail and batch.attempts[0].received == 2
    stand = batch.products[0]
    assert (batch.provider, stand.external_id, stand.price, stand.rating, stand.orders_count) == (
        "temu_secondary_scraper", "651", 12.40, 4.7, 2100,
    )
    assert stand.image_urls == ["https://img.kwcdn.com/p/stand.jpg"]


def test_every_failure_mode_fails_over_and_an_exhausted_chain_returns_nothing(engine, chain, monkeypatch, caplog,
                                                                             clean_environment):
    caplog.set_level(logging.WARNING, logger="tarshish")
    monkeypatch.delenv("RAPIDAPI_KEY_TEMU_1")  # provider 1: no key at all (no RAPIDAPI_KEY fallback either)
    chain.replies = {
        P2: [(403, {"message": "You are not subscribed to this API."})],
        P3: ["timeout", (503, {"message": "upstream busy"}), (502, {"message": "bad gateway"})],
    }

    assert temu(engine).fetch_products("gadgets", 4.5, 500, 5) == []  # graceful: no exception, no products

    assert chain.hosts() == [P2, P3, P3, P3]         # no request without a key; 2 quick retries on 5xx / timeout
    assert len(clean_environment) == 2               # the two backoff sleeps
    run = read_last_run(engine.settings.database_path)
    assert run["succeeded"] is None
    assert [attempt["outcome"] for attempt in run["attempts"]] == ["missing_key", "auth_failed", "unavailable"]
    assert [attempt["http_status"] for attempt in run["attempts"]] == [None, 403, 502]
    assert "no provider delivered products" in caplog.text
    assert "temu_primary_scraper: missing_key; temu_secondary_scraper: auth_failed; temu_realtime_backup: unavailable" in caplog.text


def test_keys_fall_back_to_rapidapi_key_and_an_exhausted_key_is_not_asked_twice(engine, chain, monkeypatch):
    for name in KEYS:
        monkeypatch.delenv(name)
    monkeypatch.setenv("RAPIDAPI_KEY", SHARED_KEY)
    chain.replies = {P1: [QUOTA], P2: [(200, {"items": SCRAPER2_ITEMS})]}

    first = temu(engine).fetch_batch("gadgets", 4.5, 500)
    assert chain.hosts() == [P1, P2] and first.provider == "temu_secondary_scraper"
    assert {request.headers["x-rapidapi-key"] for request in chain.requests} == {SHARED_KEY}
    assert [attempt.key_env for attempt in first.attempts] == ["RAPIDAPI_KEY", "RAPIDAPI_KEY"]

    # Provider 1 said 429 for this key: for the rest of the run it is skipped without a request.
    second = temu(engine).fetch_batch("gadgets", 4.5, 500)
    assert chain.hosts() == [P1, P2, P2] and outcomes(second) == ["quota_exhausted", "success"]
    assert "skipped" in second.attempts[0].detail

    # A different key on the same API is a different quota.
    monkeypatch.setenv("RAPIDAPI_KEY_TEMU_1", KEYS["RAPIDAPI_KEY_TEMU_1"])
    chain.replies[P1] = [(200, {"results": SCRAPER_ITEMS})]
    assert temu(engine).fetch_batch("gadgets", 4.5, 500).provider == "temu_primary_scraper"


def test_collecting_a_job_only_asks_providers_that_run_jobs(engine, chain):
    chain.replies = {P1: [(200, {"success": True, "status": "done", "results": SCRAPER_ITEMS,
                                 "query": {"keyword": "smart gadgets"}})]}
    report = seed_temu(engine, category="gadgets", limit=5, job_id="job-12345")

    assert [(request.url.host, request.url.path) for request in chain.requests] == [(P1, "/jobs/job-12345")]
    assert (report.provider, report.keyword, ids(report.inserted)) == ("temu_primary_scraper", "smart gadgets", ["601"])


# -- the shipped chain ------------------------------------------------------------------- #
def test_the_shipped_chain_fails_over_to_temuscout_through_the_generic_adapter(engine, hub, monkeypatch):
    monkeypatch.delenv("TEMU_PROVIDERS_CONFIG_PATH")  # read config/temu_providers.yaml itself
    monkeypatch.setenv("RAPIDAPI_KEY", SHARED_KEY)
    providers = build_chain(engine.settings.get_env)
    assert [(p.name, type(p).__name__, p.config.enabled, p.config.api_key_env) for p in providers] == [
        ("temu_primary_scraper", "TemuProductScraperAdapter", True, "RAPIDAPI_KEY_TEMU_1"),
        ("temu_secondary_scraper", "TemuScraper2Adapter", False, "RAPIDAPI_KEY_TEMU_2"),
        ("temu_realtime_backup", "TemuApiRealtimeAdapter", False, "RAPIDAPI_KEY_TEMU_3"),
        ("temuscout", "GenericConfigurableTemuAdapter", True, "RAPIDAPI_KEY_TEMU_4"),
    ]

    hub.replies = {P1: [QUOTA], SCOUT: [(200, SCOUT_REPLY)]}
    search = {"keyword": "backpack", "l1Category": "Bags & Luggage", "priceMax": 40}
    batch = temu(engine, options={"search_config": search}).fetch_batch("travel", 4.5, 500)

    assert hub.hosts() == [P1, SCOUT]  # the switched-off providers are never asked
    assert dict(hub.requests[0].url.params) == {
        "keyword": "backpack", "maxItems": "20", "l1Category": "Bags & Luggage", "priceMax": "40",
    }
    assert hub.requests[1].url.path == "/v1/search"
    assert dict(hub.requests[1].url.params) == {"q": "backpack", "market": "us", "sort": "top_sales", "min_rating": "4"}

    assert batch.provider == "temuscout" and ids(batch.products) == ["601099548785479", "601101943730548"]
    top, second = batch.products
    assert (top.price, top.original_price, top.currency, top.rating, top.orders_count) == (10.33, 35.96, "USD", 4.7, 350000)
    assert top.image_urls == ["https://img.kwcdn.com/product/fancy/c7e49ec5.jpg"] and top.discount_percent == 71
    assert second.orders_count == 4100  # "4.1K+" when the number itself is missing

    # No results is an HTTP 200 with an empty list there: a failed provider, with the API's own words.
    hub.replies[SCOUT] = [(200, {"status": 204, "description": "No results", "data": {"count": 0, "products": []}})]
    empty = temu(engine).fetch_batch("travel", 4.5, 500)
    assert empty.provider is None and empty.attempts[-1].outcome == "empty"
    assert "description: No results" in empty.attempts[-1].detail


# -- adapters ------------------------------------------------------------------------------ #
def provider(adapter, host: str, path: str, **extra) -> ProviderConfig:
    return ProviderConfig(name="probe", adapter=adapter.__name__, host=host, url=f"https://{host}{path}", **extra)


def test_adapters_build_requests_and_normalise_responses():
    filters = {"price_min": 1.0, "price_max": 99.5, "l2_category": "Phone Cases", "min_rating": 4.5, "min_orders": 500}

    scraper = TemuProductScraperAdapter(provider(TemuProductScraperAdapter, P1, "/temu"))
    assert scraper.build_request("phone case", 20, filters) == (
        f"https://{P1}/temu", {"x-rapidapi-host": P1},  # no key: the failover loop adds it
        {"keyword": "phone case", "maxItems": "20", "l2Category": "Phone Cases", "priceMin": "1", "priceMax": "99.5"},
    )
    case = scraper.parse_response({"results": SCRAPER_ITEMS})[0]
    assert set(NORMALIZED_FIELDS) <= set(case)
    assert case == {
        "goods_id": "601", "title": "Magnetic Phone Case with Stand", "price": 1.99, "rating": 5.0,
        "sales_volume": 11779, "raw_url": "https://www.temu.com/--g-601.html", "image_urls": [],
        "category_hint": "Phone Cases", "original_price": None, "currency": None,
    }
    running = {"status": "running", "jobId": "job-12345", "resultUrl": "/jobs/job-12345", "pollAfterSeconds": 15}
    job = scraper.pending_job(running)
    assert (job.job_id, job.url, job.poll_after) == ("job-12345", f"https://{P1}/jobs/job-12345", 15.0)
    assert scraper.pending_job({"status": "done", "results": []}) is None

    scraper2 = TemuScraper2Adapter(provider(TemuScraper2Adapter, P2, "/search"))
    assert scraper2.build_request("phone case", 30, {})[2] == {
        "query": "phone case", "keyword": "phone case", "page": "1", "limit": "30",
    }
    assert [item["goods_id"] for item in scraper2.parse_response({"data": {"products": SCRAPER2_ITEMS}})] == ["651"]
    assert [item["goods_id"] for item in scraper2.parse_response({"items": SCRAPER2_ITEMS})] == ["651"]
    assert scraper2.parse_response({"data": {}}) == [] and scraper2.pending_job({"items": []}) is None

    realtime = TemuApiRealtimeAdapter(provider(TemuApiRealtimeAdapter, P3, "/api/search"))
    assert realtime.build_request("phone case", 10, {})[2] == {"keywords": "phone case", "page": "1", "pageSize": "10"}
    assert [item["goods_id"] for item in realtime.parse_response({"result": {"goodsList": REALTIME_ITEMS}})] == [
        "701", "702", "703",
    ]
    assert realtime.parse_response({"products": REALTIME_ITEMS})[0]["sales_volume"] == 3600

    # The generic adapter is nothing but its YAML: parameter templates and dotted paths.
    generic = GenericConfigurableTemuAdapter(provider(
        GenericConfigurableTemuAdapter, "some-temu-api.p.rapidapi.com", "/v2/find",
        request={"params": {"term": "{keyword}", "size": "{max_items}", "region": "us", "max": "{price_max}",
                            "cat": "{l1_category}"}},
        response={"items": ["payload.list", "list"], "price_divisor": 100,
                  "fields": {"goods_id": "sku", "title": "info.label", "price": "cents", "image_urls": "pics",
                             "sales_volume": "stats.sold_label"}},
    ))
    assert generic.build_request("lamp", 25, filters)[2] == {"term": "lamp", "size": "25", "region": "us", "max": "99.5"}
    lamp = generic.parse_response({"payload": {"list": [{
        "sku": 9001, "info": {"label": "Sunset Lamp"}, "cents": 1299, "rating": "4.6 stars",
        "stats": {"sold_label": "10K+ sold"}, "pics": [{"url": "https://img.example/lamp.jpg"}, "not a url"],
    }]}})[0]
    assert (lamp["goods_id"], lamp["title"], lamp["price"], lamp["rating"], lamp["sales_volume"]) == (
        "9001", "Sunset Lamp", 12.99, 4.6, 10000,
    )
    assert lamp["image_urls"] == ["https://img.example/lamp.jpg"]
    assert lamp["raw_url"] == "https://www.temu.com/goods.html?goods_id=9001"


def test_a_bare_json_array_is_read_as_items(engine, chain):
    chain.replies = {P1: [QUOTA], P2: [(200, SCRAPER2_ITEMS)]}
    assert temu(engine).fetch_batch("gadgets", 4.5, 500).provider == "temu_secondary_scraper"


# -- configuration ---------------------------------------------------------------------------- #
@pytest.mark.parametrize("entry, problem", [
    ("api_key_env: sk-ant-api03-abcdefghijklmnop", "NAME of an environment variable"),
    ("host: evil.example", "bare RapidAPI host name"),
    ("url: https://elsewhere.p.rapidapi.com/temu", "url must be https://"),
    ("adapter: TemuMagicAdapter", "unknown adapter 'TemuMagicAdapter'"),
    ("adapter: generic", "needs a 'response:' block"),
    ("response: {items: results}", "read by the generic adapter only"),
    ("api_key: abc", "Extra inputs are not permitted"),
])
def test_an_invalid_chain_file_is_a_config_error_that_names_the_provider(engine, tmp_path, monkeypatch, entry, problem):
    key, _, value = entry.partition(": ")
    fields = {"name": "broken", "adapter": "TemuProductScraperAdapter", "host": P1, "url": f"https://{P1}/temu"}
    fields[key] = value
    path = tmp_path / "chain.yaml"
    path.write_text("providers:\n  - " + "\n    ".join(f"{k}: {v}" for k, v in fields.items()) + "\n", encoding="utf-8")
    monkeypatch.setenv("TEMU_PROVIDERS_CONFIG_PATH", str(path))

    with pytest.raises(ConfigError) as excinfo:
        temu(engine).fetch_products("gadgets", 0, 0, 5)
    assert problem in str(excinfo.value) and "provider 'broken'" in str(excinfo.value)
    assert chain_status(engine.settings)["error"]  # the dashboard shows the same message instead of crashing


def test_generic_templates_only_use_known_placeholders():
    with pytest.raises(ValueError, match="unknown placeholder"):
        provider(GenericConfigurableTemuAdapter, P2, "/search",
                 request={"params": {"q": "{query}"}}, response={"items": "items"})
    with pytest.raises(ValueError, match="unknown field"):
        provider(GenericConfigurableTemuAdapter, P2, "/search", response={"items": "items", "fields": {"sku": "id"}})


def test_every_rapidapi_key_variable_is_masked_in_logs(make_settings, monkeypatch):
    numbered, shared = "plainrapid-numbered-000111222333", "plainrapid-shared-444555666777"
    assert numbered in redact(f"request failed for {numbered}")  # nothing knows the value yet
    monkeypatch.setenv("RAPIDAPI_KEY_TEMU_2", numbered)
    monkeypatch.setenv("RAPIDAPI_KEY", shared)
    bootstrap(settings=make_settings(), console_logging=False)

    masked = redact(f"GET https://{P2}/search failed for {numbered} and {shared}")
    assert numbered not in masked and shared not in masked and masked.count("[REDACTED]") == 2


def test_chain_status_reports_order_and_key_presence_without_values(engine, chain, monkeypatch):
    monkeypatch.delenv("RAPIDAPI_KEY_TEMU_2")
    monkeypatch.delenv("RAPIDAPI_KEY_TEMU_3")
    monkeypatch.setenv("RAPIDAPI_KEY", SHARED_KEY)
    status = chain_status(engine.settings)
    assert [(p["order"], p["name"], p["key_state"]) for p in status["providers"]] == [
        (1, "temu_primary_scraper", "Configured"), (2, "temu_secondary_scraper", "Configured (via RAPIDAPI_KEY)"),
        (3, "temu_realtime_backup", "Configured (via RAPIDAPI_KEY)"),
    ]
    monkeypatch.delenv("RAPIDAPI_KEY")
    assert [p["key_state"] for p in chain_status(engine.settings)["providers"]] == ["Configured", "Missing", "Missing"]
    assert status["last_run"] is None and SHARED_KEY not in str(status) and KEYS["RAPIDAPI_KEY_TEMU_1"] not in str(status)


# -- CLI ------------------------------------------------------------------------------------------ #
def test_cli_seed_temu_prints_the_chain_and_fails_when_nothing_is_delivered(make_settings, monkeypatch, capsys, chain):
    import main

    settings = make_settings()
    for name, value in {
        "DATABASE_PATH": settings.database_path, "LOG_FILE": settings.log_file, "LOCAL_MEDIA_DIR": settings.local_media_dir,
    }.items():
        monkeypatch.setenv(name, str(value))
    command = ["--seed-temu", "--category", "gadgets", "--limit", "5"]

    chain.replies = {P1: [QUOTA], P2: [(200, {"results": []})], P3: [(200, {"result": {"goodsList": REALTIME_ITEMS}})]}
    assert main.main(command) == 0
    out = capsys.readouterr().out
    assert "provider temu_realtime_backup" in out and "inserted 2 · skipped 0 duplicate" in out
    lines = [line.split() for line in out.splitlines() if " provider   " in line]
    assert [(line[0], line[2], line[3]) for line in lines] == [
        ("✗", "temu_primary_scraper", "quota_exhausted"), ("✗", "temu_secondary_scraper", "empty"),
        ("✓", "temu_realtime_backup", "success"),
    ]

    temu_providers.reset_run_state()
    chain.replies = {P1: [QUOTA], P2: [QUOTA], P3: [QUOTA]}
    assert main.main(command) == 1
    out = capsys.readouterr().out
    assert "provider none delivered" in out and "No provider delivered usable products" in out
    assert not any(key in out for key in KEYS.values())
