"""What the chain remembers: exhausted quotas (this process) and the last run (a small file).

The last-run file sits next to the SQLite database so the dashboard — a separate
process — can show which provider answered. It holds no credential: provider
names, outcomes and redacted messages only.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from core.resilience import get_logger, redact

logger = get_logger("plugins.aliexpress_rapidapi")

STATUS_FILE_NAME = "aliexpress_provider_status.json"

# (host, key fingerprint) pairs that answered HTTP 429 during this process.
_exhausted: set[tuple[str, str]] = set()


def _fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def mark_exhausted(host: str, key: str) -> None:
    """Remember that ``key`` is out of quota on ``host`` for the rest of this run."""
    _exhausted.add((host, _fingerprint(key)))


def is_exhausted(host: str, key: str) -> bool:
    return (host, _fingerprint(key)) in _exhausted


def reset_run_state() -> None:
    """Forget the exhausted quotas (a new run, or a test)."""
    _exhausted.clear()


def status_path(database_path: Path | str) -> Path:
    return Path(database_path).with_name(STATUS_FILE_NAME)


def record_run(database_path: Path | str, run: dict[str, Any]) -> None:
    """Store the outcome of one chain run for the dashboard; a failure to write is not fatal."""
    path = status_path(database_path)
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp.write_text(redact(json.dumps(run, ensure_ascii=False, indent=2)), encoding="utf-8")
        os.replace(temp, path)
    except OSError as exc:
        logger.warning("Could not store the AliExpress provider status (%s).", exc)
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def read_last_run(database_path: Path | str) -> dict[str, Any] | None:
    """The last recorded chain run, or ``None`` when there is none (or it is unreadable)."""
    try:
        data = json.loads(status_path(database_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None
