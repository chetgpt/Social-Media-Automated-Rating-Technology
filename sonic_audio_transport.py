#!/usr/bin/env python
"""Fail-closed transient audio transport for a bounded SONIC AUDIT pilot.

This module deliberately owns only transport.  It attaches to the already
verified Microsoft Edge Profile 7 session, binds each frozen master-registry
candidate to the exact TikTok item returned by its canonical URL, streams the
item's media into an owned temporary directory, and exposes local media/audio
paths only for the duration of one callback.

No signed media URL, cookie, authorization value, raw TikTok payload, video, or
decoded audio is returned or persisted by this module.  Per-item files and the
whole run directory are removed on success and on error.  The shared browser is
never closed.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
import wave
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Sequence
from urllib.parse import unquote, urljoin, urlsplit


MAX_PILOT_POSTS = 60
HARD_MAX_SOURCE_BYTES = 128 * 1024 * 1024
HARD_MAX_AUDIO_BYTES = 16 * 1024 * 1024
HARD_MAX_DURATION_SECONDS = 300.0
DEFAULT_BROWSER_STATE = Path("comments_data") / "social_browser" / "state.json"
_POST_PATH = re.compile(r"^/@([^/]+)/(video|photo)/(\d+)$", re.IGNORECASE)
_HANDLE = re.compile(r"^[A-Za-z0-9._]{1,64}$")
_SAFE_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,95}$")
_SAFE_AUDIO_CONVERSION = re.compile(r"^[a-z0-9][a-z0-9_]{0,127}$")
DEFAULT_AUDIO_CONVERSION = "local_ffmpeg_pcm_s16le_mono_16000hz"
ARCHIVE_M4A_AUDIO_CONVERSION = (
    "local_ffmpeg_aac_lc_stereo_44100hz_192kbps_ipod_m4a"
)
ALLOWED_AUDIO_CONVERSIONS = frozenset(
    {DEFAULT_AUDIO_CONVERSION, ARCHIVE_M4A_AUDIO_CONVERSION}
)
_RUN_ID = re.compile(r"^sonic_[0-9a-f]{16}$")
_TEMP_OWNER_SCHEMA = "tiktok-sonic-temp-owner-v1"
_TEMP_OWNER_MARKER = ".sonic-audit-owner.json"
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_MEDIA_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
)
_MEDIA_HOST_SUFFIXES = (
    "tiktok.com",
    "tiktokcdn.com",
    "tiktokcdn-us.com",
    "tiktokv.com",
    "tiktokv.us",
    "byteoversea.com",
    "ibytedtos.com",
    "muscdn.com",
    "musical.ly",
    "akamaized.net",
)
_FORBIDDEN_RESULT_KEYS = (
    "authorization",
    "cookie",
    "header",
    "media_url",
    "path",
    "play_url",
    "signed",
    "token",
    "url",
)


class SonicTransportError(RuntimeError):
    """A fail-closed transient transport failure."""


class CandidateIdentityError(SonicTransportError):
    """The frozen candidate and TikTok response do not identify one post."""


class TransportLimitError(SonicTransportError):
    """A configured byte, duration, or pilot-cardinality bound was exceeded."""


class UnsupportedMediaError(SonicTransportError):
    """The exact post cannot produce a supported transient audio input."""


@dataclass(frozen=True)
class AcquisitionLimits:
    """Hard-capped limits for the initial local pilot."""

    max_posts: int = MAX_PILOT_POSTS
    max_source_bytes: int = 64 * 1024 * 1024
    max_audio_bytes: int = 8 * 1024 * 1024
    max_duration_seconds: float = 180.0
    max_total_duration_seconds: float = 3600.0
    max_html_bytes: int = 8 * 1024 * 1024
    request_timeout_seconds: float = 30.0
    transcode_timeout_seconds: float = 90.0
    stream_chunk_bytes: int = 64 * 1024
    max_redirects: int = 3

    def __post_init__(self) -> None:
        integer_bounds = {
            "max_posts": (self.max_posts, 1, MAX_PILOT_POSTS),
            "max_source_bytes": (
                self.max_source_bytes,
                1,
                HARD_MAX_SOURCE_BYTES,
            ),
            "max_audio_bytes": (
                self.max_audio_bytes,
                1,
                HARD_MAX_AUDIO_BYTES,
            ),
            "max_html_bytes": (self.max_html_bytes, 1, 16 * 1024 * 1024),
            "stream_chunk_bytes": (
                self.stream_chunk_bytes,
                1024,
                1024 * 1024,
            ),
            "max_redirects": (self.max_redirects, 0, 5),
        }
        for name, (value, minimum, maximum) in integer_bounds.items():
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{name} must be an integer")
            if not minimum <= value <= maximum:
                raise ValueError(f"{name} is outside its safe pilot bound")
        numeric_bounds = {
            "max_duration_seconds": (
                self.max_duration_seconds,
                0.1,
                HARD_MAX_DURATION_SECONDS,
            ),
            "max_total_duration_seconds": (
                self.max_total_duration_seconds,
                0.1,
                MAX_PILOT_POSTS * HARD_MAX_DURATION_SECONDS,
            ),
            "request_timeout_seconds": (
                self.request_timeout_seconds,
                1.0,
                120.0,
            ),
            "transcode_timeout_seconds": (
                self.transcode_timeout_seconds,
                1.0,
                600.0,
            ),
        }
        for name, (value, minimum, maximum) in numeric_bounds.items():
            try:
                number = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{name} must be numeric") from exc
            if not math.isfinite(number) or not minimum <= number <= maximum:
                raise ValueError(f"{name} is outside its safe pilot bound")


@dataclass(frozen=True)
class FrozenPostCandidate:
    post_id: str
    canonical_url: str
    creator_handle: str
    content_type: str = "video"


@dataclass(frozen=True)
class SafeCandidateIdentity:
    """The URL-free identity allowed across the per-item error boundary."""

    post_id: str
    creator_handle: str
    content_type: str = "video"


@dataclass(frozen=True)
class MediaProbe:
    duration_seconds: float
    byte_count: int


@dataclass(frozen=True)
class DownloadReceipt:
    byte_count: int
    sha256: str
    content_type: str


@dataclass(frozen=True)
class TransientAudioItem:
    """Paths valid only while the transport callback is executing."""

    post_id: str
    creator_handle: str
    source_media_path: Path
    audio_path: Path
    duration_seconds: float
    source_byte_count: int
    audio_byte_count: int
    source_sha256: str
    audio_sha256: str
    provenance: Mapping[str, Any]
    timing: Mapping[str, float]


@dataclass(frozen=True)
class SonicTransportReceipt:
    """Safe receipt returned after all corresponding temporary files are gone."""

    post_id: str
    creator_handle: str
    duration_seconds: float
    source_byte_count: int
    audio_byte_count: int
    source_sha256: str
    audio_sha256: str
    provenance: Mapping[str, Any]
    timing: Mapping[str, float] = field(default_factory=dict)
    derived_result: Any = None
    status: str = "completed"
    error_code: str = ""


Processor = Callable[[TransientAudioItem], Any | Awaitable[Any]]
ErrorProcessor = Callable[
    [SafeCandidateIdentity, str], Any | Awaitable[Any]
]
ReceiptProcessor = Callable[[SonicTransportReceipt], Any | Awaitable[Any]]
MediaFetcher = Callable[..., DownloadReceipt | Awaitable[DownloadReceipt]]
MediaInspector = Callable[[Path], MediaProbe | Awaitable[MediaProbe]]
AudioTranscoder = Callable[..., Any | Awaitable[Any]]


def _text(value: Any) -> str:
    return str(value or "").strip()


def _elapsed_ms(started_at: float) -> float:
    elapsed = max(0.0, (time.monotonic() - started_at) * 1000.0)
    return round(elapsed if math.isfinite(elapsed) else 0.0, 3)


def _item_timing() -> dict[str, float]:
    return {
        "html_fetch_ms": 0.0,
        "media_download_ms": 0.0,
        "inspect_transcode_ms": 0.0,
        "processor_ms": 0.0,
        "total_item_ms": 0.0,
    }


def _normalize_handle(value: Any) -> str:
    return unquote(_text(value)).lstrip("@").strip().casefold()


def _canonical_candidate(value: Mapping[str, Any]) -> FrozenPostCandidate:
    post_id = _text(value.get("post_id") or value.get("id"))
    canonical_url = _text(value.get("canonical_url") or value.get("url"))
    creator = _normalize_handle(
        value.get("creator_handle") or value.get("creator") or value.get("username")
    )
    if (
        not post_id.isdigit()
        or not creator
        or not _HANDLE.fullmatch(creator)
        or not canonical_url
    ):
        raise CandidateIdentityError("candidate_identity_incomplete")
    parsed = urlsplit(canonical_url)
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() not in {"tiktok.com", "www.tiktok.com"}
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or parsed.fragment
    ):
        raise CandidateIdentityError("candidate_url_not_canonical_tiktok")
    match = _POST_PATH.fullmatch(parsed.path.rstrip("/"))
    if not match:
        raise CandidateIdentityError("candidate_url_path_not_canonical")
    url_handle, content_type, url_post_id = match.groups()
    if url_post_id != post_id:
        raise CandidateIdentityError("candidate_url_post_id_mismatch")
    if (
        not _HANDLE.fullmatch(unquote(url_handle))
        or _normalize_handle(url_handle) != creator
    ):
        raise CandidateIdentityError("candidate_url_creator_mismatch")
    declared_type = _text(value.get("content_type") or content_type).casefold()
    if declared_type != content_type.casefold():
        raise CandidateIdentityError("candidate_url_media_type_mismatch")
    if content_type.casefold() != "video":
        raise UnsupportedMediaError("photo_posts_are_not_supported_by_audio_transport")
    clean_url = f"https://www.tiktok.com/@{unquote(url_handle)}/video/{post_id}"
    return FrozenPostCandidate(post_id, clean_url, creator, "video")


def validate_frozen_candidates(
    candidates: Sequence[Mapping[str, Any]],
    *,
    limits: AcquisitionLimits,
) -> tuple[FrozenPostCandidate, ...]:
    if not candidates:
        raise ValueError("at least one frozen candidate is required")
    if len(candidates) > limits.max_posts:
        raise TransportLimitError("pilot_post_limit_exceeded")
    normalized = tuple(_canonical_candidate(value) for value in candidates)
    post_ids = [candidate.post_id for candidate in normalized]
    if len(post_ids) != len(set(post_ids)):
        raise CandidateIdentityError("duplicate_frozen_post_id")
    return normalized


def _safe_media_url(value: Any) -> str:
    candidate = _text(value)
    if candidate.startswith("//"):
        candidate = "https:" + candidate
    parsed = urlsplit(candidate)
    host = (parsed.hostname or "").casefold().rstrip(".")
    if (
        parsed.scheme.casefold() != "https"
        or not host
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or not any(
            host == suffix or host.endswith("." + suffix)
            for suffix in _MEDIA_HOST_SUFFIXES
        )
    ):
        raise SonicTransportError("unsafe_tiktok_media_endpoint")
    return candidate


def _url_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        for key in ("urlList", "url_list", "UrlList", "urls"):
            nested = value.get(key)
            if isinstance(nested, list):
                return [_text(item) for item in nested if _text(item)]
        for key in ("url", "Url"):
            if _text(value.get(key)):
                return [_text(value.get(key))]
    if isinstance(value, list):
        return [_text(item) for item in value if _text(item)]
    return []


def extract_transient_media_url(item: Mapping[str, Any]) -> str:
    """Return one validated address for immediate use; callers must not store it."""

    video = item.get("video")
    if not isinstance(video, Mapping):
        raise UnsupportedMediaError("tiktok_item_has_no_video_transport")

    bitrate_rows = video.get("bitrateInfo") or video.get("bitrate_info") or []
    ranked: list[tuple[float, str]] = []
    if isinstance(bitrate_rows, list):
        for row in bitrate_rows:
            if not isinstance(row, Mapping):
                continue
            address = row.get("PlayAddr") or row.get("playAddr") or row.get("play_addr")
            try:
                bitrate = float(row.get("Bitrate") or row.get("bitrate") or math.inf)
            except (TypeError, ValueError):
                bitrate = math.inf
            for url in _url_values(address):
                ranked.append((bitrate, url))
    for _, value in sorted(ranked, key=lambda pair: pair[0]):
        with contextlib.suppress(SonicTransportError):
            return _safe_media_url(value)

    for key in (
        "playAddr",
        "play_addr",
        "downloadAddr",
        "download_addr",
        "playUrl",
        "play_url",
    ):
        for value in _url_values(video.get(key)):
            with contextlib.suppress(SonicTransportError):
                return _safe_media_url(value)
    raise UnsupportedMediaError("tiktok_item_has_no_safe_video_transport")


def _item_identity(item: Mapping[str, Any]) -> tuple[str, str]:
    post_id = _text(item.get("id") or item.get("aweme_id") or item.get("item_id"))
    author = item.get("author") if isinstance(item.get("author"), Mapping) else {}
    creator = _normalize_handle(
        author.get("uniqueId")
        or author.get("unique_id")
        or item.get("author_unique_id")
    )
    return post_id, creator


def _file_hash(path: Path, *, max_bytes: int) -> tuple[int, str]:
    digest = hashlib.sha256()
    byte_count = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(64 * 1024)
            if not chunk:
                break
            byte_count += len(chunk)
            if byte_count > max_bytes:
                raise TransportLimitError("temporary_file_size_limit_exceeded")
            digest.update(chunk)
    return byte_count, digest.hexdigest()


def _validate_run_id(value: Any) -> str:
    run_id = _text(value)
    if _RUN_ID.fullmatch(run_id) is None:
        raise SonicTransportError("invalid_sonic_audit_run_id")
    return run_id


def _validate_temp_root(
    value: Path | str | None,
    *,
    create: bool = True,
) -> Path:
    root = (
        Path(value)
        if value is not None
        else Path(tempfile.gettempdir()) / "tiktok-sonic-audit"
    )
    root = root.expanduser().resolve()
    if root == Path(root.anchor) or root.parent == root:
        raise SonicTransportError("temporary_root_is_too_broad")
    if root.exists() and root.is_symlink():
        raise SonicTransportError("temporary_root_must_not_be_a_symlink")
    if create:
        root.mkdir(parents=True, exist_ok=True)
    if root.exists() and not root.is_dir():
        raise SonicTransportError("temporary_root_is_not_a_directory")
    return root


def _run_directory_prefix(run_id: str) -> str:
    return f"sonic-audit-{_validate_run_id(run_id)}-"


def _marker_body(run_id: str, directory_name: str) -> dict[str, str]:
    return {
        "schema_version": _TEMP_OWNER_SCHEMA,
        "run_id": _validate_run_id(run_id),
        "directory_name": directory_name,
        "ownership_state": "closed",
    }


def _marker_hash(body: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(body),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _write_closed_owner_marker(
    directory: Path,
    *,
    run_id: str,
    final_directory_name: str,
) -> None:
    body = _marker_body(run_id, final_directory_name)
    payload = {**body, "marker_hash": _marker_hash(body)}
    marker = directory / _TEMP_OWNER_MARKER
    serialized = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    with marker.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(serialized)
        handle.flush()
        os.fsync(handle.fileno())


def _owned_run_marker_valid(
    directory: Path,
    *,
    root: Path,
    run_id: str,
) -> bool:
    run_id = _validate_run_id(run_id)
    if directory.is_symlink() or not directory.is_dir():
        return False
    try:
        if directory.resolve().parent != root.resolve():
            return False
    except OSError:
        return False
    if not directory.name.startswith(_run_directory_prefix(run_id)):
        return False
    marker = directory / _TEMP_OWNER_MARKER
    if marker.is_symlink() or not marker.is_file():
        return False
    try:
        if marker.stat().st_size > 4096:
            return False
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return False
    if not isinstance(payload, Mapping):
        return False
    expected_keys = {
        "schema_version",
        "run_id",
        "directory_name",
        "ownership_state",
        "marker_hash",
    }
    if set(payload) != expected_keys:
        return False
    body = {key: payload.get(key) for key in expected_keys if key != "marker_hash"}
    return bool(
        body == _marker_body(run_id, directory.name)
        and _text(payload.get("marker_hash")) == _marker_hash(body)
    )


def _matching_run_entries(root: Path, run_id: str) -> list[Path]:
    if not root.exists():
        return []
    prefix = _run_directory_prefix(run_id)
    try:
        return sorted(
            (entry for entry in root.iterdir() if entry.name.startswith(prefix)),
            key=lambda entry: entry.name,
        )
    except OSError as exc:
        raise SonicTransportError("temporary_root_scan_failed") from exc


def _create_owned_run_directory(root: Path, run_id: str) -> Path:
    run_id = _validate_run_id(run_id)
    nonce = uuid.uuid4().hex[:16]
    final_name = f"{_run_directory_prefix(run_id)}{nonce}"
    final_directory = root / final_name
    staging = Path(tempfile.mkdtemp(prefix=".sonic-audit-init-", dir=root))
    try:
        _write_closed_owner_marker(
            staging,
            run_id=run_id,
            final_directory_name=final_name,
        )
        os.replace(staging, final_directory)
    except Exception:
        if staging.exists() and not staging.is_symlink():
            with contextlib.suppress(OSError):
                shutil.rmtree(staging)
        raise
    if not _owned_run_marker_valid(
        final_directory,
        root=root,
        run_id=run_id,
    ):
        raise SonicTransportError("temporary_run_ownership_marker_invalid")
    return final_directory


def _assert_owned_item(path: Path, run_directory: Path, run_id: str) -> None:
    root = run_directory.parent
    if not _owned_run_marker_valid(run_directory, root=root, run_id=run_id):
        raise SonicTransportError("temporary_run_ownership_marker_invalid")
    if path.is_symlink():
        raise SonicTransportError("temporary_item_path_is_symlink")
    try:
        relative = path.resolve().relative_to(run_directory.resolve())
    except ValueError as exc:
        raise SonicTransportError("temporary_path_escaped_owned_root") from exc
    if len(relative.parts) != 1 or not relative.parts[0].startswith("item-"):
        raise SonicTransportError("temporary_item_is_not_transport_owned")


def _remove_owned_item(path: Path, run_directory: Path, run_id: str) -> None:
    if not path.exists():
        return
    _assert_owned_item(path, run_directory, run_id)
    shutil.rmtree(path)


def _remove_owned_run(directory: Path, root: Path, run_id: str) -> None:
    if not directory.exists():
        return
    if not _owned_run_marker_valid(directory, root=root, run_id=run_id):
        raise SonicTransportError("temporary_run_ownership_marker_invalid")
    marker = directory / _TEMP_OWNER_MARKER
    # Keep the closed owner marker in place until every possible raw-media
    # child has gone.  A hard kill during recursive child deletion therefore
    # still leaves a provably owned run directory that resume can repair.
    for child in list(directory.iterdir()):
        if child.name == _TEMP_OWNER_MARKER:
            continue
        if child.is_symlink() or child.is_file():
            child.unlink()
        elif child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
    remaining = [child for child in directory.iterdir() if child != marker]
    if remaining:
        raise SonicTransportError("temporary_run_cleanup_incomplete")
    marker.unlink()
    directory.rmdir()


def verify_no_run_residue(
    run_id: str,
    *,
    temp_root: Path | str | None = None,
) -> bool:
    """Return whether an exact run has no marked or suspicious temp entries."""

    run_id = _validate_run_id(run_id)
    root = _validate_temp_root(temp_root, create=False)
    return not _matching_run_entries(root, run_id)


def cleanup_stale_run(
    run_id: str,
    *,
    temp_root: Path | str | None = None,
) -> bool:
    """Delete only exact-run residue with a closed, hash-bound owner marker.

    Unmarked, symlinked, or marker-mismatched entries are never deleted and
    make the result false so a resume cannot silently pass the residue gate.
    This function performs no browser access.
    """

    run_id = _validate_run_id(run_id)
    root = _validate_temp_root(temp_root, create=False)
    entries = _matching_run_entries(root, run_id)
    for entry in entries:
        if not _owned_run_marker_valid(entry, root=root, run_id=run_id):
            continue
        _remove_owned_run(entry, root, run_id)
    return verify_no_run_residue(run_id, temp_root=root)


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


class RequestsBoundedMediaFetcher:
    """Cookie-free HTTPS streaming fetcher with redirect and byte guards."""

    def __init__(self, limits: AcquisitionLimits) -> None:
        self.limits = limits

    def _download_sync(
        self,
        media_url: str,
        destination: Path,
        *,
        referer: str,
    ) -> DownloadReceipt:
        import requests

        current_url = _safe_media_url(media_url)
        response = None
        try:
            for redirect_index in range(self.limits.max_redirects + 1):
                try:
                    response = requests.get(
                        current_url,
                        headers={
                            "Accept": "video/*,audio/*;q=0.9,*/*;q=0.1",
                            "Referer": referer,
                            "User-Agent": _MEDIA_USER_AGENT,
                        },
                        timeout=self.limits.request_timeout_seconds,
                        stream=True,
                        allow_redirects=False,
                    )
                except Exception:
                    # Request exceptions commonly embed the signed address in
                    # their message.  Deliberately sever that exception chain.
                    raise SonicTransportError("tiktok_media_stream_failed") from None
                if response.status_code in _REDIRECT_STATUSES:
                    location = _text(response.headers.get("Location"))
                    response.close()
                    response = None
                    if redirect_index >= self.limits.max_redirects or not location:
                        raise SonicTransportError("tiktok_media_redirect_limit")
                    current_url = _safe_media_url(urljoin(current_url, location))
                    continue
                if not 200 <= int(response.status_code) < 300:
                    raise SonicTransportError("tiktok_media_http_failure")
                break
            if response is None:
                raise SonicTransportError("tiktok_media_response_missing")

            content_type = _text(response.headers.get("Content-Type")).split(";", 1)[0].casefold()
            if content_type and not (
                content_type.startswith("video/")
                or content_type.startswith("audio/")
                or content_type == "application/octet-stream"
            ):
                raise SonicTransportError("tiktok_media_content_type_rejected")
            content_length = _text(response.headers.get("Content-Length"))
            if content_length:
                try:
                    declared_size = int(content_length)
                except ValueError as exc:
                    raise SonicTransportError("tiktok_media_content_length_invalid") from exc
                if declared_size < 1 or declared_size > self.limits.max_source_bytes:
                    raise TransportLimitError("source_content_length_limit_exceeded")

            digest = hashlib.sha256()
            byte_count = 0
            try:
                with destination.open("xb") as handle:
                    for chunk in response.iter_content(
                        chunk_size=self.limits.stream_chunk_bytes
                    ):
                        if not chunk:
                            continue
                        byte_count += len(chunk)
                        if byte_count > self.limits.max_source_bytes:
                            raise TransportLimitError("source_stream_size_limit_exceeded")
                        handle.write(chunk)
                        digest.update(chunk)
            except TransportLimitError:
                with contextlib.suppress(OSError):
                    destination.unlink()
                raise
            except Exception:
                with contextlib.suppress(OSError):
                    destination.unlink()
                # Streaming exceptions can also contain the signed address.
                raise SonicTransportError("tiktok_media_stream_failed") from None
            if byte_count < 1:
                with contextlib.suppress(OSError):
                    destination.unlink()
                raise SonicTransportError("tiktok_media_stream_empty")
            return DownloadReceipt(byte_count, digest.hexdigest(), content_type)
        finally:
            if response is not None:
                response.close()

    async def __call__(
        self,
        media_url: str,
        destination: Path,
        *,
        referer: str,
    ) -> DownloadReceipt:
        return await asyncio.to_thread(
            self._download_sync,
            media_url,
            destination,
            referer=referer,
        )


class PlaywrightBoundedMediaFetcher:
    """Bounded first-party range fetch using Profile 7's in-memory session.

    TikTok's current web play addresses reject a cookie-free client with 403,
    while the same exact address succeeds through the authenticated TikTok
    request context. Cookie/header values remain inside Playwright and are
    never returned, logged, or persisted.
    """

    def __init__(self, request_context: Any, limits: AcquisitionLimits) -> None:
        self.request_context = request_context
        self.limits = limits

    async def _range(
        self,
        media_url: str,
        *,
        referer: str,
        start: int,
        end: int,
    ) -> Any:
        try:
            return await self.request_context.get(
                media_url,
                headers={
                    "Accept": "video/*,audio/*;q=0.9,*/*;q=0.1",
                    "Referer": referer,
                    "Range": f"bytes={start}-{end}",
                },
                timeout=int(self.limits.request_timeout_seconds * 1000),
                fail_on_status_code=False,
                max_redirects=self.limits.max_redirects,
            )
        except Exception:
            raise SonicTransportError("tiktok_media_stream_failed") from None

    async def __call__(
        self,
        media_url: str,
        destination: Path,
        *,
        referer: str,
    ) -> DownloadReceipt:
        media_url = _safe_media_url(media_url)
        first = None
        content_type = ""
        try:
            first = await self._range(
                media_url,
                referer=referer,
                start=0,
                end=0,
            )
            status = int(getattr(first, "status", 0) or 0)
            headers = getattr(first, "headers", {}) or {}
            content_type = _text(
                headers.get("content-type") or headers.get("Content-Type")
            ).split(";", 1)[0].casefold()
            if content_type and not (
                content_type.startswith("video/")
                or content_type.startswith("audio/")
                or content_type == "application/octet-stream"
            ):
                raise SonicTransportError("tiktok_media_content_type_rejected")

            if status == 206:
                content_range = _text(
                    headers.get("content-range") or headers.get("Content-Range")
                )
                match = re.fullmatch(r"bytes\s+0-0/(\d+)", content_range, re.I)
                if match is None:
                    raise SonicTransportError("tiktok_media_content_range_invalid")
                total = int(match.group(1))
                first_body = await first.body()
                if first_body != first_body[:1] or len(first_body) != 1:
                    raise SonicTransportError("tiktok_media_range_body_invalid")
            elif status == 200:
                declared = _text(
                    headers.get("content-length") or headers.get("Content-Length")
                )
                if not declared.isdigit():
                    raise SonicTransportError("tiktok_media_content_length_invalid")
                total = int(declared)
                first_body = await first.body()
                if len(first_body) != total:
                    raise SonicTransportError("tiktok_media_response_size_mismatch")
            else:
                raise SonicTransportError("tiktok_media_http_failure")

            if total < 1 or total > self.limits.max_source_bytes:
                raise TransportLimitError("source_content_length_limit_exceeded")
            digest = hashlib.sha256()
            written = 0
            try:
                with destination.open("xb") as handle:
                    if status == 200:
                        handle.write(first_body)
                        digest.update(first_body)
                        written = len(first_body)
                    else:
                        handle.write(first_body)
                        digest.update(first_body)
                        written = 1
                        chunk_size = min(
                            self.limits.stream_chunk_bytes * 16,
                            1024 * 1024,
                        )
                        while written < total:
                            end = min(total - 1, written + chunk_size - 1)
                            response = await self._range(
                                media_url,
                                referer=referer,
                                start=written,
                                end=end,
                            )
                            try:
                                if int(getattr(response, "status", 0) or 0) != 206:
                                    raise SonicTransportError(
                                        "tiktok_media_range_http_failure"
                                    )
                                response_headers = getattr(response, "headers", {}) or {}
                                supplied_range = _text(
                                    response_headers.get("content-range")
                                    or response_headers.get("Content-Range")
                                )
                                expected_range = f"bytes {written}-{end}/{total}"
                                if supplied_range.casefold() != expected_range.casefold():
                                    raise SonicTransportError(
                                        "tiktok_media_content_range_invalid"
                                    )
                                body = await response.body()
                                if len(body) != end - written + 1:
                                    raise SonicTransportError(
                                        "tiktok_media_range_body_invalid"
                                    )
                                handle.write(body)
                                digest.update(body)
                                written += len(body)
                            finally:
                                with contextlib.suppress(Exception):
                                    await response.dispose()
            except Exception:
                with contextlib.suppress(OSError):
                    destination.unlink()
                raise
            if written != total:
                with contextlib.suppress(OSError):
                    destination.unlink()
                raise SonicTransportError("tiktok_media_response_size_mismatch")
            return DownloadReceipt(written, digest.hexdigest(), content_type)
        except (SonicTransportError, TransportLimitError):
            raise
        except Exception:
            with contextlib.suppress(OSError):
                destination.unlink()
            raise SonicTransportError("tiktok_media_stream_failed") from None
        finally:
            if first is not None:
                with contextlib.suppress(Exception):
                    await first.dispose()


class FFprobeMediaInspector:
    def __init__(self, *, timeout_seconds: float = 30.0, executable: str = "ffprobe") -> None:
        self.timeout_seconds = float(timeout_seconds)
        self.executable = executable

    def __call__(self, path: Path) -> MediaProbe:
        executable = shutil.which(self.executable)
        if not executable:
            raise SonicTransportError("ffprobe_not_available")
        try:
            completed = subprocess.run(
                [
                    executable,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration,size",
                    "-of",
                    "json",
                    os.fspath(path),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
            payload = json.loads(completed.stdout)
            details = payload.get("format") if isinstance(payload, Mapping) else {}
            return MediaProbe(
                duration_seconds=float((details or {}).get("duration") or 0.0),
                byte_count=int((details or {}).get("size") or path.stat().st_size),
            )
        except SonicTransportError:
            raise
        except Exception as exc:
            raise SonicTransportError("ffprobe_media_validation_failed") from exc


class FFmpegAudioTranscoder:
    """Create bounded mono 16 kHz PCM without exposing a remote address."""

    def __init__(self, *, timeout_seconds: float = 90.0, executable: str = "ffmpeg") -> None:
        self.timeout_seconds = float(timeout_seconds)
        self.executable = executable

    def __call__(self, source: Path, destination: Path) -> None:
        executable = shutil.which(self.executable)
        if not executable:
            raise SonicTransportError("ffmpeg_not_available")
        try:
            subprocess.run(
                [
                    executable,
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    os.fspath(source),
                    "-map_metadata",
                    "-1",
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-c:a",
                    "pcm_s16le",
                    os.fspath(destination),
                ],
                check=True,
                capture_output=True,
                timeout=self.timeout_seconds,
            )
        except Exception as exc:
            with contextlib.suppress(OSError):
                destination.unlink()
            raise SonicTransportError("ffmpeg_audio_transcode_failed") from exc


def inspect_pcm_wave(path: Path) -> MediaProbe:
    try:
        with wave.open(os.fspath(path), "rb") as source:
            frame_rate = int(source.getframerate())
            frames = int(source.getnframes())
            channels = int(source.getnchannels())
            sample_width = int(source.getsampwidth())
    except Exception as exc:
        raise SonicTransportError("decoded_audio_is_not_valid_pcm_wave") from exc
    if frame_rate != 16000 or channels != 1 or sample_width != 2 or frames < 1:
        raise SonicTransportError("decoded_audio_format_mismatch")
    return MediaProbe(frames / frame_rate, path.stat().st_size)


def _validate_probe(probe: MediaProbe, limits: AcquisitionLimits, *, audio: bool) -> None:
    try:
        duration = float(probe.duration_seconds)
        byte_count = int(probe.byte_count)
    except (TypeError, ValueError) as exc:
        raise SonicTransportError("media_probe_result_invalid") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise SonicTransportError("media_duration_unavailable")
    if duration > limits.max_duration_seconds:
        raise TransportLimitError("media_duration_limit_exceeded")
    max_bytes = limits.max_audio_bytes if audio else limits.max_source_bytes
    if byte_count < 1 or byte_count > max_bytes:
        raise TransportLimitError("media_probe_size_limit_exceeded")


def _safe_derived_result(value: Any, run_dir: Path) -> Any:
    """Reject accidental transport secret/path leakage from a callback result."""

    run_path = os.path.normcase(os.fspath(run_dir.resolve()))

    def visit(current: Any, key: str = "") -> Any:
        lowered = key.casefold()
        if any(fragment in lowered for fragment in _FORBIDDEN_RESULT_KEYS):
            raise SonicTransportError("derived_result_contains_transport_field")
        if current is None or isinstance(current, (bool, int)):
            return current
        if isinstance(current, float):
            if not math.isfinite(current):
                raise SonicTransportError("derived_result_contains_nonfinite_number")
            return current
        if isinstance(current, str):
            normalized = os.path.normcase(current)
            if current.startswith(("http://", "https://")) or run_path in normalized:
                raise SonicTransportError("derived_result_contains_transport_value")
            return current
        if isinstance(current, Mapping):
            return {str(item_key): visit(item, str(item_key)) for item_key, item in current.items()}
        if isinstance(current, (list, tuple)):
            return [visit(item) for item in current]
        raise SonicTransportError("derived_result_must_be_json_safe")

    return visit(value)


def _safe_error_code(error: SonicTransportError) -> str:
    candidate = _text(error).casefold()
    return candidate if _SAFE_ERROR_CODE.fullmatch(candidate) else "transport_error"


def _safe_provenance(
    observed_account: str,
    *,
    authenticated_media_request: bool = False,
    audio_conversion: str = DEFAULT_AUDIO_CONVERSION,
) -> dict[str, Any]:
    conversion = _text(audio_conversion).casefold()
    if (
        _SAFE_AUDIO_CONVERSION.fullmatch(conversion) is None
        or conversion not in ALLOWED_AUDIO_CONVERSIONS
    ):
        raise SonicTransportError("audio_conversion_provenance_invalid")
    return {
        "browser_profile": "Profile 7",
        "browser_mode": "existing_profile_attach",
        "account_handle": observed_account,
        "identity_binding": "exact_post_id_and_creator_from_tiktok_html",
        "metadata_transport": "authenticated_exact_post_html_in_memory",
        "media_transport": (
            "bounded_authenticated_tiktok_range_stream"
            if authenticated_media_request
            else "bounded_injected_test_stream"
        ),
        "audio_conversion": conversion,
        "cookies_forwarded_to_media": bool(authenticated_media_request),
        "signed_url_retained": False,
        "raw_payload_retained": False,
        "temporary_media_retained": False,
        "timing_clock": "monotonic",
    }


@dataclass
class SonicAudioTransport:
    """Executor-friendly, callback-streaming transport for up to 60 posts."""

    state_path: Path = DEFAULT_BROWSER_STATE
    temp_root: Path | None = None
    limits: AcquisitionLimits = field(default_factory=AcquisitionLimits)
    preflight: Any = None
    media_fetcher: MediaFetcher | None = None
    media_inspector: MediaInspector | None = None
    audio_transcoder: AudioTranscoder | None = None
    audio_inspector: MediaInspector = inspect_pcm_wave
    audio_conversion: str = DEFAULT_AUDIO_CONVERSION
    playwright_manager_factory: Callable[[], Any] | None = None
    cdp_url_provider: Callable[[], str] | None = None
    designation_loader: Callable[[Path], Any] | None = None
    profile_context_verifier: Callable[..., Any] | None = None
    authentication_checker: Callable[..., Any] | None = None
    last_run_timing: dict[str, float] = field(
        default_factory=dict,
        init=False,
    )
    last_cleanup_verified: bool = field(default=False, init=False)

    def cleanup_stale_run(self, run_id: str) -> bool:
        """Browser-free exact-run cleanup for start/resume/no-pending paths."""

        self.last_cleanup_verified = False
        cleaned = cleanup_stale_run(run_id, temp_root=self.temp_root)
        self.last_cleanup_verified = bool(
            cleaned
            and verify_no_run_residue(run_id, temp_root=self.temp_root)
        )
        return self.last_cleanup_verified

    def verify_no_run_residue(self, run_id: str) -> bool:
        """Browser-free residue gate for this transport's temp root."""

        return verify_no_run_residue(run_id, temp_root=self.temp_root)

    async def _fetch_exact_item(
        self,
        page: Any,
        candidate: FrozenPostCandidate,
    ) -> Mapping[str, Any]:
        response = None
        try:
            from tiktok_scraper.tiktok_subtitles import extract_tiktok_item_from_html

            item: Mapping[str, Any] | None = None
            navigation = getattr(page, "goto", None)
            if callable(navigation):
                # TikTok's bare request context currently returns a tiny shell
                # without the exact item payload. A real navigation in the
                # already-verified Profile 7 tab materializes the bounded
                # rehydration script while retaining the same frozen URL gate.
                try:
                    response = await navigation(
                        candidate.canonical_url,
                        wait_until="domcontentloaded",
                        timeout=int(self.limits.request_timeout_seconds * 1000),
                    )
                except Exception:
                    raise SonicTransportError(
                        "exact_post_navigation_failed"
                    ) from None
                if response is not None and not getattr(response, "ok", False):
                    raise SonicTransportError("exact_post_html_unavailable")
                html_text = ""
                if response is not None:
                    try:
                        html_text = await response.text()
                    except Exception:
                        raise SonicTransportError("exact_post_html_read_failed") from None
                    if len(html_text.encode("utf-8")) > self.limits.max_html_bytes:
                        raise TransportLimitError("exact_post_html_size_limit_exceeded")
                    parsed_item = extract_tiktok_item_from_html(
                        html_text,
                        candidate.post_id,
                    )
                    if isinstance(parsed_item, Mapping):
                        item = parsed_item
                if item is None:
                    # TikTok intermittently serves the fully rendered post
                    # while omitting the rehydration object from the first
                    # navigation response. One exact-URL reload is a bounded
                    # retry; it neither discovers nor substitutes content.
                    reload_page = getattr(page, "reload", None)
                    try:
                        if callable(reload_page):
                            retry_response = await reload_page(
                                wait_until="domcontentloaded",
                                timeout=int(
                                    self.limits.request_timeout_seconds * 1000
                                ),
                            )
                        else:
                            retry_response = await navigation(
                                candidate.canonical_url,
                                wait_until="domcontentloaded",
                                timeout=int(
                                    self.limits.request_timeout_seconds * 1000
                                ),
                            )
                    except Exception:
                        retry_response = None
                    if retry_response is not None and getattr(
                        retry_response,
                        "ok",
                        False,
                    ):
                        response = retry_response
                        try:
                            html_text = await response.text()
                        except Exception:
                            raise SonicTransportError(
                                "exact_post_html_read_failed"
                            ) from None
                        if len(html_text.encode("utf-8")) > self.limits.max_html_bytes:
                            raise TransportLimitError(
                                "exact_post_html_size_limit_exceeded"
                            )
                        parsed_item = extract_tiktok_item_from_html(
                            html_text,
                            candidate.post_id,
                        )
                        if isinstance(parsed_item, Mapping):
                            item = parsed_item
                if item is None:
                    wait_for_selector = getattr(page, "wait_for_selector", None)
                    if callable(wait_for_selector):
                        with contextlib.suppress(Exception):
                            await wait_for_selector(
                                "script#__UNIVERSAL_DATA_FOR_REHYDRATION__, script#SIGI_STATE",
                                state="attached",
                                timeout=min(
                                    8000,
                                    int(self.limits.request_timeout_seconds * 1000),
                                ),
                            )
                    deadline = time.monotonic() + min(
                        15.0,
                        self.limits.request_timeout_seconds,
                    )
                    while True:
                        try:
                            html_text = await page.content()
                        except Exception:
                            raise SonicTransportError("exact_post_html_read_failed") from None
                        if len(html_text.encode("utf-8")) > self.limits.max_html_bytes:
                            raise TransportLimitError("exact_post_html_size_limit_exceeded")
                        parsed_item = extract_tiktok_item_from_html(
                            html_text,
                            candidate.post_id,
                        )
                        if isinstance(parsed_item, Mapping):
                            item = parsed_item
                            break
                        if time.monotonic() >= deadline:
                            break
                        await page.wait_for_timeout(500)
            else:
                # Test seam for a bounded response-only page double.
                try:
                    response = await page.request.get(
                        candidate.canonical_url,
                        timeout=int(self.limits.request_timeout_seconds * 1000),
                    )
                except Exception:
                    raise SonicTransportError("exact_post_html_request_failed") from None
                if not getattr(response, "ok", False):
                    raise SonicTransportError("exact_post_html_unavailable")
                try:
                    html_text = await response.text()
                except Exception:
                    raise SonicTransportError("exact_post_html_read_failed") from None
                parsed_item = extract_tiktok_item_from_html(
                    html_text,
                    candidate.post_id,
                )
                if isinstance(parsed_item, Mapping):
                    item = parsed_item
            headers = getattr(response, "headers", {}) or {}
            declared = _text(headers.get("content-length") or headers.get("Content-Length"))
            if declared:
                try:
                    if int(declared) > self.limits.max_html_bytes:
                        raise TransportLimitError("exact_post_html_size_limit_exceeded")
                except ValueError as exc:
                    raise SonicTransportError("exact_post_html_length_invalid") from exc
            if len(html_text.encode("utf-8")) > self.limits.max_html_bytes:
                raise TransportLimitError("exact_post_html_size_limit_exceeded")
            if not isinstance(item, Mapping):
                raise SonicTransportError("exact_post_item_metadata_unavailable")
            observed_id, observed_creator = _item_identity(item)
            if observed_id != candidate.post_id:
                raise CandidateIdentityError("tiktok_response_post_id_mismatch")
            if observed_creator != candidate.creator_handle:
                raise CandidateIdentityError("tiktok_response_creator_mismatch")
            return item
        finally:
            dispose = getattr(response, "dispose", None)
            if callable(dispose):
                with contextlib.suppress(Exception):
                    await _maybe_await(dispose())

    async def _process_one(
        self,
        page: Any,
        candidate: FrozenPostCandidate,
        processor: Processor,
        run_dir: Path,
        *,
        observed_account: str,
        remaining_duration_seconds: float,
        timing: dict[str, float],
        run_id: str,
    ) -> SonicTransportReceipt:
        item_dir = Path(tempfile.mkdtemp(prefix=f"item-{candidate.post_id}-", dir=run_dir))
        _assert_owned_item(item_dir, run_dir, run_id)
        source_path = item_dir / "source-media.bin"
        audio_path = item_dir / "decoded-audio.wav"
        try:
            stage_started = time.monotonic()
            try:
                item = await self._fetch_exact_item(page, candidate)
            finally:
                timing["html_fetch_ms"] = _elapsed_ms(stage_started)
            media_url = extract_transient_media_url(item)
            authenticated_media_request = self.media_fetcher is None
            fetcher = self.media_fetcher or PlaywrightBoundedMediaFetcher(
                page.request,
                self.limits,
            )
            stage_started = time.monotonic()
            try:
                supplied_receipt = await _maybe_await(
                    fetcher(
                        media_url,
                        source_path,
                        referer=candidate.canonical_url,
                    )
                )
            finally:
                timing["media_download_ms"] = _elapsed_ms(stage_started)
            del media_url
            if not isinstance(supplied_receipt, DownloadReceipt):
                raise SonicTransportError("media_fetcher_receipt_invalid")
            source_bytes, source_hash = _file_hash(
                source_path,
                max_bytes=self.limits.max_source_bytes,
            )
            if (
                supplied_receipt.byte_count != source_bytes
                or supplied_receipt.sha256 != source_hash
            ):
                raise SonicTransportError("media_fetcher_receipt_mismatch")

            stage_started = time.monotonic()
            try:
                inspector = self.media_inspector or FFprobeMediaInspector(
                    timeout_seconds=min(
                        30.0,
                        self.limits.transcode_timeout_seconds,
                    )
                )
                source_probe = await _maybe_await(inspector(source_path))
                if not isinstance(source_probe, MediaProbe):
                    raise SonicTransportError("media_probe_result_invalid")
                _validate_probe(source_probe, self.limits, audio=False)
                if source_probe.byte_count != source_bytes:
                    raise SonicTransportError("media_probe_size_mismatch")
                if source_probe.duration_seconds > remaining_duration_seconds:
                    raise TransportLimitError("pilot_total_duration_limit_exceeded")

                transcoder = self.audio_transcoder or FFmpegAudioTranscoder(
                    timeout_seconds=self.limits.transcode_timeout_seconds
                )
                await _maybe_await(transcoder(source_path, audio_path))
                if not audio_path.is_file():
                    raise SonicTransportError("decoded_audio_missing")
                audio_probe = await _maybe_await(self.audio_inspector(audio_path))
                if not isinstance(audio_probe, MediaProbe):
                    raise SonicTransportError("audio_probe_result_invalid")
                _validate_probe(audio_probe, self.limits, audio=True)
                if abs(
                    audio_probe.duration_seconds - source_probe.duration_seconds
                ) > max(1.0, source_probe.duration_seconds * 0.05):
                    raise SonicTransportError("decoded_audio_duration_mismatch")
                audio_bytes, audio_hash = _file_hash(
                    audio_path,
                    max_bytes=self.limits.max_audio_bytes,
                )
                if audio_bytes != int(audio_probe.byte_count):
                    raise SonicTransportError("decoded_audio_size_mismatch")
            finally:
                timing["inspect_transcode_ms"] = _elapsed_ms(stage_started)

            provenance = _safe_provenance(
                observed_account,
                authenticated_media_request=authenticated_media_request,
                audio_conversion=self.audio_conversion,
            )
            transient = TransientAudioItem(
                post_id=candidate.post_id,
                creator_handle=candidate.creator_handle,
                source_media_path=source_path,
                audio_path=audio_path,
                duration_seconds=audio_probe.duration_seconds,
                source_byte_count=source_bytes,
                audio_byte_count=audio_bytes,
                source_sha256=source_hash,
                audio_sha256=audio_hash,
                provenance=provenance,
                timing=dict(timing),
            )
            stage_started = time.monotonic()
            try:
                derived = _safe_derived_result(
                    await _maybe_await(processor(transient)),
                    run_dir,
                )
            finally:
                timing["processor_ms"] = _elapsed_ms(stage_started)
            return SonicTransportReceipt(
                post_id=candidate.post_id,
                creator_handle=candidate.creator_handle,
                duration_seconds=audio_probe.duration_seconds,
                source_byte_count=source_bytes,
                audio_byte_count=audio_bytes,
                source_sha256=source_hash,
                audio_sha256=audio_hash,
                provenance=provenance,
                timing=dict(timing),
                derived_result=derived,
            )
        finally:
            _remove_owned_item(item_dir, run_dir, run_id)

    async def _process_with_verified_page(
        self,
        *,
        run_id: str,
        page: Any,
        candidates: Sequence[FrozenPostCandidate],
        processor: Processor,
        observed_account: str,
        continue_on_item_error: bool = False,
        error_processor: ErrorProcessor | None = None,
        receipt_processor: ReceiptProcessor | None = None,
    ) -> list[SonicTransportReceipt]:
        transport_started = time.monotonic()
        self.last_cleanup_verified = False
        run_id = _validate_run_id(run_id)
        temp_root = _validate_temp_root(self.temp_root)
        if not cleanup_stale_run(run_id, temp_root=temp_root):
            raise SonicTransportError("stale_run_residue_unverified")
        if not verify_no_run_residue(run_id, temp_root=temp_root):
            raise SonicTransportError("stale_run_residue_unverified")
        run_dir = _create_owned_run_directory(temp_root, run_id)
        receipts: list[SonicTransportReceipt] = []
        duration_total = 0.0
        try:
            for candidate in candidates:
                timing = _item_timing()
                item_started = time.monotonic()
                try:
                    receipt = await self._process_one(
                        page,
                        candidate,
                        processor,
                        run_dir,
                        observed_account=observed_account,
                        remaining_duration_seconds=(
                            self.limits.max_total_duration_seconds - duration_total
                        ),
                        timing=timing,
                        run_id=run_id,
                    )
                except SonicTransportError as exc:
                    code = _safe_error_code(exc)
                    if (
                        not continue_on_item_error
                        or code == "pilot_total_duration_limit_exceeded"
                    ):
                        raise
                    identity = SafeCandidateIdentity(
                        candidate.post_id,
                        candidate.creator_handle,
                        candidate.content_type,
                    )
                    error_result = None
                    if error_processor is not None:
                        error_result = _safe_derived_result(
                            await _maybe_await(error_processor(identity, code)),
                            run_dir,
                        )
                    timing["total_item_ms"] = _elapsed_ms(item_started)
                    receipt = SonicTransportReceipt(
                        post_id=candidate.post_id,
                        creator_handle=candidate.creator_handle,
                        duration_seconds=0.0,
                        source_byte_count=0,
                        audio_byte_count=0,
                        source_sha256="",
                        audio_sha256="",
                        provenance=_safe_provenance(
                            observed_account,
                            authenticated_media_request=self.media_fetcher is None,
                            audio_conversion=self.audio_conversion,
                        ),
                        timing=dict(timing),
                        derived_result=error_result,
                        status="unavailable",
                        error_code=code,
                    )
                    if receipt_processor is not None:
                        await _maybe_await(receipt_processor(receipt))
                    receipts.append(receipt)
                    continue
                timing["total_item_ms"] = _elapsed_ms(item_started)
                receipt = replace(receipt, timing=dict(timing))
                duration_total += receipt.duration_seconds
                if duration_total > self.limits.max_total_duration_seconds:
                    raise TransportLimitError("pilot_total_duration_limit_exceeded")
                if receipt_processor is not None:
                    await _maybe_await(receipt_processor(receipt))
                receipts.append(receipt)
            return receipts
        finally:
            try:
                _remove_owned_run(run_dir, temp_root, run_id)
            finally:
                self.last_cleanup_verified = verify_no_run_residue(
                    run_id,
                    temp_root=temp_root,
                )
                self.last_run_timing["transport_ms"] = _elapsed_ms(
                    transport_started
                )

    async def process_candidates(
        self,
        *,
        run_id: str,
        candidates: Sequence[Mapping[str, Any]],
        processor: Processor,
        expected_account: str = "",
        continue_on_item_error: bool = False,
        error_processor: ErrorProcessor | None = None,
        receipt_processor: ReceiptProcessor | None = None,
    ) -> list[SonicTransportReceipt]:
        """Process frozen exact posts sequentially, cleaning each after callback.

        This is the production entry point.  It always executes Profile 7
        startup/reuse and then independently revalidates the attached context
        and TikTok authentication before opening one temporary page.  An
        optional ``receipt_processor`` runs only after that item's files have
        been removed and before the next frozen candidate starts.
        """
        run_started = time.monotonic()
        self.last_cleanup_verified = False
        self.last_run_timing = {
            "preflight_ms": 0.0,
            "attach_ms": 0.0,
            "transport_ms": 0.0,
            "total_run_ms": 0.0,
        }
        run_id = _validate_run_id(run_id)
        if not cleanup_stale_run(run_id, temp_root=self.temp_root):
            raise SonicTransportError("stale_run_residue_unverified")
        frozen = validate_frozen_candidates(candidates, limits=self.limits)
        from engage_tiktok import SocialBrowserPreflight, TikTokBrowserCollector
        from social_browser import (
            load_engage_profile7_designation,
            platform_authentication,
            verified_profile_context,
        )

        preflight = self.preflight or SocialBrowserPreflight(
            state_path=self.state_path,
            expected_account=expected_account,
        )
        stage_started = time.monotonic()
        try:
            ready = await preflight.ensure_ready()
        finally:
            self.last_run_timing["preflight_ms"] = _elapsed_ms(stage_started)
        profile = ready.get("profile") if isinstance(ready, Mapping) else {}
        observed_account = _normalize_handle(
            ready.get("observed_account") if isinstance(ready, Mapping) else ""
        )
        if (
            not isinstance(profile, Mapping)
            or profile.get("verified") is not True
            or _text(profile.get("profile_directory")).casefold() != "profile 7"
        ):
            raise SonicTransportError("preflight_did_not_verify_profile_7")
        if not observed_account:
            raise SonicTransportError("preflight_did_not_resolve_tiktok_account")
        normalized_expected = _normalize_handle(expected_account)
        if normalized_expected and observed_account != normalized_expected:
            raise SonicTransportError("preflight_tiktok_account_mismatch")

        cdp_provider = self.cdp_url_provider
        if cdp_provider is None:
            browser_collector = TikTokBrowserCollector(state_path=self.state_path)
            cdp_provider = browser_collector._cdp_url
        cdp_url = _text(cdp_provider())
        if not cdp_url:
            raise SonicTransportError("verified_profile_7_endpoint_missing")
        designation_loader = self.designation_loader or load_engage_profile7_designation
        context_verifier = self.profile_context_verifier or verified_profile_context
        authentication_checker = self.authentication_checker or platform_authentication
        designation = designation_loader(Path(self.state_path).resolve().parent)

        if self.playwright_manager_factory is None:
            from playwright.async_api import async_playwright

            manager_factory = async_playwright
        else:
            manager_factory = self.playwright_manager_factory

        try:
            async with manager_factory() as playwright:
                attach_started = time.monotonic()
                try:
                    browser = await playwright.chromium.connect_over_cdp(cdp_url)
                    if not getattr(browser, "contexts", None):
                        raise SonicTransportError("verified_profile_7_has_no_context")
                    try:
                        context, _ = await _maybe_await(
                            context_verifier(browser, designation)
                        )
                    except Exception as exc:
                        raise SonicTransportError(
                            "profile_7_identity_revalidation_failed"
                        ) from exc
                    authentication = await _maybe_await(
                        authentication_checker(context, "tiktok")
                    )
                    if (
                        not isinstance(authentication, Mapping)
                        or not authentication.get("authenticated")
                    ):
                        raise SonicTransportError("tiktok_logged_out_in_profile_7")
                    page = await context.new_page()
                finally:
                    self.last_run_timing["attach_ms"] = _elapsed_ms(attach_started)
                try:
                    return await self._process_with_verified_page(
                        run_id=run_id,
                        page=page,
                        candidates=frozen,
                        processor=processor,
                        observed_account=observed_account,
                        continue_on_item_error=continue_on_item_error,
                        error_processor=error_processor,
                        receipt_processor=receipt_processor,
                    )
                finally:
                    if not page.is_closed():
                        await page.close()
                # Never call browser.close(): it is the shared Profile 7 process.
        finally:
            self.last_run_timing["total_run_ms"] = _elapsed_ms(run_started)


__all__ = [
    "AcquisitionLimits",
    "CandidateIdentityError",
    "DownloadReceipt",
    "FrozenPostCandidate",
    "MAX_PILOT_POSTS",
    "MediaProbe",
    "PlaywrightBoundedMediaFetcher",
    "RequestsBoundedMediaFetcher",
    "SafeCandidateIdentity",
    "SonicAudioTransport",
    "SonicTransportError",
    "SonicTransportReceipt",
    "TransientAudioItem",
    "TransportLimitError",
    "UnsupportedMediaError",
    "extract_transient_media_url",
    "cleanup_stale_run",
    "inspect_pcm_wave",
    "validate_frozen_candidates",
    "verify_no_run_residue",
]
