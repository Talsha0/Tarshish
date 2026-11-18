"""Mobile Device Preview Simulator: an unpublished post inside a smartphone, in its platform's skin.

:func:`render_phone` builds a self-contained HTML document: a 375×812-point screen in a titanium bezel
(Dynamic Island, ``9:41`` status bar, home indicator) showing a :class:`~services.content_review.ReviewItem`
the way its audience will see it:

* ``instagram_post``  — account header, 4:5 branded image, action row, likes, caption + "Link in bio";
* ``instagram_story`` — full-bleed 9:16 image, progress bars, clickable link stickers, reply bar;
* ``vertical_video``  — Instagram Reels / TikTok / YouTube Shorts: right-hand engagement rail, audio
  disc, bottom-left handle, caption and ``#Shorts`` / ``#FindHub`` tags, each app's navigation;
* ``telegram``        — the FindHub Deals channel bubble, image or link preview, views and time, and the
  ``View on FindHub`` / ``Direct Deal`` inline URL buttons.

Hebrew posts read right-to-left: the caption block takes ``dir="rtl"`` and hashtags, mentions, URLs and
the handle are isolated with ``<bdi>`` so Latin runs keep their order (``#FindHub``, not ``FindHub#``).
App chrome stays where the apps draw it and is localized.

The document scales itself to the frame it is given (never clipped vertically), so
:func:`mobile_device_simulator` embeds it with ``st.iframe`` at any column width. Every piece of post
text is HTML-escaped and only ``http(s)`` links become anchors, so database content cannot inject markup.
Local generated images are inlined as ``data:`` URIs, so they render without the API serving ``/media``.

Icons are Lucide outlines (https://lucide.dev, ISC license).
"""

from __future__ import annotations

import base64
import html
import mimetypes
import random
import re
from pathlib import Path
from typing import Any, Literal

from services.content_review import ContentReview, ReviewItem

Appearance = Literal["light", "dark"]

SCREEN_W, SCREEN_H = 375, 812          # iPhone-class logical points
BEZEL = 12
FRAME_W, FRAME_H = SCREEN_W + 2 * BEZEL, SCREEN_H + 2 * BEZEL
SAFE_TOP = 50                           # status bar + Dynamic Island
PHONE_HEIGHT = 760                      # default iframe height in the dashboard (scale ≈ 0.9)
MAX_INLINE_IMAGE_BYTES = 8 * 1024 * 1024
_IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}

# --------------------------------------------------------------------------- #
# Lucide icons (24×24 outlines)
# --------------------------------------------------------------------------- #
ICONS: dict[str, str] = {
    "chevron-left": '<path d="m15 18-6-6 6-6"/>',
    "ellipsis": '<circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/><circle cx="5" cy="12" r="1"/>',
    "heart": '<path d="M2 9.5a5.5 5.5 0 0 1 9.591-3.676.56.56 0 0 0 .818 0A5.49 5.49 0 0 1 22 9.5c0 2.29-1.5 4-3 5.5l-5.492 5.313a2 2 0 0 1-3 .019L5 15c-1.5-1.5-3-3.2-3-5.5"/>',
    "message-circle": '<path d="M2.992 16.342a2 2 0 0 1 .094 1.167l-1.065 3.29a1 1 0 0 0 1.236 1.168l3.413-.998a2 2 0 0 1 1.099.092 10 10 0 1 0-4.777-4.719"/>',
    "send": '<path d="M14.536 21.686a.5.5 0 0 0 .937-.024l6.5-19a.496.496 0 0 0-.635-.635l-19 6.5a.5.5 0 0 0-.024.937l7.93 3.18a2 2 0 0 1 1.112 1.11z"/><path d="m21.854 2.147-10.94 10.939"/>',
    "bookmark": '<path d="M17 3a2 2 0 0 1 2 2v15a1 1 0 0 1-1.496.868l-4.512-2.578a2 2 0 0 0-1.984 0l-4.512 2.578A1 1 0 0 1 5 20V5a2 2 0 0 1 2-2z"/>',
    "house": '<path d="M15 21v-8a1 1 0 0 0-1-1h-4a1 1 0 0 0-1 1v8"/><path d="M3 10a2 2 0 0 1 .709-1.528l7-6a2 2 0 0 1 2.582 0l7 6A2 2 0 0 1 21 10v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    "search": '<path d="m21 21-4.34-4.34"/><circle cx="11" cy="11" r="8"/>',
    "square-plus": '<rect width="18" height="18" x="3" y="3" rx="2"/><path d="M8 12h8"/><path d="M12 8v8"/>',
    "clapperboard": '<path d="m12.296 3.464 3.02 3.956"/><path d="M20.2 6 3 11l-.9-2.4c-.3-1.1.3-2.2 1.3-2.5l13.5-4c1.1-.3 2.2.3 2.5 1.3z"/><path d="M3 11h18v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><path d="m6.18 5.276 3.1 3.899"/>',
    "link-2": '<path d="M9 17H7A5 5 0 0 1 7 7h2"/><path d="M15 7h2a5 5 0 1 1 0 10h-2"/><line x1="8" x2="16" y1="12" y2="12"/>',
    "x": '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
    "camera": '<path d="M13.997 4a2 2 0 0 1 1.76 1.05l.486.9A2 2 0 0 0 18.003 7H20a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V9a2 2 0 0 1 2-2h1.997a2 2 0 0 0 1.759-1.048l.489-.904A2 2 0 0 1 10.004 4z"/><circle cx="12" cy="13" r="3"/>',
    "plus": '<path d="M5 12h14"/><path d="M12 5v14"/>',
    "music": '<path d="m19 6-5.2-3.9a.5.5 0 0 0-.8.4V18"/><circle cx="9" cy="18" r="4"/>',
    "share-2": '<circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><line x1="8.59" x2="15.42" y1="13.51" y2="17.49"/><line x1="15.41" x2="8.59" y1="6.51" y2="10.49"/>',
    "thumbs-up": '<path d="M15 5.88 14 10h5.83a2 2 0 0 1 1.92 2.56l-2.33 8A2 2 0 0 1 17.5 22H4a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2h2.76a2 2 0 0 0 1.79-1.11L12 2a3.13 3.13 0 0 1 3 3.88Z"/><path d="M7 10v12"/>',
    "thumbs-down": '<path d="M9 18.12 10 14H4.17a2 2 0 0 1-1.92-2.56l2.33-8A2 2 0 0 1 6.5 2H20a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-2.76a2 2 0 0 0-1.79 1.11L12 22a3.13 3.13 0 0 1-3-3.88Z"/><path d="M17 14V2"/>',
    "message-square-text": '<path d="M22 17a2 2 0 0 1-2 2H6.828a2 2 0 0 0-1.414.586l-2.202 2.202A.71.71 0 0 1 2 21.286V5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2z"/><path d="M7 11h10"/><path d="M7 15h6"/><path d="M7 7h8"/>',
    "repeat-2": '<path d="m2 9 3-3 3 3"/><path d="M13 18H7a2 2 0 0 1-2-2V6"/><path d="m22 15-3 3-3-3"/><path d="M11 6h6a2 2 0 0 1 2 2v10"/>',
    "square-play": '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M9 9.003a1 1 0 0 1 1.517-.859l4.997 2.997a1 1 0 0 1 0 1.718l-4.997 2.997A1 1 0 0 1 9 14.996z"/>',
    "users": '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><path d="M16 3.128a4 4 0 0 1 0 7.744"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><circle cx="9" cy="7" r="4"/>',
    "inbox": '<polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>',
    "user": '<path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>',
    "eye": '<path d="M2.062 12.348a1 1 0 0 1 0-.696 10.75 10.75 0 0 1 19.876 0 1 1 0 0 1 0 .696 10.75 10.75 0 0 1-19.876 0"/><circle cx="12" cy="12" r="3"/>',
    "bell-off": '<path d="M10.268 21a2 2 0 0 0 3.464 0"/><path d="M17 17H4a1 1 0 0 1-.74-1.673C4.59 13.956 6 12.499 6 8a6 6 0 0 1 .258-1.742"/><path d="m2 2 20 20"/><path d="M8.668 3.01A6 6 0 0 1 18 8c0 2.687.77 4.653 1.707 6.05"/>',
    "arrow-up-right": '<path d="M7 7h10v10"/><path d="M7 17 17 7"/>',
    "signal": '<path d="M2 20h.01"/><path d="M7 20v-4"/><path d="M12 20v-8"/><path d="M17 20V8"/><path d="M22 4v16"/>',
    "wifi": '<path d="M12 20h.01"/><path d="M2 8.82a15 15 0 0 1 20 0"/><path d="M5 12.859a10 10 0 0 1 14 0"/><path d="M8.5 16.429a5 5 0 0 1 7 0"/>',
    "battery-full": '<path d="M10 10v4"/><path d="M14 10v4"/><path d="M22 14v-4"/><path d="M6 10v4"/><rect x="2" y="6" width="16" height="12" rx="2"/>',
    "image-off": '<line x1="2" x2="22" y1="2" y2="22"/><path d="M10.41 10.41a2 2 0 1 1-2.83-2.83"/><line x1="13.5" x2="6" y1="13.5" y2="21"/><line x1="18" x2="21" y1="12" y2="15"/><path d="M3.59 3.59A1.99 1.99 0 0 0 3 5v14a2 2 0 0 0 2 2h14c.55 0 1.052-.22 1.41-.59"/><path d="M21 15V5a2 2 0 0 0-2-2H9"/>',
}


