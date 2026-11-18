"""Content review: every unpublished post, one shape, and the actions an operator takes on it.

Two tables hold posts that have not gone out yet:

* ``generated_assets`` — FindHub assets (copy + branded image + links) in ``DRAFT`` / ``QUEUED`` /
  ``FAILED``, published by :meth:`ContentGenerator.publish_asset` with ``generated_text`` verbatim;
* ``publications`` — the pipeline's queue in ``QUEUED`` / ``PENDING`` / ``FAILED``, published by the
  queue worker, which appends the link or link-in-bio call to action, hashtags and disclosure.

:class:`ContentReview` turns both into :class:`ReviewItem` objects (what the phone preview renders:
skin, text direction, media, links, what the publisher will append) and runs the review actions on
them: save caption edits, approve and publish now through the platform's publisher plugin, and delete
with orphaned-media cleanup. The Streamlit dashboard's Content Review tab and the admin API
(``storefront_api/admin.py``) both use it, so the rules live in one place.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import ValidationError

from core.resilience import TarshishError, redact

logger = logging.getLogger(__name__)

Layout = Literal["instagram_post", "instagram_story", "vertical_video", "telegram"]
Variant = Literal["instagram_reel", "tiktok", "youtube_short"]
Source = Literal["asset", "publication"]

RTL_LANGUAGES = frozenset({"he", "ar", "fa", "ur", "yi"})
#: Caption limits per platform, in UTF-16 code units as the publishers count them.
CAPTION_LIMITS = {"instagram": 2200, "tiktok": 2200, "youtube": 5000, "telegram": 4096}
PLATFORM_LABELS = {"instagram": "Instagram", "tiktok": "TikTok", "youtube": "YouTube Shorts",
                   "telegram": "Telegram", "console": "Console"}
CONTENT_TYPE_LABELS = {
    "instagram_post": "Instagram post", "instagram_story": "Instagram Story", "instagram_reel": "Instagram Reel",
    "tiktok_post": "TikTok photo post", "tiktok_video": "TikTok video", "youtube_short": "YouTube Short",
    "telegram_post": "Telegram post", "console_post": "Console preview",
}
_FORMAT_LAYOUT: dict[str, tuple[Layout, Variant | None]] = {
    "instagram_post": ("instagram_post", None),
    "instagram_story": ("instagram_story", None),
    "instagram_reel": ("vertical_video", "instagram_reel"),
    "tiktok_post": ("vertical_video", "tiktok"),
    "tiktok_video": ("vertical_video", "tiktok"),
    "youtube_short": ("vertical_video", "youtube_short"),
    "telegram_post": ("telegram", None),
}
_PLATFORM_LAYOUT: dict[str, tuple[Layout, Variant | None]] = {
    "instagram": ("instagram_post", None),
    "tiktok": ("vertical_video", "tiktok"),
    "youtube": ("vertical_video", "youtube_short"),
    "telegram": ("telegram", None),
}
_VIDEO_RE = re.compile(r"\.(mp4|mov|webm)(\?|$)", re.IGNORECASE)
_STORY_LINK_KINDS = ("storefront", "product", "affiliate")


class ReviewError(TarshishError):
    """A review action was refused; ``status`` is the matching HTTP status (404, 409 or 422)."""

    def __init__(self, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.status = status


def text_direction(language: str | None) -> Literal["rtl", "ltr"]:
    return "rtl" if (language or "").split("-")[0].lower() in RTL_LANGUAGES else "ltr"


def utf16_length(text: str) -> int:
    """Length as Instagram, TikTok and Telegram count it (emoji outside the BMP count twice)."""
    return len(text.encode("utf-16-le")) // 2


def _http(url: Any) -> str | None:
    return url if isinstance(url, str) and url.lower().startswith(("http://", "https://")) else None


def _handle(value: str | None) -> str:
    clean = (value or "").strip()
    if not clean or clean == "default":
        return "@findhub"
    return clean if clean.startswith("@") else f"@{clean}"


def _json(text: str | None, default: Any) -> Any:
    try:
        value = json.loads(text or "")
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


@dataclass
class ReviewLinks:
    storefront_url: str | None = None   # the FindHub category page
    product_url: str | None = None      # the FindHub product page
    affiliate_url: str | None = None    # the store's tracked deal link
    bio_link: str | None = None         # what the account's bio link points at (link-in-bio CTAs)
    bio_cta: str | None = None
    stickers: list[dict[str, str]] = field(default_factory=list)  # Story link stickers: kind, label, url


@dataclass
class ReviewItem:
    """One unpublished post, as the phone preview and the review panel see it."""

    key: str                       # "asset:12" / "pub:7"
    source: Source
    id: int
    layout: Layout
    variant: Variant | None
    platform: str
    content_type: str
    type_label: str
    status: str
    language: str
    dir: Literal["rtl", "ltr"]
    handle: str
    target_account: str | None
    category_id: str
    text: str                      # the editable text as saved
    text_field: Literal["generated_text", "reel_caption", "caption"]
    suffix: str                    # what the publisher appends after the editable text
    image_path: str | None         # a local image file (rendered inline)
    image_url: str | None          # a public image URL
    video_url: str | None
    product_title: str
    price: float | None
    original_price: float | None
    currency: str
    links: ReviewLinks
    can_edit: bool
    can_publish: bool
    publish_blocked_reason: str | None
    model: str
    created_at: str
    error_message: str | None = None

    def post_text(self, text: str | None = None) -> str:
        """The post as it will read: the (edited) caption plus what the publisher appends."""
        return (self.text if text is None else text) + self.suffix

    @property
    def caption_limit(self) -> int | None:
        return CAPTION_LIMITS.get(self.platform)

    @property
    def platform_label(self) -> str:
        return PLATFORM_LABELS.get(self.platform, self.platform.title())

    @property
    def notes(self) -> list[str]:
        """What the preview shows that the publisher will not (or cannot) send as-is."""
        notes: list[str] = []
        if self.layout == "instagram_story":
            notes.append("Stories carry no caption: this text is drawn into the 9:16 image, so editing it here "
                         "only updates the stored text.")
            if self.links.stickers:
                notes.append("Meta's API cannot attach link stickers: an API-published Story goes out without "
                             "them; add them in the app if you post it by hand.")
        if self.layout == "telegram":
            notes.append("Inline buttons preview the post's links: the Telegram publisher sends them inside the "
                         "message text.")
        if self.layout == "vertical_video" and not self.video_url:
            notes.append("No video rendered yet: the phone shows the 9:16 cover image.")
        return notes


@dataclass
class PublishResult:
    key: str
    success: bool
    status: str            # PUBLISHED / FAILED / unchanged status when refused
    message: str
    platform_post_id: str = ""


class ContentReview:
    """List, edit, publish and delete unpublished posts (generated assets and queued publications)."""

    UNPUBLISHED_ASSETS = ("DRAFT", "QUEUED", "FAILED")
    UNPUBLISHED_PUBLICATIONS = ("QUEUED", "PENDING", "FAILED")

    def __init__(self, engine: Any) -> None:
        self.engine = engine
        self.settings = engine.settings
        self.db = engine.db

    # -- listing ---------------------------------------------------------------- #
    def items(
        self, *, sources: Iterable[Source] = ("asset", "publication"), include_failed: bool = True,
        category: str | None = None, language: str | None = None, platform: str | None = None, limit: int = 300,
    ) -> list[ReviewItem]:
        """Unpublished posts, newest first per source (generated assets before pipeline publications)."""
        sources = set(sources)
        result: list[ReviewItem] = []
        if "asset" in sources:
            statuses = [s for s in self.UNPUBLISHED_ASSETS if include_failed or s != "FAILED"]
            rows = self.db.list_generated_assets(statuses=statuses, category=category, language=language,
                                                 platform=platform, limit=limit)
            result += [self.from_asset(row) for row in rows]
        if "publication" in sources:
            statuses = [s for s in self.UNPUBLISHED_PUBLICATIONS if include_failed or s != "FAILED"]
            rows = self.db.list_publications(statuses, limit=limit, category=category, language=language,
                                             account_platform=platform, include_content=True)
            result += [self.from_publication(row) for row in rows]
        return result

    def item(self, key: str) -> ReviewItem:
        source, _, raw_id = key.partition(":")
        if source not in ("asset", "pub") or not raw_id.isdigit():
            raise ReviewError(f"Unknown review item {key!r}.", 404)
        if source == "asset":
            return self.from_asset(self._asset(int(raw_id)))
        row = self.db.query_one(
            "SELECT pub.*, p.title AS product_title FROM publications pub JOIN products p ON p.id = pub.product_id "
            "WHERE pub.id = ?", (int(raw_id),))
        if row is None:
            raise ReviewError(f"No publication #{raw_id}.", 404)
        return self.from_publication(row)

    def from_asset(self, row: dict[str, Any]) -> ReviewItem:
        content_type = row["content_type"]
        layout, variant = _FORMAT_LAYOUT.get(content_type, ("telegram", None))
        links = _json(row.get("links_payload_json"), {})
        cta = links.get("cta") if isinstance(links.get("cta"), dict) else {}
        reel = content_type == "instagram_reel"
        locked = bool(row.get("locked_at"))
        unpublished = row["status"] in self.UNPUBLISHED_ASSETS
        blocked = None
        if reel:
            blocked = ("Instagram publishes Reels from video only: post this 9:16 cover from the app, or enable "
                       "video rendering first.")
        elif not row.get("target_account_id"):
            blocked = f"No {row['platform']} account for {row['category_id']}/{row['language']}."
        elif locked:
            blocked = "A publish is already in flight."
        elif not unpublished:
            blocked = f"Status is {row['status']}."
        images = [url for url in _json(row.get("product_image_urls_json"), []) if _http(url)]
        language = row["language"]
        title = row.get("product_title_he") if language == "he" and row.get("product_title_he") else row.get("product_title")
        stickers = [link for link in links.get("story_links") or [] if isinstance(link, dict) and _http(link.get("url"))]
        return ReviewItem(
            key=f"asset:{row['id']}", source="asset", id=int(row["id"]), layout=layout, variant=variant,
            platform=row["platform"], content_type=content_type,
            type_label=CONTENT_TYPE_LABELS.get(content_type, content_type),
            status="PENDING" if locked else row["status"], language=language, dir=text_direction(language),
            handle=_handle(row.get("target_account_handle")), target_account=row.get("target_account_id"),
            category_id=row["category_id"],
            text=str(links.get("reel_caption") or row["generated_text"]) if reel else str(row["generated_text"]),
            text_field="reel_caption" if reel else "generated_text", suffix="",
            image_path=row.get("generated_image_path"),
            image_url=_http(row.get("generated_image_url")) or (images[0] if images else None), video_url=None,
            product_title=str(title or ""), price=row.get("product_price"),
            original_price=row.get("product_original_price"), currency=row.get("product_currency") or "USD",
            links=ReviewLinks(
                storefront_url=_http(links.get("storefront_category_url")),
                product_url=_http(links.get("storefront_product_url")),
                affiliate_url=_http(links.get("affiliate_url")),
                bio_link=_http(cta.get("bio_link")) if cta.get("kind") == "link_in_bio" else _http(links.get("storefront_category_url")),
                bio_cta=cta.get("text") if cta.get("kind") == "link_in_bio" else None,
                stickers=[{"kind": str(s.get("kind", "")), "label": str(s.get("label", "")), "url": s["url"]} for s in stickers],
            ),
            can_edit=unpublished and not locked, can_publish=blocked is None, publish_blocked_reason=blocked,
            model=row.get("llm_model_used") or "", created_at=row.get("created_at") or "",
            error_message=row.get("error_message"),
        )

    def from_publication(self, row: dict[str, Any]) -> ReviewItem:
        content = _json(row.get("content_json"), {})
        product = content.get("product") if isinstance(content.get("product"), dict) else {}
        platform = row.get("account_platform") or str(row.get("platform") or "").removesuffix("_pub")
        content_format = row.get("content_format") or ""
        layout, variant = _FORMAT_LAYOUT.get(content_format) or _PLATFORM_LAYOUT.get(platform) or ("telegram", None)
        language = row.get("language") or content.get("language") or "en"
        editable = row["status"] in ("QUEUED", "FAILED")
        _, suffix = self.post_text(row, row.get("content_json"))
        caption = str(content.get("final_text") or row.get("caption") or "")
        if suffix is None:  # caption shortened to fit: show the caption and its hashtags
            tags = [f"#{tag}" for tag in content.get("hashtags") or []]
            suffix = f"\n\n{' '.join(tags)}" if tags else ""
        media = _http(content.get("media_public_url"))
        is_video = content.get("content_type") == "video" or bool(media and _VIDEO_RE.search(media))
        images = [url for url in product.get("image_urls") or [] if _http(url)]
        local = row.get("media_uri") if row.get("media_uri") and not _http(row.get("media_uri")) else None
        return ReviewItem(
            key=f"pub:{row['id']}", source="publication", id=int(row["id"]), layout=layout, variant=variant,
            platform=platform, content_type=content_format or str(row.get("content_type") or ""),
            type_label=CONTENT_TYPE_LABELS.get(content_format, content_format or str(row.get("content_type") or "")),
            status=row["status"], language=language, dir=text_direction(language),
            handle=_handle(row.get("target_page")), target_account=row.get("target_page"),
            category_id=row.get("category") or re.sub(r"_[a-z]{2}$", "", str(row.get("pipeline_id") or "")),
            text=caption, text_field="caption", suffix=suffix,
            image_path=None if is_video else local,
            image_url=(media if media and not is_video else None) or (images[0] if images else None),
            video_url=_http(media if is_video else product.get("video_url")),
            product_title=str(row.get("product_title") or product.get("title") or ""),
            price=product.get("price"), original_price=product.get("original_price"),
            currency=product.get("currency") or "USD",
            links=ReviewLinks(affiliate_url=_http(product.get("affiliate_url"))),
            can_edit=editable, can_publish=editable,
            publish_blocked_reason=None if editable else f"Status is {row['status']}.",
            model=row.get("model_used") or "", created_at=row.get("created_at") or "",
            error_message=row.get("error_message"),
        )

    def post_text(self, row: dict[str, Any], content_json: str | None) -> tuple[str | None, str | None]:
        """``(text, suffix)``: the body the row's publisher will send, and what follows the caption in it.

        Mirrors each publisher's caption rendering (link or link-in-bio call to action, hashtags, disclosure,
        platform length limits). ``suffix`` is ``None`` when the caption itself was shortened to fit.
        """
        from core.models import GeneratedContent, category_slug
        from plugins.publishers import disclosure_enabled, render_post_text

        try:
            content = GeneratedContent.model_validate_json(content_json or "")
        except ValidationError:
            return None, None
        if content.final_text:
            return content.final_text.strip(), ""
        disclosure = disclosure_enabled(self.settings)
        lang = content.language.split("-")[0]
        path = f"/c/{category_slug(content.product.category)}"
        platform = row.get("account_platform") or ""
        if platform in ("instagram", "tiktok"):
            from plugins.publishers import instagram_pub, tiktok_pub

            module = instagram_pub if platform == "instagram" else tiktok_pub
            cta = module._CALL_TO_ACTION
            if platform == "instagram":
                content = content.model_copy(update={"hashtags": content.hashtags[:instagram_pub.HASHTAG_LIMIT]})
            text = render_post_text(content, max_length=module.CAPTION_LIMIT, disclosure=disclosure,
                                    call_to_action=cta.get(lang, cta["en"]).format(path=path))
        elif platform == "youtube":
            from plugins.publishers import youtube_pub

            text = render_post_text(content, max_length=youtube_pub.DESCRIPTION_UNITS, disclosure=disclosure)
            if youtube_pub.SHORTS_TAG.lower() not in text.lower():
                text = f"{text}\n\n{youtube_pub.SHORTS_TAG}"
        elif platform == "telegram":
            from plugins.publishers import telegram_pub

            with_media = bool(content.media_public_url or content.product.image_urls)
            text = render_post_text(content, disclosure=disclosure,
                                    max_length=telegram_pub.CAPTION_LIMIT if with_media else telegram_pub.TEXT_LIMIT)
        else:
            text = render_post_text(content, disclosure=disclosure)
        return text, (text[len(content.caption):] if text.startswith(content.caption) else None)

    # -- editing ---------------------------------------------------------------- #
    def save_text(self, key: str, text: str) -> ReviewItem:
        """Save the editable text of any review item (generated_text, Reel caption or publication caption)."""
        item = self.item(key)
        if item.source == "publication":
            self.edit_publication(item.id, text)
        elif item.text_field == "reel_caption":
            self.edit_asset(item.id, reel_caption=text)
        else:
            self.edit_asset(item.id, generated_text=text)
        return self.item(key)

    def edit_asset(
        self, asset_id: int, *, generated_text: str | None = None, reel_caption: str | None = None,
        story_links: Sequence[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        row = self._asset(asset_id)
        if generated_text is None and reel_caption is None and story_links is None:
            raise ReviewError("Nothing to update: send generated_text, reel_caption or story_links.", 422)
        if row["status"] == "PUBLISHED" or row.get("locked_at"):
            state = "being published" if row.get("locked_at") else "already published"
            raise ReviewError(f"Asset #{asset_id} is {state}; it can no longer be edited.", 409)
        text = generated_text.strip() if generated_text is not None else None
        if text == "":
            raise ReviewError("generated_text must not be blank.", 422)
        links_json = None
        if reel_caption is not None or story_links is not None:
            links = _json(row.get("links_payload_json"), {})
            if reel_caption is not None:
                if row["content_type"] != "instagram_reel":
                    raise ReviewError("reel_caption only applies to Instagram Reels.", 422)
                if not reel_caption.strip():
                    raise ReviewError("reel_caption must not be blank.", 422)
                links["reel_caption"] = reel_caption.strip()
            if story_links is not None:
                if row["content_type"] not in ("instagram_story", "instagram_reel"):
                    raise ReviewError("story_links only apply to Stories and Reels.", 422)
                links["story_links"] = [self._story_link(link) for link in story_links]
            links_json = json.dumps(links, ensure_ascii=False)
        if not self.db.update_generated_asset_content(asset_id, generated_text=text, links_payload_json=links_json):
            raise ReviewError(f"Asset #{asset_id} changed while you were editing it; reload and try again.", 409)
        return self._asset(asset_id)

    @staticmethod
    def _story_link(link: dict[str, str]) -> dict[str, str]:
        kind, label, url = str(link.get("kind", "")), str(link.get("label", "")).strip(), str(link.get("url", ""))
        if kind not in _STORY_LINK_KINDS or not label or len(label) > 60 or not re.fullmatch(r"https?://\S{1,1990}", url):
            raise ReviewError("A Story link needs a kind (storefront, product, affiliate), a label and an "
                              "http(s) URL.", 422)
        return {"kind": kind, "label": label, "url": url}

    def edit_publication(self, publication_id: int, caption: str) -> dict[str, Any]:
        from core.models import GeneratedContent

        row = self._publication(publication_id)
        if row["status"] not in ("QUEUED", "FAILED"):
            raise ReviewError(f"Publication #{publication_id} is {row['status']}; only queued or failed posts "
                              "can be edited.", 409)
        caption = caption.strip()
        if not caption:
            raise ReviewError("caption must not be blank.", 422)
        try:
            content = GeneratedContent.model_validate_json(row["content_json"])
        except ValidationError as exc:
            raise ReviewError(f"Publication #{publication_id} has an unreadable snapshot: "
                              f"{exc.error_count()} error(s).", 409) from exc
        # Pre-assembled posts (final_text) are posted verbatim, so the edit replaces that text too.
        update: dict[str, Any] = {"caption": caption}
        if content.final_text:
            update["final_text"] = caption
        content = content.model_copy(update=update)
        if not self.db.update_publication_caption(publication_id, caption, content.model_dump_json()):
            raise ReviewError(f"Publication #{publication_id} changed while you were editing it; reload and try "
                              "again.", 409)
        return {"id": publication_id, "caption": caption, "status": row["status"]}

    # -- publishing ------------------------------------------------------------- #
    def publish(self, key: str) -> PublishResult:
        """Approve (a DRAFT asset) and publish one item now through its platform's publisher plugin.

        A refusal (wrong status, no account, platform limit) leaves the item unchanged; a platform failure
        marks it ``FAILED`` with the error. Either way the result carries a message for the operator.
        """
        item = self.item(key)
        if item.source == "asset":
            if item.status == "DRAFT":
                self.approve_asset(item.id)
            return self.publish_asset(item.id)
        return self.publish_publication(item.id)

    def approve_asset(self, asset_id: int) -> None:
        row = self._asset(asset_id)
        if not row.get("target_account_id"):
            raise ReviewError(f"Asset #{asset_id} has no target account to queue for.", 409)
        if not self.db.set_generated_asset_status(asset_id, "QUEUED", allowed_from=("DRAFT", "FAILED")):
            raise ReviewError(f"Asset #{asset_id} is {row['status']}; it cannot be approved.", 409)

    def publish_asset(self, asset_id: int) -> PublishResult:
        from plugins.generators.content_generator import (
            AssetPublishError,
            ContentGenerator,
        )

        key = f"asset:{asset_id}"
        try:
            outcome = ContentGenerator(self.engine).publish_asset(asset_id)
        except AssetPublishError as exc:
            row = self.db.get_generated_asset(asset_id)
            return PublishResult(key, False, row["status"] if row else "MISSING", redact(str(exc)))
        return PublishResult(key, outcome.success, outcome.status, outcome.message, outcome.platform_post_id or "")

    def publish_publication(self, publication_id: int) -> PublishResult:
        key = f"pub:{publication_id}"
        row = self._publication(publication_id)
        if row["status"] not in ("QUEUED", "FAILED"):
            return PublishResult(key, False, row["status"], f"Publication #{publication_id} is {row['status']}.")
        if not self.db.requeue_now(publication_id) or not self.db.claim_publication(publication_id):
            fresh = self.db.get_publication(publication_id)
            return PublishResult(key, False, fresh["status"] if fresh else "MISSING",
                                 f"Publication #{publication_id} was taken by another worker or is no longer queued.")
        fresh = self.db.get_publication(publication_id) or row
        receipt = self.engine.create_worker()._publish_row(fresh)
        if receipt.success:
            return PublishResult(key, True, "PUBLISHED", f"Published to {fresh['target_page']} ({fresh['platform']})",
                                 receipt.platform_post_id)
        return PublishResult(key, False, "FAILED", redact(receipt.error_message or "the publisher reported a failure"))

    # -- deleting --------------------------------------------------------------- #
    def delete(self, key: str) -> dict[str, Any]:
        item = self.item(key)
        return self.delete_asset(item.id) if item.source == "asset" else self.delete_publication(item.id)

    def delete_asset(self, asset_id: int) -> dict[str, Any]:
        row = self._asset(asset_id)
        if not self.db.delete_generated_asset(asset_id):
            raise ReviewError(f"Asset #{asset_id} is being published right now.", 409)
        removed = self.remove_orphan_media(row.get("generated_image_path"), row.get("generated_image_url"))
        return {"deleted": asset_id, "media_removed": removed}

    def delete_publication(self, publication_id: int) -> dict[str, Any]:
        row = self._publication(publication_id)
        if row["status"] == "PENDING":
            raise ReviewError("This publication is being published right now.", 409)
        self.db.delete_publication(publication_id)
        removed = False
        if row["status"] != "PUBLISHED":  # a live post's media may still be served from this file
            removed = self.remove_orphan_media(row.get("media_uri"))
        return {"deleted": publication_id, "media_removed": removed}

    # -- media ------------------------------------------------------------------ #
    @property
    def media_roots(self) -> list[Path]:
        return [Path(self.settings.generated_media_dir).resolve(), Path(self.settings.local_media_dir).resolve()]

    def local_media_file(self, reference: str | None) -> Path | None:
        """The engine-owned file a media reference points at, or ``None``.

        Local paths count only inside the engine's media folders (generated images, local video output);
        a ``…/media/<path>`` URL maps back to the generated-media folder. Bucket URLs are never local.
        """
        if not reference:
            return None
        if reference.lower().startswith(("http://", "https://")):
            path = urlparse(reference).path
            if not path.startswith("/media/"):
                return None
            candidate = self.media_roots[0] / path.removeprefix("/media/")
        else:
            candidate = Path(reference)
        try:
            file = candidate.resolve()
        except OSError:
            return None
        if not any(root in file.parents for root in self.media_roots) or not file.is_file():
            return None
        return file

    def remove_orphan_media(self, *references: str | None) -> bool:
        """Delete a deleted row's media file once no publication or generated asset points at it any more."""
        refs = [ref for ref in references if ref]
        removed = False
        for file in dict.fromkeys(f for f in map(self.local_media_file, refs) if f is not None):
            if self.db.media_reference_count(*refs, str(file)):
                continue  # another publication or asset still uses this file
            try:
                file.unlink()
                removed = True
            except OSError as exc:
                logger.warning("Could not remove %s: %s", file, exc)
        return removed

    # -- rows ------------------------------------------------------------------- #
    def _asset(self, asset_id: int) -> dict[str, Any]:
        row = self.db.get_generated_asset(asset_id)
        if row is None:
            raise ReviewError(f"No generated asset #{asset_id}.", 404)
        return row

    def _publication(self, publication_id: int) -> dict[str, Any]:
        row = self.db.get_publication(publication_id)
        if row is None:
            raise ReviewError(f"No publication #{publication_id}.", 404)
        return row
