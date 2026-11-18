"""The dashboard's Mobile Device Preview Simulator and the Content Review actions behind it."""

import json
from pathlib import Path

import pytest

from core.config import SocialAccount, SocialPage
from core.database import product_from_row
from core.models import GeneratedContent, PublicationStatus
from dashboard.components.mobile_simulator import (
    FRAME_H,
    FRAME_W,
    phone_media_source,
    render_phone,
)
from services.content_review import ContentReview, ReviewError

PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
       b"\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa7\x35\x81\x84\x00\x00\x00\x00IEND\xaeB`\x82")
TEXT = {
    "he": "האוזניות שעושות שקט 🎧\n₪79.90 בלבד\n\n🔗 הקישור בביו\n\n#FindHub #גאדג׳טים @findhub",
    "en": "The desk lamp that fixes your evening 💡\n$24 only\n\n🔗 Link in bio\n\n#FindHub #gadgets @findhub",
}


@pytest.fixture
def review(make_settings, tmp_path, monkeypatch):
    """A review service on its own database: harvested gadgets, dry-run Instagram + Telegram accounts."""
    from core import bootstrap

    monkeypatch.setenv("SIM_IG_TOKEN", "dummy-token-value")
    monkeypatch.setenv("SIM_IG_ACCOUNT", "1784")
    monkeypatch.delenv("SIM_TG_TOKEN", raising=False)
    engine = bootstrap(settings=make_settings(generated_media_dir=tmp_path / "generated_media"), console_logging=False)
    engine.harvester.harvest("gadgets", "temu_mock")
    for language in ("he", "en"):
        engine.registry.save_social_account(SocialAccount(
            category_id="gadgets", language=language, platform="instagram", account_handle=f"@findhub_gadgets_{language}",
            token_env_var="SIM_IG_TOKEN", account_id_env_var="SIM_IG_ACCOUNT",
            platform_metadata={"app_id": "1", "business_account_id": "1784", "dry_run": "true"}))
        engine.registry.save_social_account(SocialAccount(
            category_id="gadgets", language=language, platform="telegram", account_handle=f"@findhub_deals_{language}",
            token_env_var="SIM_TG_TOKEN", platform_metadata={"chat_id": "-100123"}))
    return ContentReview(engine)


def media_file(review: ContentReview, name: str) -> Path:
    folder = Path(review.settings.generated_media_dir) / "gadgets"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(PNG)
    return path


def add_asset(review: ContentReview, content_type: str, language: str, *, status: str = "QUEUED",
              image: Path | None = None, text: str | None = None) -> int:
    platform = content_type.split("_")[0]
    product_id = review.db.query("SELECT id FROM products ORDER BY id LIMIT 1")[0]["id"]
    links = {"affiliate_url": "https://s.click.temu.com/e/deal1",
             "storefront_product_url": f"https://findhub.example/{language}/p/{product_id}",
             "storefront_category_url": f"https://findhub.example/{language}/c/gadgets"}
    if content_type in ("instagram_story", "instagram_reel"):
        links["story_links"] = [{"kind": "storefront", "label": "FindHub store", "url": links["storefront_category_url"]},
                                {"kind": "product", "label": "See the deal", "url": links["storefront_product_url"]}]
    account = review.engine.registry.get_social_account("gadgets", language, platform)
    return review.db.insert_generated_asset({
        "product_id": product_id, "category_id": "gadgets", "language": language, "platform": platform,
        "content_type": content_type, "target_account_id": f"gadgets:{language}:{platform}" if account else None,
        "target_account_handle": account.account_handle if account else None,
        "generated_text": text or TEXT[language], "generated_image_path": str(image) if image else None,
        "generated_image_url": f"http://localhost:8000/media/gadgets/{image.name}" if image else None,
        "links_payload_json": json.dumps(links), "llm_model_used": "gemini-2.5-flash", "status": status,
    })


