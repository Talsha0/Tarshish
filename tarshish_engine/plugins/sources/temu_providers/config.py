"""The ordered provider chain: ``config/temu_providers.yaml`` and API-key resolution.

The file lists RapidAPI Temu endpoints in the order they are tried. It holds
environment-variable *names* only (``api_key_env``), never a key. When the file
does not exist the built-in chain (the original single provider) is used.
"""

from __future__ import annotations

import re
from pathlib import Path
from string import Formatter
from typing import Any, Callable
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from core.config import BASE_DIR
from core.resilience import ConfigError, looks_like_secret

GetEnv = Callable[[str], "str | None"]

CONFIG_PATH_ENV = "TEMU_PROVIDERS_CONFIG_PATH"
DEFAULT_CONFIG_PATH = BASE_DIR / "config" / "temu_providers.yaml"
FALLBACK_KEY_ENV = "RAPIDAPI_KEY"       # used when a provider's own variable is empty
RAPIDAPI_DOMAIN = ".rapidapi.com"       # the only place a RapidAPI key is ever sent
PRIMARY_HOST = "temu-product-scraper.p.rapidapi.com"
PRIMARY_ADAPTER = "TemuProductScraperAdapter"

# What a request template may refer to, and what a response mapping may fill.
PLACEHOLDERS = (
    "keyword", "max_items", "page", "price_min", "price_max", "l1_category", "l2_category",
    "min_rating", "min_stars", "min_orders",
)
MAPPABLE_FIELDS = (
    "goods_id", "title", "price", "original_price", "currency", "rating", "sales_volume",
    "raw_url", "image_urls", "category_hint",
)

_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_\-]{1,63}")
_ENV_VAR_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_HOST_RE = re.compile(r"(?=.{1,253}$)([a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", re.I)


def config_error(title: str, exc: ValidationError, rename: Callable[[str], str] = str) -> ConfigError:
    """A ``ValidationError`` as a readable ``ConfigError`` (one line per field)."""
    lines = [
        f"  - {rename('.'.join(str(part) for part in err['loc']))}: {err['msg'].removeprefix('Value error, ')}"
        for err in exc.errors()
    ]
    return ConfigError(f"{title}:\n" + "\n".join(lines))


def _as_list(value: Any) -> Any:
    return [value] if isinstance(value, str) else value


class RequestMapping(BaseModel):
    """``request:`` of a generic provider — query parameters, as literals or templates."""

    model_config = ConfigDict(extra="forbid")

    params: dict[str, str | int | float | bool] = Field(default_factory=dict)

    @field_validator("params", mode="after")
    @classmethod
    def _known_placeholders(cls, value: dict[str, Any]) -> dict[str, Any]:
        for name, template in value.items():
            if not isinstance(template, str):
                continue
            try:
                used = {field for _, field, _, _ in Formatter().parse(template) if field is not None}
            except ValueError as exc:
                raise ValueError(f"parameter '{name}' is not a valid template ({exc})") from exc
            unknown = sorted(used - set(PLACEHOLDERS))
            if unknown:
                raise ValueError(
                    f"parameter '{name}' uses unknown placeholder(s) {', '.join(unknown)}; "
                    f"available: {', '.join(PLACEHOLDERS)}"
                )
        return value


