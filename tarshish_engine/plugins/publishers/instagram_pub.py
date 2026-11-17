"""Instagram publisher: Meta's Instagram API with Unified Login, two-step container flow.

Host and authentication
-----------------------
Every request goes to ``https://graph.facebook.com/v21.0`` — the host that serves the
Instagram API with Unified Login. The token (a Business Integration / System User
access token from a Meta Business Portfolio; no Facebook Page is needed) travels in an
``Authorization: Bearer`` header, never in a URL or a form body.

Publishing formats
------------------
1. ``POST /{ig_user_id}/media``          create a media container
2. ``GET  /{creation_id}?fields=status_code``
                                         poll with exponential backoff until ``FINISHED``
3. ``POST /{ig_user_id}/media_publish``  publish ``creation_id``; the reply is the live media id

==============  =================================================  =======================
format          container fields                                   caption
==============  =================================================  =======================
image / post    ``image_url``                                      yes (+ link-in-bio CTA)
video / reel    ``media_type=REELS`` + ``video_url``               yes (+ link-in-bio CTA)
story           ``media_type=STORIES`` + ``image_url``/``video_url``  **not accepted by Meta**
==============  =================================================  =======================

A social page picks its format with the non-secret ``content_type`` option; without it
the format follows the generated content (video ➔ Reel, anything else ➔ image post).
To post the same product as a feed post *and* a Story, list two pages::

    {"platform": "instagram_pub", "page_name": "gadgets",
     "token_env_var": "INSTAGRAM_ACCESS_TOKEN", "account_id_env_var": "INSTAGRAM_BUSINESS_ACCOUNT_ID"}
    {"platform": "instagram_pub", "page_name": "gadgets_story", "content_type": "story",
     "token_env_var": "INSTAGRAM_ACCESS_TOKEN", "account_id_env_var": "INSTAGRAM_BUSINESS_ACCOUNT_ID"}

Pages without explicit references fall back to ``INSTAGRAM_ACCESS_TOKEN`` /
``INSTAGRAM_BUSINESS_ACCOUNT_ID``. ``INSTAGRAM_GRAPH_API_VERSION`` (default ``v21.0``)
selects the Graph API version.

Instagram captions cannot carry a clickable link, so feed posts and Reels end with a call
to action that points at the storefront collection linked in the account's bio. Meta
downloads the media itself: the image or video must be a public HTTPS URL. A product
that only has a stand-in picture is refused rather than posted.

With ``--dry-run`` (or without credentials) nothing is sent: the exact payload is logged
instead. Set ``DRY_RUN_PUBLISHER=instagram_pub`` to preview dry-runs through this publisher.

:func:`verify_connection` (``python main.py --verify-instagram``) checks the token, the
account type and the publishing permissions with read-only calls; it never posts.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

import httpx

from core.database import category_slug
from core.interfaces import BasePublisher, PluginContext
from core.models import GeneratedContent, PublishReceipt, PublishTarget
from core.resilience import (
    ConfigError, NonRetryableError, TarshishError, get_logger, redact, register_secret, with_retry,
)
from plugins import is_placeholder_image
from plugins.publishers import disclosure_enabled, render_post_text

logger = get_logger("plugins.instagram_pub")

PLUGIN_NAME = "instagram_pub"
GRAPH_BASE = "https://graph.facebook.com"
DEFAULT_API_VERSION = "v21.0"
CAPTION_LIMIT = 2200
HASHTAG_LIMIT = 30
_CALL_TO_ACTION = {"he": "🔗 הקישור בביו ← {path}", "en": "🔗 Link in bio -> {path}"}

# Publishing formats a social page can request with its ``content_type`` option.
FORMAT_IMAGE, FORMAT_REEL, FORMAT_STORY = "image", "reel", "story"
_FORMAT_ALIASES = {
    "image": FORMAT_IMAGE, "post": FORMAT_IMAGE, "feed": FORMAT_IMAGE,
    "video": FORMAT_REEL, "reel": FORMAT_REEL, "reels": FORMAT_REEL,
    "story": FORMAT_STORY, "stories": FORMAT_STORY,
}
_FORMAT_LABELS = {FORMAT_IMAGE: "image post", FORMAT_REEL: "Reel", FORMAT_STORY: "Story"}

# Permissions a Unified Login token needs, with the Facebook Login names Meta treats as equivalent.
REQUIRED_PERMISSIONS: dict[str, tuple[str, ...]] = {
    "instagram_business_basic": ("instagram_business_basic", "instagram_basic"),
    "instagram_business_content_publish": ("instagram_business_content_publish", "instagram_content_publish"),
}
PROFESSIONAL_ACCOUNT_TYPES = ("BUSINESS", "MEDIA_CREATOR")

# Graph API error codes that describe a temporary condition (service hiccup, rate limit).
_TRANSIENT_CODES = {1, 2, 4, 17, 32, 613}
# Sub-codes that look transient but are not worth retrying within this run.
_FINAL_SUBCODES = {2207042}  # the account reached its 24-hour publishing limit
_CONNECT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout)
_TOKEN_ENV = "INSTAGRAM_ACCESS_TOKEN"
_ACCOUNT_ENV = "INSTAGRAM_BUSINESS_ACCOUNT_ID"
_PERMISSIONS_TEXT = "instagram_business_basic and instagram_business_content_publish"


class InstagramRetryableError(TarshishError):
    """Rate limit or server-side failure."""


class _StillProcessing(TarshishError):
    """Meta has not finished fetching / transcoding the media yet."""


# --------------------------------------------------------------------------- #
# Graph API helpers shared by the publisher and the verification check
# --------------------------------------------------------------------------- #
def graph_api_version(settings: Any) -> str:
    version = settings.get_env("INSTAGRAM_GRAPH_API_VERSION", DEFAULT_API_VERSION) or DEFAULT_API_VERSION
    if not re.fullmatch(r"v\d{1,3}\.\d", version):
        logger.warning(
            "INSTAGRAM_GRAPH_API_VERSION=%r is not a version such as 'v21.0'; using %s.", version, DEFAULT_API_VERSION
        )
        return DEFAULT_API_VERSION
    return version


def error_hint(code: Any, subcode: Any, message: str = "") -> str | None:
    """An actionable next step for a Graph API error, or ``None`` when there is nothing specific to say."""
    text = message.lower()
    if code == 190:
        if subcode == 463 or "expired" in text:
            return (f"the access token has expired — generate a new long-lived (or System User) token "
                    f"and update {_TOKEN_ENV} in .env")
        if subcode == 460:
            return f"the token was invalidated by a password change — generate a new token and update {_TOKEN_ENV}"
        return f"the access token is invalid or was revoked — generate a new token and update {_TOKEN_ENV}"
    if code == 10 or (isinstance(code, int) and 200 <= code <= 299):
        return (f"the token lacks a permission — grant {_PERMISSIONS_TEXT} in the Business Integration "
                "configuration (or to the System User), then generate a new token")
    if code == 3:
        return "the app cannot call this endpoint — add the 'Instagram API with Unified Login' product to the app"
    if code == 100 and subcode == 33:
        return (f"the Instagram account was not found or the token cannot see it — check {_ACCOUNT_ENV} "
                "and that the account belongs to the token's Business Portfolio")
    if subcode == 2207042:
        return "the account reached Instagram's 24-hour publishing limit — the post can be retried tomorrow"
    if code == 9004 or subcode in (2207052, 2207003, 2207020):
        return ("Meta could not download the media — the image_url / video_url must be a public HTTPS URL "
                "that answers without login, cookies or redirects to a login page")
    if code == 36003 or subcode == 2207009:
        return "the media's aspect ratio is not accepted (feed images 4:5 to 1.91:1; Reels and Stories 9:16)"
    if code == 36001 or subcode == 2207004:
        return "the image is too large (Instagram accepts JPEG up to 8 MB)"
    if subcode == 2207026:
        return "the video format is not supported (use MP4/MOV, H.264 + AAC, 3–90 s for Reels, up to 60 s for Stories)"
    if code == 9007 or subcode == 2207027:
        return "the media is not ready yet — wait for the container to reach FINISHED before publishing"
    if code == 24 or subcode == 2207006:
        return "the media container was not found or expired (containers expire after 24 hours) — create a new one"
    if code in _TRANSIENT_CODES:
        return "Meta is rate limiting or temporarily unavailable — the call is retried with backoff"
    return None


def interpret_response(action: str, response: httpx.Response) -> dict[str, Any]:
    """Return the JSON body of a successful Graph API reply; map failures onto retryable / fatal errors."""
    try:
        body = response.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    error = body.get("error") if isinstance(body.get("error"), dict) else None
    if response.status_code < 300 and error is None:
        return body

    error = error or {}
    code, subcode = error.get("code"), error.get("error_subcode")
    description = redact(str(error.get("error_user_msg") or error.get("message") or f"HTTP {response.status_code}"))
    codes = f"code {code}" + (f"/{subcode}" if subcode else "")
    message = f"Instagram {action} failed ({response.status_code}, {codes}): {description}"
    hint = error_hint(code, subcode, str(error.get("message") or ""))
    if hint:
        message += f" → {hint}"
    if subcode in _FINAL_SUBCODES:
        raise NonRetryableError(message)
    if response.status_code == 429 or response.status_code >= 500 or error.get("is_transient") or code in _TRANSIENT_CODES:
        raise InstagramRetryableError(message)
    raise NonRetryableError(message)  # expired token, missing permission, media Meta cannot fetch…


def _request(
    client: httpx.Client, version: str, token: str | None, method: str, path: str, *,
    data: dict[str, str] | None = None, params: dict[str, str] | None = None, publishing: bool = False,
    action: str | None = None,
) -> dict[str, Any]:
    # The token travels in a header, so it never appears in a URL, a form body or an httpx error.
    headers = {"Authorization": f"Bearer {token}"}
    action = action or (path.rsplit("/", 1)[-1] if "/" in path else "status check")
    try:
        response = client.request(method, f"{GRAPH_BASE}/{version}/{path}", data=data, params=params, headers=headers)
    except httpx.HTTPError as exc:
        text = redact(f"Instagram {action} failed: {type(exc).__name__}: {exc}")
        if publishing and not isinstance(exc, _CONNECT_ERRORS):
            # The request may have reached Meta: publishing again could post twice.
            raise NonRetryableError(f"{text} — outcome unknown, check the account before retrying") from None
        raise InstagramRetryableError(text) from None
    return interpret_response(action, response)


def _client(transport: Any) -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(60.0, connect=10.0), transport=transport)


# --------------------------------------------------------------------------- #
# Publisher
# --------------------------------------------------------------------------- #
class InstagramPublisher(BasePublisher):
    required_credentials = ("token", "account_id")
    default_env_vars = {"token": _TOKEN_ENV, "account_id": _ACCOUNT_ENV}
    _transport: Any = None  # test hook: an httpx.MockTransport instead of the network

    def __init__(self, context: PluginContext, target: PublishTarget) -> None:
        super().__init__(context, target)
        register_secret(target.credential("token"))  # Graph error messages can quote the token back
        self._version = graph_api_version(context.settings)
        self._format = self._configured_format(target.options.get("content_type"))
        self._story_reminder_logged = False

    @property
    def platform_name(self) -> str:
        return "instagram"

    @staticmethod
    def _configured_format(value: Any) -> str | None:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        fmt = _FORMAT_ALIASES.get(str(value).strip().lower())
        if fmt is None:
            raise ConfigError(
                f"instagram_pub: content_type {value!r} is not supported; use one of: image, post, video, reel, story"
            )
        return fmt

    # -- contract -------------------------------------------------------------------- #
    def publish(self, content: GeneratedContent) -> PublishReceipt:
        fmt = self.format_for(content)
        media, problem = self._media(content, fmt)
        payload = dict(media)
        if fmt != FORMAT_STORY:  # Meta rejects a caption on a Story container
            payload["caption"] = self.render_caption(content)
        token, account_id = self.target.credential("token"), self.target.credential("account_id")

        if self.context.dry_run or self.target.options.get("dry_run"):
            self._log_simulation(fmt, payload, "dry-run", problem, content)
            return PublishReceipt(
                platform=self.platform_name, platform_post_id=f"dry-run-{uuid.uuid4().hex[:10]}", success=True
            )
        if not token or not account_id:
            self._log_simulation(fmt, payload, "credentials are missing", problem, content)
            return PublishReceipt(
                platform=self.platform_name, success=False,
                error_message="Instagram credentials are not configured (access token and business account id); "
                              "the post was simulated in the log and nothing was published.",
            )
        if problem:
            raise NonRetryableError(f"Instagram needs public media: {problem}")
        if fmt == FORMAT_STORY:
            self._remind_story_text()

        with _client(self._transport) as client:
            creation_id = str(self._call(client, "POST", f"{account_id}/media", data=payload)["id"])
            self._await_container(client, creation_id)
            published = self._call(
                client, "POST", f"{account_id}/media_publish", data={"creation_id": creation_id}, publishing=True
            )
        logger.info("Instagram %s published to %s (media id %s).", _FORMAT_LABELS[fmt], self.target.page_name, published["id"])
        return PublishReceipt(platform=self.platform_name, platform_post_id=str(published["id"]), success=True)

    # -- payload ----------------------------------------------------------------------- #
    def format_for(self, content: GeneratedContent) -> str:
        """The page's configured format, or the one the generated content suggests."""
        if self._format:
            return self._format
        return FORMAT_REEL if content.content_type == "video" else FORMAT_IMAGE

    def render_caption(self, content: GeneratedContent) -> str:
        """Caption, link-in-bio call to action, hashtags and the affiliate disclosure."""
        lang = content.language.split("-")[0]
        path = f"/c/{category_slug(content.product.category)}"
        call_to_action = _CALL_TO_ACTION.get(lang, _CALL_TO_ACTION["en"]).format(path=path)
        trimmed = content.model_copy(update={"hashtags": content.hashtags[:HASHTAG_LIMIT]})
        return render_post_text(
            trimmed, max_length=CAPTION_LIMIT, disclosure=disclosure_enabled(self.context.settings),
            call_to_action=call_to_action,
        )

    def _media(self, content: GeneratedContent, fmt: str) -> tuple[dict[str, str], str | None]:
        """Container fields for the requested format, or the reason no usable media exists."""
        product = content.product
        public = content.media_public_url
        is_video = content.content_type == "video"
        video = (public or product.video_url) if is_video else None
        if fmt == FORMAT_REEL:
            if self._format == FORMAT_REEL:
                video = video or product.video_url  # the page asked for Reels: the product's own clip will do
            if video:
                return {"media_type": "REELS", "video_url": video}, None
            if self._format == FORMAT_REEL:
                return {"media_type": "REELS"}, "the page publishes Reels but the post has no public video URL"
        if fmt == FORMAT_STORY and video:
            return {"media_type": "STORIES", "video_url": video}, None

        # Feed image, a Story image, or an automatic Reel that degrades to the product photo.
        image = (None if is_video else public) or (product.image_urls[0] if product.image_urls else None)
        extra = {"media_type": "STORIES"} if fmt == FORMAT_STORY else {}
        if not image:
            return extra, "the post has no public image or video URL"
        if is_placeholder_image(image):
            return {**extra, "image_url": image}, "the product only has a stand-in picture, no real photo yet"
        if not image.lower().startswith("https://"):
            return {**extra, "image_url": image}, "Meta only downloads media over HTTPS"
        return {**extra, "image_url": image}, None

    def _remind_story_text(self) -> None:
        if not self._story_reminder_logged:
            self._story_reminder_logged = True
            logger.info(
                "Instagram Stories take no caption: any text, price or link-in-bio prompt for %s must be "
                "baked into the image/video itself.", self.target.page_name,
            )

    def _log_simulation(
        self, fmt: str, payload: dict[str, str], reason: str, problem: str | None, content: GeneratedContent
    ) -> None:
        media = payload.get("video_url") or payload.get("image_url") or "none"
        # An automatic Reel without a video degrades to an image post.
        kind = _FORMAT_LABELS[FORMAT_IMAGE if fmt == FORMAT_REEL and "image_url" in payload else fmt]
        note = f"\n  note   : a live run would refuse this post — {problem}" if problem else ""
        if fmt == FORMAT_STORY:
            text = ("  caption: (none — Stories take no caption; bake text into the media)\n"
                    f"  feed caption for reference:\n{self.render_caption(content)}")
        else:
            text = f"  caption:\n{payload['caption']}"
        logger.info(
            "Instagram %s simulated (%s) for %s — nothing was sent.\n  media  : %s%s\n%s",
            kind, reason, self.target.page_name, media, note, text,
        )

    # -- transport ----------------------------------------------------------------------- #
    @with_retry(max_attempts=3, base_delay=2.0)
    def _call(
        self, client: httpx.Client, method: str, path: str, *, data: dict[str, str] | None = None,
        params: dict[str, str] | None = None, publishing: bool = False,
    ) -> dict[str, Any]:
        return _request(
            client, self._version, self.target.credential("token"), method, path,
            data=data, params=params, publishing=publishing,
        )

    @with_retry(max_attempts=12, base_delay=5.0, max_delay=30.0, retry_on=(_StillProcessing,))
    def _await_container(self, client: httpx.Client, creation_id: str) -> None:
        """Poll with exponential backoff until Meta has processed the media (instant for images, minutes for video)."""
        body = self._call(client, "GET", creation_id, params={"fields": "status_code,status"})
        status = str(body.get("status_code", ""))
        if status in ("FINISHED", "PUBLISHED", ""):
            return
        if status == "IN_PROGRESS":
            raise _StillProcessing("Instagram is still processing the media")
        detail = redact(str(body.get("status") or ""))
        subcode = re.search(r"\b(22070\d\d)\b", detail)
        hint = error_hint(None, int(subcode.group(1)) if subcode else None, detail)
        if status == "EXPIRED":
            hint = error_hint(24, None)
        text = f"Instagram could not process the media (container status {status})"
        text += f": {detail}" if detail else ""
        text += f" → {hint}" if hint else ""
        raise NonRetryableError(text)


