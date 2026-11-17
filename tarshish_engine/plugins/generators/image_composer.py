"""FindHub branded promotional images (Pillow): the real product photo on an ad-grade canvas.

Every image carries the same four brand elements, drawn in the storefront's colours (spruce,
shelf-tag yellow, flash red on cool paper):

1. **FindHub header pill** — the logo mark and wordmark, top-left.
2. **Discount tag** — ``-35% OFF`` (or ``HOT DEAL`` for a flash sale / coupon), top-right, only
   when the product really is discounted or promoted.
3. **Social-proof pill** — five gold stars, the rating and the order count (``★ 4.9 | 2,400+ Sold``).
4. **Product card + CTA bar** — the photo on a white card with a soft shadow, the price on a yellow
   shelf label, and an ``Available on FindHub`` bar at the bottom.

Canvases
--------
==================  ===========  ==========================================================
format              size         used for
==================  ===========  ==========================================================
``portrait``        1080x1350    ``instagram_post`` (4:5, the most feed real estate)
``square``          1080x1080    optional 1:1 feed post
``vertical``        1080x1920    ``instagram_story``, ``instagram_reel``, ``tiktok_post`` (9:16)
==================  ===========  ==========================================================

On 9:16 canvases nothing is drawn in the top 250 px or the bottom 340 px, where Instagram and
TikTok overlay the profile bar, the reply box and the caption. Vertical images can also carry a
short overlay hook (Stories take no caption, so the hook has to be in the picture).

Images are written as JPEG (what Meta and TikTok accept) under ``GENERATED_MEDIA_DIR``
(``static/generated_media/``), which the API serves at ``/media/...``. Text is drawn with the
first system font that has Hebrew glyphs (DejaVu Sans, Arial, Liberation Sans …); right-to-left
lines use libraqm when Pillow has it and a built-in visual reordering otherwise.
"""

from __future__ import annotations

import hashlib
import io
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Mapping

import httpx
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps, features

from core.resilience import TarshishError, get_logger, redact
from plugins import format_price

logger = get_logger("plugins.image_composer")

CanvasFormat = Literal["portrait", "square", "vertical"]
CANVASES: dict[str, tuple[int, int]] = {"portrait": (1080, 1350), "square": (1080, 1080), "vertical": (1080, 1920)}
CONTENT_TYPE_CANVAS: dict[str, CanvasFormat] = {
    "instagram_post": "portrait",
    "instagram_story": "vertical",
    "instagram_reel": "vertical",
    "tiktok_post": "vertical",
}
# Platform UI covers these bands of a 9:16 frame (profile bar / reply box, caption, buttons).
VERTICAL_SAFE_TOP, VERTICAL_SAFE_BOTTOM = 250, 340

# FindHub design tokens (storefront/tailwind.config.ts).
SPRUCE, SPRUCE_DEEP, SPRUCE_TINT = (15, 76, 58), (10, 55, 41), (227, 238, 232)
TAG, TAG_DEEP, TAG_INK = (255, 212, 59), (239, 185, 0), (29, 23, 0)
FLASH, PAPER, WHITE, INK, INK_SOFT = (211, 36, 59), (243, 245, 242), (255, 255, 255), (20, 36, 31), (79, 94, 88)
GOLD = (255, 190, 30)

MAX_DOWNLOAD_BYTES = 15 * 1024 * 1024
_FONT_CANDIDATES = {
    True: (  # bold
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/Library/Fonts/Arial Bold.ttf",
        "DejaVuSans-Bold.ttf",
    ),
    False: (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial.ttf",
        "DejaVuSans.ttf",
    ),
}
_RTL_RE = re.compile(r"[\u0590-\u05FF\u0600-\u06FF\uFB1D-\uFDFF\uFE70-\uFEFF]")
_HAS_RAQM = features.check("raqm")


class ImageCompositionError(TarshishError):
    """The product photo could not be downloaded or read."""


