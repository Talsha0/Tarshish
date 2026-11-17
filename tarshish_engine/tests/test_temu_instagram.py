"""The Temu RapidAPI source, the --seed-temu command, the Instagram publisher and the gadgets category end to end.

No network: both APIs are replaced by ``httpx.MockTransport`` handlers that record every request.
"""

import logging
from urllib.parse import parse_qsl

import httpx
import pytest
from pydantic import SecretStr

from api.seed import seed_temu
from core.config import SocialPage
from core.interfaces import PluginContext
from core.models import Product, PublishTarget
from core.resilience import ConfigError, NonRetryableError
from plugins import is_placeholder_image, placeholder_image
from plugins.generators.llm_text_gen import LLMTextGenerator
from plugins.publishers.instagram_pub import InstagramPublisher
from plugins.sources.temu_rapidapi import DEFAULT_HOST, TemuRapidApiSource, TemuSettings

KEY = "rapid0123456789abcdef0123456789abcdef"
TOKEN = "EAAGm0PX4ZCpsBAtesttoken0123456789ZD"
ACCOUNT = "17841400000000000"


# -- Temu API stand-in --------------------------------------------------------------- #
def item(goods_id: str, title: str, *, rating: float, sales: int, price: float = 9.99, **extra) -> dict:
    return {
        "l1Category": "Electronics & Appliances", "l2Category": "Phone Cases & Screen Protector", "title": title,
        "goodsId": goods_id, "salesVolume": sales, "price": price, "rating": rating, "reviewCount": 5,
        "url": f"https://www.temu.com/--g-{goods_id}.html", **extra,
    }


RESULTS = [
    item("601", "Magnetic Phone Case with Stand", rating=5, sales=11779, price=1.99),
    item("602", "Wireless Charging Pad 15W", rating=4.9, sales=52000, image="https://img.kwcdn.com/product/pad.jpg"),
    item("603", "USB-C Hub 7-in-1 Aluminium", rating=4.7, sales=800),
    item("604", "Barely Rated Cable 2m", rating=4.2, sales=90000),                    # below the rating threshold
    item("605", "Brand New Smart Ring", rating=4.9, sales=40),                        # below the sales threshold
    item("606", "wireless charging pad 15w (Free Shipping)", rating=4.6, sales=700),  # the same item as 602
    {"title": "No id, no url", "price": 3},                                           # unusable
]


class TemuApi:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.responses: list[httpx.Response] = []  # queued replies; the normal batch once they run out

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.responses:
            return self.responses.pop(0)
        return httpx.Response(200, json={"success": True, "notice": "Free tier: results are limited.", "results": RESULTS})


@pytest.fixture
def temu_api(monkeypatch):
    api = TemuApi()
    monkeypatch.setenv("RAPIDAPI_KEY", KEY)
    monkeypatch.setattr(TemuRapidApiSource, "_transport", httpx.MockTransport(api))
    return api


def temu(engine, **context) -> TemuRapidApiSource:
    return TemuRapidApiSource(PluginContext(settings=engine.settings, **context))


def ids(products) -> list[str]:
    return [product.external_id for product in products]


# -- Temu source --------------------------------------------------------------------- #
def test_one_request_per_fetch_and_everything_else_is_local(engine, temu_api):
    search = {"keyword": "tech gadgets", "l2Category": "Electronics & Appliances", "priceMin": 1, "priceMax": 100}
    products = temu(engine, options={"search_config": search}).fetch_products("gadgets", 4.5, 500, 15)

    assert len(temu_api.requests) == 1
    request = temu_api.requests[0]
    assert request.method == "GET" and (request.url.host, request.url.path) == (DEFAULT_HOST, "/temu")
    assert request.headers["x-rapidapi-host"] == DEFAULT_HOST and request.headers["x-rapidapi-key"] == KEY
    assert dict(request.url.params) == {
        "keyword": "tech gadgets", "maxItems": "20", "l2Category": "Electronics & Appliances",
        "priceMin": "1", "priceMax": "100",
    }

    # Thresholds, the in-batch duplicate and the unusable item are dropped; the best seller-by-rating leads.
    assert ids(products) == ["602", "601", "603"]
    top = products[0]
    assert (top.source, top.category, top.currency) == ("temu", "gadgets", "USD")
    assert (top.title, top.price, top.rating, top.orders_count) == ("Wireless Charging Pad 15W", 9.99, 4.9, 52000)
    assert top.raw_url == "https://www.temu.com/--g-602.html" == top.affiliate_url  # no tracking id configured
    assert top.image_urls == ["https://img.kwcdn.com/product/pad.jpg"]
    assert is_placeholder_image(products[1].image_urls[0]) and "Gadgets" in products[1].image_urls[0]


