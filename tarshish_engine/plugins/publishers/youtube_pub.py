"""YouTube Shorts publisher: YouTube Data API v3, OAuth 2.0 refresh token, resumable upload.

Authentication
--------------
YouTube uploads need a user's OAuth 2.0 grant for ``https://www.googleapis.com/auth/youtube.upload``.
The engine keeps only the long-lived refresh token and the OAuth client in the environment::

    YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET, YOUTUBE_REFRESH_TOKEN

and exchanges the refresh token for a short-lived access token at the start of every upload
(``google-auth``). A channel of the social-accounts matrix may name its own refresh-token
variable with ``token_env_var`` (one refresh token per channel, one OAuth client for all).
Generate a refresh token once, on a machine with a browser::

    python -m plugins.publishers.youtube_pub --authorize path/to/client_secret.json

Upload
------
``videos.insert`` (``part=snippet,status``) with a resumable media upload
(``https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable``), sent in
chunks with ``google-api-python-client`` so a dropped connection resumes instead of
starting over. YouTube classifies a video as a Short when it is vertical and short; the
publisher also puts ``#Shorts`` in the title and the description, and refuses (before
uploading) a clip that ffprobe shows is not 9:16 or runs over 60 seconds.

Settings (all optional): ``YOUTUBE_PRIVACY_STATUS`` (public | unlisted | private, default
public — use unlisted while testing), ``YOUTUBE_CATEGORY_ID`` (default 28 Science &
Technology for gadget-like collections, otherwise 22 People & Blogs) and
``YOUTUBE_DAILY_QUOTA_UNITS`` (default 10000, Google's default project quota).

Quota
-----
Every ``videos.insert`` costs 1,600 quota units, so the default 10,000-unit daily quota
allows six uploads a day; the quota resets at midnight Pacific time. The publisher counts
the units it spends and, once Google answers ``quotaExceeded``, refuses further uploads in
this process until the reset instead of spending more calls.

:func:`verify_connection` (``python main.py --verify-youtube``) refreshes the token and
checks the granted scopes with read-only calls; it never uploads.
"""

from __future__ import annotations

import json
import re
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from core.interfaces import BasePublisher, PluginContext
from core.models import GeneratedContent, PublishReceipt, PublishTarget
from core.resilience import (
    ConfigError, NonRetryableError, TarshishError, get_logger, redact, register_secret, with_retry,
)
from plugins.publishers import disclosure_enabled, render_post_text
from plugins.publishers._video import (
    check_short_form, local_video, probe_video, require_video, video_reference,
)

logger = get_logger("plugins.youtube_pub")

PLUGIN_NAME = "youtube_pub"
UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
TOKEN_URI = "https://oauth2.googleapis.com/token"
TOKENINFO_URI = "https://oauth2.googleapis.com/tokeninfo"
UPLOAD_ENDPOINT = "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status"

INSERT_COST_UNITS = 1600
DEFAULT_DAILY_QUOTA = 10_000
QUOTA_TIMEZONE = ZoneInfo("America/Los_Angeles")  # YouTube quotas reset at midnight Pacific time
SHORTS_MAX_SECONDS = 60
SHORTS_TAG = "#Shorts"
TITLE_LIMIT = 100
DESCRIPTION_UNITS = 2400        # ≤ 4,800 UTF-8 bytes even for Hebrew / emoji; YouTube allows 5,000 bytes
TAGS_CHAR_LIMIT = 450           # YouTube allows 500 characters of tags in total
CHUNK_SIZE = 8 * 1024 * 1024    # must be a multiple of 256 KiB
PRIVACY_STATUSES = ("public", "unlisted", "private")
CATEGORY_SCIENCE_TECH, CATEGORY_PEOPLE_BLOGS = "28", "22"
_TECH_COLLECTIONS = ("gadget", "tech", "electronic", "phone", "computer", "smart", "camera", "audio")

_CLIENT_ID_ENV, _CLIENT_SECRET_ENV, _REFRESH_ENV = "YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN"
_UNVERIFIED_NOTE = ("API projects that have not passed YouTube's API compliance audit can only upload PRIVATE "
                    "videos — request the audit (YouTube API Services form) before relying on public Shorts")

