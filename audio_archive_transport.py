#!/usr/bin/env python
"""Persistent M4A + MP3 adapter over the bounded SONIC audio transport.

The SONIC transport remains the sole owner of TikTok/Profile 7 access and of
all source-video and conversion-temporary files.  This adapter copies only the
already-normalized audio during SONIC's per-item callback.  The copy is first
sealed into an archive-run-owned staging file.  SONIC then removes its source
and audio temporaries before invoking the adapter's receipt callback, where the
staged M4A and its locally derived MP3 are committed as one fail-closed pair
without replacing an existing destination.  MP3 creation never performs a
second TikTok request.

No remote address, request header, cookie, raw TikTok payload, or source video
crosses this module's result boundary.
"""

from __future__ import annotations

import contextlib
import hashlib
import inspect
import json
import math
import os
import re
import shutil
import stat
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Sequence

from sonic_audio_transport import (
    AcquisitionLimits,
    DEFAULT_BROWSER_STATE,
    MAX_PILOT_POSTS,
    MediaProbe,
    SonicAudioTransport,
    SonicTransportError,
    SonicTransportReceipt,
    TransientAudioItem,
    validate_frozen_candidates,
)


ARCHIVE_AUDIO_CONVERSION = (
    "local_ffmpeg_aac_lc_stereo_44100hz_192kbps_ipod_m4a"
)
ARCHIVE_MP3_CONVERSION = "local_ffmpeg_libmp3lame_stereo_44100hz_192kbps_mp3"
ARCHIVE_AUDIO_CAP_BYTES = 16 * 1024 * 1024
_ARCHIVE_RUN_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,191}$")
_POST_ID = re.compile(r"^[0-9]{1,32}$")
_STAGING_SCHEMA = "tiktok-audio-archive-staging-v1"
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class AudioArchiveTransportError(SonicTransportError):
    """A fail-closed archive copy or commit failure."""


@dataclass(frozen=True)
class ArchiveAudioReceipt:
    """Safe receipt for one exact matched archive file pair."""

    archive_run_id: str
    transport_owner_run_id: str
    post_id: str
    creator_handle: str
    duration_seconds: float
    files: Mapping[str, Mapping[str, Any]]
    provenance: Mapping[str, Any]
    timing: Mapping[str, float] = field(default_factory=dict)
    status: str = "completed"
    error_code: str = ""


ArchiveReceiptProcessor = Callable[
    [ArchiveAudioReceipt], Any | Awaitable[Any]
]


@dataclass(frozen=True)
class _DestinationBinding:
    post_id: str
    audio_format: str
    path: Path
    parent_path: Path
    resolved_parent: Path
    parent_device: int
    parent_inode: int


@dataclass(frozen=True)
class _StagedAudio:
    post_id: str
    audio_format: str
    path: Path
    destination: _DestinationBinding
    byte_count: int
    sha256: str
    duration_seconds: float


def _text(value: Any) -> str:
    return str(value or "").strip()


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _validate_archive_run_id(value: Any) -> str:
    run_id = _text(value)
    if _ARCHIVE_RUN_ID.fullmatch(run_id) is None:
        raise AudioArchiveTransportError("invalid_audio_archive_run_id")
    return run_id


def sonic_owner_run_id(archive_run_id: Any) -> str:
    """Map one archive identity to the exact SONIC temporary-owner shape."""

    run_id = _validate_archive_run_id(archive_run_id)
    digest = hashlib.sha256(
        ("tiktok-audio-archive-temp-owner-v1\0" + run_id).encode("utf-8")
    ).hexdigest()
    return f"sonic_{digest[:16]}"


def _is_link_or_reparse(path: Path) -> bool:
    try:
        details = path.lstat()
    except OSError as exc:
        raise AudioArchiveTransportError(
            "archive_destination_parent_unavailable"
        ) from exc
    return bool(
        stat.S_ISLNK(details.st_mode)
        or int(getattr(details, "st_file_attributes", 0)) & _REPARSE_POINT
    )


def _validate_parent_chain(parent: Path) -> tuple[Path, os.stat_result]:
    if not parent.is_absolute():
        raise AudioArchiveTransportError("archive_destination_must_be_absolute")
    if not parent.exists() or not parent.is_dir():
        raise AudioArchiveTransportError(
            "archive_destination_parent_missing"
        )
    for current in (parent, *parent.parents):
        if _is_link_or_reparse(current):
            raise AudioArchiveTransportError(
                "archive_destination_parent_is_link_or_reparse_point"
            )
    try:
        resolved = parent.resolve(strict=True)
        details = parent.stat(follow_symlinks=False)
    except OSError as exc:
        raise AudioArchiveTransportError(
            "archive_destination_parent_unavailable"
        ) from exc
    if resolved != parent.resolve(strict=True):
        raise AudioArchiveTransportError("archive_destination_parent_changed")
    return resolved, details


