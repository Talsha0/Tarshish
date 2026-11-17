"""Instagram API with Unified Login: feed posts, Reels, Stories, Meta error hints and the live verification check.

No network: ``graph.facebook.com/v21.0`` is replaced by an ``httpx.MockTransport`` that records every request.
"""

import logging
from urllib.parse import parse_qsl

import httpx
import pytest
from pydantic import SecretStr

from core.config import SocialPage
from core.interfaces import PluginContext
from core.models import Product, PublishTarget
from core.resilience import ConfigError, NonRetryableError
from plugins.generators.llm_text_gen import LLMTextGenerator
from plugins.publishers.instagram_pub import InstagramPublisher, error_hint, verify_connection

TOKEN = "EAAGm0PX4ZCpsBAunifiedlogin0123456789ZD"
ACCOUNT = "17841400000000000"
IMAGE = "https://img.kwcdn.com/product/power-bank.jpg"
VIDEO = "https://video.kwcdn.com/product/power-bank.mp4"
BOTH_SCOPES = ["instagram_business_basic", "instagram_business_content_publish"]


class Graph:
    """A stand-in for graph.facebook.com/v21.0."""

    def __init__(self, *statuses: str, account_type: str = "BUSINESS", scopes=BOTH_SCOPES) -> None:
        self.calls: list[httpx.Request] = []
        self.statuses = list(statuses) or ["FINISHED"]
        self.account_type = account_type
        self.scopes = list(scopes)
        self.replies: dict[str, list[httpx.Response]] = {}  # queued replies per endpoint

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        assert request.url.host == "graph.facebook.com" and request.url.path.startswith("/v21.0/")
        endpoint = request.url.path.rsplit("/", 1)[-1]
        if self.replies.get(endpoint):
            return self.replies[endpoint].pop(0)
        if endpoint == "media":
            return httpx.Response(200, json={"id": "17890000000000001"})
        if endpoint == "media_publish":
            return httpx.Response(200, json={"id": "18000000000000001"})
        if endpoint == "debug_token":
            return httpx.Response(200, json={"data": {
                "app_id": "1234", "type": "SYSTEM_USER", "is_valid": True, "expires_at": 0, "scopes": self.scopes,
            }})
        if endpoint == "permissions":
            return httpx.Response(200, json={"data": [{"permission": s, "status": "granted"} for s in self.scopes]})
        if endpoint == ACCOUNT:
            return httpx.Response(200, json={"id": ACCOUNT, "username": "findhub_gadgets", "account_type": self.account_type})
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return httpx.Response(200, json={"status_code": status, "id": endpoint})

    def form(self, index: int) -> dict[str, str]:
        return dict(parse_qsl(self.calls[index].content.decode()))

    def paths(self) -> list[tuple[str, str]]:
        return [(request.method, request.url.path) for request in self.calls]


def publisher(engine, graph, **options) -> InstagramPublisher:
    target = PublishTarget(
        platform="instagram_pub", page_name="gadgets", options=options,
        credentials={"token": SecretStr(TOKEN), "account_id": SecretStr(ACCOUNT)},
    )
    instance = InstagramPublisher(PluginContext(settings=engine.settings), target)
    instance._transport = httpx.MockTransport(graph)
    return instance


@pytest.fixture
def content(engine):
    product = Product(
        external_id="601099480276615", source="temu", category="gadgets", currency="USD",
        title="Magnetic Wireless Power Bank 10000mAh", price=19.90, rating=4.8, orders_count=3680,
        raw_url="https://www.temu.com/--g-601099480276615.html",
        affiliate_url="https://www.temu.com/--g-601099480276615.html?_x_sessn_id=t1",
        image_urls=[IMAGE], video_url=VIDEO,
    )
    return LLMTextGenerator(PluginContext(settings=engine.settings, llm_model="mock")).generate(product, "en", None)


def assert_token_only_in_header(graph: Graph) -> None:
    for request in graph.calls:
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        assert TOKEN not in request.content.decode()


