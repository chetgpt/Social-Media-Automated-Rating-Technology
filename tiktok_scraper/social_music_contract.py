"""Pure, hash-bound contract for declared social-platform music evidence.

This module projects already-observed metadata. It never fetches media or
calls a platform/provider, and it retains only allowlisted scalar evidence.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "social-music-evidence-v2"
CAPABILITY_SCHEMA_VERSION = "social-music-capability-manifest-v2"
DEFAULT_ADAPTER_VERSION = "offline-json-ingest-v1"
ADAPTER_AUTHORITY = "offline_normalized_import"
ADAPTER_ACCESS_SCOPE = "caller_supplied_normalized_records"
SUPPORTED_PLATFORMS = frozenset({"tiktok", "instagram", "facebook", "x", "youtube"})
TERMINAL_OUTCOMES = frozenset({
    "available", "partial", "not_provided", "unavailable", "unsupported",
})

_SUPPORT = {
    "tiktok": (True, True, True),
    "instagram": (True, False, False),
    "facebook": (False, False, False),
    "x": (False, False, False),
    "youtube": (False, True, True),
}
_MUSIC_FIELDS = (
    "music_id", "title", "artist", "album", "audio_type", "is_original",
    "duration_ms",
)
_PROVENANCE_FIELDS = (
    "source",
    "method",
    "authority",
    "access_scope",
    "observed_at",
)
_LIMITATIONS = [
    "platform_declaration_is_not_acoustic_identification",
    "transcript_and_subtitles_are_not_lyrics_verification",
]
_STATUS_ALIASES = {"ok": "available", "missing": "not_provided",
                   "disabled": "unsupported", "error": "unavailable"}
_URL_RE = re.compile(
    r"(?i)(?:https?://|www\.|data:|blob:|file://)[^\s<>{}\[\]]+"
)
_WHITESPACE_RE = re.compile(r"\s+")
_MAX_DURATION_MS = 86_400_000
_MAX_TRANSCRIPT_CHARS = 20_000
_MAX_SUBTITLE_TRACKS = 20
MAX_COMMENTS = 500


class SocialMusicContractError(ValueError):
    """Raised for an unsupported platform or malformed builder input."""


def canonical_json(document: Any) -> str:
    """Return deterministic JSON for hashing."""

    try:
        return json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise SocialMusicContractError(
            "document is not canonical-JSON compatible"
        ) from exc


def canonical_sha256(document: Any) -> str:
    return hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()


def _bind_hash(document: Mapping[str, Any], field: str) -> dict[str, Any]:
    payload = dict(document)
    payload.pop(field, None)
    return {**payload, field: canonical_sha256(payload)}


def _verify_hash(document: Mapping[str, Any], field: str) -> bool:
    expected = document.get(field) if isinstance(document, Mapping) else None
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        return False
    payload = dict(document)
    payload.pop(field, None)
    try:
        return canonical_sha256(payload) == expected
    except SocialMusicContractError:
        return False


def _platform(value: Any) -> str:
    platform = str(value or "").strip().casefold()
    platform = "x" if platform == "twitter" else platform
    if platform not in SUPPORTED_PLATFORMS:
        raise SocialMusicContractError(
            "platform must be tiktok, instagram, facebook, x, or youtube"
        )
    return platform


def _mapping(value: Mapping[str, Any] | None, name: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise SocialMusicContractError(f"{name} must be a mapping")
    return value


def _text(value: Any, limit: int) -> str:
    if value is None or isinstance(value, (Mapping, list, tuple, set, bytes)):
        return ""
    text = _URL_RE.sub("", str(value).strip())
    text = "".join(char for char in text if char >= " " or char == "\n")
    return _WHITESPACE_RE.sub(" ", text).strip()[:limit]


def _optional_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value) if value in {0, 1} else None
    normalized = str(value or "").strip().casefold()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    return None


def _nonnegative_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number >= 0 else None


def _duration_ms(raw: Mapping[str, Any]) -> int | None:
    value, multiplier = raw.get("duration_ms"), 1.0
    if value in (None, ""):
        value, multiplier = raw.get("duration_seconds"), 1_000.0
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value) * multiplier
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or not 0 < number <= _MAX_DURATION_MS:
        return None
    return int(round(number))


def _status(value: Any, default: str = "not_provided") -> str:
    normalized = str(value or "").strip().casefold()
    normalized = _STATUS_ALIASES.get(normalized, normalized)
    return normalized if normalized in TERMINAL_OUTCOMES else default


def _provenance(
    value: Any,
    observed_at: str,
    *,
    default_authority: str = ADAPTER_AUTHORITY,
    default_access_scope: str = ADAPTER_ACCESS_SCOPE,
) -> dict[str, str]:
    raw = value if isinstance(value, Mapping) else {}
    return {
        "source": _text(raw.get("source"), 240),
        "method": _text(raw.get("method"), 120),
        "authority": _text(raw.get("authority") or default_authority, 120),
        "access_scope": _text(
            raw.get("access_scope") or default_access_scope,
            240,
        ),
        "observed_at": _text(raw.get("observed_at") or observed_at, 80),
    }


def _attributed(provenance: Mapping[str, Any]) -> bool:
    return all(provenance.get(field) for field in _PROVENANCE_FIELDS)


def _attempted(raw: Mapping[str, Any], *, observed: bool) -> bool:
    explicit = _optional_bool(raw.get("attempted"))
    if explicit is not None:
        return explicit
    raw_status = str(raw.get("status") or "").strip().casefold()
    return bool(observed or (raw_status and raw_status != "not_requested"))


def capability_manifest(
    platform: str,
    *,
    adapter_version: str = DEFAULT_ADAPTER_VERSION,
) -> dict[str, Any]:
    """Return offline-import capabilities as a closed, hashed manifest.

    ``supported`` means that this adapter can normalize explicitly attributed
    caller-supplied evidence.  It does not claim that an official live API
    exposes the field.
    """

    platform = _platform(platform)
    normalized_adapter_version = _text(adapter_version, 120)
    if not normalized_adapter_version:
        raise SocialMusicContractError("adapter_version is required")
    music, transcript, subtitles = _SUPPORT[platform]
    document = {
        "schema_version": CAPABILITY_SCHEMA_VERSION,
        "platform": platform,
        "adapter": {
            "name": "offline_json_ingest",
            "version": normalized_adapter_version,
            "authority": ADAPTER_AUTHORITY,
            "access_scope": ADAPTER_ACCESS_SCOPE,
            "network_access": False,
            "source_modes": ["topic", "creator", "url"],
            "discovery_horizon": "input_payload_only",
        },
        "evidence_limits": {
            "max_comments": MAX_COMMENTS,
            "max_transcript_chars": _MAX_TRANSCRIPT_CHARS,
            "max_subtitle_tracks": _MAX_SUBTITLE_TRACKS,
        },
        "capabilities": {
            "platform_music_declaration": {
                "supported": music,
                "accepted_fields": list(_MUSIC_FIELDS),
                "required_provenance": list(_PROVENANCE_FIELDS),
            },
            "transcript": {
                "supported": transcript,
                "accepted_fields": ["text", "language", "language_name",
                                    "is_auto_generated", "segment_count"],
                "required_provenance": list(_PROVENANCE_FIELDS),
            },
            "subtitles": {
                "supported": subtitles,
                "accepted_track_fields": ["language_code", "language_name",
                                          "is_auto_generated", "segment_count"],
                "required_provenance": list(_PROVENANCE_FIELDS),
            },
            "acoustic_verification": {
                "supported": False,
                "forced_status": "not_attempted",
            },
            "lyrics": {"supported": False, "forced_status": "not_attempted"},
        },
    }
    return _bind_hash(document, "manifest_hash")


def build_capability_manifest(
    platform: str,
    *,
    adapter_version: str = DEFAULT_ADAPTER_VERSION,
) -> dict[str, Any]:
    return capability_manifest(platform, adapter_version=adapter_version)


def validate_capability_manifest(document: Mapping[str, Any]) -> bool:
    if not isinstance(document, Mapping) or not _verify_hash(document, "manifest_hash"):
        return False
    try:
        adapter = document.get("adapter")
        if not isinstance(adapter, Mapping):
            return False
        return dict(document) == capability_manifest(
            document.get("platform", ""),
            adapter_version=adapter.get("version", ""),
        )
    except (TypeError, SocialMusicContractError):
        return False


def _empty_music_fields() -> dict[str, Any]:
    return {"music_id": "", "title": "", "artist": "", "album": "",
            "audio_type": "", "is_original": None, "duration_ms": None}


def _empty_transcript_values() -> dict[str, Any]:
    return {"text": "", "language": "", "language_name": "",
            "is_auto_generated": None, "segment_count": 0}


def _music_outcomes(
    fields: Mapping[str, Any],
    status: str,
    provenance: Mapping[str, str],
    reason: str,
) -> dict[str, dict[str, str]]:
    result = {}
    for field in _MUSIC_FIELDS:
        if fields[field] not in (None, ""):
            field_status, field_reason = "available", ""
        elif status in {"unavailable", "unsupported"}:
            field_status, field_reason = status, reason
        else:
            field_status = "not_provided"
            field_reason = reason or "platform_field_not_returned"
        result[field] = {
            "status": field_status,
            "source": provenance["source"],
            "method": provenance["method"],
            "authority": provenance["authority"],
            "access_scope": provenance["access_scope"],
            "observed_at": provenance["observed_at"],
            "reason": field_reason,
        }
    return result


def _music_block(
    platform: str,
    raw_value: Mapping[str, Any] | None,
    supported: bool,
    observed_at: str,
) -> dict[str, Any]:
    raw = _mapping(raw_value, "music_declaration")
    provenance = _provenance(raw.get("provenance"), observed_at)
    fields = {
        "music_id": _text(raw.get("music_id"), 256),
        "title": _text(raw.get("title"), 500),
        "artist": _text(raw.get("artist"), 500),
        "album": _text(raw.get("album"), 500),
        "audio_type": _text(raw.get("audio_type"), 120),
        "is_original": _optional_bool(raw.get("is_original")),
        "duration_ms": _duration_ms(raw),
    }
    has_fields = any(value not in (None, "") for value in fields.values())
    attempted = _attempted(raw, observed=has_fields)
    supplied, reason = _status(raw.get("status")), _text(raw.get("reason"), 240)
    if not supported:
        fields, provenance = _empty_music_fields(), _provenance(None, "")
        attempted = False
        status, reason = "unsupported", "platform_music_declaration_not_supported"
    elif not attempted:
        fields = _empty_music_fields()
        status, reason = "unavailable", "music_collection_attempt_not_recorded"
    elif has_fields and not _attributed(provenance):
        fields, provenance = _empty_music_fields(), _provenance(None, observed_at)
        status, reason = "unavailable", "explicit_music_provenance_required"
    elif not _attributed(provenance):
        status, reason = "unavailable", "explicit_music_provenance_required"
    elif has_fields:
        status = "partial" if supplied == "partial" or not (
            fields["title"] and fields["artist"]
        ) else "available"
        reason = (
            reason or "platform_music_identity_incomplete"
        ) if status == "partial" else ""
    else:
        status = supplied if supplied not in {"available", "partial"} else "not_provided"
        reason = reason or {
            "not_provided": "platform_music_not_returned",
            "unavailable": "platform_music_metadata_unavailable",
            "unsupported": "platform_music_declaration_not_supported",
        }[status]
    has_fields = any(value not in (None, "") for value in fields.values())
    return {
        "attempted": attempted,
        "status": status,
        "reason": reason,
        "fields": fields,
        "field_outcomes": _music_outcomes(fields, status, provenance, reason),
        "provenance": provenance,
        "identification_basis": f"{platform}_declared_metadata" if has_fields else "none",
        "acoustic_recognition_performed": False,
    }


def _transcript_block(
    raw_value: Mapping[str, Any] | None,
    supported: bool,
    observed_at: str,
) -> dict[str, Any]:
    raw = _mapping(raw_value, "transcript")
    provenance = _provenance(raw.get("provenance"), observed_at)
    values = {
        "text": _text(raw.get("text"), _MAX_TRANSCRIPT_CHARS),
        "language": _text(raw.get("language"), 40),
        "language_name": _text(raw.get("language_name"), 100),
        "is_auto_generated": _optional_bool(raw.get("is_auto_generated")),
        "segment_count": _nonnegative_int(raw.get("segment_count")) or 0,
    }
    observed = any(value not in (None, "", 0) for value in values.values())
    attempted = _attempted(raw, observed=observed)
    supplied, reason = _status(raw.get("status")), _text(raw.get("reason"), 240)
    if not supported:
        values, provenance = _empty_transcript_values(), _provenance(None, "")
        attempted = False
        status, reason = "unsupported", "transcript_not_supported"
    elif not attempted:
        values = _empty_transcript_values()
        status, reason = "unavailable", "transcript_collection_attempt_not_recorded"
    elif observed and not _attributed(provenance):
        values = _empty_transcript_values()
        provenance = _provenance(None, observed_at)
        status, reason = "unavailable", "explicit_transcript_provenance_required"
    elif not _attributed(provenance):
        status, reason = "unavailable", "explicit_transcript_provenance_required"
    elif values["text"]:
        status = "partial" if supplied == "partial" else "available"
        reason = (reason or "transcript_partial") if status == "partial" else ""
    elif observed:
        status, reason = "partial", reason or "transcript_text_not_returned"
    else:
        status = supplied if supplied not in {"available", "partial"} else "not_provided"
        reason = reason or {
            "not_provided": "transcript_not_returned",
            "unavailable": "transcript_unavailable",
            "unsupported": "transcript_not_supported",
        }[status]
    return {
        "attempted": attempted,
        "status": status,
        "reason": reason,
        **values,
        "provenance": provenance,
    }


def _track(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    track = {
        "language_code": _text(value.get("language_code"), 40),
        "language_name": _text(value.get("language_name"), 100),
        "is_auto_generated": _optional_bool(value.get("is_auto_generated")),
        "segment_count": _nonnegative_int(value.get("segment_count")) or 0,
    }
    return track if track["language_code"] or track["language_name"] else None


def _subtitle_block(
    raw_value: Mapping[str, Any] | None,
    supported: bool,
    observed_at: str,
) -> dict[str, Any]:
    raw = _mapping(raw_value, "subtitles")
    supplied_tracks = raw.get("tracks")
    supplied_tracks = (
        supplied_tracks
        if isinstance(supplied_tracks, Sequence)
        and not isinstance(supplied_tracks, (str, bytes))
        else []
    )
    tracks = []
    for value in list(supplied_tracks)[:_MAX_SUBTITLE_TRACKS]:
        projected = _track(value)
        if projected is not None and projected not in tracks:
            tracks.append(projected)
    selected = _track(raw.get("selected_track"))
    selected = selected if selected in tracks else None
    provenance = _provenance(raw.get("provenance"), observed_at)
    attempted = _attempted(raw, observed=bool(tracks))
    supplied, reason = _status(raw.get("status")), _text(raw.get("reason"), 240)
    if not supported:
        tracks, selected, provenance = [], None, _provenance(None, "")
        attempted = False
        status, reason = "unsupported", "subtitles_not_supported"
    elif not attempted:
        tracks, selected = [], None
        status, reason = "unavailable", "subtitle_collection_attempt_not_recorded"
    elif tracks and not _attributed(provenance):
        tracks, selected, provenance = [], None, _provenance(None, observed_at)
        status, reason = "unavailable", "explicit_subtitle_provenance_required"
    elif not _attributed(provenance):
        status, reason = "unavailable", "explicit_subtitle_provenance_required"
    elif tracks:
        status = "partial" if supplied == "partial" else "available"
        reason = (
            reason or "subtitle_inventory_partial"
        ) if status == "partial" else ""
    else:
        status = supplied if supplied not in {"available", "partial"} else "not_provided"
        reason = reason or {
            "not_provided": "subtitle_tracks_not_returned",
            "unavailable": "subtitles_unavailable",
            "unsupported": "subtitles_not_supported",
        }[status]
    return {
        "attempted": attempted,
        "status": status,
        "reason": reason,
        "track_count": len(tracks),
        "tracks": tracks,
        "selected_track": selected,
        "provenance": provenance,
    }


def build_social_music_evidence(
    platform: str,
    *,
    music_declaration: Mapping[str, Any] | None = None,
    transcript: Mapping[str, Any] | None = None,
    subtitles: Mapping[str, Any] | None = None,
    observed_at: str = "",
    adapter_version: str = DEFAULT_ADAPTER_VERSION,
) -> dict[str, Any]:
    """Build a safe document from explicit declarations and provenance."""

    platform, observed_at = _platform(platform), _text(observed_at, 80)
    manifest = capability_manifest(platform, adapter_version=adapter_version)
    support = manifest["capabilities"]
    document = {
        "schema_version": SCHEMA_VERSION,
        "platform": platform,
        "observed_at": observed_at,
        "capability_manifest": manifest,
        "music_declaration": _music_block(
            platform,
            music_declaration,
            support["platform_music_declaration"]["supported"],
            observed_at,
        ),
        "transcript": _transcript_block(
            transcript, support["transcript"]["supported"], observed_at
        ),
        "subtitles": _subtitle_block(
            subtitles, support["subtitles"]["supported"], observed_at
        ),
        "acoustic_verification": {"status": "not_attempted", "verified": False},
        "lyrics": {"status": "not_attempted"},
        "limitations": list(_LIMITATIONS),
    }
    return _bind_hash(document, "evidence_hash")


def verify_social_music_evidence_hash(document: Mapping[str, Any]) -> bool:
    return _verify_hash(document, "evidence_hash")


def social_music_readiness_reasons(document: Mapping[str, Any]) -> list[str]:
    """Return fail-closed reasons for supported stages lacking attempt proof."""

    if not validate_social_music_evidence(document):
        return ["invalid_social_music_evidence"]
    capabilities = document["capability_manifest"]["capabilities"]
    blocks = {
        "platform_music_declaration": document["music_declaration"],
        "transcript": document["transcript"],
        "subtitles": document["subtitles"],
    }
    reasons: list[str] = []
    for capability, block in blocks.items():
        if not capabilities[capability]["supported"]:
            continue
        if block.get("attempted") is not True:
            reasons.append(f"{capability}_not_attempted")
            continue
        provenance = block.get("provenance")
        if not isinstance(provenance, Mapping) or not _attributed(provenance):
            reasons.append(f"{capability}_provenance_incomplete")
    return reasons


def validate_social_music_evidence(document: Mapping[str, Any]) -> bool:
    """Validate the exact shape by hash-checking and canonical reconstruction."""

    if not isinstance(document, Mapping) or not _verify_hash(document, "evidence_hash"):
        return False
    try:
        music = document["music_declaration"]
        transcript = document["transcript"]
        subtitles = document["subtitles"]
        fields = music["fields"]
        music_input = {
            **{field: fields[field] for field in _MUSIC_FIELDS},
            "attempted": music["attempted"],
            "status": music["status"],
            "reason": music["reason"],
            "provenance": music["provenance"],
        }
        transcript_input = {
            key: transcript[key]
            for key in (
                "attempted", "status", "reason", "text", "language", "language_name",
                "is_auto_generated", "segment_count", "provenance",
            )
        }
        subtitle_input = {
            key: subtitles[key]
            for key in (
                "attempted", "status", "reason", "tracks", "selected_track", "provenance",
            )
        }
        manifest = document["capability_manifest"]
        adapter = manifest["adapter"]
        rebuilt = build_social_music_evidence(
            document["platform"],
            music_declaration=music_input,
            transcript=transcript_input,
            subtitles=subtitle_input,
            observed_at=document["observed_at"],
            adapter_version=adapter["version"],
        )
    except (KeyError, TypeError, SocialMusicContractError):
        return False
    return dict(document) == rebuilt


__all__ = [
    "ADAPTER_ACCESS_SCOPE", "ADAPTER_AUTHORITY", "CAPABILITY_SCHEMA_VERSION",
    "DEFAULT_ADAPTER_VERSION", "MAX_COMMENTS", "SCHEMA_VERSION",
    "SUPPORTED_PLATFORMS",
    "TERMINAL_OUTCOMES", "SocialMusicContractError", "build_capability_manifest",
    "build_social_music_evidence", "canonical_json", "canonical_sha256",
    "capability_manifest", "social_music_readiness_reasons",
    "validate_capability_manifest",
    "validate_social_music_evidence", "verify_social_music_evidence_hash",
]
