"""Supabase Storage backend (STORAGE_MODE=supabase), against a fake Storage API."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from pydantic import SecretStr

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
    with pytest.raises(ConfigError, match="SUPABASE_SERVICE_ROLE_KEY"):
        SupabaseStorage(make_settings())
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", KEY)
    with pytest.raises(ConfigError, match="SUPABASE_URL"):  # and no Supabase DATABASE_URL to read the project from
        SupabaseStorage(make_settings(database_url=None))


POOLER = "postgresql://postgres.abcdefghijklmnopqrst:p%40ss@aws-0-eu-central-1.pooler.supabase.com:6543/postgres"


@pytest.mark.parametrize(("database_url", "expected"), [
    (POOLER + "?sslmode=require", "https://abcdefghijklmnopqrst.supabase.co"),
    ("postgresql://postgres:pw@db.abcdefghijklmnopqrst.supabase.co:5432/postgres", "https://abcdefghijklmnopqrst.supabase.co"),
    ("postgresql://user:pw@localhost:5432/tarshish", None),       # not Supabase
    ("postgresql://postgres:pw@aws-0-eu.pooler.supabase.com:6543/postgres", None),  # pooler user without a ref
    (None, None),
])
def test_the_project_url_is_read_from_a_supabase_database_url(database_url, expected):
    from storage.supabase_storage import supabase_url_from_database_url

    assert supabase_url_from_database_url(database_url) == expected


def test_without_supabase_url_the_bucket_is_on_the_database_s_project(make_settings, monkeypatch, tmp_path):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", KEY)
    seen: list[httpx.Request] = []
    settings = make_settings().model_copy(update={"database_url": SecretStr(POOLER)})
    storage = SupabaseStorage(settings, client=fake_api({}, seen))
    image = tmp_path / "post.png"
    image.write_bytes(b"png")

    url = storage.save(str(image), "gadgets_he/instagram_post/post.png")
    assert url == "https://abcdefghijklmnopqrst.supabase.co/storage/v1/object/public/media/gadgets_he/instagram_post/post.png"
    assert seen[-1].url.host == "abcdefghijklmnopqrst.supabase.co"


def test_an_explicit_supabase_url_wins_over_the_database_url(make_settings, storage_env):
    settings = make_settings().model_copy(update={"database_url": SecretStr(POOLER)})
    assert SupabaseStorage(settings, client=fake_api({}, [])).public_prefix.startswith(BASE + "/")


def test_the_engine_selects_it_with_storage_mode(make_settings, storage_env):
    from core import bootstrap

    engine = bootstrap(settings=make_settings(storage_mode="supabase"), console_logging=False)
    assert type(engine.registry.storage()).__name__ == "SupabaseStorage"
    assert os.environ["SUPABASE_URL"] == BASE
