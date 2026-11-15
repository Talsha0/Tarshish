import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from core.interfaces import PluginContext
from core.models import PublishTarget
from core.resilience import BudgetExceededError, BudgetGuard, NonRetryableError
from plugins.generators.llm_text_gen import (
    LLMTextGenerator,
    OFFLINE_MODEL,
    _supports_effort,
    _supports_json_schema,
    estimate_cost_usd,
    parse_caption_json,
    price_for,
    resolve_model,
)
from plugins.publishers import render_post_text, utf16_length

TOKEN = "7123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw1"


@pytest.fixture
def product(engine):
    wired = engine.registry.wire(engine.registry.get_pipeline("sports_he"), dry_run=True)
    return wired.source.fetch_products("running_and_sports", 4.5, 200, 3)[0]


@pytest.fixture
def content(engine, product):
    generator = LLMTextGenerator(PluginContext(settings=engine.settings, llm_model="mock"))
    return generator.generate(product, "he", None)


# -- source ---------------------------------------------------------------------- #
def test_mock_source_returns_three_products_and_applies_filters(engine):
    source = engine.registry.wire(engine.registry.get_pipeline("sports_he"), dry_run=True).source
    products = source.fetch_products("running_and_sports", 4.5, 200, 10)
    assert len(products) == 3 and all(p.currency == "ILS" and p.canonical_hash for p in products)
    assert len(source.fetch_products("running_and_sports", 4.75, 200, 10)) == 1
    assert len(source.fetch_products("running_and_sports", 0, 0, 2)) == 2


# -- post formatting --------------------------------------------------------------- #
def test_post_text_never_cuts_the_link_when_truncated(content):
    long_content = content.model_copy(update={"caption": "מבצע " * 600})
    text = render_post_text(long_content, max_length=1024)
    assert utf16_length(text) <= 1024
    assert content.product.affiliate_url in text and "…" in text
    assert "קישור שותפים" in render_post_text(content) and "קישור שותפים" not in render_post_text(content, disclosure=False)


# -- Telegram ------------------------------------------------------------------------ #
def telegram(engine, handler):
    cls = engine.plugins.get("publisher", "telegram_pub")
    target = PublishTarget(
        platform="telegram_pub", page_name="@sports",
        credentials={"token": SecretStr(TOKEN), "chat_id": SecretStr("@sports")},
    )
    publisher = cls(PluginContext(settings=engine.settings), target)
    publisher._transport = httpx.MockTransport(handler)
    return publisher


def ok(message_id: int = 42) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": {"message_id": message_id}})


def test_telegram_sends_a_photo_with_caption(engine, content):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, json.loads(request.content)))
        return ok()

    receipt = telegram(engine, handler).publish(content)

    path, body = seen[0]
    assert receipt.success and receipt.platform_post_id == "42"
    assert path == f"/bot{TOKEN}/sendPhoto" and body["chat_id"] == "@sports"
    assert body["photo"] == content.product.image_urls[0]
    assert content.product.affiliate_url in body["caption"] and utf16_length(body["caption"]) <= 1024


def test_telegram_falls_back_to_text_when_the_media_is_rejected(engine, content):
    methods = []

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        methods.append(method)
        if method == "sendPhoto":
            return httpx.Response(400, json={"ok": False, "error_code": 400, "description": "Bad Request: wrong file identifier/HTTP URL specified"})
        return ok(7)

    receipt = telegram(engine, handler).publish(content)
    assert methods == ["sendPhoto", "sendMessage"] and receipt.platform_post_id == "7"


def test_telegram_waits_out_rate_limits(engine, content, clean_environment):
    responses = [
        httpx.Response(429, json={"ok": False, "error_code": 429, "description": "Too Many Requests", "parameters": {"retry_after": 9}}),
        ok(),
    ]
    receipt = telegram(engine, lambda request: responses.pop(0)).publish(content)
    assert receipt.success and clean_environment == [9.0]


def test_telegram_errors_never_leak_the_bot_token(engine, content):
    unauthorized = httpx.Response(401, json={"ok": False, "error_code": 401, "description": "Unauthorized"})
    with pytest.raises(NonRetryableError) as rejected:
        telegram(engine, lambda request: unauthorized).publish(content)
    assert "401" in str(rejected.value) and TOKEN not in str(rejected.value)

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    with pytest.raises(Exception) as network:
        telegram(engine, unreachable).publish(content)
    assert TOKEN not in str(network.value) and "[REDACTED]" in str(network.value)


def test_telegram_async_variant(engine, content):
    receipt = asyncio.run(telegram(engine, lambda request: ok(99)).apublish(content))
    assert receipt.success and receipt.platform_post_id == "99"


# -- LLM generator --------------------------------------------------------------------- #
def env(name, default=None):
    return default


def test_llm_model_resolution():
    assert resolve_model("anthropic", env) == ("anthropic", "claude-opus-5-5")
    assert resolve_model("gemini", env) == ("gemini", "gemini-3.5-flash-lite")
    assert resolve_model("claude-haiku-5-5", env) == ("anthropic", "claude-haiku-5-5")
    assert resolve_model("gemini:my-tuned-model", env) == ("gemini", "my-tuned-model")
    assert resolve_model("mock", env) == ("mock", OFFLINE_MODEL)
    assert resolve_model("gpt-9", env) == ("mock", OFFLINE_MODEL)  # unknown -> safe offline fallback


