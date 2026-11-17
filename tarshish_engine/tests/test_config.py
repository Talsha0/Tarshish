"""Normalized configuration: categories, content profiles, social accounts (YAML seed ➔ SQLite)."""

import shutil
import textwrap

import pytest

from core import bootstrap
from core.config import (
    CategoryConfig,
    ContentProfile,
    SocialAccount,
    SocialPage,
    load_seed_file,
    load_settings,
)
from core.models import Product, compute_canonical_hash
from core.resilience import ConfigError

TOKEN = "7123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw1"


def config_dir(tmp_path, make_settings, **bodies: str):
    """A copy of config/ with some seed files replaced."""
    target = tmp_path / "config"
    shutil.copytree(make_settings().config_dir, target)
    for name, body in bodies.items():
        (target / f"{name}.yaml").write_text(textwrap.dedent(body), encoding="utf-8")
    return target


def test_shipped_seed_files_are_valid_and_free_of_duplication(make_settings):
    root = make_settings().config_dir
    categories = load_seed_file(root / "categories.yaml")
    profiles = {p.content_type: p for p in load_seed_file(root / "content_profiles.yaml")}
    accounts = load_seed_file(root / "social_accounts.yaml")

    gadgets = next(c for c in categories if c.category_id == "gadgets")
    assert gadgets.temu_search == {"keyword": "smart tech gadgets", "l2Category": "Electronics & Appliances",
                                   "priceMin": 1, "priceMax": 100}
    assert gadgets.aliexpress_search["keyword"] == "smart gadgets"
    assert gadgets.default_sources() == ["temu_rapidapi", "aliexpress_rapidapi"]
    assert gadgets.search_for("temu_mock") == gadgets.temu_search  # a store's mock shares its rules

    # Feed posts and Stories are routed separately, each to its own generator and model.
    assert (profiles["instagram_post"].generator, profiles["instagram_post"].publish_format) == ("llm_text_gen", "image")
    assert (profiles["instagram_story"].generator, profiles["instagram_story"].publish_format) == ("llm_story_gen", "story")
    assert profiles["instagram_post"].llm_model != profiles["instagram_story"].llm_model
    assert profiles["tiktok_video"].generator == "ffmpeg_video_gen"

    keys = {account.key for account in accounts}
    assert {("gadgets", "he", "instagram"), ("gadgets", "en", "instagram"),
            ("gadgets", "he", "tiktok"), ("gadgets", "en", "tiktok")} <= keys
    he, en = (next(a for a in accounts if a.key == ("gadgets", lang, "instagram")) for lang in ("he", "en"))
    assert he.account_handle != en.account_handle and he.token_env_var != en.token_env_var


