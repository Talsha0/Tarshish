"""A content type whose model has no API key: the free LLM chain writes instead, with a warning."""

from pathlib import Path

import pytest

from core.config import ContentProfile
from plugins.generators import free_llm_chain
from plugins.generators.free_llm_chain import FreeLLMChainExhausted, LLMResult

APP = Path(__file__).resolve().parents[1] / "dashboard" / "app.py"
REPLY = '{"caption": "אוזניות במחיר מטורף 🎧", "hashtags": ["דילים", "FindHub"]}'


@pytest.fixture
def claude_route(ready_engine):
    """console_post routed to a Claude model whose ANTHROPIC_API_KEY is not set (tests have no keys)."""
    ready_engine.registry.save_content_profile(ContentProfile(
        content_type="console_post", platform="console", generator="llm_text_gen", llm_model="claude-sonnet-5-5"))
    return ready_engine


@pytest.fixture
def ready_engine(harvested, console_route):
    return harvested


@pytest.fixture
def free_reply(monkeypatch):
    """A free key is configured and the chain answers (no network): records each prompt it was sent."""
    prompts: list[str] = []
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_free_key_0123456789")

    def complete(self, system, prompt, *, validate=None, max_tokens=1200, temperature=0.8):
        prompts.append(prompt)
        assert validate is None or validate(REPLY)
        return LLMResult(text=REPLY, provider="groq", model="llama-3.3-70b-versatile", tokens_input=120, tokens_output=40)

    monkeypatch.setattr(free_llm_chain.FreeLLMChain, "complete", complete)
    return prompts


def test_a_missing_model_key_falls_back_to_the_free_chain_with_a_warning(claude_route, free_reply):
    report = claude_route.orchestrator.run("sports", "he", ["console_post"], mode="queued", limit=2)

    assert report.ok and report.count("queued") == 2 and len(free_reply) == 2
    assert "Hebrew" in free_reply[0] and '"caption"' in free_reply[0]  # the generator's own prompt + JSON contract
    rows = claude_route.db.list_publications()
    assert {row["caption"] for row in rows} == {"אוזניות במחיר מטורף 🎧"}
    assert {row["model_used"] for row in rows} == {"groq:llama-3.3-70b-versatile [free fallback for claude-sonnet-5-5]"}
    assert {row["generation_cost_usd"] for row in rows} == {0.0}
    assert report.warnings == [(
        "console_post: ANTHROPIC_API_KEY is not set: claude-sonnet-5-5 was replaced by the free LLM "
        "groq:llama-3.3-70b-versatile. Add the key to write with claude-sonnet-5-5."
    )]
    usage = claude_route.db.query("SELECT generation_cost_usd, tokens_input FROM ai_usage")
    assert usage and all(row["generation_cost_usd"] == 0 and row["tokens_input"] == 120 for row in usage)


