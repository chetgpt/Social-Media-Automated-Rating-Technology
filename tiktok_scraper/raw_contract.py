"""Platform-neutral raw evidence contract for brief-ready collection.

The existing scrapers intentionally keep their platform-native fields.  This
module adds a stable evidence envelope without deleting or renaming those
fields, so older compilers remain compatible while newer reporting code can
reason about provenance, truncation, and unavailable first-party metrics.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from typing import Any, Iterable


RAW_SCHEMA_VERSION = "2.1"
TRANSPORT_MODES = {"api-first", "api-only", "hybrid", "dom"}
FIRST_PARTY_ONLY_FIELDS = (
    "geography",
    "demographics",
    "reach",
    "impressions",
    "traffic",
    "link_clicks",
    "conversions",
)
PUBLIC_METRIC_ALIASES = {
    "views": ("view_count", "views", "play_count"),
    "likes": ("like_count", "likes", "digg_count"),
    "reported_comments": ("reported_comment_count", "comment_count", "comments_count"),
    "shares": ("share_count", "shares"),
    "saves": ("save_count", "saves", "collect_count"),
    "followers": ("follower_count", "followers", "subscriber_count"),
}
AVAILABLE_METRIC_STATUSES = {"available", "available_public", "available_first_party"}


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return max(0, int(value))
    text = _text(value).lower().replace(",", "")
    match = re.fullmatch(r"(-?\d+(?:\.\d+)?)\s*([kmb]?)", text)
    if not match:
        return None
    multiplier = {"": 1, "k": 1_000, "m": 1_000_000, "b": 1_000_000_000}[match.group(2)]
    return max(0, int(float(match.group(1)) * multiplier))


def _timestamp_iso(value: Any, *, reference_time: Any = None) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)) or re.fullmatch(r"\d+(?:\.\d+)?", _text(value)):
        try:
            timestamp = float(value)
            if timestamp > 10_000_000_000:
                timestamp /= 1000
            return dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc).replace(
                microsecond=0
            ).isoformat()
        except (OverflowError, OSError, TypeError, ValueError):
            return ""
    text = _text(value)
    try:
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).replace(
            microsecond=0
        ).isoformat()
    except ValueError:
        pass

    # Platform comment APIs often return values such as "3 days ago" or
    # "3 hari yang lalu". Anchor those to collection time so repeated report
    # compilation does not keep moving the timestamp.
    try:
        from tiktok_scraper.date_filter import parse_datetime

        reference = parse_datetime(reference_time) if reference_time else None
        parsed = parse_datetime(text, now=reference)
        return parsed.replace(microsecond=0).isoformat() if parsed else ""
    except (ImportError, OverflowError, OSError, TypeError, ValueError):
        return ""


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return _text(value).casefold() in {"1", "true", "yes", "on"}


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _now_iso() -> str:
    return dt.datetime.now().astimezone().replace(microsecond=0).isoformat()


def _env_json(name: str) -> dict[str, Any]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def transport_mode(value: str | None = None) -> str:
    mode = _text(value or os.environ.get("SCRAPER_TRANSPORT_MODE") or "api-first").lower()
    return mode if mode in TRANSPORT_MODES else "api-first"


def source_context_from_env() -> dict[str, Any]:
    return _env_json("SCRAPER_SOURCE_CONTEXT_JSON")


def collection_context_from_env() -> dict[str, Any]:
    return _env_json("SCRAPER_COLLECTION_CONTEXT_JSON")


def run_context_from_env() -> dict[str, Any]:
    return _env_json("SCRAPER_RUN_CONTEXT_JSON")


def _iter_comments(comments: Any) -> Iterable[dict[str, Any]]:
    if not isinstance(comments, list):
        return
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        yield comment
        for key in ("replies", "reply_comments", "children"):
            yield from _iter_comments(comment.get(key))


def _comment_counts(comments: Any, *, platform: str = "") -> tuple[int, int]:
    roots = (
        [comment for comment in comments if isinstance(comment, dict)]
        if isinstance(comments, list)
        else []
    )
    flat = list(_iter_comments(comments))
    comment_ids = {
        _text(_first(comment, "comment_id", "id", "pk", "cid"))
        for comment in flat
        if isinstance(comment, dict)
    }
    comment_ids.discard("")
    # A parent id outside the collected comment ids normally points to the
    # root post (not another comment), so it is top-level for this dataset.
    top_level = 0
    for comment in roots:
        parent_id = _text(_first(comment, "parent_comment_id", "parent_id", "parent_comment_pk"))
        if parent_id:
            if parent_id not in comment_ids:
                top_level += 1
            continue
        if _bool(comment.get("is_reply")) and platform not in {"x", "twitter"}:
            continue
        top_level += 1
    return top_level, len(flat)


def _first(record: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in record and record.get(key) not in (None, ""):
            return record.get(key)
    return None


def _metric_containers(record: dict[str, Any]) -> Iterable[dict[str, Any]]:
    yield record
    for name in ("stats", "statistics", "metrics", "engagement"):
        value = record.get(name)
        if isinstance(value, dict):
            yield value


def _metric_value(record: dict[str, Any], aliases: tuple[str, ...]) -> tuple[int | None, bool]:
    for container in _metric_containers(record):
        for key in aliases:
            if key in container and container.get(key) not in (None, ""):
                return _int_or_none(container.get(key)), True
    return None, False


def _provided_metric_status(record: dict[str, Any], metric: str) -> str:
    evidence = _dict(record.get("brief_evidence"))
    for container in (
        _dict(record.get("metric_availability")),
        _dict(evidence.get("metric_availability")),
        _dict(record.get("field_availability")),
    ):
        status = _text(container.get(metric)).casefold()
        if status:
            return status
    return ""


def _metric_status(
    record: dict[str, Any],
    *,
    platform: str,
    metric: str,
    value: int | None,
    explicit: bool,
) -> str:
    provided = _provided_metric_status(record, metric)
    if provided:
        if provided in AVAILABLE_METRIC_STATUSES and value is None:
            return "missing_from_public_response"
        return provided
    if value is not None and value > 0:
        return "available"

    content_type = _text(record.get("content_type") or record.get("type")).casefold()
    url = _text(record.get("url") or record.get("video_url")).casefold()
    if platform == "instagram":
        if metric == "views" and (
            content_type in {"1", "photo", "image", "carousel", "8"}
            and "/reel/" not in url
        ):
            return "not_applicable"
        if metric in {"shares", "saves"}:
            return "not_publicly_exposed"
        if metric in {"likes", "reported_comments"} and explicit and value is not None:
            return "available"
    elif platform == "youtube":
        if metric in {"shares", "saves"}:
            return "not_publicly_exposed"
        if explicit and value is not None:
            return "available"
    elif platform in {"x", "twitter"}:
        if metric == "views" and not explicit:
            return "not_publicly_exposed"
        if explicit and value is not None:
            return "available"

    # Legacy TikTok/Facebook records used zero as a placeholder. New records
    # carry an explicit availability map, allowing a real observed zero to be
    # distinguished from a missing response field.
    return "missing_from_public_response"


def _publication(record: dict[str, Any]) -> tuple[str, str, str]:
    for key in (
        "published_at",
        "published",
        "publish_date",
        "published_time",
        "create_time",
        "createTime",
        "taken_at",
        "created_at",
    ):
        value = _text(record.get(key))
        if not value:
            continue
        confidence = "low"
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[T ].*)?", value):
            confidence = "high"
        elif re.fullmatch(r"\d{1,2}-\d{1,2}", value):
            confidence = "medium"
        return value, key, confidence
    return "", "", "none"


def _extract_tags(text: str) -> tuple[list[str], list[str]]:
    hashtags: list[str] = []
    mentions: list[str] = []
    seen_hash: set[str] = set()
    seen_mention: set[str] = set()
    for value in re.findall(r"(?<!\w)#([\w.]+)", text or "", flags=re.UNICODE):
        key = value.casefold()
        if key not in seen_hash:
            seen_hash.add(key)
            hashtags.append(value)
    for value in re.findall(r"(?<!\w)@([\w.]+)", text or "", flags=re.UNICODE):
        key = value.casefold()
        if key not in seen_mention:
            seen_mention.add(key)
            mentions.append(value)
    return hashtags, mentions


def _method(record: dict[str, Any], *keys: str, default: str = "unknown") -> str:
    value = _first(record, *keys)
    return _text(value) or default


def _has_api_marker(*methods: str) -> bool:
    markers = ("api", "graphql", "innertube", "timedtext", "network")
    return any(any(marker in method.casefold() for marker in markers) for method in methods)


def _has_dom_marker(*methods: str) -> bool:
    markers = ("dom", "ui", "click", "scroll_card")
    return any(any(marker in method.casefold() for marker in markers) for method in methods)


def finalize_comment(
    comment: dict[str, Any],
    *,
    platform: str,
    observed_at: str,
    comment_method: str,
    content_id: str = "",
    parent_comment_id: str = "",
    is_reply: bool = False,
) -> dict[str, Any]:
    output = dict(comment)
    user = _dict(output.get("user") or output.get("owner") or output.get("from"))
    comment_id = _text(_first(output, "comment_id", "id", "pk", "cid"))
    parent_id = _text(
        _first(output, "parent_comment_id", "parent_id", "parent_comment_pk")
        or parent_comment_id
    )
    author = _text(
        _first(output, "author", "username", "user_name")
        or _first(user, "unique_id", "username", "full_name", "nickname", "name")
    )
    author_id = _text(
        _first(output, "author_id", "user_id", "from_id")
        or _first(user, "pk", "id", "uid", "user_id")
    )
    author_display_name = _text(
        _first(output, "author_display_name", "author_name")
        or _first(user, "full_name", "nickname", "name")
    )
    comment_text = _text(_first(output, "text", "comment", "content", "message"))
    likes = _int_or_none(_first(output, "likes", "like_count", "digg_count"))
    reply_count = _int_or_none(_first(output, "reply_count", "child_comment_count"))
    created_source = ""
    created_raw: Any = None
    for key in ("comment_created_at", "time", "create_time", "created_at", "timestamp", "taken_at"):
        if key in output and output.get(key) not in (None, ""):
            created_source = key
            created_raw = output.get(key)
            break
    created_at = _timestamp_iso(created_raw, reference_time=observed_at)
    reply_flag = bool(is_reply or parent_id or _bool(output.get("is_reply")))

    output.setdefault("platform", platform)
    output.setdefault("observed_at", observed_at)
    output.setdefault("collection_method", comment_method)
    output.setdefault("raw_schema_version", RAW_SCHEMA_VERSION)
    output.setdefault("comment_id", comment_id)
    output.setdefault("parent_comment_id", parent_id)
    output.setdefault("author", author)
    output.setdefault("author_id", author_id)
    output.setdefault("author_display_name", author_display_name)
    if likes is not None:
        output.setdefault("likes", likes)
    if reply_count is not None:
        output.setdefault("reply_count", reply_count)
    if created_at or "comment_created_at" not in output:
        output["comment_created_at"] = created_at
    output["is_reply"] = reply_flag

    for key in ("replies", "reply_comments", "children"):
        replies = output.get(key)
        if not isinstance(replies, list):
            continue
        output[key] = [
            finalize_comment(
                reply,
                platform=platform,
                observed_at=observed_at,
                comment_method=comment_method,
                content_id=content_id,
                parent_comment_id=comment_id or parent_id,
                is_reply=True,
            )
            if isinstance(reply, dict)
            else reply
            for reply in replies
        ]

    output["comment_evidence"] = {
        "schema_version": RAW_SCHEMA_VERSION,
        "observed_at": observed_at,
        "identity": {
            "content_id": content_id,
            "comment_id": comment_id,
            "parent_comment_id": parent_id,
            "is_reply": reply_flag,
        },
        "author": {
            "username_or_name": author,
            "author_id": author_id,
            "display_name": author_display_name,
            "verified": output.get("author_verified", user.get("verified")),
        },
        "publication": {
            "raw_value": created_raw,
            "source_field": created_source,
            "normalized_at": created_at,
        },
        "engagement": {
            "likes": likes,
            "reported_replies": reply_count,
        },
        "text_present": bool(comment_text),
        "collection": {
            "platform": platform,
            "method": comment_method,
        },
    }
    return output


def finalize_content_record(
    record: dict[str, Any],
    *,
    platform: str | None = None,
    source_context: dict[str, Any] | None = None,
    collection_context: dict[str, Any] | None = None,
    observed_at: str | None = None,
) -> dict[str, Any]:
    """Return a copy of a platform record with a brief-ready evidence block."""

    output = dict(record)
    platform = _text(platform or output.get("platform")).lower() or "unknown"
    source_context = dict(source_context or source_context_from_env())
    collection_context = dict(collection_context or collection_context_from_env())
    observed_at = _text(observed_at or output.get("observed_at")) or _now_iso()
    existing_source = _dict(output.get("source_provenance"))
    existing_transport = _dict(output.get("transport_provenance"))
    existing_collection = _dict(output.get("collection_status"))
    requested_mode = transport_mode(
        collection_context.get("transport_mode")
        or existing_transport.get("requested_mode")
    )

    discovery_method = _method(
        output,
        "discovery_method",
        "discovery_source",
        default=(
            _text(existing_transport.get("discovery_method"))
            or _text(source_context.get("discovery_method"))
            or "unknown"
        ),
    )
    metadata_method = _method(
        output,
        "metadata_method",
        default=_text(existing_transport.get("metadata_method")) or "unknown",
    )
    comment_method = _method(
        output,
        "comment_method",
        "source",
        default=_text(existing_transport.get("comment_method")) or "unknown",
    )
    transcript_method = _method(
        output,
        "transcript_method",
        "transcript_source",
        default=_text(existing_transport.get("transcript_method")) or "not_applicable",
    )
    dom_used = _has_dom_marker(discovery_method, metadata_method, comment_method)
    api_data_used = _has_api_marker(discovery_method, metadata_method, comment_method, transcript_method)
    fallback_used = (
        _bool(output.get("fallback_used"))
        or _bool(existing_transport.get("fallback_used"))
        or "fallback" in " ".join(
            (discovery_method, metadata_method, comment_method, transcript_method)
        ).casefold()
        or (requested_mode in {"api-first", "api-only"} and dom_used)
    )

    content_id = _text(output.get("video_id") or output.get("media_id") or output.get("id"))
    comments = output.get("comments") if isinstance(output.get("comments"), list) else []
    comments = [
        finalize_comment(
            comment,
            platform=platform,
            observed_at=observed_at,
            comment_method=comment_method,
            content_id=content_id,
        )
        if isinstance(comment, dict)
        else comment
        for comment in comments
    ]
    output["comments"] = comments
    top_level_comments, flat_comments = _comment_counts(comments, platform=platform)

    post_limit_raw = (
        collection_context.get("posts_per_source")
        if "posts_per_source" in collection_context
        else collection_context.get("videos_per_source")
        if "videos_per_source" in collection_context
        else existing_collection.get("post_limit_per_source")
    )
    comment_limit_raw = (
        collection_context.get("comments_per_post")
        if "comments_per_post" in collection_context
        else collection_context.get("comment_limit")
        if "comment_limit" in collection_context
        else existing_collection.get("comment_limit_per_post")
    )
    candidate_limit_raw = (
        collection_context.get("discovery_candidates_per_source")
        if "discovery_candidates_per_source" in collection_context
        else existing_collection.get("candidate_limit_per_source")
    )
    post_limit = _int_or_none(post_limit_raw)
    candidate_limit = _int_or_none(candidate_limit_raw)
    comment_limit = _int_or_none(comment_limit_raw)
    reported_raw = (
        existing_collection.get("reported_comment_count")
        if "reported_comment_count" in existing_collection
        else output.get("reported_comment_count")
    )
    reported_comments = _int_or_none(reported_raw)
    if reported_comments == 0 and flat_comments > 0:
        reported_comments = None
    comments_seen_raw = _first(output, "comments_seen_in_response", "browser_api_comment_count")
    if comments_seen_raw is None:
        comments_seen_raw = existing_collection.get("comments_seen_in_response")
    comments_seen_in_response = _int_or_none(comments_seen_raw)
    explicit_limit_raw = _first(output, "comment_limit_reached", "limit_reached")
    if explicit_limit_raw is None:
        explicit_limit_raw = existing_collection.get("comment_limit_reached")
    explicit_limit_reached = _bool(explicit_limit_raw)
    limit_reached = explicit_limit_reached or bool(
        comment_limit and top_level_comments >= comment_limit
    )
    exhausted_raw = _first(output, "comments_exhausted", "exhausted")
    if exhausted_raw is None and "comments_exhausted" in existing_collection:
        exhausted_raw = existing_collection.get("comments_exhausted")
    comments_exhausted = _bool(exhausted_raw) if exhausted_raw is not None else None
    error = _text(output.get("error") or output.get("comment_error"))
    exhaustion_evidence = "explicit" if exhausted_raw is not None else ""
    if (
        comments_exhausted is None
        and platform == "youtube"
        and comment_method == "youtube_internal_api"
        and not explicit_limit_reached
        and not error
    ):
        # Schema 2.0's YouTube loop only left this field unset when the
        # generator returned normally; exceptions and explicit caps were
        # already recorded separately.
        comments_exhausted = True
        exhaustion_evidence = "legacy_generator_completed_without_error_or_limit"
    storage_truncated = bool(
        comments_seen_in_response is not None
        and comments_seen_in_response > flat_comments
    )
    if error:
        completion_status = "error"
        comments_complete: bool | None = False
    elif reported_comments is not None and top_level_comments >= reported_comments and not storage_truncated:
        completion_status = "complete"
        comments_complete = True
    elif limit_reached or storage_truncated:
        completion_status = "truncated"
        comments_complete = False
    elif comments_exhausted:
        completion_status = "complete"
        comments_complete = True
    elif reported_comments is not None and top_level_comments < reported_comments:
        completion_status = "partial"
        comments_complete = False
    elif comments_exhausted is False:
        completion_status = "partial"
        comments_complete = False
    else:
        completion_status = "unknown"
        comments_complete = None

    published_at, published_source, published_confidence = _publication(output)
    caption = _text(output.get("caption") or output.get("title"))
    description = _text(output.get("description") or output.get("video_description"))
    transcript = _text(output.get("transcript"))
    transcript_status = _text(output.get("transcript_status")).casefold()
    corpus = "\n".join(value for value in (caption, description, transcript) if value)
    hashtags, mentions = _extract_tags(corpus)
    creator = _text(
        output.get("content_creator")
        or output.get("username")
        or output.get("creator")
        or output.get("author")
    )
    creator_id = _text(
        output.get("creator_id")
        or output.get("author_id")
        or output.get("owner_id")
        or output.get("user_id")
    )

    metric_values: dict[str, int | None] = {}
    metric_availability: dict[str, str] = {}
    for metric, aliases in PUBLIC_METRIC_ALIASES.items():
        value, explicit = _metric_value(output, aliases)
        if metric == "reported_comments":
            value = reported_comments
            explicit = reported_raw is not None
        status = _metric_status(
            output,
            platform=platform,
            metric=metric,
            value=value,
            explicit=explicit,
        )
        metric_availability[metric] = status
        metric_values[metric] = value if status in AVAILABLE_METRIC_STATUSES else None
    metric_availability.update({field: "first_party_required" for field in FIRST_PARTY_ONLY_FIELDS})
    engagement = {"observed_at": observed_at, **metric_values}
    public_engagement_available = any(
        engagement[name] is not None
        for name in ("views", "likes", "reported_comments", "shares", "saves")
    )
    comment_text_available = any(_text(comment.get("text")) for comment in _iter_comments(comments))
    text_available = bool(corpus or comment_text_available)
    music_available = any(
        _text(_first(output, key))
        for key in ("music_id", "music_title", "music_author", "sound_title", "audio_title")
    )
    content_location_available = bool(
        _text(output.get("content_location_name"))
        or _text(output.get("content_location_id"))
        or output.get("content_location_latitude") is not None
        or output.get("content_location_longitude") is not None
    )

    source_provenance = {
        "source_key": _text(source_context.get("source_key") or output.get("source_key") or existing_source.get("source_key")),
        "source_kind": _text(source_context.get("kind") or source_context.get("source_kind") or output.get("source_kind") or existing_source.get("source_kind")),
        "source_value": _text(source_context.get("value") or source_context.get("source_value") or output.get("source_value") or existing_source.get("source_value")),
        "source_label": _text(source_context.get("label") or source_context.get("source_label") or output.get("source_label") or existing_source.get("source_label")),
        "source_role": _text(source_context.get("role") or source_context.get("source_role") or output.get("source_role") or existing_source.get("source_role")),
        "target_mode": _text(source_context.get("target_mode") or output.get("target_mode") or existing_source.get("target_mode")),
        "taxonomy": _dict(source_context.get("taxonomy") or output.get("source_taxonomy") or existing_source.get("taxonomy")),
        "matched_keywords": list(output.get("matched_keywords") or existing_source.get("matched_keywords") or []),
    }
    transport = {
        "requested_mode": requested_mode,
        "discovery_method": discovery_method,
        "metadata_method": metadata_method,
        "comment_method": comment_method,
        "transcript_method": transcript_method,
        "api_data_used": api_data_used,
        "dom_used": dom_used,
        "fallback_used": fallback_used,
        "browser_bootstrap_used": (
            _bool(existing_transport.get("browser_bootstrap_used"))
            or any(
                marker in " ".join((discovery_method, metadata_method, comment_method)).casefold()
                for marker in ("browser", "captured", "graphql_response")
            )
        ),
    }
    cursor_raw = _first(output, "browser_api_cursor_present", "cursor_present")
    if cursor_raw is None:
        cursor_raw = existing_collection.get("pagination_cursor_present")
    pagination_requests_raw = _first(output, "pagination_requests", "browser_api_replay_attempts")
    if pagination_requests_raw is None:
        pagination_requests_raw = existing_collection.get("pagination_requests")
    collection = {
        "candidate_limit_per_source": candidate_limit,
        "post_limit_per_source": post_limit,
        "comment_limit_per_post": comment_limit,
        "top_level_comments_collected": top_level_comments,
        "flat_comments_collected": flat_comments,
        "reported_comment_count": reported_comments,
        "comments_seen_in_response": comments_seen_in_response,
        "comments_exhausted": comments_exhausted,
        "comments_exhaustion_evidence": exhaustion_evidence,
        "comment_limit_reached": limit_reached,
        "storage_truncated": storage_truncated,
        "comments_complete": comments_complete,
        "completion_status": completion_status,
        "pagination_cursor_present": _bool(cursor_raw),
        "pagination_requests": _int_or_none(pagination_requests_raw),
    }

    field_availability = {
        "publication_time": "available" if published_at else "missing_from_public_response",
        "creator_identity": "available" if creator or creator_id else "missing_from_public_response",
        "caption_or_description": "available" if caption or description else "missing_from_public_response",
        "comment_text": "available" if flat_comments else "empty_or_not_collected",
        "public_engagement": "available" if public_engagement_available else "missing_from_public_response",
        **{metric: status for metric, status in metric_availability.items()},
        "transcript": (
            "available"
            if transcript
            else "not_available_or_disabled"
            if transcript_status in {"unavailable", "disabled"}
            else "collection_failed"
            if transcript_status
            else "not_collected"
        ),
        "music_metadata": "available" if music_available else "not_collected",
        "hashtags": "derivable_from_text" if caption or description else "missing_text",
        "mentions": "derivable_from_text" if caption or description else "missing_text",
        "language": "available" if _text(output.get("content_language")) else "derivable_from_text" if text_available else "missing_text",
        "content_location": "available" if content_location_available else "not_exposed",
    }
    source_taxonomy = source_provenance.get("taxonomy") or {}
    source_role = _text(source_provenance.get("source_role")).casefold()
    publisher_status = (
        "ready_with_account_taxonomy"
        if source_taxonomy or source_role in {"owned", "publisher", "media", "talent", "cast", "community"}
        else "taxonomy_required"
        if creator
        else "missing_creator"
    )
    analysis_readiness = {
        "sentiment": "ready_for_classification" if comment_text_available else "insufficient_comments",
        "entity_analysis": "ready_for_classification" if text_available else "insufficient_text",
        "publisher_classification": publisher_status,
        "audience_interest_classification": "inferential_only" if flat_comments else "insufficient_comments",
        "activation_theme_aggregation": "ready_for_classification" if text_available else "insufficient_text",
        "monthly_reporting": "ready" if published_at else "observation_time_only",
        "report_qa": "ready",
        "geography": "first_party_required",
        "demographics": "first_party_required",
        "reach_and_traffic": "first_party_required",
    }

    quality_flags: list[str] = []
    if not published_at:
        quality_flags.append("unknown_publication_time")
    if not creator and not creator_id:
        quality_flags.append("missing_creator_identity")
    if not caption and not description:
        quality_flags.append("missing_caption_and_description")
    if not public_engagement_available:
        quality_flags.append("missing_public_engagement")
    if completion_status == "truncated":
        quality_flags.append("comments_truncated_by_limit")
    if completion_status == "partial":
        quality_flags.append("comments_partially_collected")
    if completion_status == "unknown":
        quality_flags.append("comment_completeness_unknown")
    if dom_used:
        quality_flags.append("dom_transport_used")
    if fallback_used:
        quality_flags.append("transport_fallback_used")
    if requested_mode == "api-only" and dom_used:
        quality_flags.append("api_only_dom_violation")
    if error:
        quality_flags.append("collection_error")
    if transcript_status not in {"", "ok", "unavailable", "disabled"}:
        quality_flags.append("transcript_collection_error")

    social_music_evidence: dict[str, Any] = {}
    if platform in {"tiktok", "instagram", "facebook", "x", "youtube"}:
        from .social_music_projection import social_music_evidence_from_record

        music_source_record = dict(output)
        music_source_record["platform"] = platform
        music_source_record["observed_at"] = observed_at
        social_music_evidence = social_music_evidence_from_record(
            music_source_record,
            platform=platform,
        )

    evidence = {
        "schema_version": RAW_SCHEMA_VERSION,
        "observed_at": observed_at,
        "content_identity": {
            "platform": platform,
            "content_id": content_id,
            "media_id": _text(output.get("media_id")),
            "canonical_url": _text(output.get("url") or output.get("video_url")),
            "content_type": _text(output.get("content_type") or output.get("type")),
        },
        "publication": {
            "raw_value": published_at,
            "source_field": published_source,
            "confidence": published_confidence,
        },
        "creator": {
            "username_or_name": creator,
            "creator_id": creator_id,
            "display_name": _text(output.get("creator_display_name") or output.get("author_name")),
            "verified": output.get("creator_verified"),
        },
        "text": {
            "hashtags": hashtags,
            "mentions": mentions,
            "caption_present": bool(caption),
            "description_present": bool(description),
            "transcript_present": bool(transcript),
            "transcript_segment_count": _int_or_none(output.get("transcript_segment_count")),
            "transcript_status": transcript_status,
            "transcript_source": _text(output.get("transcript_source")),
        },
        "media": {
            "thumbnail_url": _text(output.get("thumbnail_url")),
            "duration_seconds": _int_or_none(output.get("duration_seconds")),
            "music_id": _text(output.get("music_id")),
            "music_title": _text(output.get("music_title")),
            "music_author": _text(output.get("music_author")),
            "music_album": _text(output.get("music_album")),
            "music_audio_type": _text(
                output.get("music_audio_type") or output.get("media_audio_type")
            ),
            "music_metadata_attempted": output.get("music_metadata_attempted"),
            "music_metadata_status": _text(output.get("music_metadata_status")),
            "music_metadata_source": _text(output.get("music_metadata_source")),
            "music_metadata_authority": _text(output.get("music_metadata_authority")),
            "music_metadata_access_scope": _text(output.get("music_metadata_access_scope")),
            "content_location_id": _text(output.get("content_location_id")),
            "content_location_name": _text(output.get("content_location_name")),
            "content_location_latitude": output.get("content_location_latitude"),
            "content_location_longitude": output.get("content_location_longitude"),
            "content_language": _text(output.get("content_language")),
            "platform_keywords": list(output.get("keywords") or []) if isinstance(output.get("keywords"), list) else [],
            "category": _text(output.get("category")),
            "is_live_content": output.get("is_live_content"),
        },
        "social_music": social_music_evidence,
        "engagement_snapshot": engagement,
        "metric_availability": metric_availability,
        "source_provenance": source_provenance,
        "transport_provenance": transport,
        "collection_status": collection,
        "field_availability": field_availability,
        "analysis_readiness": analysis_readiness,
        "quality_flags": quality_flags,
    }

    output["raw_schema_version"] = RAW_SCHEMA_VERSION
    output["observed_at"] = observed_at
    output["source_provenance"] = source_provenance
    output["transport_provenance"] = transport
    output["collection_status"] = collection
    output["metric_availability"] = metric_availability
    output["field_availability"] = field_availability
    output["analysis_readiness"] = analysis_readiness
    output["quality_flags"] = quality_flags
    output["social_music_evidence"] = social_music_evidence
    output["brief_evidence"] = evidence
    return output


def finalize_payload(
    payload: dict[str, Any],
    *,
    platform: str,
    source_context: dict[str, Any] | None = None,
    collection_context: dict[str, Any] | None = None,
    run_context: dict[str, Any] | None = None,
    observed_at: str | None = None,
) -> dict[str, Any]:
    output = dict(payload)
    observed_at = _text(observed_at) or _now_iso()
    source_context = dict(source_context or source_context_from_env())
    collection_context = dict(collection_context or collection_context_from_env())
    run_context = dict(run_context or run_context_from_env())
    videos = [
        finalize_content_record(
            video,
            platform=platform,
            source_context=source_context,
            collection_context=collection_context,
            observed_at=observed_at,
        )
        for video in output.get("videos", [])
        if isinstance(video, dict)
    ]
    flags = [flag for video in videos for flag in video.get("quality_flags", [])]
    transport = [video.get("transport_provenance", {}) for video in videos]
    output.update({
        "raw_schema_version": RAW_SCHEMA_VERSION,
        "generated_at": observed_at,
        "platform": platform,
        "run_context": run_context,
        "source_context": source_context,
        "collection_context": collection_context,
        "quality_summary": {
            "records": len(videos),
            "records_with_unknown_publication_time": sum(
                "unknown_publication_time" in video.get("quality_flags", []) for video in videos
            ),
            "records_with_truncated_comments": sum(
                "comments_truncated_by_limit" in video.get("quality_flags", []) for video in videos
            ),
            "records_using_dom": sum(bool(item.get("dom_used")) for item in transport),
            "records_using_api_data": sum(bool(item.get("api_data_used")) for item in transport),
            "quality_flag_counts": {
                flag: flags.count(flag)
                for flag in sorted(set(flags))
            },
        },
        "videos": videos,
    })
    return output