# -- 1. Feed image post ------------------------------------------------------------------ #
def test_feed_image_post_creates_a_container_then_publishes_it(engine, content):
    graph = Graph()
    receipt = publisher(engine, graph, content_type="post").publish(content)

    assert receipt.success and receipt.platform_post_id == "18000000000000001"
    assert graph.paths() == [
        ("POST", f"/v21.0/{ACCOUNT}/media"),
        ("GET", "/v21.0/17890000000000001"),  # status_code check: instant for images
        ("POST", f"/v21.0/{ACCOUNT}/media_publish"),
    ]
    create = graph.form(0)
    assert set(create) == {"image_url", "caption"} and create["image_url"] == IMAGE
    assert content.caption in create["caption"] and "🔗 Link in bio -> /c/gadgets" in create["caption"]
    assert all(f"#{tag}" in create["caption"] for tag in content.hashtags)
    assert graph.calls[1].url.params["fields"] == "status_code,status"
    assert graph.form(2) == {"creation_id": "17890000000000001"}
    assert_token_only_in_header(graph)


def test_reel_polls_with_exponential_backoff_before_publishing(engine, content, clean_environment):
    graph = Graph("IN_PROGRESS", "IN_PROGRESS", "FINISHED")
    receipt = publisher(engine, graph, content_type="reel").publish(content)

    assert receipt.success and graph.form(0) == {
        "media_type": "REELS", "video_url": VIDEO, "caption": graph.form(0)["caption"],
    }
    assert [method for method, _ in graph.paths()] == ["POST", "GET", "GET", "GET", "POST"]
    first, second = clean_environment
    assert second > first  # the wait grows between polls

    with pytest.raises(NonRetryableError, match="no public video URL"):  # a Reels page never posts a still image
        no_video = content.product.model_copy(update={"video_url": None})
        publisher(engine, Graph(), content_type="reel").publish(content.model_copy(update={"product": no_video}))


# -- 2. Stories -------------------------------------------------------------------------- #
def test_story_image_sends_no_caption_and_reminds_to_bake_text_in(engine, content, caplog):
    caplog.set_level(logging.INFO, logger="tarshish")
    graph = Graph()
    receipt = publisher(engine, graph, content_type="story").publish(content)

    assert receipt.success and receipt.platform_post_id == "18000000000000001"
    assert graph.form(0) == {"media_type": "STORIES", "image_url": IMAGE}  # no caption field: Meta rejects it
    assert graph.paths()[-1] == ("POST", f"/v21.0/{ACCOUNT}/media_publish")
    assert graph.form(len(graph.calls) - 1) == {"creation_id": "17890000000000001"}
    assert "Stories take no caption" in caplog.text
    assert_token_only_in_header(graph)


def test_story_video_waits_for_processing_then_publishes(engine, content, clean_environment):
    graph = Graph("IN_PROGRESS", "FINISHED")
    video_content = content.model_copy(update={"content_type": "video"})
    receipt = publisher(engine, graph, content_type="story").publish(video_content)

    assert receipt.success
    assert graph.form(0) == {"media_type": "STORIES", "video_url": VIDEO}
    assert graph.paths() == [
        ("POST", f"/v21.0/{ACCOUNT}/media"), ("GET", "/v21.0/17890000000000001"),
        ("GET", "/v21.0/17890000000000001"), ("POST", f"/v21.0/{ACCOUNT}/media_publish"),
    ]
    assert len(clean_environment) == 1  # one backoff wait while Meta transcoded the video


def test_story_dry_run_logs_the_payload_without_a_caption_field(engine, content, caplog):
    caplog.set_level(logging.INFO, logger="tarshish")
    graph = Graph()
    story = publisher(engine, graph, content_type="story", dry_run=True)
    assert story.publish(content).platform_post_id.startswith("dry-run-")
    assert "Instagram Story simulated (dry-run)" in caplog.text and IMAGE in caplog.text
    assert "Stories take no caption" in caplog.text and not graph.calls


def test_page_format_option_is_validated(engine, monkeypatch):
    with pytest.raises(ConfigError, match="carousel"):
        publisher(engine, Graph(), content_type="carousel")

    monkeypatch.setenv("INSTAGRAM_ACCESS_TOKEN", TOKEN)
    monkeypatch.setenv("INSTAGRAM_BUSINESS_ACCOUNT_ID", ACCOUNT)
    page = SocialPage.model_validate({"platform": "instagram_pub", "page_name": "gadgets_story", "content_type": "Story"})
    target, _ = engine.registry.resolve_target(page)
    assert target.options == {"content_type": "Story"}
    built, reason = engine.registry.build_publisher(page, PluginContext(settings=engine.settings))
    assert reason is None and built.format_for(None) == "story"  # type: ignore[arg-type]
    bad = page.model_copy(update={"content_type": "carousel"})
    built, reason = engine.registry.build_publisher(SocialPage.model_validate(bad.model_dump()), PluginContext(settings=engine.settings))
    assert built is None and "content_type" in reason  # skipped with a reason, other targets unaffected