def test_llm_pricing_and_capability_gates():
    assert estimate_cost_usd("claude-opus-5-5", 1_000_000, 1_000_000) == 24.0
    assert estimate_cost_usd("claude-haiku-5-5", 1000, 1000) == pytest.approx(0.0006)
    assert price_for("gemini-2.5-flash-lite") == (0.10, 0.40) and price_for("gemini-2.5-flash") == (0.30, 2.50)
    assert price_for("claude-next-9") == (10.0, 50.0)  # unknown models are priced conservatively
    assert price_for("my-model", {"my-model": (1.0, 2.0)}) == (1.0, 2.0)
    assert _supports_effort("claude-opus-5-5") and _supports_effort("claude-fable-5-2")
    assert not _supports_effort("claude-haiku-4-5-20251001") and not _supports_effort("claude-3-5-sonnet-20241022")
    assert _supports_json_schema("claude-haiku-4-5-20251001") and not _supports_json_schema("claude-sonnet-4-20250514")


def test_llm_reply_parsing():
    assert parse_caption_json('```json\n{"caption": "Hi", "hashtags": ["#a", "b"]}\n```') == ("Hi", ["#a", "b"])
    assert parse_caption_json('Sure! {"caption": "Hi", "hashtags": []} Hope it helps.') == ("Hi", [])
    assert parse_caption_json("plain prose") is None and parse_caption_json('{"caption": ""}') is None


def test_missing_api_key_falls_back_to_offline_templates(engine, product):
    generator = LLMTextGenerator(PluginContext(settings=engine.settings, llm_model="anthropic"))
    hebrew = generator.generate(product, "he", None)
    english = generator.generate(product, "en", None)
    assert hebrew.model_used == OFFLINE_MODEL and hebrew.generation_cost_usd == 0
    assert "הזמנות" in hebrew.caption and "orders" in english.caption
    assert product.affiliate_url not in hebrew.caption  # the link is appended by publishers


class FakeMessages:
    def __init__(self, response):
        self.response, self.calls = response, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def fake_anthropic(text: str, stop_reason: str = "end_turn", model: str = "claude-opus-5-5"):
    response = SimpleNamespace(
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)] if text else [],
        usage=SimpleNamespace(input_tokens=200, output_tokens=120),
        model=model,
    )
    return SimpleNamespace(messages=FakeMessages(response), beta=SimpleNamespace(messages=FakeMessages(response)))


def paid_generator(make_settings, model: str, client, spend: float = 0.0) -> LLMTextGenerator:
    pytest.importorskip("anthropic")
    settings = make_settings(anthropic_api_key="sk-ant-test-0000000000000000")
    guard = BudgetGuard(lambda: spend, settings.max_monthly_ai_budget_usd)
    generator = LLMTextGenerator(PluginContext(settings=settings, llm_model=model, budget_guard=guard))
    generator._clients["anthropic"] = client
    return generator


def test_anthropic_request_shape_and_cost_accounting(make_settings, product):
    client = fake_anthropic('{"caption": "נעלי ריצה במחיר מעולה 🔥", "hashtags": ["ריצה", "#דילים"]}')
    content = paid_generator(make_settings, "anthropic", client).generate(product, "he", None)

    request = client.beta.messages.calls[0]  # Opus 5.5 goes through the refusal-fallback beta
    assert not client.messages.calls
    assert request["model"] == "claude-opus-5-5" and request["fallbacks"] == "default"
    assert request["betas"] == ["server-side-fallback-2026-07-01"]
    assert request["output_config"]["effort"] == "low" and request["output_config"]["format"]["type"] == "json_schema"
    assert "thinking" not in request and "temperature" not in request
    assert product.title in request["messages"][0]["content"] and "Hebrew" in request["messages"][0]["content"]

    assert content.caption == "נעלי ריצה במחיר מעולה 🔥" and content.hashtags == ["ריצה", "דילים"]
    assert (content.tokens_input, content.tokens_output) == (200, 120)
    assert content.generation_cost_usd == pytest.approx((200 * 4 + 120 * 20) / 1_000_000)


def test_models_without_server_side_fallback_use_the_plain_endpoint(make_settings, product):
    client = fake_anthropic('{"caption": "Deal!", "hashtags": []}', model="claude-haiku-5-5")
    content = paid_generator(make_settings, "claude-haiku-5-5", client).generate(product, "en", "Sell {title} in {language_name}")

    request = client.messages.calls[0]
    assert not client.beta.messages.calls and "fallbacks" not in request
    assert request["messages"][0]["content"].startswith(f"Sell {product.title} in English")
    assert content.hashtags  # empty model hashtags are replaced by sensible defaults
    assert content.generation_cost_usd == pytest.approx((200 * 0.10 + 120 * 0.50) / 1_000_000)


def test_exhausted_budget_blocks_the_llm_call_entirely(make_settings, product):
    client = fake_anthropic('{"caption": "x", "hashtags": []}')
    generator = paid_generator(make_settings, "anthropic", client, spend=5.0)
    with pytest.raises(BudgetExceededError):
        generator.generate(product, "he", None)
    assert not client.beta.messages.calls and not client.messages.calls


def test_refusal_is_checked_before_content_and_cost_is_still_recorded(make_settings, product):
    client = fake_anthropic("", stop_reason="refusal")
    content = paid_generator(make_settings, "anthropic", client).generate(product, "he", None)
    assert "הזמנות" in content.caption  # template text instead of the declined reply
    assert content.tokens_input == 200 and content.generation_cost_usd > 0
