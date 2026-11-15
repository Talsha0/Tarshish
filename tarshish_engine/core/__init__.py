"""Tarshish Engine core: contracts, orchestration, persistence and resilience.

``bootstrap()`` is the composition root used by every entrypoint (CLI,
dashboard, tests). It validates configuration, opens the database, discovers
plugins and returns a ready-to-use :class:`Engine`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

__version__ = "0.1.0"

if TYPE_CHECKING:
    from core.config import Settings
    from core.database import Database
    from core.pipeline import PipelineOrchestrator
    from core.registry import PipelineRegistry, PluginRegistry
    from core.resilience import BudgetGuard
    from core.worker import QueueWorker


@dataclass
class Engine:
    """The wired application."""

    settings: "Settings"
    db: "Database"
    plugins: "PluginRegistry"
    registry: "PipelineRegistry"
    budget: "BudgetGuard"
    orchestrator: "PipelineOrchestrator"

    def create_worker(self) -> "QueueWorker":
        from core.worker import QueueWorker

        return QueueWorker(self.settings, self.db, self.registry, scheduler=self.orchestrator.scheduler)


def bootstrap(
    *,
    settings: "Settings | None" = None,
    env_file: Path | str | None = None,
    console_logging: bool = True,
    log_level: str | None = None,
) -> Engine:
    """Validate configuration and wire the engine.

    Raises :class:`core.resilience.ConfigError` with a descriptive message when
    environment variables or ``pipelines.yaml`` are invalid — callers should
    print it and exit instead of continuing in a half-configured state.
    """
    from core.config import ENV_FILE, load_settings
    from core.database import Database
    from core.pipeline import PipelineOrchestrator
    from core.registry import PipelineRegistry, PluginRegistry
    from core.resilience import BudgetGuard, register_env_secrets, register_secret, setup_logging

    if settings is None:
        settings = load_settings(env_file if env_file is not None else ENV_FILE)

    setup_logging(log_level or settings.log_level, settings.log_file, console=console_logging)
    register_env_secrets(os.environ)
    for secret in (settings.anthropic_api_key, settings.gemini_api_key):
        if secret is not None:
            register_secret(secret.get_secret_value())

    db = Database(settings.database_path)
    db.initialize()

    plugins = PluginRegistry().discover()
    registry = PipelineRegistry(settings, db, plugins)
    registry.bootstrap()

    budget = BudgetGuard(db.get_monthly_ai_spend, settings.max_monthly_ai_budget_usd)
    orchestrator = PipelineOrchestrator(settings, db, registry, budget)
    return Engine(
        settings=settings, db=db, plugins=plugins, registry=registry, budget=budget, orchestrator=orchestrator
    )


__all__ = ["Engine", "bootstrap", "__version__"]
