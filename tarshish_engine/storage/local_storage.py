"""Local filesystem storage with an age-based auto-cleanup policy.

Media is kept under ``LOCAL_MEDIA_DIR`` (default ``output/``). Files older than
``MEDIA_RETENTION_DAYS`` are removed by :meth:`LocalStorage.cleanup`, which the
orchestrator calls after every live run; ``0`` disables the cleanup.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Collection

from core.config import Settings
from core.interfaces import BaseStorage
from core.resilience import get_logger

logger = get_logger("storage.local")

_KEEP = {".gitkeep", ".gitignore"}


class LocalStorage(BaseStorage):
    plugin_name = "local"

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.root = Path(settings.local_media_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, remote_key: str) -> Path:
        """Map a key to a path inside the media root (rejects ``..`` escapes)."""
        path = (self.root / remote_key).resolve()
        if path != self.root and self.root not in path.parents:
            raise ValueError(f"storage key escapes the media directory: {remote_key!r}")
        return path

    def save(self, local_path: str, remote_key: str) -> str:
        source = Path(local_path).resolve()
        destination = self._resolve(remote_key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source != destination:
            shutil.copy2(source, destination)
        return str(destination)

    def delete(self, remote_key: str) -> bool:
        path = self._resolve(remote_key)
        if path.is_file():
            path.unlink()
            return True
        return False

    def cleanup(self, protected: Collection[str] = ()) -> int:
        retention_days = self.settings.media_retention_days
        if retention_days <= 0:
            return 0
        cutoff = time.time() - retention_days * 86_400
        keep = {str(Path(uri).resolve()) for uri in protected if "://" not in uri}
        removed = 0
        for path in sorted(self.root.rglob("*"), reverse=True):  # children before their folders
            try:
                if path.is_file():
                    if path.name in _KEEP or str(path.resolve()) in keep:
                        continue
                    if path.stat().st_mtime < cutoff:
                        path.unlink()
                        removed += 1
                elif path.is_dir() and not any(path.iterdir()):
                    path.rmdir()
            except OSError as exc:
                logger.warning("Could not clean up %s: %s", path, exc)
        return removed
