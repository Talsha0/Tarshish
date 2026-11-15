"""Console publisher: prints a formatted post card to stdout.

Used for local testing as a normal publisher and by the orchestrator to render
``--dry-run`` previews (the target then carries ``options["dry_run"] = True``
and the list of platforms a live run would publish to).
"""

from __future__ import annotations

import sys
import uuid

from core.interfaces import BasePublisher
from core.models import GeneratedContent, PublishReceipt
from plugins import format_price
from plugins.publishers import disclosure_enabled, render_post_text

_WIDTH = 78


class ConsolePublisher(BasePublisher):
    """Needs no credentials; never leaves the machine."""

    @property
    def platform_name(self) -> str:
        return "console_pub"

    def publish(self, content: GeneratedContent) -> PublishReceipt:
        options = self.target.options
        dry_run = bool(options.get("dry_run")) or self.context.dry_run
        product = content.product
        pipeline = options.get("pipeline_id") or self.context.pipeline_id or "-"

        if dry_run:
            headline = f"DRY-RUN PREVIEW · pipeline {pipeline} · mode {options.get('mode', '-')} · nothing is published"
        else:
            headline = f"CONSOLE PUBLISH · pipeline {pipeline} · page {self.target.page_name}"

        offer = format_price(product.price, product.currency)
        if product.discount_percent:
            offer += f" (was {format_price(product.original_price or 0, product.currency)}, -{product.discount_percent}%)"
        media = content.media_public_url or content.media_local_path or (product.image_urls[0] if product.image_urls else "none")

        lines = [
            "",
            "═" * _WIDTH,
            f" {headline}",
            "═" * _WIDTH,
            f" Product : {product.title}",
            f" Offer   : {offer} · ⭐ {product.rating:g} · {product.orders_count:,} orders · {product.source}",
            f" Content : {content.content_type} · {content.language} · {content.model_used}",
            f" Usage   : {content.tokens_input} in / {content.tokens_output} out tokens · ${content.generation_cost_usd:.4f}",
            f" Media   : {media}",
        ]
        intended = options.get("intended_targets") or []
        for index, target in enumerate(intended):
            lines.append(f" {'Targets :' if index == 0 else '         '} {target}")
        post = render_post_text(content, disclosure=disclosure_enabled(self.context.settings))
        lines += ["─" * _WIDTH, post, "═" * _WIDTH]

        print("\n".join(lines), file=sys.stdout, flush=True)
        return PublishReceipt(
            platform=self.platform_name, platform_post_id=f"console-{uuid.uuid4().hex[:10]}", success=True
        )
