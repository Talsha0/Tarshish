import asyncio
import logging

import pytest

from core import resilience
from core.resilience import (
    BudgetExceededError,
    BudgetGuard,
    NonRetryableError,
    RedactingFormatter,
    redact,
    register_secret,
    with_retry,
)

TELEGRAM_TOKEN = "7123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw1"
ANTHROPIC_KEY = "sk-ant-api03-AbCdEf0123456789_xyzXYZ-0123456789"


@pytest.mark.parametrize(
    "text",
    [
        f"POST https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        f"using key {ANTHROPIC_KEY} for the request",
        "google key AIzaSyA1234567890abcdefghijklmnopqrstuvw",
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature",
        "TG_SPORTS_BOT_TOKEN=abcd1234efgh5678",
        '{"api_key": "9f8e7d6c5b4a3210"}',
        "https://user:hunter2pass@example.com/path",
    ],
)
def test_secret_shapes_are_redacted(text):
    cleaned = redact(text)
    assert "[REDACTED]" in cleaned
    for secret in (TELEGRAM_TOKEN, ANTHROPIC_KEY, "AIzaSyA1234567890", "eyJhbGciOiJIUzI1NiJ9", "abcd1234efgh5678",
                   "9f8e7d6c5b4a3210", "hunter2pass"):
        assert secret not in cleaned


def test_env_var_names_and_counters_are_not_redacted():
    text = "token_env_var=TG_SPORTS_BOT_TOKEN is not set; tokens_input=1234567890 tokens"
    assert redact(text) == text


def test_registered_secret_is_masked_everywhere():
    register_secret("plain-looking-value")
    assert redact("sent plain-looking-value to the API") == "sent [REDACTED] to the API"


def test_formatter_redacts_message_args_and_traceback():
    formatter = RedactingFormatter("%(message)s")
    try:
        raise RuntimeError(f"boom {TELEGRAM_TOKEN}")
    except RuntimeError:
        import sys

        record = logging.LogRecord("t", logging.ERROR, __file__, 1, "failed with %s", (ANTHROPIC_KEY,), sys.exc_info())
    output = formatter.format(record)
    assert TELEGRAM_TOKEN not in output and ANTHROPIC_KEY not in output
    assert output.count("[REDACTED]") >= 2


def test_retry_backs_off_exponentially_then_succeeds(clean_environment):
    calls = []

    @with_retry(max_attempts=3, base_delay=2.0)
    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise ConnectionError("temporary")
        return "ok"

    assert flaky() == "ok"
    assert len(calls) == 3
    first, second = clean_environment
    assert 1.5 <= first <= 2.5 and 3.0 <= second <= 5.0  # 2s then 4s, ±25% jitter


def test_retry_gives_up_after_max_attempts_and_reraises():
    calls = []

    @with_retry(max_attempts=3, base_delay=2.0)
    def always_down():
        calls.append(1)
        raise TimeoutError("still down")

    with pytest.raises(TimeoutError):
        always_down()
    assert len(calls) == 3


def test_non_retryable_and_budget_errors_are_not_retried():
    calls = []

    @with_retry(max_attempts=3, base_delay=2.0)
    def rejected():
        calls.append(1)
        raise NonRetryableError("HTTP 400")

    with pytest.raises(NonRetryableError):
        rejected()
    assert len(calls) == 1

    @with_retry()
    def over_budget():
        calls.append(1)
        raise BudgetExceededError(5.0, 5.0)

    with pytest.raises(BudgetExceededError):
        over_budget()
    assert len(calls) == 2


def test_retry_honours_retry_after(clean_environment):
    class RateLimited(Exception):
        retry_after = 17

    attempts = []

    @with_retry(max_attempts=2, base_delay=2.0)
    def limited():
        attempts.append(1)
        if len(attempts) == 1:
            raise RateLimited()
        return True

    assert limited() is True
    assert clean_environment == [17.0]


def test_retry_supports_coroutines(monkeypatch):
    delays = []

    async def fake_sleep(seconds):
        delays.append(seconds)

    monkeypatch.setattr(resilience, "_async_sleep", fake_sleep)
    calls = []

    @with_retry(max_attempts=3, base_delay=1.0)
    async def flaky():
        calls.append(1)
        if len(calls) < 2:
            raise ConnectionError("temporary")
        return "ok"

    assert asyncio.run(flaky()) == "ok"
    assert len(calls) == 2 and len(delays) == 1


def test_budget_guard_trips_at_the_cap():
    spend = {"value": 4.99}
    guard = BudgetGuard(lambda: spend["value"], max_monthly_usd=5.0)
    guard.ensure_within_budget()
    assert guard.remaining() == pytest.approx(0.01)

    spend["value"] = 5.0  # ">= cap" halts
    with pytest.raises(BudgetExceededError) as excinfo:
        guard.ensure_within_budget()
    assert excinfo.value.spent_usd == 5.0 and excinfo.value.limit_usd == 5.0
