"""Configuration: environment settings and the three normalized configuration entities.

* **Categories** (``config/categories.yaml`` ➔ ``categories`` table): language-agnostic search
  rules per category and store — the single source of truth for Stage 1, product harvesting.
* **Content profiles** (``config/content_profiles.yaml`` ➔ ``content_profiles``): which generator
  plugin and LLM model produce each content type (``instagram_post``, ``instagram_story``…).
* **Social accounts** (``config/social_accounts.yaml`` ➔ ``social_accounts``): the
  ``category × language × platform`` matrix of publishing targets, holding env-var *names* only.

Everything here is validated with Pydantic at startup. Any failure is turned
into a :class:`~core.resilience.ConfigError` with a readable, per-field message
so the process can halt immediately instead of misbehaving later.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from string import Formatter
from typing import Any, Callable, Literal, Mapping
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from core.models import category_slug
from core.resilience import REDACTED, ConfigError, looks_like_secret, register_secret

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BASE_DIR / ".env"

ENV_VAR_SUFFIX = "_env_var"
_ENV_VAR_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SLUG_RE = re.compile(r"[a-z0-9][a-z0-9_\-]{1,63}")
_LANGUAGE_RE = re.compile(r"[a-z]{2,3}(-[a-z0-9]{2,8})?")
# RapidAPI keys: one shared key, plus optional per-provider keys that fall back to it.
RAPIDAPI_FALLBACK_KEY_ENV = "RAPIDAPI_KEY"
RAPIDAPI_KEY_PREFIX = "RAPIDAPI_KEY"
ALIEXPRESS_KEY_ENVS = ("RAPIDAPI_KEY_ALI_1", "RAPIDAPI_KEY_ALI_2", "RAPIDAPI_KEY_ALI_3")
DEFAULT_ALIEXPRESS_AFFILIATE_URL_TEMPLATE = "{url}?aff_fcid={tracking_id}&aff_platform=portals-tool"
_RAW_SECRET_KEYS = {
    "token", "bot_token", "access_token", "api_key", "apikey", "secret",
    "client_secret", "password", "passwd", "authorization",
}


# --------------------------------------------------------------------------- #
# Affiliate links & RapidAPI keys
# --------------------------------------------------------------------------- #
def validate_affiliate_template(value: str) -> str:
    """An affiliate URL template must contain ``{url}`` and may also use ``{tracking_id}``."""
    try:
        names = {name for _, name, _, _ in Formatter().parse(value) if name is not None}
    except ValueError as exc:
        raise ValueError(f"is not a valid template ({exc})") from exc
    if "url" not in names or names - {"url", "tracking_id"}:
        raise ValueError("must contain {url} and may only use the placeholders {url} and {tracking_id}")
    return value


def resolve_rapidapi_key(env_name: str, get_env: Callable[[str], str | None] | None = None) -> tuple[str | None, str | None]:
    """``(key, name of the variable it came from)``: ``env_name`` first, then ``RAPIDAPI_KEY``.

    A key that is found is registered with the log redactor, so it prints as ``[REDACTED]``
    anywhere it would otherwise appear. Only the variable NAME should ever be logged.
    """
    get_env = get_env or Settings.get_env
    for name in dict.fromkeys((env_name, RAPIDAPI_FALLBACK_KEY_ENV)):
        value = get_env(name)
        if value:
            register_secret(value)
            return value, name
    return None, None


def register_rapidapi_secrets(environ: Mapping[str, str] | None = None) -> None:
    """Mask every ``RAPIDAPI_KEY*`` value (``RAPIDAPI_KEY_ALI_1`` …) in all log output."""
    for name, value in (os.environ if environ is None else environ).items():
        if name.upper().startswith(RAPIDAPI_KEY_PREFIX) and value and value.strip():
            register_secret(value.strip())


def masked(value: str | None) -> str:
    """How a secret is shown in logs and status output: ``[REDACTED]`` or ``Missing``."""
    return REDACTED if value else "Missing"


# --------------------------------------------------------------------------- #
# Environment settings
# --------------------------------------------------------------------------- #
class Settings(BaseSettings):
    """Typed view of the process environment (``.env`` is loaded into it first)."""

    model_config = SettingsConfigDict(extra="ignore", env_ignore_empty=True, populate_by_name=True)

    # AI providers — optional: without keys the engine uses the offline template writer.
    anthropic_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("GEMINI_API_KEY", "GOOGLE_API_KEY")
    )
    max_monthly_ai_budget_usd: float = Field(default=5.0, ge=0)

    # Storage
    storage_mode: str = "local"
    local_media_dir: Path = BASE_DIR / "output"
    media_retention_days: int = Field(default=7, ge=0)

    # Persistence & declarative config (the YAML files seed SQLite, which is then the source of truth)
    database_path: Path = BASE_DIR / "tarshish_engine.db"
    config_dir: Path = BASE_DIR / "config"

    # Stage 2 — publishing runs
    publishing_mode: Literal["immediate", "queued"] = "queued"   # default for --publish (override: --mode)
    publish_batch_limit: int = Field(default=5, ge=1, le=100)    # products per content type per run
    harvest_limit: int = Field(default=20, ge=1, le=100)         # products kept per (category, source) harvest

    # Logging
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_file: Path = BASE_DIR / "logs" / "tarshish_engine.log"

    # Queue worker: human-like pacing inside an active-hours window
    worker_timezone: str = "Asia/Jerusalem"
    worker_active_hours_start: int = Field(default=9, ge=0, le=23)
    worker_active_hours_end: int = Field(default=22, ge=0, le=24)
    worker_min_delay_seconds: float = Field(default=600.0, ge=0)
    worker_max_delay_seconds: float = Field(default=2700.0, ge=0)
    worker_poll_interval_seconds: float = Field(default=30.0, gt=0)

    # Publisher plugin used to render --dry-run previews
    dry_run_publisher: str = "console_pub"

    # AliExpress affiliate links (plugins/sources/aliexpress_rapidapi.py). Not secrets: the
    # RapidAPI keys themselves (RAPIDAPI_KEY_ALI_1..3, RAPIDAPI_KEY) are read with
    # resolve_rapidapi_key() and are never stored on this object.
    aliexpress_tracking_id: str | None = None
    aliexpress_affiliate_url_template: str = DEFAULT_ALIEXPRESS_AFFILIATE_URL_TEMPLATE

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: Any) -> Any:
        return value.upper() if isinstance(value, str) else value

    @field_validator("storage_mode", mode="after")
    @classmethod
    def _normalise_storage_mode(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("local_media_dir", "database_path", "config_dir", "log_file", mode="after")
    @classmethod
    def _anchor_to_project_root(cls, value: Path) -> Path:
        """Relative paths are relative to the project, not to the caller's cwd."""
        value = value.expanduser()
        return value if value.is_absolute() else (BASE_DIR / value).resolve()

    @field_validator("aliexpress_affiliate_url_template", mode="after")
    @classmethod
    def _affiliate_template(cls, value: str) -> str:
        return validate_affiliate_template(value.strip())

    @field_validator("worker_timezone", mode="after")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except Exception as exc:  # ZoneInfoNotFoundError, ValueError…
            raise ValueError(
                f"unknown timezone {value!r} (use an IANA name such as 'Asia/Jerusalem'; "
                "on Windows the 'tzdata' package must be installed)"
            ) from exc
        return value

    @model_validator(mode="after")
    def _delay_bounds(self) -> "Settings":
        if self.worker_max_delay_seconds < self.worker_min_delay_seconds:
            raise ValueError("WORKER_MAX_DELAY_SECONDS must be >= WORKER_MIN_DELAY_SECONDS")
        return self

    @property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.worker_timezone)

    @staticmethod
    def get_env(name: str, default: str | None = None) -> str | None:
        """Read an arbitrary environment variable (empty counts as unset).

        Plugins use this for their own knobs and credentials, so supporting a
        new provider or platform never requires adding a field to this class.
        """
        value = os.environ.get(name)
        return value.strip() if value and value.strip() else default


