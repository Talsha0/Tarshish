"""FindHub live content end to end: real products only, free LLM failover, branded images, DB, publishing.

Every network call is an ``httpx.MockTransport``: the free LLM providers, the product-photo CDN, and the
Telegram / Instagram / TikTok APIs. Images are really rendered with Pillow and checked on disk.
"""

from __future__ import annotations

import io
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from core import bootstrap
from core.config import SocialAccount
from core.models import Product
from plugins.generators import free_llm_chain
from plugins.generators.content_generator import AssetPublishError, ContentGenerator, clean_copy
from plugins.generators.free_llm_chain import FreeLLMChain, FreeLLMChainExhausted
from plugins.publishers.instagram_pub import InstagramPublisher
from plugins.publishers.telegram_pub import TelegramPublisher
from plugins.publishers.tiktok_pub import TikTokPublisher

GEMINI_KEY = "AIzaSyTESTgeminiKey0123456789abcdefghij"
GROQ_KEY = "gsk_TESTgroqKey0123456789abcdefghijklmnop"
OPENROUTER_KEY = "sk-or-v1-TESTopenrouterKey0123456789abcdef"
BASE = "https://findhub.test"

COPY = {
    "hook": "🎧 Silence the commute, not your wallet",
    "highlights": ["Active noise cancelling", "30-hour battery with the case", "Bluetooth 5.3 pairing"],
    "instagram_caption": "🎧 Your commute just got quieter\n✨ Hand-picked for our gadgets niche\nSee https://evil.example.com",
    "tiktok_caption": "POV: the train is loud and you can't hear it 🎧",
    "story_overlay": "Silence for less 🎧\nTap the link",
    "hashtags": ["gadgets", "#earbuds", "deals", "niche"],
}


@pytest.fixture(autouse=True)
def _fresh_chain():
    free_llm_chain.reset_cooldowns()
    yield
    free_llm_chain.reset_cooldowns()


def png_bytes(size=(640, 640)) -> bytes:
    image = Image.new("RGB", size, (210, 220, 235))
    ImageDraw.Draw(image).ellipse((120, 120, 520, 520), fill=(40, 60, 120))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


PHOTO = png_bytes()


def photo_cdn(request: httpx.Request) -> httpx.Response:
    assert request.url.host == "img.example.com", request.url
    return httpx.Response(200, content=PHOTO, headers={"content-type": "image/png"})


class FreeLLMs:
    """Gemini answers 429 (quota exhausted); Groq answers with the JSON copy; OpenRouter is never needed."""

    def __init__(self, gemini_status=429, groq_status=200, openrouter_status=200):
        self.calls: list[httpx.Request] = []
        self.gemini_status, self.groq_status, self.openrouter_status = gemini_status, groq_status, openrouter_status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        host = request.url.host
        if host == "generativelanguage.googleapis.com":
            assert request.headers["x-goog-api-key"] == GEMINI_KEY and GEMINI_KEY not in str(request.url)
            if self.gemini_status == 429:
                return httpx.Response(429, json={"error": {
                    "code": 429, "status": "RESOURCE_EXHAUSTED",
                    "message": f"Quota exceeded for metric generate_content_free_tier_requests (key {GEMINI_KEY})",
                    "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "41s"}]}})
            return httpx.Response(200, json={
                "candidates": [{"content": {"parts": [{"text": json.dumps(COPY)}]}}],
                "usageMetadata": {"promptTokenCount": 300, "candidatesTokenCount": 120}})
        if host == "api.groq.com":
            assert request.headers["authorization"] == f"Bearer {GROQ_KEY}"
            if self.groq_status != 200:
                return httpx.Response(self.groq_status, json={"error": {"message": "Rate limit reached for requests per day (RPD)"}})
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(COPY)}}],
                                             "usage": {"prompt_tokens": 310, "completion_tokens": 140}})
        if host == "openrouter.ai":
            if self.openrouter_status != 200:
                return httpx.Response(200, json={"error": {"code": 429, "message": "Rate limit exceeded: free-models-per-day"}})
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(COPY)}}]})
        raise AssertionError(f"unexpected LLM call {request.url}")

    def hosts(self) -> list[str]:
        return [f"{c.url.host}{c.url.path}" for c in self.calls]


