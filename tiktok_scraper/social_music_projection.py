"""Project normalized collector records into the social music contract.

This is an adapter boundary, not a collector.  It consumes only fields that a
platform collector has already attributed and never inspects captions for song
names, follows media URLs, downloads audio, or invokes a catalog provider.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .social_music_contract import (
    DEFAULT_ADAPTER_VERSION,
    build_social_music_evidence,
)


def _structured(value: Any) -> bool:
    return (
        isinstance(value, Mapping)
        or isinstance(value, (bytes, bytearray, memoryview, set, frozenset))
        or (isinstance(value, Sequence) and not isinstance(value, str))
    )


def _scalar(value: Any) -> Any:
    return None if _structured(value) else value


def _text(value: Any) -> str:
    if value is None or _structured(value):
        return ""
    return str(value).strip()


def _platform(record: Mapping[str, Any], platform: str | None) -> str:
    value = _text(platform or record.get("platform")).casefold()
    return "x" if value == "twitter" else value


def _status(value: Any, *, has_value: bool = False) -> str:
    normalized = _text(value).casefold()
    aliases = {
        "ok": "available",
        "error": "unavailable",
        "disabled": "unsupported",
        "not_requested": "not_provided",
        "metadata_error": "unavailable",
        "fetch_error": "unavailable",
        "empty_track": "not_provided",
        "protocol_error": "unavailable",
        "rate_limited": "unavailable",
        "blocked": "unavailable",
        "po_token_required": "unavailable",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized in {
        "available",
        "partial",
        "not_provided",
        "unavailable",
        "unsupported",
    }:
        return normalized
    return "available" if has_value else "not_provided"


def _attempted(
    explicit: Any,
    raw_status: Any,
    *,
    observed: bool = False,
    attempt_count: Any = None,
) -> bool:
    if isinstance(explicit, bool):
        return explicit
    if isinstance(explicit, (int, float)) and not isinstance(explicit, bool):
        if explicit in {0, 1}:
            return bool(explicit)
    if _text(attempt_count).isdigit() and int(_text(attempt_count)) > 0:
        return True
    normalized = _text(raw_status).casefold()
    return bool(
        observed
        or normalized
        not in {"", "not_requested", "not_attempted", "disabled", "unsupported"}
    )


def _method(record: Mapping[str, Any], *fallbacks: Any) -> str:
    for value in (*fallbacks, record.get("metadata_method"), record.get("discovery_method")):
        if _text(value):
            return _text(value)
    return ""


def _music_input(record: Mapping[str, Any], platform: str) -> dict[str, Any]:
    fields = {
        "music_id": _text(record.get("music_id")),
        "title": _text(record.get("music_title")),
        "artist": _text(record.get("music_author"))
        or _text(record.get("music_artist")),
        "album": _text(record.get("music_album")),
        "audio_type": _text(record.get("music_audio_type"))
        or _text(record.get("media_audio_type")),
        "is_original": _scalar(record.get("music_is_original")),
        "duration_ms": _scalar(record.get("music_duration_ms")),
        "duration_seconds": _scalar(record.get("music_duration_seconds")),
    }
    present = any(value not in (None, "") for value in fields.values())
    raw_status = record.get("music_metadata_status")
    status = _status(raw_status, has_value=present)
    source = _text(record.get("music_metadata_source"))
    if not source and present and platform == "tiktok":
        source = "tiktok_item_music"
    reason = _text(record.get("music_metadata_reason"))
    if not reason and not present:
        reason = (
            "official_api_has_no_structured_music_declaration"
            if platform in {"facebook", "x", "youtube"}
            else "platform_music_not_returned"
        )
    return {
        "attempted": _attempted(
            record.get("music_metadata_attempted"),
            raw_status,
            observed=present,
            attempt_count=record.get("music_metadata_attempt_count"),
        ),
        "status": status,
        "reason": reason,
        **fields,
        "provenance": {
            "source": source,
            "method": _method(record) or source,
            "authority": _text(record.get("music_metadata_authority")),
            "access_scope": _text(record.get("music_metadata_access_scope")),
            "observed_at": _text(record.get("observed_at") or record.get("timestamp")),
        },
    }


def _transcript_input(record: Mapping[str, Any]) -> dict[str, Any]:
    transcript_text = _text(record.get("transcript"))
    source = _text(record.get("transcript_source"))
    raw_status = record.get("transcript_status")
    status = _status(raw_status, has_value=bool(transcript_text))
    return {
        "attempted": _attempted(
            record.get("transcript_attempted"),
            raw_status,
            observed=bool(transcript_text),
            attempt_count=record.get("transcript_attempt_count"),
        ),
        "status": status,
        "reason": _text(record.get("transcript_error")),
        "text": transcript_text,
        "language": _text(record.get("transcript_language")),
        "language_name": _text(record.get("transcript_language_name")),
        "is_auto_generated": _scalar(
            record.get("transcript_is_auto_generated")
        ),
        "segment_count": _scalar(record.get("transcript_segment_count")),
        "provenance": {
            "source": source,
            "method": source or _method(record),
            "authority": _text(record.get("transcript_authority"))
            or _text(record.get("transcript_metadata_authority")),
            "access_scope": _text(record.get("transcript_access_scope"))
            or _text(record.get("transcript_metadata_access_scope")),
            "observed_at": _text(record.get("observed_at") or record.get("timestamp")),
        },
    }


def _subtitle_input(record: Mapping[str, Any]) -> dict[str, Any]:
    raw_tracks = record.get("subtitle_tracks")
    raw_tracks = (
        raw_tracks
        if isinstance(raw_tracks, Sequence)
        and not isinstance(raw_tracks, (str, bytes, bytearray))
        else []
    )
    tracks: list[dict[str, Any]] = []
    for raw_track in raw_tracks:
        if not isinstance(raw_track, Mapping):
            continue
        language_code = (
            _text(raw_track.get("language_code"))
            or _text(raw_track.get("language"))
            or _text(raw_track.get("language_tag"))
        )
        language_name = _text(raw_track.get("language_name")) or _text(
            raw_track.get("display_name")
        )
        if not language_code and not language_name:
            continue
        track = {
            "language_code": language_code,
            "language_name": language_name,
            "is_auto_generated": _scalar(
                raw_track.get("is_auto_generated")
                if "is_auto_generated" in raw_track
                else raw_track.get("auto_generated")
            ),
            "segment_count": _scalar(raw_track.get("segment_count")),
        }
        if track not in tracks:
            tracks.append(track)

    selected_track = record.get("subtitle_selected_track")
    if isinstance(selected_track, Mapping):
        selected_code = (
            _text(selected_track.get("language_code"))
            or _text(selected_track.get("language"))
            or _text(selected_track.get("language_tag"))
        )
        selected_name = _text(selected_track.get("language_name")) or _text(
            selected_track.get("display_name")
        )
        selected_track = {
            "language_code": selected_code,
            "language_name": selected_name,
            "is_auto_generated": _scalar(
                selected_track.get("is_auto_generated")
                if "is_auto_generated" in selected_track
                else selected_track.get("auto_generated")
            ),
            "segment_count": _scalar(selected_track.get("segment_count")),
        }
        if not selected_code and not selected_name:
            selected_track = None
    else:
        selected_track = None

    raw_status = record.get("subtitle_status")
    status = _status(
        raw_status,
        has_value=bool(tracks),
    )
    source = _text(record.get("subtitle_source")) or _text(
        record.get("subtitle_manifest_source")
    )
    method = _text(record.get("subtitle_method")) or source
    return {
        "attempted": _attempted(
            record.get("subtitle_attempted"),
            raw_status,
            observed=bool(tracks),
            attempt_count=record.get("subtitle_attempt_count"),
        ),
        "status": status,
        "reason": _text(record.get("subtitle_error"))
        or _text(record.get("subtitle_no_caption_reason")),
        "tracks": tracks,
        "selected_track": selected_track,
        "provenance": {
            "source": source,
            "method": method,
            "authority": _text(record.get("subtitle_authority"))
            or _text(record.get("subtitle_metadata_authority")),
            "access_scope": _text(record.get("subtitle_access_scope"))
            or _text(record.get("subtitle_metadata_access_scope")),
            "observed_at": _text(record.get("observed_at") or record.get("timestamp")),
        },
    }


def social_music_evidence_from_record(
    record: Mapping[str, Any],
    *,
    platform: str | None = None,
    adapter_version: str = DEFAULT_ADAPTER_VERSION,
) -> dict[str, Any]:
    """Build a closed social-music evidence block from one collector record."""

    if not isinstance(record, Mapping):
        raise TypeError("record must be a mapping")
    normalized_platform = _platform(record, platform)
    observed_at = _text(record.get("observed_at") or record.get("timestamp"))
    return build_social_music_evidence(
        normalized_platform,
        music_declaration=_music_input(record, normalized_platform),
        transcript=_transcript_input(record),
        subtitles=_subtitle_input(record),
        observed_at=observed_at,
        adapter_version=_text(adapter_version),
    )


__all__ = ["social_music_evidence_from_record"]
