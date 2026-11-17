"""TikTok publisher: Content Posting API — Direct Post with a chunked FILE_UPLOAD.

Host and authentication
-----------------------
Every call goes to ``https://open.tiktokapis.com`` with the creator's **user access token**
(OAuth, scope ``video.publish``) in an ``Authorization: Bearer`` header — never in a URL.
User access tokens expire after 24 hours. When ``TIKTOK_CLIENT_KEY``,
``TIKTOK_CLIENT_SECRET`` and a refresh token (``TIKTOK_REFRESH_TOKEN``, valid for a year)
are set, the publisher exchanges the refresh token for a fresh access token before it
posts, so unattended runs keep working; otherwise ``TIKTOK_ACCESS_TOKEN`` is used as-is.
A social account names its own variables: ``token_env_var`` → the access token,
``account_id_env_var`` → that account's refresh token.

Direct Post flow
----------------
0. ``POST /v2/post/publish/creator_info/query/``  the privacy levels the creator may use,
   the longest video they may post and whether comments / duets / stitches are off
1. ``POST /v2/post/publish/video/init/``  ``post_info`` (caption, privacy level, interaction
   settings, commercial-content disclosure) + ``source_info`` (FILE_UPLOAD, size, chunking)
   → ``publish_id`` and an ``upload_url``
2. ``PUT {upload_url}``  the MP4 in chunks with ``Content-Range`` (5–64 MB each; a file of
   up to 64 MB goes in one request)
3. ``POST /v2/post/publish/status/fetch/``  poll until ``PUBLISH_COMPLETE`` (or ``FAILED``)

Settings (all optional): ``TIKTOK_PRIVACY_LEVEL`` (default PUBLIC_TO_EVERYONE; unaudited
apps must use SELF_ONLY), ``TIKTOK_COMMERCIAL_CONTENT`` (none | your_brand |
branded_content, default none).

Limits: TikTok allows 6 requests per minute per user token on the posting endpoints; the
publisher paces itself under that and backs off on ``rate_limit_exceeded``. Apps that have
not passed TikTok's Direct Post audit can only post SELF_ONLY (private) videos, to at most
5 creators a day — the error message says so when it happens.

:func:`verify_connection` (``python main.py --verify-tiktok``) checks the token and the
posting permission with read-only calls; it never posts.
"""

from __future__ import annotations

import collections
import hashlib
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import httpx

from core import resilience
from core.database import category_slug
from core.interfaces import BasePublisher, PluginContext
from core.models import GeneratedContent, PublishReceipt, PublishTarget
from core.resilience import (
    ConfigError, NonRetryableError, TarshishError, get_logger, redact, register_secret, with_retry,
)
from plugins.publishers import disclosure_enabled, render_post_text
from plugins.publishers._video import (
    check_short_form, local_video, probe_video, require_video, video_reference,
)

logger = get_logger("plugins.tiktok_pub")

PLUGIN_NAME = "tiktok_pub"
API_BASE = "https://open.tiktokapis.com"
CREATOR_INFO_PATH = "/v2/post/publish/creator_info/query/"
INIT_PATH = "/v2/post/publish/video/init/"
STATUS_PATH = "/v2/post/publish/status/fetch/"
TOKEN_PATH = "/v2/oauth/token/"
USER_INFO_PATH = "/v2/user/info/"

CAPTION_LIMIT = 2200                 # UTF-16 code units
MIN_CHUNK = 5 * 1024 * 1024
MAX_CHUNK = 64 * 1024 * 1024
DEFAULT_CHUNK = 10 * 1024 * 1024
MAX_CHUNKS = 1000
SHORT_FORM_MAX_SECONDS = 60          # the engine's short-form standard (TikTok itself allows longer)
REQUESTS_PER_MINUTE = 6
PRIVACY_LEVELS = ("PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR", "SELF_ONLY")
COMMERCIAL_OPTIONS = {"none": (False, False), "your_brand": (False, True), "branded_content": (True, False)}
_CALL_TO_ACTION = {"he": "🔗 הקישור בביו ← {path}", "en": "🔗 Link in bio -> {path}"}
_TOKEN_ENV, _REFRESH_ENV = "TIKTOK_ACCESS_TOKEN", "TIKTOK_REFRESH_TOKEN"
_CLIENT_KEY_ENV, _CLIENT_SECRET_ENV = "TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET"

