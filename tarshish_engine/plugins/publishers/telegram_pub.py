"""Telegram Bot API publisher (sync and async) with photo / video support.

Credentials are never stored in configuration. A social page references the
environment variables that hold them::

    {"platform": "telegram_pub", "page_name": "@my_channel",
     "token_env_var": "TG_SPORTS_BOT_TOKEN", "chat_id_env_var": "TG_SPORTS_CHAT_ID"}

Pages without explicit references fall back to ``TELEGRAM_BOT_TOKEN`` /
``TELEGRAM_CHAT_ID``.

Delivery picks the richest format available and degrades gracefully:
video -> photo with caption -> plain text. A media URL Telegram cannot fetch
therefore costs a retry, not the post.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from core.interfaces import BasePublisher
from core.models import GeneratedContent, PublishReceipt
from core.resilience import NonRetryableError, get_logger, redact, with_retry
from plugins.publishers import disclosure_enabled, render_post_text

logger = get_logger("plugins.telegram_pub")

API_BASE = "https://api.telegram.org"
TEXT_LIMIT = 4096
CAPTION_LIMIT = 1024
_VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm"}
_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
_MEDIA_METHODS = {"sendVideo", "sendPhoto"}


class TelegramRetryableError(Exception):
    """Rate limit or server-side failure; ``retry_after`` is honoured by ``with_retry``."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class TelegramMediaError(NonRetryableError):
    """Telegram rejected the media itself — fall back to a simpler format."""


@dataclass
class _Call:
    method: str
    data: dict[str, Any] = field(default_factory=dict)
    file_field: str | None = None
    file_path: str | None = None


