"""Configuration: environment settings and declarative pipeline definitions.

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

from core.resilience import REDACTED, ConfigError, looks_like_secret, register_secret

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BASE_DIR / ".env"

ENV_VAR_SUFFIX = "_env_var"
_ENV_VAR_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_PIPELINE_ID_RE = re.compile(r"[a-z0-9][a-z0-9_\-]{1,63}")
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

    # Persistence & declarative config
    database_path: Path = BASE_DIR / "tarshish_engine.db"
    pipelines_config_path: Path = BASE_DIR / "config" / "pipelines.yaml"

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

    @field_validator("local_media_dir", "database_path", "pipelines_config_path", "log_file", mode="after")
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
        # that ``*_env_var`` references in pipeline configs can be resolved at runtime.
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
# Pipeline configuration
# --------------------------------------------------------------------------- #
class PipelineFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_rating: float = Field(default=0.0, ge=0, le=5)
    min_orders: int = Field(default=0, ge=0)


class SocialPage(BaseModel):
    """One publishing target. Holds env-var *names* only — never secret values.

    ``platform`` is the publisher plugin. Any ``<credential>_env_var`` key names
    the environment variable that holds that credential (``token_env_var`` and
    ``chat_id_env_var`` are the common ones; other platforms may add their own,
    e.g. ``account_id_env_var``). Remaining extra keys are passed to the
    publisher as non-secret options.
    """

    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)

    platform: str = Field(min_length=1)
    page_name: str = Field(default="default", min_length=1)
    token_env_var: str | None = None
    chat_id_env_var: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _blank_to_none(cls, data: Any) -> Any:
        if isinstance(data, dict):
            return {k: (None if isinstance(v, str) and not v.strip() else v) for k, v in data.items()}
        return data

    @model_validator(mode="after")
    def _reject_inline_secrets(self) -> "SocialPage":
        for key, value in self.model_dump().items():
            lowered = key.lower()
            if lowered.endswith(ENV_VAR_SUFFIX):
                if value is None:
                    continue
                if not isinstance(value, str) or not _ENV_VAR_NAME_RE.fullmatch(value):
                    raise ValueError(
                        f"'{key}' must be the NAME of an environment variable (e.g. TG_SPORTS_BOT_TOKEN), "
                        "never the secret value itself"
                    )
            elif lowered in _RAW_SECRET_KEYS:
                raise ValueError(
                    f"raw secret field '{key}' is not allowed in pipeline configuration; "
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
        """Non-secret, publisher-specific extras (e.g. ``parse_mode``)."""
        return {k: v for k, v in (self.model_extra or {}).items() if not k.endswith(ENV_VAR_SUFFIX)}


class PipelineConfig(BaseModel):
    """A fully-wired pipeline: source ➔ generator (+ LLM model) ➔ social pages."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, str_strip_whitespace=True)

    id: str
    enabled: bool = True
    niche: str = Field(min_length=1, validation_alias=AliasChoices("category", "niche", "type"))
    language: str = "en"
    source: str = Field(min_length=1, validation_alias=AliasChoices("source", "source_plugin"))
    generator: str = Field(min_length=1, validation_alias=AliasChoices("generator", "generator_plugin"))
    # ``llm_model`` is opaque to core: a model id ("claude-opus-5-5") or a provider
    # alias ("anthropic", "gemini", "mock") that the generator plugin interprets.
    llm_provider: str | None = None
    llm_model: str | None = None
    publishers: list[str] = Field(default_factory=list)
    social_pages: list[SocialPage] = Field(default_factory=list)
    publishing_mode: Literal["immediate", "queued"] = "immediate"
    filters: PipelineFilters = Field(default_factory=PipelineFilters)
    prompt_template: str | None = None
    limit: int = Field(default=5, ge=1, le=100)
    options: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id", mode="after")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        if not _PIPELINE_ID_RE.fullmatch(value):
            raise ValueError("must be 2-64 chars of lowercase letters, digits, '_' or '-' (e.g. 'sports_he')")
        return value

    @field_validator("language", mode="after")
    @classmethod
    def _valid_language(cls, value: str) -> str:
        value = value.lower()
        if not _LANGUAGE_RE.fullmatch(value):
            raise ValueError("must be a language code such as 'he', 'en' or 'pt-br'")
        return value

    @model_validator(mode="after")
    def _normalise(self) -> "PipelineConfig":
        if not self.llm_model:
            self.llm_model = self.llm_provider or "mock"

        # ``publishers: [..]`` is shorthand; every name without an explicit social page
        # gets a default one (credentials then come from the plugin's default env vars).
        pages = list(self.social_pages)
        covered = {page.platform for page in pages}
        for name in self.publishers:
            if name not in covered:
                pages.append(SocialPage(platform=name))
                covered.add(name)
        if not pages:
            raise ValueError("at least one publisher or social page is required")
        keys = [page.key for page in pages]
        duplicates = sorted({f"{platform} → {page}" for platform, page in keys if keys.count((platform, page)) > 1})
        if duplicates:
            raise ValueError(f"duplicate social pages: {', '.join(duplicates)}")
        # Keep the order the author gave in ``publishers``; unlisted platforms follow.
        rank = {name: index for index, name in enumerate(dict.fromkeys(self.publishers))}
        pages.sort(key=lambda page: rank.get(page.platform, len(rank)))
        self.social_pages = pages
        self.publishers = list(dict.fromkeys(page.platform for page in pages))
        return self

    # -- persistence mapping (column names of the ``pipelines`` table) -------- #
    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.niche,
            "language": self.language,
            "source_plugin": self.source,
            "generator_plugin": self.generator,
            "llm_model": self.llm_model,
            "publishing_mode": self.publishing_mode,
            "social_pages_json": json.dumps(
                [page.model_dump(exclude_none=True) for page in self.social_pages], ensure_ascii=False
            ),
            "filters_json": json.dumps(self.filters.model_dump()),
            "options_json": json.dumps(
                {"prompt_template": self.prompt_template, "limit": self.limit, "options": self.options},
                ensure_ascii=False,
            ),
            "enabled": int(self.enabled),
        }

    @classmethod
    def from_record(cls, row: Mapping[str, Any]) -> "PipelineConfig":
        extras = json.loads(row.get("options_json") or "{}")
        return cls.model_validate(
            {
                "id": row["id"],
                "enabled": bool(row["enabled"]),
                "niche": row["type"],
                "language": row["language"],
                "source": row["source_plugin"],
                "generator": row["generator_plugin"],
                "llm_model": row["llm_model"],
                "publishing_mode": row["publishing_mode"],
                "social_pages": json.loads(row["social_pages_json"] or "[]"),
                "filters": json.loads(row["filters_json"] or "{}"),
                "prompt_template": extras.get("prompt_template"),
                "limit": extras.get("limit") or 5,
                "options": extras.get("options") or {},
            }
        )


