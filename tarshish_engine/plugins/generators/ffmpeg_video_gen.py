"""Short vertical video generator (TikTok / Reels): LLM caption + FFmpeg slideshow.

The caption is written by the content profile's ``llm_model`` through the same provider
plumbing, pricing and budget guardrail as ``llm_text_gen`` (TikTok-style: hook first,
short, a few hashtags). The clip itself:

* a video supplied by the source (``product.video_url``) is passed through as-is;
* otherwise, with ``VIDEO_RENDER_ENABLED=true`` and ``ffmpeg`` on the PATH, the product
  photos are rendered into a 1080×1920 slideshow (``output/videos/<id>_<lang>.mp4``);
  storage then uploads it and publishers use the stored copy;
* otherwise a *render job* manifest is written to ``output/render_jobs/`` describing the
  clip to produce.

Dry-runs never render, write files or call a paid model.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from core.models import GeneratedContent, Product
from core.resilience import get_logger
from plugins.generators.llm_text_gen import LLMTextGenerator

logger = get_logger("plugins.ffmpeg_video_gen")

VIDEO_FORMAT = {"width": 1080, "height": 1920, "fps": 30, "seconds_per_image": 3, "max_images": 4}

VIDEO_PROMPT_TEMPLATE = """Write the caption of a short vertical product video (TikTok / Reels) in {language_name}.

Product
- Title: {title}
- Price: {price}{was_price}
- Rating: {rating}/5 from buyers, {orders_count}+ orders
- Category: {category}

Guidelines
- Tone: {tone}
- 1-2 short lines, at most 150 characters: open with a hook, mention the price{discount_hint}, end with a call to action.
- Natural, spoken-style {language_name}; at most 2 emojis. No URLs or markdown in the caption.
- "hashtags": 3-5 fitting hashtags."""


class FfmpegVideoGenerator(LLMTextGenerator):
    """LLM caption + vertical slideshow video (or a render job when FFmpeg rendering is off)."""

    content_type = "video"
    default_prompt_template = VIDEO_PROMPT_TEMPLATE

    @property
    def generator_name(self) -> str:
        return "ffmpeg_video_gen"

    def finish(self, content: GeneratedContent) -> GeneratedContent:
        product = content.product
        if product.video_url:
            content.media_public_url = product.video_url  # the source already has a clip
            return content
        if self.context.dry_run:
            return content
        rendered = self._render(product, content.language) if self._render_enabled() else None
        if rendered:
            content.media_local_path = rendered
        else:
            self._write_render_job(product, content)
        return content

    # -- rendering ------------------------------------------------------------------- #
    def _render_enabled(self) -> bool:
        flag = (self.context.settings.get_env("VIDEO_RENDER_ENABLED", "false") or "").lower()
        return flag in ("1", "true", "yes", "on") and shutil.which("ffmpeg") is not None

    def _render(self, product: Product, language: str) -> str | None:
        """Render the product photos into a 9:16 slideshow; ``None`` when it cannot be done."""
        images = product.image_urls[: VIDEO_FORMAT["max_images"]]
        if not images:
            return None
        out_dir = Path(self.context.settings.local_media_dir) / "videos"
        out_dir.mkdir(parents=True, exist_ok=True)
        output = out_dir / f"{product.canonical_hash[:16]}_{language}.mp4"
        width, height, fps, seconds = (VIDEO_FORMAT[k] for k in ("width", "height", "fps", "seconds_per_image"))
        command = ["ffmpeg", "-y", "-loglevel", "error", "-protocol_whitelist", "https,http,tls,tcp,file"]
        for url in images:
            command += ["-loop", "1", "-t", str(seconds), "-i", url]
        scaled = [
            f"[{i}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=white,setsar=1,fps={fps}[v{i}]"
            for i in range(len(images))
        ]
        joined = "".join(f"[v{i}]" for i in range(len(images)))
        command += [
            "-filter_complex", ";".join(scaled) + f";{joined}concat=n={len(images)}:v=1:a=0,format=yuv420p[out]",
            "-map", "[out]", "-c:v", "libx264", "-movflags", "+faststart", str(output),
        ]
        try:
            subprocess.run(command, check=True, capture_output=True, timeout=180)
        except (OSError, subprocess.SubprocessError) as exc:
            stderr = getattr(exc, "stderr", b"") or b""
            logger.warning("FFmpeg could not render %s: %s %s", product.ref, exc, stderr.decode(errors="replace")[-300:])
            return None
        logger.info("Rendered %s (%d image(s)).", output, len(images))
        return str(output)

    def _write_render_job(self, product: Product, content: GeneratedContent) -> None:
        jobs_dir = Path(self.context.settings.local_media_dir) / "render_jobs"
        jobs_dir.mkdir(parents=True, exist_ok=True)
        manifest = jobs_dir / f"{product.canonical_hash[:16]}_{content.language}.json"
        manifest.write_text(
            json.dumps(
                {
                    "product": product.ref,
                    "language": content.language,
                    "format": VIDEO_FORMAT,
                    "image_urls": product.image_urls,
                    "caption": content.caption,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        logger.info("Video rendering is off (VIDEO_RENDER_ENABLED) — render job written to %s.", manifest)
