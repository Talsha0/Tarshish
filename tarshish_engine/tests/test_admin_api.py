"""The web admin control plane (storefront_api/admin.py): auth, onboarding, jobs and the queue."""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from core.config import SocialAccount
from storefront_api.server import create_app

SECRET = "test-admin-secret-0123456789"
KEY = {"X-Admin-Key": SECRET}


@pytest.fixture
def api(make_settings, monkeypatch):
    monkeypatch.setenv("ADMIN_API_SECRET", SECRET)
    app = create_app(make_settings())
    with TestClient(app) as client:
        client.ctx = app.state.admin  # type: ignore[attr-defined]
        yield client


def run_job(api, response) -> dict:
    assert response.status_code == 202, response.text
    job = response.json()
    assert job["status"] in ("QUEUED", "RUNNING", "SUCCEEDED", "FAILED")
    finished = api.ctx.runner().wait(job["id"], timeout=60)
    assert finished is not None and finished["done"], finished
    return api.get(f"/api/admin/jobs/{job['id']}", headers=KEY).json()


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #
def test_admin_api_is_disabled_without_a_secret(make_settings):
    with TestClient(create_app(make_settings())) as client:
        response = client.get("/api/admin/overview", headers={"X-Admin-Key": "anything"})
        assert response.status_code == 503
        assert "ADMIN_API_SECRET" in response.json()["detail"]
        assert client.get("/api/health").status_code == 200  # the public storefront is unaffected


@pytest.mark.parametrize("weak", ["change_me_to_a_strong_secret", "short"])
def test_placeholder_or_short_secrets_keep_the_admin_api_closed(make_settings, monkeypatch, weak):
    monkeypatch.setenv("ADMIN_API_SECRET", weak)
    with TestClient(create_app(make_settings())) as client:
        assert client.get("/api/admin/session", headers={"X-Admin-Key": weak}).status_code == 503


def test_admin_api_requires_the_exact_key(api):
    assert api.get("/api/admin/session").status_code == 401
    assert api.get("/api/admin/session", headers={"X-Admin-Key": SECRET + "x"}).status_code == 401
    assert api.post("/api/admin/actions/harvest", json={}).status_code == 401
    ok = api.get("/api/admin/session", headers=KEY)
    assert ok.status_code == 200 and ok.json()["ok"] is True
    assert ok.headers["cache-control"] == "no-store"


def test_cors_allows_the_configured_vercel_domain(make_settings, monkeypatch):
    monkeypatch.setenv("ALLOWED_ORIGINS", "https://findhub.vercel.app/, https://admin.example.com")
    with TestClient(create_app(make_settings())) as client:
        preflight = client.options("/api/admin/overview", headers={
            "Origin": "https://findhub.vercel.app", "Access-Control-Request-Method": "PUT",
            "Access-Control-Request-Headers": "X-Admin-Key",
        })
        assert preflight.headers["access-control-allow-origin"] == "https://findhub.vercel.app"
        assert "x-admin-key" in preflight.headers["access-control-allow-headers"].lower()
        blocked = client.get("/api/health", headers={"Origin": "https://evil.example"})
        assert "access-control-allow-origin" not in blocked.headers


# --------------------------------------------------------------------------- #
# Overview: KPIs, sources health, env detection (names only)
# --------------------------------------------------------------------------- #
def test_overview_reports_env_status_without_values(api, monkeypatch):
    monkeypatch.setenv("RAPIDAPI_KEY_TEMU_1", "temu-secret-value-123456")
    monkeypatch.setenv("INSTAGRAM_ACCESS_TOKEN", "IGQVJ-super-secret-token-value")
    response = api.get("/api/admin/overview", headers=KEY)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body["kpis"]) >= {"storefront_clicks", "published_posts", "active_products"}
    assert set(body["queue"]) == {"QUEUED", "PENDING", "PUBLISHED", "FAILED"}
    assert body["categories"]["active"] >= 1
    temu = body["sources"]["temu"]
    assert temu["providers"] and temu["providers"][0]["order"] == 1
    assert any(p["key_status"] == "Ready" for p in temu["providers"])
    assert {p["key_status"] for p in body["sources"]["aliexpress"]["providers"]} == {"Missing"}
    instagram = next(row for row in body["platform_credentials"] if row["platform"] == "instagram")
    token = next(env for env in instagram["env"] if env["credential"] == "token")
    assert token == {"credential": "token", "env_var": "INSTAGRAM_ACCESS_TOKEN", "status": "Configured", "required": True}
    assert "temu-secret-value" not in response.text and "super-secret" not in response.text


