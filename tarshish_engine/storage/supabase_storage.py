"""Supabase Storage backend: generated media gets a public HTTPS URL (``STORAGE_MODE=supabase``).

Instagram (and every other platform that fetches media by URL) needs a public address for
generated videos and story images. Files go to a **public** bucket of the project's Supabase
Storage, through its REST API, so no extra SDK is needed.

Settings (environment variables, read with ``Settings.get_env``):

    SUPABASE_URL                 https://<project-ref>.supabase.co
    SUPABASE_SERVICE_ROLE_KEY    the project's service_role key or new-style secret key (sb_secret_…);
                                 SUPABASE_SECRET_KEY is accepted as well. Never ship it to a browser.
    SUPABASE_STORAGE_BUCKET      bucket name (default: media). Create it once as a PUBLIC bucket:
                                 Supabase dashboard > Storage > New bucket > "Public bucket" on.

``cleanup`` deletes files older than ``MEDIA_RETENTION_DAYS`` (0 = keep everything), except
the ones unpublished content still points at.
"""

from __future__ import annotations

import mimetypes
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Collection
from urllib.parse import quote

import httpx

from core.config import Settings
from core.interfaces import BaseStorage
from core.resilience import ConfigError, get_logger, register_secret

logger = get_logger("storage.supabase")

DEFAULT_BUCKET = "media"
_LIST_PAGE = 1000


class SupabaseStorage(BaseStorage):
    plugin_name = "supabase"

    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        super().__init__(settings)
        base = (settings.get_env("SUPABASE_URL") or "").rstrip("/")
        key = settings.get_env("SUPABASE_SERVICE_ROLE_KEY") or settings.get_env("SUPABASE_SECRET_KEY")
        if not base.startswith("https://") or not key:
            raise ConfigError(
                "STORAGE_MODE=supabase needs SUPABASE_URL (https://<project-ref>.supabase.co) and "
                "SUPABASE_SERVICE_ROLE_KEY (Project Settings > API keys)."
            )
        register_secret(key)
        self.bucket = settings.get_env("SUPABASE_STORAGE_BUCKET", DEFAULT_BUCKET) or DEFAULT_BUCKET
        self.api = f"{base}/storage/v1"
        self.public_prefix = f"{self.api}/object/public/{quote(self.bucket)}/"
        headers = {"apikey": key}
        if key.startswith("eyJ"):  # legacy JWT keys also go in Authorization; sb_secret_ keys only in apikey
            headers["Authorization"] = f"Bearer {key}"
        self._client = client or httpx.Client(timeout=120.0)
        self._headers = headers

    def _object_url(self, key: str) -> str:
        return f"{self.api}/object/{quote(self.bucket)}/{quote(key)}"

    def public_url(self, key: str) -> str:
        return self.public_prefix + quote(key)

    def save(self, local_path: str, remote_key: str) -> str:
        path = Path(local_path)
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        response = self._client.post(
            self._object_url(remote_key),
            content=path.read_bytes(),
            headers={**self._headers, "Content-Type": content_type, "x-upsert": "true", "cache-control": "3600"},
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"Supabase Storage refused the upload of {remote_key} to bucket '{self.bucket}' "
                f"(HTTP {response.status_code}: {response.text[:300]}). Does the bucket exist and is it public?"
            )
        return self.public_url(remote_key)

    def delete(self, remote_key: str) -> bool:
        return bool(self._delete([remote_key]))

    def _delete(self, keys: list[str]) -> list[Any]:
        response = self._client.request(
            "DELETE", f"{self.api}/object/{quote(self.bucket)}", json={"prefixes": keys}, headers=self._headers,
        )
        response.raise_for_status()
        return response.json() or []

    def _list(self, prefix: str) -> list[dict[str, Any]]:
        """Every file under ``prefix``, recursively (``name`` is the full key)."""
        files: list[dict[str, Any]] = []
        offset = 0
        while True:
            response = self._client.post(
                f"{self.api}/object/list/{quote(self.bucket)}",
                json={"prefix": prefix, "limit": _LIST_PAGE, "offset": offset, "sortBy": {"column": "name", "order": "asc"}},
                headers=self._headers,
            )
            response.raise_for_status()
            entries = response.json() or []
            for entry in entries:
                key = f"{prefix}{entry['name']}"
                if entry.get("id") is None:  # a folder
                    files.extend(self._list(f"{key}/"))
                else:
                    files.append({**entry, "name": key})
            if len(entries) < _LIST_PAGE:
                return files
            offset += _LIST_PAGE

    def cleanup(self, protected: Collection[str] = ()) -> int:
        retention_days = self.settings.media_retention_days
        if retention_days <= 0:
            return 0
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        keep = {uri for uri in protected if uri.startswith(self.public_prefix)}
        expired = []
        for entry in self._list(""):
            created = entry.get("created_at") or entry.get("updated_at")
            if not created:
                continue
            moment = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            if moment < cutoff and self.public_url(entry["name"]) not in keep:
                expired.append(entry["name"])
        removed = 0
        for start in range(0, len(expired), 100):
            removed += len(self._delete(expired[start:start + 100]))
        return removed
