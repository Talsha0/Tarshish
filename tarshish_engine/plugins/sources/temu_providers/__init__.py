"""RapidAPI Temu provider adapters and the ordered chain the ``temu_rapidapi`` source walks.

Not a plugin itself (it holds no ``BaseSource``): ``plugins/sources/temu_rapidapi.py``
is the plugin, and it tries these providers one after another until one yields
usable products. Adding an endpoint is an entry in ``config/temu_providers.yaml``
(``adapter: generic`` needs no Python at all).
"""

from __future__ import annotations

from typing import Any

from core.config import Settings
from core.resilience import ConfigError
from plugins.sources.temu_providers.adapters import (
    ADAPTERS,
    GenericConfigurableTemuAdapter,
    TemuApiRealtimeAdapter,
    TemuProductScraperAdapter,
    TemuScraper2Adapter,
)
from plugins.sources.temu_providers.base import (
    JOB_ID_RE,
    NORMALIZED_FIELDS,
    BaseTemuProvider,
    MappedTemuProvider,
    PendingJob,
    TemuProviderError,
)
from plugins.sources.temu_providers.config import (
    CONFIG_PATH_ENV,
    FALLBACK_KEY_ENV,
    GetEnv,
    ProviderConfig,
    config_path,
    load_providers,
    resolve_key,
)
from plugins.sources.temu_providers.state import (
    is_exhausted,
    mark_exhausted,
    read_last_run,
    record_run,
    reset_run_state,
)


def build_chain(get_env: GetEnv) -> list[BaseTemuProvider]:
    """Every configured provider, in order, as an adapter instance (disabled ones included)."""
    chain: list[BaseTemuProvider] = []
    problems: list[str] = []
    for config in load_providers(get_env):
        adapter = ADAPTERS.get(config.adapter)
        if adapter is None:
            problems.append(
                f"provider '{config.name}': unknown adapter '{config.adapter}' "
                f"(available: {', '.join(sorted(ADAPTERS))})"
            )
        elif adapter is GenericConfigurableTemuAdapter and config.response is None:
            problems.append(f"provider '{config.name}': the generic adapter needs a 'response:' block with 'items'")
        elif adapter is not GenericConfigurableTemuAdapter and (config.request or config.response):
            problems.append(
                f"provider '{config.name}': 'request:' / 'response:' are read by the generic adapter only "
                f"({config.adapter} has a fixed schema)"
            )
        else:
            chain.append(adapter(config))
    if problems:
        raise ConfigError(
            f"Invalid Temu provider configuration in {config_path(get_env)}:\n"
            + "\n".join(f"  - {problem}" for problem in problems)
        )
    return chain


def chain_status(settings: Settings) -> dict[str, Any]:
    """The chain as the dashboard shows it: order, key presence and the last run. No secret values."""
    get_env = settings.get_env
    path = config_path(get_env)
    status: dict[str, Any] = {
        "config_path": str(path), "from_file": path.is_file(), "providers": [], "error": None,
        "last_run": read_last_run(settings.database_path),
    }
    try:
        chain = build_chain(get_env)
    except ConfigError as exc:
        status["error"] = str(exc)
        return status
    last_attempts = {
        attempt.get("provider"): attempt
        for attempt in (status["last_run"] or {}).get("attempts", []) if isinstance(attempt, dict)
    }
    for order, provider in enumerate(chain, start=1):
        config = provider.config
        key, found_in = resolve_key(config, get_env)
        if key is None:
            key_state = "Missing"
        elif found_in == config.api_key_env:
            key_state = "Configured"
        else:
            key_state = f"Configured (via {found_in})"
        last = last_attempts.get(config.name) or {}
        status["providers"].append({
            "order": order, "name": config.name, "adapter": type(provider).__name__, "host": config.host,
            "enabled": config.enabled, "api_key_env": config.api_key_env, "key_state": key_state,
            "last_outcome": last.get("outcome"), "last_detail": last.get("detail"),
        })
    return status


__all__ = [
    "ADAPTERS", "BaseTemuProvider", "CONFIG_PATH_ENV", "FALLBACK_KEY_ENV", "GenericConfigurableTemuAdapter",
    "JOB_ID_RE", "MappedTemuProvider", "NORMALIZED_FIELDS", "PendingJob", "ProviderConfig",
    "TemuApiRealtimeAdapter", "TemuProductScraperAdapter", "TemuProviderError", "TemuScraper2Adapter",
    "build_chain", "chain_status", "config_path", "is_exhausted", "load_providers", "mark_exhausted",
    "read_last_run", "record_run", "reset_run_state", "resolve_key",
]