def add_publication(review: ContentReview, content_format: str, platform: str, publisher: str, language: str) -> int:
    product_row = review.db.query("SELECT * FROM products ORDER BY id LIMIT 1")[0]
    product = product_from_row(product_row)
    content = GeneratedContent(product=product, content_type="text", content_format=content_format, language=language,
                               caption=TEXT[language].split("\n")[0], hashtags=["gadgets"], model_used="mock")
    page = SocialPage(platform=publisher, page_name="@findhub.gadgets", dry_run=True)
    return review.db.insert_publication(
        pipeline_id=f"gadgets_{language}", product_id=product_row["id"], platform=publisher, category="gadgets",
        language=language, account_platform=platform, target_page=page.page_name, target_json=page.model_dump_json(),
        content=content, status=PublicationStatus.QUEUED, cost_share_usd=0.0)


# --------------------------------------------------------------------------- #
# Rendering: 4 platform skins × Hebrew (RTL) / English (LTR)
# --------------------------------------------------------------------------- #
SKINS = [
    ("instagram_post", "instagram_post", None, ('class="ig-actions"', 'class="ig-tabs"', 'class="t-cta"')),
    ("instagram_story", "instagram_story", None, ('class="story-bars"', 'class="sticker"', "See the deal")),
    ("tiktok_post", "vertical_video", "tiktok", ('class="rail"', 'class="disc', "For You")),
    ("instagram_reel", "vertical_video", "instagram_reel", ('class="rail"', "Reels", 'class="audio"')),
    ("telegram_post", "telegram", None, ("FindHub Deals", 'class="tg-keyboard"', 'class="tg-preview"')),
]


@pytest.mark.parametrize("language", ["he", "en"])
@pytest.mark.parametrize(("content_type", "layout", "variant", "markers"), SKINS)
def test_every_skin_renders_a_375x812_phone_in_the_posts_direction(review, language, content_type, layout, variant, markers):
    image = media_file(review, f"{content_type}_{language}.png")
    asset_id = add_asset(review, content_type, language, image=None if content_type == "telegram_post" else image)
    item = review.item(f"asset:{asset_id}")
    assert (item.layout, item.variant) == (layout, variant)

    document = render_phone(item, media_src=phone_media_source(item, review))

    direction = "rtl" if language == "he" else "ltr"
    assert item.dir == direction
    assert f'data-layout="{layout}"' in document and f'data-dir="{direction}"' in document
    assert f"width:{FRAME_W}px;height:{FRAME_H}px" in document and "width:375px;height:812px" in document
    assert ">9:41<" in document and 'class="island"' in document and 'class="home' in document
    for marker in markers:
        assert marker in document, marker
    if layout != "instagram_story":  # a Story's text is baked into the image
        assert '<div class="rich' in document and f'dir="{direction}"' in document
        assert '<bdi class="t-tag">#FindHub</bdi>' in document  # isolated: never "FindHub#" inside Hebrew
    if layout == "instagram_post" or layout == "vertical_video":
        assert "data:image/png;base64," in document  # the engine's own image is inlined
    chrome = {"he": ("שליחת הודעה", "מעקב", "לדיל ישיר", "סימוני לייק"),
              "en": ("Send message", "Follow", "Direct Deal", "likes")}[language]
    expected = {"instagram_story": chrome[0], "instagram_reel": chrome[1], "telegram_post": chrome[2],
                "instagram_post": chrome[3]}.get(content_type)
    if expected:
        assert expected in document


@pytest.mark.parametrize("language", ["he", "en"])
def test_youtube_shorts_publications_show_the_shorts_skin_and_the_publishers_suffix(review, language):
    publication_id = add_publication(review, "youtube_short", "youtube", "youtube_pub", language)
    item = review.item(f"pub:{publication_id}")
    assert (item.layout, item.variant, item.text_field) == ("vertical_video", "youtube_short", "caption")
    assert item.suffix and "#Shorts" in item.post_text()  # what youtube_pub appends is part of the preview

    document = render_phone(item, item.post_text("Edited caption"))
    assert 'data-variant="youtube_short"' in document and ("Subscribe" if language == "en" else "הרשמה") in document
    assert "Edited caption" in document and "#Shorts" in document