# Error reasons from the YouTube Data API and what to do about them.
_QUOTA_REASONS = {"quotaExceeded", "dailyLimitExceeded"}
_RETRYABLE_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "backendError", "internalError"}
_HINTS = {
    "quotaExceeded": (f"the project's daily YouTube quota is spent (videos.insert costs {INSERT_COST_UNITS} units; "
                      "the default 10,000 units allow 6 uploads a day) — uploads resume after midnight Pacific "
                      "time, or request a quota increase in the Google Cloud console"),
    "dailyLimitExceeded": "the project's daily YouTube quota is spent — uploads resume after midnight Pacific time",
    "uploadLimitExceeded": "the channel reached YouTube's own upload limit — retry in 24 hours",
    "insufficientPermissions": (f"the token lacks the youtube.upload scope — generate a new refresh token "
                                f"requesting {UPLOAD_SCOPE}"),
    "forbidden": "the account may not upload — check the channel exists and is in good standing",
    "youtubeSignupRequired": "the Google account has no YouTube channel — create one, then authorize again",
    "authError": f"the access token was rejected — generate a new refresh token and update {_REFRESH_ENV}",
    "invalidTitle": "the title was rejected (≤ 100 characters, no '<' or '>')",
    "invalidDescription": "the description was rejected (≤ 5,000 bytes, no '<' or '>')",
    "invalidCategoryId": "YOUTUBE_CATEGORY_ID is not a valid video category in this region (try 22 or 28)",
    "invalidVideoMetadata": "the video metadata was rejected — check title, description and tags",
    "mediaBodyRequired": "the upload did not contain the video file",
}


class YouTubeRetryableError(TarshishError):
    """Rate limit, server-side failure or dropped connection: the resumable upload continues."""


# --------------------------------------------------------------------------- #
# Google client libraries (imported lazily so the plugin always registers)
# --------------------------------------------------------------------------- #
def _google() -> Any:
    try:
        import google.auth.exceptions  # noqa: F401
        import google_auth_httplib2
        import httplib2
        from google.oauth2.credentials import Credentials
        from googleapiclient import discovery, errors, http as gapi_http
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ConfigError(
            f"youtube_pub needs the Google API client libraries ({exc}) — "
            "run: python -m pip install -r requirements-engine.txt"
        ) from None

    class _Lib:
        pass

    lib = _Lib()
    lib.Credentials, lib.discovery, lib.errors, lib.gapi_http = Credentials, discovery, errors, gapi_http
    lib.AuthorizedHttp, lib.AuthRequest = google_auth_httplib2.AuthorizedHttp, google_auth_httplib2.Request
    lib.httplib2, lib.RefreshError = httplib2, google.auth.exceptions.RefreshError
    return lib


def _base_http(factory: Callable[[], Any] | None) -> Any:
    if factory is not None:
        return factory()
    return _google().httplib2.Http(timeout=120)


def refresh_hint(message: str) -> str:
    text = message.lower()
    if "invalid_grant" in text:
        return (f"the refresh token was revoked or expired — generate a new one and update {_REFRESH_ENV}. "
                "Refresh tokens of an OAuth consent screen still in 'Testing' expire after 7 days: "
                "set the publishing status to 'In production' for a long-lived token")
    if "invalid_client" in text or "unauthorized_client" in text:
        return (f"{_CLIENT_ID_ENV} / {_CLIENT_SECRET_ENV} do not match the OAuth client that issued the "
                "refresh token — use the same client for both")
    return "the token could not be refreshed — check the network and the three YOUTUBE_* variables"


def _credentials(client_id: str | None, client_secret: str | None, refresh_token: str | None) -> Any:
    return _google().Credentials(
        token=None, refresh_token=refresh_token, client_id=client_id, client_secret=client_secret,
        token_uri=TOKEN_URI, scopes=[UPLOAD_SCOPE],
    )


def _refresh(credentials: Any, http: Any) -> None:
    lib = _google()
    try:
        credentials.refresh(lib.AuthRequest(http))
    except lib.RefreshError as exc:
        detail = redact(str(exc))
        raise NonRetryableError(f"YouTube token refresh failed: {detail} → {refresh_hint(detail)}") from None
    except (OSError, lib.httplib2.HttpLib2Error) as exc:
        raise YouTubeRetryableError(redact(f"YouTube token refresh failed: {type(exc).__name__}: {exc}")) from None
    register_secret(credentials.token)