def test_affiliate_links_are_built_from_the_template(engine, temu_api, monkeypatch):
    monkeypatch.setenv("TEMU_TRACKING_ID", "aff 42")
    link = temu(engine).fetch_products("gadgets", 4.5, 500, 1)[0].affiliate_url
    assert link == "https://www.temu.com/--g-602.html?_x_sessn_id=aff%2042&refer_page_name=affiliate"

    settings = TemuSettings(temu_tracking_id="t1")
    assert settings.affiliate_url("https://www.temu.com/goods.html?goods_id=9") == (
        "https://www.temu.com/goods.html?goods_id=9&_x_sessn_id=t1&refer_page_name=affiliate"
    )
    custom = TemuSettings(temu_tracking_id="t1", temu_affiliate_url_template="https://go.example/r?to={url}&id={tracking_id}")
    assert custom.affiliate_url("https://www.temu.com/--g-1.html") == "https://go.example/r?to=https://www.temu.com/--g-1.html&id=t1"


def test_invalid_temu_settings_halt_before_any_request(engine, temu_api, monkeypatch):
    monkeypatch.setenv("TEMU_AFFILIATE_URL_TEMPLATE", "https://go.example/{goods}")
    monkeypatch.setenv("RAPIDAPI_TEMU_HOST", "https://elsewhere.example/steal")
    with pytest.raises(ConfigError) as excinfo:
        temu(engine).fetch_products("gadgets", 0, 0, 5)
    assert "TEMU_AFFILIATE_URL_TEMPLATE" in str(excinfo.value) and "RAPIDAPI_TEMU_HOST" in str(excinfo.value)

    monkeypatch.delenv("TEMU_AFFILIATE_URL_TEMPLATE")
    monkeypatch.delenv("RAPIDAPI_TEMU_HOST")
    with pytest.raises(ConfigError, match="temu_search rules"):
        temu(engine, options={"search_config": {"pricemax": 5}}).fetch_products("gadgets", 0, 0, 5)

    # A missing key is not a configuration error: the provider is skipped and the fetch comes back empty.
    monkeypatch.delenv("RAPIDAPI_KEY")
    assert temu(engine).fetch_products("gadgets", 0, 0, 5) == []
    attempt = temu(engine).fetch_batch("gadgets").attempts[0]
    assert attempt.outcome == "missing_key" and "RAPIDAPI_KEY_TEMU_1 or RAPIDAPI_KEY is not set" in attempt.detail
    assert not temu_api.requests


def test_temu_retries_transient_failures_and_never_leaks_the_key(engine, temu_api, clean_environment, caplog):
    caplog.set_level(logging.DEBUG, logger="tarshish")
    temu_api.responses = [httpx.Response(503, json={"message": "upstream busy"})]
    assert len(temu(engine).fetch_products("gadgets", 4.5, 500, 5)) == 3
    assert len(temu_api.requests) == 2 and len(clean_environment) == 1  # one retry, one backoff sleep
    assert "Free tier: results are limited." in caplog.text  # the API notice reaches the debug log

    for response, outcome, hint in [
        (httpx.Response(403, json={"message": f"You are not subscribed to this API. key={KEY}"}), "auth_failed", "subscribed"),
        (httpx.Response(200, json={"success": False, "message": "keyword is required"}), "error", "keyword is required"),
        (httpx.Response(429, json={"message": "You have exceeded the MONTHLY quota for Requests"}), "quota_exhausted", "quota"),
    ]:
        temu_api.requests.clear()
        temu_api.responses = [response]
        batch = temu(engine).fetch_batch("gadgets", 4.5, 500)
        attempt = batch.attempts[0]
        assert batch.provider is None and batch.products == [] and attempt.outcome == outcome and hint in attempt.detail
        assert len(temu_api.requests) == 1 and KEY not in attempt.detail  # no quota burnt on a hopeless retry

    # The key that answered 429 is not asked again for the rest of the run.
    temu_api.requests.clear()
    assert temu(engine).fetch_products("gadgets", 4.5, 500, 5) == [] and not temu_api.requests
    assert KEY not in caplog.text


