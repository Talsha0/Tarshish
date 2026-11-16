"""Instagram publisher: Meta's Instagram Graph API, two-step container flow.

1. ``POST /{ig_user_id}/media``          create a media container (image, or a Reel for video content)
2. ``GET  /{container_id}``              wait until Meta has fetched and processed the media
3. ``POST /{ig_user_id}/media_publish``  publish the container; its id is the live post id

Credentials are never stored in configuration. A social page references the
environment variables that hold them::

    {"platform": "instagram_pub", "page_name": "@my_gadgets",
     "token_env_var": "IG_GADGETS_TOKEN", "account_id_env_var": "IG_GADGETS_ACCOUNT_ID"}

Pages without explicit references fall back to ``INSTAGRAM_ACCESS_TOKEN`` /
``INSTAGRAM_BUSINESS_ACCOUNT_ID``. ``INSTAGRAM_GRAPH_API_VERSION`` (default
``v21.0``) selects the Graph API version.

Instagram captions cannot carry a clickable link, so the post ends with a call
to action that points at the storefront category linked in the account's bio.
Meta downloads the media itself: the image or video must be a public URL. A
product that only has a stand-in picture is refused rather than posted.

With ``--dry-run`` (or without credentials) nothing is sent: the exact payload
is logged instead. Set ``DRY_RUN_PUBLISHER=instagram_pub`` to preview dry-runs
through this publisher.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

import httpx

from core.database import category_slug
from core.interfaces import BasePublisher, PluginContext
from core.models import GeneratedContent, PublishReceipt, PublishTarget
from core.resilience import NonRetryableError, TarshishError, get_logger, redact, register_secret, with_retry
from plugins import is_placeholder_image
from plugins.publishers import disclosure_enabled, render_post_text

logger = get_logger("plugins.instagram_pub")

GRAPH_BASE = "https://graph.facebook.com"
DEFAULT_API_VERSION = "v21.0"
CAPTION_LIMIT = 2200
HASHTAG_LIMIT = 30
_CALL_TO_ACTION = {"he": "🔗 הקישור בביו ← {path}", "en": "🔗 Link in bio -> {path}"}
# Graph API error codes that describe a temporary condition (service hiccup, rate limit).
_TRANSIENT_CODES = {1, 2, 4, 17, 32, 613}
_CONNECT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout)


class InstagramRetryableError(TarshishError):
    """Rate limit or server-side failure."""


class _StillProcessing(TarshishError):
    """Meta has not finished fetching / transcoding the media yet."""


class InstagramPublisher(BasePublisher):
    required_credentials = ("token", "account_id")
    default_env_vars = {"token": "INSTAGRAM_ACCESS_TOKEN", "account_id": "INSTAGRAM_BUSINESS_ACCOUNT_ID"}
    _transport: Any = None  # test hook: an httpx.MockTransport instead of the network

    def __init__(self, context: PluginContext, target: PublishTarget) -> None:
        super().__init__(context, target)
        register_secret(target.credential("token"))  # Graph error messages can quote the token back
        version = context.settings.get_env("INSTAGRAM_GRAPH_API_VERSION", DEFAULT_API_VERSION) or DEFAULT_API_VERSION
        if not re.fullmatch(r"v\d{1,3}\.\d", version):
            logger.warning(
                "INSTAGRAM_GRAPH_API_VERSION=%r is not a version such as 'v21.0'; using %s.", version, DEFAULT_API_VERSION
            )
            version = DEFAULT_API_VERSION
        self._version = version

    @property
    def platform_name(self) -> str:
        return "instagram"

    # -- contract -------------------------------------------------------------------- #
    def publish(self, content: GeneratedContent) -> PublishReceipt:
        media, problem = self._media(content)
        payload = {**media, "caption": self.render_caption(content)}
        token, account_id = self.target.credential("token"), self.target.credential("account_id")

        if self.context.dry_run or self.target.options.get("dry_run"):
            self._log_simulation(payload, "dry-run", problem)
            return PublishReceipt(
                platform=self.platform_name, platform_post_id=f"dry-run-{uuid.uuid4().hex[:10]}", success=True
            )
        if not token or not account_id:
            self._log_simulation(payload, "credentials are missing", problem)
            return PublishReceipt(
                platform=self.platform_name, success=False,
                error_message="Instagram credentials are not configured (access token and business account id); "
                              "the post was simulated in the log and nothing was published.",
            )
        if problem:
            raise NonRetryableError(f"Instagram needs public media: {problem}")

        with httpx.Client(timeout=httpx.Timeout(60.0, connect=10.0), transport=self._transport) as client:
            container_id = str(self._call(client, "POST", f"{account_id}/media", data=payload)["id"])
            self._await_container(client, container_id)
            published = self._call(
                client, "POST", f"{account_id}/media_publish", data={"creation_id": container_id}, publishing=True
            )
        return PublishReceipt(platform=self.platform_name, platform_post_id=str(published["id"]), success=True)

    # -- payload ----------------------------------------------------------------------- #
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

    @staticmethod
    def _media(content: GeneratedContent) -> tuple[dict[str, str], str | None]:
        """Container fields for the richest usable media, or the reason there is none."""
        product = content.product
        public = content.media_public_url
        if content.content_type == "video":
            video = public or product.video_url
            if video:
                return {"media_type": "REELS", "video_url": video}, None
            public = None  # a video post without a public video degrades to the product photo
        image = public or (product.image_urls[0] if product.image_urls else None)
        if not image:
            return {}, "the post has no public image or video URL"
        if is_placeholder_image(image):
            return {"image_url": image}, "the product only has a stand-in picture, no real photo yet"
        return {"image_url": image}, None

    def _log_simulation(self, payload: dict[str, str], reason: str, problem: str | None) -> None:
        media = payload.get("video_url") or payload.get("image_url") or "none"
        kind = "Reel" if "video_url" in payload else "image post"
        note = f"\n  note   : a live run would refuse this post — {problem}" if problem else ""
        logger.info(
            "Instagram %s simulated (%s) for %s — nothing was sent.\n  media  : %s%s\n  caption:\n%s",
            kind, reason, self.target.page_name, media, note, payload["caption"],
        )

    # -- transport ----------------------------------------------------------------------- #
    @with_retry(max_attempts=3, base_delay=2.0)
    def _call(
        self, client: httpx.Client, method: str, path: str, *, data: dict[str, str] | None = None,
        params: dict[str, str] | None = None, publishing: bool = False,
    ) -> dict[str, Any]:
        # The token travels in a header, so it never appears in a URL, a form body or an httpx error.
        headers = {"Authorization": f"Bearer {self.target.credential('token')}"}
        action = path.rsplit("/", 1)[-1] if "/" in path else "status check"
        try:
            response = client.request(
                method, f"{GRAPH_BASE}/{self._version}/{path}", data=data, params=params, headers=headers
            )
        except httpx.HTTPError as exc:
            text = redact(f"Instagram {action} failed: {type(exc).__name__}: {exc}")
            if publishing and not isinstance(exc, _CONNECT_ERRORS):
                # The request may have reached Meta: publishing again could post twice.
                raise NonRetryableError(f"{text} — outcome unknown, check the account before retrying") from None
            raise InstagramRetryableError(text) from None
        return self._interpret(action, response)

    @with_retry(max_attempts=12, base_delay=5.0, max_delay=30.0, retry_on=(_StillProcessing,))
    def _await_container(self, client: httpx.Client, container_id: str) -> None:
        """Poll until Meta has processed the media (instant for images, minutes for Reels)."""
        status = str(self._call(client, "GET", container_id, params={"fields": "status_code"}).get("status_code", ""))
        if status in ("FINISHED", "PUBLISHED", ""):
            return
        if status == "IN_PROGRESS":
            raise _StillProcessing("Instagram is still processing the media")
        raise NonRetryableError(f"Instagram could not process the media (container status {status})")

    @staticmethod
    def _interpret(action: str, response: httpx.Response) -> dict[str, Any]:
        """Map a Graph API response onto retryable / fatal outcomes."""
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
        code = error.get("code")
        description = redact(str(error.get("message") or f"HTTP {response.status_code}"))
        message = f"Instagram {action} failed ({response.status_code}, code {code}): {description}"
        if response.status_code == 429 or response.status_code >= 500 or error.get("is_transient") or code in _TRANSIENT_CODES:
            raise InstagramRetryableError(message)
        raise NonRetryableError(message)  # expired token, missing permission, media Meta cannot fetch…
