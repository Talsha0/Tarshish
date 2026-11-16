"""Shared offline catalogue behind the mock sources and the storefront seeder.

The leading underscore keeps the plugin registry from treating this module as a
source. ``aliexpress_mock`` and ``temu_mock`` each serve their own slice of it,
and ``api.seed`` loads every entry into the database together with the
storefront listing details (category, Hebrew title, coupon, flash-sale flag).

Prices are in ILS. Image URLs are labelled placeholders that resolve to real
JPEGs, so photo publishing and the storefront gallery work end to end;
affiliate links are fake.
"""

from __future__ import annotations

import re
from typing import Any, Iterator
from urllib.parse import quote_plus

from core.models import Product

# Storefront-only details: stored in the ``products`` table, never part of ``Product``.
LISTING_FIELDS = ("category", "title_he", "coupon_code", "is_flash_sale", "stock_urgency_label")

# A pipeline's product type selects a shelf when it names one of these words
# ("running_and_sports" -> sports, "smart_gadgets" -> gadgets).
_CATEGORY_KEYWORDS: dict[str, frozenset[str]] = {
    "sports": frozenset({"sport", "sports", "running", "fitness", "training", "outdoor"}),
    "gadgets": frozenset({"gadget", "gadgets", "tech", "electronics", "mobile"}),
    "home": frozenset({"home", "kitchen", "household", "living"}),
}

# Placeholder tints per category: (background, text) for the main shot and two gallery shots.
_TINTS: dict[str, tuple[tuple[str, str], ...]] = {
    "sports": (("E3EFEA", "1D4A40"), ("D5E6DF", "1D4A40"), ("EEF5F2", "1D4A40")),
    "gadgets": (("E6EAF4", "26345C"), ("D8DEEE", "26345C"), ("F0F2F9", "26345C")),
    "home": (("F3ECE2", "57411F"), ("EADFCF", "57411F"), ("F8F3EB", "57411F")),
}


def _images(category: str, label: str) -> list[str]:
    # placehold.co reads a literal backslash-n in the text as a line break.
    shots = (label, f"{label}\\nDetail", f"{label}\\nIn use")
    return [
        f"https://placehold.co/1080x1080/{background}/{text}.jpg?text={quote_plus(shot)}&font=poppins"
        for (background, text), shot in zip(_TINTS[category], shots)
    ]


def _item(source: str, category: str, label: str, **fields: Any) -> dict[str, Any]:
    return {"source": source, "category": category, "image_urls": _images(category, label), **fields}


def _ali(external_id: str, slug: str) -> dict[str, str]:
    return {
        "external_id": external_id,
        "raw_url": f"https://www.aliexpress.com/item/{external_id}.html",
        "affiliate_url": f"https://s.click.aliexpress.com/e/_mock{slug}",
    }


def _temu(external_id: str, slug: str) -> dict[str, str]:
    return {
        "external_id": external_id,
        "raw_url": f"https://www.temu.com/goods.html?goods_id={external_id}",
        "affiliate_url": f"https://temu.to/k/mock{slug}",
    }