def icon(name: str, size: float = 24, *, stroke: float = 2, fill: bool = False, cls: str = "", style: str = "") -> str:
    fill_attr = "currentColor" if fill else "none"
    return (f'<svg class="i {cls}" style="width:{size}px;height:{size}px;{style}" viewBox="0 0 24 24" fill="{fill_attr}" '
            f'stroke="currentColor" stroke-width="{stroke}" stroke-linecap="round" stroke-linejoin="round" '
            f'aria-hidden="true">{ICONS[name]}</svg>')


# --------------------------------------------------------------------------- #
# Localized app chrome (the audience sees the apps in the post's language)
# --------------------------------------------------------------------------- #
CHROME: dict[str, dict[str, str]] = {
    "he": {
        "likes": "{n} סימוני לייק", "view_comments": "הצגת כל {n} התגובות", "more": "עוד",
        "send_message": "שליחת הודעה", "view_on_findhub": "צפייה ב־FindHub", "direct_deal": "לדיל ישיר",
        "subscribers": "{n} מנויים", "mute": "השתקה", "follow": "מעקב", "subscribe": "הרשמה",
        "original_audio": "אודיו מקורי", "hours_ago": "לפני 2 שע׳", "posts": "פוסטים", "today": "היום",
    },
    "en": {
        "likes": "{n} likes", "view_comments": "View all {n} comments", "more": "more",
        "send_message": "Send message", "view_on_findhub": "View on FindHub", "direct_deal": "Direct Deal",
        "subscribers": "{n} subscribers", "mute": "Mute", "follow": "Follow", "subscribe": "Subscribe",
        "original_audio": "Original audio", "hours_ago": "2 hours ago", "posts": "Posts", "today": "Today",
    },
}


def chrome_for(language: str) -> dict[str, str]:
    return CHROME["he"] if language.split("-")[0] == "he" else CHROME["en"]


def engagement(seed: int) -> dict[str, int]:
    """Stable pseudo-random engagement numbers per item (a preview must not flicker between reruns)."""
    rand = random.Random(seed * 7919 + 17)
    return {
        "likes": rand.randint(800, 14800), "comments": rand.randint(12, 392), "shares": rand.randint(20, 920),
        "saves": rand.randint(30, 1230), "views": rand.randint(2500, 62500), "subscribers": rand.randint(1200, 41200),
    }


def compact(n: float) -> str:
    for size, unit in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(n) >= size:
            return f"{n / size:.1f}".rstrip("0").rstrip(".") + unit
    return str(int(n))


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