_HINTS = {
    "access_token_invalid": (f"the user access token expired (they last 24 hours) or was revoked — set "
                             f"{_REFRESH_ENV} + {_CLIENT_KEY_ENV} + {_CLIENT_SECRET_ENV} so the engine refreshes it "
                             "automatically, or authorize again"),
    "scope_not_authorized": "the user did not grant video.publish — authorize again requesting the video.publish scope",
    "scope_permission_missed": "the app is not approved for video.publish — add the Content Posting API (Direct Post) product",
    "unaudited_client_can_only_post_to_private_accounts": (
        "the app has not passed TikTok's Direct Post audit: until it does, posts must use "
        "TIKTOK_PRIVACY_LEVEL=SELF_ONLY and the creator account must be private — submit the app for the audit "
        "in the TikTok Developers Portal to post publicly"),
    "reached_active_user_cap": "unaudited apps may post for at most 5 creators per 24 hours — wait, or get the app audited",
    "spam_risk_too_many_posts": "the creator reached TikTok's daily posting cap — the post can be retried tomorrow",
    "spam_risk_user_banned_from_posting": "TikTok has banned this creator from posting — check the account in the app",
    "spam_risk_too_many_pending_share": "too many posts are still processing for this creator — wait and retry",
    "privacy_level_option_mismatch": ("TIKTOK_PRIVACY_LEVEL is not one of the creator's allowed privacy levels "
                                      "(see --verify-tiktok)"),
    "rate_limit_exceeded": f"TikTok allows {REQUESTS_PER_MINUTE} requests per minute per user token — backing off",
    "invalid_file_upload": "TikTok rejected the file or its chunking — the MP4 must be H.264, 5–64 MB chunks",
    "file_format_check_failed": "TikTok could not read the video — render H.264 MP4 at 1080x1920",
    "duration_check_failed": "the video is longer than the creator may post (see max_video_post_duration_sec)",
    "frame_rate_check_failed": "the frame rate is not supported — render at 23–60 fps",
    "picture_size_check_failed": "the resolution is not supported — render 1080x1920 (at least 360 px each side)",
    "internal_error": "TikTok had a server error — the call is retried with backoff",
}
_RETRYABLE_CODES = {"rate_limit_exceeded", "internal_error", "spam_risk_too_many_pending_share"}