def test_env_check_reports_presence_of_names_only(api, monkeypatch):
    monkeypatch.setenv("IG_GADGETS_HE_TOKEN", "secret-value")
    body = api.get("/api/admin/env-check", params={"names": "IG_GADGETS_HE_TOKEN,NOPE_TOKEN,bad name"},
                   headers=KEY).json()
    assert body == {"vars": {"IG_GADGETS_HE_TOKEN": "Configured", "NOPE_TOKEN": "Missing"}}


# --------------------------------------------------------------------------- #
# Categories & social-account onboarding
# --------------------------------------------------------------------------- #
def test_category_crud(api):
    created = api.post("/api/admin/categories", headers=KEY, json={
        "display_name_en": "Pet Supplies", "display_name_he": "מוצרים לחיות", "aliexpress_keyword": "pet toys",
        "price_max": 40,
    })
    assert created.status_code == 201, created.text
    assert created.json()["category_id"] == "pet-supplies"
    assert created.json()["aliexpress_search"] == {"keyword": "pet toys", "priceMax": 40}
    assert api.post("/api/admin/categories", headers=KEY, json={"display_name_en": "Pet Supplies"}).status_code == 409
    updated = api.put("/api/admin/categories/pet-supplies", headers=KEY, json={"enabled": False, "min_orders": 50})
    assert updated.status_code == 200 and updated.json()["enabled"] is False and updated.json()["min_orders"] == 50
    assert updated.json()["display_name_he"] == "מוצרים לחיות"  # untouched fields are kept
    listed = {c["category_id"] for c in api.get("/api/admin/categories", headers=KEY).json()["categories"]}
    assert {"gadgets", "pet-supplies"} <= listed
    assert api.delete("/api/admin/categories/pet-supplies", headers=KEY).status_code == 200
    assert api.delete("/api/admin/categories/gadgets", headers=KEY).status_code == 409  # still has accounts


def test_onboarding_creates_a_category_inline(api, monkeypatch):
    monkeypatch.setenv("IG_KITCHEN_HE_TOKEN", "token-value-not-returned")
    response = api.post("/api/admin/social-accounts", headers=KEY, json={
        "page_handle": "@tarshish_kitchen_il", "platform": "instagram", "language": "he",
        "new_category": {"display_name_en": "Kitchen Tools", "display_name_he": "כלי מטבח",
                         "temu_keyword": "kitchen gadgets", "aliexpress_keyword": "kitchen tools"},
        "token_env_var": "IG_KITCHEN_HE_TOKEN",
        "platform_metadata": {"app_id": "1234567890", "business_account_id": "17841400000000001"},
    })
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["created_category"] == "kitchen-tools"
    account = body["account"]
    assert account["id"] == "kitchen-tools:he:instagram"
    # The business account id given inline satisfies the publisher's account credential.
    assert account["status"] == "ready", account
    assert {e["credential"]: e["inline_from"] for e in account["env"]}["account_id"] == "business_account_id"
    assert "token-value-not-returned" not in response.text

    engine = api.ctx.engine()
    category = engine.registry.get_category("kitchen-tools")
    assert category.temu_search == {"keyword": "kitchen gadgets"}
    assert category.display_name_he == "כלי מטבח"
    stored = engine.registry.get_social_account("kitchen-tools", "he", "instagram")
    assert stored.platform_metadata == {"app_id": "1234567890", "business_account_id": "17841400000000001"}
    target, reason = engine.registry.resolve_target(engine.registry.social_page(stored))
    assert reason is None and target.credential("account_id") == "17841400000000001"

    again = api.post("/api/admin/social-accounts", headers=KEY, json={
        "page_handle": "@other", "platform": "instagram", "language": "he", "category_id": "kitchen-tools",
        "token_env_var": "IG_KITCHEN_HE_TOKEN",
    })
    assert again.status_code == 409  # one page per category × language × platform


@pytest.mark.parametrize("payload, message", [
    ({"token_env_var": "EAABwzLixnjYBO1234567890abcdefABCDEF"}, "NAME"),
    ({"token_env_var": "has spaces"}, "NAME"),
    ({"platform_metadata": {"access_token": "x"}}, "credential"),
    ({"platform_metadata": {"chat_id": "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"}}, "raw credential"),
    ({"platform_metadata": {"app_id": "EAABwzLixnjYBO7ZBZCaZBq2ZAZDZD1234567890xyz"}}, "raw credential"),
])
def test_onboarding_refuses_secrets_and_leaves_no_orphan_category(api, payload, message):
    response = api.post("/api/admin/social-accounts", headers=KEY, json={
        "page_handle": "@tarshish_deals", "platform": "telegram", "language": "en",
        "new_category": {"display_name_en": "Orphan Check"}, **payload,
    })
    assert response.status_code == 422, response.text
    assert message in response.json()["detail"]
    assert api.ctx.engine().db.get_config("categories", "orphan-check") is None