class ResponseMapping(BaseModel):
    """``response:`` of a generic provider — where the items and their fields are (dotted paths)."""

    model_config = ConfigDict(extra="forbid")

    items: list[str] = Field(min_length=1)
    fields: dict[str, list[str]] = Field(default_factory=dict)
    notice: list[str] = Field(default_factory=list)
    price_divisor: float = Field(default=1.0, gt=0)   # 100 when the API reports prices in cents

    @field_validator("items", "notice", mode="before")
    @classmethod
    def _one_or_many(cls, value: Any) -> Any:
        return _as_list(value)

    @field_validator("fields", mode="before")
    @classmethod
    def _field_paths(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        unknown = sorted(set(map(str, value)) - set(MAPPABLE_FIELDS))
        if unknown:
            raise ValueError(f"unknown field(s) {', '.join(unknown)}; available: {', '.join(MAPPABLE_FIELDS)}")
        return {key: _as_list(paths) for key, paths in value.items()}


class ProviderConfig(BaseModel):
    """One entry of the chain. ``api_key_env`` is the NAME of a variable, never a key."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str
    adapter: str = Field(min_length=1)
    host: str
    url: str
    api_key_env: str = FALLBACK_KEY_ENV
    enabled: bool = True
    request: RequestMapping | None = None
    response: ResponseMapping | None = None

    @field_validator("name", mode="after")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        if not _NAME_RE.fullmatch(value):
            raise ValueError("must be 2-64 chars of lowercase letters, digits, '_' or '-'")
        return value

    @field_validator("host", mode="after")
    @classmethod
    def _rapidapi_host(cls, value: str) -> str:
        # The API key is sent to this host, so it must be a RapidAPI host name and nothing else.
        value = value.lower()
        if not _HOST_RE.fullmatch(value) or not value.endswith(RAPIDAPI_DOMAIN):
            raise ValueError(
                f"must be a bare RapidAPI host name such as '{PRIMARY_HOST}' (no https://, no path, "
                f"ending in {RAPIDAPI_DOMAIN})"
            )
        return value

    @field_validator("api_key_env", mode="after")
    @classmethod
    def _variable_name(cls, value: str) -> str:
        if not _ENV_VAR_NAME_RE.fullmatch(value) or looks_like_secret(value) or len(value) > 64:
            raise ValueError(
                "must be the NAME of an environment variable (e.g. RAPIDAPI_KEY_TEMU_1), never the key itself"
            )
        return value

    @model_validator(mode="after")
    def _url_on_host(self) -> "ProviderConfig":
        parts = urlsplit(self.url)
        if parts.scheme != "https" or (parts.hostname or "") != self.host or parts.query or parts.fragment \
                or "@" in parts.netloc or parts.port not in (None, 443):
            raise ValueError(f"url must be https://{self.host}/<path> with no query string (the key is sent there)")
        return self

    @property
    def origin(self) -> str:
        return f"https://{self.host}"


class ProvidersFile(BaseModel):
    """Schema of ``config/temu_providers.yaml``."""

    model_config = ConfigDict(extra="forbid")

    providers: list[ProviderConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_names(self) -> "ProvidersFile":
        names = [provider.name for provider in self.providers]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"duplicate provider names: {', '.join(duplicates)}")
        return self


def builtin_providers() -> list[ProviderConfig]:
    """The chain used when no YAML file exists: the original single provider."""
    return [ProviderConfig(
        name="temu_primary_scraper", adapter=PRIMARY_ADAPTER, host=PRIMARY_HOST,
        url=f"https://{PRIMARY_HOST}/temu", api_key_env="RAPIDAPI_KEY_TEMU_1",
    )]


def config_path(get_env: GetEnv) -> Path:
    """Where the chain is read from (``TEMU_PROVIDERS_CONFIG_PATH``, relative to the project)."""
    raw = get_env(CONFIG_PATH_ENV)
    if not raw:
        return DEFAULT_CONFIG_PATH
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (BASE_DIR / path).resolve()


def load_providers(get_env: GetEnv) -> list[ProviderConfig]:
    """The configured chain in order; raise :class:`ConfigError` when the file is invalid.

    ``RAPIDAPI_TEMU_HOST`` (the original single-provider setting) still moves the first
    ``TemuProductScraperAdapter`` entry to another RapidAPI host.
    """
    path = config_path(get_env)
    if not path.is_file():
        providers = builtin_providers()
    else:
        try:
            raw = YAML(typ="safe", pure=True).load(path.read_text(encoding="utf-8"))
        except YAMLError as exc:
            raise ConfigError(f"Invalid YAML in {path}:\n  {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"Invalid Temu provider configuration in {path}: expected a top-level 'providers:' list")

        def where(loc: str) -> str:
            parts = loc.split(".")
            if len(parts) >= 2 and parts[0] == "providers" and parts[1].isdigit():
                try:
                    name = raw["providers"][int(parts[1])].get("name")
                except Exception:
                    name = None
                if name:
                    return f"provider '{name}'" + (f" → {'.'.join(parts[2:])}" if parts[2:] else "")
            return loc or "<root>"

        try:
            providers = ProvidersFile.model_validate(raw).providers
        except ValidationError as exc:
            raise config_error(f"Invalid Temu provider configuration in {path}", exc, where) from None

    host = get_env("RAPIDAPI_TEMU_HOST")
    if host:
        for index, provider in enumerate(providers):
            if provider.adapter in (PRIMARY_ADAPTER, "temu_product_scraper"):
                moved = provider.model_dump()
                moved.update(host=host, url=provider.url.replace(provider.origin, f"https://{host.lower()}", 1))
                try:
                    providers[index] = ProviderConfig.model_validate(moved)
                except ValidationError as exc:
                    raise config_error(
                        "Invalid Temu source configuration (.env / environment variables)", exc,
                        lambda _loc: "RAPIDAPI_TEMU_HOST",
                    ) from None
                break
    return providers


def resolve_key(provider: ProviderConfig, get_env: GetEnv) -> tuple[str | None, str | None]:
    """``(key, name of the variable it came from)`` — the provider's own variable, else ``RAPIDAPI_KEY``."""
    for name in dict.fromkeys((provider.api_key_env, FALLBACK_KEY_ENV)):
        value = get_env(name)
        if value:
            return value, name
    return None, None
