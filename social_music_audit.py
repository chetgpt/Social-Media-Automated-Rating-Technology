"""Offline, collection-only CLI for platform-neutral social music evidence.

This module deliberately has no browser, network, subprocess, AI, analysis,
drafting, approval, or publication integration.  It accepts caller-supplied
collector JSON, normalizes it through the shared raw contract, and persists a
small allowlisted evidence envelope in :mod:`tiktok_scraper.social_music_state`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import parse_qs, unquote, urlsplit

from tiktok_scraper.raw_contract import finalize_content_record
from tiktok_scraper.social_music_contract import (
    DEFAULT_ADAPTER_VERSION,
    MAX_COMMENTS,
    SUPPORTED_PLATFORMS,
    capability_manifest,
    social_music_readiness_reasons,
    validate_social_music_evidence,
)
from tiktok_scraper.social_music_projection import social_music_evidence_from_record
from tiktok_scraper.social_music_state import (
    CheckpointConflict,
    SocialMusicState,
    SocialMusicStateError,
)


CLI_SCHEMA_VERSION = "social-music-audit-cli-v1"
SAFE_RECORD_SCHEMA_VERSION = "social-music-safe-record-v1"
EXPORT_SCHEMA_VERSION = "social-music-evidence-export-v1"
MAX_INPUT_BYTES = 64 * 1024 * 1024

_URL_RE = re.compile(r"(?i)(?:\b(?:https?|file)://|\bwww\.|\b(?:data|blob):)\S+")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_FORBIDDEN_KEY_FRAGMENT_RE = re.compile(
    r"(?i)(?:url|uri|cookie|authorization|token|secret|password|avatar|artwork|preview|embed|media_source)"
)
_NONTERMINAL_COMMENT_STATUSES = {"truncated", "partial", "unknown", "error"}
_METRIC_NAMES = (
    "views",
    "likes",
    "reported_comments",
    "shares",
    "saves",
    "followers",
)
_SOURCE_KEYS = {
    "mode",
    "target",
    "canonical_url",
    "target_content_id",
    "target_creator_handle",
    "target_creator_id",
    "provenance",
}
_SOURCE_PROVENANCE_KEYS = {"source", "method", "observed_at"}
_METRIC_KEYS = {"observed_at", "values", "availability"}
_COMMENT_KEYS = {
    "completion_status",
    "terminal",
    "comments_complete",
    "reported_count",
    "top_level_collected",
    "flat_collected",
    "comments_seen_in_response",
    "stored_count",
    "frontier",
    "items",
}
_COMMENT_FRONTIER_KEYS = {
    "comments_exhausted",
    "exhaustion_evidence",
    "comment_limit_reached",
    "pagination_cursor_present",
    "pagination_requests",
    "storage_truncated",
}
_COMMENT_ITEM_KEYS = {
    "comment_id",
    "parent_comment_id",
    "is_reply",
    "author",
    "text",
    "likes",
    "reply_count",
    "created_at",
}
_COLLECTOR_KEYS = {"status", "reason"}

_PLATFORM_HOSTS = {
    "tiktok": {"tiktok.com", "www.tiktok.com", "m.tiktok.com"},
    "youtube": {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"},
    "instagram": {"instagram.com", "www.instagram.com"},
    "facebook": {"facebook.com", "www.facebook.com", "m.facebook.com"},
    "x": {"x.com", "www.x.com", "twitter.com", "www.twitter.com"},
}


class CliError(ValueError):
    """A safe, user-facing CLI validation error."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _normalize_platform(value: Any) -> str:
    platform = str(value or "").strip().lower()
    if platform == "twitter":
        platform = "x"
    if platform not in SUPPORTED_PLATFORMS:
        raise CliError(f"unsupported platform: {platform or '<empty>'}")
    return platform


def _clean_identifier(value: Any, *, name: str) -> str:
    result = unquote(str(value or "")).strip()
    if (
        not result
        or len(result) > 256
        or any(character.isspace() for character in result)
        or re.fullmatch(r"[A-Za-z0-9._~-]+", result) is None
    ):
        raise CliError(f"{name} is invalid")
    return result


def _normalize_handle(value: Any) -> str:
    handle = unquote(str(value or "")).strip().lstrip("@").casefold()
    if (
        not handle
        or len(handle) > 200
        or any(character.isspace() for character in handle)
        or any(character in handle for character in "/\\?#")
    ):
        raise CliError("creator handle is invalid")
    return f"@{handle}"


def _url_parts(value: Any, platform: str):
    raw = str(value or "").strip()
    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise CliError("source URL is invalid") from exc
    hostname = str(parts.hostname or "").casefold()
    try:
        port = parts.port
    except ValueError as exc:
        raise CliError("source URL contains an invalid port") from exc
    if parts.scheme.casefold() not in {"http", "https"} or hostname not in _PLATFORM_HOSTS[platform]:
        raise CliError(f"source URL is not a canonical {platform} URL")
    if parts.username or parts.password or port not in (None, 80, 443):
        raise CliError("source URL contains unsupported authority data")
    return parts, hostname, [unquote(item) for item in parts.path.split("/") if item]