def job(status: str, **extra) -> httpx.Response:
    """A search-job status reply, as the real API sends it."""
    return httpx.Response(200, json={
        "success": True, "status": status, "jobId": "job-12345", "source": "temu",
        "resultUrl": "/jobs/job-12345", "pollAfterSeconds": 10, **extra,
    })


def test_temu_follows_the_search_job_until_its_results_arrive(engine, temu_api, clean_environment, monkeypatch):
    # The search answers with a running job, not with products: wait and poll, never read it as "no items".
    temu_api.responses = [job("running"), job("running"), job("done", results=RESULTS)]
    assert ids(temu(engine).fetch_products("gadgets", 4.5, 500, 5)) == ["602", "601", "603"]
    assert [request.url.path for request in temu_api.requests] == ["/temu", "/jobs/job-12345", "/jobs/job-12345"]
    assert temu_api.requests[1].headers["x-rapidapi-key"] == KEY and not temu_api.requests[1].url.params
    assert clean_environment == [10.0, 20.0]  # the server's hint first, then a widening gap

    # A job that outlives the wait is reported with its id instead of being thrown away…
    monkeypatch.setenv("TEMU_JOB_TIMEOUT", "30")
    temu_api.requests.clear()
    temu_api.responses = [job("running") for _ in range(4)]
    pending = temu(engine).fetch_batch("gadgets", 4.5, 500).attempts[0]
    assert pending.outcome == "job_pending" and "--job job-12345" in pending.detail
    assert [request.url.path for request in temu_api.requests].count("/temu") == 1

    # …and is collected later by id, without starting (and paying for) a new search.
    temu_api.requests.clear()
    clean_environment.clear()
    temu_api.responses = [job("done", results=RESULTS, query={"keyword": "smart gadgets"})]
    report = seed_temu(engine, category="gadgets", limit=5, job_id="job-12345")
    assert [request.url.path for request in temu_api.requests] == ["/jobs/job-12345"] and clean_environment == []
    assert report.keyword == "smart gadgets" and ids(report.inserted) == ["602", "601", "603"]

    for reply, reason in [
        (job("failed", message="Temu blocked the scraper"), "blocked the scraper"),
        (httpx.Response(200, json={"success": True, "status": "done"}), "neither a 'results' list nor a running job"),
    ]:
        temu_api.responses = [reply]
        failed = temu(engine).fetch_batch("gadgets", 4.5, 500).attempts[0]
        assert failed.outcome == "error" and reason in failed.detail
    with pytest.raises(ConfigError, match="not a Temu job id"):
        temu(engine).fetch_batch("gadgets", job_id="../admin")


# -- --seed-temu --------------------------------------------------------------------- #
def test_seed_temu_inserts_new_products_and_skips_duplicates(engine, temu_api):
    report = seed_temu(engine, category="gadgets", keyword="smart gadgets", limit=2)

    assert temu_api.requests[0].url.params["keyword"] == "smart gadgets"
    assert ids(report.inserted) == ["602", "601"] and ids(report.over_limit) == ["603"] and not report.duplicates
    assert report.received == 7 and len(report.rejected) == 4
    listed, total = engine.db.list_storefront_products(category="gadgets", min_rating=4.5, min_orders=200)
    assert total == 2 and {row["source"] for row in listed} == {"temu"}  # live on the storefront straight away

    again = seed_temu(engine, category="gadgets", limit=2)
    assert ids(again.duplicates) == ["602", "601"] and ids(again.inserted) == ["603"]
    assert len(temu_api.requests) == 2 and engine.db.get_monthly_ai_spend() == 0  # one request per run, no LLM

    # A dry-run previews what is stored and spends no quota.
    preview = temu(engine, dry_run=True).fetch_products("gadgets", 4.5, 500, 5)
    assert ids(preview) == ["602", "601", "603"] and len(temu_api.requests) == 2
    assert preview[0].affiliate_url == "https://www.temu.com/--g-602.html"


