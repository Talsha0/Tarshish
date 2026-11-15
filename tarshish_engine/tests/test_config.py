import textwrap

import pytest

from core import bootstrap
from core.config import PipelineConfig, SocialPage, load_pipelines_file, load_settings
from core.models import Product, compute_canonical_hash
from core.resilience import ConfigError

BASE = {
    "id": "sports_he", "niche": "running_and_sports", "language": "he",
    "source": "aliexpress_mock", "generator": "llm_text_gen", "publishers": ["console_pub"],
}


def write_yaml(tmp_path, body: str):
    path = tmp_path / "pipelines.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def test_example_yaml_is_valid_and_normalised(make_settings):
    pipelines = load_pipelines_file(make_settings().pipelines_config_path)
    pipeline = pipelines[0]
    assert pipeline.id == "sports_he"
    assert pipeline.llm_model == "anthropic"  # provider alias, resolved by the generator plugin
    assert [page.key for page in pipeline.social_pages] == [
        ("console_pub", "default"), ("telegram_pub", "@my_sports_channel"),
    ]
    assert pipeline.social_pages[1].env_refs() == {"token": "TG_SPORTS_BOT_TOKEN", "chat_id": "TG_SPORTS_CHAT_ID"}


def test_invalid_yaml_values_halt_with_a_descriptive_error(tmp_path):
    path = write_yaml(tmp_path, """
        pipelines:
          - id: "sports_he"
            niche: "running"
            source: "aliexpress_mock"
            generator: "llm_text_gen"
            publishers: ["console_pub"]
            publishing_mode: "sometimes"
            filters: {min_rating: 9.5}
    """)
    with pytest.raises(ConfigError) as excinfo:
        load_pipelines_file(path)
    message = str(excinfo.value)
    assert "pipeline 'sports_he'" in message
    assert "publishing_mode" in message and "min_rating" in message


def test_raw_secrets_are_rejected_in_pipeline_config():
    token = "7123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw1"
    with pytest.raises(ValueError, match="NAME of an environment variable"):
        SocialPage(platform="telegram_pub", page_name="@c", token_env_var=token)
    with pytest.raises(ValueError, match="raw secret field"):
        SocialPage.model_validate({"platform": "telegram_pub", "token": "anything"})
    with pytest.raises(ValueError, match="looks like a raw credential"):
        SocialPage.model_validate({"platform": "telegram_pub", "note": token})


def test_unknown_plugin_in_yaml_halts_startup(tmp_path, make_settings):
    path = write_yaml(tmp_path, """
        pipelines:
          - id: "broken"
            niche: "x"
            source: "does_not_exist"
            generator: "llm_text_gen"
            publishers: ["console_pub"]
    """)
    with pytest.raises(ConfigError, match="unknown source plugin 'does_not_exist'"):
        bootstrap(settings=make_settings(pipelines_config_path=path), console_logging=False)


def test_invalid_environment_halts_with_the_variable_name(monkeypatch, tmp_path):
    monkeypatch.setenv("MAX_MONTHLY_AI_BUDGET_USD", "a lot")
    monkeypatch.setenv("WORKER_TIMEZONE", "Mars/Olympus")
    with pytest.raises(ConfigError) as excinfo:
        load_settings(env_file=None)
    message = str(excinfo.value)
    assert "MAX_MONTHLY_AI_BUDGET_USD" in message and "WORKER_TIMEZONE" in message


def test_pipeline_round_trips_through_the_database(engine):
    original = PipelineConfig.model_validate({
        **BASE, "id": "gadgets_en", "language": "en", "llm_model": "gemini-3.5-flash-lite",
        "publishing_mode": "queued", "limit": 2, "prompt_template": "Sell {title}",
        "filters": {"min_rating": 4.2, "min_orders": 10}, "options": {"category_id": 44},
        "social_pages": [{"platform": "telegram_pub", "page_name": "@g", "token_env_var": "TG_G_TOKEN",
                          "chat_id_env_var": "TG_G_CHAT", "parse_mode": "HTML"}],
    })
    engine.registry.save_pipeline(original)
    loaded = engine.registry.get_pipeline("gadgets_en")
    assert loaded.model_dump(exclude={"llm_provider"}) == original.model_dump(exclude={"llm_provider"})
    assert loaded.social_pages[-1].options() == {"parse_mode": "HTML"}
    # Zero-secret storage: only env-var NAMES are persisted.
    stored = engine.db.get_pipeline("gadgets_en")["social_pages_json"]
    assert "TG_G_TOKEN" in stored


def test_database_is_seeded_once_then_is_the_source_of_truth(engine, make_settings):
    assert [p.id for p in engine.registry.list_pipelines()] == ["sports_he"]
    engine.registry.set_enabled("sports_he", False)
    again = bootstrap(settings=make_settings(), console_logging=False)  # restart: YAML must not overwrite the DB
    assert again.registry.get_pipeline("sports_he").enabled is False
    assert again.registry.sync_from_yaml() == 1  # explicit re-sync does
    assert again.registry.get_pipeline("sports_he").enabled is True


def test_canonical_hash_matches_the_same_item_across_sources():
    a = compute_canonical_hash("NEW 2026 Men's Running Shoes - Breathable Mesh")
    b = compute_canonical_hash("breathable mesh running shoes, men's  (Free Shipping)")
    assert a == b
    assert a != compute_canonical_hash("Women's Running Shoes - Breathable Mesh")

    product = Product(
        external_id="1", source="amazon", niche="sports", title="Breathable Mesh Men's Running Shoes", price=10,
        rating=4.5, orders_count=3, raw_url="https://a.example/1", affiliate_url="https://a.example/aff/1",
    )
    assert product.canonical_hash == a  # derived automatically when the source does not supply one