def _creator_target(platform: str, target: Any) -> tuple[str, str, str]:
    raw = str(target or "").strip()
    if raw.casefold().startswith("id:"):
        creator_id = _clean_identifier(raw[3:], name="creator ID")
        return f"id:{creator_id}", "", creator_id
    if raw.casefold().startswith(("http://", "https://")):
        parts, _hostname, segments = _url_parts(raw, platform)
        creator_id = ""
        handle = ""
        if platform == "youtube" and len(segments) >= 2 and segments[0].casefold() == "channel":
            creator_id = _clean_identifier(segments[1], name="creator ID")
        elif platform == "facebook" and (segments[:1] == ["profile.php"] or parts.path.endswith("profile.php")):
            creator_id = _clean_identifier(parse_qs(parts.query).get("id", [""])[0], name="creator ID")
        else:
            candidate = segments[0] if segments else ""
            if platform == "youtube" and candidate.casefold() in {"c", "user"} and len(segments) >= 2:
                candidate = segments[1]
            handle = _normalize_handle(candidate)
        if creator_id:
            return f"id:{creator_id}", "", creator_id
        return handle, handle, ""
    handle = _normalize_handle(raw)
    return handle, handle, ""


def _canonical_content_url(platform: str, value: Any) -> tuple[str, str]:
    parts, hostname, segments = _url_parts(value, platform)
    query = parse_qs(parts.query)
    content_id = ""
    if platform == "youtube":
        if hostname == "youtu.be" and segments:
            content_id = segments[0]
        elif segments[:1] == ["watch"]:
            content_id = query.get("v", [""])[0]
        elif segments and segments[0].casefold() in {"shorts", "embed", "live"} and len(segments) >= 2:
            content_id = segments[1]
        content_id = _clean_identifier(content_id, name="YouTube content ID")
        return f"https://www.youtube.com/watch?v={content_id}", content_id
    if platform == "tiktok":
        for index, segment in enumerate(segments[:-1]):
            if segment.casefold() in {"video", "photo"}:
                content_id = segments[index + 1]
                break
        content_id = _clean_identifier(content_id, name="TikTok content ID")
        creator = next((item for item in segments if item.startswith("@")), "")
        creator = _normalize_handle(creator) if creator else "@unknown"
        kind = "photo" if "photo" in [item.casefold() for item in segments] else "video"
        return f"https://www.tiktok.com/{creator}/{kind}/{content_id}", content_id
    if platform == "instagram":
        if len(segments) >= 2 and segments[0].casefold() in {"p", "reel", "reels", "tv"}:
            content_id = segments[1]
        content_id = _clean_identifier(content_id, name="Instagram content ID")
        return f"https://www.instagram.com/p/{content_id}/", content_id
    if platform == "x":
        lowered = [item.casefold() for item in segments]
        if "status" in lowered and lowered.index("status") + 1 < len(segments):
            content_id = segments[lowered.index("status") + 1]
        content_id = _clean_identifier(content_id, name="X content ID")
        creator = segments[0] if segments and segments[0].casefold() != "i" else "i"
        creator = "i" if creator == "i" else _normalize_handle(creator).lstrip("@")
        return f"https://x.com/{creator}/status/{content_id}", content_id
    if platform == "facebook":
        if "v" in query:
            content_id = query["v"][0]
        else:
            lowered = [item.casefold() for item in segments]
            for marker in ("videos", "reel"):
                if marker in lowered and lowered.index(marker) + 1 < len(segments):
                    content_id = segments[lowered.index(marker) + 1]
                    break
        content_id = _clean_identifier(content_id, name="Facebook content ID")
        return f"https://www.facebook.com/watch/?v={content_id}", content_id
    raise CliError("unsupported platform")


def _normalize_source_target(platform: str, source_mode: str, target: Any) -> str:
    if source_mode == "creator":
        return _creator_target(platform, target)[0]
    if source_mode == "url":
        return _canonical_content_url(platform, target)[0]
    normalized = " ".join(str(target or "").split())
    if not normalized or len(normalized) > 2_000:
        raise CliError("topic target is invalid")
    return normalized


def _source_binding(run: Mapping[str, Any]) -> dict[str, Any]:
    mode = str(run["source_mode"])
    target = str(run["target"])
    canonical_url = ""
    target_content_id = ""
    target_creator_handle = ""
    target_creator_id = ""
    if mode == "url":
        canonical_url, target_content_id = _canonical_content_url(str(run["platform"]), target)
    elif mode == "creator":
        target, target_creator_handle, target_creator_id = _creator_target(str(run["platform"]), target)
    return {
        "mode": mode,
        "target": target,
        "canonical_url": canonical_url,
        "target_content_id": target_content_id,
        "target_creator_handle": target_creator_handle,
        "target_creator_id": target_creator_id,
        "provenance": {
            "source": "immutable_run_scope",
            "method": {"topic": "topic_query", "creator": "exact_creator", "url": "exact_url"}[mode],
            "observed_at": str(run["created_at"]),
        },
    }