def test_post_text_is_escaped_and_only_http_links_become_anchors(review):
    asset_id = add_asset(review, "telegram_post", "en",
                         text='<script>alert(1)</script> <img src=x onerror=alert(2)> https://findhub.example/en/p/1 '
                              'javascript:alert(3)')
    document = render_phone(review.item(f"asset:{asset_id}"))
    assert "<script>alert" not in document and "<img src=x" not in document
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in document
    assert '<a href="https://findhub.example/en/p/1"' in document
    assert 'href="javascript' not in document


def test_only_the_engines_own_media_files_are_inlined(review, tmp_path):
    inside = media_file(review, "inside.png")
    outside = tmp_path / "outside.png"
    outside.write_bytes(PNG)
    inside_item = review.item(f"asset:{add_asset(review, 'instagram_post', 'he', image=inside)}")
    assert phone_media_source(inside_item, review).startswith("data:image/png;base64,")
    outside_id = add_asset(review, "instagram_post", "he", image=outside)
    outside_item = review.item(f"asset:{outside_id}")
    assert not (phone_media_source(outside_item, review) or "").startswith("data:")


# --------------------------------------------------------------------------- #
# Actions: Save edits, Publish now, Delete
# --------------------------------------------------------------------------- #
def test_save_edits_updates_sqlite_for_assets_reels_and_publications(review):
    post = add_asset(review, "instagram_post", "he", status="DRAFT")
    reel = add_asset(review, "instagram_reel", "he")
    publication = add_publication(review, "youtube_short", "youtube", "youtube_pub", "en")

    assert review.save_text(f"asset:{post}", "  קפשן חדש #FindHub  ").text == "קפשן חדש #FindHub"
    assert review.db.get_generated_asset(post)["generated_text"] == "קפשן חדש #FindHub"
    assert review.db.get_generated_asset(post)["status"] == "DRAFT"

    review.save_text(f"asset:{reel}", "Reel caption")
    reel_row = review.db.get_generated_asset(reel)
    assert json.loads(reel_row["links_payload_json"])["reel_caption"] == "Reel caption"
    assert reel_row["generated_text"] == TEXT["he"]  # the cover text stays

    review.save_text(f"pub:{publication}", "New caption")
    row = review.db.get_publication(publication)
    assert row["caption"] == "New caption" and json.loads(row["content_json"])["caption"] == "New caption"

    with pytest.raises(ReviewError) as blank:
        review.save_text(f"asset:{post}", "   ")
    assert blank.value.status == 422
    with pytest.raises(ReviewError) as missing:
        review.item("asset:999999")
    assert missing.value.status == 404


def test_publish_now_approves_a_draft_and_records_the_receipt(review):
    image = media_file(review, "post.png")
    draft = add_asset(review, "instagram_post", "he", status="DRAFT", image=image)

    result = review.publish(f"asset:{draft}")

    assert result.success and result.status == "PUBLISHED" and result.platform_post_id.startswith("dry-run")
    row = review.db.get_generated_asset(draft)
    assert row["status"] == "PUBLISHED" and row["platform_post_id"] == result.platform_post_id
    with pytest.raises(ReviewError):
        review.save_text(f"asset:{draft}", "too late")
    assert all(item.key != f"asset:{draft}" for item in review.items())  # no longer waiting for review


