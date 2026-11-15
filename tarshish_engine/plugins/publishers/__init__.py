"""Publisher plugins and the post-formatting helper they share.

Add a module with one concrete ``core.interfaces.BasePublisher`` subclass (for
example ``tiktok_pub.py``) and reference it from a pipeline's ``publishers`` /
``social_pages`` by its module name. No change to ``core/`` is needed.
"""

from __future__ import annotations

from core.models import GeneratedContent

_LINK_LABEL = {"he": "🛒 לרכישה:", "en": "🛒 Buy here:"}
_DISCLOSURE = {"he": "* הפוסט כולל קישור שותפים", "en": "* Contains an affiliate link"}


def disclosure_enabled(settings: object) -> bool:
    """Affiliate disclosure is on unless ``AFFILIATE_DISCLOSURE=false``."""
    value = settings.get_env("AFFILIATE_DISCLOSURE", "true")  # type: ignore[attr-defined]
    return str(value).strip().lower() not in ("false", "0", "no", "off")


def utf16_length(text: str) -> int:
    """Length the way most platform limits count it (emoji = 2 units)."""
    return len(text.encode("utf-16-le")) // 2


def render_post_text(content: GeneratedContent, *, max_length: int | None = None, disclosure: bool = True) -> str:
    """Final post body: caption, affiliate link, hashtags and an affiliate disclosure.

    The link is always appended here (never trusted to the LLM). When
    ``max_length`` is given the hashtags are dropped first and then the caption
    is shortened — the link itself is never cut.
    """
    lang = content.language.split("-")[0]
    link = f"{_LINK_LABEL.get(lang, _LINK_LABEL['en'])} {content.product.affiliate_url}"
    tags = " ".join(f"#{tag}" for tag in content.hashtags)
    note = _DISCLOSURE.get(lang, _DISCLOSURE["en"]) if disclosure else ""

    def assemble(caption: str, with_tags: bool) -> str:
        footer = "\n".join(part for part in (tags if with_tags else "", note) if part)
        return "\n\n".join(part for part in (caption, link, footer) if part)

    text = assemble(content.caption, with_tags=True)
    if max_length is None or utf16_length(text) <= max_length:
        return text

    text = assemble(content.caption, with_tags=False)
    caption = content.caption
    while utf16_length(text) > max_length and caption:
        overflow = utf16_length(text) - max_length
        caption = caption[: max(0, len(caption) - max(overflow, 1) - 1)].rstrip()
        text = assemble(f"{caption}…" if caption else "", with_tags=False)
    return text