def default_env_file() -> Path:
    """The ``.env`` next to the project, unless ``TARSHISH_ENV_FILE`` names another file."""
    override = os.environ.get("TARSHISH_ENV_FILE", "").strip()
    return Path(override).expanduser() if override else ENV_FILE


def load_settings(env_file: Path | str | None = ENV_FILE) -> Settings:
    """Load ``.env`` into the process environment and validate it."""
    if env_file and Path(env_file).is_file():
        # Real environment variables win over the file; values land in os.environ so
        # that ``*_env_var`` references of social accounts can be resolved at runtime.
        load_dotenv(env_file, override=False, encoding="utf-8")
    register_rapidapi_secrets()
    try:
        return Settings()
    except ValidationError as exc:
        lines = ["Invalid environment configuration (.env / environment variables):"]
        for err in exc.errors():
            name = ".".join(str(part) for part in err["loc"]).upper() or "SETTINGS"
            lines.append(f"  - {name}: {err['msg']}")
        raise ConfigError("\n".join(lines)) from exc


# --------------------------------------------------------------------------- #
# Shared validation helpers
# --------------------------------------------------------------------------- #
def _slug(value: str, what: str) -> str:
    value = value.strip().lower()
    if not _SLUG_RE.fullmatch(value):
        raise ValueError(f"{what} must be 2-64 chars of lowercase letters, digits, '_' or '-' (e.g. 'gadgets')")
    return value