def test_publish_failures_are_reported_and_recorded(review):
    refused = add_asset(review, "telegram_post", "en")  # its bot token variable is not set
    result = review.publish(f"asset:{refused}")
    assert not result.success and "SIM_TG_TOKEN" in result.message
    assert review.db.get_generated_asset(refused)["status"] == "QUEUED"  # refused before sending: unchanged

    publication = add_publication(review, "youtube_short", "youtube", "youtube_pub", "en")
    review.db._execute("UPDATE publications SET target_json = '{}' WHERE id = ?", (publication,))
    failed = review.publish(f"pub:{publication}")
    assert not failed.success and failed.status == "FAILED" and "Target unavailable" in failed.message
    row = review.db.get_publication(publication)
    assert row["status"] == "FAILED" and row["error_message"]


def test_delete_removes_the_record_and_only_orphaned_images(review):
    shared = media_file(review, "shared.png")
    first = add_asset(review, "instagram_post", "en", image=shared)
    second = add_asset(review, "instagram_story", "en", image=shared)
    publication = add_publication(review, "youtube_short", "youtube", "youtube_pub", "en")

    assert review.delete(f"asset:{first}") == {"deleted": first, "media_removed": False}
    assert shared.exists()  # the Story still uses it
    assert review.delete(f"asset:{second}") == {"deleted": second, "media_removed": True}
    assert not shared.exists()
    assert review.db.get_generated_asset(first) is None and review.db.get_generated_asset(second) is None
    assert review.delete(f"pub:{publication}")["deleted"] == publication
    assert review.db.get_publication(publication) is None


# --------------------------------------------------------------------------- #
# The dashboard's Content Review tab (Streamlit's test harness)
# --------------------------------------------------------------------------- #
AppTest = pytest.importorskip("streamlit.testing.v1").AppTest
APP = Path(__file__).resolve().parents[1] / "dashboard" / "app.py"


def test_content_review_tab_edits_publishes_and_deletes(tmp_path, monkeypatch):
    import streamlit as st

    from core import bootstrap
    from core.config import load_settings

    st.cache_resource.clear()
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "dashboard.db"))
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "logs" / "dashboard.log"))
    monkeypatch.setenv("LOCAL_MEDIA_DIR", str(tmp_path / "media"))
    monkeypatch.setenv("GENERATED_MEDIA_DIR", str(tmp_path / "generated_media"))
    monkeypatch.setenv("SIM_IG_TOKEN", "dummy-token-value")
    monkeypatch.setenv("SIM_IG_ACCOUNT", "1784")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    engine = bootstrap(settings=load_settings(None), console_logging=False)
    engine.harvester.harvest("gadgets", "temu_mock")
    engine.registry.save_social_account(SocialAccount(
        category_id="gadgets", language="he", platform="instagram", account_handle="@findhub_gadgets_he",
        token_env_var="SIM_IG_TOKEN", account_id_env_var="SIM_IG_ACCOUNT",
        platform_metadata={"app_id": "1", "business_account_id": "1784", "dry_run": "true"}))
    review = ContentReview(engine)
    image = media_file(review, "post.png")
    discard = add_asset(review, "instagram_story", "he", image=image)
    post = add_asset(review, "instagram_post", "he", status="DRAFT", image=image)  # newest: shown first

    app = AppTest.from_file(str(APP), default_timeout=120)
    app.query_params["tab"] = "review"
    app.run()
    assert not app.exception
    assert any(tab.label == "Content Review" for tab in app.tabs)
    assert app.selectbox(key="review_pick").value == f"asset:{post}"
    assert app.button(key="review_publish").label == "Approve & Publish Now"

    # Save Caption Edits
    app.text_area(key=f"review_text::asset:{post}").set_value("קפשן מעודכן #FindHub").run()
    assert not app.button(key="review_save").disabled
    app.button(key="review_save").click().run()
    assert engine.db.get_generated_asset(post)["generated_text"] == "קפשן מעודכן #FindHub"
    assert any("Saved the post text" in message.value for message in app.success)

    # Approve & Publish Now → PUBLISHED, then the next item is shown
    app.button(key="review_publish").click().run()
    assert not app.exception
    row = engine.db.get_generated_asset(post)
    assert row["status"] == "PUBLISHED" and row["platform_post_id"].startswith("dry-run")
    assert any("Post id dry-run" in message.value for message in app.success)
    assert app.selectbox(key="review_pick").value == f"asset:{discard}"

    # Delete / Discard → the row and its (now orphaned) image are gone... unless the published post uses it
    app.button(key="review_delete_confirm").click().run()
    assert engine.db.get_generated_asset(discard) is None
    assert image.exists()  # the published post still references this file
    assert any("Nothing is waiting for review" in info.value for info in app.info)
    st.cache_resource.clear()