# -- Error handling ------------------------------------------------------------------------ #
@pytest.mark.parametrize("error, expected", [
    ({"message": f"Error validating access token {TOKEN}: Session has expired", "code": 190, "error_subcode": 463},
     ["code 190/463", "has expired", "INSTAGRAM_ACCESS_TOKEN"]),
    ({"message": "(#10) Application does not have permission for this action", "code": 10},
     ["code 10", "instagram_business_content_publish", "instagram_business_basic"]),
    ({"message": "Media download has failed.", "code": 9004, "error_subcode": 2207052,
      "error_user_msg": "The media could not be fetched from this URI"},
     ["code 9004/2207052", "could not be fetched", "public HTTPS URL"]),
    ({"message": "Unsupported get request.", "code": 100, "error_subcode": 33},
     ["INSTAGRAM_BUSINESS_ACCOUNT_ID"]),
])
def test_graph_errors_become_actionable_messages(engine, content, error, expected):
    graph = Graph()
    graph.replies["media"] = [httpx.Response(400, json={"error": {"type": "OAuthException", **error}})]
    with pytest.raises(NonRetryableError) as excinfo:
        publisher(engine, graph).publish(content)
    message = str(excinfo.value)
    assert all(part in message for part in expected), message
    assert TOKEN not in message and len(graph.calls) == 1  # fatal: no retry, no publish


def test_publishing_limit_and_failed_containers_are_final(engine, content):
    graph = Graph()
    graph.replies["media"] = [httpx.Response(400, json={"error": {
        "message": "Application request limit reached", "code": 4, "error_subcode": 2207042,
    }})]
    with pytest.raises(NonRetryableError, match="24-hour publishing limit"):
        publisher(engine, graph).publish(content)
    assert len(graph.calls) == 1

    graph = Graph()
    graph.replies["17890000000000001"] = [httpx.Response(200, json={
        "status_code": "ERROR", "status": "Error: Media upload has failed with error code 2207026",
    })]
    with pytest.raises(NonRetryableError, match="video format is not supported"):
        publisher(engine, graph, content_type="reel").publish(content)
    assert ("POST", f"/v21.0/{ACCOUNT}/media_publish") not in graph.paths()
    assert error_hint(190, 460) and "password" in error_hint(190, 460)


# -- 3. Live verification ------------------------------------------------------------------ #
def test_verify_connection_reports_account_and_permissions_without_posting(engine):
    graph = Graph()
    result = verify_connection(engine.settings, TOKEN, ACCOUNT, transport=httpx.MockTransport(graph))

    assert result.ok and result.token_valid and result.account_type == "BUSINESS"
    assert result.summary() == (
        "Connected as @findhub_gadgets | BUSINESS | "
        "Permissions: ✓ instagram_business_basic, ✓ instagram_business_content_publish"
    )
    assert "never" in result.token_expires
    assert {request.method for request in graph.calls} == {"GET"}  # read-only
    lookup = graph.calls[0]
    assert lookup.url.path == f"/v21.0/{ACCOUNT}" and lookup.url.params["fields"] == "id,username,account_type"
    assert graph.calls[1].url.path == "/v21.0/debug_token"
    assert all(request.headers["authorization"] == f"Bearer {TOKEN}" for request in graph.calls)
    assert TOKEN not in "\n".join(result.lines())


def test_verify_connection_flags_missing_permissions_and_personal_accounts(engine):
    graph = Graph(account_type="MEDIA_CREATOR", scopes=["instagram_business_basic"])
    result = verify_connection(engine.settings, TOKEN, ACCOUNT, transport=httpx.MockTransport(graph))
    assert not result.ok and result.account_ok and result.granted == {
        "instagram_business_basic": True, "instagram_business_content_publish": False,
    }
    assert "✗ instagram_business_content_publish" in result.summary()
    assert any("missing permission(s): instagram_business_content_publish" in p for p in result.problems)

    personal = verify_connection(engine.settings, TOKEN, ACCOUNT, transport=httpx.MockTransport(Graph(account_type="PERSONAL")))
    assert not personal.ok and any("Professional account" in p for p in personal.problems)


