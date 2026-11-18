"""The real Anthropic and Gemini SDK clients, driven against a local stand-in server.

Nothing leaves the machine and nothing is billed, but unlike a hand-written fake
this exercises the installed SDK versions: the request they actually build, the
response objects they return and the exceptions they raise. If an SDK upgrade
breaks the integration, these tests fail before a paid call does.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from core import bootstrap
from core.interfaces import PluginContext
from core.resilience import BudgetExceededError, BudgetGuard, NonRetryableError
from plugins.generators.llm_text_gen import LLMTextGenerator

# exc_type: a native dependency refused by the OS raises ImportError, not ModuleNotFoundError.
pytest.importorskip("anthropic", exc_type=ImportError)
pytest.importorskip("google.genai", exc_type=ImportError)

CLAUDE_REPLY = {
    "id": "msg_test", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
    "content": [
        {"type": "thinking", "thinking": "", "signature": "sig"},
        {"type": "text", "text": json.dumps({"caption": "נעלי ריצה במחיר מעולה 🔥", "hashtags": ["ריצה", "דילים"]}, ensure_ascii=False)},
    ],
    "stop_reason": "end_turn", "stop_sequence": None,
    "usage": {"input_tokens": 321, "output_tokens": 87},
}
GEMINI_REPLY = {
    "candidates": [{
        "content": {"role": "model", "parts": [{"text": json.dumps({"caption": "Great shoes, great price!", "hashtags": ["running"]})}]},
        "finishReason": "STOP",
    }],
    "usageMetadata": {"promptTokenCount": 210, "candidatesTokenCount": 40, "thoughtsTokenCount": 15, "totalTokenCount": 265},
}
OPUS_COST = round((321 * 4.00 + 87 * 20.00) / 1_000_000, 6)


def api_error(status: int, kind: str, message: str):
    return status, {"type": "error", "error": {"type": kind, "message": message}}


class StandInServer:
    """Answers every POST with the next scripted reply, or a success for the endpoint that was called."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict, dict]] = []
        self.script: list[tuple[int, dict]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
                outer.requests.append((self.path, {k.lower(): v for k, v in self.headers.items()}, body))
                default = GEMINI_REPLY if "generateContent" in self.path else CLAUDE_REPLY
                status, payload = outer.script.pop(0) if outer.script else (200, default)
                data = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def llm_server(monkeypatch):
    server = StandInServer()
    monkeypatch.setenv("ANTHROPIC_BASE_URL", server.url)
    monkeypatch.setenv("GOOGLE_GEMINI_BASE_URL", server.url)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    for proxy in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(proxy, raising=False)
    yield server
    server.close()


@pytest.fixture
def paid_settings(make_settings):
    return make_settings(
        anthropic_api_key="sk-ant-test-0000000000000000",
        gemini_api_key="AIzaTESTKEY0000000000000000000000000000",
    )


@pytest.fixture
def product(paid_settings):
    from plugins.sources.aliexpress_mock import AliExpressMockSource

    return AliExpressMockSource(PluginContext(settings=paid_settings)).fetch_products("sports", 4.5, 200, 1)[0]


def generator(settings, model: str, spent: float = 0.0) -> LLMTextGenerator:
    guard = BudgetGuard(lambda: spent, settings.max_monthly_ai_budget_usd)
    return LLMTextGenerator(PluginContext(settings=settings, llm_model=model, budget_guard=guard))


def test_claude_default_model_request_and_cost(llm_server, paid_settings, product):
    content = generator(paid_settings, "anthropic").generate(product, "he", None)

    path, headers, body = llm_server.requests[0]
    assert path.startswith("/v1/messages")
    assert headers["anthropic-beta"] == "server-side-fallback-2026-07-01"
    # The key travels as "Authorization: Bearer …" or "x-api-key", depending on the SDK version.
    assert "sk-ant-test" in headers.get("authorization", "") + headers.get("x-api-key", "")
    assert body["model"] == "claude-opus-5-5" and body["fallbacks"] == "default"
    assert body["output_config"]["effort"] == "low" and body["output_config"]["format"]["type"] == "json_schema"
    assert not {"temperature", "top_p", "top_k", "thinking"} & set(body)  # rejected or pointless on current models
    assert content.caption == "נעלי ריצה במחיר מעולה 🔥" and content.hashtags == ["ריצה", "דילים"]
    assert (content.tokens_input, content.tokens_output, content.generation_cost_usd) == (321, 87, OPUS_COST)


def test_claude_model_without_fallback_support_uses_the_plain_request(llm_server, paid_settings, product):
    llm_server.script.append((200, {**CLAUDE_REPLY, "model": "claude-haiku-5-5"}))
    content = generator(paid_settings, "claude-haiku-5-5").generate(product, "he", None)

    _, headers, body = llm_server.requests[0]
    assert "fallbacks" not in body and "anthropic-beta" not in headers
    assert content.model_used == "claude-haiku-5-5"
    assert content.generation_cost_usd == round((321 * 0.10 + 87 * 0.50) / 1_000_000, 6)


def test_claude_rate_limit_is_retried_and_bad_request_degrades(llm_server, paid_settings, product):
    llm_server.script += [api_error(429, "rate_limit_error", "slow down"), (200, CLAUDE_REPLY)]
    assert generator(paid_settings, "anthropic").generate(product, "he", None).generation_cost_usd == OPUS_COST
    assert len(llm_server.requests) == 2  # the SDK's own retries are off; @with_retry did this

    llm_server.requests.clear()
    llm_server.script += [api_error(400, "invalid_request_error", "output_config: unsupported"), (200, CLAUDE_REPLY)]
    generator(paid_settings, "anthropic").generate(product, "he", None)
    retried_body = llm_server.requests[1][2]
    assert "output_config" not in retried_body and "fallbacks" not in retried_body  # minimal request


def test_claude_auth_failure_is_not_retried_and_falls_back(llm_server, paid_settings, product):
    llm_server.script.append(api_error(401, "authentication_error", "Invalid bearer token"))
    writer = generator(paid_settings, "claude-haiku-5-5")
    first = writer.generate(product, "he", None)
    second = writer.generate(product, "he", None)

    assert len(llm_server.requests) == 1  # neither retried nor asked again for the next product
    assert first.model_used == second.model_used == "offline-template"  # no free key in the tests
    assert writer.notices and "ANTHROPIC_API_KEY was rejected (Anthropic: HTTP 401, Invalid bearer token)" \
        in writer.notices[0]


def test_claude_refusal_is_read_from_stop_reason(llm_server, paid_settings, product):
    refusal = {**CLAUDE_REPLY, "content": [], "stop_reason": "refusal",
               "stop_details": {"type": "refusal", "category": None, "explanation": None}}
    llm_server.script.append((200, refusal))
    content = generator(paid_settings, "anthropic").generate(product, "he", None)
    assert "הזמנות" in content.caption and content.generation_cost_usd == OPUS_COST  # template text, spend kept


def test_gemini_request_and_cost_including_thinking_tokens(llm_server, paid_settings, product):
    content = generator(paid_settings, "gemini").generate(product, "en", None)

    path, headers, body = llm_server.requests[0]
    assert path.endswith("/models/gemini-3.5-flash-lite:generateContent") and "x-goog-api-key" in headers
    assert body["generationConfig"]["responseMimeType"] == "application/json" and "systemInstruction" in body
    assert content.caption == "Great shoes, great price!" and content.hashtags == ["running"]
    assert (content.tokens_input, content.tokens_output) == (210, 55)  # 40 answer + 15 thinking
    assert content.generation_cost_usd == round((210 * 0.30 + 55 * 2.50) / 1_000_000, 6)


def test_gemini_quota_error_is_retried_and_bad_request_is_not(llm_server, paid_settings, product):
    llm_server.script += [(429, {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}}), (200, GEMINI_REPLY)]
    generator(paid_settings, "gemini").generate(product, "en", None)
    assert len(llm_server.requests) == 2

    llm_server.requests.clear()
    llm_server.script.append((400, {"error": {"code": 400, "message": "model not found", "status": "INVALID_ARGUMENT"}}))
    with pytest.raises(NonRetryableError, match="HTTP 400"):
        generator(paid_settings, "gemini").generate(product, "en", None)
    assert len(llm_server.requests) == 1


