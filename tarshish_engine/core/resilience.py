"""Cross-cutting resilience primitives shared by the whole engine.

* the engine's exception hierarchy
* a global logger whose formatter redacts secrets before anything is written
* ``with_retry`` — exponential-backoff decorator for every external API call
* ``BudgetGuard`` — the monthly AI spend guardrail

This module is a dependency leaf: it imports nothing else from ``core`` so that
every other module (and every plugin) can import it freely.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import random
import re
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Mapping

LOGGER_NAME = "tarshish"
REDACTED = "[REDACTED]"


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #
class TarshishError(Exception):
    """Base class for every error raised by the engine."""


class ConfigError(TarshishError):
    """Invalid environment variables or pipeline configuration (halts startup)."""


class PluginError(TarshishError):
    """A plugin is missing, failed to load, or violates its contract."""


class PipelineError(TarshishError):
    """A pipeline cannot be found or executed."""


class NonRetryableError(TarshishError):
    """An external call failed in a way that retrying cannot fix (4xx, refusal…)."""


class BudgetExceededError(TarshishError):
    """The monthly AI budget is exhausted — LLM calls must stop."""

    def __init__(self, spent_usd: float, limit_usd: float) -> None:
        self.spent_usd = spent_usd
        self.limit_usd = limit_usd
        super().__init__(
            f"Monthly AI budget exhausted: ${spent_usd:.4f} spent of ${limit_usd:.2f} allowed "
            "(MAX_MONTHLY_AI_BUDGET_USD). LLM calls are halted until the cap is raised or the month rolls over."
        )


# --------------------------------------------------------------------------- #
# Secret redaction
# --------------------------------------------------------------------------- #
# Token-shaped values. Used both for log redaction and to reject raw secrets
# that someone tries to put into pipelines.yaml / the pipelines table.
_TOKEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),            # Anthropic API keys
    re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"),             # generic "sk-" provider keys
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),              # Google / Gemini API keys
    re.compile(r"\d{6,12}:[A-Za-z0-9_\-]{30,}"),         # Telegram bot tokens
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),        # AWS access key ids
)

# Context-shaped values: the secret is recognised by what surrounds it.
_CONTEXT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/\-]{8,}=*"), r"\1 " + REDACTED),
    (re.compile(r"(://[^/\s:@]+:)[^@\s/]+(@)"), r"\1" + REDACTED + r"\2"),  # user:pass@host
    (
        # KEY=value / "token": "value" — the key must END with a sensitive word, so
        # names such as ``token_env_var`` or ``tokens_input`` are left untouched.
        re.compile(
            r"""(?ix)
            ((?:api[_-]?key|token|secret|password|passwd|authorization)["']?\s*[=:]\s*["']?)
            (?!\[REDACTED\])
            (?=[^\s"',;&]*\d)[^\s"',;&]{8,}
            """
        ),
        r"\1" + REDACTED,
    ),
)

_SENSITIVE_NAME_PARTS = {"KEY", "APIKEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIALS"}
_registered_secrets: set[str] = set()


def register_secret(value: str | None) -> None:
    """Remember an exact secret value so it is masked wherever it appears."""
    if value and len(value) >= 6:
        _registered_secrets.add(value)


def is_sensitive_name(name: str) -> bool:
    """True when an env-var / credential name denotes a secret (``*_TOKEN``, ``*_API_KEY``…)."""
    return any(part in _SENSITIVE_NAME_PARTS for part in re.split(r"[^A-Za-z0-9]+", name.upper()))


def register_env_secrets(environ: Mapping[str, str]) -> None:
    """Register the value of every secret-looking environment variable."""
    for name, value in environ.items():
        if is_sensitive_name(name):
            register_secret(value)


def looks_like_secret(value: str) -> bool:
    """True when ``value`` is shaped like a real credential (not just a variable name)."""
    return any(pattern.search(value) for pattern in _TOKEN_PATTERNS)


def redact(text: str) -> str:
    """Mask every known or secret-shaped value in ``text`` with ``[REDACTED]``."""
    if not text:
        return text
    for secret in sorted(_registered_secrets, key=len, reverse=True):
        if secret in text:
            text = text.replace(secret, REDACTED)
    for pattern in _TOKEN_PATTERNS:
        text = pattern.sub(REDACTED, text)
    for pattern, replacement in _CONTEXT_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFormatter(logging.Formatter):
    """Formatter that redacts the *final* line — message, args and tracebacks alike."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a child of the global engine logger (``tarshish.<name>``)."""
    return logging.getLogger(f"{LOGGER_NAME}.{name}" if name else LOGGER_NAME)


def setup_logging(level: str = "INFO", log_file: Path | str | None = None, *, console: bool = True) -> logging.Logger:
    """Configure the global logger. Safe to call repeatedly (handlers are replaced).

    Handlers are attached to the *root* logger so that third-party libraries
    (HTTP clients love to log request URLs, which may embed bot tokens) are
    redacted by the same formatter before reaching the console or the disk.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_tarshish_handler", False):
            root.removeHandler(handler)
            handler.close()

    handlers: list[logging.Handler] = []
    if console:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(RedactingFormatter("%(asctime)s %(levelname)-8s %(message)s", "%H:%M:%S"))
        handlers.append(stream)
    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        file_handler.setFormatter(
            RedactingFormatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s", "%Y-%m-%d %H:%M:%S")
        )
        handlers.append(file_handler)

    for handler in handlers:
        handler._tarshish_handler = True  # type: ignore[attr-defined]
        root.addHandler(handler)

    root.setLevel(logging.WARNING)  # third-party libraries: warnings and above only
    engine_logger = logging.getLogger(LOGGER_NAME)
    engine_logger.setLevel(level.upper())
    return engine_logger


logger = get_logger("resilience")


# --------------------------------------------------------------------------- #
# Exponential backoff
# --------------------------------------------------------------------------- #
_NEVER_RETRY: tuple[type[BaseException], ...] = (NonRetryableError, BudgetExceededError, ConfigError)
_MAX_SERVER_REQUESTED_DELAY = 300.0


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


async def _async_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


def _backoff_delay(attempt: int, base_delay: float, max_delay: float, exc: BaseException) -> float:
    """Delay before retry number ``attempt`` (1-based): base * 2^(n-1), ±25% jitter."""
    delay = min(max_delay, base_delay * (2 ** (attempt - 1))) * random.uniform(0.75, 1.25)
    retry_after = getattr(exc, "retry_after", None)  # e.g. HTTP 429 "Retry-After"
    if isinstance(retry_after, (int, float)) and retry_after > 0:
        delay = max(delay, min(float(retry_after), _MAX_SERVER_REQUESTED_DELAY))
    return delay


def with_retry(
    max_attempts: int = 3,
    base_delay: float = 2.0,
    *,
    max_delay: float = 60.0,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Retry the decorated callable with exponential backoff.

    Works on plain functions and coroutines. ``NonRetryableError``,
    ``BudgetExceededError`` and ``ConfigError`` are never retried; an exception
    carrying a numeric ``retry_after`` attribute stretches the delay to honour it.
    After the last attempt the original exception propagates unchanged.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        label = getattr(func, "__qualname__", repr(func))

        def should_retry(exc: BaseException, attempt: int) -> bool:
            return attempt < max_attempts and isinstance(exc, retry_on) and not isinstance(exc, _NEVER_RETRY)

        def announce(exc: BaseException, attempt: int, delay: float) -> None:
            logger.warning(
                "%s failed (attempt %d/%d): %s: %s — retrying in %.1fs",
                label, attempt, max_attempts, type(exc).__name__, exc, delay,
            )

        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                attempt = 1
                while True:
                    try:
                        return await func(*args, **kwargs)
                    except Exception as exc:
                        if not should_retry(exc, attempt):
                            raise
                        delay = _backoff_delay(attempt, base_delay, max_delay, exc)
                        announce(exc, attempt, delay)
                        await _async_sleep(delay)
                        attempt += 1

            return async_wrapper

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            attempt = 1
            while True:
                try:
                    return func(*args, **kwargs)
                except Exception as exc:
                    if not should_retry(exc, attempt):
                        raise
                    delay = _backoff_delay(attempt, base_delay, max_delay, exc)
                    announce(exc, attempt, delay)
                    _sleep(delay)
                    attempt += 1

        return wrapper

    return decorator


# --------------------------------------------------------------------------- #
# AI budget guardrail
# --------------------------------------------------------------------------- #
class BudgetGuard:
    """Blocks paid LLM calls once the calendar month's AI spend reaches the cap.

    ``spend_provider`` returns the month-to-date sum of ``generation_cost_usd``
    (the engine passes ``Database.get_monthly_ai_spend``). Generators call
    :meth:`ensure_within_budget` immediately before every paid request.
    """

    def __init__(self, spend_provider: Callable[[], float], max_monthly_usd: float) -> None:
        self._spend_provider = spend_provider
        self.max_monthly_usd = float(max_monthly_usd)

    def current_spend(self) -> float:
        return float(self._spend_provider())

    def remaining(self) -> float:
        return max(0.0, self.max_monthly_usd - self.current_spend())

    def is_exceeded(self) -> bool:
        return self.current_spend() >= self.max_monthly_usd

    def ensure_within_budget(self) -> None:
        spent = self.current_spend()
        if spent >= self.max_monthly_usd:
            logger.critical(
                "AI BUDGET ALERT: month-to-date AI spend $%.4f has reached the cap of $%.2f — halting LLM calls.",
                spent, self.max_monthly_usd,
            )
            raise BudgetExceededError(spent, self.max_monthly_usd)
