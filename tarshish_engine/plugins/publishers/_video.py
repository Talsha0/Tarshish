"""Helpers shared by the short-form video publishers (YouTube Shorts, TikTok).

Not a plugin: modules whose name starts with ``_`` are skipped by plugin discovery.

* :func:`local_video` yields a local MP4 for a generated post. Both platforms take the video
  binary itself (no URL pull), so a clip that only exists at a public URL — the stored copy
  in Supabase, or the source's own ``product.video_url`` — is downloaded to a temporary file
  first and removed afterwards.
* :func:`probe_video` reads width, height, duration and codec with ``ffprobe`` when it is on
  the PATH, so a clip the platforms would reject (not 9:16, too long) fails fast with a
  readable reason instead of after a full upload.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import httpx

from core.models import GeneratedContent
from core.resilience import NonRetryableError, get_logger, redact

logger = get_logger("plugins.publishers.video")

VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm"}
MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024
TARGET_RATIO = 9 / 16
RATIO_TOLERANCE = 0.02

# Test hook: an httpx.MockTransport instead of the network for remote clips.
download_transport: Any = None


@dataclass(frozen=True)
class VideoInfo:
    width: int
    height: int
    duration: float
    codec: str | None = None

    @property
    def is_vertical_9_16(self) -> bool:
        return self.height > self.width and abs(self.width / self.height - TARGET_RATIO) <= RATIO_TOLERANCE

    def describe(self) -> str:
        codec = f", {self.codec}" if self.codec else ""
        return f"{self.width}x{self.height}, {self.duration:.1f}s{codec}"


def require_video(content: GeneratedContent, platform: str) -> None:
    if content.content_type != "video":
        raise NonRetryableError(
            f"{platform} only publishes videos, but this post is '{content.content_type}' — route the content "
            "type to the ffmpeg_video_gen generator (config/content_profiles.yaml)"
        )


def video_reference(content: GeneratedContent) -> str | None:
    """Where the clip lives (local path or URL), for logs and dry-runs."""
    local = content.media_local_path
    if local and Path(local).is_file():
        return local
    return content.media_public_url or content.product.video_url


@contextmanager
def local_video(content: GeneratedContent, platform: str) -> Iterator[Path]:
    """A local copy of the post's video; temporary downloads are deleted on exit."""
    local = Path(content.media_local_path) if content.media_local_path else None
    if local is not None and local.is_file():
        if local.suffix.lower() not in VIDEO_SUFFIXES:
            raise NonRetryableError(f"{platform} needs an MP4/MOV video, not '{local.name}'")
        yield local
        return
    url = content.media_public_url or content.product.video_url
    if not url:
        raise NonRetryableError(
            f"{platform} needs a rendered video, but this post has none (only a render-job manifest was written) "
            "→ set VIDEO_RENDER_ENABLED=true and install ffmpeg so ffmpeg_video_gen renders the 1080x1920 MP4"
        )
    handle = tempfile.NamedTemporaryFile(prefix="tarshish_", suffix=".mp4", delete=False)
    path = Path(handle.name)
    try:
        with handle:
            _download(url, handle, platform)
        yield path
    finally:
        path.unlink(missing_ok=True)


def _download(url: str, handle: Any, platform: str) -> None:
    received = 0
    try:
        with httpx.Client(timeout=httpx.Timeout(120.0, connect=10.0), follow_redirects=True,
                          transport=download_transport) as client:
            with client.stream("GET", url) as response:
                if response.status_code >= 400:
                    raise NonRetryableError(
                        f"{platform}: the video at {redact(url)} could not be downloaded (HTTP {response.status_code})"
                    )
                for chunk in response.iter_bytes():
                    received += len(chunk)
                    if received > MAX_DOWNLOAD_BYTES:
                        raise NonRetryableError(f"{platform}: the video at {redact(url)} is larger than 512 MB")
                    handle.write(chunk)
    except httpx.HTTPError as exc:
        raise NonRetryableError(redact(f"{platform}: downloading the video failed: {type(exc).__name__}: {exc}")) from None
    if received == 0:
        raise NonRetryableError(f"{platform}: the video at {redact(url)} is empty")


def probe_video(path: Path | str) -> VideoInfo | None:
    """Width, height, duration and codec via ffprobe; ``None`` when ffprobe is unavailable or fails."""
    if shutil.which("ffprobe") is None:
        logger.debug("ffprobe is not on the PATH; skipping the local 9:16 / duration check of %s.", path)
        return None
    command = ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
               "stream=width,height,codec_name:format=duration", "-of", "json", str(path)]
    try:
        result = subprocess.run(command, check=True, capture_output=True, timeout=60)
        data = json.loads(result.stdout or b"{}")
        stream = (data.get("streams") or [{}])[0]
        return VideoInfo(
            width=int(stream["width"]), height=int(stream["height"]),
            duration=float((data.get("format") or {}).get("duration") or 0.0), codec=stream.get("codec_name"),
        )
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError, TypeError) as exc:
        logger.warning("ffprobe could not read %s (%s); skipping the local format check.", path, exc)
        return None


def check_short_form(info: VideoInfo | None, platform: str, max_seconds: float) -> None:
    """Refuse a clip the platform would reject: not vertical 9:16, or longer than ``max_seconds``."""
    if info is None:
        return
    problems = []
    if not info.is_vertical_9_16:
        problems.append(f"it is {info.width}x{info.height}, not vertical 9:16 (render 1080x1920)")
    if info.duration > max_seconds:
        problems.append(f"it runs {info.duration:.1f}s, longer than {max_seconds:g}s")
    if problems:
        raise NonRetryableError(f"{platform} would reject this video: " + "; ".join(problems))