@pytest.fixture
def engine_with_keys(make_settings, tmp_path, monkeypatch):
    settings = make_settings(
        gemini_api_key=GEMINI_KEY, groq_api_key=GROQ_KEY, openrouter_api_key=OPENROUTER_KEY,
        storefront_base_url=BASE, generated_media_dir=tmp_path / "generated_media",
    )
    return bootstrap(settings=settings, console_logging=False)


def real_product(source: str, external_id: str, title: str, *, orders: int, price=79.9, original=129.0) -> Product:
    return Product(
        external_id=external_id, source=source, category="gadgets", title=title, price=price, original_price=original,
        currency="ILS", rating=4.9, orders_count=orders, raw_url=f"https://www.{source}.com/item/{external_id}.html",
        affiliate_url=f"https://s.click.{source}.com/e/aff{external_id}", image_urls=[f"https://img.example.com/{external_id}.jpg"],
    )


@pytest.fixture
def catalogue(engine_with_keys):
    """The mock catalogue harvested next to two real products (one per store)."""
    engine = engine_with_keys
    engine.harvester.harvest("gadgets", "temu_mock")
    engine.harvester.harvest("gadgets", "aliexpress_mock")
    earbuds = engine.db.upsert_product(real_product("aliexpress", "1005001", "ANC Wireless Earbuds BT 5.3", orders=2431))
    lamp = engine.db.upsert_product(real_product("temu", "601002", "LED Smart Desk Lamp Touch Dimmer", orders=980,
                                                 price=24.0, original=None))
    return engine, earbuds, lamp


def generator(engine, llms=None) -> ContentGenerator:
    chain = FreeLLMChain(engine.settings, transport=httpx.MockTransport(llms or FreeLLMs()))
    return ContentGenerator(engine, llm_chain=chain, image_transport=httpx.MockTransport(photo_cdn))


# --------------------------------------------------------------------------- #
# 1. Real products only
# --------------------------------------------------------------------------- #
def test_mock_products_are_flagged_and_never_selected(catalogue):
    engine, earbuds, lamp = catalogue
    flags = {row["source"]: row["is_mock"] for row in engine.db.query("SELECT DISTINCT source, is_mock FROM products")}
    assert flags == {"temu_mock": 1, "aliexpress_mock": 1, "temu": 0, "aliexpress": 0}
    mock_ids = {row["id"] for row in engine.db.query("SELECT id FROM products WHERE source LIKE '%mock'")}
    assert mock_ids, "the mock catalogue should be harvested next to the real products"

    rows = engine.db.real_products_for_assets(category="gadgets", language="he", content_type="instagram_post", limit=50)
    assert [row["id"] for row in rows] == [earbuds, lamp]          # best sellers first, mocks excluded

    report = generator(engine).generate_batch("gadgets", "he", ["telegram_post", "instagram_post"], limit=10)
    used = {item.product_id for item in report.items}
    assert used == {earbuds, lamp} and not used & mock_ids
    stored = {row["product_id"] for row in engine.db.query("SELECT product_id FROM generated_assets")}
    assert stored == {earbuds, lamp}
    # Once generated, a product is not offered again for the same type + language.
    assert engine.db.real_products_for_assets(category="gadgets", language="he", content_type="instagram_post") == []


def test_mock_rows_written_before_the_flag_existed_are_backfilled(catalogue):
    engine, *_ = catalogue
    engine.db.query("SELECT 1")  # connection sanity
    with engine.db.connect() as conn:
        conn.execute("UPDATE products SET is_mock = 0")
    engine.db.initialize()  # the migration runs on every start
    assert {row["source"] for row in engine.db.query("SELECT source FROM products WHERE is_mock = 1")} == {
        "temu_mock", "aliexpress_mock"}