def test_cli_seed_temu_prints_inserted_and_skipped(make_settings, monkeypatch, capsys, temu_api):
    import main

    settings = make_settings()
    for name, value in {
        "DATABASE_PATH": settings.database_path, "LOG_FILE": settings.log_file, "LOCAL_MEDIA_DIR": settings.local_media_dir,
    }.items():
        monkeypatch.setenv(name, str(value))
    command = ["--seed-temu", "--category", "gadgets", "--keyword", "smart gadgets", "--limit", "2"]

    assert main.main(command) == 0
    first = capsys.readouterr().out
    assert "inserted 2 · skipped 0 duplicate" in first and "Wireless Charging Pad 15W" in first
    assert "TEMU_TRACKING_ID is not set" in first and "stand-in picture" in first

    assert main.main(command) == 0
    assert "inserted 1 · skipped 2 duplicate" in capsys.readouterr().out

    monkeypatch.delenv("RAPIDAPI_KEY")
    assert main.main(command) == 1  # nothing delivered: a failure, with the reason per provider
    failed = capsys.readouterr().out
    assert "temu_primary_scraper" in failed and "missing_key" in failed and "RAPIDAPI_KEY" in failed
    with pytest.raises(SystemExit):
        main.main(["--seed-temu"])  # --category is required
    with pytest.raises(SystemExit):
        main.main([*command, "--dry-run"])
    assert len(temu_api.requests) == 2


# -- Instagram publisher --------------------------------------------------------------- #
class GraphApi:
    def __init__(self, *statuses: str) -> None:
        self.calls: list[tuple[str, str, dict, httpx.Request]] = []
        self.statuses = list(statuses) or ["FINISHED"]
        self.replies: dict[str, list[httpx.Response]] = {}  # queued replies per endpoint

    def __call__(self, request: httpx.Request) -> httpx.Response:
        endpoint = request.url.path.rsplit("/", 1)[-1]
        self.calls.append((request.method, request.url.path, dict(parse_qsl(request.content.decode())), request))
        if self.replies.get(endpoint):
            return self.replies[endpoint].pop(0)
        if endpoint == "media":
            return httpx.Response(200, json={"id": "container-1"})
        if endpoint == "media_publish":
            return httpx.Response(200, json={"id": "18000000000000001"})
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return httpx.Response(200, json={"status_code": status, "id": "container-1"})

    def paths(self) -> list[tuple[str, str]]:
        return [(method, path) for method, path, _, _ in self.calls]


def instagram(engine, graph, *, credentials: bool = True, dry_run: bool = False) -> InstagramPublisher:
    secrets = {"token": SecretStr(TOKEN), "account_id": SecretStr(ACCOUNT)} if credentials else {}
    target = PublishTarget(platform="instagram_pub", page_name="gadgets", credentials=secrets)
    publisher = engine.plugins.get("publisher", "instagram_pub")(
        PluginContext(settings=engine.settings, dry_run=dry_run), target
    )
    publisher._transport = httpx.MockTransport(graph)
    return publisher


@pytest.fixture
def content(engine):
    product = Product(
        external_id="601099480276615", source="temu", category="gadgets", currency="USD",
        title="Magnetic Wireless Power Bank 10000mAh", price=19.90, rating=4.8, orders_count=3680,
        raw_url="https://www.temu.com/--g-601099480276615.html",
        affiliate_url="https://www.temu.com/--g-601099480276615.html?_x_sessn_id=t1&refer_page_name=affiliate",
        image_urls=["https://img.kwcdn.com/product/power-bank.jpg"],
        video_url="https://video.kwcdn.com/product/power-bank.mp4",
    )
    return LLMTextGenerator(PluginContext(settings=engine.settings, llm_model="mock")).generate(product, "he", None)


def test_instagram_publishes_an_image_in_two_steps(engine, content):
    graph = GraphApi()
    publisher = instagram(engine, graph)
    receipt = publisher.publish(content)

    assert receipt.success and receipt.platform == "instagram" and receipt.platform_post_id == "18000000000000001"
    assert graph.paths() == [
        ("POST", f"/v21.0/{ACCOUNT}/media"), ("GET", "/v21.0/container-1"), ("POST", f"/v21.0/{ACCOUNT}/media_publish"),
    ]
    create = graph.calls[0][2]
    assert create["image_url"] == content.product.image_urls[0] and "media_type" not in create
    assert graph.calls[2][2] == {"creation_id": "container-1"}

    caption = create["caption"]
    assert content.caption in caption and "בביו" in caption and "/c/gadgets" in caption
    assert all(f"#{tag}" in caption for tag in content.hashtags) and "קישור שותפים" in caption
    assert content.product.affiliate_url not in caption  # links are not clickable in captions
    assert "🔗 Link in bio -> /c/gadgets" in publisher.render_caption(content.model_copy(update={"language": "en"}))

    for _, _, form, request in graph.calls:  # the token travels in a header only
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        assert TOKEN not in str(request.url) and TOKEN not in str(form)