def test_paid_pipeline_end_to_end_records_spend_and_respects_the_budget(llm_server, paid_settings):
    """Harvest -> real SDK -> store -> publish, with the spend landing in the ledger."""
    from core.config import ContentProfile, SocialAccount

    engine = bootstrap(settings=paid_settings, console_logging=False)
    engine.harvester.harvest("sports", "aliexpress_mock")
    engine.registry.save_content_profile(ContentProfile(
        content_type="console_post", platform="console", generator="llm_text_gen", llm_model="anthropic",
    ))
    engine.registry.save_social_account(SocialAccount(
        category_id="sports", language="he", platform="console", account_handle="default",
    ))

    def run(**kwargs):
        return engine.orchestrator.run("sports", "he", ["console_post"], **kwargs)

    report = run(mode="immediate", limit=2)

    assert report.ok and report.count("published") == 2
    assert report.cost_usd == pytest.approx(2 * OPUS_COST)
    assert engine.db.get_monthly_ai_spend() == pytest.approx(2 * OPUS_COST)
    rows = engine.db.list_publications(statuses=["PUBLISHED"])
    assert {row["model_used"] for row in rows} == {"claude-opus-5-5"} and rows[0]["caption"] == "נעלי ריצה במחיר מעולה 🔥"

    # Dry-run must never reach the provider, even with a key configured.
    llm_server.requests.clear()
    assert run(dry_run=True, force=True).count("preview") == 3
    assert llm_server.requests == []

    # A cap below what is already spent halts before the next request is sent.
    engine.budget.max_monthly_usd = OPUS_COST
    with pytest.raises(BudgetExceededError):
        run(force=True)
    assert llm_server.requests == []