# --------------------------------------------------------------------------- #
# 2. Free LLM failover chain
# --------------------------------------------------------------------------- #
def test_gemini_429_fails_over_to_groq_and_cools_down(engine_with_keys, caplog):
    llms = FreeLLMs(gemini_status=429)
    chain = FreeLLMChain(engine_with_keys.settings, transport=httpx.MockTransport(llms))

    result = chain.complete("system", "write json", validate=lambda text: "hook" in text)
    assert (result.provider, result.model) == ("groq", "llama-3.3-70b-versatile")
    assert result.tokens_input == 310 and result.tokens_output == 140
    assert llms.hosts() == [
        "generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
        "generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent",
        "api.groq.com/openai/v1/chat/completions",
    ]
    assert len(result.attempts) == 2 and all("429" in attempt for attempt in result.attempts)
    # The key quoted back in Gemini's error is masked everywhere.
    assert GEMINI_KEY not in " ".join(result.attempts) and GEMINI_KEY not in caplog.text
    assert "[REDACTED]" in " ".join(result.attempts)

    # Gemini is cooling down (retryDelay 41s): the next call goes straight to Groq.
    llms.calls.clear()
    assert chain.complete("system", "again").provider == "groq"
    assert llms.hosts() == ["api.groq.com/openai/v1/chat/completions"]
    status = {row["model"]: row for row in chain.status()}
    assert status["gemini-2.5-flash"]["cooling_down_seconds"] > 0 and status["gemini-2.5-flash"]["configured"]


def test_the_chain_reaches_openrouter_and_then_reports_exhaustion(engine_with_keys):
    llms = FreeLLMs(gemini_status=429, groq_status=429, openrouter_status=200)
    chain = FreeLLMChain(engine_with_keys.settings, transport=httpx.MockTransport(llms))
    assert chain.complete("s", "p").label == "openrouter:meta-llama/llama-3.3-70b-instruct:free"

    free_llm_chain.reset_cooldowns()
    dead = FreeLLMChain(engine_with_keys.settings,
                        transport=httpx.MockTransport(FreeLLMs(gemini_status=429, groq_status=429, openrouter_status=429)))
    with pytest.raises(FreeLLMChainExhausted) as excinfo:
        dead.complete("s", "p")
    assert len(excinfo.value.attempts) == 4


def test_without_free_llm_quota_the_offline_templates_write_the_copy(engine_with_keys):
    engine = engine_with_keys
    product_id = engine.db.upsert_product(real_product("temu", "77", "Magnetic Phone Stand", orders=500))
    llms = FreeLLMs(gemini_status=429, groq_status=429, openrouter_status=429)
    report = generator(engine, llms).generate_batch("gadgets", "en", ["telegram_post"])
    asset = engine.db.get_generated_asset(report.items[0].asset_id)
    assert asset["product_id"] == product_id
    assert asset["llm_model_used"] == "offline-template (free LLM quota exhausted)"
    assert f"{BASE}/en/p/{product_id}" in asset["generated_text"]


