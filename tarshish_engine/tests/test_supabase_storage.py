"""Supabase Storage backend (STORAGE_MODE=supabase), against a fake Storage API."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from core.resilience import ConfigError, redact
from storage.supabase_storage import SupabaseStorage

BASE = "https://abcd.supabase.co"
KEY = "sb_secret_0123456789abcdefghijklmnopqrstuv"


def fake_api(files: dict[str, str], seen: list[httpx.Request]):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if request.method == "POST" and path.startswith("/storage/v1/object/list/media"):
            prefix = json.loads(request.content)["prefix"]
            names: dict[str, dict] = {}
            for key, created in files.items():
                if key.startswith(prefix):
                    head, _, rest = key[len(prefix):].partition("/")
                    names[head] = {"name": head, "id": None} if rest else {"name": head, "id": "x", "created_at": created}
            return httpx.Response(200, json=list(names.values()))
        if request.method == "POST" and path.startswith("/storage/v1/object/media/"):
            files[path.removeprefix("/storage/v1/object/media/")] = datetime.now(timezone.utc).isoformat()
            return httpx.Response(200, json={"Key": path})
        if request.method == "DELETE" and path == "/storage/v1/object/media":
            gone = [key for key in json.loads(request.content)["prefixes"] if files.pop(key, None)]
            return httpx.Response(200, json=[{"name": key} for key in gone])
        return httpx.Response(404, json={"error": "not found"})

    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture
def storage_env(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", BASE)
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", KEY)


def test_upload_returns_the_public_url_and_the_key_is_masked(make_settings, storage_env, tmp_path):
    files: dict[str, str] = {}
    seen: list[httpx.Request] = []
    storage = SupabaseStorage(make_settings(), client=fake_api(files, seen))
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"\x00\x01video")

    url = storage.save(str(clip), "gadgets_he/instagram_story/20261117/abc.mp4")
    assert url == f"{BASE}/storage/v1/object/public/media/gadgets_he/instagram_story/20261117/abc.mp4"
    upload = seen[-1]
    assert upload.headers["apikey"] == KEY and "authorization" not in upload.headers  # sb_secret_ keys: apikey only
    assert upload.headers["content-type"] == "video/mp4" and upload.headers["x-upsert"] == "true"
    assert upload.content == b"\x00\x01video"
    assert KEY not in redact(f"key={KEY}")


def test_cleanup_removes_only_expired_unprotected_files(make_settings, storage_env):
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    new = datetime.now(timezone.utc).isoformat()
    files = {"a/x/old.mp4": old, "a/x/kept.mp4": old, "a/y/new.mp4": new}
    storage = SupabaseStorage(make_settings(media_retention_days=7), client=fake_api(files, []))
    removed = storage.cleanup(protected=[storage.public_url("a/x/kept.mp4"), "/local/path.mp4"])
    assert removed == 1 and set(files) == {"a/x/kept.mp4", "a/y/new.mp4"}
    assert storage.delete("a/y/new.mp4") is True and storage.delete("a/y/new.mp4") is False


def test_missing_settings_are_a_config_error(make_settings, monkeypatch):
    for name in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ConfigError, match="SUPABASE_URL"):
        SupabaseStorage(make_settings())


def test_the_engine_selects_it_with_storage_mode(make_settings, storage_env):
    from core import bootstrap

    engine = bootstrap(settings=make_settings(storage_mode="supabase"), console_logging=False)
    assert type(engine.registry.storage()).__name__ == "SupabaseStorage"
    assert os.environ["SUPABASE_URL"] == BASE