CATALOGUE: tuple[dict[str, Any], ...] = (
    # ── AliExpress · sports ──────────────────────────────────────────────────
    _item(
        "aliexpress_mock", "sports", "Running Shoes", **_ali("1005006482913507", "RunShoes01"),
        title="Men's Lightweight Running Shoes - Breathable Mesh Cushioned Road Sneakers",
        title_he="נעלי ריצה קלות לגברים - רשת נושמת וסוליה מרופדת לכביש",
        price=189.90, original_price=329.00, rating=4.8, orders_count=3412,
        coupon_code="RUN15",
    ),
    _item(
        "aliexpress_mock", "sports", "Hydration Vest", **_ali("1005005937421186", "HydraVest02"),
        title="5L Running Hydration Vest Backpack with 2 x 500ml Soft Flasks",
        title_he="אפוד שתייה לריצה 5 ליטר עם שני בקבוקים רכים של 500 מ״ל",
        price=129.00, original_price=199.00, rating=4.7, orders_count=1876,
    ),
    _item(
        "aliexpress_mock", "sports", "GPS Sports Watch", **_ali("1005007120854332", "GpsWatch03"),
        title="GPS Running Smartwatch - AMOLED Display, Heart Rate and SpO2, 5ATM Waterproof",
        title_he="שעון ריצה חכם עם GPS - מסך AMOLED, דופק וחמצן בדם, עמיד במים 5ATM",
        price=249.00, original_price=449.00, rating=4.6, orders_count=5230,
        is_flash_sale=True,
    ),
    # ── AliExpress · gadgets ─────────────────────────────────────────────────
    _item(
        "aliexpress_mock", "gadgets", "ANC Earbuds", **_ali("1005006771230458", "AncBuds04"),
        title="Wireless Earbuds with Active Noise Cancelling - Bluetooth 5.4, 40h Charging Case",
        title_he="אוזניות אלחוטיות עם ביטול רעשים אקטיבי - בלוטות׳ 5.4, קייס טעינה ל-40 שעות",
        price=119.90, original_price=239.00, rating=4.8, orders_count=4120,
        coupon_code="SOUND10", is_flash_sale=True,
    ),
    _item(
        "aliexpress_mock", "gadgets", "65W GaN Charger", **_ali("1005005540917723", "GanCharge05"),
        title="65W GaN Fast Charger - 3 Ports USB-C and USB-A, Foldable Travel Plug",
        title_he="מטען מהיר GaN 65W - שלוש יציאות USB-C ו-USB-A, תקע מתקפל לנסיעות",
        price=79.90, original_price=139.00, rating=4.9, orders_count=2870,
        stock_urgency_label="Top 1% Deal",
    ),
    # ── AliExpress · home ────────────────────────────────────────────────────
    _item(
        "aliexpress_mock", "home", "Robot Vacuum", **_ali("1005007350268841", "RoboVac06"),
        title="Robot Vacuum and Mop - 4000Pa Suction, Laser Mapping, App and Voice Control",
        title_he="שואב אבק רובוטי שוטף - שאיבה 4000Pa, מיפוי לייזר, שליטה באפליקציה ובקול",
        price=549.00, original_price=999.00, rating=4.7, orders_count=1340,
        coupon_code="CLEAN40",
    ),
    _item(
        "aliexpress_mock", "home", "LED Desk Lamp", **_ali("1005006018834190", "DeskLamp07"),
        title="LED Desk Lamp with Wireless Charging Pad - 5 Colour Modes, Touch Dimmer",
        title_he="מנורת שולחן LED עם משטח טעינה אלחוטי - חמישה גווני אור ועמעם מגע",
        price=99.90, original_price=169.00, rating=4.6, orders_count=780,
    ),
    # ── Temu · sports ────────────────────────────────────────────────────────
    _item(
        "temu_mock", "sports", "Resistance Bands", **_temu("601099527318844", "Bands08"),
        title="Resistance Bands Set - 5 Levels with Handles, Door Anchor and Carry Bag",
        title_he="סט גומיות התנגדות - חמש רמות עם ידיות, עוגן לדלת ותיק נשיאה",
        price=59.90, original_price=119.00, rating=4.8, orders_count=2460,
        is_flash_sale=True, stock_urgency_label="Limited Stock",
    ),
    _item(
        "temu_mock", "sports", "Yoga Mat", **_temu("601099612045371", "YogaMat09"),
        title="Non-Slip Yoga Mat 6mm - Dual-Layer TPE with Alignment Lines and Strap",
        title_he="מזרן יוגה מונע החלקה 6 מ״מ - TPE דו-שכבתי עם קווי יישור ורצועת נשיאה",
        price=74.90, original_price=129.00, rating=4.7, orders_count=960,
    ),
    # ── Temu · gadgets ───────────────────────────────────────────────────────
    _item(
        "temu_mock", "gadgets", "Magnetic Power Bank", **_temu("601099480276615", "PowerBank10"),
        title="Magnetic Wireless Power Bank 10000mAh - 20W Fast Charge, Slim Aluminium Body",
        title_he="סוללה ניידת מגנטית אלחוטית 10000mAh - טעינה מהירה 20W בגוף אלומיניום דק",
        price=89.90, original_price=179.00, rating=4.7, orders_count=3680,
        coupon_code="POWER12",
    ),
    _item(
        "temu_mock", "gadgets", "Bluetooth Speaker", **_temu("601099233908127", "Speaker11"),
        title="Portable Bluetooth Speaker - IPX7 Waterproof, 16h Playtime, Stereo Pairing",
        title_he="רמקול בלוטות׳ נייד - עמיד במים IPX7, 16 שעות ניגון, חיבור סטריאו",
        price=69.90, original_price=119.90, rating=4.6, orders_count=1520,
    ),
    _item(
        "temu_mock", "gadgets", "Mechanical Keyboard", **_temu("601099705511382", "Keyboard12"),
        title="Compact 75% Mechanical Keyboard - Hot-Swappable Switches, Wireless, RGB",
        title_he="מקלדת מכנית קומפקטית 75% - מתגים נשלפים, חיבור אלחוטי ותאורת RGB",
        price=169.00, original_price=299.00, rating=4.9, orders_count=1180,
        coupon_code="KEYS20",
    ),
    # ── Temu · home ──────────────────────────────────────────────────────────
    _item(
        "temu_mock", "home", "Digital Air Fryer", **_temu("601099388720459", "AirFryer13"),
        title="Digital Air Fryer 5.5L - 8 Presets, Viewing Window, Non-Stick Basket",
        title_he="סיר טיגון באוויר דיגיטלי 5.5 ליטר - שמונה תוכניות, חלון הצצה וסל נון-סטיק",
        price=219.00, original_price=399.00, rating=4.8, orders_count=5120,
        coupon_code="FRY25", is_flash_sale=True,
    ),
    _item(
        "temu_mock", "home", "Insulated Bottle", **_temu("601099164437902", "Bottle14"),
        title="Vacuum Insulated Stainless Steel Bottle 1L - 24h Cold, 12h Hot, Leakproof",
        title_he="בקבוק נירוסטה מבודד 1 ליטר - 24 שעות קר, 12 שעות חם, אטום לנזילות",
        price=49.90, original_price=89.90, rating=4.9, orders_count=350,
    ),
)