def test_when_no_free_model_answers_the_offline_templates_write_and_the_warning_says_why(claude_route, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_free_key_0123456789")

    def exhausted(self, *args, **kwargs):
        raise FreeLLMChainExhausted(["groq:llama-3.3-70b-versatile: quota exhausted (HTTP 429)"])

    monkeypatch.setattr(free_llm_chain.FreeLLMChain, "complete", exhausted)
    report = claude_route.orchestrator.run("sports", "he", ["console_post"], mode="queued", limit=1)

    assert report.ok and report.count("queued") == 1
    assert claude_route.db.list_publications()[0]["model_used"] == "offline-template"
    assert len(report.warnings) == 1 and "no free LLM could answer" in report.warnings[0]
    assert "HTTP 429" in report.warnings[0] and "offline templates" in report.warnings[0]


def test_without_any_free_key_the_warning_names_the_keys_to_add(claude_route):
    report = claude_route.orchestrator.run("sports", "he", ["console_post"], mode="queued", limit=1)

    assert report.ok and claude_route.db.list_publications()[0]["model_used"] == "offline-template"
    assert len(report.warnings) == 1
    assert all(name in report.warnings[0] for name in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY"))


def test_a_dry_run_warns_ahead_and_calls_nothing(claude_route, free_reply, capsys):
    report = claude_route.orchestrator.run("sports", "he", ["console_post"], dry_run=True, limit=1)

    assert free_reply == []  # dry-runs never call a model
    assert report.items[0].model_used.endswith("[ANTHROPIC_API_KEY not set: live runs use the free LLM chain]")
    assert report.warnings == [("console_post: ANTHROPIC_API_KEY is not set: live runs replace claude-sonnet-5-5 "
                                "with the free LLM chain (Gemini -> Groq -> OpenRouter free tiers).")]


def test_a_gemini_model_without_its_key_uses_the_other_free_providers(claude_route, free_reply):
    claude_route.registry.save_content_profile(ContentProfile(
        content_type="console_post", platform="console", generator="llm_text_gen", llm_model="gemini-3.8-flash"))

    report = claude_route.orchestrator.run("sports", "he", ["console_post"], mode="queued", limit=1)

    assert claude_route.db.list_publications()[0]["model_used"] == (
        "groq:llama-3.3-70b-versatile [free fallback for gemini-3.8-flash]")
    assert report.warnings and report.warnings[0].startswith("console_post: GEMINI_API_KEY is not set:")


def test_the_dashboard_flags_models_without_keys_and_free_written_posts(tmp_path, monkeypatch):
    AppTest = pytest.importorskip("streamlit.testing.v1").AppTest
    import streamlit as st

    st.cache_resource.clear()
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "dashboard.db"))
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "logs" / "dashboard.log"))
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_free_key_0123456789")
    app = AppTest.from_file(str(APP), default_timeout=120).run()

    assert not app.exception
    warnings = [warning.value for warning in app.warning]
    assert any("ANTHROPIC_API_KEY is not set: live runs replace" in text and "free LLM chain" in text for text in warnings)
    st.cache_resource.clear()


def _rejecting_client(calls: list[str]):
    """An Anthropic client whose every call fails like a wrong ANTHROPIC_API_KEY (no network)."""
    import anthropic
    import httpx

    def create(**request):
        calls.append(request["model"])
        response = httpx.Response(401, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
        raise anthropic.AuthenticationError(
            "Error code: 401", response=response,
            body={"type": "error", "error": {"type": "authentication_error", "message": "Invalid bearer token"}})

    messages = type("Messages", (), {"create": staticmethod(create)})()
    return type("Client", (), {"messages": messages, "beta": type("Beta", (), {"messages": messages})()})()


def test_a_rejected_model_key_falls_back_to_the_free_chain_and_says_to_check_the_key(claude_route, free_reply,
                                                                                      monkeypatch):
    from pydantic import SecretStr

    from plugins.generators.llm_text_gen import LLMTextGenerator

    calls: list[str] = []
    monkeypatch.setattr(claude_route.settings, "anthropic_api_key", SecretStr("sk-ant-api03-revoked"))
    monkeypatch.setattr(LLMTextGenerator, "_client", lambda self, api_key: _rejecting_client(calls))
    report = claude_route.orchestrator.run("sports", "he", ["console_post"], mode="queued", limit=2)

    assert report.ok and report.count("queued") == 2 and report.count("failed") == 0
    assert calls == ["claude-sonnet-5-5"]  # asked once: the second product goes straight to the free chain
    assert {row["model_used"] for row in claude_route.db.list_publications()} == {
        "groq:llama-3.3-70b-versatile [free fallback for claude-sonnet-5-5]"}
    assert len(report.warnings) == 1
    assert "ANTHROPIC_API_KEY was rejected (Anthropic: HTTP 401, Invalid bearer token)" in report.warnings[0]
    assert "without quotes or spaces" in report.warnings[0]


@pytest.mark.parametrize("raw", ['  sk-ant-api03-abc\n', '"sk-ant-api03-abc"', "'sk-ant-api03-abc' "])
def test_llm_keys_are_read_without_spaces_or_wrapping_quotes(monkeypatch, raw):
    from core.config import Settings

    monkeypatch.setenv("ANTHROPIC_API_KEY", raw)
    monkeypatch.setenv("GROQ_API_KEY", raw)
    settings = Settings()
    assert settings.anthropic_api_key.get_secret_value() == "sk-ant-api03-abc"
    assert settings.groq_api_key.get_secret_value() == "sk-ant-api03-abc"