@dataclass(frozen=True)
class ProductVisual:
    """What the composer needs to know about a product (no database or plugin types)."""

    image_url: str
    price: float
    currency: str
    original_price: float | None = None
    discount_percent: int | None = None
    rating: float = 0.0
    orders_count: int = 0
    is_promoted: bool = False      # flash sale or coupon: "HOT DEAL" when there is no percentage

    @classmethod
    def from_row(cls, row: Mapping[str, Any], image_url: str) -> "ProductVisual":
        original = row.get("original_price")
        price = float(row["price"])
        discount = row.get("discount_percent")
        if discount is None and original and float(original) > price:
            discount = round((1 - price / float(original)) * 100)
        return cls(
            image_url=image_url, price=price, currency=str(row.get("currency") or "USD"),
            original_price=float(original) if original else None, discount_percent=int(discount) if discount else None,
            rating=float(row.get("rating") or 0), orders_count=int(row.get("orders_count") or 0),
            is_promoted=bool(row.get("is_flash_sale")) or bool(row.get("coupon_code")),
        )


@dataclass(frozen=True)
class ComposedImage:
    path: Path
    width: int
    height: int
    canvas: str


# --------------------------------------------------------------------------- #
# Fonts and text
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=64)
def font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for candidate in _FONT_CANDIDATES[bold]:
        try:
            layout = ImageFont.Layout.RAQM if _HAS_RAQM else ImageFont.Layout.BASIC
            return ImageFont.truetype(candidate, size, layout_engine=layout)
        except OSError:
            continue
    logger.warning("No TrueType font with Hebrew glyphs was found; using Pillow's built-in font.")
    return ImageFont.load_default(size)


def drawable(text: str) -> str:
    """Drop emoji and pictographs (text fonts have no glyphs for them) and tidy the spaces."""
    kept = []
    for char in unicodedata.normalize("NFC", text):
        if char in "\u200d\ufe0f\ufe0e" or ord(char) > 0xFFFF:
            continue
        if unicodedata.category(char) in ("So", "Cs", "Co", "Cn") and char not in "★₪":
            continue
        kept.append(char)
    return re.sub(r"[ \t]+", " ", "".join(kept)).strip()


def _visual_order(text: str) -> str:
    """Minimal bidi for right-to-left lines without libraqm: reverse the runs, keep LTR runs intact."""
    runs = re.findall(r"[A-Za-z0-9$€£₪%.,:+\-/]+(?:\s+[A-Za-z0-9$€£₪%.,:+\-/]+)*|.", text)
    out: list[str] = []
    for run in reversed(runs):
        out.append(run if re.match(r"[A-Za-z0-9$€£₪]", run) else run[::-1])
    mirrored = {"(": ")", ")": "(", "[": "]", "]": "["}
    return "".join(mirrored.get(char, char) for char in "".join(out))


def _text_args(text: str) -> tuple[str, dict[str, Any]]:
    if _RTL_RE.search(text):
        if _HAS_RAQM:
            return text, {"direction": "rtl"}
        return _visual_order(text), {}
    return text, {}


def text_width(draw: ImageDraw.ImageDraw, text: str, face: Any) -> float:
    shaped, args = _text_args(text)
    left, _, right, _ = draw.textbbox((0, 0), shaped, font=face, **args)
    return right - left


def draw_text(draw: ImageDraw.ImageDraw, xy: tuple[float, float], text: str, face: Any, fill: Any, anchor: str = "la") -> None:
    shaped, args = _text_args(text)
    draw.text(xy, shaped, font=face, fill=fill, anchor=anchor, **args)


def fit_font(draw: ImageDraw.ImageDraw, text: str, max_width: float, size: int, minimum: int, bold: bool = True) -> Any:
    while size > minimum and text_width(draw, text, font(size, bold)) > max_width:
        size -= 2
    return font(size, bold)


def wrap(draw: ImageDraw.ImageDraw, text: str, face: Any, max_width: float, max_lines: int) -> list[str]:
    lines: list[str] = []
    for paragraph in text.splitlines():
        words, line = paragraph.split(), ""
        for word in words:
            candidate = f"{line} {word}".strip()
            if text_width(draw, candidate, face) <= max_width or not line:
                line = candidate
            else:
                lines.append(line)
                line = word
        if line:
            lines.append(line)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(" .,") + "…"
    return lines


def star_points(cx: float, cy: float, outer: float, inner: float | None = None) -> list[tuple[float, float]]:
    import math

    inner = inner if inner is not None else outer * 0.45
    points = []
    for index in range(10):
        radius = outer if index % 2 == 0 else inner
        angle = math.pi / 2 + index * math.pi / 5
        points.append((cx + radius * math.cos(angle), cy - radius * math.sin(angle)))
    return points