# --------------------------------------------------------------------------- #
# 3. Copy, links, images and persistence
# --------------------------------------------------------------------------- #
def test_generation_writes_branded_images_links_and_db_rows(catalogue):
    engine, earbuds, lamp = catalogue
    report = generator(engine).generate_batch(
        "gadgets", "he", ["telegram_post", "instagram_post", "instagram_story", "instagram_reel", "tiktok_post"], limit=1)
    assert report.count("skipped") == 0 and report.created == 5
    assets = {row["content_type"]: row for row in engine.db.list_generated_assets()}
    assert set(assets) == {"telegram_post", "instagram_post", "instagram_story", "instagram_reel", "tiktok_post"}

    for row in assets.values():
        assert row["product_id"] == earbuds and row["category_id"] == "gadgets" and row["language"] == "he"
        assert row["llm_model_used"] == "groq:llama-3.3-70b-versatile"
        assert "niche" not in row["generated_text"].lower()
    # Routing: the seeded matrix has Instagram + TikTok accounts for gadgets/he, but no Telegram one.
    assert (assets["instagram_post"]["status"], assets["instagram_post"]["target_account_id"]) == ("QUEUED", "gadgets:he:instagram")
    assert assets["instagram_post"]["target_account_handle"] == "gadgets"
    assert assets["tiktok_post"]["target_account_id"] == "gadgets:he:tiktok"
    assert (assets["telegram_post"]["status"], assets["telegram_post"]["target_account_id"]) == ("DRAFT", None)

    # Telegram: both links, storefront deep link by product id.
    telegram = assets["telegram_post"]["generated_text"]
    assert "https://s.click.aliexpress.com/e/aff1005001" in telegram
    assert f"{BASE}/he/p/{earbuds}" in telegram
    assert "🔇" not in telegram and "• Active noise cancelling" in telegram and "38% הנחה" in telegram

    # Feed posts: no raw URL, localized link-in-bio CTA naming the category, hashtags.
    for kind in ("instagram_post", "tiktok_post"):
        text = assets[kind]["generated_text"]
        assert "http" not in text and "evil.example" not in text
        assert "לרכישה ופרטים נוספים כנסו ללינק בביו (קטגוריית" in text and "#gadgets" in text
        links = json.loads(assets[kind]["links_payload_json"])
        assert links["cta"]["kind"] == "link_in_bio" and links["cta"]["bio_link"] == f"{BASE}/he/c/gadgets"

    # Stories / Reels: overlay hook + clickable sticker metadata.
    story = assets["instagram_story"]
    assert story["generated_text"] == "Silence for less 🎧\nTap the link"
    stickers = json.loads(story["links_payload_json"])["story_links"]
    assert [(s["kind"], s["url"]) for s in stickers] == [
        ("storefront", f"{BASE}/he/c/gadgets"), ("product", f"{BASE}/he/p/{earbuds}"),
        ("affiliate", "https://s.click.aliexpress.com/e/aff1005001")]
    assert "reel_caption" in json.loads(assets["instagram_reel"]["links_payload_json"])

    # Images: real files with the right framing, served under /media.
    sizes = {"instagram_post": (1080, 1350), "instagram_story": (1080, 1920), "instagram_reel": (1080, 1920),
             "tiktok_post": (1080, 1920)}
    for kind, size in sizes.items():
        row = assets[kind]
        with Image.open(row["generated_image_path"]) as image:
            assert image.size == size and image.format == "JPEG"
        assert row["generated_image_url"].startswith(f"{BASE}/media/gadgets/{kind}_{earbuds}_he_")
    assert assets["telegram_post"]["generated_image_path"] is None

    # The free model's tokens are on the AI ledger, at no cost.
    usage = engine.db.query("SELECT model_used, generation_cost_usd, tokens_input FROM ai_usage")
    assert usage == [{"model_used": "groq:llama-3.3-70b-versatile", "generation_cost_usd": 0.0, "tokens_input": 310}]


def test_the_branded_image_carries_the_discount_and_rating_elements(catalogue):
    """Pixel checks: flash-red discount tag top-right, spruce header top-left, dark rating pill, yellow price."""
    engine, earbuds, lamp = catalogue
    generator(engine).generate_batch("gadgets", "en", ["instagram_post", "instagram_story"], limit=2)
    rows = {(r["product_id"], r["content_type"]): r for r in engine.db.list_generated_assets()}

    def close(pixel, colour, tolerance=40):
        return all(abs(a - b) <= tolerance for a, b in zip(pixel, colour))

    with Image.open(rows[(earbuds, "instagram_post")]["generated_image_path"]) as post:
        post = post.convert("RGB")
        assert close(post.getpixel((138, 68)), (15, 76, 58))           # FindHub pill (spruce)
        assert close(post.getpixel((1000, 80)), (211, 36, 59))         # -38% OFF tag (flash red)
        assert close(post.getpixel((80, 1145)), (20, 36, 31))          # rating pill
        assert close(post.getpixel((1000, 1130)), (255, 212, 59))      # price shelf label
        assert close(post.getpixel((540, 1340)), (15, 76, 58))         # CTA bar
    with Image.open(rows[(lamp, "instagram_post")]["generated_image_path"]) as plain:
        assert not close(plain.convert("RGB").getpixel((1000, 80)), (211, 36, 59))  # no discount, no tag
    with Image.open(rows[(earbuds, "instagram_story")]["generated_image_path"]) as story:
        story = story.convert("RGB")
        top_band = story.crop((0, 0, 1080, 240)).getcolors(1_000_000)
        bottom_band = story.crop((0, 1590, 1080, 1920)).getcolors(1_000_000)
        # Nothing but the background gradient in the platform overlay zones.
        assert all(min(colour) > 200 for _, colour in top_band + bottom_band)


def test_clean_copy_strips_urls_and_the_word_niche():
    assert clean_copy("Best niche pick at www.spam.com https://x.y/z", "en") == "Best category pick at"
    assert clean_copy("הנישה הכי חמה", "he") == "הקטגוריה הכי חמה"


