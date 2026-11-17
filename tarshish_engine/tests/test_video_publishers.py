"""YouTube Shorts (Data API v3, resumable upload) and TikTok (Content Posting API, Direct Post).

No network: Google's endpoints are served by a recording ``googleapiclient.http.HttpMockSequence``
(token refresh, resumable session, chunk PUTs) and TikTok's by an ``httpx.MockTransport``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from googleapiclient.http import HttpMockSequence
from pydantic import SecretStr

from core.config import ContentProfile
from core.interfaces import PluginContext
from core.models import GeneratedContent, Product, PublishTarget
from core.resilience import ConfigError, NonRetryableError, redact
from plugins.publishers import _video, tiktok_pub, youtube_pub
from plugins.publishers._video import VideoInfo
from plugins.publishers.tiktok_pub import TikTokPublisher, plan_chunks
from plugins.publishers.youtube_pub import (
    INSERT_COST_UNITS, UPLOAD_SCOPE, YouTubeShortsPublisher, quota_ledger, shorts_title,
)

CLIENT_ID = "123456789012-abcdefghijklmnop.apps.googleusercontent.com"
CLIENT_SECRET = "GOCSPX-unitTestClientSecret0123"
REFRESH = "1//0gUnitTestRefreshToken-abcdefghijklmnopqrstuvwxyz"
ACCESS = "ya29.a0UnitTestAccessToken0123456789"
TIKTOK_TOKEN = "act.unitTestTikTokAccessToken0123456789abcdef"
TIKTOK_REFRESH = "rft.unitTestTikTokRefreshToken0123456789abcdef"
UPLOAD_URL = "https://open-upload.tiktokapis.com/video/?upload_id=7300000000000000001&upload_token=signed-xyz"
VERTICAL = VideoInfo(width=1080, height=1920, duration=12.0, codec="h264")


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    quota_ledger.reset()
    tiktok_pub.rate_limiter.reset()
    for name in ("YOUTUBE_PRIVACY_STATUS", "YOUTUBE_CATEGORY_ID", "YOUTUBE_DAILY_QUOTA_UNITS",
                 "TIKTOK_PRIVACY_LEVEL", "TIKTOK_COMMERCIAL_CONTENT"):
        monkeypatch.delenv(name, raising=False)
    # Dummy bytes stand in for the MP4: report the 1080x1920 / 12 s the generator renders.
    monkeypatch.setattr(youtube_pub, "probe_video", lambda path: VERTICAL)
    monkeypatch.setattr(tiktok_pub, "probe_video", lambda path: VERTICAL)
    yield
    YouTubeShortsPublisher._http_factory = None
    TikTokPublisher._transport = None


@pytest.fixture
def video_file(tmp_path):
    path = tmp_path / "short.mp4"
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 1200)  # ~300 KB
    return path


@pytest.fixture
def content(video_file):
    product = Product(
        external_id="601099480276615", source="temu", category="gadgets", currency="USD",
        title="Magnetic Wireless Power Bank 10000mAh", price=19.90, rating=4.8, orders_count=3680,
        raw_url="https://www.temu.com/--g-601099480276615.html",
        affiliate_url="https://www.temu.com/--g-601099480276615.html?_x_sessn_id=aff",
        image_urls=["https://img.kwcdn.com/product/power-bank.jpg"],
    )
    return GeneratedContent(
        product=product, content_type="video", content_format="youtube_short", language="en",
        caption="This power bank snaps on and charges anything ⚡ only $19.90 — grab it before it's gone!",
        hashtags=["gadgets", "tech deals", "powerbank"], media_local_path=str(video_file), model_used="mock",
    )


# --------------------------------------------------------------------------- #
# YouTube Shorts
# --------------------------------------------------------------------------- #
class RecordingHttp(HttpMockSequence):
    """HttpMockSequence that remembers every request (uri, method, headers, body)."""

    def __init__(self, responses):
        super().__init__(responses)
        self.requests: list[dict] = []
        self.timeout = None

    def request(self, uri, method="GET", body=None, headers=None, redirections=1, connection_type=None, **kwargs):
        text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
        self.requests.append({"uri": uri, "method": method, "body": text,
                              "headers": {str(k).lower(): v for k, v in (headers or {}).items()}})
        return super().request(uri, method, body, headers, redirections, connection_type)


def token_reply(scope: str = UPLOAD_SCOPE):
    return ({"status": "200"}, json.dumps({"access_token": ACCESS, "expires_in": 3599, "token_type": "Bearer",
                                           "scope": scope}))


def google_error(status: int, reason: str, message: str = "error"):
    return ({"status": str(status)}, json.dumps({"error": {"code": status, "message": message,
                                                           "errors": [{"reason": reason, "message": message}]}}))


def youtube(engine, http, **options) -> YouTubeShortsPublisher:
    YouTubeShortsPublisher._http_factory = lambda: http
    target = PublishTarget(
        platform="youtube_pub", page_name="@gadgets.en", options=options,
        credentials={"token": SecretStr(REFRESH), "client_id": SecretStr(CLIENT_ID),
                     "client_secret": SecretStr(CLIENT_SECRET)},
    )
    return YouTubeShortsPublisher(PluginContext(settings=engine.settings), target)


def test_youtube_resumable_upload_sends_shorts_metadata_in_chunks(engine, content, monkeypatch, video_file):
    monkeypatch.setenv("YOUTUBE_PRIVACY_STATUS", "unlisted")
    size = video_file.stat().st_size
    chunk = 256 * 1024
    http = RecordingHttp([
        token_reply(),
        ({"status": "200", "location": "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable"
                                       "&upload_id=UPLOAD123"}, ""),
        ({"status": "308", "range": f"bytes=0-{chunk - 1}"}, ""),
        ({"status": "200"}, json.dumps({"id": "dQw4w9WgXcQ", "status": {"uploadStatus": "uploaded",
                                                                       "privacyStatus": "unlisted"}})),
    ])
    publisher = youtube(engine, http)
    publisher.chunk_size = chunk

    receipt = publisher.publish(content)

    assert receipt.success and receipt.platform == "youtube" and receipt.platform_post_id == "dQw4w9WgXcQ"
    token, init, first, last = http.requests
    assert token["uri"] == "https://oauth2.googleapis.com/token" and "refresh_token" in token["body"]
    url = urlparse(init["uri"])
    query = parse_qs(url.query)
    assert url.path == "/upload/youtube/v3/videos" and query["uploadType"] == ["resumable"]
    assert query["part"] == ["snippet,status"] and init["headers"]["authorization"] == f"Bearer {ACCESS}"
    assert init["headers"]["x-upload-content-type"] == "video/mp4"
    body = json.loads(init["body"])
    assert body["snippet"]["title"].endswith("#Shorts") and len(body["snippet"]["title"]) <= 100
    assert body["snippet"]["description"].rstrip().endswith("#Shorts")
    assert content.product.affiliate_url in body["snippet"]["description"]
    assert body["snippet"]["categoryId"] == "28"                         # gadgets ➔ Science & Technology
    assert body["snippet"]["tags"][-1] == "Shorts" and "tech deals" in body["snippet"]["tags"]
    assert body["status"] == {"privacyStatus": "unlisted", "selfDeclaredMadeForKids": False, "embeddable": True}
    assert first["method"] == last["method"] == "PUT" and "upload_id=UPLOAD123" in first["uri"]
    assert first["headers"]["content-range"] == f"bytes 0-{chunk - 1}/{size}"
    assert last["headers"]["content-range"] == f"bytes {chunk}-{size - 1}/{size}"
    assert quota_ledger.units(CLIENT_ID) == INSERT_COST_UNITS


def test_youtube_category_falls_back_to_people_and_blogs_and_can_be_overridden(engine, content, monkeypatch):
    sports = content.model_copy(update={"product": content.product.model_copy(update={"category": "sports"})})
    assert youtube(engine, RecordingHttp([])).build_metadata(sports)["snippet"]["categoryId"] == "22"
    monkeypatch.setenv("YOUTUBE_CATEGORY_ID", "17")
    assert youtube(engine, RecordingHttp([])).build_metadata(content)["snippet"]["categoryId"] == "17"
    monkeypatch.setenv("YOUTUBE_PRIVACY_STATUS", "friends-only")
    with pytest.raises(ConfigError, match="YOUTUBE_PRIVACY_STATUS"):
        youtube(engine, RecordingHttp([]))


def test_shorts_title_keeps_the_hashtag_within_100_characters():
    title = shorts_title("A" * 140 + "\nsecond line")
    assert len(title) <= 100 and title.endswith(" #Shorts") and "…" in title
    assert shorts_title("Already tagged #shorts <b>") == "Already tagged #shorts ‹b›"
    assert shorts_title("\n\n") == "Deal of the day #Shorts"


def test_youtube_quota_exceeded_is_actionable_and_stops_later_uploads(engine, content, caplog):
    http = RecordingHttp([token_reply(), google_error(403, "quotaExceeded", "The request cannot be completed.")])
    publisher = youtube(engine, http)

    with pytest.raises(NonRetryableError) as raised:
        publisher.publish(content)
    message = str(raised.value)
    assert "quotaExceeded" in message and "1600 units" in message and "midnight Pacific" in message
    assert len(http.requests) == 2                                       # not retried

    with pytest.raises(NonRetryableError, match="quota is exhausted until"):
        publisher.publish(content)
    assert len(http.requests) == 2                                       # no call was spent on it


def test_youtube_local_quota_budget_refuses_the_seventh_upload(engine, content):
    for _ in range(6):
        quota_ledger.charge(CLIENT_ID, INSERT_COST_UNITS)
    with pytest.raises(NonRetryableError, match="9600 of 10000 quota units"):
        youtube(engine, RecordingHttp([])).publish(content)


def test_youtube_token_refresh_failure_is_graceful_and_never_leaks_the_secret(engine, content):
    http = RecordingHttp([({"status": "400"}, json.dumps({"error": "invalid_grant",
                                                          "error_description": "Token has been expired or revoked."}))])
    with pytest.raises(NonRetryableError) as raised:
        youtube(engine, http).publish(content)
    message = str(raised.value)
    assert "invalid_grant" in message and "Testing" in message and "7 days" in message
    assert REFRESH not in message and CLIENT_SECRET not in message
    assert REFRESH not in redact(f"refresh_token={REFRESH}")


def test_youtube_server_errors_resume_the_upload(engine, content, clean_environment):
    http = RecordingHttp([
        token_reply(),
        ({"status": "200", "location": "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=U1"}, ""),
        google_error(503, "backendError"),
        ({"status": "308", "range": "bytes=0-1023"}, ""),                # "how much arrived?" → resume after it
        ({"status": "201"}, json.dumps({"id": "abc123XYZ00", "status": {"privacyStatus": "public"}})),
    ])
    receipt = youtube(engine, http).publish(content)
    assert receipt.platform_post_id == "abc123XYZ00" and clean_environment  # a backoff was slept
    assert http.requests[3]["headers"]["content-range"].startswith("bytes */")


@pytest.mark.parametrize("info, problem", [
    (VideoInfo(1920, 1080, 12.0), "not vertical 9:16"),
    (VideoInfo(1080, 1920, 75.0), "longer than 60s"),
])
def test_youtube_refuses_clips_that_are_not_shorts(engine, content, monkeypatch, info, problem):
    monkeypatch.setattr(youtube_pub, "probe_video", lambda path: info)
    http = RecordingHttp([])
    with pytest.raises(NonRetryableError, match=problem):
        youtube(engine, http).publish(content)
    assert http.requests == []                                           # refused before any upload


def test_youtube_needs_a_rendered_video(engine, content):
    pending = content.model_copy(update={"media_local_path": None})
    with pytest.raises(NonRetryableError, match="VIDEO_RENDER_ENABLED=true"):
        youtube(engine, RecordingHttp([])).publish(pending)
    text = content.model_copy(update={"content_type": "text"})
    with pytest.raises(NonRetryableError, match="ffmpeg_video_gen"):
        youtube(engine, RecordingHttp([])).publish(text)


def test_youtube_downloads_a_stored_copy_before_uploading(engine, content):
    remote = content.model_copy(update={"media_local_path": None,
                                        "media_public_url": "https://cdn.example.com/media/short.mp4"})
    _video.download_transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"\x00" * 2048))
    try:
        http = RecordingHttp([
            token_reply(),
            ({"status": "200", "location": "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=U2"}, ""),
            ({"status": "200"}, json.dumps({"id": "remoteVid01"})),
        ])
        assert youtube(engine, http).publish(remote).platform_post_id == "remoteVid01"
        assert http.requests[2]["headers"]["content-range"] == "bytes 0-2047/2048"
    finally:
        _video.download_transport = None


def test_youtube_dry_run_uploads_nothing(engine, content, caplog):
    http = RecordingHttp([])
    receipt = youtube(engine, http, dry_run=True).publish(content)
    assert receipt.success and receipt.platform_post_id.startswith("dry-run-") and http.requests == []
    assert "#Shorts" in caplog.text and "nothing was uploaded" in caplog.text


def test_verify_youtube_checks_refresh_scope_and_channel(engine):
    http = RecordingHttp([
        token_reply(),
        ({"status": "200"}, json.dumps({"scope": UPLOAD_SCOPE, "expires_in": 3599})),
        google_error(403, "insufficientPermissions", "Request had insufficient authentication scopes."),
    ])
    result = youtube_pub.verify_connection(engine.settings, CLIENT_ID, CLIENT_SECRET, REFRESH, page_name="@gadgets.en",
                                           http_factory=lambda: http)
    assert result.ok and result.token_valid and result.upload_scope
    text = "\n".join(result.lines())
    assert "6 Shorts a day" in text and "upload-only token" in text and "PRIVATE" in text
    assert ACCESS not in http.requests[1]["uri"]                         # the token travels in the POST body

    missing = youtube_pub.verify_connection(engine.settings, CLIENT_ID, None, None)
    assert not missing.ok and "YOUTUBE_CLIENT_SECRET and YOUTUBE_REFRESH_TOKEN missing" in missing.problems[0]


def test_verify_youtube_flags_a_token_without_the_upload_scope(engine):
    http = RecordingHttp([
        token_reply("https://www.googleapis.com/auth/youtube.readonly"),
        ({"status": "200"}, json.dumps({"scope": "https://www.googleapis.com/auth/youtube.readonly"})),
        ({"status": "200"}, json.dumps({"items": [{"id": "UC123", "snippet": {"title": "FindHub Gadgets"}}]})),
    ])
    result = youtube_pub.verify_connection(engine.settings, CLIENT_ID, CLIENT_SECRET, REFRESH, http_factory=lambda: http)
    assert not result.ok and result.upload_scope is False and result.channel_title == "FindHub Gadgets"
    assert "youtube.upload" in result.problems[0]


# --------------------------------------------------------------------------- #
# TikTok Direct Post
# --------------------------------------------------------------------------- #
class TikTokApi:
    """A stand-in for open.tiktokapis.com and the upload host."""

    def __init__(self, statuses=("PROCESSING_UPLOAD", "PUBLISH_COMPLETE"),
                 privacy=("PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "SELF_ONLY")) -> None:
        self.calls: list[httpx.Request] = []
        self.statuses = list(statuses)
        self.privacy = list(privacy)
        self.replies: dict[str, list[httpx.Response]] = {}

    @staticmethod
    def ok(data):
        return httpx.Response(200, json={"data": data, "error": {"code": "ok", "message": "", "log_id": "202611170001"}})

    @staticmethod
    def error(status, code, message="error"):
        return httpx.Response(status, json={"data": {}, "error": {"code": code, "message": message,
                                                                   "log_id": "20261117LOGID"}})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        path = request.url.path
        if self.replies.get(path):
            return self.replies[path].pop(0)
        if request.url.host == "open-upload.tiktokapis.com":
            start, end, total = map(int, request.headers["content-range"].removeprefix("bytes ").replace("/", "-").split("-"))
            return httpx.Response(201 if end == total - 1 else 206)
        assert request.url.host == "open.tiktokapis.com"
        if path == "/v2/oauth/token/":
            return httpx.Response(200, json={"access_token": "act.refreshedAccessToken0123456789",
                                             "refresh_token": TIKTOK_REFRESH, "expires_in": 86400})
        if path == "/v2/user/info/":
            return self.ok({"user": {"open_id": "open-1", "display_name": "FindHub Gadgets"}})
        if path == "/v2/post/publish/creator_info/query/":
            return self.ok({"creator_nickname": "FindHub", "privacy_level_options": self.privacy,
                            "comment_disabled": False, "duet_disabled": True, "stitch_disabled": False,
                            "max_video_post_duration_sec": 600})
        if path == "/v2/post/publish/video/init/":
            return self.ok({"publish_id": "v_pub_file~v2-1.7300000000000000001", "upload_url": UPLOAD_URL})
        if path == "/v2/post/publish/status/fetch/":
            status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            extra = {"publicaly_available_post_id": [7300000000000012345]} if status == "PUBLISH_COMPLETE" else {}
            return self.ok({"status": status, **extra})
        raise AssertionError(f"unexpected call {request.method} {request.url}")

    def json(self, path: str, index: int = 0) -> dict:
        return json.loads([c for c in self.calls if c.url.path == path][index].content)

    def puts(self) -> list[httpx.Request]:
        return [c for c in self.calls if c.method == "PUT"]


def tiktok(engine, api, *, refresh=False, **options) -> TikTokPublisher:
    credentials = {"token": SecretStr(TIKTOK_TOKEN)}
    if refresh:
        credentials |= {"refresh_token": SecretStr(TIKTOK_REFRESH), "client_key": SecretStr("awunittestclientkey"),
                        "client_secret": SecretStr("unitTestTikTokClientSecret0123")}
    target = PublishTarget(platform="tiktok_pub", page_name="@gadgets.en", options=options, credentials=credentials)
    publisher = TikTokPublisher(PluginContext(settings=engine.settings), target)
    publisher._transport = httpx.MockTransport(api)
    return publisher


def test_tiktok_direct_post_initialises_uploads_and_confirms(engine, content, video_file):
    api = TikTokApi()
    receipt = tiktok(engine, api).publish(content)

    assert receipt.success and receipt.platform == "tiktok" and receipt.platform_post_id == "7300000000000012345"
    assert [c.url.path for c in api.calls if c.method == "POST"] == [
        "/v2/post/publish/creator_info/query/", "/v2/post/publish/video/init/",
        "/v2/post/publish/status/fetch/", "/v2/post/publish/status/fetch/"]
    assert all(c.headers["authorization"] == f"Bearer {TIKTOK_TOKEN}" for c in api.calls if c.method == "POST")
    assert all(TIKTOK_TOKEN not in str(c.url) for c in api.calls)
    body = api.json("/v2/post/publish/video/init/")
    size = video_file.stat().st_size
    assert body["source_info"] == {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": size, "total_chunk_count": 1}
    info = body["post_info"]
    assert info["privacy_level"] == "PUBLIC_TO_EVERYONE" and info["disable_duet"] is True
    assert info["disable_comment"] is False and info["brand_content_toggle"] is False
    assert content.caption in info["title"] and "#gadgets" in info["title"] and "Link in bio -> /c/gadgets" in info["title"]
    assert content.product.affiliate_url not in info["title"]           # TikTok captions carry no clickable link
    (put,) = api.puts()
    assert str(put.url) == UPLOAD_URL and put.headers["content-type"] == "video/mp4"
    assert put.headers["content-range"] == f"bytes 0-{size - 1}/{size}" and len(put.content) == size


def test_tiktok_large_files_are_uploaded_in_chunks(engine, content, video_file):
    api = TikTokApi()
    publisher = tiktok(engine, api)
    publisher.chunk_size, publisher.single_upload_max = 100_000, 100_000
    publisher.publish(content)

    size = video_file.stat().st_size
    count = size // 100_000
    assert api.json("/v2/post/publish/video/init/")["source_info"]["total_chunk_count"] == count
    ranges = [p.headers["content-range"] for p in api.puts()]
    assert ranges[0] == f"bytes 0-99999/{size}" and ranges[-1] == f"bytes {(count - 1) * 100_000}-{size - 1}/{size}"
    assert sum(len(p.content) for p in api.puts()) == size


def test_tiktok_chunk_plan_follows_the_api_rules():
    assert plan_chunks(3 * 1024 * 1024) == (3 * 1024 * 1024, 1)                 # under 5 MB: one chunk
    assert plan_chunks(60 * 1024 * 1024) == (60 * 1024 * 1024, 1)               # up to 64 MB: one chunk
    assert plan_chunks(105 * 1024 * 1024) == (10 * 1024 * 1024, 10)             # last chunk takes the remainder
    with pytest.raises(NonRetryableError):
        plan_chunks(0)


def test_tiktok_refreshes_the_24_hour_token_when_refresh_credentials_exist(engine, content):
    api = TikTokApi()
    tiktok(engine, api, refresh=True).publish(content)
    refresh = api.calls[0]
    assert refresh.url.path == "/v2/oauth/token/" and b"grant_type=refresh_token" in refresh.content
    assert api.calls[1].headers["authorization"] == "Bearer act.refreshedAccessToken0123456789"


def test_tiktok_unaudited_client_error_is_actionable(engine, content):
    api = TikTokApi()
    api.replies["/v2/post/publish/video/init/"] = [api.error(
        403, "unaudited_client_can_only_post_to_private_accounts", "Please review our integration guidelines")]
    with pytest.raises(NonRetryableError) as raised:
        tiktok(engine, api).publish(content)
    message = str(raised.value)
    assert "unaudited_client_can_only_post_to_private_accounts" in message and "20261117LOGID" in message
    assert "TIKTOK_PRIVACY_LEVEL=SELF_ONLY" in message and api.puts() == []


def test_tiktok_rate_limit_backs_off_and_retries(engine, content, clean_environment):
    api = TikTokApi()
    api.replies["/v2/post/publish/video/init/"] = [api.error(429, "rate_limit_exceeded", "Too many requests")]
    receipt = tiktok(engine, api).publish(content)
    assert receipt.success
    assert len([c for c in api.calls if c.url.path == "/v2/post/publish/video/init/"]) == 2
    assert max(clean_environment) >= 20                                   # waited out the 6/min window


def test_tiktok_client_side_limiter_keeps_six_requests_per_minute(clean_environment):
    clock = iter(range(1000))
    limiter = tiktok_pub._RateLimiter(clock=lambda: next(clock) * 0.0)   # all calls in the same instant
    for _ in range(6):
        limiter.acquire(TIKTOK_TOKEN)
    assert clean_environment == []
    limiter.acquire(TIKTOK_TOKEN)
    assert clean_environment and clean_environment[-1] > 59


def test_tiktok_expired_token_explains_how_to_refresh(engine, content):
    api = TikTokApi()
    api.replies["/v2/post/publish/creator_info/query/"] = [api.error(401, "access_token_invalid", "token expired")]
    with pytest.raises(NonRetryableError, match="TIKTOK_REFRESH_TOKEN"):
        tiktok(engine, api).publish(content)


def test_tiktok_privacy_level_must_be_offered_to_the_creator(engine, content, monkeypatch):
    monkeypatch.setenv("TIKTOK_PRIVACY_LEVEL", "PUBLIC_TO_EVERYONE")
    api = TikTokApi(privacy=("SELF_ONLY",))
    with pytest.raises(NonRetryableError, match="allowed: SELF_ONLY"):
        tiktok(engine, api).publish(content)
    monkeypatch.setenv("TIKTOK_PRIVACY_LEVEL", "SELF_ONLY")
    assert tiktok(engine, TikTokApi(privacy=("SELF_ONLY",))).publish(content).success
    monkeypatch.setenv("TIKTOK_COMMERCIAL_CONTENT", "branded_content")
    with pytest.raises(ConfigError, match="branded content"):
        tiktok(engine, TikTokApi()).publish(content)


def test_tiktok_failed_processing_reports_the_reason(engine, content):
    api = TikTokApi(statuses=("FAILED",))
    api.replies["/v2/post/publish/status/fetch/"] = [api.ok({"status": "FAILED", "fail_reason": "duration_check_failed"})]
    with pytest.raises(NonRetryableError, match="duration_check_failed → the video is longer"):
        tiktok(engine, api).publish(content)


def test_tiktok_dry_run_sends_nothing(engine, content, caplog):
    api = TikTokApi()
    receipt = tiktok(engine, api, dry_run=True).publish(content)
    assert receipt.platform_post_id.startswith("dry-run-") and api.calls == []
    assert "TikTok Direct Post simulated" in caplog.text


def test_verify_tiktok_reports_identity_and_posting_permission(engine):
    api = TikTokApi(privacy=("SELF_ONLY",))
    result = tiktok_pub.verify_connection(engine.settings, TIKTOK_TOKEN, transport=httpx.MockTransport(api))
    assert result.token_valid and result.can_post and result.display_name == "FindHub Gadgets"
    assert not result.ok and "PUBLIC_TO_EVERYONE is not among" in result.problems[0]
    assert any("24 hours" in note for note in result.notes)
    assert all(c.method in ("GET", "POST") and TIKTOK_TOKEN not in str(c.url) for c in api.calls)

    bad = TikTokApi()
    bad.replies["/v2/user/info/"] = [bad.error(401, "access_token_invalid", "expired")]
    expired = tiktok_pub.verify_connection(engine.settings, TIKTOK_TOKEN, transport=httpx.MockTransport(bad))
    assert not expired.token_valid and "access_token_invalid" in expired.problems[0]


# --------------------------------------------------------------------------- #
# Wiring: content profiles, accounts, CLI
# --------------------------------------------------------------------------- #
def test_video_content_profiles_route_to_the_video_generator(engine):
    for content_type, platform in (("youtube_short", "youtube"), ("tiktok_video", "tiktok")):
        profile: ContentProfile = engine.registry.get_content_profile(content_type)
        assert (profile.platform, profile.generator, profile.publish_format) == (platform, "ffmpeg_video_gen", "video")
        assert profile.llm_model.startswith("claude-")


def test_publish_routes_youtube_and_tiktok_through_their_publishers(harvested, monkeypatch):
    monkeypatch.setenv("YOUTUBE_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("YOUTUBE_CLIENT_SECRET", CLIENT_SECRET)
    monkeypatch.setenv("YOUTUBE_REFRESH_TOKEN", REFRESH)
    report = harvested.orchestrator.run("gadgets", "he", ["youtube_short", "tiktok_video"], mode="queued", dry_run=True)
    routes = {route.content_type: route for route in report.routes}
    assert routes["youtube_short"].publisher == "youtube_pub" and routes["youtube_short"].ready
    assert routes["tiktok_video"].publisher == "tiktok_pub" and not routes["tiktok_video"].ready
    assert "TIKTOK_ACCESS_TOKEN_GADGETS_HE" in routes["tiktok_video"].reason
    assert report.count("preview") >= 1


def test_cli_verify_flags(make_settings, monkeypatch, capsys):
    import main

    settings = make_settings()
    for name, value in {"DATABASE_PATH": settings.database_path, "LOG_FILE": settings.log_file}.items():
        monkeypatch.setenv(name, str(value))
    assert main.main(["--verify-youtube", "--category", "gadgets", "--lang", "en"]) == 1
    out = capsys.readouterr().out
    assert "YouTube Data API v3" in out and "YOUTUBE_REFRESH_TOKEN_GADGETS_EN" in out and "section A" in out
    assert main.main(["--verify-tiktok"]) == 1
    assert "TIKTOK_ACCESS_TOKEN_GADGETS_HE" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main.main(["--verify-tiktok", "--dry-run"])


@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="needs ffmpeg")
def test_probe_reads_a_real_vertical_render(tmp_path):
    def render(name: str, size: str, seconds: int):
        path = tmp_path / name
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"color=c=white:s={size}:d={seconds}",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True, timeout=60)
        return _video.probe_video(path)

    vertical = render("v.mp4", "108x192", 2)
    assert vertical.is_vertical_9_16 and vertical.codec == "h264" and 1.5 < vertical.duration < 2.5
    _video.check_short_form(vertical, "YouTube Shorts", 60)
    with pytest.raises(NonRetryableError, match="not vertical"):
        _video.check_short_form(render("h.mp4", "192x108", 1), "YouTube Shorts", 60)