class PipelinesFile(BaseModel):
    """Schema of ``config/pipelines.yaml``."""

    model_config = ConfigDict(extra="forbid")

    pipelines: list[PipelineConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_ids(self) -> "PipelinesFile":
        ids = [pipeline.id for pipeline in self.pipelines]
        duplicates = sorted({pid for pid in ids if ids.count(pid) > 1})
        if duplicates:
            raise ValueError(f"duplicate pipeline ids: {', '.join(duplicates)}")
        return self


def describe_validation_error(exc: ValidationError, raw: Any = None) -> list[str]:
    """Render a pipeline ``ValidationError`` as short '<where>: <problem>' lines."""
    lines: list[str] = []
    for err in exc.errors():
        loc = list(err["loc"])
        where = ".".join(str(part) for part in loc) or "<root>"
        if len(loc) >= 2 and loc[0] == "pipelines" and isinstance(loc[1], int):
            try:
                pipeline_id = raw["pipelines"][loc[1]].get("id")
            except Exception:
                pipeline_id = None
            if pipeline_id:
                rest = ".".join(str(part) for part in loc[2:])
                where = f"pipeline '{pipeline_id}'" + (f" → {rest}" if rest else "")
        message = err["msg"].removeprefix("Value error, ")
        lines.append(f"{where}: {message}")
    return lines


def load_pipelines_file(path: Path | str) -> list[PipelineConfig]:
    """Parse and validate ``pipelines.yaml``; raise :class:`ConfigError` on any problem."""
    path = Path(path)
    try:
        # Safe loader (plain dicts/lists, no object construction); the pure-Python
        # parser needs no compiled extension, so it installs on any Python version.
        raw = YAML(typ="safe", pure=True).load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"Pipeline configuration file not found: {path}") from exc
    except YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path}:\n  {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"Invalid pipeline configuration in {path}: expected a top-level 'pipelines:' list")
    try:
        return PipelinesFile.model_validate(raw).pipelines
    except ValidationError as exc:
        details = "\n".join(f"  - {line}" for line in describe_validation_error(exc, raw))
        raise ConfigError(f"Invalid pipeline configuration in {path}:\n{details}") from exc
