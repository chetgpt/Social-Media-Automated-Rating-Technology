"""Reusable campaign specification and deterministic discovery planning."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SUPPORTED_PLATFORMS = ("youtube", "tiktok", "instagram", "facebook", "x")
PLATFORM_ALIASES = {"twitter": "x"}
KEYWORD_GROUPS = (
    "core",
    "hashtags",
    "entities",
    "optional",
    "campaign",
    "exclusions",
)
PUBLICATION_VALUE_TYPES = (
    "answer",
    "fact_correction",
    "verified_context",
    "useful_gap",
    "clarifying_question",
)
DEFAULT_SOCIAL_REVIEW_DIMENSIONS = (
    {
        "key": "integrity_and_evidence",
        "label": "Integrity and evidence",
        "description": (
            "Factual support for informational content, or authenticity and internal "
            "coherence for opinion, entertainment, and creative content."
        ),
        "weight": 20.0,
    },
    {
        "key": "substance_and_audience_value",
        "label": "Substance and audience value",
        "description": (
            "The post's practical, intellectual, emotional, or entertainment value "
            "for its intended audience."
        ),
        "weight": 20.0,
    },
    {
        "key": "clarity_and_craft",
        "label": "Clarity and craft",
        "description": "How clearly, coherently, and effectively the available native text communicates.",
        "weight": 15.0,
    },
    {
        "key": "context_and_completeness",
        "label": "Context and completeness",
        "description": "Whether important qualifications, conditions, and context are present.",
        "weight": 15.0,
    },
    {
        "key": "originality_and_perspective",
        "label": "Originality and perspective",
        "description": "Whether the post offers a specific, distinctive, or meaningfully framed perspective.",
        "weight": 10.0,
    },
    {
        "key": "audience_fit",
        "label": "Audience fit",
        "description": "How relevant, accessible, and appropriate the post is for its likely audience.",
        "weight": 10.0,
    },
    {
        "key": "responsibility_and_safety",
        "label": "Responsibility and safety",
        "description": "Whether the post avoids unsupported harmful guidance and misleading framing.",
        "weight": 10.0,
    },
)
DEFAULT_CONVERSATION_VALUE_DIMENSIONS = (
    {
        "key": "relevant_contribution",
        "label": "Relevant contribution",
        "description": "How much of the observed discussion remains relevant to the post.",
        "weight": 25.0,
    },
    {
        "key": "specific_useful_information",
        "label": "Specific useful information",
        "description": "Whether comments add concrete details, experience, corrections, or examples.",
        "weight": 25.0,
    },
    {
        "key": "meaningful_perspective_diversity",
        "label": "Meaningful perspective diversity",
        "description": "Whether the discussion adds distinct, relevant viewpoints rather than repetition.",
        "weight": 15.0,
    },
    {
        "key": "question_resolution",
        "label": "Question resolution",
        "description": "How well useful audience questions are answered or clearly left unresolved.",
        "weight": 15.0,
    },
    {
        "key": "creator_responsiveness",
        "label": "Creator responsiveness",
        "description": "Whether creator replies clarify, correct, or extend the original post.",
        "weight": 10.0,
    },
    {
        "key": "civility_and_responsibility",
        "label": "Civility and responsibility",
        "description": "Whether the useful discussion remains constructive and responsibly framed.",
        "weight": 10.0,
    },
)
DEFAULT_PUBLIC_RATING_BANDS = (
    {"minimum": 9.0, "key": "exceptional"},
    {"minimum": 8.0, "key": "highly_recommended"},
    {"minimum": 7.0, "key": "recommended"},
    {"minimum": 6.0, "key": "promising_internal_only"},
    {"minimum": 5.0, "key": "mixed_internal_only"},
    {"minimum": 0.0, "key": "weak_internal_only"},
)


class CampaignSpecError(ValueError):
    pass


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _platform_name(value: Any) -> str:
    name = _text(value).lower()
    return PLATFORM_ALIASES.get(name, name)


def _search_text(value: Any) -> str:
    """Normalize human-written search phrases before sending them to platforms."""
    text = _text(value)
    # Platform search boxes/APIs often treat quote marks literally. Exactness is
    # handled later by relevance scoring, so do not preserve quote operators.
    text = text.replace('"', " ").replace("“", " ").replace("”", " ")
    text = text.replace("'", " ").replace("‘", " ").replace("’", " ")
    return re.sub(r"\s+", " ", text).strip()


def _unique_strings(values: Any, field: str) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list):
        raise CampaignSpecError(f"{field} must be a list")
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _text(value)
        if not text:
            continue
        key = text.casefold()
        if key not in seen:
            seen.add(key)
            output.append(text)
    return output


def _validate_iso(value: str, field: str) -> str:
    if not value:
        return ""
    try:
        dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CampaignSpecError(f"{field} must be ISO-8601: {value}") from exc
    return value


def _normalize_scoring_dimensions(raw_dimensions: Any, field: str) -> list[dict[str, Any]]:
    if raw_dimensions is None:
        raw_dimensions = []
    if not isinstance(raw_dimensions, list):
        raise CampaignSpecError(f"{field} must be a list")
    dimensions = []
    seen_dimensions: set[str] = set()
    for index, dimension in enumerate(raw_dimensions):
        if not isinstance(dimension, dict):
            raise CampaignSpecError(f"{field}[{index}] must be an object")
        key = _text(dimension.get("key") or dimension.get("name")).lower()
        key = re.sub(r"[^a-z0-9]+", "_", key).strip("_")
        if not key:
            raise CampaignSpecError(f"{field}[{index}] needs a key or name")
        if key in seen_dimensions:
            raise CampaignSpecError(f"duplicate {field} dimension: {key}")
        seen_dimensions.add(key)
        dimensions.append({
            "key": key,
            "label": _text(dimension.get("label") or dimension.get("name") or key),
            "description": _text(dimension.get("description")),
            "weight": max(0.0, float(dimension.get("weight") or 0.0)),
        })
    return dimensions


def _normalize_accounts(raw: Any) -> dict[str, list[dict[str, str]]]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise CampaignSpecError("accounts must be an object keyed by platform")

    accounts: dict[str, list[dict[str, str]]] = {platform: [] for platform in SUPPORTED_PLATFORMS}
    seen_by_platform: dict[str, set[str]] = {platform: set() for platform in SUPPORTED_PLATFORMS}
    for platform, values in raw.items():
        platform_key = _platform_name(platform)
        if platform_key not in SUPPORTED_PLATFORMS:
            raise CampaignSpecError(f"unsupported account platform: {platform}")
        if not isinstance(values, list):
            raise CampaignSpecError(f"accounts.{platform_key} must be a list")

        seen = seen_by_platform[platform_key]
        for index, value in enumerate(values):
            if isinstance(value, str):
                item = {"value": value}
            elif isinstance(value, dict):
                item = value
            else:
                raise CampaignSpecError(f"accounts.{platform_key}[{index}] must be a string or object")

            target = _text(item.get("url") or item.get("handle") or item.get("value") or item.get("name"))
            if not target:
                raise CampaignSpecError(f"accounts.{platform_key}[{index}] has no url, handle, value, or name")
            key = target.casefold().rstrip("/")
            if key in seen:
                continue
            taxonomy = item.get("taxonomy") or {}
            if not isinstance(taxonomy, dict):
                raise CampaignSpecError(f"accounts.{platform_key}[{index}].taxonomy must be an object")
            seen.add(key)
            accounts[platform_key].append({
                "value": target,
                "label": _text(item.get("label") or item.get("name") or item.get("handle") or target),
                "role": _text(item.get("role") or "account"),
                "taxonomy": dict(taxonomy),
            })
    return accounts


def normalize_analysis_config(raw: Any) -> dict[str, Any]:
    """Normalize the native-text analysis and controlled publication policy."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise CampaignSpecError("analysis must be an object")

    enabled = bool(raw.get("enabled", False))
    mode = _text(raw.get("mode") or "shadow").lower()
    if mode not in {"off", "shadow", "review", "publish"}:
        raise CampaignSpecError("analysis.mode must be off, shadow, review, or publish")
    provider = _text(raw.get("provider") or "manual").lower()
    if provider not in {"none", "manual", "codex", "gemini"}:
        raise CampaignSpecError("analysis.provider must be none, manual, codex, or gemini")

    raw_fact_check = raw.get("fact_check") or {}
    if not isinstance(raw_fact_check, dict):
        raise CampaignSpecError("analysis.fact_check must be an object")
    raw_scoring = raw.get("scoring") or {}
    if not isinstance(raw_scoring, dict):
        raise CampaignSpecError("analysis.scoring must be an object")
    dimensions_configured = "dimensions" in raw_scoring
    formula_version = _text(
        raw_scoring.get("formula_version")
        or ("unconfigured-v1" if dimensions_configured and not raw_scoring.get("dimensions") else "social-review-v1")
    )
    raw_dimensions = (
        raw_scoring.get("dimensions")
        if dimensions_configured
        else list(DEFAULT_SOCIAL_REVIEW_DIMENSIONS)
    )
    dimensions = _normalize_scoring_dimensions(
        raw_dimensions,
        "analysis.scoring.dimensions",
    )
    conversation_dimensions_configured = "conversation_dimensions" in raw_scoring
    conversation_enabled = bool(
        raw_scoring.get("conversation_enabled", formula_version == "social-review-v1")
    )
    raw_conversation_dimensions = (
        raw_scoring.get("conversation_dimensions")
        if conversation_dimensions_configured
        else list(DEFAULT_CONVERSATION_VALUE_DIMENSIONS) if conversation_enabled else []
    )
    conversation_dimensions = _normalize_scoring_dimensions(
        raw_conversation_dimensions,
        "analysis.scoring.conversation_dimensions",
    )
    genre_mode = _text(raw_scoring.get("genre_mode") or "auto").lower()
    if genre_mode not in {"auto", "fixed"}:
        raise CampaignSpecError("analysis.scoring.genre_mode must be auto or fixed")

    raw_publication = raw.get("publication") or {}
    if not isinstance(raw_publication, dict):
        raise CampaignSpecError("analysis.publication must be an object")
    raw_language = raw_publication.get("language") or {}
    if not isinstance(raw_language, dict):
        raise CampaignSpecError("analysis.publication.language must be an object")
    language_mode = _text(raw_language.get("mode") or "auto").lower()
    if language_mode not in {"auto", "neutral-id", "creator-style", "fixed", "bilingual"}:
        raise CampaignSpecError(
            "analysis.publication.language.mode must be auto, neutral-id, creator-style, fixed, or bilingual"
        )
    raw_public_rating = raw_publication.get("public_rating") or {}
    if not isinstance(raw_public_rating, dict):
        raise CampaignSpecError("analysis.publication.public_rating must be an object")
    allow_public_scores = bool(raw_publication.get("allow_public_scores", True))
    public_rating_enabled = allow_public_scores and bool(
        raw_public_rating.get("enabled", True)
    )
    public_rating_scale = max(
        1.0,
        min(100.0, float(raw_public_rating.get("scale") or 10.0)),
    )
    public_rating_increment = max(
        0.1,
        min(public_rating_scale, float(raw_public_rating.get("increment") or 0.1)),
    )
    raw_rating_bands = raw_public_rating.get("bands") or list(DEFAULT_PUBLIC_RATING_BANDS)
    if not isinstance(raw_rating_bands, list):
        raise CampaignSpecError("analysis.publication.public_rating.bands must be a list")
    public_rating_bands = []
    for index, band in enumerate(raw_rating_bands):
        if not isinstance(band, dict):
            raise CampaignSpecError(
                f"analysis.publication.public_rating.bands[{index}] must be an object"
            )
        key = _text(band.get("key") or band.get("label")).lower()
        if not key:
            raise CampaignSpecError(
                f"analysis.publication.public_rating.bands[{index}] needs a key"
            )
        public_rating_bands.append({
            "minimum": max(
                0.0,
                min(public_rating_scale, float(band.get("minimum") or 0.0)),
            ),
            "key": key,
        })
    public_rating_bands.sort(key=lambda item: item["minimum"], reverse=True)
    publication_mode = _text(
        raw_publication.get("mode") or ("shadow" if enabled else "disabled")
    ).lower()
    if publication_mode not in {"disabled", "shadow", "capture", "live"}:
        raise CampaignSpecError(
            "analysis.publication.mode must be disabled, shadow, capture, or live"
        )
    allowed_platforms = _unique_strings(
        raw_publication.get("allowed_platforms") or list(SUPPORTED_PLATFORMS),
        "analysis.publication.allowed_platforms",
    )
    allowed_platforms = list(dict.fromkeys(_platform_name(value) for value in allowed_platforms))
    unsupported = sorted(set(allowed_platforms) - set(SUPPORTED_PLATFORMS))
    if unsupported:
        raise CampaignSpecError(
            f"unsupported analysis publication platforms: {', '.join(unsupported)}"
        )
    allowed_value_types = [
        _text(value).lower()
        for value in _unique_strings(
            raw_publication.get("allowed_value_types") or list(PUBLICATION_VALUE_TYPES),
            "analysis.publication.allowed_value_types",
        )
    ]
    unsupported_value_types = sorted(
        set(allowed_value_types) - set(PUBLICATION_VALUE_TYPES)
    )
    if unsupported_value_types:
        raise CampaignSpecError(
            "unsupported analysis publication value types: "
            + ", ".join(unsupported_value_types)
        )
    min_public_comment_characters = max(
        1, int(raw_publication.get("min_public_comment_characters") or 20)
    )
    max_public_comment_characters = max(
        min_public_comment_characters,
        int(raw_publication.get("max_public_comment_characters") or 1000),
    )

    return {
        **raw,
        "enabled": enabled and mode != "off",
        "mode": mode,
        "provider": provider,
        "model": _text(raw.get("model")),
        "analysis_version": _text(raw.get("analysis_version") or "native-text-v4"),
        "max_posts_per_run": max(0, int(raw.get("max_posts_per_run") or 20)),
        "max_comments_per_post": max(0, int(raw.get("max_comments_per_post") or 200)),
        "max_attempts": max(1, int(raw.get("max_attempts") or 3)),
        "timeout_seconds": max(30, int(raw.get("timeout_seconds") or 120)),
        "fact_check": {
            "enabled": bool(raw_fact_check.get("enabled", True)),
            "require_citations": bool(raw_fact_check.get("require_citations", True)),
            "google_search_grounding": bool(
                raw_fact_check.get("google_search_grounding", True)
            ),
            "max_claims": max(1, int(raw_fact_check.get("max_claims") or 10)),
            "max_sources_per_claim": max(
                1, int(raw_fact_check.get("max_sources_per_claim") or 3)
            ),
        },
        "scoring": {
            "formula_version": formula_version,
            "dimensions": dimensions,
            "conversation_enabled": conversation_enabled and bool(conversation_dimensions),
            "conversation_dimensions": conversation_dimensions,
            "post_weight": max(
                0.0, min(100.0, float(raw_scoring.get("post_weight") or 80.0))
            ),
            "conversation_weight": max(
                0.0, min(100.0, float(raw_scoring.get("conversation_weight") or 20.0))
            ),
            "max_conversation_adjustment": max(
                0.0,
                min(100.0, float(raw_scoring.get("max_conversation_adjustment") or 10.0)),
            ),
            "require_core_content_for_publication": bool(
                raw_scoring.get("require_core_content_for_publication", True)
            ),
            "video_requires_transcript_or_subtitle": bool(
                raw_scoring.get("video_requires_transcript_or_subtitle", True)
            ),
            "genre_mode": genre_mode,
            "genre": _text(raw_scoring.get("genre")),
        },
        "publication": {
            "mode": publication_mode,
            "require_approval": bool(raw_publication.get("require_approval", True)),
            "positive_only": bool(raw_publication.get("positive_only", True)),
            "min_public_score": max(
                0.0,
                min(100.0, float(raw_publication.get("min_public_score", 70.0))),
            ),
            "recommendation_only": bool(
                raw_publication.get("recommendation_only", True)
            ),
            "require_strength_and_recommendation": bool(
                raw_publication.get("require_strength_and_recommendation", True)
            ),
            "require_score": bool(
                raw_publication.get(
                    "require_score",
                    raw_publication.get("positive_only", True),
                )
            ),
            "require_publication_decision": bool(
                raw_publication.get("require_publication_decision", True)
            ),
            "require_novel_value": bool(
                raw_publication.get("require_novel_value", True)
            ),
            "require_evidence_refs": bool(
                raw_publication.get("require_evidence_refs", True)
            ),
            "require_fresh_evidence": bool(
                raw_publication.get("require_fresh_evidence", True)
            ),
            "min_confidence": max(
                0.0, min(100.0, float(raw_publication.get("min_confidence") or 75.0))
            ),
            "min_data_completeness": max(
                0.0,
                min(100.0, float(raw_publication.get("min_data_completeness") or 70.0)),
            ),
            "min_public_comment_characters": min_public_comment_characters,
            "max_public_comment_characters": max_public_comment_characters,
            "max_evidence_age_minutes": max(
                1, int(raw_publication.get("max_evidence_age_minutes") or 60)
            ),
            "max_draft_age_minutes": max(
                1, int(raw_publication.get("max_draft_age_minutes") or 60)
            ),
            "daily_limit": max(0, int(raw_publication.get("daily_limit") or 5)),
            "allowed_platforms": allowed_platforms,
            "allowed_value_types": allowed_value_types,
            "disclose_automation": bool(
                raw_publication.get("disclose_automation", True)
            ),
            "allow_public_scores": allow_public_scores,
            "public_rating": {
                "enabled": public_rating_enabled,
                "required": public_rating_enabled and bool(
                    raw_public_rating.get("required", True)
                ),
                "scale": public_rating_scale,
                "increment": public_rating_increment,
                "placeholder": _text(
                    raw_public_rating.get("placeholder") or "{PUBLIC_RATING}"
                ),
                "bands": public_rating_bands,
            },
            "language": {
                "mode": language_mode,
                "default": _text(raw_language.get("default") or "id"),
                "fixed": _text(raw_language.get("fixed")),
                "secondary": _text(raw_language.get("secondary")),
                "match_creator_tone": bool(raw_language.get("match_creator_tone", True)),
                "allow_code_switching": bool(raw_language.get("allow_code_switching", True)),
                "neutral_on_low_confidence": bool(
                    raw_language.get("neutral_on_low_confidence", True)
                ),
                "preserve_ai_disclosure": True,
            },
            "allow_internal_diagnostics": bool(
                raw_publication.get("allow_internal_diagnostics", False)
            ),
        },
    }


