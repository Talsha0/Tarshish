"""Vercel entry point for the FindHub storefront API.

Vercel turns every Python file in ``api/`` into a serverless function, so this folder holds
only this file; the API itself lives in ``storefront_api/``. ``vercel.json`` sends every
``/api/*`` request here, and FastAPI routes it (``/api/products``, ``/api/health``…).

Configuration comes from the Vercel project's environment variables (``DATABASE_URL`` and
the ``STOREFRONT_*`` settings); there is no ``.env`` file in a deployment.

Locally, run the same app with:  python -m uvicorn storefront_api.server:app --reload --port 8000
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from storefront_api.server import create_app  # noqa: E402

app = create_app()