def _category_id(value: Any) -> str:
    """Category ids are URL slugs (the storefront serves ``/c/<category_id>``): 'Home Decor' -> 'home-decor'."""
    return _slug(category_slug(str(value)), "category_id")


def _language(value: str) -> str:
    value = value.strip().lower()
    if not _LANGUAGE_RE.fullmatch(value):
        raise ValueError("must be a language code such as 'he', 'en' or 'pt-br'")
    return value


def _env_var_name(key: str, value: Any) -> Any:
    if value is None:
        return None
    if not isinstance(value, str) or not _ENV_VAR_NAME_RE.fullmatch(value):
        raise ValueError(
            f"'{key}' must be the NAME of an environment variable (e.g. INSTAGRAM_ACCESS_TOKEN), "
            "never the secret value itself"
        )
    return value


def _record_fields(model: type[BaseModel], row: Mapping[str, Any]) -> dict[str, Any]:
    """A database row restricted to the model's fields (drops ``created_at``), ``enabled`` as bool."""
    fields = {name: row[name] for name in model.model_fields if name in row}
    if "enabled" in fields:
        fields["enabled"] = bool(fields["enabled"])
    return fields


def _blank_to_none(data: Any) -> Any:
    if isinstance(data, dict):
        return {k: (None if isinstance(v, str) and not v.strip() else v) for k, v in data.items()}
    return data


# --------------------------------------------------------------------------- #
# Publishing targets (runtime shape handed to publisher plugins)
# --------------------------------------------------------------------------- #
class SocialPage(BaseModel):
    """One resolved publishing target. Holds env-var *names* only — never secret values.

    ``platform`` is the publisher plugin. Any ``<credential>_env_var`` key names
    the environment variable that holds that credential (``token_env_var``,
    ``account_id_env_var``, ``chat_id_env_var``…). Remaining extra keys are passed
    to the publisher as non-secret options (e.g. ``content_type: story``).

    Built from a :class:`SocialAccount` + :class:`ContentProfile` for every publishing
    run, and stored (as JSON) on each publication so the queue worker can rebuild it.
    """

    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)

    platform: str = Field(min_length=1)
    page_name: str = Field(default="default", min_length=1)
    token_env_var: str | None = None
    chat_id_env_var: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _blank_to_none(cls, data: Any) -> Any:
        return _blank_to_none(data)

    @model_validator(mode="after")
    def _reject_inline_secrets(self) -> "SocialPage":
        for key, value in self.model_dump().items():
            lowered = key.lower()
            if lowered.endswith(ENV_VAR_SUFFIX):
                _env_var_name(key, value)
            elif lowered in _RAW_SECRET_KEYS:
                raise ValueError(
                    f"raw secret field '{key}' is not allowed in configuration; "
                    f"put the value in .env and reference it with '{key}{ENV_VAR_SUFFIX}'"
                )
            elif isinstance(value, str) and looks_like_secret(value):
                raise ValueError(
                    f"'{key}' looks like a raw credential; store it in .env and reference the variable name instead"
                )
        return self

    @property
    def key(self) -> tuple[str, str]:
        return (self.platform, self.page_name)

    @property
    def label(self) -> str:
        return f"{self.platform} → {self.page_name}"

    def env_refs(self) -> dict[str, str]:
        """Map credential name -> environment variable name (``token`` -> ``TG_BOT_TOKEN``)."""
        return {
            key[: -len(ENV_VAR_SUFFIX)]: value
            for key, value in self.model_dump(exclude_none=True).items()
            if key.endswith(ENV_VAR_SUFFIX) and isinstance(value, str)
        }

    def options(self) -> dict[str, Any]:
        """Non-secret, publisher-specific extras (e.g. ``content_type``)."""
        return {k: v for k, v in (self.model_extra or {}).items() if not k.endswith(ENV_VAR_SUFFIX)}