# --------------------------------------------------------------------------- #
# Post text: hashtags, mentions and URLs coloured, "link in bio" highlighted
# --------------------------------------------------------------------------- #
_TOKEN = re.compile(r"(https?://[^\s<>\"']+)|(#[\w֑-ׇ׳״]+)|(@[\w.]+)")
_CTA = re.compile(r"link in bio|לינק בביו|קישור בביו|הקישור בביו", re.IGNORECASE)


def _tokens(line: str) -> str:
    out: list[str] = []
    last = 0
    for match in _TOKEN.finditer(line):
        out.append(esc(line[last:match.start()]))
        url, tag, mention = match.groups()
        if url:
            out.append(f'<bdi class="t-link"><a href="{esc(url)}" target="_blank" rel="noopener noreferrer">{esc(url)}</a></bdi>')
        elif tag:
            out.append(f'<bdi class="t-tag">{esc(tag)}</bdi>')
        else:
            out.append(f'<bdi class="t-tag">{esc(mention)}</bdi>')
        last = match.end()
    out.append(esc(line[last:]))
    return "".join(out)


def rich_text(text: str, direction: str, *, lead: str | None = None, cls: str = "") -> str:
    """Escaped post text in a block whose direction follows the post's language."""
    lines = []
    for line in text.split("\n"):
        lines.append(f'<mark class="t-cta">{_tokens(line)}</mark>' if _CTA.search(line) else _tokens(line))
    body = "\n".join(lines)
    lead_html = f'<bdi class="lead">{esc(lead)}</bdi> ' if lead else ""
    return f'<div class="rich {cls}" dir="{direction}">{lead_html}{body}</div>'


def clip(text: str, limit: int) -> tuple[str, bool]:
    """First two lines / ``limit`` characters on a word boundary, for the collapsed "… more" captions."""
    first = "\n".join(text.split("\n")[:2])
    base = first if len(first) < len(text) else text
    if len(base) <= limit:
        return base, len(base) < len(text)
    cut = base[:limit]
    space = cut.rfind(" ")
    return (cut[:space] if space > limit * 0.6 else cut).rstrip(), True


def caption_block(text: str, direction: str, more: str, *, limit: int, expanded: bool, lead: str | None = None,
                  cls: str = "") -> str:
    """A caption that shows "… more" collapsed and expands on click (both states rendered, CSS toggles)."""
    short, clipped = clip(text, limit)
    if not clipped:
        return rich_text(text, direction, lead=lead, cls=cls)
    state = "open" if expanded else ""
    return (f'<div class="cap {state}" onclick="this.classList.toggle(\'open\')" role="button" tabindex="0">'
            f'<div class="cap-short">{rich_text(short, direction, lead=lead, cls=cls)}'
            f'<span class="more" dir="{direction}">… {esc(more)}</span></div>'
            f'<div class="cap-full">{rich_text(text, direction, lead=lead, cls=cls)}</div></div>')


# --------------------------------------------------------------------------- #
# Shared pieces
# --------------------------------------------------------------------------- #
def avatar(size: int = 32, *, ring: bool = False) -> str:
    inner = f'<span class="avatar" style="width:{size}px;height:{size}px;font-size:{size * 0.45:.1f}px">F</span>'
    return f'<span class="ring"><span class="ring-in">{inner}</span></span>' if ring else inner


def media(item: ReviewItem, src: str | None, *, fit: str = "cover") -> str:
    """The post's visual: a looping muted video, else the image, else (or when either fails to load) a
    branded placeholder."""
    placeholder = (f'<div class="media placeholder"__HIDDEN__>{icon("image-off", 28)}<p dir="auto">{esc(item.product_title)}</p>'
                   '<small>FindHub · no image rendered yet</small></div>')
    fallback = "this.nextElementSibling.style.display='flex';this.remove()"
    if item.video_url:
        poster = f' poster="{esc(src)}"' if src else ""
        return (f'<video class="media" style="object-fit:{fit}" src="{esc(item.video_url)}"{poster} autoplay muted loop '
                f'playsinline onerror="{fallback}"></video>' + placeholder.replace("__HIDDEN__", ' style="display:none"', 1))
    if src:
        return (f'<img class="media" style="object-fit:{fit}" src="{esc(src)}" alt="{esc(item.product_title)}" '
                f'draggable="false" onerror="{fallback}">' + placeholder.replace("__HIDDEN__", ' style="display:none"', 1))
    return placeholder.replace("__HIDDEN__", "", 1)


def _handle(item: ReviewItem) -> str:
    return item.handle.removeprefix("@")


def _host(url: str | None) -> str:
    if not url:
        return "findhub"
    host = re.sub(r"^https?://", "", url).split("/")[0]
    return host.removeprefix("www.") or "findhub"


# --------------------------------------------------------------------------- #
# Skins
# --------------------------------------------------------------------------- #
def instagram_post(item: ReviewItem, text: str, src: str | None, appearance: Appearance, expanded: bool) -> str:
    t = chrome_for(item.language)
    stats = engagement(item.id)
    handle = _handle(item)
    caption = caption_block(text, item.dir, t["more"], limit=90, expanded=expanded, lead=handle)
    return f"""
<div class="ig {appearance}" style="padding-top:{SAFE_TOP}px">
  <div class="ig-top">{icon("chevron-left", 28, stroke=1.75)}
    <div class="ig-title"><p class="soft upper">{esc(handle)}</p><p class="bold">{esc(t["posts"])}</p></div><span style="width:28px"></span></div>
  <div class="scroll">
    <div class="ig-head">{avatar(32, ring=True)}
      <div class="ig-who"><p class="bold">{esc(handle)}</p><p class="small">FindHub · {esc(item.category_id)}</p></div>{icon("ellipsis", 20)}</div>
    <div class="ig-image">{media(item, src)}</div>
    <div class="ig-actions">{icon("heart", 25, stroke=1.8)}{icon("message-circle", 25, stroke=1.8, style="transform:scaleX(-1)")}
      {icon("send", 23, stroke=1.8)}<span class="grow"></span>{icon("bookmark", 25, stroke=1.8)}</div>
    <div class="ig-body">
      <p class="bold" dir="{item.dir}">{esc(t["likes"].format(n=f"{stats['likes']:,}"))}</p>
      {caption}
      <p class="soft" dir="{item.dir}">{esc(t["view_comments"].format(n=stats["comments"]))}</p>
      <p class="soft tiny" dir="{item.dir}">{esc(t["hours_ago"])}</p>
    </div>
  </div>
  <div class="ig-tabs">{icon("house", 24, stroke=2.1)}{icon("search", 24, stroke=1.8)}{icon("square-plus", 24, stroke=1.8)}
    {icon("clapperboard", 24, stroke=1.8)}{avatar(24)}</div>
</div>"""


