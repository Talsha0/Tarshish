"""Plugin discovery and pipeline wiring.

``PluginRegistry`` imports every module of the plugin packages and registers
the concrete contract implementations it finds — nothing in ``core/`` names a
specific plugin, so new sources, generators, publishers and storage backends
are picked up just by adding a file.

``ConfigRegistry`` treats SQLite as the dynamic source of truth for the three
normalized configuration entities — categories, content profiles and social
accounts — seeded once from ``config/*.yaml``. It also turns a social account +
content profile into a live publisher, resolving credentials from environment
variables.
"""

from __future__ import annotations

import importlib
import inspect
import os
import pkgutil
from pathlib import Path
from typing import Any

from pydantic import SecretStr, ValidationError

from core.config import (
    CategoryConfig,
    ContentProfile,
    Settings,
    SocialAccount,
    SocialPage,
    describe_validation_error,
    load_seed_file,
)
from core.database import Database
from core.interfaces import BaseGenerator, BasePublisher, BaseSource, BaseStorage, PluginContext
from core.models import PublishTarget
from core.resilience import (
    BudgetGuard,
    ConfigError,
    PipelineError,
    PluginError,
    get_logger,
    is_sensitive_name,
    register_secret,
)

logger = get_logger("registry")

# kind -> (package scanned for plugins, contract every plugin of that kind implements)
PLUGIN_KINDS: dict[str, tuple[str, type]] = {
    "source": ("plugins.sources", BaseSource),
    "generator": ("plugins.generators", BaseGenerator),
    "publisher": ("plugins.publishers", BasePublisher),
    "storage": ("storage", BaseStorage),
}


class PluginRegistry:
    """Finds plugin classes by scanning the plugin packages."""

    def __init__(self) -> None:
        self._plugins: dict[str, dict[str, type]] = {kind: {} for kind in PLUGIN_KINDS}
        self.load_errors: dict[str, str] = {}

    def discover(self) -> "PluginRegistry":
        for kind, (package_name, contract) in PLUGIN_KINDS.items():
            try:
                package = importlib.import_module(package_name)
            except ImportError as exc:
                self.load_errors[package_name] = f"{type(exc).__name__}: {exc}"
                logger.warning("Plugin package '%s' could not be imported: %s", package_name, exc)
                continue
            for module_info in pkgutil.iter_modules(package.__path__):
                if module_info.name.startswith("_"):
                    continue
                module_name = f"{package_name}.{module_info.name}"
                try:
                    module = importlib.import_module(module_name)
                except Exception as exc:  # one broken plugin must not take the others down
                    self.load_errors[module_name] = f"{type(exc).__name__}: {exc}"
                    logger.warning("Plugin module '%s' failed to load and was skipped: %s", module_name, exc)
                    continue
                self._register_module(kind, contract, module, module_info.name)
        return self

    def _register_module(self, kind: str, contract: type, module: Any, default_name: str) -> None:
        classes = [
            obj
            for _, obj in inspect.getmembers(module, inspect.isclass)
            if obj.__module__ == module.__name__
            and issubclass(obj, contract)
            and obj is not contract
            and not inspect.isabstract(obj)
        ]
        for cls in classes:
            name = cls.__dict__.get("plugin_name") or default_name
            existing = self._plugins[kind].get(name)
            if existing is not None and existing is not cls:
                logger.warning(
                    "Duplicate %s plugin name '%s' (%s vs %s) — set a unique `plugin_name` on one of them.",
                    kind, name, existing.__qualname__, cls.__qualname__,
                )
                continue
            self._plugins[kind][name] = cls

    def names(self, kind: str) -> list[str]:
        return sorted(self._plugins[kind])

    def has(self, kind: str, name: str) -> bool:
        return name in self._plugins[kind]

    def get(self, kind: str, name: str) -> type:
        try:
            return self._plugins[kind][name]
        except KeyError:
            package = PLUGIN_KINDS[kind][0]
            hint = self.load_errors.get(f"{package}.{name}")
            detail = f" (it failed to load: {hint})" if hint else ""
            available = ", ".join(self.names(kind)) or "none"
            raise PluginError(f"Unknown {kind} plugin '{name}'{detail}. Available: {available}.") from None


# Seed file -> (table, model). Order matters for nothing but readable logs.
CONFIG_ENTITIES: dict[str, tuple[str, type]] = {
    "categories.yaml": ("categories", CategoryConfig),
    "content_profiles.yaml": ("content_profiles", ContentProfile),
    "social_accounts.yaml": ("social_accounts", SocialAccount),
}


