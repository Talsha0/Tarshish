"""Free-tier LLM failover chain: Google Gemini -> Groq -> OpenRouter.

Live FindHub copy is written by whichever free model still has quota. The chain is walked in
priority order and a link is skipped, for the rest of the process or until its cooldown ends, as
soon as it answers with a rate-limit / quota error::

    1. Google Gemini   gemini-2.5-flash, then gemini-2.0-flash      GEMINI_API_KEY
    2. Groq            llama-3.3-70b-versatile                       GROQ_API_KEY
    3. OpenRouter      meta-llama/llama-3.3-70b-instruct:free        OPENROUTER_API_KEY

Failover rules
--------------
* **HTTP 429**, **402**, ``RESOURCE_EXHAUSTED`` or a quota message (also an error envelope inside a
  200 reply, which OpenRouter sends) -> the model cools down (``Retry-After`` / Gemini's
  ``retryDelay`` when given, longer for daily quotas) and the next link is tried at once.
* **401 / 403** (bad or revoked key) -> every model of that provider is skipped for an hour.
* **5xx, timeouts, 400 / 404** (overloaded, unknown model) and replies the caller's ``validate``
  rejects -> the next link is tried; no cooldown, the model gets another chance next call.
* A provider without a key is skipped silently. When no link can answer,
  :class:`FreeLLMChainExhausted` lists what happened to each one, so the caller can fall back
  to its offline templates.

Calls use plain HTTPS (``httpx``), not provider SDKs. Keys travel in headers only and every key
is registered with the log redactor, so error text prints them as ``[REDACTED]``. Free tiers cost
nothing: results report token counts for the AI ledger but no price.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

import httpx

from core.resilience import TarshishError, get_logger, redact, register_secret

logger = get_logger("plugins.free_llm_chain")

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


@dataclass(frozen=True)
class FreeModel:
    provider: str       # gemini | groq | openrouter
    model: str

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.model}"


# Priority order. Change it here (or pass ``chain=`` to FreeLLMChain) — nothing else names a model.
FREE_CHAIN: tuple[FreeModel, ...] = (
    FreeModel("gemini", "gemini-2.5-flash"),
    FreeModel("gemini", "gemini-2.0-flash"),
    FreeModel("groq", "llama-3.3-70b-versatile"),
    FreeModel("openrouter", "meta-llama/llama-3.3-70b-instruct:free"),
)
# provider -> (Settings attribute, env var, display name)
PROVIDERS: dict[str, tuple[str, str, str]] = {
    "gemini": ("gemini_api_key", "GEMINI_API_KEY", "Google Gemini (free tier)"),
    "groq": ("groq_api_key", "GROQ_API_KEY", "Groq (free tier)"),
    "openrouter": ("openrouter_api_key", "OPENROUTER_API_KEY", "OpenRouter (free models)"),
}

DEFAULT_QUOTA_COOLDOWN = 15 * 60.0      # 429 without a hint
DAILY_QUOTA_COOLDOWN = 6 * 3600.0       # "per day" quotas reset at an unknown hour: stay away for a while
AUTH_COOLDOWN = 3600.0                  # rejected key
MAX_COOLDOWN = 24 * 3600.0
_QUOTA_WORDS = re.compile(r"quota|rate.?limit|resource.?exhausted|too many requests|free-models-per-day", re.I)
_DAILY_WORDS = re.compile(r"per.?day|daily|PerDay|free-models-per-day", re.I)


class FreeLLMError(TarshishError):
    """One link of the chain could not answer (the chain moves on)."""


class QuotaExhausted(FreeLLMError):
    def __init__(self, message: str, cooldown: float) -> None:
        super().__init__(message)
        self.cooldown = cooldown


class ProviderAuthError(FreeLLMError):
    """The provider rejected the key."""


class FreeLLMChainExhausted(TarshishError):
    """No free model could answer; ``attempts`` says why for each link."""

    def __init__(self, attempts: Sequence[str]) -> None:
        detail = "; ".join(attempts) if attempts else "no free LLM key is configured (GEMINI_API_KEY, GROQ_API_KEY, OPENROUTER_API_KEY)"
        super().__init__(f"Every free LLM is unavailable: {detail}")
        self.attempts = list(attempts)


@dataclass
class LLMResult:
    text: str
    provider: str
    model: str
    tokens_input: int = 0
    tokens_output: int = 0
    attempts: list[str] = field(default_factory=list)    # links skipped before this one answered, and why

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.model}"


# Cooldowns are shared by every chain of the process (a batch, the next batch, the admin API), so a
# provider that ran out of quota is not hammered again by each new generator instance.
_cooldowns: dict[tuple[str, str], tuple[float, str]] = {}
_cooldown_lock = threading.Lock()


def reset_cooldowns() -> None:
    """Forget every cooldown (tests, or after topping up a quota)."""
    with _cooldown_lock:
        _cooldowns.clear()


def _retry_hint(response: httpx.Response, body: Any) -> float | None:
    """Seconds suggested by ``Retry-After`` or Gemini's ``RetryInfo.retryDelay`` ("37s")."""
    header = response.headers.get("retry-after")
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    details = ((body or {}).get("error") or {}).get("details") if isinstance(body, dict) else None
    for item in details or []:
        delay = isinstance(item, dict) and item.get("retryDelay")
        if isinstance(delay, str):
            match = re.fullmatch(r"(\d+(?:\.\d+)?)s", delay.strip())
            if match:
                return float(match.group(1))
    return None