def normalize_campaign_spec(payload: dict[str, Any], source_path: str = "") -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise CampaignSpecError("campaign specification must be a JSON object")

    schema_version = int(payload.get("schema_version") or 1)
    if schema_version != 1:
        raise CampaignSpecError(f"unsupported campaign schema_version: {schema_version}")

    name = _text(payload.get("name") or payload.get("project"))
    if not name:
        raise CampaignSpecError("campaign name is required")
    project = _text(payload.get("project") or name)
    timezone_name = _text(payload.get("timezone") or "Asia/Jakarta")
    try:
        campaign_timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise CampaignSpecError(f"unknown campaign timezone: {timezone_name}") from exc

    platforms = _unique_strings(payload.get("platforms") or list(SUPPORTED_PLATFORMS), "platforms")
    platforms = list(dict.fromkeys(_platform_name(platform) for platform in platforms))
    unsupported = sorted(set(platforms) - set(SUPPORTED_PLATFORMS))
    if unsupported:
        raise CampaignSpecError(f"unsupported platforms: {', '.join(unsupported)}")
    if not platforms:
        raise CampaignSpecError("at least one platform is required")

    raw_keywords = payload.get("keywords") or {}
    if not isinstance(raw_keywords, dict):
        raise CampaignSpecError("keywords must be an object")
    keywords = {
        group: _unique_strings(raw_keywords.get(group), f"keywords.{group}")
        for group in KEYWORD_GROUPS
    }
    if not any(keywords[group] for group in ("core", "hashtags", "entities", "campaign")):
        raise CampaignSpecError("at least one core, hashtag, entity, or campaign keyword is required")

    raw_queries = payload.get("queries") or {}
    if isinstance(raw_queries, list):
        raw_queries = {"all": raw_queries}
    if not isinstance(raw_queries, dict):
        raise CampaignSpecError("queries must be a list or an object keyed by platform")
    queries: dict[str, list[str]] = {
        "all": _unique_strings(raw_queries.get("all"), "queries.all")
    }
    for platform in SUPPORTED_PLATFORMS:
        values = _unique_strings(raw_queries.get(platform), f"queries.{platform}")
        if platform == "x":
            values.extend(_unique_strings(raw_queries.get("twitter"), "queries.twitter"))
        queries[platform] = list(dict.fromkeys(values))

    raw_windows = payload.get("date_windows") or []
    if not isinstance(raw_windows, list):
        raise CampaignSpecError("date_windows must be a list")
    date_windows = []
    for index, window in enumerate(raw_windows):
        if not isinstance(window, dict):
            raise CampaignSpecError(f"date_windows[{index}] must be an object")
        start = _validate_iso(_text(window.get("start")), f"date_windows[{index}].start")
        end = _validate_iso(_text(window.get("end")), f"date_windows[{index}].end")
        if start and end:
            start_time = dt.datetime.fromisoformat(start.replace("Z", "+00:00"))
            end_time = dt.datetime.fromisoformat(end.replace("Z", "+00:00"))
            if start_time.tzinfo is None:
                start_time = start_time.replace(tzinfo=campaign_timezone)
            if end_time.tzinfo is None:
                end_time = end_time.replace(tzinfo=campaign_timezone)
            if start_time.astimezone(dt.timezone.utc) > end_time.astimezone(dt.timezone.utc):
                raise CampaignSpecError(f"date_windows[{index}] starts after it ends")
        date_windows.append({
            "name": _text(window.get("name") or f"window_{index + 1}"),
            "start": start,
            "end": end,
        })

    discovery = dict(payload.get("discovery") or {})
    collection = dict(payload.get("collection") or {})
    relevance = dict(payload.get("relevance") or {})
    raw_profiles = payload.get("profiles") or {}
    if not isinstance(raw_profiles, dict):
        raise CampaignSpecError("profiles must be an object")
    profiles = dict(raw_profiles)
    raw_storage = payload.get("storage") or {}
    if not isinstance(raw_storage, dict):
        raise CampaignSpecError("storage must be an object")
    storage = dict(raw_storage)
    transport_mode = _text(collection.get("transport_mode") or "api-first").lower()
    if transport_mode not in {"api-first", "api-only", "hybrid", "dom"}:
        raise CampaignSpecError("collection.transport_mode must be api-first, api-only, hybrid, or dom")
    relevance_mode = _text(relevance.get("mode") or "filter").lower()
    if relevance_mode not in {"off", "audit", "filter"}:
        raise CampaignSpecError("relevance.mode must be off, audit, or filter")
    review_action = _text(relevance.get("review_action") or "skip").lower()
    if review_action not in {"skip", "scrape"}:
        raise CampaignSpecError("relevance.review_action must be skip or scrape")
    accept_threshold = max(1, int(relevance.get("accept_threshold") or 60))
    review_threshold = max(1, int(relevance.get("review_threshold") or 30))
    if review_threshold > accept_threshold:
        raise CampaignSpecError("relevance.review_threshold cannot exceed accept_threshold")
    storage_provider = _text(storage.get("provider") or "none").lower().replace("_", "-")
    if storage_provider not in {"none", "google-drive"}:
        raise CampaignSpecError("storage.provider must be none or google-drive")
    raw_run_archive_timing = _text(storage.get("raw_run_archive_timing") or "immediate").lower().replace("_", "-")
    if raw_run_archive_timing not in {"immediate", "final"}:
        raise CampaignSpecError("storage.raw_run_archive_timing must be immediate or final")
    normalized = {
        "schema_version": schema_version,
        "name": name,
        "project": project,
        "title": _text(payload.get("title") or name),
        "timezone": timezone_name,
        "platforms": platforms,
        "keywords": keywords,
        "queries": queries,
        "accounts": _normalize_accounts(payload.get("accounts")),
        "date_windows": date_windows,
        "discovery": {
            "include_core": bool(discovery.get("include_core", True)),
            "include_hashtags": bool(discovery.get("include_hashtags", True)),
            "include_campaign": bool(discovery.get("include_campaign", True)),
            "pair_entities_with_core": bool(discovery.get("pair_entities_with_core", True)),
            "pair_optional_with_core": bool(discovery.get("pair_optional_with_core", True)),
            "anchor_limit": max(1, int(discovery.get("anchor_limit") or 2)),
            "max_sources_per_platform": max(0, int(discovery.get("max_sources_per_platform") or 0)),
        },
        "collection": {
            "videos_per_source": max(0, int(collection.get("videos_per_source") or 0)),
            "discovery_candidates_per_source": max(
                0,
                int(collection.get("discovery_candidates_per_source") or 0),
            ),
            "comments_per_post": max(0, int(collection.get("comments_per_post") or 0)),
            "refresh_limit_per_platform": max(0, int(collection.get("refresh_limit_per_platform") or 0)),
            "refresh_after_hours": max(0.0, float(collection.get("refresh_after_hours") or 6.0)),
            "report_hours": [
                int(value)
                for value in collection.get("report_hours", [24, 72])
                if str(value).isdigit() and int(value) > 0
            ],
            "transport_mode": transport_mode,
        },
        "relevance": {
            "mode": relevance_mode,
            "review_action": review_action,
            "accept_threshold": accept_threshold,
            "review_threshold": review_threshold,
            "anchor_terms": _unique_strings(relevance.get("anchor_terms"), "relevance.anchor_terms"),
            "ambiguous_terms": _unique_strings(relevance.get("ambiguous_terms"), "relevance.ambiguous_terms"),
            "context_terms": _unique_strings(relevance.get("context_terms"), "relevance.context_terms"),
            "require_anchor": bool(relevance.get("require_anchor", True)),
        },
        "profiles": {
            "enabled": bool(profiles.get("enabled", False)),
            "limit_per_run": max(0, int(profiles.get("limit_per_run") or 100)),
            "cache_days": max(1, int(profiles.get("cache_days") or 14)),
            "browser_fallback": bool(profiles.get("browser_fallback", True)),
        },
        "storage": {
            "provider": storage_provider,
            "root_folder_name": _text(
                storage.get("root_folder_name") or "Social Listening Projects"
            ),
            "root_folder_id": _text(storage.get("root_folder_id")),
            "account_hint": _text(storage.get("account_hint")),
            "mount_path": _text(storage.get("mount_path")),
            "credentials_file": _text(storage.get("credentials_file")),
            "token_file": _text(storage.get("token_file")),
            "archive_raw_runs": bool(storage.get("archive_raw_runs", True)),
            "archive_backlog": bool(storage.get("archive_backlog", True)),
            "raw_run_archive_timing": raw_run_archive_timing,
            "upload_compiled": bool(storage.get("upload_compiled", True)),
            "upload_logs": bool(storage.get("upload_logs", True)),
            "snapshot_database": bool(storage.get("snapshot_database", True)),
            "delete_local_after_upload": bool(
                storage.get("delete_local_after_upload", False)
            ),
            "required": bool(storage.get("required", True)),
            "chunk_size_mb": max(1, min(256, int(storage.get("chunk_size_mb") or 16))),
            "max_retries": max(1, int(storage.get("max_retries") or 7)),
        },
        "analysis": normalize_analysis_config(payload.get("analysis")),
        "reporting": dict(payload.get("reporting") or {}),
        "source_path": source_path,
    }
    return normalized