def test_each_content_type_calls_its_own_model_and_each_language_its_own_account(llm_server, paid_settings, monkeypatch):
    """The end-to-end routing check, live through the real SDK: harvest once, publish he + en."""
    for name, value in {
        "INSTAGRAM_ACCESS_TOKEN": "EAAGm0PX4ZCpsBAhe0123456789ZD", "INSTAGRAM_BUSINESS_ACCOUNT_ID": "17841400000000001",
        "INSTAGRAM_ACCESS_TOKEN_GADGETS_EN": "EAAGm0PX4ZCpsBAen0123456789ZD",
        "INSTAGRAM_BUSINESS_ACCOUNT_ID_GADGETS_EN": "17841400000000002",
    }.items():
        monkeypatch.setenv(name, value)
    engine = bootstrap(settings=paid_settings, console_logging=False)

    [harvest] = engine.harvester.harvest("gadgets", "temu_mock")
    assert len(harvest.inserted) == 3
    for language in ("he", "en"):  # queued: content is generated for real, nothing is posted
        report = engine.orchestrator.run(
            "gadgets", language, ["instagram_post", "instagram_story"], mode="queued", limit=1,
        )
        assert report.ok and report.count("queued") == 2

    # One harvested row per product, shared by both languages.
    assert engine.db.query_one("SELECT COUNT(*) AS n FROM products WHERE category = 'gadgets'")["n"] == 3
    # Four generations, each sent to the model its content profile routes to.
    requested = [body["model"] for path, _, body in llm_server.requests if path.endswith("/messages")]
    assert sorted(requested) == ["claude-haiku-5-5", "claude-haiku-5-5", "claude-sonnet-5-5", "claude-sonnet-5-5"]
    rows = engine.db.query(
        "SELECT language, content_format, target_page FROM publications ORDER BY language, content_format"
    )
    assert [tuple(row.values()) for row in rows] == [
        ("en", "instagram_post", "gadgets_en"), ("en", "instagram_story", "gadgets_en"),
        ("he", "instagram_post", "gadgets"), ("he", "instagram_story", "gadgets"),
    ]
