"""Plugin discovery and pipeline wiring.

``PluginRegistry`` imports every module of the plugin packages and registers
the concrete contract implementations it finds — nothing in ``core/`` names a
specific plugin, so new sources, generators, publishers and storage backends
are picked up just by adding a file.

``PipelineRegistry`` treats SQLite as the dynamic source of truth for pipeline
definitions (seeded once from ``config/pipelines.yaml``) and turns a definition
into live plugin instances, resolving credentials from environment variables.
"""

from __future__ import annotations

import importlib
import inspect
import os
import pkgutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import SecretStr, ValidationError

from core.config import (
    PipelineConfig,
    Settings,
    SocialPage,
    describe_validation_error,
    load_pipelines_file,
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


@dataclass
class WiredTarget:
    page: SocialPage
    publisher: BasePublisher


@dataclass
class WiredPipeline:
    """A pipeline definition turned into live plugin instances."""

    config: PipelineConfig
    source: BaseSource
    generator: BaseGenerator
    storage: BaseStorage
    targets: list[WiredTarget] = field(default_factory=list)
    skipped: list[tuple[SocialPage, str]] = field(default_factory=list)  # (page, reason)


class PipelineRegistry:
    """Loads pipeline definitions from SQLite and wires them to plugins."""

    def __init__(self, settings: Settings, db: Database, plugins: PluginRegistry) -> None:
        self.settings = settings
        self.db = db
        self.plugins = plugins
        self._storage: BaseStorage | None = None

    # -- startup -------------------------------------------------------------- #
    def bootstrap(self) -> None:
        """Validate the YAML file and seed the ``pipelines`` table when it is empty."""
        if not self.plugins.has("storage", self.settings.storage_mode):
            raise ConfigError(
                f"STORAGE_MODE={self.settings.storage_mode!r} does not match a storage backend. "
                f"Available: {', '.join(self.plugins.names('storage')) or 'none'}."
            )

        path = self.settings.pipelines_config_path
        seeding = self.db.count_pipelines() == 0
        if not path.is_file():
            if seeding:
                raise ConfigError(
                    f"No pipelines are defined: the database is empty and {path} does not exist. "
                    "Create the file (see README) or add a pipeline in the dashboard."
                )
            return

        pipelines = self._load_yaml(path)
        if seeding:
            for pipeline in pipelines:
                self.db.upsert_pipeline(pipeline.to_record())
            logger.info("Seeded %d pipeline(s) into the database from %s.", len(pipelines), path.name)

    def sync_from_yaml(self) -> int:
        """Upsert every pipeline of the YAML file into the database (explicit re-sync)."""
        pipelines = self._load_yaml(self.settings.pipelines_config_path)
        for pipeline in pipelines:
            self.db.upsert_pipeline(pipeline.to_record())
        return len(pipelines)

    def _load_yaml(self, path: Path) -> list[PipelineConfig]:
        pipelines = load_pipelines_file(path)
        problems = [
            f"  - pipeline '{pipeline.id}': {problem}"
            for pipeline in pipelines
            for problem in self.plugin_problems(pipeline)
        ]
        if problems:
            raise ConfigError(f"Invalid pipeline configuration in {path}:\n" + "\n".join(problems))
        return pipelines

    # -- definitions ---------------------------------------------------------- #
    def list_pipelines(self, enabled_only: bool = False) -> list[PipelineConfig]:
        """Read pipelines straight from SQLite, so dashboard edits apply immediately."""
        pipelines: list[PipelineConfig] = []
        for row in self.db.list_pipelines(enabled_only=enabled_only):
            try:
                pipelines.append(PipelineConfig.from_record(row))
            except ValidationError as exc:  # a corrupt row must not hide the healthy ones
                logger.error(
                    "Pipeline row '%s' is invalid and was skipped: %s",
                    row.get("id"), "; ".join(describe_validation_error(exc)),
                )
        return pipelines

    def get_pipeline(self, pipeline_id: str) -> PipelineConfig:
        row = self.db.get_pipeline(pipeline_id)
        if row is None:
            known = ", ".join(r["id"] for r in self.db.list_pipelines()) or "none"
            raise PipelineError(f"Unknown pipeline '{pipeline_id}'. Known pipelines: {known}.")
        try:
            return PipelineConfig.from_record(row)
        except ValidationError as exc:
            raise PipelineError(
                f"Pipeline '{pipeline_id}' has an invalid stored configuration: "
                + "; ".join(describe_validation_error(exc))
            ) from exc

    def save_pipeline(self, pipeline: PipelineConfig) -> None:
        problems = self.plugin_problems(pipeline)
        if problems:
            raise PipelineError("; ".join(problems))
        self.db.upsert_pipeline(pipeline.to_record())
        logger.info("Pipeline '%s' saved.", pipeline.id)

    def set_enabled(self, pipeline_id: str, enabled: bool) -> None:
        if not self.db.set_pipeline_enabled(pipeline_id, enabled):
            raise PipelineError(f"Unknown pipeline '{pipeline_id}'.")
        logger.info("Pipeline '%s' %s.", pipeline_id, "enabled" if enabled else "disabled")

    def delete_pipeline(self, pipeline_id: str) -> None:
        if not self.db.delete_pipeline(pipeline_id):
            raise PipelineError(f"Unknown pipeline '{pipeline_id}'.")
        logger.info("Pipeline '%s' deleted.", pipeline_id)

    def plugin_problems(self, pipeline: PipelineConfig) -> list[str]:
        """Human-readable problems with the plugin names a pipeline refers to."""
        problems: list[str] = []
        references = [("source", pipeline.source), ("generator", pipeline.generator)]
        references += [("publisher", name) for name in pipeline.publishers]
        for kind, name in references:
            if not self.plugins.has(kind, name):
                available = ", ".join(self.plugins.names(kind)) or "none"
                problems.append(f"unknown {kind} plugin '{name}' (available: {available})")
        return problems

    # -- wiring --------------------------------------------------------------- #
    def storage(self) -> BaseStorage:
        if self._storage is None:
            self._storage = self.plugins.get("storage", self.settings.storage_mode)(self.settings)
        return self._storage

    def context_for(
        self, pipeline: PipelineConfig, *, dry_run: bool = False, budget_guard: BudgetGuard | None = None
    ) -> PluginContext:
        return PluginContext(
            settings=self.settings,
            pipeline_id=pipeline.id,
            llm_model=pipeline.llm_model,
            options=dict(pipeline.options),
            dry_run=dry_run,
            budget_guard=budget_guard,
            storage=self.storage(),
        )

    def resolve_target(self, page: SocialPage) -> tuple[PublishTarget | None, str | None]:
        """Resolve a social page's credentials from the environment.

        Returns ``(target, None)`` when publishable or ``(None, reason)`` when the
        page must be skipped. Only env-var names ever appear in the reason.
        """
        try:
            publisher_cls = self.plugins.get("publisher", page.platform)
        except PluginError as exc:
            return None, str(exc)

        env_names = {**publisher_cls.default_env_vars, **page.env_refs()}
        credentials: dict[str, SecretStr] = {}
        missing: list[str] = []
        for name in dict.fromkeys([*publisher_cls.required_credentials, *sorted(env_names)]):
            env_name = env_names.get(name)
            value = os.getenv(env_name, "").strip() if env_name else ""
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

    def wire(
        self, pipeline: PipelineConfig, *, dry_run: bool = False, budget_guard: BudgetGuard | None = None
    ) -> WiredPipeline:
        """Instantiate the pipeline's plugins. Unusable targets are skipped, not fatal."""
        context = self.context_for(pipeline, dry_run=dry_run, budget_guard=budget_guard)
        wired = WiredPipeline(
            config=pipeline,
            source=self.plugins.get("source", pipeline.source)(context),
            generator=self.plugins.get("generator", pipeline.generator)(context),
            storage=self.storage(),
        )
        for page in pipeline.social_pages:
            publisher, reason = self.build_publisher(page, context)
            if publisher is None:
                wired.skipped.append((page, reason or "unknown reason"))
                logger.warning(
                    "Pipeline '%s': skipping target %s — %s. Other targets are unaffected.",
                    pipeline.id, page.label, reason,
                )
            else:
                wired.targets.append(WiredTarget(page=page, publisher=publisher))
        return wired