def _record_canonical_url(record: Mapping[str, Any], platform: str, content_id: str) -> str:
    brief = record.get("brief_evidence")
    brief_identity = brief.get("content_identity") if isinstance(brief, Mapping) else {}
    candidates = (
        record.get("canonical_url"),
        record.get("url"),
        record.get("video_url"),
        record.get("post_url"),
        record.get("permalink"),
        brief_identity.get("canonical_url") if isinstance(brief_identity, Mapping) else None,
    )
    for candidate in candidates:
        if candidate in (None, ""):
            continue
        try:
            canonical_url, candidate_id = _canonical_content_url(platform, candidate)
        except CliError:
            continue
        if candidate_id == content_id:
            return canonical_url
    return ""


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _safe_text(value: Any, limit: int) -> str:
    if value is None:
        return ""
    if isinstance(value, Mapping) or (
        isinstance(value, Sequence) and not isinstance(value, str)
    ) or isinstance(value, (set, frozenset)):
        raise CliError("structured values are not valid text evidence")
    text = _CONTROL_RE.sub("", str(value)).strip()
    text = _URL_RE.sub("[link removed]", text)
    return text[:limit]


def _safe_scalar(value: Any, *, text_limit: int = 256) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if value == value and value not in (float("inf"), float("-inf")) else None
    return _safe_text(value, text_limit)


def _first_present(source: Mapping[str, Any], names: Sequence[str], default: Any = "") -> Any:
    for name in names:
        if name in source and source[name] not in (None, ""):
            return source[name]
    return default


def _nonnegative_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed >= 0 else None


def _content_identity(record: Mapping[str, Any], platform: str) -> dict[str, Any]:
    brief = record.get("brief_evidence") if isinstance(record.get("brief_evidence"), Mapping) else {}
    brief_identity = (
        brief.get("content_identity") if isinstance(brief.get("content_identity"), Mapping) else {}
    )
    content_id = _safe_text(
        _first_present(
            brief_identity,
            ("content_id",),
            _first_present(record, ("video_id", "media_id", "content_id", "id")),
        ),
        256,
    )
    creator_value = _first_present(
        brief_identity,
        ("creator_handle", "username"),
        _first_present(
            record,
            ("creator_handle", "username", "content_creator", "creator", "author", "owner_username"),
        ),
    )
    if isinstance(creator_value, Mapping):
        creator_value = _first_present(
            creator_value, ("username", "unique_id", "handle", "name", "display_name")
        )
    creator_handle = _safe_text(creator_value, 256)
    creator_id = _safe_text(
        _first_present(
            brief_identity,
            ("creator_id",),
            _first_present(record, ("creator_id", "author_id", "owner_id", "channel_id")),
        ),
        256,
    )
    content_type = _safe_text(
        _first_present(
            brief_identity,
            ("content_type", "media_type"),
            _first_present(record, ("content_type", "media_type", "type")),
        ),
        80,
    )
    published_at = _safe_text(
        _first_present(
            brief_identity,
            ("published_at", "create_time"),
            _first_present(record, ("published_at", "created_at", "create_time", "timestamp")),
        ),
        80,
    )
    return {
        "platform": platform,
        "content_id": content_id,
        "creator_handle": creator_handle,
        "creator_id": creator_id,
        "content_type": content_type,
        "published_at": published_at,
    }


def _public_metrics(record: Mapping[str, Any]) -> dict[str, Any]:
    brief = record.get("brief_evidence") if isinstance(record.get("brief_evidence"), Mapping) else {}
    snapshot = brief.get("engagement_snapshot") if isinstance(brief.get("engagement_snapshot"), Mapping) else {}
    if not snapshot:
        snapshot = record.get("metrics") if isinstance(record.get("metrics"), Mapping) else record
    aliases: dict[str, tuple[str, ...]] = {
        "views": ("views", "view_count", "play_count", "playCount"),
        "likes": ("likes", "like_count", "digg_count", "diggCount"),
        "reported_comments": ("reported_comments", "comments", "comment_count", "commentCount"),
        "shares": ("shares", "share_count", "shareCount", "reposts", "retweet_count"),
        "saves": ("saves", "save_count", "collect_count", "collectCount"),
        "followers": ("followers", "follower_count", "followers_count"),
    }
    values: dict[str, Any] = {}
    for output_name, names in aliases.items():
        values[output_name] = _nonnegative_int(_first_present(snapshot, names, None))
    availability_source = (
        record.get("metric_availability")
        if isinstance(record.get("metric_availability"), Mapping)
        else {}
    )
    availability = {
        name: _safe_text(
            availability_source.get(name)
            or ("available" if values[name] is not None else "not_provided"),
            80,
        )
        for name in _METRIC_NAMES
    }
    return {
        "observed_at": _safe_text(
            _first_present(snapshot, ("observed_at", "collected_at"), record.get("collected_at", "")),
            80,
        ),
        "values": values,
        "availability": availability,
    }


