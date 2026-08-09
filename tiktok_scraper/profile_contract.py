"""Normalized public-profile evidence shared by every platform.

Profile records deliberately separate public/self-declared profile attributes
from first-party audience analytics. A profile location is not evidence of an
audience location, and pronouns or age statements are never converted into
guessed demographic classifications.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from typing import Any, Iterable
from urllib.parse import urlparse


PROFILE_SCHEMA_VERSION = "1.2"
SUPPORTED_PLATFORMS = {"tiktok", "youtube", "instagram", "facebook", "x"}
NON_DEMOGRAPHIC_GENDER_TOKENS = {
    "NEUTER",
    "UNKNOWN",
    "UNSPECIFIED",
    "NOT_A_PERSON",
}

_PROFILE_CONTAINER_KEYS = (
    "profile",
    "creator_profile",
    "author_profile",
    "user",
    "owner",
    "from",
    "author",
    "x_api_author",
    "x_graphql_author",
    "userInfo",
    "user_info",
    "legacy",
    "core",
    "public_metrics",
    "stats",
    "statistics",
    "authorStats",
    "author_stats",
)


def text_value(value: Any) -> str:
    return "" if value is None else str(value).strip()


def optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return max(0, int(value))
    text = text_value(value).lower().replace(",", "")
    match = re.fullmatch(r"(-?\d+(?:\.\d+)?)\s*([kmb]?)", text)
    if not match:
        return None
    multiplier = {"": 1, "k": 1_000, "m": 1_000_000, "b": 1_000_000_000}[match.group(2)]
    return max(0, int(float(match.group(1)) * multiplier))


def optional_bool(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    lowered = text_value(value).casefold()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    return None


def normalize_platform(value: Any) -> str:
    platform = text_value(value).casefold()
    if platform == "twitter":
        return "x"
    return platform


def is_non_demographic_gender_value(platform: Any, value: Any) -> bool:
    return (
        normalize_platform(platform) == "facebook"
        and text_value(value).upper() in NON_DEMOGRAPHIC_GENDER_TOKENS
    )


def normalize_username(platform: str, value: Any) -> str:
    username = text_value(value)
    if not username:
        return ""
    if username.startswith(("http://", "https://")):
        parsed = urlparse(username)
        parts = [part for part in parsed.path.split("/") if part]
        if not parts:
            return ""
        if platform == "youtube" and parts[0].casefold() in {"channel", "user", "c"}:
            return parts[1] if len(parts) > 1 and parts[0].casefold() != "channel" else ""
        username = parts[0]
    username = username.lstrip("@").strip().rstrip("/")
    if re.search(r"\s", username):
        return ""
    if username.casefold() in {
        "unknown",
        "n/a",
        "none",
        "facebook user",
        "instagram user",
        "youtube channel",
        "tiktok user",
        "x user",
    }:
        return ""
    return username


def normalize_user_id(platform: str, value: Any) -> str:
    user_id = text_value(value)
    if not user_id:
        return ""
    if user_id.startswith(("http://", "https://")):
        parsed = urlparse(user_id)
        parts = [part for part in parsed.path.split("/") if part]
        if platform == "youtube" and len(parts) >= 2 and parts[0].casefold() == "channel":
            return parts[1]
        return ""
    return user_id


def profile_key(platform: str, user_id: Any = "", username: Any = "") -> str:
    platform = normalize_platform(platform)
    normalized_id = normalize_user_id(platform, user_id)
    normalized_username = normalize_username(platform, username)
    if normalized_id:
        return f"id:{normalized_id}"
    if normalized_username:
        return f"username:{normalized_username.casefold()}"
    return ""


def canonical_profile_url(platform: str, username: Any = "", user_id: Any = "") -> str:
    platform = normalize_platform(platform)
    username = normalize_username(platform, username)
    user_id = normalize_user_id(platform, user_id)
    if platform == "tiktok" and username:
        return f"https://www.tiktok.com/@{username}"
    if platform == "youtube":
        if user_id.startswith("UC"):
            return f"https://www.youtube.com/channel/{user_id}/about"
        if username:
            return f"https://www.youtube.com/@{username}/about"
    if platform == "instagram" and username:
        return f"https://www.instagram.com/{username}/"
    if platform == "facebook" and (username or user_id):
        return f"https://www.facebook.com/{username or user_id}/about"
    if platform == "x" and username:
        return f"https://x.com/{username}"
    return ""


def _containers(record: dict[str, Any]) -> Iterable[dict[str, Any]]:
    seen: set[int] = set()
    queue: list[dict[str, Any]] = [record]
    while queue:
        current = queue.pop(0)
        marker = id(current)
        if marker in seen:
            continue
        seen.add(marker)
        yield current
        for key in _PROFILE_CONTAINER_KEYS:
            value = current.get(key)
            if isinstance(value, dict):
                queue.append(value)


def _first(containers: Iterable[dict[str, Any]], *keys: str) -> Any:
    for container in containers:
        for key in keys:
            if key in container and container.get(key) not in (None, ""):
                return container.get(key)
    return None


def _location_parts(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        return {"raw": value.strip()}
    if not isinstance(value, dict):
        return {}
    location_value: dict[str, Any] = dict(value)
    address: dict[str, Any] = (
        dict(location_value["address"])
        if isinstance(location_value.get("address"), dict)
        else {}
    )
    containers: list[dict[str, Any]] = [location_value, address]
    raw = _first(
        containers,
        "raw",
        "name",
        "full_name",
        "formatted_address",
        "full_address",
        "single_line_address",
        "street",
    )
    city = _first(containers, "city", "city_name", "locality", "town")
    region = _first(
        containers,
        "region",
        "region_name",
        "state",
        "administrative_area_level_1",
    )
    country = _first(containers, "country", "country_name")
    country_code = _first(containers, "country_code", "countryCode")
    latitude = _first(containers, "latitude", "lat")
    longitude = _first(containers, "longitude", "lng", "lon")
    if not raw:
        raw = ", ".join(
            part for part in (text_value(city), text_value(region), text_value(country)) if part
        )
    return {
        "raw": text_value(raw),
        "city": text_value(city),
        "region": text_value(region),
        "country": text_value(country),
        "country_code": text_value(country_code).upper(),
        "latitude": latitude,
        "longitude": longitude,
    }


def _geography_semantics(basis: Any) -> tuple[str, bool]:
    normalized_basis = text_value(basis)
    classification = {
        "platform_account_region": "platform_reported_account_region_not_audience",
        "channel_country": "channel_configured_country_not_audience",
        "public_business_address": "public_business_address_not_audience",
        "self_declared_profile_location": "self_declared_profile_location_not_audience",
        "platform_structured_location": "public_profile_structured_location_not_audience",
    }.get(normalized_basis, "public_profile_geography_not_audience")
    return (
        classification,
        normalized_basis
        in {
            "self_declared_profile_location",
            "public_business_address",
        },
    )


def _has_location(value: dict[str, Any]) -> bool:
    return any(
        value.get(field)
        for field in (
            "raw",
            "city",
            "region",
            "country",
            "country_code",
            "latitude",
            "longitude",
        )
    )


def _normalize_geography_evidence(
    value: Any,
    *,
    default_basis: str = "",
    default_kind: str = "profile_location",
    default_surface: str = "",
) -> dict[str, Any]:
    if isinstance(value, str):
        source: dict[str, Any] = {"location": value}
    elif isinstance(value, dict):
        source = dict(value)
    else:
        return {}
    location = _location_parts(source.get("location") or source)
    if not _has_location(location):
        return {}
    basis = text_value(
        source.get("geography_basis")
        or source.get("evidence_type")
        or default_basis
    )
    if not basis:
        basis = (
            "platform_structured_location"
            if any(
                location.get(field)
                for field in (
                    "city",
                    "region",
                    "country",
                    "country_code",
                    "latitude",
                    "longitude",
                )
            )
            else "self_declared_profile_location"
        )
    classification, is_self_declared = _geography_semantics(basis)
    return {
        "kind": text_value(source.get("kind") or default_kind),
        "raw": text_value(location.get("raw")),
        "city": text_value(location.get("city")),
        "region": text_value(location.get("region")),
        "country": text_value(location.get("country")),
        "country_code": text_value(location.get("country_code")).upper(),
        "latitude": location.get("latitude"),
        "longitude": location.get("longitude"),
        "evidence_type": basis,
        "classification": classification,
        "is_self_declared": is_self_declared,
        "is_audience_geography": False,
        "source_surface": text_value(
            source.get("source_surface") or default_surface
        ),
    }


def _pronouns(text: str) -> list[str]:
    patterns = (
        ("she/her", r"(?<!\w)she\s*/\s*her(?!\w)"),
        ("he/him", r"(?<!\w)he\s*/\s*him(?!\w)"),
        ("they/them", r"(?<!\w)they\s*/\s*them(?!\w)"),
        ("she/they", r"(?<!\w)she\s*/\s*they(?!\w)"),
        ("he/they", r"(?<!\w)he\s*/\s*they(?!\w)"),
    )
    return [label for label, pattern in patterns if re.search(pattern, text, re.I)]


def _explicit_pronouns(value: Any) -> list[str]:
    values = value if isinstance(value, list) else [value]
    output: list[str] = []
    for item in values:
        if isinstance(item, dict):
            item = item.get("text") or item.get("value") or item.get("name")
        pronoun = text_value(item)
        if pronoun and pronoun not in output:
            output.append(pronoun)
    return output


def _age_statement(text: str) -> str:
    patterns = (
        r"(?<!\w)(?:age|aged)\s*[:=-]?\s*(?:1[3-9]|[2-9]\d)(?!\d)",
        r"(?<!\w)(?:1[3-9]|[2-9]\d)\s*(?:y/o|yo|years?\s+old)(?!\w)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return match.group(0).strip()
    return ""


def _bio_location_statement(text: str) -> str:
    if not text:
        return ""
    pattern = (
        r"(?i)(?:\bbased\s+in\b|\blocated\s+in\b|\bliving\s+in\b)"
        r"\s+([^\n|;]{2,80})"
    )
    match = re.search(pattern, text)
    return match.group(0).strip(" .") if match else ""


def _source_entry(
    *,
    source: str,
    collection_method: str,
    observed_at: str,
    classification: str = "public_profile",
) -> dict[str, str]:
    return {
        "source": source,
        "collection_method": collection_method,
        "observed_at": observed_at,
        "classification": classification,
    }


def extract_profile_candidate(
    record: dict[str, Any],
    *,
    platform: str,
    role: str,
) -> dict[str, Any]:
    """Extract a compact profile candidate from a content or comment payload."""

    if not isinstance(record, dict):
        return {}
    platform = normalize_platform(platform or record.get("platform"))
    containers = list(_containers(record))
    nested = containers[1:] or containers
    is_creator = role == "creator"

    if is_creator:
        creator_label = _first([record], "content_creator", "creator")
        username = _first(
            [record],
            "username",
            "creator_username",
            "author_username",
            "handle",
        )
        if platform not in {"facebook", "youtube"}:
            username = username or creator_label
        user_id = _first([record], "creator_id", "owner_id", "user_id")
        display_name = _first(
            [record],
            "creator_display_name",
            "creator_name",
            "channel_name",
        )
        if platform in {"facebook", "youtube"}:
            display_name = display_name or creator_label
        verified = _first([record], "creator_verified")
        location = _first([record], "creator_profile_location")
        bio = _first([record], "creator_bio", "creator_profile_bio")
        avatar_url = _first([record], "creator_avatar_url", "creator_profile_image_url")
    else:
        author_label = _first([record], "author", "author_name")
        username = _first(
            [record],
            "username",
            "user_name",
            "author_username",
            "handle",
        )
        if platform != "facebook":
            username = username or author_label
        user_id = _first([record], "author_id", "user_id", "from_id")
        display_name = _first([record], "author_display_name", "author_name")
        if platform == "facebook" or not normalize_username(platform, username):
            display_name = display_name or author_label
        verified = _first([record], "author_verified")
        location = _first([record], "author_profile_location")
        bio = _first([record], "author_bio", "author_profile_bio")
        avatar_url = _first([record], "author_avatar_url", "author_profile_image_url")

    username = username or _first(
        nested,
        "username",
        "unique_id",
        "uniqueId",
        "screen_name",
        "handle",
    )
    user_id = user_id or _first(
        nested,
        "user_id",
        "creator_id",
        "author_id",
        "rest_id",
        "channelId",
        "externalChannelId",
        "id",
        "uid",
        "pk",
        "sec_uid",
        "secUid",
    )
    display_name = display_name or _first(
        nested,
        "display_name",
        "full_name",
        "nickname",
        "name",
        "title",
    )
    verified = verified if verified not in (None, "") else _first(
        nested, "verified", "is_verified", "is_blue_verified"
    )
    location = location or _first(
        nested, "location", "profile_location", "declared_location"
    )
    bio = bio or _first(
        nested, "bio", "biography", "bio_description", "signature", "description"
    )
    avatar_url = avatar_url or _first(
        nested,
        "avatar_url",
        "profile_image_url",
        "profile_pic_url",
        "profile_picture_url",
        "avatarLarger",
        "avatarMedium",
    )
    website = _first(
        nested, "website", "bio_url", "external_url", "url"
    )
    follower_count = _first(
        containers, "follower_count", "followers_count", "followerCount", "subscriber_count"
    )
    following_count = _first(
        containers, "following_count", "follows_count", "friends_count", "followingCount"
    )
    content_count = _first(
        containers, "video_count", "media_count", "tweet_count", "statuses_count", "videoCount"
    )
    likes_count = _first(
        containers, "likes_count", "heart_count", "heart", "digg_count"
    )
    account_type = _first(nested, "account_type", "user_type", "type")
    category = _first(nested, "category", "category_name", "business_category_name")
    created_at = _first(nested, "created_at", "create_time", "publishedAt")
    protected = _first(nested, "protected", "is_private", "private_account")
    language = _first(nested, "default_language", "language", "lang")
    country = _first(nested, "country", "country_name")
    country_code = _first(nested, "country_code", "countryCode")

    return {
        "platform": platform,
        "user_id": normalize_user_id(platform, user_id),
        "username": normalize_username(platform, username),
        "display_name": text_value(display_name),
        "profile_url": canonical_profile_url(platform, username, user_id),
        "avatar_url": text_value(avatar_url),
        "bio": text_value(bio),
        "website": text_value(website),
        "verified": optional_bool(verified),
        "protected": optional_bool(protected),
        "account_type": text_value(account_type),
        "category": text_value(category),
        "created_at": text_value(created_at),
        "follower_count": optional_int(follower_count),
        "following_count": optional_int(following_count),
        "content_count": optional_int(content_count),
        "likes_count": optional_int(likes_count),
        "location": location,
        "country": text_value(country),
        "country_code": text_value(country_code).upper(),
        "default_language": text_value(language),
    }


def normalize_profile_record(
    record: dict[str, Any],
    *,
    platform: str | None = None,
    source: str = "profile",
    collection_method: str = "unknown",
    observed_at: str = "",
) -> dict[str, Any]:
    """Return a stable public-profile record with field-level provenance."""

    source_record = dict(record or {})
    platform = normalize_platform(platform or source_record.get("platform"))
    if platform not in SUPPORTED_PLATFORMS:
        platform = platform or "unknown"
    observed_at = text_value(observed_at or source_record.get("observed_at"))
    if not observed_at:
        observed_at = dt.datetime.now().astimezone().replace(microsecond=0).isoformat()

    user_id = normalize_user_id(platform, source_record.get("user_id"))
    username = normalize_username(platform, source_record.get("username"))
    key = profile_key(platform, user_id, username)
    display_name = text_value(source_record.get("display_name"))
    bio = text_value(source_record.get("bio"))
    website = text_value(source_record.get("website"))
    avatar_url = text_value(source_record.get("avatar_url"))
    profile_url = text_value(source_record.get("profile_url")) or canonical_profile_url(
        platform, username, user_id
    )
    location = _location_parts(
        source_record.get("location")
        or source_record.get("declared_location")
        or source_record.get("profile_location")
    )
    if source_record.get("country") and not location.get("country"):
        location["country"] = text_value(source_record.get("country"))
    if source_record.get("country_code") and not location.get("country_code"):
        location["country_code"] = text_value(source_record.get("country_code")).upper()
    for key_name in ("city", "region", "latitude", "longitude"):
        if source_record.get(key_name) not in (None, "") and not location.get(key_name):
            location[key_name] = source_record.get(key_name)

    geography_basis = text_value(source_record.get("geography_basis"))
    if _has_location(location) and not geography_basis:
        geography_basis = (
            "platform_structured_location"
            if any(
                location.get(field)
                for field in (
                    "city",
                    "region",
                    "country",
                    "country_code",
                    "latitude",
                    "longitude",
                )
            )
            else "self_declared_profile_location"
        )
    evidence_values = source_record.get("geography_evidence")
    if not isinstance(evidence_values, list):
        evidence_values = source_record.get("profile_geography_evidence")
    if not isinstance(evidence_values, list):
        evidence_values = []
    geography_evidence: list[dict[str, Any]] = []
    seen_geographies: set[tuple[Any, ...]] = set()
    for value in evidence_values:
        evidence = _normalize_geography_evidence(value)
        if not evidence:
            continue
        marker = tuple(
            evidence.get(field)
            for field in (
                "kind",
                "raw",
                "city",
                "region",
                "country",
                "country_code",
                "latitude",
                "longitude",
                "evidence_type",
            )
        )
        if marker in seen_geographies:
            continue
        seen_geographies.add(marker)
        geography_evidence.append(evidence)
    primary_evidence = _normalize_geography_evidence(
        {
            "kind": source_record.get("location_kind") or "profile_location",
            "location": location,
            "geography_basis": geography_basis,
            "source_surface": source_record.get("location_source_surface") or "",
        }
    )
    if primary_evidence:
        primary_location_marker = tuple(
            primary_evidence.get(field)
            for field in (
                "raw",
                "city",
                "region",
                "country",
                "country_code",
                "latitude",
                "longitude",
            )
        )
        existing_location_markers = {
            tuple(
                evidence.get(field)
                for field in (
                    "raw",
                    "city",
                    "region",
                    "country",
                    "country_code",
                    "latitude",
                    "longitude",
                )
            )
            for evidence in geography_evidence
        }
        if primary_location_marker not in existing_location_markers:
            geography_evidence.insert(0, primary_evidence)
    elif geography_evidence:
        primary_evidence = geography_evidence[0]
        location = {
            field: primary_evidence.get(field)
            for field in (
                "raw",
                "city",
                "region",
                "country",
                "country_code",
                "latitude",
                "longitude",
            )
        }
        geography_basis = text_value(primary_evidence.get("evidence_type"))

    direct_location = bool(primary_evidence)
    geography_classification, is_self_declared_geography = _geography_semantics(
        geography_basis
    )
    bio_location = _bio_location_statement(bio)
    pronouns = _explicit_pronouns(source_record.get("pronouns"))
    for pronoun in _pronouns(bio):
        if pronoun not in pronouns:
            pronouns.append(pronoun)
    age_statement = text_value(source_record.get("age_statement")) or _age_statement(bio)
    birthdate_statement = text_value(
        source_record.get("birthdate") or source_record.get("birthday")
    )
    gender_statement = text_value(source_record.get("gender_statement"))
    if is_non_demographic_gender_value(platform, gender_statement):
        gender_statement = ""

    scalar_fields = {
        "display_name": display_name,
        "profile_url": profile_url,
        "avatar_url": avatar_url,
        "bio": bio,
        "website": website,
        "verified": optional_bool(source_record.get("verified")),
        "protected": optional_bool(source_record.get("protected")),
        "account_type": text_value(source_record.get("account_type")),
        "category": text_value(source_record.get("category")),
        "created_at": text_value(source_record.get("created_at")),
        "follower_count": optional_int(source_record.get("follower_count")),
        "following_count": optional_int(source_record.get("following_count")),
        "content_count": optional_int(source_record.get("content_count")),
        "likes_count": optional_int(source_record.get("likes_count")),
        "view_count": optional_int(source_record.get("view_count")),
        "default_language": text_value(source_record.get("default_language")),
    }
    provenance = {
        field: _source_entry(
            source=source,
            collection_method=collection_method,
            observed_at=observed_at,
        )
        for field, value in scalar_fields.items()
        if value not in (None, "")
    }
    if direct_location:
        provenance["profile_geography"] = _source_entry(
            source=source,
            collection_method=collection_method,
            observed_at=observed_at,
            classification=geography_classification,
        )
        provenance["declared_geography"] = dict(provenance["profile_geography"])
        provenance["profile_geography_evidence"] = _source_entry(
            source=source,
            collection_method=collection_method,
            observed_at=observed_at,
            classification="public_profile_geography_evidence_not_audience",
        )
    if bio_location:
        provenance["bio_location_statement"] = _source_entry(
            source="profile_bio",
            collection_method=collection_method,
            observed_at=observed_at,
            classification="self_declared_text_not_geocoded",
        )
    if pronouns or age_statement or birthdate_statement or gender_statement:
        provenance["self_declared_demographic_text"] = _source_entry(
            source=(
                "profile_structured_field"
                if (
                    source_record.get("pronouns")
                    or birthdate_statement
                    or gender_statement
                )
                else "profile_bio"
            ),
            collection_method=collection_method,
            observed_at=observed_at,
            classification="self_declared_text_not_inferred",
        )

    substantive = any(
        value not in (None, "")
        for value in (
            display_name,
            bio,
            website,
            avatar_url,
            scalar_fields["verified"],
            scalar_fields["follower_count"],
            scalar_fields["following_count"],
            scalar_fields["content_count"],
            scalar_fields["view_count"],
            location.get("raw"),
            location.get("country_code"),
            pronouns,
            age_statement,
            birthdate_statement,
            gender_statement,
        )
    )
    availability = {
        "identity": "available" if key else "missing",
        "bio": "available" if bio else "not_exposed",
        "declared_geography": "available" if direct_location else "not_exposed",
        "profile_geography": "available" if direct_location else "not_exposed",
        "profile_geography_evidence": (
            "available" if geography_evidence else "not_exposed"
        ),
        "bio_location_statement": "available" if bio_location else "not_observed",
        "self_declared_demographic_text": (
            "available"
            if pronouns or age_statement or birthdate_statement or gender_statement
            else "not_observed"
        ),
        "audience_geography": "first_party_required",
        "audience_demographics": "first_party_required",
    }

    normalized_geography = {
        "raw": text_value(location.get("raw")),
        "city": text_value(location.get("city")),
        "region": text_value(location.get("region")),
        "country": text_value(location.get("country")),
        "country_code": text_value(location.get("country_code")).upper(),
        "latitude": location.get("latitude"),
        "longitude": location.get("longitude"),
        "evidence_type": geography_basis if direct_location else "unavailable",
        "classification": geography_classification if direct_location else "unavailable",
        "is_self_declared": is_self_declared_geography if direct_location else False,
        "is_audience_geography": False,
    }
    normalized = {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "platform": platform,
        "profile_key": key,
        "user_id": user_id,
        "username": username,
        **scalar_fields,
        "profile_geography": normalized_geography,
        "profile_geography_evidence": geography_evidence,
        # Retained for output compatibility; evidence_type now makes the basis explicit.
        "declared_geography": dict(normalized_geography),
        "bio_location_statement": bio_location,
        "self_declared": {
            "pronouns": pronouns,
            "age_statement": age_statement,
            "birthdate_statement": birthdate_statement,
            "gender_statement": gender_statement,
            "gender_classification": None,
            "inference_policy": "no_name_photo_or_pronoun_based_demographic_inference",
        },
        "audience_analytics": {
            "geography": None,
            "age_bands": None,
            "gender_distribution": None,
            "status": "first_party_required",
        },
        "availability": availability,
        "field_provenance": provenance,
        "collection_method": collection_method,
        "supporting_methods": [
            text_value(method)
            for method in (
                source_record.get("supporting_methods")
                or source_record.get("_supporting_methods")
                or []
            )
            if text_value(method)
        ],
        "source": source,
        "status": "available" if substantive else "identity_only" if key else "invalid",
        "observed_at": observed_at,
    }
    return normalized


def profile_has_public_data(profile: dict[str, Any]) -> bool:
    return text_value(profile.get("status")) == "available"


def profile_observation_hash(profile: dict[str, Any]) -> str:
    stable = {
        key: value
        for key, value in profile.items()
        if key not in {"observed_at", "field_provenance"}
    }
    payload = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()


def compact_profile_reference(profile: dict[str, Any] | None) -> dict[str, Any]:
    if not profile:
        return {
            "profile_key": "",
            "status": "pending_or_unavailable",
            "profile_geography": None,
            "profile_geography_evidence": [],
            "declared_geography": None,
            "self_declared": None,
        }
    return {
        "profile_key": text_value(profile.get("profile_key")),
        "status": text_value(profile.get("status") or "unknown"),
        "observed_at": text_value(profile.get("observed_at")),
        "profile_geography": profile.get("profile_geography")
        or profile.get("declared_geography"),
        "profile_geography_evidence": profile.get("profile_geography_evidence")
        or [],
        "declared_geography": profile.get("declared_geography"),
        "self_declared": profile.get("self_declared"),
        "audience_analytics": profile.get("audience_analytics"),
    }