# --------------------------------------------------------------------------- #
# 4. Publishing on approval
# --------------------------------------------------------------------------- #
def test_publishing_routes_each_asset_through_its_platform(catalogue, monkeypatch):
    engine, earbuds, _ = catalogue
    monkeypatch.setenv("TG_GADGETS_HE_TOKEN", "123456:TESTtelegramBotToken0123456789")
    monkeypatch.setenv("TG_GADGETS_HE_CHAT", "-1001234567890")
    monkeypatch.setenv("INSTAGRAM_ACCESS_TOKEN", "IGQVJtestInstagramToken0123456789abcdef")
    monkeypatch.setenv("INSTAGRAM_BUSINESS_ACCOUNT_ID", "17841400000000001")
    monkeypatch.setenv("TIKTOK_ACCESS_TOKEN_GADGETS_HE", "act.testTikTokAccessToken0123456789")
    engine.registry.save_social_account(SocialAccount(
        category_id="gadgets", language="he", platform="telegram", account_handle="@findhub_gadgets_il",
        token_env_var="TG_GADGETS_HE_TOKEN", account_id_env_var="TG_GADGETS_HE_CHAT"))
    gen = generator(engine)
    gen.generate_batch("gadgets", "he", ["telegram_post", "instagram_story", "instagram_reel", "tiktok_post"], limit=1)
    ids = {row["content_type"]: row["id"] for row in engine.db.list_generated_assets()}

    sent: list[httpx.Request] = []

    def telegram(request):
        sent.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 555}})

    def instagram(request):
        sent.append(request)
        if request.url.path.endswith("/media"):
            return httpx.Response(200, json={"id": "container-1"})
        if request.url.path.endswith("/media_publish"):
            return httpx.Response(200, json={"id": "ig-media-9"})
        return httpx.Response(200, json={"status_code": "FINISHED"})

    def tiktok(request):
        sent.append(request)
        ok = lambda data: httpx.Response(200, json={"data": data, "error": {"code": "ok"}})  # noqa: E731
        if request.url.path == "/v2/post/publish/creator_info/query/":
            return ok({"privacy_level_options": ["PUBLIC_TO_EVERYONE", "SELF_ONLY"]})
        if request.url.path == "/v2/post/publish/content/init/":
            return ok({"publish_id": "p_photo_1"})
        return ok({"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": [7311]})

    monkeypatch.setattr(TelegramPublisher, "_transport", httpx.MockTransport(telegram))
    monkeypatch.setattr(InstagramPublisher, "_transport", httpx.MockTransport(instagram))
    monkeypatch.setattr(TikTokPublisher, "_transport", httpx.MockTransport(tiktok))

    # Telegram: a plain text post with both links, exactly once each.
    assert gen.publish_asset(ids["telegram_post"]).success
    message = json.loads(sent[-1].content)
    assert sent[-1].url.path.endswith("/sendMessage")
    assert message["text"].count("https://s.click.aliexpress.com/e/aff1005001") == 1
    assert message["text"].count(f"{BASE}/he/p/{earbuds}") == 1

    # Instagram Story: the generated 9:16 image, no caption; stickers stay on the asset.
    sent.clear()
    outcome = gen.publish_asset(ids["instagram_story"])
    assert outcome.success and outcome.platform_post_id == "ig-media-9"
    container = dict(httpx.QueryParams(sent[0].content.decode()))
    assert container["media_type"] == "STORIES" and "caption" not in container
    assert container["image_url"].startswith(f"{BASE}/media/gadgets/instagram_story_")
    story = engine.db.get_generated_asset(ids["instagram_story"])
    assert story["status"] == "PUBLISHED" and json.loads(story["links_payload_json"])["story_links_applied"] is False

    # Reels need video: refused with an explanation, nothing sent, status unchanged.
    sent.clear()
    with pytest.raises(AssetPublishError, match="Reels from a video"):
        gen.publish_asset(ids["instagram_reel"])
    assert sent == [] and engine.db.get_generated_asset(ids["instagram_reel"])["status"] == "QUEUED"

    # TikTok: Direct Post in photo mode, pulled from the public /media URL.
    outcome = gen.publish_asset(ids["tiktok_post"])
    assert outcome.success and outcome.platform_post_id == "7311"
    init = json.loads(next(r for r in sent if r.url.path == "/v2/post/publish/content/init/").content)
    assert init["media_type"] == "PHOTO" and init["post_mode"] == "DIRECT_POST"
    assert init["source_info"]["photo_images"][0].startswith(f"{BASE}/media/gadgets/tiktok_post_")
    assert len(init["post_info"]["title"]) <= 90 and "http" not in init["post_info"]["description"]

    # Published assets are not published twice.
    with pytest.raises(AssetPublishError, match="PUBLISHED"):
        gen.publish_asset(ids["telegram_post"])


