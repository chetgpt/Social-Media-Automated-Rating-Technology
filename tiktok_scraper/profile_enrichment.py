"""Profile enrichment queue, storage, collectors, and exports.

The main scrape path only queues identities and preserves embedded public
profile snippets. Network enrichment runs after ingestion and is fail-soft, so
rate limits or unavailable profiles never invalidate collected content.
"""

from __future__ import annotations

import asyncio
import csv
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Iterable
from urllib.parse import quote, urlparse

import requests  # type: ignore[import-untyped]

from tiktok_scraper.profile_contract import (
    canonical_profile_url,
    compact_profile_reference,
    extract_profile_candidate,
    is_non_demographic_gender_value,
    normalize_platform,
    normalize_profile_record,
    normalize_user_id,
    normalize_username,
    optional_int,
    profile_has_public_data,
    profile_key,
    profile_observation_hash,
    text_value,
)
from tiktok_scraper.profile_web_api import (
    decode_json_payloads,
    merge_profile_records,
    parse_facebook_about_text_blocks,
    parse_profile_payload,
    profile_payload_shape_hints,
    profile_response_url_matches,
)


DEFAULT_CACHE_DAYS = 14
DEFAULT_PROFILE_LIMIT = 100
DEFAULT_PROFILE_TIMEOUT_SECONDS = 120


def _now() -> dt.datetime:
    return dt.datetime.now().astimezone().replace(microsecond=0)


def _iso(value: dt.datetime) -> str:
    return value.replace(microsecond=0).isoformat()