def compact_count(count: int) -> str:
    """2400 -> '2,400+', 15300 -> '15K+', 2_100_000 -> '2.1M+'."""
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}".rstrip("0").rstrip(".") + "M+"
    if count >= 10_000:
        return f"{count // 1000}K+"
    if count >= 100:
        return f"{(count // 100) * 100:,}+"
    return f"{count}"


# --------------------------------------------------------------------------- #
# Composer
# --------------------------------------------------------------------------- #
class ImageComposer:
    """Download a real product photo and render the FindHub promotional image around it."""

    def __init__(self, output_dir: Path | str, *, transport: Any = None, brand_domain: str | None = None) -> None:
        self.output_dir = Path(output_dir)
        self._transport = transport
        self.brand_domain = brand_domain

    # -- download ------------------------------------------------------------------- #
    def fetch_photo(self, url: str) -> Image.Image:
        if not url.lower().startswith(("http://", "https://")):
            raise ImageCompositionError(f"not an http(s) image URL: {url[:120]}")
        headers = {"User-Agent": "Mozilla/5.0 (compatible; FindHubMedia/1.0)", "Accept": "image/*"}
        try:
            with httpx.Client(timeout=httpx.Timeout(25.0, connect=10.0), follow_redirects=True,
                              transport=self._transport) as client:
                with client.stream("GET", url, headers=headers) as response:
                    if response.status_code != 200:
                        raise ImageCompositionError(f"product photo answered HTTP {response.status_code}")
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        data.extend(chunk)
                        if len(data) > MAX_DOWNLOAD_BYTES:
                            raise ImageCompositionError("product photo is larger than 15 MB")
        except httpx.HTTPError as exc:
            raise ImageCompositionError(redact(f"product photo download failed: {type(exc).__name__}: {exc}")) from None
        try:
            photo = Image.open(io.BytesIO(bytes(data)))
            photo = ImageOps.exif_transpose(photo)
            photo.load()
        except Exception as exc:
            raise ImageCompositionError(f"product photo is not a readable image ({type(exc).__name__})") from None
        if photo.mode in ("RGBA", "LA", "P"):
            photo = photo.convert("RGBA")
            flat = Image.new("RGB", photo.size, WHITE)
            flat.paste(photo, mask=photo.split()[-1])
            return flat
        return photo.convert("RGB")

    # -- composition ------------------------------------------------------------------ #
    def compose(
        self, product: ProductVisual, canvas: CanvasFormat, *, file_stem: str, hook: str | None = None,
        cta: str = "Available on FindHub", photo: Image.Image | None = None, subfolder: str = "",
    ) -> ComposedImage:
        """Render, save as JPEG and return where it went. Raises :class:`ImageCompositionError`."""
        photo = photo if photo is not None else self.fetch_photo(product.image_url)
        width, height = CANVASES[canvas]
        vertical = canvas == "vertical"
        top = VERTICAL_SAFE_TOP if vertical else 48
        bottom = height - (VERTICAL_SAFE_BOTTOM if vertical else 0)

        image = self._background(width, height)
        draw = ImageDraw.Draw(image)

        # Layout first (top to bottom: header, card, rating/price row, hook, CTA bar), then paint.
        header_bottom = top + 12 + 58 + 24
        bar_height = 118 if vertical else 128
        bar_top = bottom - bar_height - (16 if vertical else 0)
        hook_face = font(54, True)
        hook_lines = wrap(draw, drawable(hook), hook_face, width - 160, 2) if vertical and hook else []
        hook_block = len(hook_lines) * 68 + (24 if hook_lines else 0)
        row_height = 92
        row_top = bar_top - 28 - hook_block - row_height
        card_top, card_bottom = header_bottom + 34, row_top - 30

        # 4. product card (its shadow goes down first, so nothing drawn later is dimmed by it)
        self._product_card(image, photo, 64, card_top, width - 64, card_bottom)
        draw = ImageDraw.Draw(image)
        # 1. header pill + 2. discount tag
        self._brand_pill(image, draw, 56, top + 12)
        self._discount_tag(draw, product, width - 56, top + 8)
        # 3. social proof pill (left) + price shelf label (right)
        self._rating_pill(draw, product, 64, row_top, row_height)
        self._price_label(draw, product, width - 64, row_top, row_height)
        # CTA bar
        self._cta_bar(image, draw, cta, bar_top, bar_height, width, rounded=vertical)

        y = row_top + row_height + 24
        for line in hook_lines:
            draw_text(draw, (width / 2, y), line, hook_face, INK, anchor="ma")
            y += 68

        self.output_dir.mkdir(parents=True, exist_ok=True)
        folder = self.output_dir / subfolder if subfolder else self.output_dir
        folder.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(f"{product.image_url}|{canvas}|{hook}|{cta}|{product.price}".encode()).hexdigest()[:8]
        path = folder / f"{_safe_stem(file_stem)}_{digest}.jpg"
        image.convert("RGB").save(path, "JPEG", quality=90, optimize=True, progressive=True)
        return ComposedImage(path=path, width=width, height=height, canvas=canvas)

    # -- elements ------------------------------------------------------------------------ #
    @staticmethod
    def _background(width: int, height: int) -> Image.Image:
        """Cool paper fading into the spruce tint, top to bottom."""
        gradient = Image.linear_gradient("L").resize((width, height))
        top_layer = Image.new("RGB", (width, height), PAPER)
        bottom_layer = Image.new("RGB", (width, height), SPRUCE_TINT)
        return Image.composite(bottom_layer, top_layer, gradient).convert("RGBA")

    def _brand_pill(self, image: Image.Image, draw: ImageDraw.ImageDraw, x: int, y: int) -> int:
        face = font(46, True)
        label = "FindHub"
        mark = 58
        pad = 16
        w = pad + mark + 16 + int(text_width(draw, label, face)) + pad + 10
        h = mark + 2 * 12
        draw.rounded_rectangle((x, y, x + w, y + h), radius=h // 2, fill=SPRUCE)
        # logo mark: yellow shelf tag with a check
        mx, my = x + pad, y + 12
        draw.rounded_rectangle((mx, my, mx + mark, my + mark), radius=14, fill=SPRUCE_DEEP)
        s = mark / 64
        draw.polygon([(mx + 22 * s, my + 18 * s), (mx + 52 * s, my + 18 * s), (mx + 52 * s, my + 46 * s),
                      (mx + 22 * s, my + 46 * s), (mx + 8 * s, my + 32 * s)], fill=TAG)
        draw.line([(mx + 27 * s, my + 32.5 * s), (mx + 33 * s, my + 38.5 * s), (mx + 45 * s, my + 25.5 * s)],
                  fill=TAG_INK, width=max(3, int(5 * s)), joint="curve")
        draw_text(draw, (mx + mark + 16, y + h / 2), label, face, WHITE, anchor="lm")
        return y + h

    def _discount_tag(self, draw: ImageDraw.ImageDraw, product: ProductVisual, right: int, y: int) -> None:
        if product.discount_percent and product.discount_percent >= 1:
            label, fill, ink = f"-{product.discount_percent}% OFF", FLASH, WHITE
        elif product.is_promoted:
            label, fill, ink = "HOT DEAL", FLASH, WHITE
        else:
            return
        face = font(50, True)
        w = int(text_width(draw, label, face)) + 64
        h = 92
        draw.rounded_rectangle((right - w + 6, y + 8, right + 6, y + h + 8), radius=22, fill=(150, 20, 38))
        draw.rounded_rectangle((right - w, y, right, y + h), radius=22, fill=fill)
        draw_text(draw, (right - w / 2, y + h / 2), label, face, ink, anchor="mm")

    @staticmethod
    def _product_card(image: Image.Image, photo: Image.Image, left: int, top: int, right: int, bottom: int) -> None:
        radius = 44
        shadow = Image.new("RGBA", image.size, (0, 0, 0, 0))
        ImageDraw.Draw(shadow).rounded_rectangle((left + 4, top + 22, right - 4, bottom + 26), radius=radius,
                                                 fill=(10, 55, 41, 70))
        image.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(26)))
        card = Image.new("RGBA", image.size, (0, 0, 0, 0))
        ImageDraw.Draw(card).rounded_rectangle((left, top, right, bottom), radius=radius, fill=WHITE + (255,))
        image.alpha_composite(card)
        pad = 40
        box_w, box_h = right - left - 2 * pad, bottom - top - 2 * pad
        fitted = ImageOps.contain(photo, (box_w, box_h), Image.Resampling.LANCZOS)
        mask = Image.new("L", fitted.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, fitted.width, fitted.height), radius=26, fill=255)
        image.paste(fitted, (left + pad + (box_w - fitted.width) // 2, top + pad + (box_h - fitted.height) // 2), mask)

    def _rating_pill(self, draw: ImageDraw.ImageDraw, product: ProductVisual, x: int, y: int, h: int) -> None:
        if product.rating <= 0 and product.orders_count <= 0:
            return
        face = font(38, True)
        parts = []
        if product.rating > 0:
            parts.append(f"{product.rating:.1f}")
        if product.orders_count > 0:
            parts.append(f"{compact_count(product.orders_count)} Sold")
        label = " | ".join(parts)
        star = 22
        stars = 5 if product.rating > 0 else 0
        w = 28 + stars * (2 * star + 4) + (14 if stars else 0) + int(text_width(draw, label, face)) + 30
        draw.rounded_rectangle((x, y, x + w, y + h), radius=h // 2, fill=INK)
        cx = x + 28 + star
        for index in range(stars):
            fill = GOLD if product.rating >= index + 0.75 else ((255, 225, 140) if product.rating >= index + 0.25 else (90, 104, 98))
            draw.polygon(star_points(cx, y + h / 2, star), fill=fill)
            cx += 2 * star + 4
        draw_text(draw, (cx - star + 14, y + h / 2), label, face, WHITE, anchor="lm")

    def _price_label(self, draw: ImageDraw.ImageDraw, product: ProductVisual, right: int, y: int, h: int) -> None:
        price = format_price(product.price, product.currency)
        face = font(48, True)
        small = font(28, False)
        was = format_price(product.original_price, product.currency) if product.discount_percent and product.original_price else ""
        w = int(max(text_width(draw, price, face), text_width(draw, was, small) if was else 0)) + 56
        left = right - w
        draw.rounded_rectangle((left, y + 4, right, y + h + 4), radius=18, fill=TAG_DEEP)
        draw.rounded_rectangle((left, y, right, y + h), radius=18, fill=TAG)
        if was:
            draw_text(draw, (left + w / 2, y + 26), price, face, TAG_INK, anchor="mm")
            draw_text(draw, (left + w / 2, y + h - 18), was, small, (95, 80, 20), anchor="mm")
            was_w = text_width(draw, was, small)
            draw.line([(left + w / 2 - was_w / 2, y + h - 18), (left + w / 2 + was_w / 2, y + h - 18)],
                      fill=(95, 80, 20), width=2)
        else:
            draw_text(draw, (left + w / 2, y + h / 2), price, face, TAG_INK, anchor="mm")

    def _cta_bar(self, image: Image.Image, draw: ImageDraw.ImageDraw, cta: str, top: int, h: int, width: int,
                 *, rounded: bool) -> None:
        if rounded:
            draw.rounded_rectangle((48, top, width - 48, top + h), radius=h // 2, fill=SPRUCE)
            left, right = 48, width - 48
        else:
            draw.rectangle((0, top, width, top + h), fill=SPRUCE)
            left, right = 0, width
        text = drawable(cta) or "Available on FindHub"
        face = fit_font(draw, text, right - left - 220, 46, 28)
        domain = self.brand_domain
        arrow_x = right - 70
        if domain and not rounded:
            small = font(28, False)
            draw_text(draw, (width / 2, top + h - 26), domain, small, (190, 214, 203), anchor="mm")
            draw_text(draw, ((left + right) / 2 - 20, top + h / 2 - 14), text, face, WHITE, anchor="mm")
        else:
            draw_text(draw, ((left + right) / 2 - 20, top + h / 2), text, face, WHITE, anchor="mm")
        # yellow arrow button
        r = 34
        cy = top + h / 2
        draw.ellipse((arrow_x - r, cy - r, arrow_x + r, cy + r), fill=TAG)
        draw.line([(arrow_x - 14, cy), (arrow_x + 12, cy)], fill=TAG_INK, width=7)
        draw.polygon([(arrow_x + 18, cy), (arrow_x + 2, cy - 14), (arrow_x + 2, cy + 14)], fill=TAG_INK)


def _safe_stem(stem: str) -> str:
    return re.sub(r"[^A-Za-z0-9_\-]+", "-", stem).strip("-")[:80] or "asset"
