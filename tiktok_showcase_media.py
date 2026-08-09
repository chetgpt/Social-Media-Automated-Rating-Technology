"""Prepare and verify comment screenshots for TikTok photo publishing.

TikTok's official photo-post endpoint accepts a publicly reachable WebP or
JPEG URL owned by the developer application.  This module deliberately does
not upload to a third-party service.  Instead, it copies a normalized JPEG
into an operator-configured directory that is already served from the
verified HTTPS URL prefix.
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any, Callable
from urllib.parse import quote, urlsplit
import uuid


MAX_IMAGE_BYTES = 20 * 1024 * 1024
OUTPUT_WIDTH = 1080
OUTPUT_HEIGHT = 1920


class ShowcaseMediaError(RuntimeError):
    """Raised when a screenshot cannot be prepared or verified safely."""


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def validate_public_base_url(value: str) -> str:
    """Require a stable, non-IP HTTPS prefix suitable for TikTok verification."""

    raw = str(value or "").strip()
    parsed = urlsplit(raw)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ShowcaseMediaError(
            "public media base URL has an invalid port"
        ) from exc
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or port not in (None, 443)
    ):
        raise ShowcaseMediaError(
            "public media base URL must be a credential-free HTTPS URL "
            "prefix on the standard port"
        )
    hostname = str(parsed.hostname).casefold()
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise ShowcaseMediaError(
            "TikTok verified media URLs must use a domain, not an IP address"
        )
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise ShowcaseMediaError(
            "TikTok verified media URLs must use a public domain"
        )
    return raw.rstrip("/") + "/"


def jpeg_dimensions(path: str | Path) -> tuple[int, int]:
    """Read JPEG dimensions without adding an image-library dependency."""

    data = Path(path).read_bytes()
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        raise ShowcaseMediaError("normalized showcase media is not a JPEG")
    index = 2
    sof_markers = {
        0xC0,
        0xC1,
        0xC2,
        0xC3,
        0xC5,
        0xC6,
        0xC7,
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,
    }
    while index + 3 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        while index < len(data) and data[index] == 0xFF:
            index += 1
        if index >= len(data):
            break
        marker = data[index]
        index += 1
        if marker in {0xD8, 0xD9}:
            continue
        if index + 1 >= len(data):
            break
        length = int.from_bytes(data[index : index + 2], "big")
        if length < 2 or index + length > len(data):
            break
        if marker in sof_markers and length >= 7:
            height = int.from_bytes(data[index + 3 : index + 5], "big")
            width = int.from_bytes(data[index + 5 : index + 7], "big")
            if width <= 0 or height <= 0:
                break
            return width, height
        index += length
    raise ShowcaseMediaError("JPEG dimensions could not be resolved")


def _ffmpeg_path(explicit: str | Path | None = None) -> str:
    candidate = str(
        explicit
        or os.environ.get("TIKTOK_SHOWCASE_FFMPEG")
        or shutil.which("ffmpeg")
        or ""
    ).strip()
    if not candidate or not Path(candidate).is_file():
        raise ShowcaseMediaError(
            "ffmpeg is required to normalize the comment screenshot to JPEG"
        )
    return candidate


def stage_comment_screenshot(
    source_path: str | Path,
    *,
    expected_source_hash: str,
    allowed_source_root: str | Path,
    public_directory: str | Path,
    public_base_url: str,
    ffmpeg_path: str | Path | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """Normalize one exact-comment screenshot into content-addressed JPEG."""

    source = Path(source_path).resolve()
    source_root = Path(allowed_source_root).resolve()
    destination_root = Path(public_directory).resolve()
    if not _within(source, source_root):
        raise ShowcaseMediaError("comment screenshot is outside its allowed root")
    if not source.is_file():
        raise ShowcaseMediaError("comment screenshot does not exist")
    actual_source_hash = sha256_file(source)
    if (
        not expected_source_hash
        or actual_source_hash.casefold() != str(expected_source_hash).casefold()
    ):
        raise ShowcaseMediaError("comment screenshot hash does not match")

    base_url = validate_public_base_url(public_base_url)
    destination_root.mkdir(parents=True, exist_ok=True)
    temporary = destination_root / (
        f".showcase-{uuid.uuid4().hex}.tmp.jpg"
    )
    command = [
        _ffmpeg_path(ffmpeg_path),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-vf",
        (
            f"scale=w={OUTPUT_WIDTH}:h={OUTPUT_HEIGHT}:"
            "force_original_aspect_ratio=decrease,"
            f"pad={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:(ow-iw)/2:(oh-ih)/2:"
            "color=white"
        ),
        "-frames:v",
        "1",
        "-q:v",
        "2",
        str(temporary),
    ]
    try:
        completed = runner(
            command,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if int(getattr(completed, "returncode", 1)) != 0:
            error = str(getattr(completed, "stderr", "") or "").strip()
            raise ShowcaseMediaError(
                "ffmpeg could not normalize the screenshot"
                + (f": {error[:300]}" if error else "")
            )
        if not temporary.is_file():
            raise ShowcaseMediaError("ffmpeg did not create normalized media")
        media_size = temporary.stat().st_size
        if media_size <= 0 or media_size > MAX_IMAGE_BYTES:
            raise ShowcaseMediaError("normalized JPEG violates TikTok size limits")
        width, height = jpeg_dimensions(temporary)
        if width > OUTPUT_WIDTH or height > OUTPUT_HEIGHT:
            raise ShowcaseMediaError(
                "normalized JPEG violates configured picture dimensions"
            )
        media_hash = sha256_file(temporary)
        final_path = destination_root / f"{media_hash}.jpg"
        if final_path.exists():
            if sha256_file(final_path) != media_hash:
                raise ShowcaseMediaError(
                    "content-addressed showcase destination is inconsistent"
                )
            temporary.unlink()
        else:
            temporary.replace(final_path)
        media_url = base_url + quote(final_path.name)
        return {
            "source_path": str(source),
            "source_sha256": actual_source_hash,
            "media_path": str(final_path),
            "media_sha256": media_hash,
            "media_bytes": final_path.stat().st_size,
            "mime_type": "image/jpeg",
            "width": width,
            "height": height,
            "media_url": media_url,
        }
    finally:
        if temporary.exists():
            temporary.unlink()


def verify_hosted_media(
    media_url: str,
    *,
    expected_hash: str,
    session: Any | None = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """Verify the exact hosted bytes without following a redirect."""

    validate_public_base_url(media_url)
    if session is None:
        try:
            import requests
        except ImportError as exc:  # pragma: no cover - production dependency
            raise ShowcaseMediaError(
                "requests is required to verify hosted showcase media"
            ) from exc
        session = requests.Session()
    response = session.get(
        media_url,
        timeout=timeout,
        allow_redirects=False,
        stream=True,
    )
    status = int(getattr(response, "status_code", 0))
    if status != 200:
        raise ShowcaseMediaError(
            f"hosted showcase media returned HTTP {status}"
        )
    content_type = str(
        getattr(response, "headers", {}).get("content-type", "")
    ).split(";", 1)[0].strip().casefold()
    if content_type not in {"image/jpeg", "image/webp"}:
        raise ShowcaseMediaError(
            "hosted showcase media has an unsupported content type"
        )
    digest = hashlib.sha256()
    size = 0
    for chunk in response.iter_content(chunk_size=1024 * 1024):
        if not chunk:
            continue
        size += len(chunk)
        if size > MAX_IMAGE_BYTES:
            raise ShowcaseMediaError("hosted showcase media exceeds 20 MB")
        digest.update(chunk)
    actual_hash = digest.hexdigest()
    if not expected_hash or actual_hash.casefold() != expected_hash.casefold():
        raise ShowcaseMediaError("hosted showcase media hash does not match")
    return {
        "media_url": media_url,
        "media_sha256": actual_hash,
        "media_bytes": size,
        "content_type": content_type,
        "redirected": False,
    }
