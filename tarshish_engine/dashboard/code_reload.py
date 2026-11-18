"""Pick up a deploy's new engine code in a Streamlit process that is already running.

Streamlit reloads a changed module only when it sits in the script's folder (``dashboard/``). After a
deploy pulls new code (Streamlit Community Cloud does it without restarting the process) ``core/``,
``plugins/`` and ``services/`` would stay the old modules in memory, and the new ``app.py`` fails on them
("cannot import name …"). ``app.py`` therefore fingerprints the engine's sources on every rerun and, when
they no longer match what is loaded, drops those modules so they are imported again.

Imports nothing from the engine, so it can run before it.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
ENGINE_PACKAGES = ("core", "plugins", "services", "storefront_api", "dashboard")
MARKER = "_tarshish_code_fingerprint"  # set on the loaded ``core`` package: the code it was loaded from
APP = ROOT / "dashboard" / "app.py"    # Streamlit reruns the script itself: a change to it alone needs no reload


def fingerprint() -> str:
    """A hash of every engine source file's path, size and modification time."""
    stamps = sorted(
        (str(path.relative_to(ROOT)), stat.st_mtime_ns, stat.st_size)
        for package in ENGINE_PACKAGES for path in (ROOT / package).rglob("*.py")
        if path != APP and (stat := path.stat())
    )
    return hashlib.sha256(repr(stamps).encode()).hexdigest()


def _is_engine_module(name: str, module: ModuleType | None) -> bool:
    if name == "__main__":
        return False
    if name.split(".")[0] in ENGINE_PACKAGES:
        return True
    file = Path(getattr(module, "__file__", None) or "/").resolve()
    return file.is_relative_to(ROOT) and ".venv" not in file.relative_to(ROOT).parts


def drop_stale_modules(current: str) -> bool:
    """Forget the loaded engine modules unless they came from ``current``; True when it dropped them.

    A loaded ``core`` without a fingerprint was loaded by code that predates this check: stale too.
    """
    loaded = sys.modules.get("core")
    if loaded is None or getattr(loaded, MARKER, None) == current:
        return False
    for name, module in list(sys.modules.items()):
        if _is_engine_module(name, module):
            sys.modules.pop(name, None)
    return True


def mark_loaded(current: str) -> None:
    """Record that the engine modules now in memory were loaded from ``current``."""
    setattr(sys.modules["core"], MARKER, current)