def test_invalid_seed_values_halt_with_a_descriptive_error(tmp_path):
    path = tmp_path / "categories.yaml"
    path.write_text(textwrap.dedent("""
        categories:
          - category_id: "gadgets"
            display_name_en: "Gadgets"
            min_rating: 9.5
          - category_id: "gadgets"
            display_name_en: "Again"
    """), encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_seed_file(path)
    message = str(excinfo.value)
    assert "categories 'gadgets'" in message and "min_rating" in message

    path.write_text("categories:\n  - {category_id: a1, display_name_en: A}\n  - {category_id: a1, display_name_en: B}\n")
    with pytest.raises(ConfigError, match="duplicate category ids: a1"):
        load_seed_file(path)


def test_raw_secrets_are_rejected_everywhere():
    with pytest.raises(ValueError, match="NAME of an environment variable"):
        SocialAccount(category_id="g1", language="he", platform="telegram", account_handle="@c", token_env_var=TOKEN)
    with pytest.raises(ValueError, match="looks like a raw credential"):
        SocialAccount(category_id="g1", language="he", platform="telegram", account_handle=TOKEN)
    with pytest.raises(ValueError, match="NAME of an environment variable"):
        SocialPage(platform="telegram_pub", page_name="@c", token_env_var=TOKEN)
    with pytest.raises(ValueError, match="raw secret field"):
        SocialPage.model_validate({"platform": "telegram_pub", "token": "anything"})


def test_category_ids_are_url_slugs_and_languages_are_codes():
    assert CategoryConfig(category_id="Home Decor", display_name_en="Home").category_id == "home-decor"
    assert SocialAccount(category_id="Home Decor", language="EN", platform="Instagram",
                         account_handle="h").key == ("home-decor", "en", "instagram")
    with pytest.raises(ValueError, match="language code"):
        SocialAccount(category_id="x1", language="english!", platform="instagram", account_handle="h")
    with pytest.raises(ValueError, match="must be one of"):
        ContentProfile(content_type="x_post", platform="x", generator="llm_text_gen", publish_format="hologram")


def test_unknown_generator_in_yaml_halts_startup(tmp_path, make_settings):
    root = config_dir(tmp_path, make_settings, content_profiles="""
        content_profiles:
          - content_type: "broken_post"
            platform: "instagram"
            generator: "does_not_exist"
    """)
    with pytest.raises(ConfigError, match="unknown generator plugin 'does_not_exist'"):
        bootstrap(settings=make_settings(config_dir=root), console_logging=False)


def test_invalid_environment_halts_with_the_variable_name(monkeypatch, tmp_path):
    monkeypatch.setenv("MAX_MONTHLY_AI_BUDGET_USD", "a lot")
    monkeypatch.setenv("WORKER_TIMEZONE", "Mars/Olympus")
    with pytest.raises(ConfigError) as excinfo:
        load_settings(env_file=None)
    message = str(excinfo.value)
    assert "MAX_MONTHLY_AI_BUDGET_USD" in message and "WORKER_TIMEZONE" in message


def test_every_entity_round_trips_through_the_database(engine):
    category = CategoryConfig(
        category_id="kitchen", display_name_en="Kitchen", display_name_he="מטבח", min_rating=4.2, min_orders=10,
        temu_search={"keyword": "kitchen tools", "priceMax": 40}, aliexpress_search=None,
    )
    profile = ContentProfile(content_type="youtube_short", platform="youtube", generator="ffmpeg_video_gen",
                             llm_model="gemini-3.5-flash-lite", publish_format="video", tone="calm")
    account = SocialAccount(category_id="kitchen", language="en", platform="telegram", account_handle="@k",
                            token_env_var="TG_K_TOKEN", account_id_env_var="TG_K_CHAT")
    engine.registry.save_category(category)
    engine.registry.save_content_profile(profile)
    engine.registry.save_social_account(account)

    assert engine.registry.get_category("kitchen") == category
    assert engine.registry.get_content_profile("youtube_short") == profile
    assert engine.registry.get_social_account("kitchen", "en", "telegram") == account
    # The account id feeds the publisher's own account credential: Telegram calls it chat_id.
    page = engine.registry.social_page(account)
    assert page.env_refs() == {"token": "TG_K_TOKEN", "chat_id": "TG_K_CHAT"} and page.platform == "telegram_pub"
    story = engine.registry.social_page(engine.registry.get_social_account("gadgets", "he", "instagram"),
                                        engine.registry.get_content_profile("instagram_story"))
    assert story.options() == {"content_type": "story"}
    assert story.env_refs() == {"token": "INSTAGRAM_ACCESS_TOKEN", "account_id": "INSTAGRAM_BUSINESS_ACCOUNT_ID"}


def test_database_is_seeded_once_then_is_the_source_of_truth(engine, make_settings):
    assert sorted(c.category_id for c in engine.registry.list_categories()) == ["gadgets", "home", "sports"]
    sports = engine.registry.get_category("sports")
    engine.registry.save_category(sports.model_copy(update={"enabled": False}))
    again = bootstrap(settings=make_settings(), console_logging=False)  # restart: YAML must not overwrite the DB
    assert again.registry.get_category("sports").enabled is False
    assert again.registry.sync_from_yaml() == {"categories": 3, "content_profiles": 5, "social_accounts": 5}
    assert again.registry.get_category("sports").enabled is True


def test_canonical_hash_matches_the_same_item_across_sources():
    a = compute_canonical_hash("NEW 2026 Men's Running Shoes - Breathable Mesh")
    b = compute_canonical_hash("breathable mesh running shoes, men's  (Free Shipping)")
    assert a == b
    assert a != compute_canonical_hash("Women's Running Shoes - Breathable Mesh")

    product = Product(
        external_id="1", source="amazon", category="sports", title="Breathable Mesh Men's Running Shoes", price=10,
        rating=4.5, orders_count=3, raw_url="https://a.example/1", affiliate_url="https://a.example/aff/1",
    )
    assert product.canonical_hash == a  # derived automatically when the source does not supply one