# --------------------------------------------------------------------------- #
# 1. Categories & source search rules (language-agnostic)
# --------------------------------------------------------------------------- #
# Store family of a source plugin -> the category field holding its search rules.
# ``temu_rapidapi`` and ``temu_mock`` both read ``temu_search``; the same for AliExpress.
SEARCH_FIELDS: dict[str, str] = {"temu": "temu_search", "aliexpress": "aliexpress_search"}
# Live sources harvested by default for a category that has search rules for their store.
DEFAULT_HARVEST_SOURCES: dict[str, str] = {"temu": "temu_rapidapi", "aliexpress": "aliexpress_rapidapi"}


def store_of(source_plugin: str) -> str:
    """``temu_rapidapi`` -> ``temu``; ``aliexpress_mock`` -> ``aliexpress``."""
    return source_plugin.split("_", 1)[0].lower()


class CategoryConfig(BaseModel):
    """One product category: its display names and how each store is searched for it.

    Search rules are English keywords and filters, shared by every language: a category is
    harvested once per source, never once per language.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    category_id: str
    display_name_en: str = Field(min_length=1)
    display_name_he: str = ""
    temu_search: dict[str, Any] | None = None          # e.g. {"keyword": "smart tech gadgets", "priceMax": 100}
    aliexpress_search: dict[str, Any] | None = None
    min_rating: float = Field(default=0.0, ge=0, le=5)
    min_orders: int = Field(default=0, ge=0)
    enabled: bool = True

    @field_validator("category_id", mode="before")
    @classmethod
    def _valid_id(cls, value: Any) -> Any:
        return _category_id(value)

    @field_validator("temu_search", "aliexpress_search", mode="before")
    @classmethod
    def _empty_search_is_none(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = json.loads(value) if value.strip() else None
        return value or None

    def search_for(self, source_plugin: str) -> dict[str, Any] | None:
        """The search rules this category defines for a source plugin's store (``None`` = none).

        Temu and AliExpress have dedicated rule columns. A store added later as a plugin (no
        column of its own) is searched with the category's English display name, so a new
        source works with zero core or schema changes.
        """
        field_name = SEARCH_FIELDS.get(store_of(source_plugin))
        if field_name is None:
            return {"keyword": self.display_name_en}
        rules = getattr(self, field_name)
        return dict(rules) if rules else None

    def default_sources(self) -> list[str]:
        """Live sources to harvest when ``--source`` is not given: every store with search rules."""
        return [plugin for store, plugin in DEFAULT_HARVEST_SOURCES.items() if getattr(self, SEARCH_FIELDS[store])]

    def display_name(self, language: str) -> str:
        if language.split("-")[0] == "he" and self.display_name_he:
            return self.display_name_he
        return self.display_name_en

    def to_record(self) -> dict[str, Any]:
        return {
            "category_id": self.category_id,
            "display_name_en": self.display_name_en,
            "display_name_he": self.display_name_he,
            "temu_search_json": json.dumps(self.temu_search, ensure_ascii=False) if self.temu_search else None,
            "aliexpress_search_json": (
                json.dumps(self.aliexpress_search, ensure_ascii=False) if self.aliexpress_search else None
            ),
            "min_rating": self.min_rating,
            "min_orders": self.min_orders,
            "enabled": int(self.enabled),
        }

    @classmethod
    def from_record(cls, row: Mapping[str, Any]) -> "CategoryConfig":
        return cls.model_validate({
            "category_id": row["category_id"],
            "display_name_en": row["display_name_en"],
            "display_name_he": row.get("display_name_he") or "",
            "temu_search": row.get("temu_search_json"),
            "aliexpress_search": row.get("aliexpress_search_json"),
            "min_rating": row["min_rating"],
            "min_orders": row["min_orders"],
            "enabled": bool(row["enabled"]),
        })


# --------------------------------------------------------------------------- #
# 2. Content types & AI model routing
# --------------------------------------------------------------------------- #
PUBLISH_FORMATS = ("image", "reel", "story", "video", "text")


class ContentProfile(BaseModel):
    """How one content type is produced: generator plugin, LLM model, tone and target platform."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    content_type: str                       # instagram_post | instagram_story | tiktok_video | telegram_post …
    platform: str                           # social platform the content type is published on (instagram…)
    generator: str = Field(min_length=1)    # generator plugin (plugins/generators/<name>.py)
    llm_model: str = Field(default="mock", min_length=1)
    # The format the publisher is asked for (Instagram: image | reel | story). None = the publisher decides.
    publish_format: str | None = None
    tone: str | None = None                 # free-text tone guidance injected into the prompt
    prompt_template: str | None = None      # full custom prompt; None = the generator's default
    enabled: bool = True

    @model_validator(mode="before")
    @classmethod
    def _blank_to_none(cls, data: Any) -> Any:
        return _blank_to_none(data)

    @field_validator("content_type", mode="before")
    @classmethod
    def _valid_type(cls, value: Any) -> Any:
        return _slug(str(value), "content_type")

    @field_validator("platform", mode="before")
    @classmethod
    def _valid_platform(cls, value: Any) -> Any:
        return _slug(str(value), "platform")

    @field_validator("publish_format", mode="after")
    @classmethod
    def _valid_format(cls, value: str | None) -> str | None:
        if value is not None and value.lower() not in PUBLISH_FORMATS:
            raise ValueError(f"must be one of: {', '.join(PUBLISH_FORMATS)}")
        return value.lower() if value else None

    def to_record(self) -> dict[str, Any]:
        return self.model_dump() | {"enabled": int(self.enabled)}

    @classmethod
    def from_record(cls, row: Mapping[str, Any]) -> "ContentProfile":
        return cls.model_validate(_record_fields(cls, row))