def test_instagram_publishes_video_as_a_reel_once_it_is_processed(engine, content, clean_environment):
    graph = GraphApi("IN_PROGRESS", "IN_PROGRESS", "FINISHED")
    receipt = instagram(engine, graph).publish(content.model_copy(update={"content_type": "video"}))

    create = graph.calls[0][2]
    assert receipt.success and create["media_type"] == "REELS" and create["video_url"] == content.product.video_url
    assert "image_url" not in create
    assert [method for method, _ in graph.paths()] == ["POST", "GET", "GET", "GET", "POST"]
    assert len(clean_environment) == 2  # waited twice for processing instead of publishing too early


def test_instagram_simulates_in_dry_run_and_without_credentials(engine, content, caplog):
    caplog.set_level(logging.INFO, logger="tarshish")
    graph = GraphApi()

    receipt = instagram(engine, graph, dry_run=True).publish(content)
    assert receipt.success and receipt.platform_post_id.startswith("dry-run-")
    assert content.product.image_urls[0] in caplog.text and content.caption in caplog.text
    assert "/c/gadgets" in caplog.text and TOKEN not in caplog.text

    caplog.clear()
    receipt = instagram(engine, graph, credentials=False).publish(content)
    assert not receipt.success and "not configured" in receipt.error_message
    assert content.product.image_urls[0] in caplog.text and content.caption in caplog.text
    assert not graph.calls  # neither path touched the network


