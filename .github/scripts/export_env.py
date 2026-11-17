"""Expose every repository secret and variable to the next workflow steps as environment variables.

The engine reads its credentials by variable NAME (the social-accounts matrix names them), so
the workflow passes everything through instead of listing names one by one. GitHub keeps
masking secret values in the logs. Variables are applied first, so a secret wins over a
variable of the same name. Nothing is printed except the variable names.
"""

from __future__ import annotations

import json
import os
import re
import secrets as random
import sys

NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
SKIP = {"GITHUB_TOKEN", "ACTIONS_RUNTIME_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_TOKEN"}


def main() -> int:
    values: dict[str, str] = {}
    for source in ("VARS_JSON", "SECRETS_JSON"):
        values.update(json.loads(os.environ.get(source) or "{}") or {})
    names = []
    with open(os.environ["GITHUB_ENV"], "a", encoding="utf-8") as env:
        for name, value in values.items():
            if name.upper() in SKIP or not NAME.fullmatch(name) or not isinstance(value, str):
                continue
            delimiter = f"EOF_{random.token_hex(8)}"
            env.write(f"{name}<<{delimiter}\n{value}\n{delimiter}\n")
            names.append(name)
    print(f"Exported {len(names)} setting(s): {', '.join(sorted(names)) or '(none)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