def test_verify_connection_reports_an_expired_token(engine):
    graph = Graph()
    expired = {"error": {"message": "Error validating access token: Session has expired", "code": 190, "error_subcode": 463}}
    graph.replies[ACCOUNT] = [httpx.Response(400, json=expired)]
    graph.replies["debug_token"] = [httpx.Response(200, json={"data": {
        "is_valid": False, "scopes": [], "error": {"message": "Session has expired", "code": 190},
    }})]
    result = verify_connection(engine.settings, TOKEN, ACCOUNT, transport=httpx.MockTransport(graph))
    assert not result.ok and not result.token_valid and result.username is None
    assert "Not connected" in result.summary()
    assert any("has expired" in p and "INSTAGRAM_ACCESS_TOKEN" in p for p in result.problems)


def test_verify_connection_falls_back_to_me_permissions_and_older_fields(engine):
    graph = Graph()
    graph.replies[ACCOUNT] = [httpx.Response(400, json={"error": {
        "message": "(#100) Tried accessing nonexisting field (account_type) on node type (ShadowIGUser)", "code": 100,
    }})]
    graph.replies["debug_token"] = [httpx.Response(400, json={"error": {"message": "(#100) not allowed", "code": 100}})]
    result = verify_connection(engine.settings, TOKEN, ACCOUNT, transport=httpx.MockTransport(graph))
    assert result.ok and result.account_type == "BUSINESS"
    assert graph.calls[1].url.params["fields"] == "id,username"
    assert graph.paths()[-1] == ("GET", "/v21.0/me/permissions")

    unconfigured = verify_connection(engine.settings, None, ACCOUNT)
    assert not unconfigured.ok and "INSTAGRAM_ACCESS_TOKEN" in unconfigured.problems[0]


def test_cli_verify_instagram(make_settings, monkeypatch, capsys):
    import main

    settings = make_settings()
    for name, value in {
        "DATABASE_PATH": settings.database_path, "LOG_FILE": settings.log_file, "LOCAL_MEDIA_DIR": settings.local_media_dir,
    }.items():
        monkeypatch.setenv(name, str(value))
    graph = Graph()
    monkeypatch.setattr(InstagramPublisher, "_transport", httpx.MockTransport(graph))

    assert main.main(["--verify-instagram"]) == 1  # no credentials in .env
    out = capsys.readouterr().out
    assert "not configured" in out and "INSTAGRAM_ACCESS_TOKEN" in out and not graph.calls

    monkeypatch.setenv("INSTAGRAM_ACCESS_TOKEN", TOKEN)
    monkeypatch.setenv("INSTAGRAM_BUSINESS_ACCOUNT_ID", ACCOUNT)
    assert main.main(["--verify-instagram", "--pipeline", "gadgets_he"]) == 0
    out = capsys.readouterr().out
    assert "Connected as @findhub_gadgets | BUSINESS | Permissions: ✓ instagram_business_basic" in out
    assert "Ready for live publishing" in out and TOKEN not in out
    assert {request.method for request in graph.calls} == {"GET"}

    assert main.main(["--verify-instagram", "--pipeline", "sports_he"]) == 1  # no Instagram page there
    assert "no instagram_pub social page" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main.main(["--verify-instagram", "--dry-run"])


def test_dashboard_test_instagram_connection_button(tmp_path, monkeypatch):
    AppTest = pytest.importorskip("streamlit.testing.v1").AppTest
    import streamlit as st
    from pathlib import Path

    st.cache_resource.clear()
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "dashboard.db"))
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "logs" / "dashboard.log"))
    monkeypatch.setenv("LOCAL_MEDIA_DIR", str(tmp_path / "media"))
    monkeypatch.setenv("INSTAGRAM_ACCESS_TOKEN", TOKEN)
    monkeypatch.setenv("INSTAGRAM_BUSINESS_ACCOUNT_ID", ACCOUNT)
    graph = Graph()
    monkeypatch.setattr(InstagramPublisher, "_transport", httpx.MockTransport(graph))
    try:
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "dashboard" / "app.py"), default_timeout=120).run()
        assert not app.exception and not graph.calls  # nothing is called until the button is pressed
        app.button(key="verify_instagram").click().run()
        assert not app.exception
        assert any("Connected as @findhub_gadgets" in message.value for message in app.success)
        assert {request.method for request in graph.calls} == {"GET"}
    finally:
        st.cache_resource.clear()
