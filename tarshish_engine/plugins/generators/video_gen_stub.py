"""Video generator stub — contract-complete, rendering left to implement.

It already behaves like a real generator inside the pipeline: it returns
``content_type="video"`` content with a caption and hashtags, reuses a video
supplied by the source when there is one, and writes a *render job* manifest
describing the clip to produce. To make it real, implement :meth:`_render`
with FFmpeg / MoviePy and return the path of the rendered file — storage,
queueing and publishers already handle ``media_local_path``.
"""

from __future__ import annotations

import json
from pathlib import Path

from core.interfaces import BaseGenerator, PluginContext
from core.models import GeneratedContent, Product
from core.resilience import get_logger
from plugins.generators.llm_text_gen import OfflineTemplateWriter

logger = get_logger("plugins.video_gen_stub")


class VideoGeneratorStub(BaseGenerator):
    content_type = "video"

    def __init__(self, context: PluginContext) -> None:
        super().__init__(context)
        self._writer = OfflineTemplateWriter()

    @property
    def generator_name(self) -> str:
        return "video_gen_stub"

    def generate(self, product: Product, language: str, prompt_template: str | None) -> GeneratedContent:
        caption, hashtags = self._writer.write(product, language)
        rendered = None if self.context.dry_run else self._render(product, language, caption)
        return GeneratedContent(
            product=product,
            content_type="video",
            language=language,
            caption=caption,
            hashtags=hashtags,
            media_local_path=rendered,
            # Until rendering exists, a video supplied by the source is passed through.
            media_public_url=product.video_url,
            model_used="video-stub",
        )

    def _render(self, product: Product, language: str, script: str) -> str | None:
        """Render a clip and return its local path (``None`` = nothing rendered).

        Replace the body with a real implementation, e.g.::

            clip = build_slideshow(product.image_urls, captions=script.splitlines())   # MoviePy
            clip.write_videofile(str(output_path), fps=30, codec="libx264")
            return str(output_path)

        The stub only records what should be rendered.
        """
        jobs_dir = Path(self.context.settings.local_media_dir) / "render_jobs"
        jobs_dir.mkdir(parents=True, exist_ok=True)
        manifest = jobs_dir / f"{product.canonical_hash[:16]}_{language}.json"
        manifest.write_text(
            json.dumps(
                {
                    "product": product.ref,
                    "language": language,
                    "format": {"width": 1080, "height": 1920, "fps": 30, "max_seconds": 20},
                    "image_urls": product.image_urls,
                    "script_lines": script.splitlines(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        logger.info("Video rendering is not implemented yet — render job written to %s.", manifest)
        return None
