"""FindHub storefront backend: the public REST API over the engine's database.

Run it from the project root, next to the pipelines and the dashboard:

    python -m uvicorn storefront_api.server:app --reload --port 8000

``storefront_api.server`` serves products, categories and click tracking; ``storefront_api.search``
is the typo-tolerant search it uses; ``api.seed`` loads the demo catalogue.
"""