# --------------------------------------------------------------------------- #
# Content for an account that cannot publish yet (no Instagram token): stored for review, never sent
# --------------------------------------------------------------------------- #
def instagram_without_token(review: ContentReview, monkeypatch) -> None:
    from core.config import ContentProfile

    monkeypatch.delenv("SIM_IG_TOKEN", raising=False)  # the Instagram key was never provided
    review.engine.registry.save_content_profile(ContentProfile(
        content_type="instagram_post", platform="instagram", generator="llm_text_gen", llm_model="mock",
        publish_format="image",
    ))


def test_without_the_option_a_live_run_skips_an_account_without_credentials(review, monkeypatch):
    instagram_without_token(review, monkeypatch)

    report = review.engine.orchestrator.run("gadgets", "he", ["instagram_post"], mode="queued", limit=2)

    assert report.count("stored", "queued") == 0 and review.db.list_publications() == []
    assert any("SIM_IG_TOKEN" in warning for warning in report.warnings)


def test_content_for_an_account_without_credentials_is_stored_for_review_and_never_sent(review, monkeypatch):
    from core.orchestrator import NOT_SENT

    instagram_without_token(review, monkeypatch)

    report = review.engine.orchestrator.run("gadgets", "he", ["instagram_post"], mode="queued", limit=2,
                                            store_unpublishable=True)

    assert report.ok and report.count("stored") == 2 and report.count("queued", "published") == 0
    rows = review.db.list_publications()
    assert len(rows) == 2 and {row["status"] for row in rows} == {"FAILED"}
    assert all(row["error_message"].startswith(NOT_SENT) and "SIM_IG_TOKEN" in row["error_message"] for row in rows)
    review.engine.create_worker().run_once()  # the worker never touches them
    assert {row["status"] for row in review.db.list_publications()} == {"FAILED"}

    items = [item for item in review.items() if item.source == "publication"]
    assert len(items) == 2
    item = items[0]
    assert item.layout == "instagram_post" and item.dir == "rtl" and item.can_edit and item.can_publish
    assert "SIM_IG_TOKEN" in item.not_sent_reason
    assert 'data-layout="instagram_post"' in render_phone(item)
    review.save_text(item.key, "קפשן לבדיקה")
    assert review.db.get_publication(item.id)["caption"] == "קפשן לבדיקה"

    # Once the key exists, Publish Now sends it (the account is a dry-run account here).
    monkeypatch.setenv("SIM_IG_TOKEN", "dummy-token-value")
    result = review.publish(item.key)
    assert result.success and result.platform_post_id.startswith("dry-run"), result.message
    assert review.db.get_publication(item.id)["status"] == "PUBLISHED"


def test_a_dry_run_ignores_the_option_and_writes_nothing(review, monkeypatch):
    instagram_without_token(review, monkeypatch)
    review.engine.orchestrator.run("gadgets", "he", ["instagram_post"], dry_run=True, store_unpublishable=True)
    assert review.db.list_publications() == []