def _lexists(path: Path) -> bool:
    return os.path.lexists(os.fspath(path))


def _bind_destination(
    post_id: str,
    audio_format: str,
    value: Path | str,
) -> _DestinationBinding:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise AudioArchiveTransportError("archive_destination_must_be_absolute")
    if audio_format not in {"m4a", "mp3"}:
        raise AudioArchiveTransportError("archive_destination_format_invalid")
    if path.name in {"", ".", ".."} or path.suffix.casefold() != f".{audio_format}":
        raise AudioArchiveTransportError(
            f"archive_destination_must_be_{audio_format}"
        )
    parent = path.parent
    resolved, details = _validate_parent_chain(parent)
    if _lexists(path):
        raise AudioArchiveTransportError("archive_destination_already_exists")
    return _DestinationBinding(
        post_id=post_id,
        audio_format=audio_format,
        path=path,
        parent_path=parent,
        resolved_parent=resolved,
        parent_device=int(details.st_dev),
        parent_inode=int(details.st_ino),
    )


def _revalidate_destination(binding: _DestinationBinding) -> None:
    resolved, details = _validate_parent_chain(binding.parent_path)
    if (
        resolved != binding.resolved_parent
        or int(details.st_dev) != binding.parent_device
        or int(details.st_ino) != binding.parent_inode
    ):
        raise AudioArchiveTransportError("archive_destination_parent_changed")


def _file_hash(path: Path, *, max_bytes: int) -> tuple[int, str]:
    digest = hashlib.sha256()
    byte_count = 0
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(64 * 1024)
                if not chunk:
                    break
                byte_count += len(chunk)
                if byte_count > max_bytes:
                    raise AudioArchiveTransportError(
                        "archive_audio_size_limit_exceeded"
                    )
                digest.update(chunk)
    except AudioArchiveTransportError:
        raise
    except OSError as exc:
        raise AudioArchiveTransportError("archive_audio_read_failed") from exc
    return byte_count, digest.hexdigest()


def _owned_stage_names(
    owner_run_id: str,
    post_id: str,
    audio_format: str,
) -> tuple[str, str]:
    nonce = uuid.uuid4().hex[:16]
    stem = (
        f".audio-archive-{owner_run_id[6:]}-{post_id}-"
        f"{audio_format}-{nonce}"
    )
    return f"{stem}.partial", f"{stem}.staged"


def _copy_to_staging(
    item: TransientAudioItem,
    binding: _DestinationBinding,
    *,
    owner_run_id: str,
    owned_paths: set[Path],
    staging_parent: Path,
    staging_parent_identity: tuple[Path, int, int],
) -> _StagedAudio:
    _revalidate_destination(binding)
    resolved_staging, staging_details = _validate_parent_chain(staging_parent)
    if (
        resolved_staging != staging_parent_identity[0]
        or int(staging_details.st_dev) != staging_parent_identity[1]
        or int(staging_details.st_ino) != staging_parent_identity[2]
    ):
        raise AudioArchiveTransportError("archive_staging_parent_changed")
    if _lexists(binding.path):
        raise AudioArchiveTransportError("archive_destination_already_exists")
    source = Path(item.audio_path)
    try:
        source_details = source.lstat()
    except OSError as exc:
        raise AudioArchiveTransportError("archive_transcoded_audio_missing") from exc
    if (
        not stat.S_ISREG(source_details.st_mode)
        or _is_link_or_reparse(source)
    ):
        raise AudioArchiveTransportError("archive_transcoded_audio_not_regular")

    partial_name, staged_name = _owned_stage_names(
        owner_run_id,
        item.post_id,
        binding.audio_format,
    )
    partial = staging_parent / partial_name
    staged = staging_parent / staged_name
    owned_paths.update((partial, staged))
    digest = hashlib.sha256()
    byte_count = 0
    try:
        with source.open("rb") as input_handle, partial.open("xb") as output_handle:
            while True:
                chunk = input_handle.read(64 * 1024)
                if not chunk:
                    break
                byte_count += len(chunk)
                if byte_count > ARCHIVE_AUDIO_CAP_BYTES:
                    raise AudioArchiveTransportError(
                        "archive_audio_size_limit_exceeded"
                    )
                output_handle.write(chunk)
                digest.update(chunk)
            output_handle.flush()
            os.fsync(output_handle.fileno())
        if (
            byte_count != int(item.audio_byte_count)
            or digest.hexdigest() != _text(item.audio_sha256)
        ):
            raise AudioArchiveTransportError("archive_staging_copy_mismatch")
        os.replace(partial, staged)
        staged_bytes, staged_hash = _file_hash(
            staged,
            max_bytes=ARCHIVE_AUDIO_CAP_BYTES,
        )
        if staged_bytes != byte_count or staged_hash != digest.hexdigest():
            raise AudioArchiveTransportError("archive_staging_validation_failed")
    except AudioArchiveTransportError:
        with contextlib.suppress(OSError):
            staged.unlink()
        raise
    except FileExistsError as exc:
        with contextlib.suppress(OSError):
            staged.unlink()
        raise AudioArchiveTransportError("archive_staging_collision") from exc
    except OSError as exc:
        with contextlib.suppress(OSError):
            staged.unlink()
        raise AudioArchiveTransportError("archive_staging_copy_failed") from exc
    finally:
        with contextlib.suppress(OSError):
            partial.unlink()
    return _StagedAudio(
        post_id=item.post_id,
        audio_format=binding.audio_format,
        path=staged,
        destination=binding,
        byte_count=staged_bytes,
        sha256=staged_hash,
        duration_seconds=float(item.duration_seconds),
    )