def test_instagram_failures_are_classified_and_never_leak_the_token(engine, content, clean_environment):
    graph = GraphApi()
    graph.replies["media"] = [
        httpx.Response(500, json={"error": {"message": "Service temporarily unavailable", "code": 2, "is_transient": True}}),
    ]
    assert instagram(engine, graph).publish(content).success  # a transient error costs a retry, not the post
    assert graph.paths().count(("POST", f"/v21.0/{ACCOUNT}/media")) == 2 and len(clean_environment) == 1

    graph = GraphApi()
    graph.replies["media"] = [httpx.Response(400, json={"error": {
        "message": f"Error validating access token {TOKEN}: Session has expired", "type": "OAuthException", "code": 190,
    }})]
    with pytest.raises(NonRetryableError) as expired:
        instagram(engine, graph).publish(content)
    assert "code 190" in str(expired.value) and TOKEN not in str(expired.value) and len(graph.calls) == 1

    def lost_reply(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/media_publish"):
            raise httpx.ReadTimeout("timed out", request=request)
        return graph(request)

    graph = GraphApi()
    with pytest.raises(NonRetryableError, match="outcome unknown"):  # publishing twice would post twice
        instagram(engine, lost_reply).publish(content)

    graph = GraphApi()
    stand_in = content.product.model_copy(update={"image_urls": [placeholder_image("gadgets")]})
    with pytest.raises(NonRetryableError, match="stand-in picture"):
        instagram(engine, graph).publish(content.model_copy(update={"product": stand_in}))
    assert not graph.calls  # refused before anything was sent


def test_instagram_page_credentials_come_from_the_named_env_vars(engine, monkeypatch):
    page = SocialPage.model_validate({
        "platform": "instagram_pub", "page_name": "@gadgets",
        "token_env_var": "IG_GADGETS_TOKEN", "account_id_env_var": "IG_GADGETS_ACCOUNT_ID",
    })
    target, reason = engine.registry.resolve_target(page)
    assert target is None and "IG_GADGETS_TOKEN" in reason and "IG_GADGETS_ACCOUNT_ID" in reason

    monkeypatch.setenv("IG_GADGETS_TOKEN", TOKEN)
    monkeypatch.setenv("IG_GADGETS_ACCOUNT_ID", ACCOUNT)
    target, reason = engine.registry.resolve_target(page)
    assert reason is None and (target.credential("token"), target.credential("account_id")) == (TOKEN, ACCOUNT)


def test_dry_runs_can_be_previewed_through_the_instagram_publisher(make_settings, caplog):
    from core import bootstrap

    engine = bootstrap(settings=make_settings(dry_run_publisher="instagram_pub"), console_logging=False)
    engine.harvester.harvest("gadgets", "temu_mock")
    caplog.set_level(logging.INFO, logger="tarshish")
    report = engine.orchestrator.run("gadgets", "he", ["instagram_post", "instagram_story"], dry_run=True)
    assert report.ok and report.count("preview") == 6
    assert caplog.text.count("simulated (dry-run)") == 6 and "stand-in picture" in caplog.text  # mock photos
    assert caplog.text.count("Instagram Story simulated") == 3  # the story profile asks for the Story format


# -- gadgets: harvest once (Temu) ➔ publish per language (Instagram) ------------------------- #
def instagram_env(monkeypatch, graph):
    for suffix in ("", "_GADGETS_EN"):
        monkeypatch.setenv(f"INSTAGRAM_ACCESS_TOKEN{suffix}", TOKEN)
        monkeypatch.setenv(f"INSTAGRAM_BUSINESS_ACCOUNT_ID{suffix}", ACCOUNT)
    monkeypatch.setattr(InstagramPublisher, "_transport", httpx.MockTransport(graph))


def test_gadgets_configuration_is_seeded_from_yaml(engine, temu_api):
    category = engine.registry.get_category("gadgets")
    assert (category.min_rating, category.min_orders, category.enabled) == (4.5, 500, True)
    assert category.temu_search == {
        "keyword": "smart tech gadgets", "l2Category": "Electronics & Appliances", "priceMin": 1, "priceMax": 100,
    }
    he = engine.registry.get_social_account("gadgets", "he", "instagram")
    assert (he.account_handle, he.token_env_var, he.account_id_env_var) == (
        "gadgets", "INSTAGRAM_ACCESS_TOKEN", "INSTAGRAM_BUSINESS_ACCOUNT_ID")

    # Nothing harvested yet: publishing never asks a store — it reports what to harvest.
    preview = engine.orchestrator.run("gadgets", "he", ["instagram_post"], dry_run=True)
    assert preview.ok and preview.items == [] and not temu_api.requests
    assert any("--harvest --category gadgets" in warning for warning in preview.warnings)


def test_gadgets_is_harvested_once_and_each_language_publishes_fresh_products_first(engine, temu_api, monkeypatch):
    instagram_env(monkeypatch, GraphApi())
    [harvest] = engine.harvester.harvest("gadgets", "temu_rapidapi")
    assert harvest.ok and ids(harvest.inserted) == ["602", "601", "603"]
    assert len(temu_api.requests) == 1 and temu_api.requests[0].url.params["keyword"] == "smart tech gadgets"

    first = engine.orchestrator.run("gadgets", "he", ["instagram_post"], limit=2)
    assert first.ok and [(i.external_id, i.status) for i in first.items] == [("602", "queued"), ("601", "queued")]
    second = engine.orchestrator.run("gadgets", "he", ["instagram_post"], limit=2)
    assert [(i.external_id, i.status) for i in second.items] == [("603", "queued")]
    english = engine.orchestrator.run("gadgets", "en", ["instagram_post"], limit=5)
    assert [i.external_id for i in english.items] == ["602", "601", "603"]  # its own account: all still fresh

    assert len(temu_api.requests) == 1  # Stage 2 never fetches: one search served both languages
    queued = engine.db.list_publications(statuses=["QUEUED"])
    assert sorted({(row["language"], row["target_page"]) for row in queued}) == [("en", "gadgets_en"), ("he", "gadgets")]


def test_gadgets_posts_and_stories_go_out_with_their_own_models(engine, temu_api, monkeypatch):
    graph = GraphApi()
    instagram_env(monkeypatch, graph)
    engine.harvester.harvest("gadgets", "temu_rapidapi")

    report = engine.orchestrator.run("gadgets", "he", ["instagram_post", "instagram_story"], mode="immediate", limit=1)

    assert report.ok and report.count("published") == 2 and not report.warnings
    assert {(r.content_type, r.generator, r.llm_model) for r in report.routes} == {
        ("instagram_post", "llm_text_gen", "claude-sonnet-5-5"), ("instagram_story", "llm_story_gen", "claude-haiku-5-5"),
    }
    rows = {row["content_format"]: row for row in engine.db.list_publications(pipeline_id="gadgets_he")}
    assert {row["status"] for row in rows.values()} == {"PUBLISHED"}
    post, story = (call[2] for call in graph.calls if call[0] == "POST" and call[1].endswith("/media"))
    assert post["image_url"] == "https://img.kwcdn.com/product/pad.jpg" and "/c/gadgets" in post["caption"]
    assert story["media_type"] == "STORIES" and "caption" not in story  # Stories take no caption
    assert len(rows["instagram_story"]["caption"]) <= 90  # the overlay hook, not a feed caption
