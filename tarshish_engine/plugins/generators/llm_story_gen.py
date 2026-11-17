"""Instagram Story generator: concise overlay hooks in a vertical 9:16 frame.

Instagram Stories take **no caption** — Meta rejects one on a Story container — so the
copy this generator writes is *overlay text*: a two-to-three line hook meant to sit on
top of the product photo, plus a frame layout. It shares the provider plumbing,
pricing and budget guardrail of ``llm_text_gen``; the content profile's ``llm_model``
picks the model (a fast, cheap one is plenty for a 90-character hook).

Every generation also yields a *story frame* (``output/story_frames/<id>_<lang>.json``,
not written on a dry-run): canvas 1080×1920, the product photo fitted inside the safe
zone, and the overlay lines with their positions — the input for a renderer, or for a
designer who bakes the hook into the image before the Story is posted.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.models import GeneratedContent, Product
from core.resilience import get_logger
from plugins import format_price
from plugins.generators.llm_text_gen import LLMTextGenerator

logger = get_logger("plugins.llm_story_gen")

STORY_CANVAS = {"width": 1080, "height": 1920}
# Instagram covers roughly the top 250 px (profile bar) and bottom 340 px (reply bar) of a Story.
STORY_SAFE_ZONE = {"top": 250, "bottom": 340, "sides": 60}
MAX_OVERLAY_CHARS = 90

STORY_PROMPT_TEMPLATE = """Write the overlay text for a vertical Instagram Story (9:16) in {language_name} that promotes this product.

Product
- Title: {title}
- Price: {price}{was_price}
- Rating: {rating}/5 from buyers, {orders_count}+ orders
- Category: {category}

Guidelines
- Tone: {tone}
- The "caption" field is the text drawn ON the image: 2-3 very short lines separated by line breaks, at most 90 characters in total.
- Line 1 is a scroll-stopping hook; then the price{discount_hint}; end with a nudge to tap the link in the bio.
- At most one emoji per line. No hashtags, URLs or markdown in the caption.
- "hashtags": 0-2 hashtags for the Story's hashtag sticker."""


class StoryGenerator(LLMTextGenerator):
    """Overlay hook + vertical frame for Instagram Stories."""

    content_type = "text"
    default_prompt_template = STORY_PROMPT_TEMPLATE

    _HOOKS = {
        "he": ("🔥 {short}", "💰 רק {price}{off}", "👆 הקישור בביו"),
        "en": ("🔥 {short}", "💰 Only {price}{off}", "👆 Link in bio"),
    }
    _OFF = {"he": " · {percent}% הנחה", "en": " · {percent}% off"}

    @property
    def generator_name(self) -> str:
        return "llm_story_gen"

    def offline_write(self, product: Product, language: str) -> tuple[str, list[str]]:
        lang = language.split("-")[0]
        lang = lang if lang in self._HOOKS else "en"
        percent = product.discount_percent
        lines = [
            line.format(
                short=" ".join(product.title.split()[:4]),
                price=format_price(product.price, product.currency),
                off=self._OFF[lang].format(percent=percent) if percent else "",
            )
            for line in self._HOOKS[lang]
        ]
        return "\n".join(lines), []

    def finish(self, content: GeneratedContent) -> GeneratedContent:
        lines = [line.strip() for line in content.caption.splitlines() if line.strip()][:3]
        overlay = "\n".join(lines)
        if len(overlay) > MAX_OVERLAY_CHARS:
            logger.debug("Story overlay is %d characters (target %d).", len(overlay), MAX_OVERLAY_CHARS)
        product = content.product
        image = product.image_urls[0] if product.image_urls else None
        content.caption = overlay or content.caption
        content.hashtags = content.hashtags[:2]
        if content.media_public_url is None and image:
            content.media_public_url = image  # Instagram downloads the Story image from this URL
        if not self.context.dry_run:
            self._write_frame(content, lines, image)
        return content

    def frame(self, content: GeneratedContent, lines: list[str], image: str | None) -> dict[str, Any]:
        """The 9:16 layout: photo inside the safe zone, overlay lines in the lower third."""
        usable = STORY_CANVAS["height"] - STORY_SAFE_ZONE["top"] - STORY_SAFE_ZONE["bottom"]
        line_height = 110
        first = STORY_SAFE_ZONE["top"] + int(usable * 0.62)
        return {
            "product": content.product.ref,
            "language": content.language,
            "direction": "rtl" if content.language.split("-")[0] in ("he", "ar") else "ltr",
            "canvas": STORY_CANVAS,
            "safe_zone": STORY_SAFE_ZONE,
            "background": {"image_url": image, "fit": "contain", "max_height": int(usable * 0.6),
                           "top": STORY_SAFE_ZONE["top"]},
            "overlay": [
                {"text": text, "y": first + index * line_height, "size": 64 if index == 0 else 52,
                 "weight": "bold" if index == 0 else "regular"}
                for index, text in enumerate(lines)
            ],
            "hashtag_sticker": content.hashtags,
        }

    def _write_frame(self, content: GeneratedContent, lines: list[str], image: str | None) -> None:
        frames = Path(self.context.settings.local_media_dir) / "story_frames"
        try:
            frames.mkdir(parents=True, exist_ok=True)
            path = frames / f"{content.product.canonical_hash[:16]}_{content.language}.json"
            path.write_text(json.dumps(self.frame(content, lines, image), ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:  # the frame is a by-product: never fail the Story over it
            logger.warning("Could not write the story frame: %s", exc)