# --------------------------------------------------------------------------- #
# Live verification (read-only)
# --------------------------------------------------------------------------- #
@dataclass
class InstagramVerification:
    """Outcome of :func:`verify_connection`. Never contains the token."""

    page_name: str
    account_id: str = ""
    username: str | None = None
    account_type: str | None = None
    token_valid: bool = False
    token_expires: str | None = None
    granted: dict[str, bool | None] = field(default_factory=lambda: {name: None for name in REQUIRED_PERMISSIONS})
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def account_ok(self) -> bool:
        return self.account_type in PROFESSIONAL_ACCOUNT_TYPES

    @property
    def permissions_ok(self) -> bool:
        return all(self.granted.values())

    @property
    def ok(self) -> bool:
        return self.token_valid and self.account_ok and self.permissions_ok and not self.problems

    def permission_status(self) -> str:
        marks = {True: "✓", False: "✗", None: "?"}
        return ", ".join(f"{marks[state]} {name}" for name, state in self.granted.items())

    def summary(self) -> str:
        who = f"Connected as @{self.username}" if self.username else "Not connected"
        account = self.account_type or "unknown account type"
        return f"{who} | {account} | Permissions: {self.permission_status()}"

    def lines(self) -> list[str]:
        mark = lambda ok: "✓" if ok else "✗"  # noqa: E731
        token = "valid" if self.token_valid else "invalid or unverified"
        if self.token_valid and self.token_expires:
            token += f", expires {self.token_expires}"
        rows = [
            f"{mark(self.token_valid)} Token          : {token}",
            f"{mark(self.account_ok)} Account        : "
            + (f"@{self.username} ({self.account_id}) · {self.account_type or 'type unknown'}"
               if self.username else (self.account_id or "not reached")),
            f"{mark(self.permissions_ok)} Permissions    : {self.permission_status()}",
        ]
        rows += [f"✗ {problem}" for problem in self.problems]
        rows += [f"· {note}" for note in self.notes]
        return rows