_PRODUCT_FIELDS = tuple(name for name in Product.model_fields if name not in ("source", "niche"))


def category_for(product_type: str) -> str | None:
    """The shelf a pipeline's product type points at, or ``None`` when it names none of them."""
    words = set(re.split(r"[^a-z0-9]+", product_type.casefold()))
    for category, keywords in _CATEGORY_KEYWORDS.items():
        if words & keywords:
            return category
    return None


def product_fields(source_name: str, product_type: str) -> list[dict[str, Any]]:
    """``Product`` keyword arguments for one source's slice of the catalogue.

    A product type that names a shelf gets that shelf; any other type gets the
    source's whole range, so a mock pipeline always has something to work with.
    """
    shelf = [item for item in CATALOGUE if item["source"] == source_name]
    category = category_for(product_type)
    if category is not None:
        shelf = [item for item in shelf if item["category"] == category]
    return [{name: item[name] for name in _PRODUCT_FIELDS if name in item} for item in shelf]


def seed_entries() -> Iterator[tuple[Product, dict[str, Any]]]:
    """Every catalogue entry as ``(product, storefront listing)`` for the seeder."""
    for item in CATALOGUE:
        product = Product(
            source=item["source"], niche=item["category"], currency="ILS",
            **{name: item[name] for name in _PRODUCT_FIELDS if name in item},
        )
        listing = {name: item[name] for name in LISTING_FIELDS if name in item}
        listing.setdefault("is_flash_sale", False)
        yield product, listing