def test_a_platform_failure_marks_the_asset_failed(catalogue, monkeypatch):
    engine, *_ = catalogue
    monkeypatch.setenv("INSTAGRAM_ACCESS_TOKEN", "IGQVJtestInstagramToken0123456789abcdef")
    monkeypatch.setenv("INSTAGRAM_BUSINESS_ACCOUNT_ID", "17841400000000001")
    gen = generator(engine)
    gen.generate_batch("gadgets", "he", ["instagram_post"], limit=1)
    asset_id = engine.db.list_generated_assets()[0]["id"]
    monkeypatch.setattr(InstagramPublisher, "_transport", httpx.MockTransport(lambda r: httpx.Response(
        400, json={"error": {"code": 190, "error_subcode": 463, "message": "Error validating access token: expired"}})))
    outcome = gen.publish_asset(asset_id)
    assert not outcome.success and "expired" in outcome.message
    row = engine.db.get_generated_asset(asset_id)
    assert row["status"] == "FAILED" and row["locked_at"] is None and row["attempts"] == 1


# --------------------------------------------------------------------------- #
# 5. Admin API and /media
# --------------------------------------------------------------------------- #
def test_admin_lists_assets_and_media_is_served(catalogue, monkeypatch):
    engine, earbuds, _ = catalogue
    generator(engine).generate_batch("gadgets", "en", ["instagram_post", "instagram_reel"], limit=1)
    monkeypatch.setenv("ADMIN_API_SECRET", "test-admin-secret-0123456789")
    from storefront_api.server import create_app

    with TestClient(create_app(engine.settings)) as api:
        key = {"X-Admin-Key": "test-admin-secret-0123456789"}
        body = api.get("/api/admin/assets", headers=key).json()
        assert body["counts"]["QUEUED"] == 2 and body["storefront_base_url"] == BASE
        assert [row["provider"] for row in body["llm_chain"]] == ["gemini", "gemini", "groq", "openrouter"]
        by_type = {item["content_type"]: item for item in body["items"]}
        assert by_type["instagram_post"]["publishable"] is True
        assert by_type["instagram_reel"]["publishable"] is False and "video" in by_type["instagram_reel"]["not_publishable_reason"]
        assert by_type["instagram_post"]["links"]["cta"]["kind"] == "link_in_bio"
        assert GEMINI_KEY not in json.dumps(body)

        relative = by_type["instagram_post"]["generated_image_url"].removeprefix(BASE)
        media = api.get(relative)
        assert media.status_code == 200 and media.headers["content-type"] == "image/jpeg"
        assert Image.open(io.BytesIO(media.content)).size == (1080, 1350)

        reel = by_type["instagram_reel"]["id"]
        refused = api.post("/api/admin/assets/publish", headers=key, json={"ids": [reel]})
        assert refused.status_code == 409 and "video" in refused.json()["detail"]
        assert api.post(f"/api/admin/assets/{reel}/status", headers=key, json={"status": "DRAFT"}).json()["status"] == "DRAFT"
        assert api.post(f"/api/admin/assets/{reel}/mark-published", headers=key).json()["status"] == "PUBLISHED"
        post = by_type["instagram_post"]
        assert api.delete(f"/api/admin/assets/{post['id']}", headers=key).json() == {"deleted": post["id"], "media_removed": True}
        assert api.get(relative).status_code == 404


def test_cli_flags_are_validated():
    import main

    parser = main.build_parser()
    with pytest.raises(SystemExit):
        main.validate_args(parser, parser.parse_args(["--generate-assets", "--category", "gadgets"]))
    with pytest.raises(SystemExit):
        main.validate_args(parser, parser.parse_args(["--generate-assets", "--category", "gadgets", "--lang", "fr"]))
    args = parser.parse_args(["--generate-assets", "--category", "gadgets", "--lang", "he",
                              "--content-types", "telegram_post,instagram_story", "--limit", "2"])
    main.validate_args(parser, args)
    assert parser.parse_args(["--publish-assets", "3,4,3"]).publish_assets == [3, 4]