class TelegramPublisher(BasePublisher):
    required_credentials = ("token", "chat_id")
    account_credential = "chat_id"
    inline_credentials = {"chat_id": "chat_id"}
    default_env_vars = {"token": "TELEGRAM_BOT_TOKEN", "chat_id": "TELEGRAM_CHAT_ID"}
    _transport: Any = None  # test hook: an httpx.MockTransport instead of the network

    @property
    def platform_name(self) -> str:
        return "telegram_pub"

    # -- contract (sync) --------------------------------------------------------- #
    def publish(self, content: GeneratedContent) -> PublishReceipt:
        calls = self._plan(content)
        with httpx.Client(timeout=httpx.Timeout(60.0, connect=10.0), transport=self._transport) as client:
            for index, call in enumerate(calls):
                try:
                    return self._receipt(self._send(client, call))
                except TelegramMediaError as exc:
                    self._log_downgrade(call, calls, index, exc)
        raise NonRetryableError("Telegram rejected every delivery format for this post")

    # -- async variant ------------------------------------------------------------ #
    async def apublish(self, content: GeneratedContent) -> PublishReceipt:
        """Same behaviour as :meth:`publish` for async callers (FastAPI, bots, asyncio workers)."""
        calls = self._plan(content)
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0), transport=self._transport) as client:
            for index, call in enumerate(calls):
                try:
                    return self._receipt(await self._asend(client, call))
                except TelegramMediaError as exc:
                    self._log_downgrade(call, calls, index, exc)
        raise NonRetryableError("Telegram rejected every delivery format for this post")

    # -- planning ------------------------------------------------------------------- #
    def _plan(self, content: GeneratedContent) -> list[_Call]:
        """Ordered delivery attempts, richest first; the last one is always plain text."""
        chat_id = self.target.credential("chat_id")
        disclosure = disclosure_enabled(self.context.settings)
        caption = render_post_text(content, max_length=CAPTION_LIMIT, disclosure=disclosure)
        text = render_post_text(content, max_length=TEXT_LIMIT, disclosure=disclosure)
        product = content.product
        local = Path(content.media_local_path) if content.media_local_path else None
        if local is not None and not local.is_file():
            local = None
        calls: list[_Call] = []

        if content.content_type == "video":
            video_url = content.media_public_url or product.video_url
            if local is not None and local.suffix.lower() in _VIDEO_SUFFIXES:
                calls.append(
                    _Call("sendVideo", {"chat_id": chat_id, "caption": caption, "supports_streaming": "true"},
                          file_field="video", file_path=str(local))
                )
            elif video_url:
                calls.append(
                    _Call("sendVideo", {"chat_id": chat_id, "video": video_url, "caption": caption,
                                        "supports_streaming": True})
                )

        if local is not None and local.suffix.lower() in _IMAGE_SUFFIXES:
            calls.append(_Call("sendPhoto", {"chat_id": chat_id, "caption": caption}, file_field="photo", file_path=str(local)))
        elif product.image_urls:
            calls.append(_Call("sendPhoto", {"chat_id": chat_id, "photo": product.image_urls[0], "caption": caption}))

        calls.append(_Call("sendMessage", {"chat_id": chat_id, "text": text}))
        return calls

    # -- transport ------------------------------------------------------------------- #
    def _url(self, method: str) -> str:
        return f"{API_BASE}/bot{self.target.credential('token')}/{method}"

    @with_retry(max_attempts=3, base_delay=2.0)
    def _send(self, client: httpx.Client, call: _Call) -> dict[str, Any]:
        try:
            if call.file_path and call.file_field:
                with open(call.file_path, "rb") as handle:
                    files = {call.file_field: (Path(call.file_path).name, handle)}
                    response = client.post(self._url(call.method), data=call.data, files=files)
            else:
                response = client.post(self._url(call.method), json=call.data)
        except httpx.HTTPError as exc:
            # httpx errors can carry the request URL, which embeds the bot token.
            raise TelegramRetryableError(redact(f"{type(exc).__name__}: {exc}")) from None
        return self._interpret(call, response)

    @with_retry(max_attempts=3, base_delay=2.0)
    async def _asend(self, client: httpx.AsyncClient, call: _Call) -> dict[str, Any]:
        try:
            if call.file_path and call.file_field:
                payload = Path(call.file_path).read_bytes()
                files = {call.file_field: (Path(call.file_path).name, payload)}
                response = await client.post(self._url(call.method), data=call.data, files=files)
            else:
                response = await client.post(self._url(call.method), json=call.data)
        except httpx.HTTPError as exc:
            raise TelegramRetryableError(redact(f"{type(exc).__name__}: {exc}")) from None
        return self._interpret(call, response)

    @staticmethod
    def _interpret(call: _Call, response: httpx.Response) -> dict[str, Any]:
        """Map a Bot API response onto retryable / media / fatal outcomes."""
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code == 200 and body.get("ok"):
            return body.get("result") or {}

        code = int(body.get("error_code") or response.status_code)
        description = str(body.get("description") or f"HTTP {response.status_code}")
        message = f"Telegram {call.method} failed ({code}): {description}"
        if code == 429:
            retry_after = (body.get("parameters") or {}).get("retry_after")
            raise TelegramRetryableError(message, retry_after=float(retry_after) if retry_after else None)
        if code >= 500:
            raise TelegramRetryableError(message)
        if code == 400 and call.method in _MEDIA_METHODS:
            raise TelegramMediaError(message)
        raise NonRetryableError(message)  # bad token, bot not in the chat, chat not found…

    def _receipt(self, result: dict[str, Any]) -> PublishReceipt:
        return PublishReceipt(
            platform=self.platform_name, platform_post_id=str(result.get("message_id", "")), success=True
        )

    def _log_downgrade(self, call: _Call, calls: list[_Call], index: int, exc: Exception) -> None:
        following = calls[index + 1].method if index + 1 < len(calls) else "nothing"
        logger.warning(
            "Telegram refused %s for %s (%s); falling back to %s.", call.method, self.target.page_name, exc, following
        )


# --------------------------------------------------------------------------- #
# Read-only connection check (`python main.py --verify-telegram`, web admin "Test connection")
# --------------------------------------------------------------------------- #
@dataclass
class TelegramVerification:
    """Outcome of ``getMe`` + ``getChat`` + ``getChatMember`` for one channel. Nothing is posted."""

    page_name: str
    bot_username: str | None = None
    chat_title: str | None = None
    chat_type: str | None = None
    bot_can_post: bool | None = None
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.bot_username) and bool(self.chat_title) and self.bot_can_post is not False and not self.problems

    def summary(self) -> str:
        bot = f"Bot @{self.bot_username}" if self.bot_username else "Bot not connected"
        chat = f"{self.chat_type or 'chat'} \"{self.chat_title}\"" if self.chat_title else "chat not reachable"
        post = {True: "can post", False: "cannot post", None: "posting rights unknown"}[self.bot_can_post]
        return f"{bot} | {chat} | {post}"

    def lines(self) -> list[str]:
        mark = lambda ok: "✓" if ok else "✗"  # noqa: E731
        rows = [
            f"{mark(bool(self.bot_username))} Bot token      : "
            + (f"valid (@{self.bot_username})" if self.bot_username else "invalid or not checked"),
            f"{mark(bool(self.chat_title))} Chat           : "
            + (f"{self.chat_title} ({self.chat_type})" if self.chat_title else "not found or bot not a member"),
        ]
        if self.bot_can_post is not None:
            rows.append(f"{mark(self.bot_can_post)} Post rights    : "
                        + ("the bot may post here" if self.bot_can_post else "make the bot an admin with 'Post messages'"))
        rows.extend(f"✗ {problem}" for problem in self.problems)
        return rows