def instagram_story(item: ReviewItem, src: str | None) -> str:
    t = chrome_for(item.language)
    stickers = "".join(
        f'<a class="sticker" style="--tilt:{-1.5 if index % 2 == 0 else 1.5}deg;animation-delay:{0.12 * index:.2f}s" '
        f'href="{esc(s["url"])}" target="_blank" rel="noopener noreferrer" title="{esc(s["label"])} → {esc(_host(s["url"]))}">'
        f'{icon("link-2", 16, stroke=2.5, cls="sticker-icon")}<span>{esc(s["label"])}</span></a>'
        for index, s in enumerate(item.links.stickers)
    )
    return f"""
<div class="story" style="padding-top:{SAFE_TOP - 6}px">
  <div class="story-card">
    {media(item, src)}
    <div class="shade-top"></div>
    <div class="story-bars" dir="ltr"><span><i class="run"></i></span><span></span><span></span></div>
    <div class="story-head" dir="{item.dir}">{avatar(32)}<bdi class="bold">{esc(_handle(item))}</bdi><span class="dim">2h</span>
      <span class="grow"></span>{icon("ellipsis", 24)}{icon("x", 28, stroke=1.75)}</div>
    {f'<div class="stickers">{stickers}</div>' if stickers else ""}
  </div>
  <div class="story-reply" dir="{item.dir}"><div class="reply-box">{esc(t["send_message"])}</div>{icon("heart", 28, stroke=1.7)}
    {icon("send", 26, stroke=1.7)}</div>
</div>"""


NAV_H = 82


def vertical_video(item: ReviewItem, text: str, src: str | None, expanded: bool) -> str:
    variant = item.variant or "tiktok"
    tiktok, shorts = variant == "tiktok", variant == "youtube_short"
    t = chrome_for(item.language)
    stats = engagement(item.id)
    handle = _handle(item)
    tags = " ".join(tag for tag, missing in (
        ("#Shorts", shorts and not re.search(r"#shorts\b", text, re.IGNORECASE)),
        ("#FindHub", not re.search(r"#findhub\b", text, re.IGNORECASE)),
    ) if missing)

    if shorts:
        rail = [("thumbs-up", compact(stats["likes"])), ("thumbs-down", "Dislike"),
                ("message-square-text", compact(stats["comments"])), ("share-2", "Share"), ("repeat-2", "Remix")]
    elif tiktok:
        rail = [("heart", compact(stats["likes"])), ("message-circle", compact(stats["comments"])),
                ("bookmark", compact(stats["saves"])), ("share-2", compact(stats["shares"]))]
    else:
        rail = [("heart", compact(stats["likes"])), ("message-circle", compact(stats["comments"])),
                ("send", compact(stats["shares"])), ("ellipsis", "")]
    size, stroke = (36, 1.25) if tiktok else (24, 1.9) if shorts else (28, 1.9)
    rail_html = "".join(
        f'<span class="rail-item"><span class="{"pill" if shorts else ""}">{icon(name, size, stroke=stroke, fill=tiktok and name != "ellipsis")}</span>'
        + (f'<span class="rail-label">{esc(label)}</span>' if label else "") + "</span>"
        for name, label in rail
    )
    follow = "" if tiktok else (
        f'<span class="follow {"sub" if shorts else ""}">{esc(t["subscribe"] if shorts else t["follow"])}</span>')
    if tiktok:
        top = ('<span style="width:24px"></span><div class="tt-tabs"><span class="dim">Following</span>'
               f'<span class="active">For You</span></div>{icon("search", 24)}')
        nav = (_nav("house", "Home", True) + _nav("users", "Friends") +
               f'<span class="tt-plus">{icon("plus", 20, stroke=3)}</span>' + _nav("inbox", "Inbox") + _nav("user", "Profile"))
    elif shorts:
        top = f'<span class="bold" style="font-size:17px">Shorts</span><span class="grow"></span>{icon("search", 24)}{icon("ellipsis", 24, style="transform:rotate(90deg)")}'
        nav = (_nav("house", "Home") + _nav("square-play", "Shorts", True) +
               f'<span class="yt-plus">{icon("plus", 24, stroke=1.5)}</span>' + _nav("clapperboard", "Subscriptions") +
               f'<span class="nav-item">{avatar(24)}You</span>')
    else:
        top = f'<span class="bold" style="font-size:22px">Reels</span><span class="grow"></span>{icon("camera", 28, stroke=1.75)}'
        nav = (icon("house", 24, stroke=1.8) + icon("search", 24, stroke=1.8) + icon("square-plus", 24, stroke=1.8) +
               icon("clapperboard", 24, stroke=2.4) + avatar(24))
    profile = (f'<span class="tt-avatar">{avatar(44)}<span class="tt-follow">{icon("plus", 14, stroke=3)}</span></span>'
               if tiktok else "")
    caption = caption_block(text, item.dir, t["more"], limit=70, expanded=expanded, cls="vv-caption")
    marquee_dir = "rtl" if item.dir == "rtl" else "ltr"
    progress = f'<div class="shorts-progress" style="bottom:{NAV_H}px"><i></i></div>' if shorts else ""
    return f"""
<div class="vv {variant}">
  <div class="vv-media">{media(item, src)}</div>
  <div class="shade-top tall"></div><div class="shade-bottom"></div>
  <div class="vv-top" style="top:{SAFE_TOP + 2}px" dir="ltr">{top}</div>
  <div class="rail" style="bottom:{NAV_H + (56 if tiktok else 48)}px">{profile}{rail_html}
    <span class="disc {"square" if not tiktok else ""}">{avatar(34) if not tiktok else '<i></i>'}</span></div>
  <div class="vv-info" style="bottom:{NAV_H + (14 if shorts else 8)}px" dir="{item.dir}">
    <div class="vv-who">{"" if tiktok else avatar(32)}<bdi class="bold">{esc(handle if tiktok else "@" + handle)}</bdi>{follow}</div>
    {caption}
    {f'<p class="vv-tags">{esc(tags)}</p>' if tags else ""}
    <div class="audio">{icon("music", 14)}<div class="marquee"><span class="{marquee_dir}">{esc(t["original_audio"])} · {esc(handle)} · {esc(t["original_audio"])} · {esc(handle)}</span></div></div>
  </div>
  {progress}
  <div class="vv-nav {"solid" if tiktok or shorts else ""}" style="height:{NAV_H}px" dir="ltr">{nav}</div>
</div>"""