def _error_text(body: Any, response: httpx.Response) -> str:
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("status") or error.get("code") or "")
        if isinstance(error, str):
            return error
    return response.text[:300] if response.text else f"HTTP {response.status_code}"


class FreeLLMChain:
    """Ask the first free model that still has quota; fail over on 429 / quota errors."""

    def __init__(
        self,
        settings: Any,
        *,
        chain: Sequence[FreeModel] = FREE_CHAIN,
        transport: Any = None,
        clock: Callable[[], float] = time.monotonic,
        timeout: float = 45.0,
    ) -> None:
        self.settings = settings
        self.chain = tuple(chain)
        self._transport = transport
        self._clock = clock
        self._timeout = timeout
        self._keys: dict[str, str] = {}
        for provider, (attr, env, _) in PROVIDERS.items():
            secret = getattr(settings, attr, None)
            value = secret.get_secret_value().strip() if secret is not None else (settings.get_env(env) or "")
            if value:
                register_secret(value)
                self._keys[provider] = value

    # -- introspection ---------------------------------------------------------------- #
    def configured(self) -> bool:
        return any(link.provider in self._keys for link in self.chain)

    def status(self) -> list[dict[str, Any]]:
        """Per link: key configured, cooling down (and why). Never includes a key."""
        now = self._clock()
        rows = []
        for link in self.chain:
            until, reason = _cooldowns.get((link.provider, link.model), (0.0, ""))
            rows.append({
                "provider": link.provider, "model": link.model, "label": PROVIDERS[link.provider][2],
                "env_var": PROVIDERS[link.provider][1], "configured": link.provider in self._keys,
                "cooling_down_seconds": max(0, round(until - now)) if until > now else 0,
                "last_problem": reason or None,
            })
        return rows

    # -- the call ---------------------------------------------------------------------- #
    def complete(
        self,
        system: str,
        prompt: str,
        *,
        validate: Callable[[str], bool] | None = None,
        max_tokens: int = 1200,
        temperature: float = 0.8,
    ) -> LLMResult:
        """Return the first acceptable reply of the chain. Raises :class:`FreeLLMChainExhausted`."""
        attempts: list[str] = []
        with httpx.Client(timeout=httpx.Timeout(self._timeout, connect=10.0), transport=self._transport) as client:
            for index, link in enumerate(self.chain):
                key = self._keys.get(link.provider)
                if not key:
                    continue
                cooling = self._cooling(link)
                if cooling:
                    attempts.append(f"{link.label} skipped ({cooling})")
                    continue
                try:
                    result = self._ask(client, link, key, system, prompt, max_tokens, temperature)
                except QuotaExhausted as exc:
                    self._cool(link, exc.cooldown, "quota exhausted (HTTP 429)")
                    attempts.append(f"{link.label}: {exc}")
                    self._log_failover(link, index, str(exc))
                    continue
                except ProviderAuthError as exc:
                    for other in self.chain:
                        if other.provider == link.provider:
                            self._cool(other, AUTH_COOLDOWN, "key rejected")
                    attempts.append(f"{link.label}: {exc}")
                    self._log_failover(link, index, str(exc))
                    continue
                except FreeLLMError as exc:
                    attempts.append(f"{link.label}: {exc}")
                    self._log_failover(link, index, str(exc))
                    continue
                if not result.text.strip() or (validate is not None and not validate(result.text)):
                    attempts.append(f"{link.label}: reply did not follow the output contract")
                    self._log_failover(link, index, "reply did not follow the output contract")
                    continue
                result.attempts = attempts
                if attempts:
                    logger.info("Free LLM chain answered with %s after: %s", result.label, "; ".join(attempts))
                return result
        raise FreeLLMChainExhausted(attempts)

    # -- cooldown bookkeeping ------------------------------------------------------------- #
    def _cooling(self, link: FreeModel) -> str | None:
        with _cooldown_lock:
            entry = _cooldowns.get((link.provider, link.model))
            if entry is None:
                return None
            until, reason = entry
            if until <= self._clock():
                del _cooldowns[(link.provider, link.model)]
                return None
            return f"{reason}, cooling down {round(until - self._clock())} s"

    def _cool(self, link: FreeModel, seconds: float, reason: str) -> None:
        with _cooldown_lock:
            _cooldowns[(link.provider, link.model)] = (self._clock() + min(max(seconds, 1.0), MAX_COOLDOWN), reason)

    def _log_failover(self, link: FreeModel, index: int, reason: str) -> None:
        following = next((other.label for other in self.chain[index + 1:] if other.provider in self._keys), None)
        logger.warning("Free LLM %s unavailable (%s) — %s.", link.label, redact(reason),
                       f"failing over to {following}" if following else "no further free model to try")

    # -- providers ---------------------------------------------------------------------------- #
    def _ask(
        self, client: httpx.Client, link: FreeModel, key: str, system: str, prompt: str, max_tokens: int,
        temperature: float,
    ) -> LLMResult:
        if link.provider == "gemini":
            config: dict[str, Any] = {"responseMimeType": "application/json", "maxOutputTokens": max_tokens,
                                      "temperature": temperature}
            if link.model.startswith("gemini-2.5"):
                config["thinkingConfig"] = {"thinkingBudget": 0}   # thinking would eat the free output budget
            body = {
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": config,
            }
            data = self._post(client, link, GEMINI_URL.format(model=link.model), {"x-goog-api-key": key}, body)
            candidates = data.get("candidates") or []
            parts = ((candidates[0].get("content") or {}).get("parts") or []) if candidates else []
            usage = data.get("usageMetadata") or {}
            return LLMResult(
                text="".join(str(part.get("text") or "") for part in parts if isinstance(part, dict)),
                provider=link.provider, model=link.model,
                tokens_input=int(usage.get("promptTokenCount") or 0),
                tokens_output=int(usage.get("candidatesTokenCount") or 0) + int(usage.get("thoughtsTokenCount") or 0),
            )

        url = GROQ_URL if link.provider == "groq" else OPENROUTER_URL
        body = {
            "model": link.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        headers = {"Authorization": f"Bearer {key}"}
        if link.provider == "groq":
            body["response_format"] = {"type": "json_object"}
        else:
            headers["X-Title"] = "FindHub"
        data = self._post(client, link, url, headers, body)
        choices = data.get("choices") or []
        message = (choices[0].get("message") or {}) if choices else {}
        usage = data.get("usage") or {}
        return LLMResult(
            text=str(message.get("content") or ""), provider=link.provider, model=link.model,
            tokens_input=int(usage.get("prompt_tokens") or 0), tokens_output=int(usage.get("completion_tokens") or 0),
        )

    def _post(self, client: httpx.Client, link: FreeModel, url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        try:
            response = client.post(url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise FreeLLMError(redact(f"{type(exc).__name__}: {exc}")) from None
        try:
            data = response.json()
        except ValueError:
            data = None
        status = response.status_code
        message = redact(_error_text(data, response))
        # OpenRouter may wrap a provider's 429 in a 200 reply.
        envelope = data.get("error") if isinstance(data, dict) else None
        if status < 300 and isinstance(envelope, dict):
            status = int(envelope.get("code") or 502) if str(envelope.get("code") or "").isdigit() else 502
        if status < 300 and isinstance(data, dict):
            return data
        if status in (429, 402) or (status in (403, 400) and _QUOTA_WORDS.search(message) and "key" not in message.lower()):
            hint = _retry_hint(response, data)
            cooldown = hint if hint is not None else (
                DAILY_QUOTA_COOLDOWN if _DAILY_WORDS.search(message) else DEFAULT_QUOTA_COOLDOWN)
            raise QuotaExhausted(f"HTTP {status} quota/rate limit: {message[:200]}", cooldown)
        if status in (401, 403) or (status == 400 and re.search(r"api.?key", message, re.I)):
            raise ProviderAuthError(f"HTTP {status} key rejected: {message[:200]}")
        raise FreeLLMError(f"HTTP {status}: {message[:200]}")