def _telegram_get(client: httpx.Client, token: str, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        response = client.get(f"{API_BASE}/bot{token}/{method}", params=params or {})
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise NonRetryableError(redact(f"{method}: {type(exc).__name__}: {exc}")) from None
    if not payload.get("ok"):
        raise NonRetryableError(redact(f"{method}: {payload.get('description') or f'HTTP {response.status_code}'}"))
    return payload.get("result") or {}


def verify_connection(token: str, chat_id: str, *, page_name: str = "default", transport: Any = None) -> TelegramVerification:
    """Check the bot token and that the bot can reach (and post to) ``chat_id``. Read-only."""
    result = TelegramVerification(page_name=page_name)
    with httpx.Client(timeout=httpx.Timeout(15.0, connect=10.0), transport=transport) as client:
        try:
            me = _telegram_get(client, token, "getMe")
            result.bot_username = me.get("username")
        except NonRetryableError as exc:
            result.problems.append(f"bot token rejected: {exc}")
            return result
        try:
            chat = _telegram_get(client, token, "getChat", {"chat_id": chat_id})
            result.chat_title = chat.get("title") or chat.get("username") or str(chat.get("id"))
            result.chat_type = chat.get("type")
        except NonRetryableError as exc:
            result.problems.append(f"chat {chat_id} not reachable: {exc}")
            return result
        if result.chat_type in ("channel", "group", "supergroup") and me.get("id"):
            try:
                member = _telegram_get(client, token, "getChatMember", {"chat_id": chat_id, "user_id": me["id"]})
                status = member.get("status")
                if status == "creator":
                    result.bot_can_post = True
                elif status == "administrator":
                    result.bot_can_post = result.chat_type != "channel" or bool(member.get("can_post_messages", True))
                else:
                    result.bot_can_post = result.chat_type != "channel" and status == "member"
            except NonRetryableError as exc:
                result.problems.append(f"could not read the bot's rights: {exc}")
        else:
            result.bot_can_post = True
    return result


def verify_pages(registry: Any, pages: Any, *, transport: Any = None) -> list[TelegramVerification]:
    """Resolve each ``telegram_pub`` social page's credentials and verify it (read-only)."""
    results: list[TelegramVerification] = []
    for page in pages:
        if page.platform != "telegram_pub":
            continue
        target, reason = registry.resolve_target(page)
        if target is None:
            refs = {**TelegramPublisher.default_env_vars, **page.env_refs()}
            results.append(TelegramVerification(
                page_name=page.page_name,
                problems=[f"not configured: {reason or 'credentials missing'} "
                          f"(expects {refs['token']} and a chat id: {refs['chat_id']} or platform metadata chat_id)"],
            ))
            continue
        results.append(verify_connection(
            target.credential("token"), target.credential("chat_id"), page_name=page.page_name, transport=transport,
        ))
    return results


def pages_to_verify(registry: Any, category: str | None = None, language: str | None = None) -> list[Any]:
    """The Telegram channels of the social-accounts matrix (one check per bot token + chat)."""
    from core.config import SocialPage

    pages: dict[tuple[str, str], Any] = {}
    for account in registry.list_social_accounts(category=category, language=language, platform="telegram"):
        page = registry.social_page(account)
        refs = {**TelegramPublisher.default_env_vars, **page.env_refs()}
        pages.setdefault((refs["token"], str(page.options().get("chat_id") or refs["chat_id"])), page)
    if not pages and category is None and language is None:
        return [SocialPage(platform="telegram_pub", page_name="default")]
    return list(pages.values())