# --------------------------------------------------------------------------- #
# 3. Social accounts matrix: (category, language, platform) -> account
# --------------------------------------------------------------------------- #
class SocialAccount(BaseModel):
    """One publishing account of the ``category × language × platform`` matrix.

    ``token_env_var`` / ``account_id_env_var`` hold the NAMES of ``.env`` variables. The
    account id maps onto the publisher's account credential (Instagram ``account_id``,
    Telegram ``chat_id``); see :attr:`core.interfaces.BasePublisher.account_credential`.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    category_id: str
    language: str
    platform: str
    account_handle: str = Field(min_length=1)
    token_env_var: str | None = None
    account_id_env_var: str | None = None
    enabled: bool = True

    @model_validator(mode="before")
    @classmethod
    def _blank_to_none(cls, data: Any) -> Any:
        return _blank_to_none(data)

    @field_validator("category_id", mode="before")
    @classmethod
    def _valid_category(cls, value: Any) -> Any:
        return _category_id(value)

    @field_validator("platform", mode="before")
    @classmethod
    def _valid_platform(cls, value: Any) -> Any:
        return _slug(str(value), "platform")

    @field_validator("language", mode="before")
    @classmethod
    def _valid_language(cls, value: Any) -> Any:
        return _language(str(value))

    @field_validator("token_env_var", "account_id_env_var", mode="after")
    @classmethod
    def _env_names(cls, value: str | None, info: Any) -> str | None:
        return _env_var_name(info.field_name, value)

    @model_validator(mode="after")
    def _no_secret_handle(self) -> "SocialAccount":
        if looks_like_secret(self.account_handle):
            raise ValueError("'account_handle' looks like a raw credential; it must be the page name or @handle")
        return self

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.category_id, self.language, self.platform)

    @property
    def publisher(self) -> str:
        """Publisher plugin by convention: ``instagram`` -> ``instagram_pub``."""
        return f"{self.platform}_pub"

    @property
    def label(self) -> str:
        return f"{self.platform} → {self.account_handle} ({self.category_id}/{self.language})"

    def to_record(self) -> dict[str, Any]:
        return self.model_dump() | {"enabled": int(self.enabled)}

    @classmethod
    def from_record(cls, row: Mapping[str, Any]) -> "SocialAccount":
        return cls.model_validate(_record_fields(cls, row))


# --------------------------------------------------------------------------- #
# YAML seed files
# --------------------------------------------------------------------------- #
class CategoriesFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    categories: list[CategoryConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self) -> "CategoriesFile":
        _reject_duplicates([c.category_id for c in self.categories], "category ids")
        return self


class ContentProfilesFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content_profiles: list[ContentProfile] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self) -> "ContentProfilesFile":
        _reject_duplicates([p.content_type for p in self.content_profiles], "content types")
        return self


class SocialAccountsFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    social_accounts: list[SocialAccount] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self) -> "SocialAccountsFile":
        _reject_duplicates(["/".join(a.key) for a in self.social_accounts], "(category, language, platform) rows")
        return self


def _reject_duplicates(keys: list[str], what: str) -> None:
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    if duplicates:
        raise ValueError(f"duplicate {what}: {', '.join(duplicates)}")


# file name -> (schema, top-level list key)
SEED_FILES: dict[str, tuple[type[BaseModel], str]] = {
    "categories.yaml": (CategoriesFile, "categories"),
    "content_profiles.yaml": (ContentProfilesFile, "content_profiles"),
    "social_accounts.yaml": (SocialAccountsFile, "social_accounts"),
}


def describe_validation_error(exc: ValidationError, raw: Any = None) -> list[str]:
    """Render a ``ValidationError`` as short '<where>: <problem>' lines (naming the row when possible)."""
    lines: list[str] = []
    for err in exc.errors():
        loc = list(err["loc"])
        where = ".".join(str(part) for part in loc) or "<root>"
        if len(loc) >= 2 and isinstance(loc[1], int) and isinstance(raw, dict):
            try:
                row = raw[loc[0]][loc[1]]
                name = row.get("category_id") or row.get("content_type")
                if "platform" in row and "language" in row:
                    name = f"{row.get('category_id')}/{row.get('language')}/{row.get('platform')}"
            except Exception:
                name = None
            if name:
                rest = ".".join(str(part) for part in loc[2:])
                where = f"{loc[0]} '{name}'" + (f" → {rest}" if rest else "")
        message = err["msg"].removeprefix("Value error, ")
        lines.append(f"{where}: {message}")
    return lines


def load_seed_file(path: Path | str) -> list[Any]:
    """Parse and validate one seed file; raise :class:`ConfigError` on any problem.

    A missing file yields an empty list (the database may already hold the configuration).
    """
    path = Path(path)
    schema, key = SEED_FILES[path.name]
    if not path.is_file():
        return []
    try:
        # Safe loader (plain dicts/lists, no object construction); pure Python, no compiler needed.
        raw = YAML(typ="safe", pure=True).load(path.read_text(encoding="utf-8")) or {key: []}
    except YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path}:\n  {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"Invalid configuration in {path}: expected a top-level '{key}:' list")
    try:
        return list(getattr(schema.model_validate(raw), key))
    except ValidationError as exc:
        details = "\n".join(f"  - {line}" for line in describe_validation_error(exc, raw))
        raise ConfigError(f"Invalid configuration in {path}:\n{details}") from exc