def test_account_update_verify_and_delete(api, monkeypatch):
    created = api.post("/api/admin/social-accounts", headers=KEY, json={
        "page_handle": "@tarshish_deals", "platform": "telegram", "language": "en", "category_id": "gadgets",
        "token_env_var": "TG_GADGETS_EN_TOKEN", "platform_metadata": {"chat_id": "-1001234567890"},
    })
    assert created.status_code == 201, created.text
    assert created.json()["account"]["status"] == "missing_credentials"
    monkeypatch.setenv("TG_GADGETS_EN_TOKEN", "123456:telegram-bot-token")

    from plugins.publishers import telegram_pub

    def telegram(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        assert request.url.params.get("chat_id") in (None, "-1001234567890")
        result = {
            "getMe": {"id": 42, "username": "tarshish_bot"},
            "getChat": {"id": -1001234567890, "title": "Tarshish Deals", "type": "channel"},
            "getChatMember": {"status": "administrator", "can_post_messages": True},
        }[method]
        return httpx.Response(200, json={"ok": True, "result": result})

    real = telegram_pub.verify_pages
    monkeypatch.setattr(telegram_pub, "verify_pages",
                        lambda registry, pages: real(registry, pages, transport=httpx.MockTransport(telegram)))
    check = api.post("/api/admin/actions/verify-account/gadgets:en:telegram", headers=KEY).json()
    assert check["ok"] is True, check
    assert "tarshish_bot" in check["summary"] and "can post" in check["summary"]
    assert "telegram-bot-token" not in json.dumps(check)

    updated = api.put("/api/admin/social-accounts/gadgets:en:telegram", headers=KEY,
                      json={"page_handle": "@tarshish_deals_en", "enabled": False})
    assert updated.status_code == 200 and updated.json()["status"] == "disabled"
    assert updated.json()["platform_metadata"] == {"chat_id": "-1001234567890"}
    assert api.delete("/api/admin/social-accounts/gadgets:en:telegram", headers=KEY).status_code == 200
    assert api.post("/api/admin/actions/verify-account/gadgets:en:telegram", headers=KEY).status_code == 404


def test_explicit_env_var_beats_inline_metadata(engine, monkeypatch):
    account = SocialAccount(category_id="gadgets", language="he", platform="instagram", account_handle="g",
                            token_env_var="IG_T", account_id_env_var="IG_ID",
                            platform_metadata={"business_account_id": "111"})
    monkeypatch.setenv("IG_T", "token")
    page = engine.registry.social_page(account)
    target, _ = engine.registry.resolve_target(page)
    assert target.credential("account_id") == "111"  # IG_ID unset -> the inline id
    monkeypatch.setenv("IG_ID", "222")
    target, _ = engine.registry.resolve_target(page)
    assert target.credential("account_id") == "222"  # the variable the account names wins


def test_content_profiles_route_models(api):
    body = api.get("/api/admin/content-profiles", headers=KEY).json()
    ids = {m["id"] for m in body["models"]}
    assert {"mock", "claude-sonnet-5-5", "claude-haiku-5-5"} <= ids
    assert {p["content_type"] for p in body["profiles"]} >= {"instagram_post", "instagram_story", "tiktok_video",
                                                              "youtube_short", "telegram_post"}
    updated = api.put("/api/admin/content-profiles/instagram_story", headers=KEY,
                      json={"llm_model": "claude-sonnet-5-5", "tone": "calm"})
    assert updated.status_code == 200 and updated.json()["model_label"] == "Claude Sonnet 5.5"
    profile = api.ctx.engine().registry.get_content_profile("instagram_story")
    assert (profile.llm_model, profile.tone, profile.generator) == ("claude-sonnet-5-5", "calm", "llm_story_gen")
    assert api.put("/api/admin/content-profiles/instagram_story", headers=KEY,
                   json={"generator": "no_such_gen"}).status_code == 422


# --------------------------------------------------------------------------- #
# Background jobs: harvest and publish (dry run)
# --------------------------------------------------------------------------- #
def test_harvest_runs_as_a_background_job(api):
    job = run_job(api, api.post("/api/admin/actions/harvest", headers=KEY,
                                json={"source": "temu_mock", "category_id": "gadgets", "limit": 5}))
    assert job["status"] == "SUCCEEDED", job["log"]
    assert job["command"] == "python main.py --harvest --source temu_mock --category gadgets --limit 5"
    text = "\n".join(job["log"])
    assert "Harvest temu_mock ➔ category gadgets" in text and "inserted" in text
    products = api.ctx.engine().db.count_products("gadgets")
    assert sum(row["products"] for row in products) > 0

    # incremental polling: only the lines after `since`
    tail = api.get(f"/api/admin/jobs/{job['id']}", params={"since": job["log_total"] - 1}, headers=KEY).json()
    assert len(tail["log"]) == 1 and tail["log_total"] == job["log_total"]
    listed = api.get("/api/admin/jobs", headers=KEY).json()["jobs"]
    assert listed[0]["id"] == job["id"] and listed[0]["log"] == []
    row = api.ctx.engine().db.get_job_run(job["id"])
    assert row["status"] == "SUCCEEDED" and row["exit_code"] == 0 and "Harvest temu_mock" in row["log_text"]


def test_publish_dry_run_job_writes_nothing(api, harvested, console_route):
    job = run_job(api, api.post("/api/admin/actions/publish", headers=KEY, json={
        "category_id": "gadgets", "language": "he", "content_types": ["console_post"], "dry_run": True,
    }))
    assert job["status"] == "SUCCEEDED", job["log"]
    text = "\n".join(job["log"])
    assert "Publishing pipeline gadgets_he" in text and "DRY-RUN" in text
    assert api.ctx.engine().db.get_status_counts() == {"PENDING": 0, "QUEUED": 0, "PUBLISHED": 0, "FAILED": 0}


def test_invalid_actions_are_refused_before_a_job_starts(api):
    assert api.post("/api/admin/actions/harvest", headers=KEY, json={"source": "nope"}).status_code == 422
    assert api.post("/api/admin/actions/harvest", headers=KEY, json={"category_id": "nope"}).status_code == 404
    assert api.post("/api/admin/actions/publish", headers=KEY,
                    json={"content_types": ["no_such_type"]}).status_code == 422
    assert api.get("/api/admin/jobs", headers=KEY).json()["jobs"] == []


def test_a_failing_command_marks_the_job_failed(api):
    job = run_job(api, api.post("/api/admin/actions/seed", headers=KEY,
                                json={"store": "temu", "category_id": "gadgets", "keyword": "watch"}))
    assert job["status"] == "FAILED" and job["exit_code"] == 1  # no RapidAPI key: no provider delivers


def test_jobs_are_refused_on_a_serverless_host(api, monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    response = api.post("/api/admin/actions/sync-config", headers=KEY)
    assert response.status_code == 409 and "long-running backend" in response.json()["detail"]
    assert api.get("/api/admin/categories", headers=KEY).status_code == 200  # configuration still works


# --------------------------------------------------------------------------- #
# Publication queue
# --------------------------------------------------------------------------- #
def test_queue_retry_publish_now_and_delete(api, harvested, console_route):
    job = run_job(api, api.post("/api/admin/actions/publish", headers=KEY, json={
        "category_id": "gadgets", "language": "en", "content_types": ["console_post"], "mode": "queued", "limit": 2,
    }))
    assert job["status"] == "SUCCEEDED", job["log"]
    queue = api.get("/api/admin/queue", params={"status": "QUEUED", "category": "gadgets"}, headers=KEY).json()
    assert len(queue["items"]) == 2 and queue["counts"]["QUEUED"] == 2
    first, second = (item["id"] for item in queue["items"])

    assert api.post(f"/api/admin/queue/{first}/retry", headers=KEY).status_code == 409  # not FAILED
    published = run_job(api, api.post(f"/api/admin/queue/{first}/publish-now", headers=KEY))
    assert published["status"] == "SUCCEEDED", published["log"]
    assert api.ctx.engine().db.get_publication(first)["status"] == "PUBLISHED"

    api.ctx.engine().db.mark_failed(second, "boom")
    assert api.post(f"/api/admin/queue/{second}/retry", headers=KEY).json() == {"id": second, "status": "QUEUED"}
    assert api.delete(f"/api/admin/queue/{second}", headers=KEY).status_code == 200
    assert api.ctx.engine().db.get_publication(second) is None
    assert api.post("/api/admin/queue/flush", headers=KEY, json={"category_id": "gadgets"}).status_code == 422
    assert api.post("/api/admin/queue/flush", headers=KEY, json={}).json() == {"flushed": 0}