def _remove_owned_stage(path: Path, owned_paths: set[Path]) -> None:
    if path not in owned_paths:
        raise AudioArchiveTransportError("archive_staging_ownership_invalid")
    with contextlib.suppress(FileNotFoundError):
        path.unlink()


def _promote_staging_pair(
    staged_files: Mapping[str, _StagedAudio],
    *,
    owned_paths: set[Path],
) -> None:
    if set(staged_files) != {"m4a", "mp3"}:
        raise AudioArchiveTransportError("archive_staging_pair_incomplete")
    ordered = [staged_files["m4a"], staged_files["mp3"]]
    for staged in ordered:
        binding = staged.destination
        _revalidate_destination(binding)
        if staged.audio_format != binding.audio_format:
            raise AudioArchiveTransportError("archive_staging_format_mismatch")
        if staged.path not in owned_paths or not staged.path.is_file():
            raise AudioArchiveTransportError("archive_staging_file_missing")
        if _is_link_or_reparse(staged.path):
            raise AudioArchiveTransportError("archive_staging_file_is_link")
        if _lexists(binding.path):
            raise AudioArchiveTransportError("archive_destination_already_exists")

    created: list[_StagedAudio] = []
    try:
        # Each name is created atomically and never replaces an existing file.
        # If the second link or either verification fails, every name created
        # by this operation is rolled back, leaving no half-pair.
        for staged in ordered:
            binding = staged.destination
            os.link(
                os.fspath(staged.path),
                os.fspath(binding.path),
                follow_symlinks=False,
            )
            created.append(staged)
            if not binding.path.is_file() or _is_link_or_reparse(binding.path):
                raise AudioArchiveTransportError(
                    "archive_destination_commit_invalid"
                )
            if not os.path.samefile(staged.path, binding.path):
                raise AudioArchiveTransportError(
                    "archive_destination_commit_mismatch"
                )
            byte_count, digest = _file_hash(
                binding.path,
                max_bytes=ARCHIVE_AUDIO_CAP_BYTES,
            )
            if byte_count != staged.byte_count or digest != staged.sha256:
                raise AudioArchiveTransportError(
                    "archive_destination_hash_mismatch"
                )
    except FileExistsError as exc:
        for created_file in reversed(created):
            with contextlib.suppress(OSError):
                if os.path.samefile(
                    created_file.path,
                    created_file.destination.path,
                ):
                    created_file.destination.path.unlink()
        raise AudioArchiveTransportError("archive_destination_already_exists") from exc
    except AudioArchiveTransportError:
        for created_file in reversed(created):
            with contextlib.suppress(OSError):
                if os.path.samefile(
                    created_file.path,
                    created_file.destination.path,
                ):
                    created_file.destination.path.unlink()
        raise
    except OSError as exc:
        for created_file in reversed(created):
            with contextlib.suppress(OSError):
                if os.path.samefile(
                    created_file.path,
                    created_file.destination.path,
                ):
                    created_file.destination.path.unlink()
        raise AudioArchiveTransportError("archive_destination_commit_failed") from exc
    finally:
        for staged in ordered:
            _remove_owned_stage(staged.path, owned_paths)