def test_dashboard_run_form_stores_unsendable_instagram_posts_for_content_review(tmp_path, monkeypatch):
    import streamlit as st

    from core import bootstrap
    from core.config import ContentProfile, load_settings

    st.cache_resource.clear()
    for name, value in {"DATABASE_PATH": tmp_path / "dashboard.db", "LOG_FILE": tmp_path / "logs" / "d.log",
                        "LOCAL_MEDIA_DIR": tmp_path / "media", "GENERATED_MEDIA_DIR": tmp_path / "generated_media",
                        "WORKER_ACTIVE_HOURS_START": 0, "WORKER_ACTIVE_HOURS_END": 24}.items():
        monkeypatch.setenv(name, str(value))
    for name in ("DATABASE_URL", "DASHBOARD_PASSWORD", "SIM_IG_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    engine = bootstrap(settings=load_settings(None), console_logging=False)
    engine.harvester.harvest("gadgets", "temu_mock")
    engine.registry.save_content_profile(ContentProfile(
        content_type="instagram_post", platform="instagram", generator="llm_text_gen", llm_model="mock"))
    engine.registry.save_social_account(SocialAccount(
        category_id="gadgets", language="he", platform="instagram", account_handle="@findhub_gadgets_he",
        token_env_var="SIM_IG_TOKEN", account_id_env_var="SIM_IG_ACCOUNT",
        platform_metadata={"app_id": "1", "business_account_id": "1784"}))

    app = AppTest.from_file(str(APP), default_timeout=120).run()
    app.selectbox(key="run_category").set_value("gadgets")
    app.selectbox(key="run_language").set_value("he")
    app.multiselect(key="run_types").set_value(["instagram_post"])
    app.toggle(key="run_dry").set_value(False)
    app.toggle(key="run_store_unpublishable").set_value(True)
    app.button(key="FormSubmitter:run_form-▶ Run pipeline").click().run()

    assert not app.exception
    assert any("stored unsent" in caption.value for caption in app.caption)
    rows = engine.db.list_publications()
    assert rows and {row["status"] for row in rows} == {"FAILED"}
    assert app.selectbox(key="review_pick").value.startswith("pub:")
    assert any("Not sent — stored for review only" in warning.value and "SIM_IG_TOKEN" in warning.value
               for warning in app.warning)
    st.cache_resource.clear()


# --------------------------------------------------------------------------- #
# No account linked at all: created, previewed and stored anyway; published once an account exists
# --------------------------------------------------------------------------- #
def mock_instagram_profile(review: ContentReview) -> None:
    from core.config import ContentProfile

    review.engine.registry.save_content_profile(ContentProfile(
        content_type="instagram_post", platform="instagram", generator="llm_text_gen", llm_model="mock",
        publish_format="image"))


def test_content_for_a_pair_without_an_account_is_previewed_stored_and_published_once_linked(review):
    from core.orchestrator import UNLINKED_PAGE

    review.engine.harvester.harvest("sports", "aliexpress_mock")  # sports/en has no Instagram account
    mock_instagram_profile(review)
    orchestrator = review.engine.orchestrator

    skipped = orchestrator.run("sports", "en", ["instagram_post"], mode="queued", limit=1)
    assert skipped.count("stored") == 0 and any("no instagram account" in w for w in skipped.warnings)

    preview = orchestrator.run("sports", "en", ["instagram_post"], dry_run=True, limit=1, store_unpublishable=True)
    assert preview.count("preview") == 1 and review.db.list_publications() == []
    assert any("Previewing it anyway" in w for w in preview.warnings)

    report = orchestrator.run("sports", "en", ["instagram_post"], mode="queued", limit=1, store_unpublishable=True)
    assert report.ok and report.count("stored") == 1
    row = review.db.list_publications()[0]
    assert row["status"] == "FAILED" and row["target_page"] == UNLINKED_PAGE and "no instagram account" in row["error_message"]

    item = review.item(f"pub:{row['id']}")
    assert item.layout == "instagram_post" and item.dir == "ltr" and item.target_account is None
    assert item.handle == "@findhub" and not item.can_publish and "Social Accounts" in item.publish_blocked_reason
    refused = review.publish(item.key)
    assert not refused.success and review.db.get_publication(row["id"])["status"] == "FAILED"

    review.engine.registry.save_social_account(SocialAccount(
        category_id="sports", language="en", platform="instagram", account_handle="@sports_en",
        token_env_var="SIM_IG_TOKEN", account_id_env_var="SIM_IG_ACCOUNT",
        platform_metadata={"app_id": "1", "business_account_id": "1784", "dry_run": "true"}))
    assert review.item(item.key).can_publish
    result = review.publish(item.key)
    assert result.success, result.message
    published = review.db.get_publication(row["id"])
    assert published["status"] == "PUBLISHED" and published["target_page"] == "@sports_en"


def test_without_content_types_the_option_covers_every_enabled_type_and_any_language(review):
    review.engine.harvester.harvest("sports", "aliexpress_mock")
    mock_instagram_profile(review)
    assert ("sports", "en") in review.engine.orchestrator.plan("sports", include_unlinked=True)
    assert ("sports", "en") not in review.engine.orchestrator.plan("sports")
    report = review.engine.orchestrator.run("sports", "en", dry_run=True, limit=1, store_unpublishable=True)
    assert {route.content_type for route in report.routes} >= {"instagram_post", "telegram_post"}


def test_review_lists_the_newest_posts_first_across_assets_and_publications(review):
    asset = add_asset(review, "instagram_post", "he")
    review.db._execute("UPDATE generated_assets SET created_at = '2026-01-01T00:00:00+00:00' WHERE id = ?", (asset,))
    publication = add_publication(review, "youtube_short", "youtube", "youtube_pub", "en")
    assert [item.key for item in review.items()][:2] == [f"pub:{publication}", f"asset:{asset}"]


def test_after_a_run_content_review_opens_the_new_posts_of_its_category(tmp_path, monkeypatch):
    import streamlit as st

    from core import bootstrap
    from core.config import ContentProfile, load_settings

    st.cache_resource.clear()
    for name, value in {"DATABASE_PATH": tmp_path / "dashboard.db", "LOG_FILE": tmp_path / "logs" / "d.log",
                        "LOCAL_MEDIA_DIR": tmp_path / "media", "GENERATED_MEDIA_DIR": tmp_path / "generated_media"}.items():
        monkeypatch.setenv(name, str(value))
    for name in ("DATABASE_URL", "DASHBOARD_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    engine = bootstrap(settings=load_settings(None), console_logging=False)
    engine.harvester.harvest("gadgets", "temu_mock")
    engine.harvester.harvest("sports", "aliexpress_mock")
    review = ContentReview(engine)
    for _ in range(3):  # older gadgets content already waiting
        add_asset(review, "instagram_post", "he")
    for content_type in ("instagram_post", "telegram_post"):
        engine.registry.save_content_profile(ContentProfile(
            content_type=content_type, platform=content_type.split("_")[0], generator="llm_text_gen", llm_model="mock"))

    app = AppTest.from_file(str(APP), default_timeout=120).run()
    app.selectbox(key="review_category").set_value("gadgets").run()  # an earlier filter must not hide the run
    app.selectbox(key="run_category").set_value("sports")
    app.selectbox(key="run_language").set_value("en")  # sports/en: no Instagram and no Telegram account
    app.multiselect(key="run_types").set_value(["instagram_post", "telegram_post"])
    app.toggle(key="run_dry").set_value(False)
    app.toggle(key="run_store_unpublishable").set_value(True)
    app.button(key="FormSubmitter:run_form-▶ Run pipeline").click().run()

    assert not app.exception
    rows = engine.db.list_publications()
    assert {row["account_platform"] for row in rows} == {"instagram", "telegram"}
    assert app.selectbox(key="review_category").value == "sports"
    assert app.selectbox(key="review_pick").value == f"pub:{max(row['id'] for row in rows)}"
    assert any("from the last run" in info.value for info in app.info)
    assert any("no telegram account" in w.value or "no instagram account" in w.value for w in app.warning)
    st.cache_resource.clear()