def _future_iso(*, days: float = 0, hours: float = 0) -> str:
    return _iso(_now() + dt.timedelta(days=days, hours=hours))


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return list(value)
    if not value:
        return []
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def init_profile_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS user_profiles (
            project TEXT NOT NULL,
            platform TEXT NOT NULL,
            profile_key TEXT NOT NULL,
            user_id TEXT DEFAULT '',
            username TEXT DEFAULT '',
            display_name TEXT DEFAULT '',
            status TEXT NOT NULL,
            collection_method TEXT DEFAULT '',
            declared_location TEXT DEFAULT '',
            country_code TEXT DEFAULT '',
            region TEXT DEFAULT '',
            city TEXT DEFAULT '',
            observed_at TEXT NOT NULL,
            expires_at TEXT DEFAULT '',
            profile_json TEXT NOT NULL,
            PRIMARY KEY (project, platform, profile_key)
        );
        CREATE INDEX IF NOT EXISTS idx_user_profiles_identity
            ON user_profiles(project, platform, user_id, username);
        CREATE INDEX IF NOT EXISTS idx_user_profiles_expiry
            ON user_profiles(project, expires_at);

        CREATE TABLE IF NOT EXISTS user_profile_observations (
            observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
            project TEXT NOT NULL,
            platform TEXT NOT NULL,
            profile_key TEXT NOT NULL,
            observation_hash TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            collection_method TEXT DEFAULT '',
            profile_json TEXT NOT NULL,
            UNIQUE(project, platform, profile_key, observation_hash)
        );
        CREATE INDEX IF NOT EXISTS idx_profile_observations_identity
            ON user_profile_observations(project, platform, profile_key, observed_at);

        CREATE TABLE IF NOT EXISTS profile_enrichment_queue (
            project TEXT NOT NULL,
            platform TEXT NOT NULL,
            profile_key TEXT NOT NULL,
            user_id TEXT DEFAULT '',
            username TEXT DEFAULT '',
            display_name TEXT DEFAULT '',
            profile_url TEXT DEFAULT '',
            priority INTEGER DEFAULT 0,
            source_roles_json TEXT DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'pending',
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            last_attempt_at TEXT DEFAULT '',
            next_attempt_at TEXT DEFAULT '',
            attempt_count INTEGER DEFAULT 0,
            last_error TEXT DEFAULT '',
            PRIMARY KEY (project, platform, profile_key)
        );
        CREATE INDEX IF NOT EXISTS idx_profile_queue_pending
            ON profile_enrichment_queue(project, status, next_attempt_at, priority);
        """
    )


def _merge_dict(preferred: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    output = dict(fallback)
    for key, value in preferred.items():
        if isinstance(value, dict) and isinstance(output.get(key), dict):
            output[key] = _merge_dict(value, output[key])
        elif value not in (None, "", [], {}):
            output[key] = value
        elif key not in output:
            output[key] = value
    return output


def upsert_profile(
    conn: sqlite3.Connection,
    *,
    project: str,
    profile: dict[str, Any],
    cache_days: int = DEFAULT_CACHE_DAYS,
    mark_queue_complete: bool = True,
) -> dict[str, Any]:
    init_profile_schema(conn)
    platform = normalize_platform(profile.get("platform"))
    key = text_value(profile.get("profile_key"))
    if not platform or not key:
        raise ValueError("profile requires platform and profile_key")

    existing_row = conn.execute(
        """
        SELECT profile_json FROM user_profiles
        WHERE project = ? AND platform = ? AND profile_key = ?
        """,
        (project, platform, key),
    ).fetchone()
    existing = _json_dict(existing_row[0]) if existing_row else {}
    merged = _merge_dict(profile, existing)
    merged["platform"] = platform
    merged["profile_key"] = key
    merged["observed_at"] = text_value(profile.get("observed_at")) or _iso(_now())
    self_declared = merged.get("self_declared")
    if isinstance(self_declared, dict) and is_non_demographic_gender_value(
        platform,
        self_declared.get("gender_statement"),
    ):
        self_declared["gender_statement"] = ""
        if not any(
            self_declared.get(field)
            for field in (
                "pronouns",
                "age_statement",
                "birthdate_statement",
                "gender_statement",
            )
        ):
            provenance = merged.get("field_provenance")
            if isinstance(provenance, dict):
                provenance.pop("self_declared_demographic_text", None)
            availability = merged.get("availability")
            if isinstance(availability, dict):
                availability["self_declared_demographic_text"] = "not_observed"
    if profile_has_public_data(profile) or profile_has_public_data(existing):
        merged["status"] = "available"

    geo = merged.get("declared_geography")
    if not isinstance(geo, dict):
        geo = {}
    expires_at = _future_iso(days=max(1, int(cache_days)))
    payload = json.dumps(merged, ensure_ascii=False, separators=(",", ":"))
    conn.execute(
        """
        INSERT INTO user_profiles (
            project, platform, profile_key, user_id, username, display_name,
            status, collection_method, declared_location, country_code,
            region, city, observed_at, expires_at, profile_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(project, platform, profile_key) DO UPDATE SET
            user_id = COALESCE(NULLIF(excluded.user_id, ''), user_profiles.user_id),
            username = COALESCE(NULLIF(excluded.username, ''), user_profiles.username),
            display_name = COALESCE(NULLIF(excluded.display_name, ''), user_profiles.display_name),
            status = excluded.status,
            collection_method = COALESCE(NULLIF(excluded.collection_method, ''), user_profiles.collection_method),
            declared_location = COALESCE(NULLIF(excluded.declared_location, ''), user_profiles.declared_location),
            country_code = COALESCE(NULLIF(excluded.country_code, ''), user_profiles.country_code),
            region = COALESCE(NULLIF(excluded.region, ''), user_profiles.region),
            city = COALESCE(NULLIF(excluded.city, ''), user_profiles.city),
            observed_at = excluded.observed_at,
            expires_at = excluded.expires_at,
            profile_json = excluded.profile_json
        """,
        (
            project,
            platform,
            key,
            text_value(merged.get("user_id")),
            text_value(merged.get("username")),
            text_value(merged.get("display_name")),
            text_value(merged.get("status") or "identity_only"),
            text_value(merged.get("collection_method")),
            text_value(geo.get("raw")),
            text_value(geo.get("country_code")),
            text_value(geo.get("region")),
            text_value(geo.get("city")),
            text_value(merged.get("observed_at")),
            expires_at,
            payload,
        ),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO user_profile_observations (
            project, platform, profile_key, observation_hash, observed_at,
            collection_method, profile_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            project,
            platform,
            key,
            profile_observation_hash(merged),
            text_value(merged.get("observed_at")),
            text_value(merged.get("collection_method")),
            payload,
        ),
    )
    if mark_queue_complete:
        conn.execute(
            """
            UPDATE profile_enrichment_queue
            SET status = 'complete', last_error = '', next_attempt_at = ?
            WHERE project = ? AND platform = ? AND profile_key = ?
            """,
            (expires_at, project, platform, key),
        )
    return merged


def enqueue_profile_candidate(
    conn: sqlite3.Connection,
    *,
    project: str,
    platform: str,
    candidate: dict[str, Any],
    source_role: str,
    observed_at: str,
    priority: int,
) -> str:
    init_profile_schema(conn)
    platform = normalize_platform(platform)
    normalized = normalize_profile_record(
        candidate,
        platform=platform,
        source=f"embedded_{source_role}",
        collection_method="embedded_payload",
        observed_at=observed_at,
    )
    key = text_value(normalized.get("profile_key"))
    if not key:
        return ""
    user_id = text_value(normalized.get("user_id"))
    username = text_value(normalized.get("username"))
    existing_identity = conn.execute(
        """
        SELECT profile_key
        FROM (
            SELECT profile_key, user_id, username, 0 AS source_order
            FROM user_profiles
            WHERE project = ? AND platform = ?
            UNION ALL
            SELECT profile_key, user_id, username, 1 AS source_order
            FROM profile_enrichment_queue
            WHERE project = ? AND platform = ?
        )
        WHERE (? != '' AND user_id = ?)
           OR (? != '' AND LOWER(username) = LOWER(?))
        ORDER BY source_order, profile_key
        LIMIT 1
        """,
        (
            project,
            platform,
            project,
            platform,
            user_id,
            user_id,
            username,
            username,
        ),
    ).fetchone()
    if existing_identity:
        key = text_value(existing_identity[0])
        normalized["profile_key"] = key

    existing = conn.execute(
        """
        SELECT source_roles_json FROM profile_enrichment_queue
        WHERE project = ? AND platform = ? AND profile_key = ?
        """,
        (project, platform, key),
    ).fetchone()
    roles = {text_value(item) for item in _json_list(existing[0])} if existing else set()
    roles.add(source_role)
    roles.discard("")
    now_value = observed_at or _iso(_now())
    conn.execute(
        """
        INSERT INTO profile_enrichment_queue (
            project, platform, profile_key, user_id, username, display_name,
            profile_url, priority, source_roles_json, status, first_seen_at,
            last_seen_at, next_attempt_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, '')
        ON CONFLICT(project, platform, profile_key) DO UPDATE SET
            user_id = COALESCE(NULLIF(excluded.user_id, ''), profile_enrichment_queue.user_id),
            username = COALESCE(NULLIF(excluded.username, ''), profile_enrichment_queue.username),
            display_name = COALESCE(NULLIF(excluded.display_name, ''), profile_enrichment_queue.display_name),
            profile_url = COALESCE(NULLIF(excluded.profile_url, ''), profile_enrichment_queue.profile_url),
            priority = MAX(profile_enrichment_queue.priority, excluded.priority),
            source_roles_json = excluded.source_roles_json,
            last_seen_at = excluded.last_seen_at,
            status = CASE
                WHEN profile_enrichment_queue.status = 'complete'
                THEN profile_enrichment_queue.status
                ELSE 'pending'
            END
        """,
        (
            project,
            platform,
            key,
            text_value(normalized.get("user_id")),
            text_value(normalized.get("username")),
            text_value(normalized.get("display_name")),
            text_value(normalized.get("profile_url")),
            max(0, int(priority)),
            json.dumps(sorted(roles), ensure_ascii=False),
            now_value,
            now_value,
        ),
    )
    if profile_has_public_data(normalized):
        upsert_profile(
            conn,
            project=project,
            profile=normalized,
            cache_days=1,
            mark_queue_complete=False,
        )
    return key


def enqueue_content_profile(
    conn: sqlite3.Connection,
    *,
    project: str,
    platform: str,
    record: dict[str, Any],
    observed_at: str,
) -> str:
    candidate = extract_profile_candidate(record, platform=platform, role="creator")
    return enqueue_profile_candidate(
        conn,
        project=project,
        platform=platform,
        candidate=candidate,
        source_role="creator",
        observed_at=observed_at,
        priority=100,
    )


def enqueue_comment_profile(
    conn: sqlite3.Connection,
    *,
    project: str,
    platform: str,
    record: dict[str, Any],
    observed_at: str,
) -> str:
    candidate = extract_profile_candidate(record, platform=platform, role="comment_author")
    likes = optional_int(record.get("likes") or record.get("like_count") or record.get("digg_count")) or 0
    priority = min(80, 20 + (10 if likes > 0 else 0) + (10 if likes >= 10 else 0) + (10 if likes >= 100 else 0))
    return enqueue_profile_candidate(
        conn,
        project=project,
        platform=platform,
        candidate=candidate,
        source_role="comment_author",
        observed_at=observed_at,
        priority=priority,
    )


def backfill_profile_queue(
    conn: sqlite3.Connection,
    project: str,
    *,
    force: bool = False,
) -> dict[str, int]:
    """Queue identities already stored before profile enrichment was added."""

    init_profile_schema(conn)
    marker = f"profile_queue_backfill_v1:{project}"
    if not force:
        existing = conn.execute(
            "SELECT value FROM project_meta WHERE key = ?",
            (marker,),
        ).fetchone()
        if existing:
            return {"content_profiles": 0, "comment_profiles": 0, "already_complete": 1}

    queued_content: set[tuple[str, str]] = set()
    content_rows = conn.execute(
        """
        SELECT platform, creator, raw_json, last_scraped_at
        FROM content_items
        WHERE project = ?
        """,
        (project,),
    ).fetchall()
    for platform, creator, raw_json, observed_at in content_rows:
        raw = _json_dict(raw_json)
        raw["content_creator"] = raw.get("content_creator") or creator
        key = enqueue_content_profile(
            conn,
            project=project,
            platform=platform,
            record=raw,
            observed_at=observed_at or _iso(_now()),
        )
        if key:
            queued_content.add((platform, key))

    queued_comments: set[tuple[str, str]] = set()
    comment_rows = conn.execute(
        """
        SELECT platform, author, author_id, MAX(likes), MAX(last_seen_at), MAX(raw_json)
        FROM comments
        WHERE project = ? AND (author != '' OR author_id != '')
        GROUP BY platform, author, author_id
        """,
        (project,),
    ).fetchall()
    for platform, author, author_id, likes, observed_at, raw_json in comment_rows:
        raw = _json_dict(raw_json)
        raw["author"] = raw.get("author") or author
        raw["author_id"] = raw.get("author_id") or author_id
        raw["likes"] = max(optional_int(raw.get("likes")) or 0, optional_int(likes) or 0)
        key = enqueue_comment_profile(
            conn,
            project=project,
            platform=platform,
            record=raw,
            observed_at=observed_at or _iso(_now()),
        )
        if key:
            queued_comments.add((platform, key))

    conn.execute(
        "INSERT OR REPLACE INTO project_meta(key, value) VALUES (?, ?)",
        (marker, _iso(_now())),
    )
    conn.commit()
    return {
        "content_profiles": len(queued_content),
        "comment_profiles": len(queued_comments),
        "already_complete": 0,
    }


def load_profile_lookup(conn: sqlite3.Connection, project: str) -> dict[str, Any]:
    init_profile_schema(conn)
    rows = conn.execute(
        """
        SELECT platform, profile_key, user_id, username, profile_json
        FROM user_profiles WHERE project = ?
        ORDER BY observed_at
        """,
        (project,),
    ).fetchall()
    profiles: list[dict[str, Any]] = []
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    by_id: dict[tuple[str, str], dict[str, Any]] = {}
    by_username: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        platform = normalize_platform(row[0])
        profile = _json_dict(row[4])
        if not profile:
            continue
        profiles.append(profile)
        by_key[(platform, text_value(row[1]))] = profile
        user_id = normalize_user_id(platform, row[2])
        username = normalize_username(platform, row[3]).casefold()
        if user_id:
            by_id[(platform, user_id)] = profile
        if username:
            by_username[(platform, username)] = profile
    return {
        "profiles": profiles,
        "by_key": by_key,
        "by_id": by_id,
        "by_username": by_username,
    }


def match_profile(
    lookup: dict[str, Any],
    *,
    platform: str,
    user_id: Any = "",
    username: Any = "",
) -> dict[str, Any] | None:
    platform = normalize_platform(platform)
    normalized_id = normalize_user_id(platform, user_id)
    normalized_username = normalize_username(platform, username).casefold()
    if normalized_id:
        found = lookup.get("by_id", {}).get((platform, normalized_id))
        if found:
            return found
    if normalized_username:
        return lookup.get("by_username", {}).get((platform, normalized_username))
    return None


def profile_reference_for(
    lookup: dict[str, Any],
    *,
    platform: str,
    user_id: Any = "",
    username: Any = "",
) -> dict[str, Any]:
    return compact_profile_reference(
        match_profile(
            lookup,
            platform=platform,
            user_id=user_id,
            username=username,
        )
    )


def profile_coverage(conn: sqlite3.Connection, project: str) -> dict[str, Any]:
    init_profile_schema(conn)
    queue_rows = conn.execute(
        """
        SELECT platform, status, COUNT(*)
        FROM profile_enrichment_queue
        WHERE project = ?
        GROUP BY platform, status
        """,
        (project,),
    ).fetchall()
    profile_rows = conn.execute(
        """
        SELECT platform, status, declared_location, country_code, profile_json
        FROM user_profiles
        WHERE project = ?
        """,
        (project,),
    ).fetchall()
    by_platform: dict[str, dict[str, Any]] = {}
    for platform, status, count in queue_rows:
        entry = by_platform.setdefault(
            platform,
            {
                "queued": 0,
                "queue_status_counts": {},
                "profiles_available": 0,
                "profiles_with_declared_geography": 0,
                "profiles_with_profile_geography": 0,
                "profiles_with_self_declared_geography": 0,
                "profiles_with_platform_geography": 0,
                "profiles_with_country_code": 0,
                "profiles_with_multiple_geography_evidence": 0,
                "profile_geography_evidence_count": 0,
                "profiles_with_self_declared_demographic_text": 0,
                "profiles_with_pronouns": 0,
                "profiles_with_public_birthdate": 0,
                "profiles_with_public_gender_statement": 0,
            },
        )
        entry["queued"] += int(count)
        entry["queue_status_counts"][status] = int(count)
    for platform, status, location, country_code, payload in profile_rows:
        entry = by_platform.setdefault(
            platform,
            {
                "queued": 0,
                "queue_status_counts": {},
                "profiles_available": 0,
                "profiles_with_declared_geography": 0,
                "profiles_with_profile_geography": 0,
                "profiles_with_self_declared_geography": 0,
                "profiles_with_platform_geography": 0,
                "profiles_with_country_code": 0,
                "profiles_with_multiple_geography_evidence": 0,
                "profile_geography_evidence_count": 0,
                "profiles_with_self_declared_demographic_text": 0,
                "profiles_with_pronouns": 0,
                "profiles_with_public_birthdate": 0,
                "profiles_with_public_gender_statement": 0,
            },
        )
        profile = _json_dict(payload)
        profile_geo = profile.get("profile_geography")
        if not isinstance(profile_geo, dict):
            profile_geo = profile.get("declared_geography")
        if not isinstance(profile_geo, dict):
            profile_geo = {}
        has_profile_geography = bool(
            location
            or country_code
            or any(
                profile_geo.get(field)
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
        )
        profile_country_code = text_value(
            profile_geo.get("country_code") or country_code
        )
        geography_evidence = profile.get("profile_geography_evidence")
        if not isinstance(geography_evidence, list):
            geography_evidence = []
        if status == "available":
            entry["profiles_available"] += 1
        if has_profile_geography:
            entry["profiles_with_declared_geography"] += 1
            entry["profiles_with_profile_geography"] += 1
            if profile_geo.get("is_self_declared"):
                entry["profiles_with_self_declared_geography"] += 1
            else:
                entry["profiles_with_platform_geography"] += 1
        if profile_country_code:
            entry["profiles_with_country_code"] += 1
        entry["profile_geography_evidence_count"] += len(geography_evidence)
        if len(geography_evidence) > 1:
            entry["profiles_with_multiple_geography_evidence"] += 1
        self_declared = profile.get("self_declared")
        if isinstance(self_declared, dict):
            if self_declared.get("pronouns"):
                entry["profiles_with_pronouns"] += 1
            if self_declared.get("birthdate_statement"):
                entry["profiles_with_public_birthdate"] += 1
            if self_declared.get("gender_statement"):
                entry["profiles_with_public_gender_statement"] += 1
            if (
                self_declared.get("pronouns")
                or self_declared.get("age_statement")
                or self_declared.get("birthdate_statement")
                or self_declared.get("gender_statement")
            ):
                entry["profiles_with_self_declared_demographic_text"] += 1

    totals = {
        "queued": sum(item["queued"] for item in by_platform.values()),
        "profiles_available": sum(item["profiles_available"] for item in by_platform.values()),
        "profiles_with_declared_geography": sum(
            item["profiles_with_declared_geography"] for item in by_platform.values()
        ),
        "profiles_with_profile_geography": sum(
            item["profiles_with_profile_geography"] for item in by_platform.values()
        ),
        "profiles_with_self_declared_geography": sum(
            item["profiles_with_self_declared_geography"] for item in by_platform.values()
        ),
        "profiles_with_platform_geography": sum(
            item["profiles_with_platform_geography"] for item in by_platform.values()
        ),
        "profiles_with_country_code": sum(
            item["profiles_with_country_code"] for item in by_platform.values()
        ),
        "profiles_with_multiple_geography_evidence": sum(
            item["profiles_with_multiple_geography_evidence"]
            for item in by_platform.values()
        ),
        "profile_geography_evidence_count": sum(
            item["profile_geography_evidence_count"]
            for item in by_platform.values()
        ),
        "profiles_with_self_declared_demographic_text": sum(
            item["profiles_with_self_declared_demographic_text"] for item in by_platform.values()
        ),
        "profiles_with_pronouns": sum(
            item["profiles_with_pronouns"] for item in by_platform.values()
        ),
        "profiles_with_public_birthdate": sum(
            item["profiles_with_public_birthdate"] for item in by_platform.values()
        ),
        "profiles_with_public_gender_statement": sum(
            item["profiles_with_public_gender_statement"] for item in by_platform.values()
        ),
    }
    return {
        "schema_version": "1.0",
        "project": project,
        "compiled_at": _iso(_now()),
        "totals": totals,
        "by_platform": by_platform,
        "interpretation": {
            "profile_geography": (
                "Public profile geography labeled by evidence basis. It may be self-declared, "
                "a public business address, a configured channel country, or a platform-reported "
                "account region; it is never audience geography."
            ),
            "self_declared_demographic_text": (
                "Pronouns, explicit profile gender text, public birthdate text, or explicit "
                "age statements preserved without normalization, ethnicity inference, or "
                "image/name-based inference."
            ),
            "audience_geography_and_demographics": (
                "Require first-party analytics access and must remain aggregate."
            ),
        },
    }


def _atomic_json(path: Path, payload: Any) -> None:
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(temp, path)


def _atomic_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with temp.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temp, path)


def export_profiles(conn: sqlite3.Connection, project: str, output_dir: Path) -> dict[str, Any]:
    lookup = load_profile_lookup(conn, project)
    profiles = lookup["profiles"]
    coverage = profile_coverage(conn, project)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "user_profiles.json"
    csv_path = output_dir / "user_profiles.csv"
    coverage_path = output_dir / "profile_coverage.json"

    rows = []
    for profile in profiles:
        geo = profile.get("profile_geography")
        if not isinstance(geo, dict):
            geo = profile.get("declared_geography")
        if not isinstance(geo, dict):
            geo = {}
        geography_evidence = profile.get("profile_geography_evidence")
        if not isinstance(geography_evidence, list):
            geography_evidence = []
        self_declared = profile.get("self_declared") if isinstance(profile.get("self_declared"), dict) else {}
        rows.append(
            {
                "platform": text_value(profile.get("platform")),
                "profile_key": text_value(profile.get("profile_key")),
                "user_id": text_value(profile.get("user_id")),
                "username": text_value(profile.get("username")),
                "display_name": text_value(profile.get("display_name")),
                "profile_url": text_value(profile.get("profile_url")),
                "bio": text_value(profile.get("bio")),
                "website": text_value(profile.get("website")),
                "verified": profile.get("verified"),
                "protected": profile.get("protected"),
                "account_type": text_value(profile.get("account_type")),
                "category": text_value(profile.get("category")),
                "follower_count": profile.get("follower_count"),
                "following_count": profile.get("following_count"),
                "content_count": profile.get("content_count"),
                "likes_count": profile.get("likes_count"),
                "view_count": profile.get("view_count"),
                "default_language": text_value(profile.get("default_language")),
                "declared_location": text_value(geo.get("raw")),
                "city": text_value(geo.get("city")),
                "region": text_value(geo.get("region")),
                "country": text_value(geo.get("country")),
                "country_code": text_value(geo.get("country_code")),
                "location_evidence_type": text_value(geo.get("evidence_type")),
                "location_classification": text_value(geo.get("classification")),
                "location_is_self_declared": geo.get("is_self_declared"),
                "geography_evidence_count": len(geography_evidence),
                "geography_evidence_kinds": "|".join(
                    text_value(item.get("kind"))
                    for item in geography_evidence
                    if isinstance(item, dict) and text_value(item.get("kind"))
                ),
                "geography_evidence_json": json.dumps(
                    geography_evidence,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                "bio_location_statement": text_value(profile.get("bio_location_statement")),
                "self_declared_pronouns": "|".join(self_declared.get("pronouns") or []),
                "self_declared_age_statement": text_value(self_declared.get("age_statement")),
                "self_declared_birthdate_statement": text_value(
                    self_declared.get("birthdate_statement")
                ),
                "self_declared_gender_statement": text_value(
                    self_declared.get("gender_statement")
                ),
                "audience_analytics_status": text_value(
                    (profile.get("audience_analytics") or {}).get("status")
                ),
                "status": text_value(profile.get("status")),
                "collection_method": text_value(profile.get("collection_method")),
                "supporting_methods": "|".join(profile.get("supporting_methods") or []),
                "observed_at": text_value(profile.get("observed_at")),
            }
        )
    fieldnames = list(rows[0]) if rows else [
        "platform",
        "profile_key",
        "user_id",
        "username",
        "display_name",
        "profile_url",
        "bio",
        "website",
        "verified",
        "protected",
        "account_type",
        "category",
        "follower_count",
        "following_count",
        "content_count",
        "likes_count",
        "view_count",
        "default_language",
        "declared_location",
        "city",
        "region",
        "country",
        "country_code",
        "location_evidence_type",
        "location_classification",
        "location_is_self_declared",
        "geography_evidence_count",
        "geography_evidence_kinds",
        "geography_evidence_json",
        "bio_location_statement",
        "self_declared_pronouns",
        "self_declared_age_statement",
        "self_declared_birthdate_statement",
        "self_declared_gender_statement",
        "audience_analytics_status",
        "status",
        "collection_method",
        "supporting_methods",
        "observed_at",
    ]
    _atomic_json(
        json_path,
        {
            "schema_version": "1.0",
            "project": project,
            "compiled_at": _iso(_now()),
            "profiles": profiles,
        },
    )
    _atomic_csv(csv_path, fieldnames, rows)
    _atomic_json(coverage_path, coverage)
    return {
        "profiles_json": str(json_path),
        "profiles_csv": str(csv_path),
        "profile_coverage_json": str(coverage_path),
        "profile_count": len(profiles),
        "coverage": coverage,
    }


class ProfileCollectionError(RuntimeError):
    def __init__(self, status: str, message: str, *, retry_hours: float = 24):
        super().__init__(message)
        self.status = status
        self.retry_hours = retry_hours


class PublicProfileCollector:
    """API-first collector with an optional authenticated-browser fallback."""

    def __init__(
        self,
        *,
        cdp_url: str = "",
        browser_fallback: bool = True,
        session: requests.Session | None = None,
        timeout_seconds: float = 25,
    ):
        self.cdp_url = cdp_url.strip()
        self.browser_fallback = bool(browser_fallback and self.cdp_url)
        self.session = session or requests.Session()
        self.timeout_seconds = max(5.0, float(timeout_seconds))
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._owns_context = False
        self.last_diagnostics: list[dict[str, Any]] = []

    async def __aenter__(self) -> "PublicProfileCollector":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._owns_context and self._context is not None:
            try:
                await self._context.close()
            except Exception:
                pass
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass

    @staticmethod
    def _token_from_file(*names: str) -> str:
        for name in names:
            raw_path = os.environ.get(name, "").strip()
            if not raw_path:
                continue
            try:
                token = Path(raw_path).read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if token:
                return token
        return ""

    def _json_response(self, response: requests.Response, platform: str) -> dict[str, Any]:
        if response.status_code == 404:
            raise ProfileCollectionError("not_found", f"{platform} profile not found", retry_hours=24 * 30)
        if response.status_code == 429:
            raise ProfileCollectionError("rate_limited", f"{platform} profile API rate limited", retry_hours=6)
        if response.status_code in {401, 403}:
            raise ProfileCollectionError(
                "credentials_required",
                f"{platform} profile API credentials were rejected",
                retry_hours=24,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProfileCollectionError(
                "retry",
                f"{platform} profile API returned invalid JSON",
                retry_hours=2,
            ) from exc
        if response.status_code >= 400:
            error = payload.get("error") if isinstance(payload, dict) else {}
            message = text_value(
                error.get("message") if isinstance(error, dict) else error
            ) or f"HTTP {response.status_code}"
            raise ProfileCollectionError("retry", f"{platform}: {message}", retry_hours=4)
        return payload if isinstance(payload, dict) else {}

    def _collect_x_api(self, candidate: dict[str, Any]) -> dict[str, Any] | None:
        token = (
            os.environ.get("X_BEARER_TOKEN", "").strip()
            or os.environ.get("TWITTER_BEARER_TOKEN", "").strip()
            or self._token_from_file("X_BEARER_TOKEN_FILE", "TWITTER_BEARER_TOKEN_FILE")
        )
        username = normalize_username("x", candidate.get("username"))
        user_id = normalize_user_id("x", candidate.get("user_id"))
        if not token or not (username or user_id):
            return None
        endpoint = (
            f"https://api.x.com/2/users/{quote(user_id)}"
            if user_id
            else f"https://api.x.com/2/users/by/username/{quote(username)}"
        )
        response = self.session.get(
            endpoint,
            headers={"Authorization": f"Bearer {token}"},
            params={
                "user.fields": (
                    "created_at,description,entities,id,is_identity_verified,location,"
                    "name,profile_banner_url,profile_image_url,protected,public_metrics,"
                    "url,username,verified,verified_type"
                )
            },
            timeout=self.timeout_seconds,
        )
        payload = self._json_response(response, "x")
        user: dict[str, Any] = (
            dict(payload["data"]) if isinstance(payload.get("data"), dict) else {}
        )
        if not user:
            raise ProfileCollectionError("not_found", "X profile not found", retry_hours=24 * 30)
        metrics: dict[str, Any] = (
            dict(user["public_metrics"])
            if isinstance(user.get("public_metrics"), dict)
            else {}
        )
        return {
            "platform": "x",
            "user_id": user.get("id"),
            "username": user.get("username"),
            "display_name": user.get("name"),
            "profile_url": canonical_profile_url("x", user.get("username"), user.get("id")),
            "avatar_url": user.get("profile_image_url"),
            "bio": user.get("description"),
            "website": user.get("url"),
            "verified": user.get("verified"),
            "protected": user.get("protected"),
            "created_at": user.get("created_at"),
            "follower_count": metrics.get("followers_count"),
            "following_count": metrics.get("following_count"),
            "content_count": metrics.get("tweet_count"),
            "location": user.get("location"),
            "account_type": user.get("verified_type"),
            "_method": "x_api_v2_user_lookup",
        }

    def _collect_youtube_api(self, candidate: dict[str, Any]) -> dict[str, Any] | None:
        api_key = (
            os.environ.get("YOUTUBE_API_KEY", "").strip()
            or os.environ.get("GOOGLE_API_KEY", "").strip()
        )
        if not api_key:
            return None
        user_id = normalize_user_id("youtube", candidate.get("user_id"))
        username = normalize_username("youtube", candidate.get("username"))
        params: dict[str, Any] = {
            "part": "snippet,statistics,brandingSettings,topicDetails,status",
            "key": api_key,
            "maxResults": 1,
        }
        if user_id.startswith("UC"):
            params["id"] = user_id
        elif username:
            params["forHandle"] = f"@{username}"
        else:
            return None
        response = self.session.get(
            "https://www.googleapis.com/youtube/v3/channels",
            headers={"Accept": "application/json"},
            params=params,
            timeout=self.timeout_seconds,
        )
        payload = self._json_response(response, "youtube")
        items: list[Any] = (
            list(payload["items"]) if isinstance(payload.get("items"), list) else []
        )
        if not items or not isinstance(items[0], dict):
            raise ProfileCollectionError("not_found", "YouTube channel not found", retry_hours=24 * 30)
        item: dict[str, Any] = dict(items[0])
        snippet: dict[str, Any] = (
            dict(item["snippet"]) if isinstance(item.get("snippet"), dict) else {}
        )
        statistics: dict[str, Any] = (
            dict(item["statistics"])
            if isinstance(item.get("statistics"), dict)
            else {}
        )
        branding: dict[str, Any] = (
            dict(item["brandingSettings"])
            if isinstance(item.get("brandingSettings"), dict)
            else {}
        )
        channel: dict[str, Any] = (
            dict(branding["channel"])
            if isinstance(branding.get("channel"), dict)
            else {}
        )
        thumbnails: dict[str, Any] = (
            dict(snippet["thumbnails"])
            if isinstance(snippet.get("thumbnails"), dict)
            else {}
        )
        avatar: dict[str, Any] = {}
        for size in ("high", "medium", "default"):
            if isinstance(thumbnails.get(size), dict):
                avatar = dict(thumbnails[size])
                break
        custom_url = text_value(snippet.get("customUrl")).lstrip("@")
        return {
            "platform": "youtube",
            "user_id": item.get("id"),
            "username": custom_url or username,
            "display_name": snippet.get("title"),
            "profile_url": canonical_profile_url(
                "youtube", custom_url or username, item.get("id")
            ),
            "avatar_url": avatar.get("url"),
            "bio": snippet.get("description") or channel.get("description"),
            "created_at": snippet.get("publishedAt"),
            "follower_count": statistics.get("subscriberCount"),
            "content_count": statistics.get("videoCount"),
            "country": snippet.get("country") or channel.get("country"),
            "country_code": snippet.get("country") or channel.get("country"),
            "default_language": snippet.get("defaultLanguage") or channel.get("defaultLanguage"),
            "category": "|".join(item.get("topicDetails", {}).get("topicCategories", []))
            if isinstance(item.get("topicDetails"), dict)
            else "",
            "_method": "youtube_data_api_v3_channels",
        }

    def _collect_tiktok_api(self, candidate: dict[str, Any]) -> dict[str, Any] | None:
        token = (
            os.environ.get("TIKTOK_RESEARCH_ACCESS_TOKEN", "").strip()
            or os.environ.get("TIKTOK_RESEARCH_TOKEN", "").strip()
        )
        username = normalize_username("tiktok", candidate.get("username"))
        if not token or not username:
            return None
        fields = (
            "display_name,bio_description,avatar_url,is_verified,follower_count,"
            "following_count,likes_count,video_count,bio_url"
        )
        response = self.session.post(
            f"https://open.tiktokapis.com/v2/research/user/info/?fields={fields}",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={"username": username},
            timeout=self.timeout_seconds,
        )
        payload = self._json_response(response, "tiktok")
        data: dict[str, Any] = (
            dict(payload["data"]) if isinstance(payload.get("data"), dict) else {}
        )
        user: dict[str, Any] = (
            dict(data["user"]) if isinstance(data.get("user"), dict) else data
        )
        if not user:
            raise ProfileCollectionError("not_found", "TikTok profile not found", retry_hours=24 * 30)
        return {
            "platform": "tiktok",
            "username": username,
            "display_name": user.get("display_name"),
            "profile_url": canonical_profile_url("tiktok", username),
            "avatar_url": user.get("avatar_url"),
            "bio": user.get("bio_description"),
            "website": user.get("bio_url"),
            "verified": user.get("is_verified"),
            "follower_count": user.get("follower_count"),
            "following_count": user.get("following_count"),
            "content_count": user.get("video_count"),
            "likes_count": user.get("likes_count"),
            "_method": "tiktok_research_user_info",
        }

    def _collect_instagram_api(self, candidate: dict[str, Any]) -> dict[str, Any] | None:
        token = (
            os.environ.get("INSTAGRAM_GRAPH_TOKEN", "").strip()
            or os.environ.get("META_GRAPH_TOKEN", "").strip()
        )
        discovery_account = (
            os.environ.get("INSTAGRAM_BUSINESS_DISCOVERY_ACCOUNT_ID", "").strip()
            or os.environ.get("INSTAGRAM_IG_USER_ID", "").strip()
        )
        username = normalize_username("instagram", candidate.get("username"))
        if not token or not discovery_account or not username:
            return None
        version = (os.environ.get("INSTAGRAM_GRAPH_VERSION") or os.environ.get("FACEBOOK_GRAPH_VERSION") or "v23.0").strip().lstrip("/")
        fields = (
            f"business_discovery.username({username})"
            "{id,username,name,biography,website,followers_count,follows_count,"
            "media_count,profile_picture_url}"
        )
        response = self.session.get(
            f"https://graph.facebook.com/{version}/{quote(discovery_account)}",
            headers={"Accept": "application/json"},
            params={"fields": fields, "access_token": token},
            timeout=self.timeout_seconds,
        )
        payload = self._json_response(response, "instagram")
        user = payload.get("business_discovery")
        if not isinstance(user, dict) or not user:
            raise ProfileCollectionError(
                "not_available",
                "Instagram profile is unavailable through Business Discovery",
                retry_hours=24 * 7,
            )
        return {
            "platform": "instagram",
            "user_id": user.get("id"),
            "username": user.get("username") or username,
            "display_name": user.get("name"),
            "profile_url": canonical_profile_url("instagram", user.get("username") or username),
            "avatar_url": user.get("profile_picture_url"),
            "bio": user.get("biography"),
            "website": user.get("website"),
            "follower_count": user.get("followers_count"),
            "following_count": user.get("follows_count"),
            "content_count": user.get("media_count"),
            "account_type": "professional_public_profile",
            "_method": "instagram_graph_business_discovery",
        }

    def _collect_facebook_api(self, candidate: dict[str, Any]) -> dict[str, Any] | None:
        token = (
            os.environ.get("FACEBOOK_GRAPH_TOKEN", "").strip()
            or os.environ.get("FB_GRAPH_TOKEN", "").strip()
            or os.environ.get("META_GRAPH_TOKEN", "").strip()
        )
        identifier = (
            normalize_user_id("facebook", candidate.get("user_id"))
            or normalize_username("facebook", candidate.get("username"))
        )
        if not token or not identifier:
            return None
        version = (os.environ.get("FACEBOOK_GRAPH_VERSION") or "v23.0").strip().lstrip("/")
        fields = (
            "id,name,username,about,description,category,fan_count,followers_count,"
            "location,website,picture.type(large)"
        )
        response = self.session.get(
            f"https://graph.facebook.com/{version}/{quote(identifier)}",
            headers={"Accept": "application/json"},
            params={"fields": fields, "access_token": token},
            timeout=self.timeout_seconds,
        )
        page = self._json_response(response, "facebook")
        if not page or not page.get("id"):
            raise ProfileCollectionError(
                "not_available",
                "Facebook personal profiles are not available; only eligible Pages can be enriched",
                retry_hours=24 * 30,
            )
        picture: dict[str, Any] = (
            dict(page["picture"]) if isinstance(page.get("picture"), dict) else {}
        )
        picture_data: dict[str, Any] = (
            dict(picture["data"]) if isinstance(picture.get("data"), dict) else {}
        )
        return {
            "platform": "facebook",
            "user_id": page.get("id"),
            "username": page.get("username") or candidate.get("username"),
            "display_name": page.get("name"),
            "profile_url": canonical_profile_url(
                "facebook", page.get("username") or candidate.get("username"), page.get("id")
            ),
            "avatar_url": picture_data.get("url"),
            "bio": page.get("about") or page.get("description"),
            "website": page.get("website"),
            "category": page.get("category"),
            "follower_count": page.get("followers_count") or page.get("fan_count"),
            "location": page.get("location"),
            "account_type": "facebook_page",
            "_method": "facebook_graph_page_lookup",
        }

    def _collect_official_sync(self, candidate: dict[str, Any]) -> dict[str, Any] | None:
        platform = normalize_platform(candidate.get("platform"))
        methods = {
            "x": self._collect_x_api,
            "youtube": self._collect_youtube_api,
            "tiktok": self._collect_tiktok_api,
            "instagram": self._collect_instagram_api,
            "facebook": self._collect_facebook_api,
        }
        method = methods.get(platform)
        return method(candidate) if method else None

    async def _ensure_browser(self) -> None:
        if self._context is not None:
            return
        if not self.browser_fallback or not self.cdp_url:
            raise ProfileCollectionError(
                "credentials_required",
                "No official profile API credentials or social-browser CDP session is available",
                retry_hours=24,
            )
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        try:
            self._browser = await self._playwright.chromium.connect_over_cdp(self.cdp_url)
        except Exception as exc:
            raise ProfileCollectionError(
                "browser_unavailable",
                f"Could not connect to the social browser: {exc}",
                retry_hours=1,
            ) from exc
        contexts = self._browser.contexts
        if contexts:
            self._context = contexts[0]
        else:
            self._context = await self._browser.new_context()
            self._owns_context = True

    async def _embedded_profile_payloads(
        self,
        page: Any,
        platform: str,
        candidate: dict[str, Any],
    ) -> list[Any]:
        settings = {
            "tiktok": {
                "keys": ["userInfo", "UserModule"],
                "script_ids": ["__UNIVERSAL_DATA_FOR_REHYDRATION__", "SIGI_STATE"],
                "globals": ["__UNIVERSAL_DATA_FOR_REHYDRATION__", "SIGI_STATE"],
                "json_ld": False,
                "json_scripts": False,
            },
            "youtube": {
                "keys": [
                    "channelMetadataRenderer",
                    "c4TabbedHeaderRenderer",
                    "pageHeaderViewModel",
                    "aboutChannelViewModel",
                    "microformatDataRenderer",
                ],
                "script_ids": [],
                "globals": ["ytInitialData"],
                "json_ld": False,
                "json_scripts": False,
            },
            "facebook": {
                "keys": [],
                "script_ids": [],
                "globals": [],
                "json_ld": True,
                "json_scripts": True,
            },
        }.get(platform)
        if not settings:
            return []
        settings["identity"] = text_value(
            candidate.get("user_id") or candidate.get("username")
        )
        settings["shape_diagnostics"] = os.environ.get(
            "PROFILE_API_SHAPE_DIAGNOSTICS", ""
        ).strip().casefold() in {"1", "true", "yes", "on"}
        try:
            payloads = await page.evaluate(
                """
                ({
                  keys,
                  script_ids: scriptIds,
                  globals: globalNames,
                  json_ld: includeJsonLd,
                  json_scripts: includeJsonScripts,
                  identity,
                  shape_diagnostics: shapeDiagnostics
                }) => {
                  const roots = [];
                  for (const name of globalNames) {
                    if (window[name] && typeof window[name] === "object") roots.push(window[name]);
                  }
                  for (const id of scriptIds) {
                    const text = document.getElementById(id)?.textContent || "";
                    if (!text) continue;
                    try { roots.push(JSON.parse(text)); } catch (_) {}
                  }
                  if (includeJsonLd) {
                    for (const script of document.querySelectorAll('script[type="application/ld+json"]')) {
                      try { roots.push(JSON.parse(script.textContent || "")); } catch (_) {}
                    }
                  }
                  if (includeJsonScripts) {
                    const selectors = [
                      'script[type="application/json"]',
                      'script[data-sjs]'
                    ];
                    for (const script of document.querySelectorAll(selectors.join(","))) {
                      const text = script.textContent || "";
                      if (!text || text.length > 3000000) continue;
                      if (identity && !text.includes(identity)) continue;
                      try { roots.push(JSON.parse(text)); } catch (_) {}
                    }
                  }
                  if (!keys.length && identity) {
                    const output = [];
                    const seen = new WeakSet();
                    const emitted = new WeakSet();
                    let visitedCount = 0;
                    const emit = (value) => {
                      if (!value || typeof value !== "object" || emitted.has(value) || output.length >= 24) return;
                      emitted.add(value);
                      output.push(value);
                    };
                    const visit = (value, depth, parents) => {
                      visitedCount += 1;
                      if (
                        !value
                        || typeof value !== "object"
                        || depth > 30
                        || output.length >= 24
                        || visitedCount > 75000
                      ) return;
                      if (seen.has(value)) return;
                      seen.add(value);
                      if (Array.isArray(value)) {
                        for (const item of value) visit(item, depth + 1, parents);
                        return;
                      }
                      const directMatch = Object.values(value).some((item) => {
                        if (typeof item !== "string" && typeof item !== "number") return false;
                        const text = String(item);
                        return text === identity || text.includes("/" + identity);
                      });
                      if (directMatch) {
                        if (shapeDiagnostics && parents.length) emit(parents[parents.length - 1]);
                        emit(value);
                      }
                      const nextParents = [...parents.slice(-1), value];
                      for (const child of Object.values(value)) visit(child, depth + 1, nextParents);
                    };
                    for (const root of roots) visit(root, 0, []);
                    return output;
                  }
                  if (!keys.length) return roots.slice(0, 12);
                  const wanted = new Set(keys);
                  const output = [];
                  const seen = new WeakSet();
                  const visit = (value, depth) => {
                    if (!value || typeof value !== "object" || depth > 24 || output.length >= 24) return;
                    if (seen.has(value)) return;
                    seen.add(value);
                    if (Array.isArray(value)) {
                      for (const item of value) visit(item, depth + 1);
                      return;
                    }
                    for (const [key, child] of Object.entries(value)) {
                      if (wanted.has(key) && child && typeof child === "object") {
                        output.push({[key]: child});
                        if (output.length >= 24) return;
                      }
                      visit(child, depth + 1);
                    }
                  };
                  for (const root of roots) visit(root, 0);
                  return output;
                }
                """,
                settings,
            )
        except Exception:
            return []
        return payloads if isinstance(payloads, list) else []

    async def _capture_profile_response(
        self,
        response: Any,
        *,
        candidate: dict[str, Any],
        records: list[dict[str, Any]],
    ) -> None:
        try:
            raw = await response.text()
        except Exception:
            return
        if len(raw) > 5_000_000:
            self.last_diagnostics.append(
                {
                    "event": "profile_response",
                    "path": urlparse(response.url).path,
                    "status": response.status,
                    "skipped": "response_too_large",
                }
            )
            return
        decoded_count = 0
        matched_methods: list[str] = []
        for payload in decode_json_payloads(raw):
            decoded_count += 1
            record = parse_profile_payload(
                text_value(candidate.get("platform")),
                payload,
                expected_username=text_value(candidate.get("username")),
                expected_user_id=text_value(candidate.get("user_id")),
            )
            if record:
                records.append(record)
                method = text_value(record.get("_method"))
                if method and method not in matched_methods:
                    matched_methods.append(method)
        self.last_diagnostics.append(
            {
                "event": "profile_response",
                "path": urlparse(response.url).path,
                "status": response.status,
                "decoded_payloads": decoded_count,
                "matched_methods": matched_methods,
            }
        )

    @staticmethod
    async def _first_text(page: Any, selectors: Iterable[str]) -> str:
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if await locator.count():
                    value = text_value(await locator.inner_text(timeout=1500))
                    if value:
                        return value
            except Exception:
                continue
        return ""

    @staticmethod
    async def _first_attr(page: Any, selectors: Iterable[str], attribute: str) -> str:
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if await locator.count():
                    value = text_value(await locator.get_attribute(attribute, timeout=1500))
                    if value:
                        return value
            except Exception:
                continue
        return ""

    async def _browser_tiktok(self, page: Any, candidate: dict[str, Any]) -> dict[str, Any]:
        return {
            "platform": "tiktok",
            "user_id": candidate.get("user_id"),
            "username": candidate.get("username"),
            "display_name": await self._first_text(page, ('[data-e2e="user-title"]', "h1")),
            "bio": await self._first_text(page, ('[data-e2e="user-bio"]',)),
            "avatar_url": await self._first_attr(
                page, ('[data-e2e="user-avatar"] img', "header img"), "src"
            ),
            "follower_count": await self._first_text(page, ('[data-e2e="followers-count"]',)),
            "following_count": await self._first_text(page, ('[data-e2e="following-count"]',)),
            "likes_count": await self._first_text(page, ('[data-e2e="likes-count"]',)),
            "_method": "tiktok_profile_dom",
        }

    async def _browser_instagram(self, page: Any, candidate: dict[str, Any]) -> dict[str, Any]:
        username = normalize_username("instagram", candidate.get("username"))
        try:
            payload = await page.evaluate(
                """
                async (username) => {
                  const controller = new AbortController();
                  const timer = setTimeout(() => controller.abort(), 10000);
                  try {
                    const response = await fetch(
                      "https://www.instagram.com/api/v1/users/web_profile_info/?username="
                        + encodeURIComponent(username),
                      {
                        credentials: "include",
                        signal: controller.signal,
                        headers: {
                          "accept": "application/json, text/plain, */*",
                          "x-asbd-id": "129477",
                          "x-ig-app-id": "936619743392459",
                          "x-requested-with": "XMLHttpRequest"
                        }
                      }
                    );
                    if (!response.ok) return {};
                    return await response.json();
                  } catch (_) {
                    return {};
                  } finally {
                    clearTimeout(timer);
                  }
                }
                """,
                username,
            )
        except Exception:
            payload = {}
        api_record = parse_profile_payload(
            "instagram",
            payload,
            expected_username=username,
            expected_user_id=text_value(candidate.get("user_id")),
        )
        if api_record:
            return api_record
        return {
            "platform": "instagram",
            "user_id": candidate.get("user_id"),
            "username": username,
            "display_name": await self._first_text(page, ("header h1", "header h2")),
            "bio": await self._first_text(page, ("header section", "header")),
            "avatar_url": await self._first_attr(page, ("header img",), "src"),
            "_method": "instagram_profile_dom",
        }

    async def _browser_x(self, page: Any, candidate: dict[str, Any]) -> dict[str, Any]:
        return {
            "platform": "x",
            "user_id": candidate.get("user_id"),
            "username": candidate.get("username"),
            "display_name": await self._first_text(page, ('[data-testid="UserName"]',)),
            "bio": await self._first_text(page, ('[data-testid="UserDescription"]',)),
            "location": await self._first_text(page, ('[data-testid="UserLocation"]',)),
            "website": await self._first_text(page, ('[data-testid="UserUrl"]',)),
            "avatar_url": await self._first_attr(
                page, ('[data-testid="UserAvatar-Container-unknown"] img', 'img[src*="profile_images"]'), "src"
            ),
            "geography_basis": "self_declared_profile_location",
            "_method": "x_profile_dom",
        }

    async def _browser_youtube(self, page: Any, candidate: dict[str, Any]) -> dict[str, Any]:
        return {
            "platform": "youtube",
            "user_id": candidate.get("user_id"),
            "username": candidate.get("username"),
            "display_name": await self._first_text(
                page, ("#channel-name", "yt-dynamic-text-view-model h1", "h1")
            ),
            "bio": await self._first_text(
                page, ("#description-container", "#description", "yt-formatted-string#description")
            ),
            "avatar_url": await self._first_attr(
                page, ("#avatar img", "yt-avatar-shape img"), "src"
            ),
            "follower_count": await self._first_text(
                page, ("#subscriber-count", "yt-content-metadata-view-model")
            ),
            "_method": "youtube_channel_dom",
        }

    async def _browser_facebook(self, page: Any, candidate: dict[str, Any]) -> dict[str, Any]:
        meta = await page.evaluate(
            """
            () => {
              const value = (selector) =>
                document.querySelector(selector)?.getAttribute("content") || "";
              return {
                title: value('meta[property="og:title"]') || document.title || "",
                description: value('meta[property="og:description"]')
                  || value('meta[name="description"]') || "",
                image: value('meta[property="og:image"]') || ""
              };
            }
            """
        )
        return {
            "platform": "facebook",
            "user_id": candidate.get("user_id"),
            "username": candidate.get("username"),
            "display_name": meta.get("title") if isinstance(meta, dict) else "",
            "bio": meta.get("description") if isinstance(meta, dict) else "",
            "avatar_url": meta.get("image") if isinstance(meta, dict) else "",
            "account_type": "facebook_public_page_or_profile",
            "_method": "facebook_profile_dom",
        }

    async def _browser_facebook_about(
        self,
        page: Any,
        candidate: dict[str, Any],
        *,
        section: str,
    ) -> dict[str, Any]:
        try:
            blocks = await page.evaluate(
                """
                () => {
                  const root = document.querySelector('[role="main"]')
                    || document.querySelector("main")
                    || document.body;
                  if (!root) return [];
                  const output = [];
                  const seen = new Set();
                  const add = (value) => {
                    const text = String(value || "").trim();
                    if (!text || text.length > 50000 || seen.has(text)) return;
                    seen.add(text);
                    output.push(text);
                  };
                  for (const element of root.querySelectorAll('[role="listitem"], li')) {
                    const text = element.innerText || "";
                    if (text.length <= 800) add(text);
                    if (output.length >= 250) break;
                  }
                  add((root.innerText || "").slice(0, 50000));
                  return output;
                }
                """
            )
        except Exception:
            return {}
        return parse_facebook_about_text_blocks(
            blocks if isinstance(blocks, list) else [],
            section=section,
            expected_username=text_value(candidate.get("username")),
            expected_user_id=text_value(candidate.get("user_id")),
        )

    async def _collect_browser(self, candidate: dict[str, Any]) -> dict[str, Any]:
        await self._ensure_browser()
        platform = normalize_platform(candidate.get("platform"))
        url = text_value(candidate.get("profile_url")) or canonical_profile_url(
            platform, candidate.get("username"), candidate.get("user_id")
        )
        if not url:
            raise ProfileCollectionError("invalid_identity", "Profile URL cannot be built", retry_hours=24 * 30)
        page = await self._context.new_page()
        captured_records: list[dict[str, Any]] = []
        capture_tasks: set[asyncio.Task[Any]] = set()
        capture_count = 0
        capture_limit = 80 if platform == "facebook" else 30

        def schedule_capture(response: Any) -> None:
            nonlocal capture_count
            if (
                capture_count >= capture_limit
                or not profile_response_url_matches(platform, response.url)
            ):
                return
            capture_count += 1
            task = asyncio.create_task(
                self._capture_profile_response(
                    response,
                    candidate=candidate,
                    records=captured_records,
                )
            )
            capture_tasks.add(task)
            task.add_done_callback(capture_tasks.discard)

        page.on("response", schedule_capture)
        try:
            response = await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=int(self.timeout_seconds * 1000),
            )
            if response is not None and response.status == 404:
                raise ProfileCollectionError("not_found", f"{platform} profile not found", retry_hours=24 * 30)
            await page.wait_for_timeout(1200)
            readiness_selectors = {
                "x": '[data-testid="UserName"]',
                "instagram": "header",
                "youtube": "#channel-name, yt-page-header-renderer, yt-dynamic-text-view-model",
            }
            readiness_selector = readiness_selectors.get(platform)
            if readiness_selector:
                try:
                    await page.locator(readiness_selector).first.wait_for(
                        state="visible",
                        timeout=min(
                            7_000,
                            max(1_500, int(self.timeout_seconds * 1000)),
                        ),
                    )
                except Exception:
                    self.last_diagnostics.append(
                        {
                            "event": "profile_readiness_timeout",
                            "platform": platform,
                            "selector": readiness_selector,
                        }
                    )
            self.last_diagnostics.append(
                {
                    "event": "profile_page",
                    "requested_path": urlparse(url).path,
                    "final_path": urlparse(page.url).path,
                    "status": response.status if response is not None else None,
                    "title": (await page.title())[:160],
                }
            )
            methods = {
                "tiktok": self._browser_tiktok,
                "instagram": self._browser_instagram,
                "x": self._browser_x,
                "youtube": self._browser_youtube,
                "facebook": self._browser_facebook,
            }
            method = methods.get(platform)
            if not method:
                raise ProfileCollectionError("unsupported", f"Unsupported platform: {platform}", retry_hours=24 * 30)
            page_record = await method(page, candidate)
            embedded_records: list[dict[str, Any]] = []
            section_records: list[dict[str, Any]] = []
            shape_event_count = 0
            shape_diagnostics = os.environ.get(
                "PROFILE_API_SHAPE_DIAGNOSTICS", ""
            ).strip().casefold() in {"1", "true", "yes", "on"}

            async def collect_embedded_records(
                timeout_seconds: float,
                context: str,
            ) -> None:
                nonlocal shape_event_count
                try:
                    payloads = await asyncio.wait_for(
                        self._embedded_profile_payloads(
                            page,
                            platform,
                            candidate,
                        ),
                        timeout=max(1.0, timeout_seconds),
                    )
                except asyncio.TimeoutError:
                    self.last_diagnostics.append(
                        {
                            "event": "embedded_profile_timeout",
                            "context": context,
                            "timeout_seconds": timeout_seconds,
                        }
                    )
                    return
                for payload in payloads:
                    if (
                        platform == "facebook"
                        and shape_diagnostics
                        and shape_event_count < 3
                    ):
                        hints = profile_payload_shape_hints(
                            payload,
                            candidate.get("user_id") or candidate.get("username"),
                        )
                        if hints:
                            self.last_diagnostics.append(
                                {
                                    "event": "embedded_profile_shape",
                                    "hints": hints[:10],
                                }
                            )
                            shape_event_count += 1
                    embedded = parse_profile_payload(
                        platform,
                        payload,
                        expected_username=text_value(candidate.get("username")),
                        expected_user_id=text_value(candidate.get("user_id")),
                    )
                    if embedded:
                        embedded_records.append(embedded)

            await collect_embedded_records(12, "profile")
            if platform == "facebook":
                initial_about_record = await self._browser_facebook_about(
                    page,
                    candidate,
                    section="about",
                )
                if initial_about_record:
                    section_records.append(initial_about_record)
                identifier = (
                    normalize_username("facebook", candidate.get("username"))
                    or normalize_user_id("facebook", candidate.get("user_id"))
                )
                if identifier:
                    base_url = f"https://www.facebook.com/{quote(identifier)}"
                    for section in (
                        "about_places",
                        "about_contact_and_basic_info",
                    ):
                        section_url = f"{base_url}/{section}"
                        try:
                            section_response = await page.goto(
                                section_url,
                                wait_until="domcontentloaded",
                                timeout=min(
                                    8_000,
                                    int(self.timeout_seconds * 1000),
                                ),
                            )
                            await page.wait_for_timeout(900)
                        except Exception as exc:
                            self.last_diagnostics.append(
                                {
                                    "event": "profile_section_page",
                                    "requested_path": urlparse(section_url).path,
                                    "error": text_value(exc)[:300],
                                }
                            )
                            continue
                        self.last_diagnostics.append(
                            {
                                "event": "profile_section_page",
                                "requested_path": urlparse(section_url).path,
                                "final_path": urlparse(page.url).path,
                                "status": (
                                    section_response.status
                                    if section_response is not None
                                    else None
                                ),
                                "title": (await page.title())[:160],
                            }
                        )
                        await collect_embedded_records(6, section)
                        section_record = await self._browser_facebook_about(
                            page,
                            candidate,
                            section=section,
                        )
                        if section_record:
                            section_records.append(section_record)
            await page.wait_for_timeout(300)
            if capture_tasks:
                await asyncio.gather(*list(capture_tasks), return_exceptions=True)

            page_method = text_value(page_record.get("_method"))
            page_is_api = any(
                marker in page_method
                for marker in ("_api", "_graphql", "_innertube", "_user_detail")
            )
            ordered_records = [
                *captured_records,
                *([page_record] if page_is_api else []),
                *embedded_records,
                *section_records,
                *([] if page_is_api else [page_record]),
            ]
            record = merge_profile_records(ordered_records)
            self.last_diagnostics.append(
                {
                    "event": "profile_merge",
                    "captured_records": len(captured_records),
                    "embedded_records": len(embedded_records),
                    "section_records": len(section_records),
                    "page_method": page_method,
                    "selected_method": text_value(record.get("_method")),
                }
            )
            record["profile_url"] = url
            return record
        except ProfileCollectionError:
            raise
        except Exception as exc:
            raise ProfileCollectionError(
                "retry",
                f"{platform} browser profile collection failed: {exc}",
                retry_hours=2,
            ) from exc
        finally:
            await page.close()

    async def collect(self, candidate: dict[str, Any]) -> dict[str, Any]:
        self.last_diagnostics = []
        platform = normalize_platform(candidate.get("platform"))
        api_error: ProfileCollectionError | None = None
        official = None
        try:
            official = await asyncio.to_thread(self._collect_official_sync, candidate)
        except ProfileCollectionError as exc:
            api_error = exc
        if official:
            method = text_value(official.pop("_method", "official_api"))
            return normalize_profile_record(
                official,
                platform=platform,
                source="official_api",
                collection_method=method,
            )
        if self.browser_fallback:
            browser_record = await self._collect_browser(candidate)
            method = text_value(browser_record.pop("_method", "browser_profile"))
            source = (
                "browser_session_api"
                if any(
                    marker in method
                    for marker in ("_api", "_graphql", "_innertube", "_user_detail")
                )
                else "public_profile_page"
            )
            profile = normalize_profile_record(
                browser_record,
                platform=platform,
                source=source,
                collection_method=method,
            )
            if profile_has_public_data(profile):
                return profile
        if api_error:
            raise api_error
        raise ProfileCollectionError(
            "credentials_required",
            (
                f"No usable {platform} profile API credentials are configured and "
                "no social-browser fallback is available"
            ),
            retry_hours=24,
        )


def pending_profile_candidates(
    conn: sqlite3.Connection,
    project: str,
    *,
    limit: int = DEFAULT_PROFILE_LIMIT,
    platforms: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    init_profile_schema(conn)
    now_value = _iso(_now())
    normalized_platforms = [
        normalize_platform(platform)
        for platform in (platforms or [])
        if normalize_platform(platform)
    ]
    where_platform = ""
    params: list[Any] = [project, now_value]
    if normalized_platforms:
        placeholders = ",".join("?" for _ in normalized_platforms)
        where_platform = f" AND platform IN ({placeholders})"
        params.extend(normalized_platforms)
    params.append(max(0, int(limit)))
    rows = conn.execute(
        f"""
        SELECT *
        FROM profile_enrichment_queue
        WHERE project = ?
          AND status NOT IN ('not_found', 'private', 'unsupported', 'invalid_identity')
          AND (next_attempt_at = '' OR next_attempt_at <= ?)
          {where_platform}
        ORDER BY priority DESC, last_seen_at DESC, profile_key
        LIMIT ?
        """,
        params,
    ).fetchall()
    columns = [description[0] for description in conn.execute(
        "SELECT * FROM profile_enrichment_queue LIMIT 0"
    ).description]
    return [dict(zip(columns, row)) for row in rows]


def _mark_queue_error(
    conn: sqlite3.Connection,
    *,
    candidate: dict[str, Any],
    status: str,
    error: str,
    retry_hours: float,
) -> None:
    conn.execute(
        """
        UPDATE profile_enrichment_queue
        SET status = ?, last_attempt_at = ?, next_attempt_at = ?,
            last_error = ?
        WHERE project = ? AND platform = ? AND profile_key = ?
        """,
        (
            status,
            _iso(_now()),
            _future_iso(hours=max(1, retry_hours)),
            error[:1000],
            candidate["project"],
            candidate["platform"],
            candidate["profile_key"],
        ),
    )


async def enrich_queued_profiles(
    conn: sqlite3.Connection,
    project: str,
    *,
    limit: int = DEFAULT_PROFILE_LIMIT,
    cache_days: int = DEFAULT_CACHE_DAYS,
    platforms: Iterable[str] | None = None,
    cdp_url: str = "",
    browser_fallback: bool = True,
    collector: Any = None,
) -> dict[str, Any]:
    candidates = pending_profile_candidates(
        conn,
        project,
        limit=limit,
        platforms=platforms,
    )
    summary: dict[str, Any] = {
        "status": "complete",
        "project": project,
        "requested": max(0, int(limit)),
        "selected": len(candidates),
        "enriched": 0,
        "status_counts": {},
        "errors": [],
    }
    try:
        profile_timeout_seconds = max(
            15.0,
            float(
                os.environ.get(
                    "PROFILE_ENRICHMENT_TIMEOUT_SECONDS",
                    DEFAULT_PROFILE_TIMEOUT_SECONDS,
                )
            ),
        )
    except (TypeError, ValueError):
        profile_timeout_seconds = float(DEFAULT_PROFILE_TIMEOUT_SECONDS)
    summary["profile_timeout_seconds"] = profile_timeout_seconds
    active_collector = collector or PublicProfileCollector(
        cdp_url=cdp_url,
        browser_fallback=browser_fallback,
    )
    async with active_collector:
        for candidate in candidates:
            conn.execute(
                """
                UPDATE profile_enrichment_queue
                SET status = 'running', last_attempt_at = ?,
                    attempt_count = attempt_count + 1
                WHERE project = ? AND platform = ? AND profile_key = ?
                """,
                (
                    _iso(_now()),
                    candidate["project"],
                    candidate["platform"],
                    candidate["profile_key"],
                ),
            )
            conn.commit()
            try:
                profile = await asyncio.wait_for(
                    active_collector.collect(candidate),
                    timeout=profile_timeout_seconds,
                )
                # Keep the queue's canonical key when an API reveals a stronger
                # ID than was available at first sight. Future observations are
                # matched by the newly stored ID and reuse this same key.
                profile["profile_key"] = candidate["profile_key"]
                profile["platform"] = candidate["platform"]
                upsert_profile(
                    conn,
                    project=project,
                    profile=profile,
                    cache_days=cache_days,
                    mark_queue_complete=True,
                )
                status = "complete"
                summary["enriched"] += 1
            except asyncio.TimeoutError:
                status = "retry"
                message = (
                    f"Profile collection timed out after "
                    f"{profile_timeout_seconds:g} seconds"
                )
                _mark_queue_error(
                    conn,
                    candidate=candidate,
                    status=status,
                    error=message,
                    retry_hours=2,
                )
                summary["errors"].append(
                    {
                        "platform": candidate["platform"],
                        "profile_key": candidate["profile_key"],
                        "status": status,
                        "error": message,
                    }
                )
            except ProfileCollectionError as exc:
                status = exc.status
                _mark_queue_error(
                    conn,
                    candidate=candidate,
                    status=status,
                    error=str(exc),
                    retry_hours=exc.retry_hours,
                )
                summary["errors"].append(
                    {
                        "platform": candidate["platform"],
                        "profile_key": candidate["profile_key"],
                        "status": status,
                        "error": str(exc),
                    }
                )
            except Exception as exc:
                status = "retry"
                attempts = int(candidate.get("attempt_count") or 0) + 1
                _mark_queue_error(
                    conn,
                    candidate=candidate,
                    status=status,
                    error=str(exc),
                    retry_hours=min(24 * 7, 2 ** min(attempts, 7)),
                )
                summary["errors"].append(
                    {
                        "platform": candidate["platform"],
                        "profile_key": candidate["profile_key"],
                        "status": status,
                        "error": str(exc),
                    }
                )
            summary["status_counts"][status] = summary["status_counts"].get(status, 0) + 1
            conn.commit()
    if summary["errors"] and not summary["enriched"]:
        summary["status"] = "completed_without_enrichment"
    return summary