def _archive_provenance(
    source: Mapping[str, Any],
    *,
    retained: bool,
) -> dict[str, Any]:
    conversion = _text(source.get("audio_conversion"))
    if conversion != ARCHIVE_AUDIO_CONVERSION:
        raise AudioArchiveTransportError("archive_audio_conversion_mismatch")
    return {
        "schema_version": "tiktok-audio-archive-transport-v1",
        "browser_profile": _text(source.get("browser_profile")),
        "browser_mode": _text(source.get("browser_mode")),
        "account_handle": _text(source.get("account_handle")),
        "identity_binding": _text(source.get("identity_binding")),
        "media_transport": _text(source.get("media_transport")),
        "audio_conversion": conversion,
        "primary_audio_conversion": conversion,
        "derived_audio_conversion": ARCHIVE_MP3_CONVERSION,
        "retained_formats": ["m4a", "mp3"] if retained else [],
        "mp3_derived_from_normalized_m4a": True,
        "source_video_retained": False,
        "signed_address_retained": False,
        "request_credentials_retained": False,
        "persistent_audio_retained": bool(retained),
        "staging_schema": _STAGING_SCHEMA,
    }


class ArchiveM4ATranscoder:
    """Normalize one local input to metadata-free AAC-LC in an M4A container."""

    def __init__(self, *, timeout_seconds: float = 90.0, executable: str = "ffmpeg") -> None:
        self.timeout_seconds = float(timeout_seconds)
        self.executable = executable

    def __call__(self, source: Path, destination: Path) -> None:
        executable = shutil.which(self.executable)
        if not executable:
            raise AudioArchiveTransportError("ffmpeg_not_available")
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
                    "-map_chapters",
                    "-1",
                    "-vn",
                    "-ac",
                    "2",
                    "-ar",
                    "44100",
                    "-c:a",
                    "aac",
                    "-profile:a",
                    "aac_low",
                    "-b:a",
                    "192k",
                    "-movflags",
                    "+faststart",
                    "-f",
                    "ipod",
                    os.fspath(destination),
                ],
                check=True,
                capture_output=True,
                timeout=self.timeout_seconds,
            )
        except Exception as exc:
            with contextlib.suppress(OSError):
                destination.unlink()
            raise AudioArchiveTransportError("ffmpeg_m4a_transcode_failed") from exc


class ArchiveMP3Transcoder:
    """Derive metadata-free 192 kbps MP3 from the normalized local M4A."""

    def __init__(self, *, timeout_seconds: float = 90.0, executable: str = "ffmpeg") -> None:
        self.timeout_seconds = float(timeout_seconds)
        self.executable = executable

    def __call__(self, source: Path, destination: Path) -> None:
        executable = shutil.which(self.executable)
        if not executable:
            raise AudioArchiveTransportError("ffmpeg_not_available")
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
                    "-map_chapters",
                    "-1",
                    "-vn",
                    "-ac",
                    "2",
                    "-ar",
                    "44100",
                    "-c:a",
                    "libmp3lame",
                    "-b:a",
                    "192k",
                    "-id3v2_version",
                    "0",
                    "-write_id3v1",
                    "0",
                    "-f",
                    "mp3",
                    os.fspath(destination),
                ],
                check=True,
                capture_output=True,
                timeout=self.timeout_seconds,
            )
        except Exception as exc:
            with contextlib.suppress(OSError):
                destination.unlink()
            raise AudioArchiveTransportError("ffmpeg_mp3_transcode_failed") from exc


@dataclass(frozen=True)
class ArchiveFormatProbe(MediaProbe):
    """Duration/size plus the actual normalized audio profile."""

    format_name: str
    codec_name: str
    codec_profile: str
    sample_rate_hz: int
    channels: int
    bitrate_bps: int