class ConfigRegistry:
    """Reads and writes the normalized configuration in SQLite and wires publishers."""

    def __init__(self, settings: Settings, db: Database, plugins: PluginRegistry) -> None:
        self.settings = settings
        self.db = db
        self.plugins = plugins
        self._storage: BaseStorage | None = None

    # -- startup -------------------------------------------------------------- #
    def bootstrap(self) -> None:
        """Validate every seed file and copy each one into its table while that table is empty.

        After the first start SQLite is the source of truth: edit in the dashboard, or
        push later YAML edits explicitly with :meth:`sync_from_yaml` (``--sync-config``).
        """
        if not self.plugins.has("storage", self.settings.storage_mode):
            raise ConfigError(
                f"STORAGE_MODE={self.settings.storage_mode!r} does not match a storage backend. "
                f"Available: {', '.join(self.plugins.names('storage')) or 'none'}."
            )
        for file_name, (table, _) in CONFIG_ENTITIES.items():
            if self.db.count_rows(table) == 0:
                rows = self._load_yaml(file_name)
                for row in rows:
                    self.db.upsert_config(table, row.to_record())
                if rows:
                    logger.info("Seeded %d %s row(s) from %s.", len(rows), table, file_name)

    def sync_from_yaml(self) -> dict[str, int]:
        """Upsert every row of the three seed files into the database (overwrites dashboard edits)."""
        counts: dict[str, int] = {}
        for file_name, (table, _) in CONFIG_ENTITIES.items():
            rows = self._load_yaml(file_name)
            for row in rows:
                self.db.upsert_config(table, row.to_record())
            counts[table] = len(rows)
        return counts

    def _load_yaml(self, file_name: str) -> list[Any]:
        path = Path(self.settings.config_dir) / file_name
        rows = load_seed_file(path)
        problems = [problem for row in rows for problem in self.plugin_problems(row)]
        if problems:
            raise ConfigError(f"Invalid configuration in {path}:\n" + "\n".join(f"  - {p}" for p in problems))
        return rows

    def plugin_problems(self, row: Any) -> list[str]:
        """Human-readable problems with the plugin names a configuration row refers to."""
        if isinstance(row, ContentProfile) and not self.plugins.has("generator", row.generator):
            available = ", ".join(self.plugins.names("generator")) or "none"
            return [f"content type '{row.content_type}': unknown generator plugin '{row.generator}' "
                    f"(available: {available})"]
        return []

    # -- generic table access ---------------------------------------------------- #
    def _rows(self, table: str, model: type, enabled_only: bool) -> list[Any]:
        rows: list[Any] = []
        for record in self.db.list_config(table, enabled_only=enabled_only):
            try:
                rows.append(model.from_record(record))
            except ValidationError as exc:  # a corrupt row must not hide the healthy ones
                logger.error("Invalid %s row %s skipped: %s", table, record, "; ".join(describe_validation_error(exc)))
        return rows

    def _save(self, table: str, row: Any) -> None:
        problems = self.plugin_problems(row)
        if problems:
            raise PipelineError("; ".join(problems))
        self.db.upsert_config(table, row.to_record())

    # -- categories ----------------------------------------------------------------- #
    def list_categories(self, enabled_only: bool = False) -> list[CategoryConfig]:
        return self._rows("categories", CategoryConfig, enabled_only)

    def get_category(self, category_id: str) -> CategoryConfig:
        record = self.db.get_config("categories", category_id)
        if record is None:
            known = ", ".join(c.category_id for c in self.list_categories()) or "none"
            raise PipelineError(f"Unknown category '{category_id}'. Known categories: {known}.")
        return CategoryConfig.from_record(record)

    def save_category(self, category: CategoryConfig) -> None:
        self._save("categories", category)
        logger.info("Category '%s' saved.", category.category_id)

    # -- content profiles --------------------------------------------------------------- #
    def list_content_profiles(self, enabled_only: bool = False) -> list[ContentProfile]:
        return self._rows("content_profiles", ContentProfile, enabled_only)

    def get_content_profile(self, content_type: str) -> ContentProfile:
        record = self.db.get_config("content_profiles", content_type)
        if record is None:
            known = ", ".join(p.content_type for p in self.list_content_profiles()) or "none"
            raise PipelineError(f"Unknown content type '{content_type}'. Known content types: {known}.")
        return ContentProfile.from_record(record)

    def save_content_profile(self, profile: ContentProfile) -> None:
        self._save("content_profiles", profile)
        logger.info("Content profile '%s' saved (%s, %s).", profile.content_type, profile.generator, profile.llm_model)

    # -- social accounts ------------------------------------------------------------------ #
    def list_social_accounts(
        self, *, category: str | None = None, language: str | None = None, platform: str | None = None,
        enabled_only: bool = False,
    ) -> list[SocialAccount]:
        return [
            account for account in self._rows("social_accounts", SocialAccount, enabled_only)
            if (category is None or account.category_id == category)
            and (language is None or account.language == language)
            and (platform is None or account.platform == platform)
        ]

    def get_social_account(self, category: str, language: str, platform: str) -> SocialAccount | None:
        record = self.db.get_config("social_accounts", category, language, platform)
        return SocialAccount.from_record(record) if record else None

    def save_social_account(self, account: SocialAccount) -> None:
        self._save("social_accounts", account)
        logger.info("Social account %s saved.", account.label)

    def languages(self) -> list[str]:
        """Every language that has at least one social account, Hebrew and English first."""
        found = {account.language for account in self.list_social_accounts()}
        return sorted(found | {"he", "en"}, key=lambda code: ({"he": 0, "en": 1}.get(code, 2), code))

    # -- wiring --------------------------------------------------------------- #
    def storage(self) -> BaseStorage:
        if self._storage is None:
            self._storage = self.plugins.get("storage", self.settings.storage_mode)(self.settings)
        return self._storage

    def context_for(
        self, *, pipeline_id: str | None = None, llm_model: str | None = None, options: dict[str, Any] | None = None,
        dry_run: bool = False, budget_guard: BudgetGuard | None = None,
    ) -> PluginContext:
        return PluginContext(
            settings=self.settings, pipeline_id=pipeline_id, llm_model=llm_model, options=dict(options or {}),
            dry_run=dry_run, budget_guard=budget_guard, storage=self.storage(),
        )

    def social_page(self, account: SocialAccount, profile: ContentProfile | None = None) -> SocialPage:
        """The publisher-facing target for one account, publishing in ``profile``'s format."""
        page: dict[str, Any] = {"platform": account.publisher, "page_name": account.account_handle}
        if account.token_env_var:
            page["token_env_var"] = account.token_env_var
        if account.account_id_env_var:
            credential = "account_id"
            if self.plugins.has("publisher", account.publisher):
                credential = self.plugins.get("publisher", account.publisher).account_credential
            page[f"{credential}_env_var"] = account.account_id_env_var
        for key, value in account.platform_metadata.items():
            page.setdefault(key, value)  # non-secret identifiers, handed to the publisher as options
        if profile is not None and profile.publish_format:
            page["content_type"] = profile.publish_format
        return SocialPage.model_validate(page)

    def resolve_target(self, page: SocialPage) -> tuple[PublishTarget | None, str | None]:
        """Resolve a social page's credentials from the environment.

        Returns ``(target, None)`` when publishable or ``(None, reason)`` when the
        page must be skipped. Only env-var names ever appear in the reason.
        """
        try:
            publisher_cls = self.plugins.get("publisher", page.platform)
        except PluginError as exc:
            return None, str(exc)

        explicit = page.env_refs()
        env_names = {**publisher_cls.default_env_vars, **explicit}
        options = page.options()
        # Identifiers given inline in the account's platform metadata (never secrets).
        inline = {
            credential: str(options[key]).strip()
            for key, credential in publisher_cls.inline_credentials.items()
            if str(options.get(key) or "").strip()
        }
        credentials: dict[str, SecretStr] = {}
        missing: list[str] = []
        for name in dict.fromkeys([*publisher_cls.required_credentials, *sorted(env_names), *sorted(inline)]):
            env_name = env_names.get(name)
            value = os.getenv(env_name, "").strip() if env_name else ""
            # An env var the account names explicitly wins; otherwise the inline identifier beats a default.
            if name in inline and not (name in explicit and value):
                value = inline[name]
            if value:
                credentials[name] = SecretStr(value)
                if is_sensitive_name(name) or is_sensitive_name(env_name or ""):
                    register_secret(value)
            elif name in publisher_cls.required_credentials:
                missing.append(
                    f"environment variable '{env_name}' ({name}) is not set"
                    if env_name
                    else f"no environment variable is configured for '{name}' (add '{name}_env_var')"
                )
        if missing:
            return None, "; ".join(missing)
        target = PublishTarget(
            platform=page.platform, page_name=page.page_name, credentials=credentials, options=page.options()
        )
        return target, None

    def build_publisher(self, page: SocialPage, context: PluginContext) -> tuple[BasePublisher | None, str | None]:
        target, reason = self.resolve_target(page)
        if target is None:
            return None, reason
        try:
            return self.plugins.get("publisher", page.platform)(context, target), None
        except Exception as exc:
            return None, f"publisher could not be initialised: {type(exc).__name__}: {exc}"
