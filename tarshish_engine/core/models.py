"""Strict Pydantic v2 domain models shared by core and every plugin."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Mapping, Optional

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

ContentType = Literal["text", "video"]


class PublicationStatus(str, Enum):
    PENDING = "PENDING"      # row created, delivery in flight
    QUEUED = "QUEUED"        # waiting for the worker
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Marketplace filler that differs between listings of the same item.
_NOISE_WORDS = frozenset(
    "a an and the for with of in on to by new hot sale free shipping original official "
    "2023 2024 2025 2026 2027 pcs pc set".split()
)


def compute_canonical_hash(title: str, specs: Mapping[str, Any] | None = None) -> str:
    """Stable fingerprint of a product's normalised title (+ optional specs).

    Case, punctuation, word order, duplicate words and marketplace filler are
    ignored, so the same item listed by different sources hashes identically
    and can be de-duplicated across sources.
    """
    text = unicodedata.normalize("NFKC", title).casefold()
    tokens = re.sub(r"[^\w\s]|_", " ", text).split()
    meaningful = sorted({token for token in tokens if token not in _NOISE_WORDS}) or sorted(set(tokens))
    parts = [" ".join(meaningful)]
    for key in sorted(specs or {}):
        value = unicodedata.normalize("NFKC", str(specs[key])).casefold().strip()  # type: ignore[index]
        parts.append(f"{str(key).casefold().strip()}={value}")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]


def _require_http_url(value: str, field_name: str) -> str:
    if not re.match(r"https?://\S+$", value):
        raise ValueError(f"{field_name} must be an http(s) URL")
    return value


class Product(BaseModel):
    """A product offer returned by a source plugin (immutable)."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    external_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    canonical_hash: str = Field(default="", description="Derived from the title when omitted.")
    niche: str = Field(min_length=1, validation_alias=AliasChoices("niche", "category"))
    title: str = Field(min_length=3)
    price: float = Field(gt=0)
    original_price: Optional[float] = Field(default=None, gt=0)
    currency: str = "ILS"
    rating: float = Field(ge=0, le=5)
    orders_count: int = Field(ge=0)
    raw_url: str
    affiliate_url: str
    image_urls: list[str] = Field(default_factory=list)
    video_url: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _derive_canonical_hash(cls, data: Any) -> Any:
        if isinstance(data, dict) and not data.get("canonical_hash") and data.get("title"):
            data = {**data, "canonical_hash": compute_canonical_hash(str(data["title"]))}
        return data

    @field_validator("currency", mode="after")
    @classmethod
    def _currency_code(cls, value: str) -> str:
        value = value.upper()
        if not re.fullmatch(r"[A-Z]{3}", value):
            raise ValueError("currency must be a 3-letter ISO 4217 code such as 'ILS' or 'USD'")
        return value

    @field_validator("raw_url", "affiliate_url", mode="after")
    @classmethod
    def _http_url(cls, value: str, info: Any) -> str:
        return _require_http_url(value, info.field_name)

    @field_validator("image_urls", mode="after")
    @classmethod
    def _http_image_urls(cls, value: list[str]) -> list[str]:
        return [_require_http_url(url, "image_urls") for url in value]

    @field_validator("video_url", mode="after")
    @classmethod
    def _http_video_url(cls, value: Optional[str]) -> Optional[str]:
        return _require_http_url(value, "video_url") if value else None

    @property
    def discount_percent(self) -> Optional[int]:
        """Whole-percent discount versus ``original_price`` (``None`` when not discounted)."""
        if self.original_price and self.original_price > self.price:
            return round((1 - self.price / self.original_price) * 100)
        return None

    @property
    def ref(self) -> str:
        return f"{self.source}:{self.external_id}"

    @property
    def category(self) -> str:
        """The product's category as its pipeline names it (``Product(category=...)`` sets it)."""
        return self.niche


class GeneratedContent(BaseModel):
    """What a generator plugin produced for one product."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    product: Product
    content_type: ContentType
    language: str = Field(min_length=2)
    caption: str = Field(min_length=1)
    hashtags: list[str] = Field(default_factory=list)
    media_local_path: Optional[str] = None
    media_public_url: Optional[str] = None
    model_used: str = Field(min_length=1)
    tokens_input: int = Field(default=0, ge=0)
    tokens_output: int = Field(default=0, ge=0)
    generation_cost_usd: float = Field(default=0.0, ge=0)

    @field_validator("caption", mode="after")
    @classmethod
    def _non_blank_caption(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("caption must not be blank")
        return value

    @field_validator("hashtags", mode="after")
    @classmethod
    def _clean_hashtags(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for tag in value:
            tag = re.sub(r"\s+", "_", str(tag).strip().lstrip("#").strip())
            if tag and tag not in cleaned:
                cleaned.append(tag)
        return cleaned


class PublishReceipt(BaseModel):
    """Outcome of a single publish attempt on one platform."""

    model_config = ConfigDict(extra="forbid")

    platform: str
    platform_post_id: str = ""
    published_at: datetime = Field(default_factory=utcnow)
    success: bool
    error_message: Optional[str] = None


class PublishTarget(BaseModel):
    """A social page resolved at runtime, with its credentials read from the environment.

    Never persisted: the database and YAML only ever hold env-var *names*.
    """

    model_config = ConfigDict(frozen=True)

    platform: str
    page_name: str
    credentials: dict[str, SecretStr] = Field(default_factory=dict)
    options: dict[str, Any] = Field(default_factory=dict)

    def credential(self, name: str) -> Optional[str]:
        secret = self.credentials.get(name)
        return secret.get_secret_value() if secret is not None else None