def _nav(name: str, label: str, active: bool = False) -> str:
    return (f'<span class="nav-item {"active" if active else ""}">'
            f'{icon(name, 24, stroke=2.3 if active else 1.7, fill=active)}{esc(label)}</span>')


def telegram(item: ReviewItem, text: str, src: str | None, appearance: Appearance) -> str:
    t = chrome_for(item.language)
    stats = engagement(item.id)
    link_preview = item.source == "asset" and item.content_type == "telegram_post"
    buttons = [(label, url) for label, url in (
        (t["view_on_findhub"], item.links.product_url or item.links.storefront_url),
        (t["direct_deal"], item.links.affiliate_url),
    ) if url]
    clock = (item.created_at or "")[11:16] or "14:32"
    body = rich_text(text, item.dir, cls="tg-text")
    image = "" if link_preview else f'<div class="tg-image">{media(item, src)}</div>'
    preview = (f'<div class="tg-preview"><div class="grow"><p class="tg-site">{esc(_host(item.links.affiliate_url))}</p>'
               f'<p class="tg-ptitle" dir="auto">{esc(item.product_title)}</p></div>'
               f'<div class="tg-thumb">{media(item, src)}</div></div>') if link_preview else ""
    keyboard = "".join(
        f'<a class="tg-btn" href="{esc(url)}" target="_blank" rel="noopener noreferrer" title="{esc(url)}">'
        f'<span>{esc(label)}</span>{icon("arrow-up-right", 12, cls="tg-arrow")}</a>'
        for label, url in buttons
    )
    return f"""
<div class="tg {appearance}">
  <div class="tg-header" style="padding-top:{SAFE_TOP}px"><div class="tg-bar" dir="ltr">{icon("chevron-left", 28, cls="tg-blue")}
    <div class="tg-title"><p class="bold">FindHub Deals</p><p class="soft">{esc(t["subscribers"].format(n=f"{stats['subscribers']:,}"))}</p></div>{avatar(36)}</div></div>
  <div class="tg-chat scroll">
    <div class="tg-day"><span>{esc(t["today"])}</span></div>
    <div class="tg-post">
      <div class="tg-bubble">{image}
        <div class="tg-content"><p class="tg-channel">FindHub Deals</p>{body}{preview}
          <div class="tg-meta" dir="ltr">{icon("eye", 14)}{compact(stats["views"])}<span>{esc(clock)}</span></div></div>
      </div>
      {f'<div class="tg-keyboard" dir="{item.dir}">{keyboard}</div>' if keyboard else ""}
    </div>
  </div>
  <div class="tg-footer" dir="ltr">{icon("search", 24, cls="tg-blue")}<span class="tg-mute">{icon("bell-off", 16)} {esc(t["mute"])}</span><span style="width:24px"></span></div>
</div>"""