def error_reason(exc: Any) -> tuple[int, str, str]:
    """``(status, reason, message)`` of a googleapiclient ``HttpError``."""
    status = int(getattr(getattr(exc, "resp", None), "status", 0) or 0)
    reason, message = "", ""
    try:
        body = json.loads(exc.content.decode("utf-8") if isinstance(exc.content, bytes) else exc.content)
        error = body.get("error") or {}
        message = str(error.get("message") or "")
        details = error.get("errors") or []
        if details:
            reason = str(details[0].get("reason") or "")
        if not reason and isinstance(error.get("status"), str):
            reason = error["status"]
    except (ValueError, AttributeError, TypeError):
        message = str(getattr(exc, "reason", "") or "")
    if status == 401 and not reason:
        reason = "authError"
    return status, reason, redact(message or f"HTTP {status}")


# --------------------------------------------------------------------------- #
# Quota bookkeeping (per OAuth client, per Pacific day, per process)
# --------------------------------------------------------------------------- #
def _quota_day(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(QUOTA_TIMEZONE).strftime("%Y-%m-%d")


def next_quota_reset(now: datetime | None = None) -> datetime:
    local = (now or datetime.now(timezone.utc)).astimezone(QUOTA_TIMEZONE)
    midnight = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(timezone.utc)


class _QuotaLedger:
    def __init__(self) -> None:
        self.spent: dict[tuple[str, str], int] = {}
        self.exhausted: dict[str, str] = {}

    def reset(self) -> None:
        self.spent.clear()
        self.exhausted.clear()

    def units(self, client_id: str) -> int:
        return self.spent.get((client_id, _quota_day()), 0)

    def charge(self, client_id: str, units: int) -> int:
        key = (client_id, _quota_day())
        self.spent[key] = self.spent.get(key, 0) + units
        return self.spent[key]

    def mark_exhausted(self, client_id: str) -> None:
        self.exhausted[client_id] = _quota_day()

    def is_exhausted(self, client_id: str) -> bool:
        return self.exhausted.get(client_id) == _quota_day()


quota_ledger = _QuotaLedger()


def daily_quota(settings: Any) -> int:
    raw = settings.get_env("YOUTUBE_DAILY_QUOTA_UNITS", str(DEFAULT_DAILY_QUOTA)) or str(DEFAULT_DAILY_QUOTA)
    try:
        return max(INSERT_COST_UNITS, int(raw))
    except ValueError:
        logger.warning("YOUTUBE_DAILY_QUOTA_UNITS=%r is not a number; using %d.", raw, DEFAULT_DAILY_QUOTA)
        return DEFAULT_DAILY_QUOTA


# --------------------------------------------------------------------------- #
# Publisher
# --------------------------------------------------------------------------- #
class YouTubeShortsPublisher(BasePublisher):
    required_credentials = ("token", "client_id", "client_secret")
    default_env_vars = {"token": _REFRESH_ENV, "client_id": _CLIENT_ID_ENV, "client_secret": _CLIENT_SECRET_ENV}
    account_credential = "channel_id"   # optional: verification checks the token belongs to this channel
    inline_credentials = {"client_id": "client_id", "channel_id": "channel_id"}
    _http_factory: Callable[[], Any] | None = None  # test hook: returns an httplib2.Http-like object
    chunk_size: int = CHUNK_SIZE

    def __init__(self, context: PluginContext, target: PublishTarget) -> None:
        super().__init__(context, target)
        for name in ("token", "client_secret"):
            register_secret(target.credential(name))
        self._privacy = self._privacy_status()
        self._category_override = str(
            target.options.get("youtube_category_id") or context.settings.get_env("YOUTUBE_CATEGORY_ID", "") or ""
        ).strip() or None

    @property
    def platform_name(self) -> str:
        return "youtube"

    def _privacy_status(self) -> str:
        value = str(self.context.settings.get_env("YOUTUBE_PRIVACY_STATUS", "public") or "public").strip().lower()
        if value not in PRIVACY_STATUSES:
            raise ConfigError(f"YOUTUBE_PRIVACY_STATUS={value!r} is not one of: {', '.join(PRIVACY_STATUSES)}")
        return value

    # -- contract -------------------------------------------------------------------- #
    def publish(self, content: GeneratedContent) -> PublishReceipt:
        require_video(content, "YouTube Shorts")
        body = self.build_metadata(content)

        if self.context.dry_run or self.target.options.get("dry_run"):
            self._log_simulation(body, content, "dry-run")
            return PublishReceipt(
                platform=self.platform_name, platform_post_id=f"dry-run-{uuid.uuid4().hex[:10]}", success=True
            )

        client_id = self.target.credential("client_id") or ""
        if quota_ledger.is_exhausted(client_id):
            raise NonRetryableError(
                f"YouTube upload skipped: the daily quota is exhausted until "
                f"{next_quota_reset():%Y-%m-%d %H:%M} UTC → {_HINTS['quotaExceeded']}"
            )
        limit = daily_quota(self.context.settings)
        if quota_ledger.units(client_id) + INSERT_COST_UNITS > limit:
            raise NonRetryableError(
                f"YouTube upload skipped: this process already spent {quota_ledger.units(client_id)} of "
                f"{limit} quota units today (each upload costs {INSERT_COST_UNITS}) — raise "
                "YOUTUBE_DAILY_QUOTA_UNITS if Google granted the project more"
            )

        with local_video(content, "YouTube Shorts") as path:
            info = probe_video(path)
            check_short_form(info, "YouTube Shorts", SHORTS_MAX_SECONDS)
            logger.info(
                "Uploading a Short to %s (%s, %s, ≈%d quota units).", self.target.page_name,
                info.describe() if info else path.name, self._privacy, INSERT_COST_UNITS,
            )
            response = self._upload(path, body)

        video_id = str(response.get("id") or "")
        if not video_id:
            raise NonRetryableError(f"YouTube accepted the upload but returned no video id: {redact(str(response))}")
        privacy = (response.get("status") or {}).get("privacyStatus")
        if privacy == "private" and self._privacy != "private":
            logger.warning("YouTube stored video %s as PRIVATE although %s was requested: %s.",
                           video_id, self._privacy, _UNVERIFIED_NOTE)
        logger.info("YouTube Short published to %s: https://youtube.com/shorts/%s", self.target.page_name, video_id)
        return PublishReceipt(platform=self.platform_name, platform_post_id=video_id, success=True)

    # -- metadata ------------------------------------------------------------------------ #
    def category_id(self, content: GeneratedContent) -> str:
        if self._category_override:
            return self._category_override
        collection = content.product.category.lower()
        return CATEGORY_SCIENCE_TECH if any(word in collection for word in _TECH_COLLECTIONS) else CATEGORY_PEOPLE_BLOGS

    def build_metadata(self, content: GeneratedContent) -> dict[str, Any]:
        """The ``videos.insert`` body: Shorts title/description, tags, category and privacy."""
        return {
            "snippet": {
                "title": shorts_title(content.caption or content.product.title),
                "description": self.render_description(content),
                "tags": _tags(content.hashtags),
                "categoryId": self.category_id(content),
                "defaultLanguage": content.language,
                "defaultAudioLanguage": content.language,
            },
            "status": {
                "privacyStatus": self._privacy,
                "selfDeclaredMadeForKids": False,
                "embeddable": True,
            },
        }

    def render_description(self, content: GeneratedContent) -> str:
        text = render_post_text(content, max_length=DESCRIPTION_UNITS, disclosure=disclosure_enabled(self.context.settings))
        text = _strip_angle_brackets(text)
        if SHORTS_TAG.lower() not in text.lower():
            text = f"{text}\n\n{SHORTS_TAG}"
        return text

    def _log_simulation(self, body: dict[str, Any], content: GeneratedContent, reason: str) -> None:
        snippet, status = body["snippet"], body["status"]
        logger.info(
            "YouTube Short simulated (%s) for %s — nothing was uploaded.\n  video  : %s\n  title  : %s\n"
            "  privacy: %s · category %s · tags %s\n  description:\n%s",
            reason, self.target.page_name, video_reference(content) or "none (render job only)", snippet["title"],
            status["privacyStatus"], snippet["categoryId"], ", ".join(snippet["tags"]) or "none", snippet["description"],
        )

    # -- transport --------------------------------------------------------------------- #
    def _upload(self, path: Path, body: dict[str, Any]) -> dict[str, Any]:
        lib = _google()
        http = _base_http(type(self)._http_factory)  # via the class: a plain function, not a bound method
        credentials = _credentials(
            self.target.credential("client_id"), self.target.credential("client_secret"), self.target.credential("token"),
        )
        with_retry(max_attempts=3, base_delay=2.0)(_refresh)(credentials, http)
        youtube = lib.discovery.build(
            "youtube", "v3", http=lib.AuthorizedHttp(credentials, http=http), cache_discovery=False, static_discovery=True,
        )
        media = lib.gapi_http.MediaFileUpload(str(path), mimetype="video/mp4", chunksize=self.chunk_size, resumable=True)
        request = youtube.videos().insert(part="snippet,status", body=body, media_body=media, notifySubscribers=True)
        client_id = self.target.credential("client_id") or ""
        quota_ledger.charge(client_id, INSERT_COST_UNITS)  # Google charges for the insert even if it later fails
        response: dict[str, Any] | None = None
        while response is None:
            status, response = self._next_chunk(request)
            if status is not None:
                logger.debug("YouTube upload %d%% complete.", int(status.progress() * 100))
        return response

    @with_retry(max_attempts=5, base_delay=2.0, max_delay=60.0)
    def _next_chunk(self, request: Any) -> tuple[Any, dict[str, Any] | None]:
        """Send the next chunk. After a failure the library first asks YouTube how much arrived, then resumes."""
        lib = _google()
        try:
            return request.next_chunk()
        except lib.errors.HttpError as exc:
            raise self._map_error(exc) from None
        except (OSError, lib.httplib2.HttpLib2Error) as exc:
            raise YouTubeRetryableError(redact(f"YouTube upload interrupted: {type(exc).__name__}: {exc}")) from None

    def _map_error(self, exc: Any) -> Exception:
        status, reason, message = error_reason(exc)
        text = f"YouTube videos.insert failed ({status}{', ' + reason if reason else ''}): {message}"
        hint = _HINTS.get(reason)
        if hint:
            text += f" → {hint}"
        if reason in _QUOTA_REASONS:
            quota_ledger.mark_exhausted(self.target.credential("client_id") or "")
            logger.error("%s (next reset %s UTC).", text, f"{next_quota_reset():%Y-%m-%d %H:%M}")
            return NonRetryableError(text)
        if status == 429 or status >= 500 or reason in _RETRYABLE_REASONS:
            return YouTubeRetryableError(text)
        return NonRetryableError(text)


def shorts_title(caption: str) -> str:
    """First line of the caption, cleaned and cut so that ``<title> #Shorts`` fits YouTube's 100 characters."""
    line = next((part.strip() for part in caption.splitlines() if part.strip()), "") or "Deal of the day"
    line = _strip_angle_brackets(re.sub(r"\s+", " ", line))
    if SHORTS_TAG.lower() in line.lower():
        return line[:TITLE_LIMIT].rstrip()
    room = TITLE_LIMIT - len(SHORTS_TAG) - 1
    if len(line) > room:
        line = line[: room - 1].rstrip() + "…"
    return f"{line} {SHORTS_TAG}"


def _strip_angle_brackets(text: str) -> str:
    return text.replace("<", "‹").replace(">", "›")  # YouTube rejects '<' and '>' in titles and descriptions


def _tags(hashtags: Iterable[str]) -> list[str]:
    tags: list[str] = []
    total = len("Shorts")
    for tag in hashtags:
        clean = _strip_angle_brackets(tag.replace("_", " ")).strip()
        if not clean or clean.lower() == "shorts":
            continue
        cost = len(clean) + (2 if " " in clean else 0) + 1
        if total + cost > TAGS_CHAR_LIMIT:
            break
        tags.append(clean)
        total += cost
    return tags + ["Shorts"]


# --------------------------------------------------------------------------- #
# Live verification (read-only)
# --------------------------------------------------------------------------- #
@dataclass
class YouTubeVerification:
    """Outcome of :func:`verify_connection`. Never contains a secret."""

    page_name: str
    token_valid: bool = False
    upload_scope: bool | None = None
    channel_title: str | None = None
    channel_id: str | None = None
    quota_note: str = ""
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.token_valid and bool(self.upload_scope) and not self.problems

    def summary(self) -> str:
        who = f"Channel {self.channel_title}" if self.channel_title else (
            "Token refreshed" if self.token_valid else "Not connected")
        scope = {True: "✓", False: "✗", None: "?"}[self.upload_scope]
        return f"{who} | Scope: {scope} youtube.upload"

    def lines(self) -> list[str]:
        mark = lambda ok: "✓" if ok else "✗"  # noqa: E731
        rows = [
            f"{mark(self.token_valid)} OAuth token    : " + ("refresh token exchanged for an access token"
                                                            if self.token_valid else "not refreshed"),
            f"{mark(bool(self.upload_scope))} Upload scope   : "
            + {True: "youtube.upload granted", False: "youtube.upload NOT granted", None: "unknown"}[self.upload_scope],
            f"{mark(self.token_valid)} Channel        : "
            + (f"{self.channel_title} ({self.channel_id})" if self.channel_title else
               "not readable with an upload-only token (fine for publishing)" if self.token_valid else "not checked"),
        ]
        if self.quota_note:
            rows.append(f"· Quota          : {self.quota_note}")
        rows += [f"✗ {problem}" for problem in self.problems]
        rows += [f"· {note}" for note in self.notes]
        return rows


def verify_connection(
    settings: Any, client_id: str | None, client_secret: str | None, refresh_token: str | None, *,
    page_name: str = "default", channel_id: str | None = None, http_factory: Callable[[], Any] | None = None,
) -> YouTubeVerification:
    """Refresh the token, check the youtube.upload scope and (when the scopes allow) read the channel.

    Read-only: the OAuth token endpoint, ``tokeninfo`` and ``channels.list`` (1 quota unit).
    """
    result = YouTubeVerification(page_name=page_name)
    missing = [name for name, value in ((_CLIENT_ID_ENV, client_id), (_CLIENT_SECRET_ENV, client_secret),
                                        (_REFRESH_ENV, refresh_token)) if not value]
    if missing:
        result.problems.append(f"not configured: {' and '.join(missing)} missing in .env")
        return result
    for secret in (client_secret, refresh_token):
        register_secret(secret)
    try:
        lib = _google()
    except ConfigError as exc:
        result.problems.append(str(exc))
        return result
    factory = http_factory if http_factory is not None else YouTubeShortsPublisher._http_factory
    http = _base_http(factory)
    credentials = _credentials(client_id, client_secret, refresh_token)
    try:
        _refresh(credentials, http)
        result.token_valid = True
    except TarshishError as exc:
        result.problems.append(redact(str(exc)))
        return result

    # Granted scopes (POST keeps the access token out of the URL).
    try:
        response, raw = http.request(
            TOKENINFO_URI, "POST", body=urlencode({"access_token": credentials.token}),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        info = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw or "{}")
        if int(response.status) >= 400:
            raise ValueError(info.get("error_description") or f"HTTP {response.status}")
        scopes = set(str(info.get("scope") or "").split())
        result.upload_scope = UPLOAD_SCOPE in scopes or "https://www.googleapis.com/auth/youtube" in scopes
        if not result.upload_scope:
            result.problems.append(f"the refresh token was granted without youtube.upload → {_HINTS['insufficientPermissions']}")
    except (ValueError, OSError, lib.httplib2.HttpLib2Error) as exc:
        result.notes.append(f"could not read the granted scopes ({redact(str(exc))})")

    # The channel: needs a youtube / youtube.readonly scope, which an upload-only token does not have.
    try:
        youtube = lib.discovery.build(
            "youtube", "v3", http=lib.AuthorizedHttp(credentials, http=http), cache_discovery=False, static_discovery=True,
        )
        items = youtube.channels().list(part="snippet", mine=True).execute().get("items") or []
        if items:
            result.channel_id = str(items[0].get("id") or "")
            result.channel_title = str((items[0].get("snippet") or {}).get("title") or result.channel_id)
            if channel_id and result.channel_id and result.channel_id != channel_id:
                result.problems.append(
                    f"the token belongs to channel {result.channel_id}, not {channel_id} — authorize with the right "
                    "Google account / brand channel"
                )
        else:
            result.problems.append(f"the Google account has no YouTube channel → {_HINTS['youtubeSignupRequired']}")
    except lib.errors.HttpError as exc:
        status, reason, message = error_reason(exc)
        if reason in ("insufficientPermissions", "forbidden", "ACCESS_TOKEN_SCOPE_INSUFFICIENT") or status == 403:
            result.notes.append("channel details need the youtube.readonly scope; the upload-only token is enough to publish")
        else:
            result.notes.append(f"channels.list failed ({status} {reason}): {message}")
    except (OSError, lib.httplib2.HttpLib2Error) as exc:
        result.notes.append(f"channels.list failed: {redact(str(exc))}")

    spent = quota_ledger.units(client_id or "")
    limit = daily_quota(settings)
    result.quota_note = (f"{INSERT_COST_UNITS} units per upload, {limit} a day → about {limit // INSERT_COST_UNITS} "
                         f"Shorts a day ({spent} spent by this process today)")
    result.notes.append(_UNVERIFIED_NOTE)
    return result


def verify_pages(registry: Any, pages: Iterable[Any], *, http_factory: Callable[[], Any] | None = None) -> list[YouTubeVerification]:
    """Resolve each ``youtube_pub`` social page's env vars and verify it (read-only)."""
    results: list[YouTubeVerification] = []
    for page in pages:
        if page.platform != PLUGIN_NAME:
            continue
        refs = {**YouTubeShortsPublisher.default_env_vars, **page.env_refs()}
        target, reason = registry.resolve_target(page)
        if target is None:
            results.append(YouTubeVerification(
                page_name=page.page_name,
                problems=[f"not configured: {reason or 'credentials missing'} (expects {refs['client_id']}, "
                          f"{refs['client_secret']} and {refs['token']} in .env)"],
            ))
            continue
        results.append(verify_connection(
            registry.settings, target.credential("client_id"), target.credential("client_secret"),
            target.credential("token"), page_name=page.page_name, channel_id=target.credential("channel_id"),
            http_factory=http_factory,
        ))
    return results


def pages_to_verify(registry: Any, category: str | None = None, language: str | None = None) -> list[Any]:
    """The YouTube channels of the social-accounts matrix (one check per refresh-token variable)."""
    from core.config import SocialPage

    pages: dict[str, Any] = {}
    for account in registry.list_social_accounts(category=category, language=language, platform="youtube"):
        page = registry.social_page(account)
        refs = {**YouTubeShortsPublisher.default_env_vars, **page.env_refs()}
        pages.setdefault(refs["token"], page)
    if not pages and category is None and language is None:
        return [SocialPage(platform=PLUGIN_NAME, page_name="default")]
    return list(pages.values())


# --------------------------------------------------------------------------- #
# One-off: generate a refresh token (needs a browser)
# --------------------------------------------------------------------------- #
def generate_refresh_token(client_secrets_file: str | Path, *, port: int = 0) -> str:
    """Run Google's installed-app consent flow and return a long-lived refresh token."""
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ConfigError(f"google-auth-oauthlib is not installed ({exc}): pip install -r requirements-engine.txt") from None
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets_file), scopes=[UPLOAD_SCOPE])
    # access_type=offline + prompt=consent: Google only returns a refresh token on a fresh consent.
    credentials = flow.run_local_server(port=port, access_type="offline", prompt="consent", open_browser=True)
    if not credentials.refresh_token:
        raise ConfigError("Google returned no refresh token — revoke the app's access at "
                          "myaccount.google.com/permissions and run the flow again")
    return str(credentials.refresh_token)


def _main(argv: list[str]) -> int:  # pragma: no cover - interactive
    import argparse

    parser = argparse.ArgumentParser(description="Generate a YouTube Data API refresh token (youtube.upload scope).")
    parser.add_argument("--authorize", metavar="CLIENT_SECRET_JSON", required=True,
                        help="the OAuth client file downloaded from Google Cloud console > Credentials")
    parser.add_argument("--port", type=int, default=0, help="local redirect port (default: any free port)")
    args = parser.parse_args(argv)
    token = generate_refresh_token(args.authorize, port=args.port)
    data = json.loads(Path(args.authorize).read_text(encoding="utf-8"))
    client = data.get("installed") or data.get("web") or {}
    print("\nAdd these lines to .env (keep them secret, never commit them):\n")
    print(f"{_CLIENT_ID_ENV}={client.get('client_id', '<client id>')}")
    print(f"{_CLIENT_SECRET_ENV}=<client secret from {Path(args.authorize).name}>")
    print(f"{_REFRESH_ENV}={token}\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_main(sys.argv[1:]))