def load_campaign_spec(path: str | Path) -> dict[str, Any]:
    file_path = Path(path)
    with file_path.open("r", encoding="utf-8-sig") as handle:
        payload = json.load(handle)
    return normalize_campaign_spec(payload, source_path=str(file_path.resolve()))


def campaign_keywords(spec: dict[str, Any]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for group in ("core", "hashtags", "entities", "campaign", "optional"):
        for value in spec["keywords"].get(group, []):
            key = value.casefold()
            if key not in seen:
                seen.add(key)
                output.append(value)
    return output


def _source_key(platform: str, kind: str, value: str) -> str:
    digest = hashlib.sha1(f"{platform}\x1f{kind}\x1f{value.casefold()}".encode("utf-8")).hexdigest()[:16]
    return f"{kind}:{digest}"


def source_slug(source: dict[str, Any]) -> str:
    value = re.sub(r"[^A-Za-z0-9]+", "_", _text(source.get("value"))).strip("_").lower()
    return (value[:24] or source.get("kind") or "source")


def _query_sources(spec: dict[str, Any], platform: str) -> list[dict[str, Any]]:
    discovery = spec["discovery"]
    keywords = spec["keywords"]
    anchors = keywords["core"][:discovery["anchor_limit"]]
    raw: list[tuple[str, str, str]] = []

    for value in [*spec["queries"]["all"], *spec["queries"][platform]]:
        raw.append(("query", value, "explicit"))
    if discovery["include_core"]:
        raw.extend(("query", value, "core") for value in keywords["core"])
    if discovery["include_hashtags"]:
        raw.extend(("hashtag", value if value.startswith("#") else f"#{value}", "hashtag") for value in keywords["hashtags"])
    if discovery["include_campaign"]:
        raw.extend(("campaign_query", value, "campaign") for value in keywords["campaign"])

    for value in keywords["entities"]:
        if discovery["pair_entities_with_core"] and anchors:
            raw.extend(("entity_query", f"{value} {anchor}", "entity") for anchor in anchors)
        else:
            raw.append(("entity_query", value, "entity"))
    for value in keywords["optional"]:
        if discovery["pair_optional_with_core"] and anchors:
            raw.extend(("optional_query", f"{value} {anchor}", "optional") for anchor in anchors)
        else:
            raw.append(("optional_query", value, "optional"))

    sources: list[dict[str, Any]] = []
    seen: set[str] = set()
    for kind, value, group in raw:
        value = _search_text(value)
        key = value.casefold()
        if not value or key in seen:
            continue
        seen.add(key)
        sources.append({
            "platform": platform,
            "kind": kind,
            "value": value,
            "label": value,
            "role": group,
            "target_mode": "search",
            "source_key": _source_key(platform, kind, value),
        })
    return sources


def expand_campaign_sources(
    spec: dict[str, Any],
    selected_platforms: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    selected = set(selected_platforms or spec["platforms"])
    sources: list[dict[str, Any]] = []
    for platform in spec["platforms"]:
        if platform not in selected:
            continue
        platform_sources = _query_sources(spec, platform)
        for account in spec["accounts"].get(platform, []):
            raw_value = account["value"]
            value = raw_value
            target_mode = "url" if "://" in raw_value else "search"
            handle = raw_value.lstrip("@").strip("/")
            if target_mode == "search" and re.fullmatch(r"[A-Za-z0-9._-]+", handle):
                if platform == "youtube":
                    value = f"https://www.youtube.com/@{handle}"
                elif platform == "tiktok":
                    value = f"https://www.tiktok.com/@{handle}"
                elif platform == "instagram":
                    value = f"https://www.instagram.com/{handle}/"
                elif platform == "facebook":
                    value = f"https://www.facebook.com/{handle}/"
                elif platform == "x":
                    value = f"https://x.com/{handle}"
                target_mode = "url"
            platform_sources.append({
                "platform": platform,
                "kind": "account",
                "value": value,
                "label": account["label"],
                "role": account["role"],
                "taxonomy": account.get("taxonomy") or {},
                "target_mode": target_mode,
                "source_key": _source_key(platform, "account", value),
            })

        limit = spec["discovery"]["max_sources_per_platform"]
        if limit:
            platform_sources = platform_sources[:limit]
        sources.extend(platform_sources)
    return sources


def campaign_spec_digest(spec: dict[str, Any]) -> str:
    payload = {key: value for key, value in spec.items() if key != "source_path"}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