def _expiry_text(value: Any) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    if value == 0:
        return "never (long-lived / System User token)"
    return datetime.fromtimestamp(value, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def verify_connection(
    settings: Any, token: str | None, account_id: str | None, *, page_name: str = "default", transport: Any = None,
) -> InstagramVerification:
    """Check the Unified Login token, the professional account and the publishing permissions.

    Read-only: ``GET /{ig_user_id}``, ``GET /debug_token`` and, as a fallback,
    ``GET /me/permissions``. Nothing is ever posted.
    """
    result = InstagramVerification(page_name=page_name, account_id=account_id or "")
    if not token or not account_id:
        missing = [name for name, value in ((_TOKEN_ENV, token), (_ACCOUNT_ENV, account_id)) if not value]
        result.problems.append(f"not configured: {' and '.join(missing)} missing in .env")
        return result
    register_secret(token)
    version = graph_api_version(settings)
    transport = transport if transport is not None else InstagramPublisher._transport

    def get(client: httpx.Client, path: str, params: dict[str, str], action: str) -> dict[str, Any]:
        return _request(client, version, token, "GET", path, params=params, action=action)

    with _client(transport) as client:
        # 1. The account: proves the token works and that it can see this professional account.
        try:
            try:
                account = get(client, account_id, {"fields": "id,username,account_type"}, "account lookup")
            except NonRetryableError as exc:
                if "account_type" not in str(exc):
                    raise
                # Some versions do not expose account_type on graph.facebook.com; the IG User node
                # itself is only reachable for professional accounts.
                account = get(client, account_id, {"fields": "id,username"}, "account lookup")
                account.setdefault("account_type", "BUSINESS")
                result.notes.append("account_type is not exposed by this API version; "
                                    "the account is professional because its IG User node is reachable")
            result.token_valid = True
            result.username = account.get("username")
            result.account_type = str(account.get("account_type") or "").upper() or None
            result.account_id = str(account.get("id") or account_id)
            if result.account_type and not result.account_ok:
                result.problems.append(
                    f"the account is {result.account_type}: switch it to a Professional account "
                    "(Business or Creator) and link it to a Meta Business Portfolio"
                )
        except TarshishError as exc:
            result.problems.append(redact(str(exc)))

        # 2. The token: validity, expiry and granted scopes.
        scopes: set[str] | None = None
        try:
            data = get(client, "debug_token", {"input_token": token}, "token check").get("data") or {}
            if data.get("is_valid") is False:
                result.token_valid = False
                reason = (data.get("error") or {}).get("message") or "Meta reports the token as invalid"
                result.problems.append(f"{redact(str(reason))} → {error_hint(190, None, str(reason))}")
            else:
                result.token_valid = result.token_valid or bool(data.get("is_valid"))
                result.token_expires = _expiry_text(data.get("expires_at"))
            if isinstance(data.get("scopes"), list):
                scopes = {str(scope) for scope in data["scopes"]}
        except TarshishError as exc:
            result.notes.append(f"debug_token unavailable ({redact(str(exc))}); checking /me/permissions instead")
        if scopes is None:
            try:
                rows = get(client, "me/permissions", {}, "permissions check").get("data") or []
                scopes = {str(row.get("permission")) for row in rows if row.get("status") == "granted"}
            except TarshishError as exc:
                result.problems.append(f"could not read the token's permissions: {redact(str(exc))}")

    if scopes is not None:
        result.granted = {name: any(alias in scopes for alias in aliases) for name, aliases in REQUIRED_PERMISSIONS.items()}
        missing = [name for name, granted in result.granted.items() if not granted]
        if missing:
            result.problems.append(
                f"missing permission(s): {', '.join(missing)} → {error_hint(10, None)}"
            )
    return result


def verify_pages(registry: Any, pages: Iterable[Any], *, transport: Any = None) -> list[InstagramVerification]:
    """Resolve each ``instagram_pub`` social page's env vars and verify it (read-only)."""
    results: list[InstagramVerification] = []
    for page in pages:
        if page.platform != PLUGIN_NAME:
            continue
        target, reason = registry.resolve_target(page)
        if target is None:
            refs = {**InstagramPublisher.default_env_vars, **page.env_refs()}
            results.append(InstagramVerification(
                page_name=page.page_name,
                problems=[f"not configured: {reason or 'credentials missing'} "
                          f"(expects {refs['token']} and {refs['account_id']} in .env)"],
            ))
            continue
        results.append(verify_connection(
            registry.settings, target.credential("token"), target.credential("account_id"),
            page_name=page.page_name, transport=transport,
        ))
    return results


def pages_to_verify(registry: Any, pipeline_id: str | None = None) -> list[Any]:
    """The Instagram pages of one pipeline, of every pipeline, or the default ``.env`` account."""
    from core.config import SocialPage

    pipelines = [registry.get_pipeline(pipeline_id)] if pipeline_id else registry.list_pipelines()
    pages: dict[tuple[str, str], Any] = {}
    for pipeline in pipelines:
        for page in pipeline.social_pages:
            if page.platform == PLUGIN_NAME:
                refs = {**InstagramPublisher.default_env_vars, **page.env_refs()}
                pages.setdefault((refs["token"], refs["account_id"]), page)  # one check per account
    if not pages and not pipeline_id:
        return [SocialPage(platform=PLUGIN_NAME, page_name="default")]
    return list(pages.values())