# --------------------------------------------------------------------------- #
# The device
# --------------------------------------------------------------------------- #
_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;background:transparent;overflow:hidden}
body{font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",Roboto,"Noto Sans Hebrew","Noto Sans",Arial,sans-serif;
  -webkit-font-smoothing:antialiased;color:#0c1014}
a{color:inherit;text-decoration:none}
.stage{width:100vw;height:100vh;display:flex;align-items:center;justify-content:center}
.fit{position:relative;flex:none}
.device{position:absolute;left:0;top:0;width:FRAME_Wpx;height:FRAME_Hpx;transform-origin:top left}
.band{position:absolute;inset:0;border-radius:60px;padding:3px;background:linear-gradient(135deg,#71717a,#27272a 45%,#52525b);
  box-shadow:0 40px 80px -30px rgba(0,0,0,.6),0 18px 36px -18px rgba(0,0,0,.45)}
.band>div{width:100%;height:100%;border-radius:57px;background:#000}
.hw{position:absolute;width:4px;background:#3f3f46}
.hw.l{left:-3px;border-radius:3px 0 0 3px}.hw.r{right:-3px;border-radius:0 3px 3px 0}
.screen{position:absolute;left:BEZELpx;top:BEZELpx;width:SCREEN_Wpx;height:SCREEN_Hpx;border-radius:48px;overflow:hidden;background:#fff}
.screen.black{background:#000}
.status{position:absolute;left:0;right:0;top:0;height:50px;z-index:30;display:flex;align-items:center;justify-content:space-between;
  padding:4px 28px 0;font-size:15px;font-weight:600;pointer-events:none}
.status .clock{width:70px;text-align:center;font-variant-numeric:tabular-nums}
.status .sys{width:70px;display:flex;gap:4px;align-items:center;justify-content:center}
.light-ui{color:#fff}.dark-ui{color:#000}
.island{position:absolute;left:50%;top:11px;width:122px;height:35px;margin-left:-61px;border-radius:999px;background:#000;z-index:30}
.home{position:absolute;bottom:8px;left:50%;width:134px;height:5px;margin-left:-67px;border-radius:999px;z-index:30;background:rgba(0,0,0,.85)}
.home.light{background:rgba(255,255,255,.9)}
svg.i{flex:none;display:block}
.grow{flex:1;min-width:0}
.bold{font-weight:600}.soft{opacity:.55}.tiny{font-size:11px}.small{font-size:11.5px}.upper{text-transform:uppercase;font-size:11px;font-weight:600}
.dim{opacity:.65}
.scroll{overflow-y:auto;scrollbar-width:none;min-height:0;flex:1}.scroll::-webkit-scrollbar{display:none}
.avatar{display:inline-flex;align-items:center;justify-content:center;border-radius:50%;background:#0f6e52;color:#ffd23f;
  font-weight:700;flex:none;box-shadow:inset 0 1px 3px rgba(0,0,0,.25)}
.ring{display:inline-block;border-radius:50%;padding:2px;background:linear-gradient(45deg,#feda75,#d62976 55%,#4f5bd5);flex:none}
.ring-in{display:block;border-radius:50%;padding:2px;background:#000}
.ig.light .ring-in{background:#fff}
.media{display:block;width:100%;height:100%}
.media.placeholder{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:8px;padding:24px;text-align:center;
  color:#fff;background:linear-gradient(135deg,#0f6e52,#0a3d2e)}
.media.placeholder p{font-size:14px;font-weight:600}.media.placeholder small{font-size:11px;opacity:.7}
.rich{white-space:pre-wrap;word-break:break-word;text-align:start;unicode-bidi:isolate}
.t-cta{border-radius:4px;padding:0 4px;font-weight:600;background:rgba(0,149,246,.12);color:#0064b0}
.cap{cursor:pointer}.cap .cap-full{display:none}.cap.open .cap-full{display:block}.cap.open .cap-short{display:none}
.cap-short .rich{display:inline}.more{opacity:.55}
/* Instagram post */
.ig{height:100%;display:flex;flex-direction:column;background:#fff;color:#0c1014}
.ig.dark{background:#000;color:#fff}
.ig-top{height:44px;flex:none;display:flex;align-items:center;gap:12px;padding:0 12px}
.ig-title{flex:1;text-align:center;line-height:1.2}.ig-title .bold{font-size:15px;font-weight:700}
.ig-head{display:flex;align-items:center;gap:10px;padding:8px 12px}
.ig-who{flex:1;min-width:0;line-height:1.2}.ig-who .bold{font-size:13.5px}
.ig-image{aspect-ratio:4/5;width:100%;overflow:hidden;background:#efefef}
.ig-actions{display:flex;align-items:center;gap:16px;padding:10px 12px 6px}
.ig-body{padding:0 12px 24px;font-size:13.5px;line-height:1.35;display:flex;flex-direction:column;gap:4px}
.ig .lead{font-weight:600}
.ig .t-tag,.ig .t-link{color:#00376b}.ig.dark .t-tag,.ig.dark .t-link{color:#e0f1ff}
.ig.dark .t-cta{background:rgba(0,149,246,.25);color:#8fd0ff}
.ig-tabs{height:80px;flex:none;display:flex;align-items:flex-start;justify-content:space-around;padding-top:10px;border-top:1px solid rgba(0,0,0,.06)}
.ig.dark .ig-tabs{border-color:rgba(255,255,255,.1)}
/* Instagram Story */
.story{height:100%;display:flex;flex-direction:column;background:#000;color:#fff}
.story-card{position:relative;aspect-ratio:9/16;width:100%;flex:none;overflow:hidden;border-radius:12px;background:#18181b}
.shade-top{position:absolute;left:0;right:0;top:0;height:112px;background:linear-gradient(to bottom,rgba(0,0,0,.55),transparent);pointer-events:none}
.shade-top.tall{height:144px;background:linear-gradient(to bottom,rgba(0,0,0,.5),transparent)}
.shade-bottom{position:absolute;left:0;right:0;bottom:0;height:55%;pointer-events:none;
  background:linear-gradient(to top,rgba(0,0,0,.85),rgba(0,0,0,.35) 50%,transparent)}
.story-bars{position:absolute;left:8px;right:8px;top:8px;display:flex;gap:4px}
.story-bars span{flex:1;height:2px;border-radius:2px;background:rgba(255,255,255,.35);overflow:hidden}
.story-bars .run{display:block;height:100%;width:0;background:#fff;animation:fill 5s linear infinite}
@keyframes fill{from{width:0}to{width:100%}}
.story-head{position:absolute;left:12px;right:12px;top:20px;display:flex;align-items:center;gap:8px;font-size:13.5px;
  text-shadow:0 1px 2px rgba(0,0,0,.5)}
.stickers{position:absolute;left:0;right:0;bottom:6%;display:flex;flex-direction:column;align-items:center;gap:10px;padding:0 24px}
.sticker{display:flex;align-items:center;gap:6px;max-width:100%;padding:8px 12px;border-radius:8px;background:#fff;
  box-shadow:0 6px 18px rgba(0,0,0,.35);transform:rotate(var(--tilt));animation:pop .45s cubic-bezier(.34,1.56,.64,1) both;
  transition:transform .15s}
.sticker:hover{transform:scale(1.05) rotate(0)}
.sticker span{font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:.03em;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis;background:linear-gradient(90deg,#2f6cf6,#8a3ab9);-webkit-background-clip:text;background-clip:text;color:transparent}
.sticker-icon{color:#2f6cf6;transform:rotate(-45deg)}
@keyframes pop{from{opacity:0;transform:scale(.85) translateY(8px)}to{opacity:1;transform:rotate(var(--tilt))}}
.story-reply{flex:1;display:flex;align-items:center;gap:12px;padding:0 12px}
.reply-box{flex:1;height:44px;display:flex;align-items:center;padding:0 16px;border:1px solid rgba(255,255,255,.5);border-radius:999px;
  font-size:14px;color:rgba(255,255,255,.85)}
/* Reels / TikTok / Shorts */
.vv{position:relative;height:100%;overflow:hidden;background:#000;color:#fff}
.vv-media{position:absolute;inset:0}
.vv-top{position:absolute;left:0;right:0;display:flex;align-items:center;gap:20px;padding:0 16px;filter:drop-shadow(0 1px 2px rgba(0,0,0,.5))}
.tt-tabs{flex:1;display:flex;justify-content:center;gap:20px;font-size:16px;font-weight:600}
.tt-tabs .active{position:relative}
.tt-tabs .active::after{content:"";position:absolute;left:50%;bottom:-6px;width:28px;height:3px;margin-left:-14px;border-radius:3px;background:#fff}
.rail{position:absolute;right:10px;display:flex;flex-direction:column;align-items:center;gap:18px;filter:drop-shadow(0 1px 2px rgba(0,0,0,.45))}
.rail-item{display:flex;flex-direction:column;align-items:center;gap:4px}
.rail-label{font-size:12px;font-weight:600}
.pill{display:flex;width:48px;height:48px;align-items:center;justify-content:center;border-radius:50%;background:rgba(255,255,255,.15)}
.tt-avatar{position:relative;margin-bottom:8px;border:2px solid #fff;border-radius:50%;display:block}
.tt-avatar .avatar{display:flex}
.tt-follow{position:absolute;left:50%;bottom:-10px;margin-left:-10px;width:20px;height:20px;border-radius:50%;background:#fe2c55;
  display:flex;align-items:center;justify-content:center}
.disc{width:40px;height:40px;border-radius:50%;border:3px solid #3f3f46;background:linear-gradient(135deg,#27272a,#000);
  display:flex;align-items:center;justify-content:center;overflow:hidden;animation:spin 4s linear infinite}
.disc i{width:20px;height:20px;border-radius:50%;background:#0f6e52;box-shadow:0 0 0 2px #ffd23f}
.disc.square{border-radius:8px;border:2px solid #fff;animation:none}
@keyframes spin{to{transform:rotate(360deg)}}
.vv-info{position:absolute;left:12px;right:72px;display:flex;flex-direction:column;gap:8px;text-shadow:0 1px 2px rgba(0,0,0,.5)}
.vv-who{display:flex;align-items:center;gap:8px;font-size:15px}
.follow{border:1px solid rgba(255,255,255,.8);border-radius:999px;padding:4px 12px;font-size:12.5px;font-weight:600;text-shadow:none}
.follow.sub{background:#fff;color:#000;border-color:#fff}
.vv-caption{font-size:14px;line-height:1.35}
.vv .t-tag{font-weight:600}.vv .t-link{text-decoration:underline}.vv .t-cta{background:rgba(255,255,255,.2);color:#fff}
.vv .cap.open{max-height:220px;overflow-y:auto;scrollbar-width:none}
.vv .more{opacity:.8;font-weight:600;font-size:14px}
.vv-tags{font-size:14px;font-weight:600}
.audio{display:flex;align-items:center;gap:6px;font-size:13px;overflow:hidden}
.marquee{flex:1;min-width:0;overflow:hidden;white-space:nowrap}
.marquee span{display:inline-block;animation:marquee-ltr 7s linear infinite}
.marquee span.rtl{animation-name:marquee-rtl}
@keyframes marquee-ltr{from{transform:translateX(0)}to{transform:translateX(-40%)}}
@keyframes marquee-rtl{from{transform:translateX(0)}to{transform:translateX(40%)}}
.shorts-progress{position:absolute;left:0;right:0;height:3px;background:rgba(255,255,255,.25)}
.shorts-progress i{display:block;height:100%;width:0;background:#ff0033;animation:fill 15s linear infinite}
.vv-nav{position:absolute;left:0;right:0;bottom:0;display:flex;align-items:flex-start;justify-content:space-around;padding-top:8px;
  background:linear-gradient(to top,#000,transparent)}
.vv-nav.solid{background:#000}
.nav-item{display:flex;flex-direction:column;align-items:center;gap:2px;font-size:10px;color:rgba(255,255,255,.7)}
.nav-item.active{color:#fff;font-weight:600}
.tt-plus{margin-top:2px;width:48px;height:32px;border-radius:8px;background:#fff;color:#000;display:flex;align-items:center;
  justify-content:center;box-shadow:-3px 0 0 #25f4ee,3px 0 0 #fe2c55}
.yt-plus{width:36px;height:36px;border-radius:50%;border:1px solid rgba(255,255,255,.4);display:flex;align-items:center;justify-content:center}
/* Telegram */
.tg{height:100%;display:flex;flex-direction:column;color:#000}
.tg.dark{color:#fff}
.tg-header{flex:none;background:#f6f6f6;border-bottom:1px solid rgba(0,0,0,.1)}
.tg.dark .tg-header,.tg.dark .tg-footer{background:#1c1c1d;border-color:rgba(255,255,255,.1)}
.tg-bar{height:48px;display:flex;align-items:center;gap:8px;padding:0 8px}
.tg-title{flex:1;text-align:center;line-height:1.25}.tg-title .bold{font-size:16px}.tg-title .soft{font-size:12.5px}
.tg-blue{color:#2a9ef1}
.tg-chat{position:relative;padding:8px 10px 12px;background:radial-gradient(circle at 15% 20%,#d5e6b4 0,transparent 45%),
  radial-gradient(circle at 85% 65%,#b6d39b 0,transparent 50%),radial-gradient(circle at 50% 100%,#8fbf8a 0,transparent 55%),#c7dca8}
.tg.dark .tg-chat{background:radial-gradient(circle at 20% 20%,#1d3a32 0,transparent 45%),radial-gradient(circle at 80% 70%,#18302b 0,transparent 50%),#0f1c19}
.tg-day{display:flex;justify-content:center;margin-bottom:8px}
.tg-day span{border-radius:999px;background:rgba(0,0,0,.25);color:#fff;padding:2px 10px;font-size:12px;font-weight:500}
.tg-post{max-width:92%}
.tg-bubble{overflow:hidden;border-radius:16px 16px 16px 6px;background:#fff;box-shadow:0 1px 2px rgba(0,0,0,.12)}
.tg.dark .tg-bubble{background:#212b2a}
.tg-image{aspect-ratio:4/5;max-height:330px;width:100%;overflow:hidden;background:#e4e4e7}
.tg-content{padding:8px 12px 6px}
.tg-channel{margin-bottom:2px;font-size:14px;font-weight:600;color:#3a8fd6}
.tg-text{font-size:15px;line-height:1.38}
.tg .t-tag,.tg .t-link{color:#168acd}.tg .t-cta{background:rgba(22,138,205,.1);color:#168acd}
.tg-preview{margin-top:8px;display:flex;gap:8px;padding:8px;border-radius:6px;background:rgba(58,143,214,.1);border-inline-start:3px solid #3a8fd6}
.tg-site,.tg-ptitle{font-size:13px;font-weight:600}.tg-site{color:#3a8fd6}
.tg-ptitle{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.tg-thumb{width:56px;height:56px;flex:none;overflow:hidden;border-radius:4px}
.tg-thumb .placeholder>*{display:none}
.tg-meta{margin-top:2px;display:flex;align-items:center;justify-content:flex-end;gap:4px;font-size:11.5px;opacity:.45}
.tg-meta span{margin-left:4px}
.tg-keyboard{margin-top:4px;display:grid;grid-template-columns:1fr 1fr;gap:4px}
.tg-btn{position:relative;height:40px;display:flex;align-items:center;justify-content:center;padding:0 12px;border-radius:8px;
  background:rgba(0,0,0,.3);color:#fff;font-size:14px;font-weight:500;backdrop-filter:blur(4px)}
.tg-btn:hover{background:rgba(0,0,0,.4)}
.tg-btn span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tg-arrow{position:absolute;top:4px;inset-inline-end:4px;opacity:.8}
.tg-footer{height:78px;flex:none;display:flex;align-items:flex-start;justify-content:space-between;padding:12px 16px 0;
  background:#f6f6f6;border-top:1px solid rgba(0,0,0,.1)}
.tg-mute{display:flex;align-items:center;gap:6px;font-size:16px;font-weight:500;color:#2a9ef1}
""".replace("FRAME_Wpx", f"{FRAME_W}px").replace("FRAME_Hpx", f"{FRAME_H}px").replace("BEZELpx", f"{BEZEL}px") \
   .replace("SCREEN_Wpx", f"{SCREEN_W}px").replace("SCREEN_Hpx", f"{SCREEN_H}px")

# Scale the phone to the largest size (≤ 100%) that fits the frame it is embedded in, on load and resize.
_FIT_SCRIPT = f"""
(function(){{
  var fit=document.querySelector('.fit'),device=document.querySelector('.device');
  function size(){{
    var s=Math.max(0.3,Math.min(1,(window.innerWidth-8)/{FRAME_W},(window.innerHeight-8)/{FRAME_H}));
    fit.style.width=({FRAME_W}*s)+'px';fit.style.height=({FRAME_H}*s)+'px';device.style.transform='scale('+s+')';
  }}
  size();window.addEventListener('resize',size);
}})();
"""


def screen_html(item: ReviewItem, text: str, src: str | None, appearance: Appearance, expanded: bool) -> str:
    if item.layout == "instagram_post":
        return instagram_post(item, text, src, appearance, expanded)
    if item.layout == "instagram_story":
        return instagram_story(item, src)
    if item.layout == "vertical_video":
        return vertical_video(item, text, src, expanded)
    return telegram(item, text, src, appearance)


def render_phone(
    item: ReviewItem, text: str | None = None, *, media_src: str | None = None, appearance: Appearance = "light",
    expanded: bool = False,
) -> str:
    """A complete HTML document: ``item`` on a 375×812 phone, showing ``text`` (default: the saved post text)."""
    shown = item.post_text() if text is None else text
    full_bleed = item.layout in ("instagram_story", "vertical_video")
    light_ui = full_bleed or appearance == "dark"
    src = media_src if media_src is not None else item.image_url
    label = f"{item.type_label} preview for {item.handle}"
    return f"""<!doctype html>
<html lang="{esc(item.language)}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(label)}</title><style>{_CSS}</style></head>
<body><div class="stage"><div class="fit" style="width:{FRAME_W * 0.9:.0f}px;height:{FRAME_H * 0.9:.0f}px">
<div class="device" role="img" aria-label="{esc(label)}" data-layout="{item.layout}" data-variant="{item.variant or ''}"
  data-dir="{item.dir}" style="transform:scale(0.9)">
  <div class="band"><div></div></div>
  <span class="hw l" style="top:118px;height:32px"></span><span class="hw l" style="top:178px;height:56px"></span>
  <span class="hw l" style="top:246px;height:56px"></span><span class="hw r" style="top:200px;height:80px"></span>
  <div class="screen {"black" if full_bleed or appearance == "dark" else ""}" lang="{esc(item.language)}">
    {screen_html(item, shown, src, appearance, expanded)}
    <div class="status {"light-ui" if light_ui else "dark-ui"}" dir="ltr"><span class="clock">9:41</span>
      <span class="sys">{icon("signal", 15, stroke=2.6)}{icon("wifi", 15, stroke=2.6)}{icon("battery-full", 22, stroke=2)}</span></div>
    <span class="island"></span>
    <span class="home {"light" if light_ui else ""}"></span>
  </div>
</div></div></div>
<script>{_FIT_SCRIPT}</script></body></html>"""


# --------------------------------------------------------------------------- #
# Media + Streamlit embedding
# --------------------------------------------------------------------------- #
def image_data_uri(path: Path) -> str | None:
    """A ``data:`` URI for a local image (PNG / JPEG / WebP / GIF up to 8 MB), else ``None``."""
    mime = mimetypes.guess_type(path.name)[0]
    try:
        if mime not in _IMAGE_TYPES or path.stat().st_size > MAX_INLINE_IMAGE_BYTES:
            return None
        return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"
    except OSError:
        return None


def phone_media_source(item: ReviewItem, review: ContentReview) -> str | None:
    """What the phone shows: the engine's own image file inlined (so it renders without the API serving
    ``/media``), else the public image URL, else nothing (a branded placeholder)."""
    for reference in (item.image_path, item.image_url):
        file = review.local_media_file(reference)
        if file is not None and (uri := image_data_uri(file)):
            return uri
    return item.image_url


def mobile_device_simulator(
    item: ReviewItem, text: str | None = None, *, review: ContentReview, appearance: Appearance = "light",
    expanded: bool = False, height: int = PHONE_HEIGHT,
) -> str:
    """Render the phone in the current Streamlit container (an isolated iframe) and return its HTML."""
    import streamlit as st

    document = render_phone(item, text, media_src=phone_media_source(item, review), appearance=appearance,
                            expanded=expanded)
    st.iframe(document, height=height, alt=f"{item.type_label} phone preview")
    return document
