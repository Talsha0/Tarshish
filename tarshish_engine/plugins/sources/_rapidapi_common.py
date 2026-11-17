"""Provider-agnostic tools shared by the RapidAPI provider chains (Temu, AliExpress).

Pure functions only — no HTTP, no store-specific URLs. The leading underscore keeps
the plugin registry from scanning this module for plugins.
"""

from __future__ import annotations

import re
from string import Formatter
from typing import Any, Callable, Mapping, Sequence

from pydantic import ValidationError

from core.resilience import ConfigError


def config_error(title: str, exc: ValidationError, rename: Callable[[str], str] = str) -> ConfigError:
    """A ``ValidationError`` as a readable ``ConfigError`` (one line per field)."""
    lines = [
        f"  - {rename('.'.join(str(part) for part in err['loc']))}: {err['msg'].removeprefix('Value error, ')}"
        for err in exc.errors()
    ]
    return ConfigError(f"{title}:\n" + "\n".join(lines))


def dig(value: Any, path: str) -> Any:
    """Follow a dotted path through dicts and lists (``data.products``, ``images.0.url``)."""
    for part in path.split("."):
        if isinstance(value, Mapping):
            value = value.get(part)
        elif isinstance(value, list) and part.lstrip("-").isdigit():
            index = int(part)
            value = value[index] if -len(value) <= index < len(value) else None
        else:
            return None
        if value is None:
            return None
    return value


def first(value: Any, paths: Sequence[str]) -> Any:
    """The first non-empty value found at any of the paths."""
    for path in paths:
        found = dig(value, path)
        if found not in (None, "", [], {}):
            return found
    return None


def number(value: Any) -> float | None:
    """``1.99``, ``"$1.99"``, ``"11,779 sold"`` and ``"10K+"`` as a number; ``None`` when there is none."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"(\d+(?:\.\d+)?)(?:\s*([km])(?![a-z]))?", str(value).replace(",", ""), re.I)
    if not match:
        return None
    return float(match[1]) * {"k": 1_000, "m": 1_000_000}.get((match[2] or "").lower(), 1)


def image_urls(item: Any, paths: Sequence[str]) -> list[str]:
    """Every http(s) image URL found at the paths (strings, lists, or ``{"url": ...}`` objects)."""
    urls: list[str] = []
    for path in paths:
        value = dig(item, path)
        for candidate in value if isinstance(value, list) else [value]:
            if isinstance(candidate, Mapping):
                candidate = candidate.get("url") or candidate.get("src")
            if not isinstance(candidate, str):
                continue
            url = candidate.strip()
            url = f"https:{url}" if url.startswith("//") else url
            if re.match(r"https?://\S+$", url) and url not in urls:
                urls.append(url)
    return urls



def render_params(template: Mapping[str, Any], values: Mapping[str, Any]) -> dict[str, Any]:
    """Fill a parameter template; a parameter whose placeholder has no value is left out."""
    params: dict[str, Any] = {}
    for name, spec in template.items():
        if not isinstance(spec, str):
            params[name] = spec
            continue
        used = [field for _, field, _, _ in Formatter().parse(spec) if field]
        if any(values.get(field) is None for field in used):
            continue
        params[name] = spec.format(**{
            field: f"{values[field]:g}" if isinstance(values[field], float) else str(values[field]) for field in used
        })
    return params