class ArchiveFFprobeInspector:
    """Verify the actual container, codec, channels, rate, and bitrate."""

    def __init__(
        self,
        expected_format: str,
        *,
        timeout_seconds: float = 30.0,
        executable: str = "ffprobe",
    ) -> None:
        if expected_format not in {"m4a", "mp3"}:
            raise ValueError("expected_format must be m4a or mp3")
        self.expected_format = expected_format
        self.timeout_seconds = float(timeout_seconds)
        self.executable = executable

    def __call__(self, path: Path) -> ArchiveFormatProbe:
        executable = shutil.which(self.executable)
        if not executable:
            raise AudioArchiveTransportError("ffprobe_not_available")
        try:
            completed = subprocess.run(
                [
                    executable,
                    "-v",
                    "error",
                    "-select_streams",
                    "a:0",
                    "-show_entries",
                    (
                        "format=duration,size,format_name,bit_rate:"
                        "stream=codec_name,profile,sample_rate,channels,bit_rate"
                    ),
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
            details = payload.get("format") if isinstance(payload, Mapping) else None
            streams = payload.get("streams") if isinstance(payload, Mapping) else None
            if not isinstance(details, Mapping) or not isinstance(streams, list):
                raise AudioArchiveTransportError("archive_ffprobe_payload_invalid")
            if len(streams) != 1 or not isinstance(streams[0], Mapping):
                raise AudioArchiveTransportError("archive_audio_stream_invalid")
            stream = streams[0]
            format_names = {
                value.strip().casefold()
                for value in _text(details.get("format_name")).split(",")
                if value.strip()
            }
            codec = _text(stream.get("codec_name")).casefold()
            profile = _text(stream.get("profile"))
            sample_rate = int(stream.get("sample_rate") or 0)
            channels = int(stream.get("channels") or 0)
            bitrate = int(
                stream.get("bit_rate") or details.get("bit_rate") or 0
            )
            duration = float(details.get("duration") or 0.0)
            byte_count = int(details.get("size") or path.stat().st_size)
            if self.expected_format == "m4a":
                container_ok = "m4a" in format_names
                codec_ok = codec == "aac" and profile.casefold() in {
                    "lc",
                    "low complexity",
                }
            else:
                container_ok = "mp3" in format_names
                codec_ok = codec == "mp3"
            if not container_ok:
                raise AudioArchiveTransportError(
                    "archive_audio_container_mismatch"
                )
            if not codec_ok:
                raise AudioArchiveTransportError("archive_audio_codec_mismatch")
            if sample_rate != 44100 or channels != 2:
                raise AudioArchiveTransportError("archive_audio_profile_mismatch")
            # Native AAC honors ``-b:a 192k`` as an encoder target, but its
            # measured average bitrate can be much lower for silence or very
            # low-complexity signals. MP3 is CBR here and must remain near the
            # configured target; AAC still must report a positive sane rate.
            if self.expected_format == "m4a":
                bitrate_ok = 0 < bitrate <= 224000
            else:
                bitrate_ok = 160000 <= bitrate <= 224000
            if not bitrate_ok:
                raise AudioArchiveTransportError("archive_audio_bitrate_mismatch")
            if (
                not math.isfinite(duration)
                or duration <= 0
                or byte_count < 1
            ):
                raise AudioArchiveTransportError("archive_audio_probe_invalid")
            return ArchiveFormatProbe(
                duration_seconds=duration,
                byte_count=byte_count,
                format_name=self.expected_format,
                codec_name=codec,
                codec_profile=profile,
                sample_rate_hz=sample_rate,
                channels=channels,
                bitrate_bps=bitrate,
            )
        except AudioArchiveTransportError:
            raise
        except Exception as exc:
            raise AudioArchiveTransportError(
                "archive_ffprobe_validation_failed"
            ) from exc


async def _derive_mp3_to_staging(
    source_m4a: _StagedAudio,
    binding: _DestinationBinding,
    *,
    owner_run_id: str,
    owned_paths: set[Path],
    staging_parent: Path,
    staging_parent_identity: tuple[Path, int, int],
    transcoder: Callable[[Path, Path], Any],
    inspector: Callable[[Path], Any],
    expected_duration_seconds: float,
) -> _StagedAudio:
    if source_m4a.audio_format != "m4a" or binding.audio_format != "mp3":
        raise AudioArchiveTransportError("archive_mp3_derivation_binding_invalid")
    _revalidate_destination(binding)
    resolved_staging, staging_details = _validate_parent_chain(staging_parent)
    if (
        resolved_staging != staging_parent_identity[0]
        or int(staging_details.st_dev) != staging_parent_identity[1]
        or int(staging_details.st_ino) != staging_parent_identity[2]
    ):
        raise AudioArchiveTransportError("archive_staging_parent_changed")
    if _lexists(binding.path):
        raise AudioArchiveTransportError("archive_destination_already_exists")

    partial_name, staged_name = _owned_stage_names(
        owner_run_id,
        source_m4a.post_id,
        "mp3",
    )
    partial = staging_parent / partial_name
    staged_path = staging_parent / staged_name
    owned_paths.update((partial, staged_path))
    try:
        await _maybe_await(transcoder(source_m4a.path, partial))
        try:
            details = partial.lstat()
        except OSError as exc:
            raise AudioArchiveTransportError("archive_mp3_missing") from exc
        if not stat.S_ISREG(details.st_mode) or _is_link_or_reparse(partial):
            raise AudioArchiveTransportError("archive_mp3_not_regular")
        byte_count, digest = _file_hash(
            partial,
            max_bytes=ARCHIVE_AUDIO_CAP_BYTES,
        )
        probe = await _maybe_await(inspector(partial))
        if not isinstance(probe, MediaProbe):
            raise AudioArchiveTransportError("archive_mp3_probe_invalid")
        if int(probe.byte_count) != byte_count:
            raise AudioArchiveTransportError("archive_mp3_probe_size_mismatch")
        if (
            not math.isfinite(float(probe.duration_seconds))
            or abs(float(probe.duration_seconds) - expected_duration_seconds)
            > max(1.0, expected_duration_seconds * 0.05)
        ):
            raise AudioArchiveTransportError("archive_mp3_duration_mismatch")
        os.replace(partial, staged_path)
        staged_bytes, staged_hash = _file_hash(
            staged_path,
            max_bytes=ARCHIVE_AUDIO_CAP_BYTES,
        )
        if staged_bytes != byte_count or staged_hash != digest:
            raise AudioArchiveTransportError("archive_mp3_staging_mismatch")
    except AudioArchiveTransportError:
        with contextlib.suppress(OSError):
            staged_path.unlink()
        raise
    except FileExistsError as exc:
        with contextlib.suppress(OSError):
            staged_path.unlink()
        raise AudioArchiveTransportError("archive_staging_collision") from exc
    except OSError as exc:
        with contextlib.suppress(OSError):
            staged_path.unlink()
        raise AudioArchiveTransportError("archive_mp3_staging_failed") from exc
    except Exception as exc:
        with contextlib.suppress(OSError):
            staged_path.unlink()
        raise AudioArchiveTransportError("archive_mp3_processing_failed") from exc
    finally:
        with contextlib.suppress(OSError):
            partial.unlink()
    return _StagedAudio(
        post_id=source_m4a.post_id,
        audio_format="mp3",
        path=staged_path,
        destination=binding,
        byte_count=staged_bytes,
        sha256=staged_hash,
        duration_seconds=float(probe.duration_seconds),
    )


def _receipt_files(
    staged_files: Mapping[str, _StagedAudio],
) -> dict[str, dict[str, Any]]:
    if set(staged_files) != {"m4a", "mp3"}:
        raise AudioArchiveTransportError("archive_receipt_pair_incomplete")
    profiles = {
        "m4a": {
            "mime_type": "audio/mp4",
            "codec": "aac-lc",
            "conversion": ARCHIVE_AUDIO_CONVERSION,
        },
        "mp3": {
            "mime_type": "audio/mpeg",
            "codec": "mp3",
            "conversion": ARCHIVE_MP3_CONVERSION,
        },
    }
    return {
        audio_format: {
            "format": audio_format,
            **profiles[audio_format],
            "target_bitrate_kbps": 192,
            "sample_rate_hz": 44100,
            "channels": 2,
            "destination_path": staged.destination.path,
            "byte_count": staged.byte_count,
            "sha256": staged.sha256,
            "duration_seconds": staged.duration_seconds,
        }
        for audio_format, staged in staged_files.items()
    }


@dataclass
class AudioArchiveTransport:
    """Archive exact frozen candidates without extending canonical workflows."""

    state_path: Path = DEFAULT_BROWSER_STATE
    temp_root: Path | None = None
    staging_root: Path | None = None
    limits: AcquisitionLimits = field(
        default_factory=lambda: AcquisitionLimits(
            max_audio_bytes=ARCHIVE_AUDIO_CAP_BYTES,
        )
    )
    sonic_transport: Any = None
    mp3_transcoder: Callable[[Path, Path], Any] | None = None
    mp3_inspector: Callable[[Path], Any] | None = None
    ffmpeg_executable: str = "ffmpeg"
    ffprobe_executable: str = "ffprobe"

    def _transport(self) -> Any:
        if self.limits.max_posts > MAX_PILOT_POSTS:
            raise AudioArchiveTransportError("archive_partition_limit_exceeded")
        if self.limits.max_audio_bytes > ARCHIVE_AUDIO_CAP_BYTES:
            raise AudioArchiveTransportError("archive_audio_cap_exceeded")
        if self.sonic_transport is None:
            return SonicAudioTransport(
                state_path=self.state_path,
                temp_root=self.temp_root,
                limits=self.limits,
                audio_transcoder=ArchiveM4ATranscoder(
                    timeout_seconds=self.limits.transcode_timeout_seconds,
                    executable=self.ffmpeg_executable,
                ),
                audio_inspector=ArchiveFFprobeInspector(
                    "m4a",
                    timeout_seconds=min(
                        30.0,
                        self.limits.transcode_timeout_seconds,
                    ),
                    executable=self.ffprobe_executable,
                ),
                audio_conversion=ARCHIVE_AUDIO_CONVERSION,
            )
        supplied_limits = getattr(self.sonic_transport, "limits", self.limits)
        if (
            not isinstance(supplied_limits, AcquisitionLimits)
            or supplied_limits.max_posts > MAX_PILOT_POSTS
            or supplied_limits.max_audio_bytes > ARCHIVE_AUDIO_CAP_BYTES
        ):
            raise AudioArchiveTransportError("archive_transport_limits_invalid")
        return self.sonic_transport

    async def archive_candidates(
        self,
        archive_run_id: str,
        candidates: Sequence[Mapping[str, Any]],
        destinations: Mapping[str, Mapping[str, Path | str]],
        expected_account: str = "",
        receipt_processor: ArchiveReceiptProcessor | None = None,
    ) -> list[ArchiveAudioReceipt]:
        """Archive at most 60 exact posts using one Profile 7 transport pass."""

        run_id = _validate_archive_run_id(archive_run_id)
        owner_run_id = sonic_owner_run_id(run_id)
        transport = self._transport()
        transport_limits = getattr(transport, "limits", self.limits)
        frozen = validate_frozen_candidates(
            candidates,
            limits=transport_limits,
        )
        if len(frozen) > MAX_PILOT_POSTS:
            raise AudioArchiveTransportError("archive_partition_limit_exceeded")
        destination_keys = list(destinations.keys())
        if not all(isinstance(value, str) and _POST_ID.fullmatch(value) for value in destination_keys):
            raise AudioArchiveTransportError("archive_destination_key_invalid")
        post_ids = [candidate.post_id for candidate in frozen]
        if set(destination_keys) != set(post_ids) or len(destination_keys) != len(post_ids):
            raise AudioArchiveTransportError("archive_destination_mapping_mismatch")

        bindings: dict[str, dict[str, _DestinationBinding]] = {}
        for post_id in post_ids:
            destination_pair = destinations[post_id]
            if not isinstance(destination_pair, Mapping) or set(
                destination_pair
            ) != {"m4a", "mp3"}:
                raise AudioArchiveTransportError(
                    "archive_destination_pair_invalid"
                )
            pair = {
                audio_format: _bind_destination(
                    post_id,
                    audio_format,
                    destination_pair[audio_format],
                )
                for audio_format in ("m4a", "mp3")
            }
            if (
                pair["m4a"].resolved_parent != pair["mp3"].resolved_parent
                or pair["m4a"].parent_device != pair["mp3"].parent_device
                or pair["m4a"].parent_inode != pair["mp3"].parent_inode
            ):
                raise AudioArchiveTransportError(
                    "archive_destination_pair_parent_mismatch"
                )
            bindings[post_id] = pair
        normalized_paths = [
            os.path.normcase(os.fspath(binding.path))
            for pair in bindings.values()
            for binding in pair.values()
        ]
        if len(normalized_paths) != len(set(normalized_paths)):
            raise AudioArchiveTransportError("archive_destination_path_duplicate")
        if self.staging_root is None:
            # Per-destination staging remains atomic because the private file
            # and final name share one directory and filesystem.
            staging_bindings = {
                post_id: (
                    pair["m4a"].parent_path,
                    pair["m4a"].resolved_parent,
                    pair["m4a"].parent_device,
                    pair["m4a"].parent_inode,
                )
                for post_id, pair in bindings.items()
            }
        else:
            staging_root = Path(self.staging_root).expanduser()
            resolved_staging, staging_details = _validate_parent_chain(staging_root)
            staging_device = int(staging_details.st_dev)
            staging_inode = int(staging_details.st_ino)
            if any(
                binding.parent_device != staging_device
                for pair in bindings.values()
                for binding in pair.values()
            ):
                raise AudioArchiveTransportError(
                    "archive_staging_destination_filesystem_mismatch"
                )
            staging_bindings = {
                post_id: (
                    staging_root,
                    resolved_staging,
                    staging_device,
                    staging_inode,
                )
                for post_id in post_ids
            }

        normalized_candidates = [
            {
                "post_id": candidate.post_id,
                "canonical_url": candidate.canonical_url,
                "creator_handle": candidate.creator_handle,
                "content_type": candidate.content_type,
            }
            for candidate in frozen
        ]
        mp3_transcoder = self.mp3_transcoder or ArchiveMP3Transcoder(
            timeout_seconds=self.limits.transcode_timeout_seconds,
            executable=self.ffmpeg_executable,
        )
        mp3_inspector = self.mp3_inspector or ArchiveFFprobeInspector(
            "mp3",
            timeout_seconds=min(
                30.0,
                self.limits.transcode_timeout_seconds,
            ),
            executable=self.ffprobe_executable,
        )
        owned_paths: set[Path] = set()
        staged_by_post: dict[str, dict[str, _StagedAudio]] = {}
        archive_receipts: list[ArchiveAudioReceipt] = []
        seen_receipts: set[str] = set()

        async def stage_audio(item: TransientAudioItem) -> Mapping[str, Any]:
            if item.post_id not in bindings or item.post_id in staged_by_post:
                raise AudioArchiveTransportError("archive_candidate_mapping_mismatch")
            _archive_provenance(item.provenance, retained=False)
            staged_m4a = _copy_to_staging(
                item,
                bindings[item.post_id]["m4a"],
                owner_run_id=owner_run_id,
                owned_paths=owned_paths,
                staging_parent=staging_bindings[item.post_id][0],
                staging_parent_identity=staging_bindings[item.post_id][1:],
            )
            try:
                staged_mp3 = await _derive_mp3_to_staging(
                    staged_m4a,
                    bindings[item.post_id]["mp3"],
                    owner_run_id=owner_run_id,
                    owned_paths=owned_paths,
                    staging_parent=staging_bindings[item.post_id][0],
                    staging_parent_identity=staging_bindings[item.post_id][1:],
                    transcoder=mp3_transcoder,
                    inspector=mp3_inspector,
                    expected_duration_seconds=float(item.duration_seconds),
                )
            except Exception:
                _remove_owned_stage(staged_m4a.path, owned_paths)
                raise
            staged_pair = {"m4a": staged_m4a, "mp3": staged_mp3}
            staged_by_post[item.post_id] = staged_pair
            return {
                "archive_staged": True,
                "post_id": item.post_id,
                "m4a_sha256": staged_m4a.sha256,
                "m4a_byte_count": staged_m4a.byte_count,
                "mp3_sha256": staged_mp3.sha256,
                "mp3_byte_count": staged_mp3.byte_count,
            }

        async def finish_receipt(receipt: SonicTransportReceipt) -> None:
            post_id = _text(receipt.post_id)
            if post_id not in bindings or post_id in seen_receipts:
                raise AudioArchiveTransportError("archive_receipt_mapping_mismatch")
            seen_receipts.add(post_id)
            staged_pair = staged_by_post.pop(post_id, None)
            if receipt.status != "completed":
                if staged_pair is not None:
                    for staged in staged_pair.values():
                        _remove_owned_stage(staged.path, owned_paths)
                archived = ArchiveAudioReceipt(
                    archive_run_id=run_id,
                    transport_owner_run_id=owner_run_id,
                    post_id=post_id,
                    creator_handle=_text(receipt.creator_handle),
                    duration_seconds=0.0,
                    files={},
                    provenance=_archive_provenance(
                        receipt.provenance,
                        retained=False,
                    ),
                    timing=dict(receipt.timing),
                    status="unavailable",
                    error_code=_text(receipt.error_code) or "audio_unavailable",
                )
            else:
                if staged_pair is None:
                    raise AudioArchiveTransportError("archive_staging_receipt_missing")
                staged_m4a = staged_pair["m4a"]
                if (
                    staged_m4a.byte_count != int(receipt.audio_byte_count)
                    or staged_m4a.sha256 != _text(receipt.audio_sha256)
                ):
                    raise AudioArchiveTransportError("archive_receipt_hash_mismatch")
                files = _receipt_files(staged_pair)
                _promote_staging_pair(staged_pair, owned_paths=owned_paths)
                archived = ArchiveAudioReceipt(
                    archive_run_id=run_id,
                    transport_owner_run_id=owner_run_id,
                    post_id=post_id,
                    creator_handle=_text(receipt.creator_handle),
                    duration_seconds=float(receipt.duration_seconds),
                    files=files,
                    provenance=_archive_provenance(
                        receipt.provenance,
                        retained=True,
                    ),
                    timing=dict(receipt.timing),
                )
            archive_receipts.append(archived)
            if receipt_processor is not None:
                await _maybe_await(receipt_processor(archived))

        try:
            await transport.process_candidates(
                run_id=owner_run_id,
                candidates=normalized_candidates,
                processor=stage_audio,
                expected_account=expected_account,
                continue_on_item_error=True,
                receipt_processor=finish_receipt,
            )
            if [receipt.post_id for receipt in archive_receipts] != post_ids:
                raise AudioArchiveTransportError("archive_receipt_set_incomplete")
            if staged_by_post:
                raise AudioArchiveTransportError("archive_staging_receipt_incomplete")
            return archive_receipts
        finally:
            for staged_pair in list(staged_by_post.values()):
                for staged in staged_pair.values():
                    with contextlib.suppress(OSError):
                        staged.path.unlink()
            for path in list(owned_paths):
                with contextlib.suppress(OSError):
                    path.unlink()


__all__ = [
    "ARCHIVE_AUDIO_CAP_BYTES",
    "ARCHIVE_AUDIO_CONVERSION",
    "ARCHIVE_MP3_CONVERSION",
    "ArchiveAudioReceipt",
    "ArchiveFFprobeInspector",
    "ArchiveFormatProbe",
    "ArchiveM4ATranscoder",
    "ArchiveMP3Transcoder",
    "AudioArchiveTransport",
    "AudioArchiveTransportError",
    "sonic_owner_run_id",
]