class TikTokRetryableError(TarshishError):
    """Rate limit or server-side failure; ``retry_after`` is honoured by ``with_retry``."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class TikTokTokenError(NonRetryableError):
    """The access token was rejected (expired / revoked)."""


class _StillProcessing(TarshishError):
    """TikTok has not finished processing the upload yet."""


# --------------------------------------------------------------------------- #
# Rate limiting: 6 requests / minute / user token, shared by every publisher in the process
# --------------------------------------------------------------------------- #
class _RateLimiter:
    def __init__(self, per_minute: int = REQUESTS_PER_MINUTE, clock: Any = time.monotonic) -> None:
        self.per_minute = per_minute
        self.clock = clock
        self.calls: dict[str, collections.deque[float]] = collections.defaultdict(collections.deque)

    def reset(self) -> None:
        self.calls.clear()

    def acquire(self, token: str) -> None:
        key = hashlib.sha256(token.encode()).hexdigest()[:16]
        window = self.calls[key]
        now = self.clock()
        while window and now - window[0] >= 60:
            window.popleft()
        if len(window) >= self.per_minute:
            wait = 60 - (now - window[0]) + 0.5
            logger.info("TikTok rate limit (%d/min): waiting %.1fs before the next call.", self.per_minute, wait)
            resilience._sleep(wait)
            now = self.clock()
            while window and now - window[0] >= 60:
                window.popleft()
            if len(window) >= self.per_minute:   # the clock did not move (tests): make room explicitly
                window.popleft()
        window.append(now)


rate_limiter = _RateLimiter()


# --------------------------------------------------------------------------- #
# HTTP helpers shared by the publisher and the verification check
# --------------------------------------------------------------------------- #
def interpret_response(action: str, response: httpx.Response) -> dict[str, Any]:
    """Return ``data`` of a successful reply; map failures onto retryable / token / fatal errors."""
    try:
        body = response.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    error = body.get("error") if isinstance(body.get("error"), dict) else {}
    code = str(error.get("code") or "")
    if response.status_code < 300 and code in ("", "ok"):
        return body.get("data") if isinstance(body.get("data"), dict) else {}

    code = code or f"http_{response.status_code}"
    description = redact(str(error.get("message") or f"HTTP {response.status_code}"))
    log_id = error.get("log_id")
    message = f"TikTok {action} failed ({response.status_code}, {code}): {description}"
    if log_id:
        message += f" [log_id {log_id}]"
    hint = _HINTS.get(code)
    if hint:
        message += f" → {hint}"
    if code == "access_token_invalid" or response.status_code == 401:
        raise TikTokTokenError(message)
    if response.status_code == 429 or code == "rate_limit_exceeded":
        retry_after = response.headers.get("retry-after")
        raise TikTokRetryableError(message, retry_after=float(retry_after) if retry_after else 60 / REQUESTS_PER_MINUTE * 2)
    if response.status_code >= 500 or code in _RETRYABLE_CODES:
        raise TikTokRetryableError(message)
    raise NonRetryableError(message)


def _client(transport: Any) -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(120.0, connect=10.0), transport=transport)


def _post(client: httpx.Client, token: str, path: str, payload: dict[str, Any], action: str, *,
          limited: bool = True) -> dict[str, Any]:
    if limited:
        rate_limiter.acquire(token)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"}
    try:
        response = client.post(f"{API_BASE}{path}", json=payload, headers=headers)
    except httpx.HTTPError as exc:
        raise TikTokRetryableError(redact(f"TikTok {action} failed: {type(exc).__name__}: {exc}")) from None
    return interpret_response(action, response)


def refresh_access_token(
    client: httpx.Client, client_key: str, client_secret: str, refresh_token: str,
) -> tuple[str, str | None]:
    """Exchange a refresh token for ``(access_token, new_refresh_token)``."""
    try:
        response = client.post(
            f"{API_BASE}{TOKEN_PATH}",
            data={"client_key": client_key, "client_secret": client_secret, "grant_type": "refresh_token",
                  "refresh_token": refresh_token},
            headers={"Content-Type": "application/x-www-form-urlencoded", "Cache-Control": "no-cache"},
        )
    except httpx.HTTPError as exc:
        raise TikTokRetryableError(redact(f"TikTok token refresh failed: {type(exc).__name__}: {exc}")) from None
    try:
        body = response.json()
    except ValueError:
        body = {}
    token = body.get("access_token") if isinstance(body, dict) else None
    if response.status_code >= 300 or not token:
        error = body.get("error") if isinstance(body, dict) else None
        detail = redact(str(body.get("error_description") or error or f"HTTP {response.status_code}"))
        if response.status_code >= 500:
            raise TikTokRetryableError(f"TikTok token refresh failed: {detail}")
        raise NonRetryableError(
            f"TikTok token refresh failed: {detail} → the refresh token expired or does not belong to "
            f"{_CLIENT_KEY_ENV}; authorize again and update {_REFRESH_ENV}"
        )
    register_secret(token)
    new_refresh = body.get("refresh_token")
    register_secret(new_refresh)
    return str(token), (str(new_refresh) if new_refresh else None)


def plan_chunks(size: int, chunk_size: int = DEFAULT_CHUNK, single_max: int = MAX_CHUNK) -> tuple[int, int]:
    """``(chunk_size, total_chunk_count)`` following TikTok's rules.

    Up to ``single_max`` bytes go in one chunk; larger files use ``chunk_size`` chunks and the
    last chunk carries the remainder (``total = floor(size / chunk_size)``).
    """
    if size <= 0:
        raise NonRetryableError("TikTok: the video file is empty")
    if size <= single_max:
        return size, 1
    count = size // chunk_size
    if count > MAX_CHUNKS:
        raise NonRetryableError("TikTok: the video is too large for 1,000 chunks")
    return chunk_size, count


def commercial_flags(settings: Any) -> tuple[bool, bool]:
    value = str(settings.get_env("TIKTOK_COMMERCIAL_CONTENT", "none") or "none").strip().lower()
    if value not in COMMERCIAL_OPTIONS:
        raise ConfigError(f"TIKTOK_COMMERCIAL_CONTENT={value!r} is not one of: {', '.join(COMMERCIAL_OPTIONS)}")
    return COMMERCIAL_OPTIONS[value]


# --------------------------------------------------------------------------- #
# Publisher
# --------------------------------------------------------------------------- #
class TikTokPublisher(BasePublisher):
    required_credentials = ("token",)
    default_env_vars = {
        "token": _TOKEN_ENV, "refresh_token": _REFRESH_ENV,
        "client_key": _CLIENT_KEY_ENV, "client_secret": _CLIENT_SECRET_ENV,
    }
    account_credential = "refresh_token"
    inline_credentials = {"client_key": "client_key"}
    _transport: Any = None              # test hook: an httpx.MockTransport instead of the network
    chunk_size: int = DEFAULT_CHUNK
    single_upload_max: int = MAX_CHUNK

    def __init__(self, context: PluginContext, target: PublishTarget) -> None:
        super().__init__(context, target)
        for name in ("token", "refresh_token", "client_secret"):
            register_secret(target.credential(name))
        self._privacy = self._privacy_level()
        self._brand_content, self._brand_organic = commercial_flags(context.settings)
        self._access_token: str | None = None

    @property
    def platform_name(self) -> str:
        return "tiktok"

    def _privacy_level(self) -> str:
        value = str(self.context.settings.get_env("TIKTOK_PRIVACY_LEVEL", "PUBLIC_TO_EVERYONE") or "").strip().upper()
        value = value or "PUBLIC_TO_EVERYONE"
        if value not in PRIVACY_LEVELS:
            raise ConfigError(f"TIKTOK_PRIVACY_LEVEL={value!r} is not one of: {', '.join(PRIVACY_LEVELS)}")
        return value

    # -- contract -------------------------------------------------------------------- #
    def publish(self, content: GeneratedContent) -> PublishReceipt:
        require_video(content, "TikTok")
        caption = self.render_caption(content)
        if self.context.dry_run or self.target.options.get("dry_run"):
            self._log_simulation(caption, content)
            return PublishReceipt(
                platform=self.platform_name, platform_post_id=f"dry-run-{uuid.uuid4().hex[:10]}", success=True
            )
        if self._brand_content and self._privacy == "SELF_ONLY":
            raise ConfigError("TikTok does not allow branded content (TIKTOK_COMMERCIAL_CONTENT=branded_content) "
                              "to be private (TIKTOK_PRIVACY_LEVEL=SELF_ONLY)")

        with local_video(content, "TikTok") as path, _client(self._transport) as client:
            token = self._token(client)
            creator = self._call(client, token, CREATOR_INFO_PATH, {}, "creator_info query")
            privacy = self._choose_privacy(creator)
            info = probe_video(path)
            max_seconds = min(SHORT_FORM_MAX_SECONDS, float(creator.get("max_video_post_duration_sec") or 600))
            check_short_form(info, "TikTok", max_seconds)

            size = path.stat().st_size
            chunk_size, chunks = plan_chunks(size, self.chunk_size, self.single_upload_max)
            body = {
                "post_info": self.post_info(caption, privacy, creator),
                "source_info": {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk_size,
                                "total_chunk_count": chunks},
            }
            init = self._call(client, token, INIT_PATH, body, "video init")
            publish_id, upload_url = str(init.get("publish_id") or ""), str(init.get("upload_url") or "")
            if not publish_id or not upload_url:
                raise NonRetryableError(f"TikTok video init returned no publish_id / upload_url: {redact(str(init))}")
            logger.info("TikTok upload started for %s (publish_id %s, %d byte(s) in %d chunk(s), %s).",
                        self.target.page_name, publish_id, size, chunks, privacy)
            self._upload(client, upload_url, path, size, chunk_size, chunks)
            post_id = self._await_publish(client, token, publish_id)

        logger.info("TikTok video published to %s (%s).", self.target.page_name, post_id)
        return PublishReceipt(platform=self.platform_name, platform_post_id=post_id, success=True)

    # -- payload ----------------------------------------------------------------------- #
    def render_caption(self, content: GeneratedContent) -> str:
        """Caption + hashtags + link-in-bio call to action (TikTok captions carry no clickable link)."""
        lang = content.language.split("-")[0]
        path = f"/c/{category_slug(content.product.category)}"
        call_to_action = _CALL_TO_ACTION.get(lang, _CALL_TO_ACTION["en"]).format(path=path)
        return render_post_text(content, max_length=CAPTION_LIMIT, disclosure=disclosure_enabled(self.context.settings),
                                call_to_action=call_to_action)

    def post_info(self, caption: str, privacy: str, creator: dict[str, Any]) -> dict[str, Any]:
        info: dict[str, Any] = {
            "title": caption,
            "privacy_level": privacy,
            "disable_comment": bool(creator.get("comment_disabled")),
            "disable_duet": bool(creator.get("duet_disabled")),
            "disable_stitch": bool(creator.get("stitch_disabled")),
            "video_cover_timestamp_ms": 1000,
            "brand_content_toggle": self._brand_content,
            "brand_organic_toggle": self._brand_organic,
        }
        return info

    def _choose_privacy(self, creator: dict[str, Any]) -> str:
        options = [str(option) for option in creator.get("privacy_level_options") or []]
        if options and self._privacy not in options:
            raise NonRetryableError(
                f"TikTok: TIKTOK_PRIVACY_LEVEL={self._privacy} is not allowed for {self.target.page_name} "
                f"(allowed: {', '.join(options)}) → {_HINTS['privacy_level_option_mismatch']}"
            )
        return self._privacy

    def _log_simulation(self, caption: str, content: GeneratedContent) -> None:
        logger.info(
            "TikTok Direct Post simulated (dry-run) for %s — nothing was sent.\n  video  : %s\n  privacy: %s%s\n"
            "  caption:\n%s",
            self.target.page_name, video_reference(content) or "none (render job only)", self._privacy,
            " · branded content" if self._brand_content else (" · your brand" if self._brand_organic else ""), caption,
        )

    # -- transport ----------------------------------------------------------------------- #
    def _token(self, client: httpx.Client) -> str:
        """The access token; refreshed first when the refresh credentials are configured."""
        if self._access_token:
            return self._access_token
        refresh = self.target.credential("refresh_token")
        key, secret = self.target.credential("client_key"), self.target.credential("client_secret")
        if refresh and key and secret:
            token, _ = with_retry(max_attempts=3, base_delay=2.0)(refresh_access_token)(client, key, secret, refresh)
            self._access_token = token
        else:
            self._access_token = self.target.credential("token") or ""
        return self._access_token

    @with_retry(max_attempts=4, base_delay=5.0, max_delay=60.0)
    def _call(self, client: httpx.Client, token: str, path: str, payload: dict[str, Any], action: str) -> dict[str, Any]:
        return _post(client, token, path, payload, action)

    def _upload(self, client: httpx.Client, upload_url: str, path: Path, size: int, chunk_size: int, chunks: int) -> None:
        with path.open("rb") as handle:
            for index in range(chunks):
                start = index * chunk_size
                end = size - 1 if index == chunks - 1 else start + chunk_size - 1
                handle.seek(start)
                data = handle.read(end - start + 1)
                self._put_chunk(client, upload_url, data, start, end, size, last=index == chunks - 1)

    @with_retry(max_attempts=4, base_delay=2.0, max_delay=30.0)
    def _put_chunk(self, client: httpx.Client, url: str, data: bytes, start: int, end: int, size: int, *, last: bool) -> None:
        headers = {"Content-Type": "video/mp4", "Content-Length": str(len(data)),
                   "Content-Range": f"bytes {start}-{end}/{size}"}
        try:
            response = client.put(url, content=data, headers=headers)
        except httpx.HTTPError as exc:
            # The upload URL embeds a signed upload token: never log it.
            raise TikTokRetryableError(f"TikTok chunk upload ({start}-{end}) failed: {type(exc).__name__}") from None
        expected = (200, 201) if last else (200, 201, 206)
        if response.status_code in expected:
            return
        text = f"TikTok chunk upload ({start}-{end}/{size}) failed: HTTP {response.status_code}"
        if response.status_code == 429 or response.status_code >= 500:
            raise TikTokRetryableError(text)
        detail = redact(response.text[:300]) if response.text else ""
        raise NonRetryableError(f"{text} {detail} → {_HINTS['invalid_file_upload']}".rstrip())

    def _await_publish(self, client: httpx.Client, token: str, publish_id: str) -> str:
        try:
            return self._poll_status(client, token, publish_id)
        except _StillProcessing:
            logger.warning("TikTok is still processing publish_id %s; it will appear on %s when done.",
                           publish_id, self.target.page_name)
            return publish_id

    @with_retry(max_attempts=10, base_delay=5.0, max_delay=30.0, retry_on=(_StillProcessing,))
    def _poll_status(self, client: httpx.Client, token: str, publish_id: str) -> str:
        data = self._call(client, token, STATUS_PATH, {"publish_id": publish_id}, "status fetch")
        status = str(data.get("status") or "")
        if status == "PUBLISH_COMPLETE":
            # TikTok's own spelling: "publicaly_available_post_id" (only filled for public posts).
            ids = data.get("publicaly_available_post_id") or data.get("publicly_available_post_id") or []
            return str(ids[0]) if ids else publish_id
        if status == "SEND_TO_USER_INBOX":
            return publish_id
        if status == "FAILED":
            reason = str(data.get("fail_reason") or "unknown")
            hint = _HINTS.get(reason)
            raise NonRetryableError(f"TikTok could not publish {publish_id}: {reason}" + (f" → {hint}" if hint else ""))
        raise _StillProcessing(f"TikTok status {status or 'unknown'} for {publish_id}")


# --------------------------------------------------------------------------- #
# Live verification (read-only)
# --------------------------------------------------------------------------- #
@dataclass
class TikTokVerification:
    """Outcome of :func:`verify_connection`. Never contains a token."""

    page_name: str
    token_valid: bool = False
    refreshed: bool | None = None
    display_name: str | None = None
    can_post: bool = False
    privacy_options: list[str] = field(default_factory=list)
    max_duration: int | None = None
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.token_valid and self.can_post and not self.problems

    def summary(self) -> str:
        who = f"Connected as {self.display_name}" if self.display_name else (
            "Token valid" if self.token_valid else "Not connected")
        return f"{who} | Direct Post: {'✓' if self.can_post else '✗'} video.publish"

    def lines(self) -> list[str]:
        mark = lambda ok: "✓" if ok else "✗"  # noqa: E731
        refresh = {True: " (refreshed from the refresh token)", False: "", None: ""}[self.refreshed]
        rows = [
            f"{mark(self.token_valid)} Access token   : " + ("valid" if self.token_valid else "invalid or unverified") + refresh,
            f"{mark(self.can_post)} Direct Post    : "
            + (f"allowed · privacy {', '.join(self.privacy_options) or 'unknown'}"
               + (f" · videos up to {self.max_duration}s" if self.max_duration else "")
               if self.can_post else "not available"),
        ]
        rows += [f"✗ {problem}" for problem in self.problems]
        rows += [f"· {note}" for note in self.notes]
        return rows


def verify_connection(
    settings: Any, token: str | None, *, refresh_token: str | None = None, client_key: str | None = None,
    client_secret: str | None = None, page_name: str = "default", transport: Any = None,
) -> TikTokVerification:
    """Check the user token (``/v2/user/info/``) and the Direct Post permission (``creator_info``). Never posts."""
    result = TikTokVerification(page_name=page_name)
    can_refresh = bool(refresh_token and client_key and client_secret)
    if not token and not can_refresh:
        result.problems.append(f"not configured: {_TOKEN_ENV} missing in .env")
        return result
    for secret in (token, refresh_token, client_secret):
        register_secret(secret)
    transport = transport if transport is not None else TikTokPublisher._transport
    with _client(transport) as client:
        if can_refresh:
            try:
                token, _ = refresh_access_token(client, client_key or "", client_secret or "", refresh_token or "")
                result.refreshed = True
            except TarshishError as exc:
                result.refreshed = False
                result.problems.append(redact(str(exc)))
                if not token:
                    return result
        elif refresh_token or client_key or client_secret:
            result.notes.append(f"automatic refresh needs all of {_REFRESH_ENV}, {_CLIENT_KEY_ENV} and "
                                f"{_CLIENT_SECRET_ENV}; the access token expires after 24 hours")
        else:
            result.notes.append(f"no refresh credentials: {_TOKEN_ENV} expires after 24 hours — set "
                                f"{_REFRESH_ENV}, {_CLIENT_KEY_ENV} and {_CLIENT_SECRET_ENV} for unattended runs")
        assert token is not None

        try:
            response = client.get(f"{API_BASE}{USER_INFO_PATH}", params={"fields": "open_id,display_name"},
                                  headers={"Authorization": f"Bearer {token}"})
            user = interpret_response("user info", response).get("user") or {}
            result.token_valid = True
            result.display_name = user.get("display_name")
        except TikTokTokenError as exc:
            result.problems.append(redact(str(exc)))
            return result
        except httpx.HTTPError as exc:
            result.problems.append(redact(f"TikTok user info failed: {type(exc).__name__}: {exc}"))
            return result
        except TarshishError as exc:
            # user.info.basic may not be granted; the posting check below still proves the token works.
            result.notes.append(f"user info unavailable ({redact(str(exc))})")

        try:
            creator = _post(client, token, CREATOR_INFO_PATH, {}, "creator_info query")
            result.token_valid = True
            result.can_post = True
            result.display_name = result.display_name or creator.get("creator_nickname") or creator.get("creator_username")
            result.privacy_options = [str(option) for option in creator.get("privacy_level_options") or []]
            result.max_duration = creator.get("max_video_post_duration_sec")
            wanted = str(settings.get_env("TIKTOK_PRIVACY_LEVEL", "PUBLIC_TO_EVERYONE") or "PUBLIC_TO_EVERYONE").upper()
            if result.privacy_options and wanted not in result.privacy_options:
                result.problems.append(f"TIKTOK_PRIVACY_LEVEL={wanted} is not among the creator's options "
                                       f"({', '.join(result.privacy_options)})")
            if wanted != "SELF_ONLY":
                result.notes.append("until the app passes TikTok's Direct Post audit, only SELF_ONLY (private) posts "
                                    "succeed — set TIKTOK_PRIVACY_LEVEL=SELF_ONLY for sandbox / unaudited testing")
        except TarshishError as exc:
            result.problems.append(redact(str(exc)))
    return result


def verify_pages(registry: Any, pages: Iterable[Any], *, transport: Any = None) -> list[TikTokVerification]:
    """Resolve each ``tiktok_pub`` social page's env vars and verify it (read-only)."""
    results: list[TikTokVerification] = []
    for page in pages:
        if page.platform != PLUGIN_NAME:
            continue
        target, reason = registry.resolve_target(page)
        if target is None:
            refs = {**TikTokPublisher.default_env_vars, **page.env_refs()}
            results.append(TikTokVerification(
                page_name=page.page_name,
                problems=[f"not configured: {reason or 'credentials missing'} (expects {refs['token']} in .env)"],
            ))
            continue
        results.append(verify_connection(
            registry.settings, target.credential("token"), refresh_token=target.credential("refresh_token"),
            client_key=target.credential("client_key"), client_secret=target.credential("client_secret"),
            page_name=page.page_name, transport=transport,
        ))
    return results


def pages_to_verify(registry: Any, category: str | None = None, language: str | None = None) -> list[Any]:
    """The TikTok accounts of the social-accounts matrix (one check per access-token variable)."""
    from core.config import SocialPage

    pages: dict[str, Any] = {}
    for account in registry.list_social_accounts(category=category, language=language, platform="tiktok"):
        page = registry.social_page(account)
        refs = {**TikTokPublisher.default_env_vars, **page.env_refs()}
        pages.setdefault(refs["token"], page)
    if not pages and category is None and language is None:
        return [SocialPage(platform=PLUGIN_NAME, page_name="default")]
    return list(pages.values())

