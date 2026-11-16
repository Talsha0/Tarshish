"""Plugin packages (sources, generators, publishers) plus tiny helpers they share.

Nothing in ``core/`` imports a specific plugin: modules placed in the
sub-packages are discovered at startup by ``core.registry.PluginRegistry``.
"""

from __future__ import annotations

import re
from urllib.parse import quote_plus, urlsplit

_CURRENCY_SYMBOLS = {"ILS": "₪", "USD": "$", "EUR": "€", "GBP": "£"}

# Stand-in pictures for products whose source has no photo yet: (background, text) per category.
PLACEHOLDER_IMAGE_HOST = "placehold.co"
_PLACEHOLDER_TINTS = {
    "sports": ("E3EFEA", "1D4A40"),
    "gadgets": ("E6EAF4", "26345C"),
    "home": ("F3ECE2", "57411F"),
}
_PLACEHOLDER_DEFAULT_TINT = ("ECEEF1", "30363F")


def placeholder_image(category: str) -> str:
    """A labelled, category-tinted stand-in image URL (a real JPEG, so galleries still render)."""
    words = [word for word in re.split(r"[\W_]+", category) if word]
    background, text = _PLACEHOLDER_DEFAULT_TINT
    for word in words:
        if word.casefold() in _PLACEHOLDER_TINTS:
            background, text = _PLACEHOLDER_TINTS[word.casefold()]
            break
    label = " ".join(word.capitalize() for word in words) or "Product"
    return f"https://{PLACEHOLDER_IMAGE_HOST}/1080x1080/{background}/{text}.jpg?text={quote_plus(label)}&font=poppins"


def is_placeholder_image(url: str) -> bool:
    """True for stand-in pictures, which must never be posted to a real social account."""
    return (urlsplit(url).hostname or "").casefold() == PLACEHOLDER_IMAGE_HOST


def format_price(amount: float, currency: str) -> str:
    """Human-friendly price: ``₪189.90`` for known currencies, ``189.90 CHF`` otherwise."""
    symbol = _CURRENCY_SYMBOLS.get(currency.upper())
    return f"{symbol}{amount:,.2f}" if symbol else f"{amount:,.2f} {currency.upper()}"