def _iter_comments(comments: Any, parent_id: str = "") -> Iterable[tuple[Mapping[str, Any], str]]:
    if not isinstance(comments, list):
        return
    for comment in comments:
        if not isinstance(comment, Mapping):
            continue
        yield comment, parent_id
        comment_id = _safe_text(_first_present(comment, ("comment_id", "id")), 256)
        replies = comment.get("replies")
        if isinstance(replies, list):
            yield from _iter_comments(replies, comment_id or parent_id)


def _optional_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _compact_comments(record: Mapping[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    compact: list[dict[str, Any]] = []
    for comment, inherited_parent_id in _iter_comments(record.get("comments")):
        if len(compact) >= MAX_COMMENTS:
            return compact, True
        parent_id = _safe_text(
            _first_present(comment, ("parent_comment_id", "parent_id"), inherited_parent_id), 256
        )
        author_value = _first_present(comment, ("author", "username", "user_name", "display_name"))
        if isinstance(author_value, Mapping):
            author_value = _first_present(author_value, ("username", "unique_id", "name", "display_name"))
        compact.append(
            {
                "comment_id": _safe_text(_first_present(comment, ("comment_id", "id")), 256),
                "parent_comment_id": parent_id,
                "is_reply": bool(comment.get("is_reply") or parent_id),
                "author": _safe_text(author_value, 256),
                "text": _safe_text(_first_present(comment, ("text", "comment", "body")), 2_000),
                "likes": _nonnegative_int(_first_present(comment, ("likes", "like_count", "digg_count"), None)),
                "reply_count": _nonnegative_int(
                    _first_present(comment, ("reply_count", "replies_count"), None)
                ),
                "created_at": _safe_text(
                    _first_present(comment, ("created_at", "create_time", "timestamp")), 80
                ),
            }
        )
    return compact, False


def _comment_evidence(record: Mapping[str, Any]) -> dict[str, Any]:
    collection = (
        record.get("collection_status") if isinstance(record.get("collection_status"), Mapping) else {}
    )
    status = str(collection.get("completion_status") or "unknown").strip().lower() or "unknown"
    items, locally_truncated = _compact_comments(record)
    frontier = {
        "comments_exhausted": _optional_bool(collection.get("comments_exhausted")),
        "exhaustion_evidence": _safe_text(collection.get("comments_exhaustion_evidence"), 240),
        "comment_limit_reached": bool(collection.get("comment_limit_reached")),
        "pagination_cursor_present": bool(collection.get("pagination_cursor_present")),
        "pagination_requests": _nonnegative_int(collection.get("pagination_requests")),
        "storage_truncated": bool(collection.get("storage_truncated")) or locally_truncated,
    }
    return {
        "completion_status": status,
        "terminal": status not in _NONTERMINAL_COMMENT_STATUSES,
        "comments_complete": _optional_bool(collection.get("comments_complete")),
        "reported_count": _nonnegative_int(collection.get("reported_comment_count")),
        "top_level_collected": _nonnegative_int(collection.get("top_level_comments_collected")),
        "flat_collected": _nonnegative_int(collection.get("flat_comments_collected")),
        "comments_seen_in_response": _nonnegative_int(collection.get("comments_seen_in_response")),
        "stored_count": len(items),
        "frontier": frontier,
        "items": items,
    }


def _collector_error(record: Mapping[str, Any]) -> str:
    for key in ("error", "collector_error", "collection_error", "comment_error"):
        value = record.get(key)
        if value not in (None, "", False):
            return _safe_text(value, 500) or key
    return ""


def _collector_evidence(record: Mapping[str, Any]) -> dict[str, str]:
    failed = bool(_collector_error(record))
    return {"status": "error" if failed else "ok", "reason": "collector_error" if failed else ""}


def _safe_record_envelope(
    record: Mapping[str, Any], run: Mapping[str, Any], social_music: Mapping[str, Any]
) -> dict[str, Any]:
    platform = str(run["platform"])
    brief = record.get("brief_evidence") if isinstance(record.get("brief_evidence"), Mapping) else {}
    identity = _content_identity(record, platform)
    source = _source_binding(run)
    if not source["canonical_url"]:
        source["canonical_url"] = _record_canonical_url(
            record,
            platform,
            identity["content_id"],
        )
    caption = _first_present(brief, ("caption",), _first_present(record, ("caption", "title")))
    description = _first_present(
        brief, ("description",), _first_present(record, ("description", "video_description"))
    )
    return {
        "schema_version": SAFE_RECORD_SCHEMA_VERSION,
        "identity": identity,
        "source": source,
        "caption": _safe_text(caption, 5_000),
        "description": _safe_text(description, 10_000),
        "public_metrics": _public_metrics(record),
        "collector": _collector_evidence(record),
        "comments": _comment_evidence(record),
        "social_music": dict(social_music),
    }


def _social_manifest_hash(document: Mapping[str, Any]) -> str:
    manifest = document.get("capability_manifest")
    return str(manifest.get("manifest_hash") or "") if isinstance(manifest, Mapping) else ""


def _record_matches_source(
    run: Mapping[str, Any], raw: Mapping[str, Any], normalized: Mapping[str, Any]
) -> bool:
    binding = _source_binding(run)
    identity = _content_identity(normalized, str(run["platform"]))
    if binding["mode"] == "creator":
        if binding["target_creator_id"]:
            return identity["creator_id"] == binding["target_creator_id"]
        try:
            return _normalize_handle(identity["creator_handle"]) == binding["target_creator_handle"]
        except CliError:
            return False
    if binding["mode"] != "url":
        return True
    if identity["content_id"] != binding["target_content_id"]:
        return False
    brief = normalized.get("brief_evidence")
    brief_identity = brief.get("content_identity") if isinstance(brief, Mapping) else {}
    candidates = [
        raw.get("canonical_url"), raw.get("url"), raw.get("video_url"),
        raw.get("post_url"), raw.get("permalink"),
        brief_identity.get("canonical_url") if isinstance(brief_identity, Mapping) else None,
    ]
    for candidate in candidates:
        if candidate in (None, ""):
            continue
        try:
            _canonical, candidate_id = _canonical_content_url(str(run["platform"]), candidate)
        except CliError:
            return False
        if candidate_id != binding["target_content_id"]:
            return False
    return True


def _readiness_reasons(envelope: Mapping[str, Any]) -> list[str]:
    reasons: list[str] = []
    collector = envelope.get("collector")
    comments = envelope.get("comments")
    social_music = envelope.get("social_music")
    if not isinstance(collector, Mapping) or collector.get("status") != "ok":
        reasons.append("collector_error")
    if not isinstance(comments, Mapping):
        reasons.append("comments_unknown")
    else:
        status = str(comments.get("completion_status") or "unknown")
        if status in _NONTERMINAL_COMMENT_STATUSES:
            reasons.append(f"comments_{status}")
        frontier = comments.get("frontier")
        if isinstance(frontier, Mapping) and frontier.get("storage_truncated") is True:
            reasons.append("comments_storage_truncated")
    if isinstance(social_music, Mapping):
        reasons.extend(social_music_readiness_reasons(social_music))
    else:
        reasons.append("invalid_social_music_evidence")
    return list(dict.fromkeys(reasons))


def _validate_safe_envelope(
    envelope: Any, *, run: Mapping[str, Any], content_id: str
) -> None:
    expected_keys = {
        "schema_version",
        "identity",
        "source",
        "caption",
        "description",
        "public_metrics",
        "collector",
        "comments",
        "social_music",
    }
    if not isinstance(envelope, Mapping) or set(envelope) != expected_keys:
        raise CliError("stored evidence is not a closed safe-record envelope")
    if envelope.get("schema_version") != SAFE_RECORD_SCHEMA_VERSION:
        raise CliError("stored safe-record schema version is invalid")
    identity = envelope.get("identity")
    if not isinstance(identity, Mapping):
        raise CliError("stored evidence identity is invalid")
    if identity.get("platform") != run["platform"] or str(identity.get("content_id") or "") != content_id:
        raise CliError("stored evidence identity does not match its checkpoint")
    for key in identity:
        if key not in {
            "platform",
            "content_id",
            "creator_handle",
            "creator_id",
            "content_type",
            "published_at",
        }:
            raise CliError("stored evidence identity contains an unapproved field")
    source = envelope.get("source")
    expected_source = _source_binding(run)
    if isinstance(source, Mapping) and not expected_source["canonical_url"]:
        record_url = source.get("canonical_url")
        if record_url:
            try:
                canonical_url, source_content_id = _canonical_content_url(
                    str(run["platform"]),
                    record_url,
                )
            except CliError as exc:
                raise CliError("stored evidence source binding is invalid") from exc
            if canonical_url != record_url or source_content_id != content_id:
                raise CliError("stored evidence source binding is invalid")
            expected_source["canonical_url"] = canonical_url
    if (
        not isinstance(source, Mapping)
        or set(source) != _SOURCE_KEYS
        or not isinstance(source.get("provenance"), Mapping)
        or set(source["provenance"]) != _SOURCE_PROVENANCE_KEYS
        or dict(source) != expected_source
    ):
        raise CliError("stored evidence source binding is invalid")
    metrics = envelope.get("public_metrics")
    if not isinstance(metrics, Mapping) or set(metrics) != _METRIC_KEYS:
        raise CliError("stored public metrics are invalid")
    values, availability = metrics.get("values"), metrics.get("availability")
    if (
        not isinstance(values, Mapping)
        or set(values) != set(_METRIC_NAMES)
        or not isinstance(availability, Mapping)
        or set(availability) != set(_METRIC_NAMES)
        or any(value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0)
               for value in values.values())
        or any(not isinstance(value, str) or not value for value in availability.values())
    ):
        raise CliError("stored metric values/availability are invalid")
    collector = envelope.get("collector")
    if (
        not isinstance(collector, Mapping)
        or set(collector) != _COLLECTOR_KEYS
        or collector.get("status") not in {"ok", "error"}
        or collector.get("reason") != ("" if collector.get("status") == "ok" else "collector_error")
    ):
        raise CliError("stored collector outcome is invalid")
    comments = envelope.get("comments")
    if not isinstance(comments, Mapping) or set(comments) != _COMMENT_KEYS:
        raise CliError("stored compact comments are invalid")
    items, frontier = comments.get("items"), comments.get("frontier")
    if (
        comments.get("completion_status") not in {"complete", "truncated", "partial", "unknown", "error"}
        or comments.get("terminal") is not (
            comments.get("completion_status") not in _NONTERMINAL_COMMENT_STATUSES
        )
        or comments.get("comments_complete") not in {True, False, None}
        or not isinstance(items, list)
        or len(items) > MAX_COMMENTS
        or comments.get("stored_count") != len(items)
        or any(not isinstance(item, Mapping) or set(item) != _COMMENT_ITEM_KEYS for item in items)
        or not isinstance(frontier, Mapping)
        or set(frontier) != _COMMENT_FRONTIER_KEYS
        or not isinstance(frontier.get("storage_truncated"), bool)
    ):
        raise CliError("stored compact comment shape is invalid")
    for key in (
        "reported_count", "top_level_collected", "flat_collected",
        "comments_seen_in_response", "stored_count",
    ):
        value = comments.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise CliError("stored comment counts are invalid")
    flat_count = comments.get("flat_collected")
    if isinstance(flat_count, int) and flat_count > len(items) and not frontier["storage_truncated"]:
        raise CliError("stored comment truncation is not declared")
    if not validate_social_music_evidence(envelope.get("social_music")):
        raise CliError("stored social music evidence is invalid")
    _reject_forbidden_transport_fields(envelope)


def _reject_forbidden_transport_fields(value: Any, path: str = "evidence") -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            key_text = str(key)
            if path == "evidence.source" and (
                key_text == "canonical_url"
                or (key_text == "target" and value.get("mode") == "url")
            ):
                # The exact public URL is part of the immutable source binding;
                # it was canonicalized above and contains no transport secrets.
                continue
            # The contract itself has closed, safe provider fields such as
            # result hashes.  Reject transport-shaped fields anywhere else.
            if _FORBIDDEN_KEY_FRAGMENT_RE.search(key_text):
                raise CliError(f"forbidden transport or credential field at {path}.{key_text}")
            _reject_forbidden_transport_fields(nested, f"{path}.{key_text}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _reject_forbidden_transport_fields(nested, f"{path}[{index}]")
    elif isinstance(value, str) and _URL_RE.search(value):
        raise CliError(f"unsanitized URL in stored evidence at {path}")


def _verify_checkpoint_readiness(run: Mapping[str, Any], record: Mapping[str, Any]) -> None:
    evidence = record.get("evidence")
    _validate_safe_envelope(evidence, run=run, content_id=str(record["content_id"]))
    social_music = evidence["social_music"]
    if _social_manifest_hash(social_music) != run["capability_manifest_hash"]:
        raise CliError(f"capability manifest mismatch for {record['content_id']}")
    reasons = _readiness_reasons(evidence)
    calculated_ready = not reasons
    if bool(record.get("evidence_ready")) is not calculated_ready:
        raise CliError(f"stored readiness flag mismatch for {record['content_id']}")
    if str(record.get("collection_error") or "") != ";".join(reasons):
        raise CliError(f"stored readiness reasons mismatch for {record['content_id']}")


def _load_payload(path: Path) -> tuple[list[Mapping[str, Any]], str]:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise CliError(f"cannot read input file: {exc}") from exc
    if size > MAX_INPUT_BYTES:
        raise CliError(f"input file exceeds {MAX_INPUT_BYTES} bytes")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CliError(f"input file is not valid UTF-8 JSON: {exc}") from exc
    payload_platform = ""
    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, Mapping) and "videos" in payload:
        if not isinstance(payload.get("videos"), list):
            raise CliError("videos payload must contain a JSON list")
        records = payload["videos"]
        if payload.get("platform") not in (None, ""):
            payload_platform = str(payload["platform"])
    elif isinstance(payload, Mapping):
        records = [payload]
    else:
        raise CliError("input must be a record, a record list, or an object with a videos list")
    if any(not isinstance(item, Mapping) for item in records):
        raise CliError("every input record must be a JSON object")
    return list(records), payload_platform


def _source_context(run: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "source_kind": run["source_mode"],
        "source_value": run["target"],
        "target_mode": run["source_mode"],
    }


def _collection_context(run: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "posts_per_source": run["requested_count"],
        "transport_mode": "offline-json",
    }


def _emit(document: Any, *, stream: Any | None = None) -> None:
    (stream or sys.stdout).write(_canonical_json(document) + "\n")


def command_capabilities(args: argparse.Namespace) -> dict[str, Any]:
    platforms = [_normalize_platform(args.platform)] if args.platform else sorted(SUPPORTED_PLATFORMS)
    return {
        "schema_version": CLI_SCHEMA_VERSION,
        "capabilities": [
            capability_manifest(platform, adapter_version=args.adapter_version)
            for platform in platforms
        ],
    }


def command_create(args: argparse.Namespace) -> dict[str, Any]:
    platform = _normalize_platform(args.platform)
    if args.source_mode == "url" and args.posts != 1:
        raise CliError("url source mode requires --posts 1")
    target = _normalize_source_target(platform, args.source_mode, args.target)
    manifest = capability_manifest(platform, adapter_version=args.adapter_version)
    run_id = args.run_id or f"social_music_{uuid.uuid4().hex}"
    state = SocialMusicState(args.database)
    return state.create_run(
        run_id,
        platform=platform,
        source_mode=args.source_mode,
        target=target,
        requested_count=args.posts,
        capability_manifest_hash=manifest["manifest_hash"],
        adapter_version=manifest["adapter"]["version"],
    )


def command_status(args: argparse.Namespace) -> dict[str, Any]:
    state = SocialMusicState(args.database)
    run = state.status(args.run_id)
    for record in state.records(args.run_id):
        _verify_checkpoint_readiness(run, record)
    return run


def command_ingest(args: argparse.Namespace) -> dict[str, Any]:
    state = SocialMusicState(args.database)
    run = state.get_run(args.run_id)
    platform = _normalize_platform(run["platform"])
    expected_manifest_hash = capability_manifest(
        platform, adapter_version=run["adapter_version"]
    )["manifest_hash"]
    if run["capability_manifest_hash"] != expected_manifest_hash:
        raise CliError("run capability manifest no longer matches the platform contract")

    raw_records, payload_platform_value = _load_payload(Path(args.file))
    payload_platform = ""
    if payload_platform_value:
        try:
            payload_platform = _normalize_platform(payload_platform_value)
        except CliError:
            payload_platform = "invalid"

    existing = {row["content_id"]: row for row in state.records(args.run_id)}
    next_ordinal = max((row["ordinal"] for row in existing.values()), default=0) + 1
    seen_in_payload: set[str] = set()
    result: dict[str, Any] = {
        "schema_version": CLI_SCHEMA_VERSION,
        "run_id": args.run_id,
        "input_records": len(raw_records),
        "inserted": 0,
        "idempotent": 0,
        "evidence_ready": 0,
        "not_ready": 0,
        "platform_filtered": 0,
        "duplicate_input": 0,
        "known_content": 0,
        "invalid_records": 0,
        "conflicts": 0,
        "reasons": [],
    }

    for raw in raw_records:
        raw_platform_value = raw.get("platform") or payload_platform
        if raw_platform_value:
            try:
                raw_platform = _normalize_platform(raw_platform_value)
            except CliError:
                raw_platform = "invalid"
            if raw_platform != platform:
                result["platform_filtered"] += 1
                continue

        normalized = finalize_content_record(
            dict(raw),
            platform=platform,
            source_context=_source_context(run),
            collection_context=_collection_context(run),
            observed_at=(
                raw.get("observed_at")
                or raw.get("collected_at")
                or raw.get("timestamp")
                or run["created_at"]
            ),
        )
        identity = _content_identity(normalized, platform)
        content_id = identity["content_id"]
        if not content_id:
            result["invalid_records"] += 1
            result["reasons"].append("missing_content_id")
            continue
        if content_id in seen_in_payload:
            result["duplicate_input"] += 1
            continue
        seen_in_payload.add(content_id)

        if not _record_matches_source(run, raw, normalized):
            result["invalid_records"] += 1
            result["reasons"].append(f"source_identity_mismatch:{content_id}")
            continue

        if content_id not in existing and state.is_known_content(platform, content_id):
            result["known_content"] += 1
            continue

        social_music = social_music_evidence_from_record(
            normalized,
            platform=platform,
            adapter_version=run["adapter_version"],
        )
        if not validate_social_music_evidence(social_music):
            result["invalid_records"] += 1
            result["reasons"].append(f"invalid_social_music:{content_id}")
            continue
        if _social_manifest_hash(social_music) != run["capability_manifest_hash"]:
            result["invalid_records"] += 1
            result["reasons"].append(f"capability_manifest_mismatch:{content_id}")
            continue

        envelope = _safe_record_envelope(normalized, run, social_music)
        _validate_safe_envelope(envelope, run=run, content_id=content_id)

        readiness_reasons = _readiness_reasons(envelope)
        evidence_ready = not readiness_reasons
        ordinal = existing[content_id]["ordinal"] if content_id in existing else next_ordinal
        try:
            checkpoint = state.checkpoint_record(
                args.run_id,
                ordinal=ordinal,
                content_id=content_id,
                evidence=envelope,
                evidence_ready=evidence_ready,
                collection_error=";".join(readiness_reasons),
            )
        except CheckpointConflict as exc:
            result["conflicts"] += 1
            result["reasons"].append(f"checkpoint_conflict:{content_id}:{exc}")
            continue
        if checkpoint["inserted"]:
            result["inserted"] += 1
            existing[content_id] = checkpoint
            next_ordinal += 1
        else:
            result["idempotent"] += 1
        if evidence_ready:
            result["evidence_ready"] += 1
        else:
            result["not_ready"] += 1
            result["reasons"].extend(f"{reason}:{content_id}" for reason in readiness_reasons)

    result["reasons"] = list(dict.fromkeys(result["reasons"]))
    result["status"] = state.status(args.run_id)
    return result


def command_finalize(args: argparse.Namespace) -> dict[str, Any]:
    state = SocialMusicState(args.database)
    run = state.status(args.run_id)
    for record in state.records(args.run_id):
        _verify_checkpoint_readiness(run, record)
    return state.finalize(args.run_id, reasons=args.reason)


def _export_rows(state: SocialMusicState, run_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    run = state.status(run_id)
    if run["status"] != "collection_complete":
        raise CliError("export requires a collection_complete run")
    records = state.records(run_id)
    ready = [row for row in records if row["evidence_ready"]]
    if len(ready) != run["requested_count"] or run["evidence_ready"] != run["requested_count"]:
        raise CliError("export requires exactly the immutable requested number of evidence-ready rows")
    if len({row["content_id"] for row in ready}) != run["requested_count"]:
        raise CliError("export records are not unique by content ID")
    rows: list[dict[str, Any]] = []
    for record in sorted(ready, key=lambda item: item["ordinal"]):
        if _hash_json(record["evidence"]) != record["evidence_hash"]:
            raise CliError(f"stored evidence hash mismatch for {record['content_id']}")
        _verify_checkpoint_readiness(run, record)
        rows.append(
            {
                "schema_version": EXPORT_SCHEMA_VERSION,
                "run_id": run_id,
                "platform": run["platform"],
                "source_mode": run["source_mode"],
                "target": run["target"],
                "scope_hash": run["scope_hash"],
                "ordinal": record["ordinal"],
                "content_id": record["content_id"],
                "evidence_hash": record["evidence_hash"],
                "evidence": record["evidence"],
            }
        )
    return run, rows


def _atomic_write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> str:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(_canonical_json(row) + "\n" for row in rows).encode("utf-8")
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
    except OSError as exc:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise CliError(f"could not write export: {exc}") from exc
    return hashlib.sha256(payload).hexdigest()


def command_export(args: argparse.Namespace) -> dict[str, Any]:
    database_path = Path(args.database).resolve()
    export_path = Path(args.file).resolve()
    same_file = export_path == database_path
    if not same_file and database_path.exists() and export_path.exists():
        try:
            same_file = os.path.samefile(database_path, export_path)
        except OSError:
            same_file = False
    if same_file:
        raise CliError("export file must not replace the state database")
    state = SocialMusicState(args.database)
    run, rows = _export_rows(state, args.run_id)
    export_hash = _atomic_write_jsonl(export_path, rows)
    return {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "run_id": args.run_id,
        "platform": run["platform"],
        "records": len(rows),
        "file": str(export_path),
        "export_hash": export_hash,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Offline social music evidence collection state")
    parser.add_argument(
        "--database",
        required=True,
        help="Path to the platform-neutral collection state database (must precede the command)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    capabilities_parser = subparsers.add_parser("capabilities", help="Show closed platform capabilities")
    capabilities_parser.add_argument("--platform", choices=sorted(SUPPORTED_PLATFORMS))
    capabilities_parser.add_argument("--adapter-version", default=DEFAULT_ADAPTER_VERSION)
    capabilities_parser.set_defaults(handler=command_capabilities)

    create_parser = subparsers.add_parser("create", help="Create an immutable collection run")
    create_parser.add_argument("--platform", required=True, choices=sorted(SUPPORTED_PLATFORMS))
    create_parser.add_argument("--source-mode", required=True, choices=("topic", "creator", "url"))
    create_parser.add_argument("--target", required=True)
    create_parser.add_argument("--posts", required=True, type=_positive_int)
    create_parser.add_argument("--run-id")
    create_parser.add_argument("--adapter-version", default=DEFAULT_ADAPTER_VERSION)
    create_parser.set_defaults(handler=command_create)

    ingest_parser = subparsers.add_parser("ingest", help="Ingest offline collector JSON")
    ingest_parser.add_argument("--run-id", required=True)
    ingest_parser.add_argument("--file", required=True)
    ingest_parser.set_defaults(handler=command_ingest)

    status_parser = subparsers.add_parser("status", help="Show durable run counters and status")
    status_parser.add_argument("--run-id", required=True)
    status_parser.set_defaults(handler=command_status)

    finalize_parser = subparsers.add_parser("finalize", help="Fail closed at the immutable cardinality")
    finalize_parser.add_argument("--run-id", required=True)
    finalize_parser.add_argument("--reason", action="append", default=[])
    finalize_parser.set_defaults(handler=command_finalize)

    export_parser = subparsers.add_parser("export", help="Atomically export a complete run as JSONL")
    export_parser.add_argument("--run-id", required=True)
    export_parser.add_argument("--file", required=True)
    export_parser.set_defaults(handler=command_export)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.handler(args)
    except (CliError, SocialMusicStateError, OSError) as exc:
        _emit(
            {"schema_version": CLI_SCHEMA_VERSION, "status": "error", "error": str(exc)},
            stream=sys.stderr,
        )
        return 1
    _emit(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
