#!/usr/bin/env python
"""Project-level incremental scraper wrapper.

This script keeps the existing platform scrapers as batch workers, then ingests
their outputs into a project-level SQLite store. The store is the canonical
deduped source; compiled JSON/CSV reports are regenerated from it.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


COMMENTS_ROOT = Path("comments_data")
BROWSER_BACKED_PLATFORMS = {"tiktok", "instagram", "facebook", "x"}
COMBINED_COMMENT_FILE_BY_PLATFORM = {
    "youtube": "youtube_comments.json",
    "instagram": "instagram_comments.json",
    "facebook": "facebook_comments.json",
    "x": "x_comments.json",
}


def selected_platforms(args: argparse.Namespace, campaign: dict[str, Any] | None = None) -> list[str]:
    platforms = (
        ["youtube", "tiktok", "instagram", "facebook", "x"]
        if args.platform == "all"
        else [args.platform]
    )
    if campaign:
        platforms = [platform for platform in platforms if platform in campaign["platforms"]]
    return platforms


def social_browser_is_required(
    args: argparse.Namespace,
    campaign: dict[str, Any] | None = None,
) -> bool:
    if args.compile_only:
        return bool(args.profile_enrichment and args.profile_browser_fallback)
    platforms = selected_platforms(args, campaign)
    return bool(BROWSER_BACKED_PLATFORMS.intersection(platforms)) or bool(
        args.profile_enrichment and args.profile_browser_fallback
    )


def ensure_social_browser_session(args: argparse.Namespace) -> str:
    explicit_url = str(args.social_browser_cdp_url or "").strip()
    if explicit_url:
        return explicit_url

    state_path = Path(args.social_browser_state).resolve()
    controller = Path(__file__).resolve().with_name("social_browser.py")
    command = [
        sys.executable,
        str(controller),
        "start",
        "--runtime-dir",
        str(state_path.parent),
        "--startup-timeout",
        str(max(1.0, float(args.social_browser_startup_timeout))),
        "--no-open-tabs",
    ]
    print("[INFO] Ensuring the designated authenticated social browser...", flush=True)
    result = subprocess.run(command, cwd=Path(__file__).resolve().parent, check=False)
    if result.returncode != 0:
        raise SystemExit(
            "The designated social browser could not be started or verified. "
            "Check the Edge profile login state with: python social_browser.py status"
        )

    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Social-browser state is unavailable at {state_path}") from exc

    cdp_url = str(state.get("cdp_url") or "").strip()
    if not cdp_url:
        raise SystemExit(f"Social-browser state has no cdp_url: {state_path}")
    if state.get("profile_verified") is False:
        raise SystemExit(
            "The active social browser does not match the saved designated Edge profile."
        )
    profile_directory = str(state.get("profile_directory") or "designated profile")
    print(
        f"[INFO] Social browser ready: {profile_directory} at {cdp_url}",
        flush=True,
    )
    return cdp_url


def apply_shared_cdp_url(args: argparse.Namespace, cdp_url: str) -> None:
    if not cdp_url:
        return
    if not args.tiktok_user_data_dir and not args.tiktok_cdp_url:
        args.tiktok_cdp_url = cdp_url
    if not args.instagram_user_data_dir and not args.instagram_cdp_url:
        args.instagram_cdp_url = cdp_url
    if not args.facebook_user_data_dir and not args.facebook_cdp_url:
        args.facebook_cdp_url = cdp_url
    if not args.x_user_data_dir and not args.x_cdp_url:
        args.x_cdp_url = cdp_url


def is_transient_cdp_attach_failure(log_path: Path) -> bool:
    try:
        with log_path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 512 * 1024), os.SEEK_SET)
            text = handle.read().decode("utf-8", errors="replace").lower()
    except OSError:
        return False
    closed_driver = (
        "browsertype.connect_over_cdp" in text
        and "connection closed while reading from the driver" in text
    )
    shared_worker_collision = (
        "targetinfo:" in text
        and '"type": "shared_worker"' in text
        and '"attached": true' in text
    )
    return closed_driver or shared_worker_collision


def configured_timezone() -> dt.tzinfo:
    timezone_name = os.environ.get("SCRAPER_TIMEZONE", "").strip()
    if timezone_name:
        try:
            return ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            pass
    return dt.datetime.now().astimezone().tzinfo or dt.timezone.utc


def now_local() -> dt.datetime:
    return dt.datetime.now(tz=configured_timezone()).replace(microsecond=0)


def parse_stored_datetime(value: Any) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(text_value(value).replace("Z", "+00:00"))
    except ValueError:
        return dt.datetime.min.replace(tzinfo=configured_timezone())
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=configured_timezone())
    return parsed


def iso_now() -> str:
    return now_local().isoformat()


def slugify(value: str, prefix: str = "project") -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", value.strip()).strip("_").lower()
    return f"{prefix}_{slug or 'default'}"


def split_keywords(raw_value: str) -> list[str]:
    seen: set[str] = set()
    keywords: list[str] = []
    for part in re.split(r"[\n,;|]+", raw_value or ""):
        keyword = part.strip()
        key = keyword.casefold()
        if keyword and key not in seen:
            seen.add(key)
            keywords.append(keyword)
    return keywords


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def json_value(value: Any, default: Any) -> Any:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return default
    return default if parsed is None else parsed


def text_value(value: Any) -> str:
    return "" if value is None else str(value)


def max_x_post_id(records: Iterable[dict[str, Any]]) -> str:
    """Return the newest Snowflake ID represented by candidate records."""
    values: list[int] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        candidate = record.get("candidate") if isinstance(record.get("candidate"), dict) else record
        for raw in (record.get("content_key"), candidate.get("video_id"), candidate.get("id")):
            text = text_value(raw).strip()
            if text.isdigit():
                values.append(int(text))
        url = text_value(candidate.get("url") or candidate.get("video_url")).strip()
        match = re.search(r"/(?:i/web/)?status(?:es)?/(\d+)", url, re.I)
        if match:
            values.append(int(match.group(1)))
    return str(max(values)) if values else ""


def int_value(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


def count_value(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return max(0, int(value))
    text = text_value(value).strip().lower().replace(",", "")
    match = re.search(r"(-?\d+(?:\.\d+)?)\s*([kmb]?)", text)
    if not match:
        return default
    multiplier = {"": 1, "k": 1_000, "m": 1_000_000, "b": 1_000_000_000}[match.group(2)]
    return max(0, int(float(match.group(1)) * multiplier))


def bool_value(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return 1 if value else 0
    if isinstance(value, str):
        return 1 if value.strip().lower() in {"1", "true", "yes", "y", "on"} else 0
    return 0


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def stable_hash(*parts: Any) -> str:
    payload = "\x1f".join(text_value(part) for part in parts)
    return hashlib.sha1(payload.encode("utf-8", "replace")).hexdigest()


def atomic_json_dump(path: Path, payload: Any, *, compact: bool = False) -> None:
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        if compact:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def atomic_csv_write(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with tmp_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp_path, path)


def atomic_jsonl_write(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with tmp_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            json.dump(row, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
    os.replace(tmp_path, path)


def parse_unix_timestamp(value: Any) -> str:
    if isinstance(value, (int, float)) and value > 1_000_000_000:
        return dt.datetime.fromtimestamp(int(value)).isoformat()
    if isinstance(value, str) and value.isdigit() and int(value) > 1_000_000_000:
        return dt.datetime.fromtimestamp(int(value)).isoformat()
    return ""


def content_published_at(video: dict[str, Any], reference_time: Any = None) -> str:
    from tiktok_scraper.date_filter import parse_datetime

    reference = parse_datetime(reference_time) if reference_time else None
    fallback = ""
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
        value = video.get(key)
        parsed = parse_datetime(value, now=reference)
        if parsed:
            return parsed.isoformat()
        text = text_value(value).strip()
        if text and not fallback:
            fallback = text
    caption_date = parse_datetime(video.get("caption") or video.get("title"), now=reference)
    if caption_date:
        return caption_date.isoformat()
    return fallback


def nested_metric(video: dict[str, Any], *keys: str) -> int | None:
    containers = [video]
    for name in ("stats", "statistics", "metrics", "engagement"):
        value = video.get(name)
        if isinstance(value, dict):
            containers.append(value)
    for container in containers:
        for key in keys:
            if key in container and container.get(key) not in (None, ""):
                return count_value(container.get(key))
    return None


def comment_time(platform: str, comment: dict[str, Any], reference_time: Any = None) -> str:
    from tiktok_scraper.date_filter import parse_datetime

    evidence = comment.get("comment_evidence") if isinstance(comment.get("comment_evidence"), dict) else {}
    publication = evidence.get("publication") if isinstance(evidence.get("publication"), dict) else {}
    for value in (
        comment.get("comment_created_at"),
        publication.get("normalized_at"),
        comment.get("time"),
        comment.get("create_time"),
        comment.get("created_at"),
        comment.get("timestamp"),
    ):
        reference = parse_datetime(reference_time) if reference_time else None
        parsed = parse_datetime(value, now=reference)
        if parsed:
            return parsed.replace(microsecond=0).isoformat()
    return ""


def comment_author(comment: dict[str, Any]) -> str:
    user = comment.get("user")
    if isinstance(user, dict):
        return text_value(
            user.get("unique_id")
            or user.get("nickname")
            or user.get("username")
            or user.get("id")
        )
    return text_value(comment.get("author") or comment.get("username"))


def comment_author_id(comment: dict[str, Any]) -> str:
    user = comment.get("user")
    if isinstance(user, dict):
        return text_value(user.get("id") or user.get("uid") or user.get("sec_uid"))
    return text_value(comment.get("author_id"))


def comment_likes(comment: dict[str, Any]) -> int:
    for key in ("likes", "digg_count", "like_count"):
        value = comment.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    digits = "".join(ch for ch in text_value(comment.get("likes_text")) if ch.isdigit())
    return int(digits) if digits else 0


def iter_comments(comments: Any, parent_id: str = "") -> Iterable[tuple[dict[str, Any], str]]:
    for comment in as_list(comments):
        if not isinstance(comment, dict):
            continue
        yield comment, parent_id
        comment_id = text_value(comment.get("comment_id") or comment.get("id") or comment.get("cid"))
        next_parent = comment_id or parent_id
        yield from iter_comments(comment.get("replies"), next_parent)


def top_level_comment_count(comments: Iterable[dict[str, Any]], platform: str = "") -> int:
    rows = [comment for comment in comments if isinstance(comment, dict)]
    comment_ids = {
        text_value(comment.get("comment_id") or comment.get("id") or comment.get("cid"))
        for comment in rows
    }
    comment_ids.discard("")
    count = 0
    for comment in rows:
        parent_id = text_value(comment.get("parent_comment_id"))
        if parent_id:
            if parent_id not in comment_ids:
                count += 1
            continue
        if comment.get("is_reply") and platform not in {"x", "twitter"}:
            continue
        count += 1
    return count


def extract_shortcode(url: str) -> str:
    match = re.search(r"instagram\.com/(?:p|reel|tv)/([^/?#]+)/?", url or "")
    return match.group(1) if match else ""


def content_key(platform: str, video: dict[str, Any], fallback: str = "") -> str:
    from tiktok_scraper.known_content import candidate_content_keys

    keys = candidate_content_keys(platform, video)
    return text_value(keys[0] if keys else fallback)


def comment_key(platform: str, content_id: str, comment: dict[str, Any], parent_id: str) -> str:
    direct_id = text_value(comment.get("comment_id") or comment.get("id") or comment.get("cid"))
    if direct_id:
        return direct_id
    return stable_hash(
        platform,
        content_id,
        parent_id,
        comment_author(comment),
        comment.get("text"),
        comment.get("time") or comment.get("create_time"),
    )


def normalize_video(
    platform: str,
    video: dict[str, Any],
    index: int,
    reference_time: Any = None,
) -> dict[str, Any]:
    comments = as_list(video.get("comments"))
    return {
        "platform": platform,
        "video_number": video.get("video_number") or index,
        "video_id": text_value(video.get("video_id")),
        "media_id": text_value(video.get("media_id")),
        "url": text_value(video.get("url") or video.get("video_url")),
        "caption": text_value(video.get("caption") or video.get("title")),
        "description": text_value(video.get("description") or video.get("video_description")),
        "published_at": content_published_at(video, reference_time=reference_time),
        "content_type": text_value(video.get("content_type") or video.get("type") or video.get("youtube_renderer")),
        "view_count": nested_metric(video, "views", "view_count", "viewCount", "play_count", "playCount"),
        "like_count": nested_metric(video, "likes", "like_count", "likeCount", "digg_count", "diggCount"),
        "share_count": nested_metric(video, "shares", "share_count", "shareCount"),
        "save_count": nested_metric(video, "saves", "save_count", "saveCount", "collect_count", "collectCount"),
        "follower_count": nested_metric(video, "followers", "follower_count", "followerCount"),
        "transcript": text_value(video.get("transcript")),
        "transcript_available": bool_value(video.get("transcript_available")),
        "transcript_language": text_value(video.get("transcript_language")),
        "transcript_language_name": text_value(video.get("transcript_language_name")),
        "transcript_is_auto_generated": bool_value(video.get("transcript_is_auto_generated")),
        "transcript_segment_count": int_value(video.get("transcript_segment_count")),
        "transcript_segments": as_list(video.get("transcript_segments")),
        "transcript_error": text_value(video.get("transcript_error")),
        "transcript_status": text_value(video.get("transcript_status")),
        "transcript_source": text_value(video.get("transcript_source")),
        "transcript_attempt_count": int_value(video.get("transcript_attempt_count")),
        "subtitle_tracks": as_list(video.get("subtitle_tracks")),
        "subtitle_selected_track": (
            video.get("subtitle_selected_track")
            if isinstance(video.get("subtitle_selected_track"), dict)
            else {}
        ),
        "subtitle_no_caption_reason": text_value(video.get("subtitle_no_caption_reason")),
        "subtitle_manifest_source": text_value(video.get("subtitle_manifest_source")),
        "content_creator": text_value(
            video.get("content_creator")
            or video.get("username")
            or video.get("author")
            or video.get("creator")
        ),
        "reported_comment_count": nested_metric(video, "reported_comment_count", "comment_count", "commentCount"),
        "matched_keywords": as_list(video.get("matched_keywords")),
        "error": text_value(video.get("error")),
        "comments": comments,
    }


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS project_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY,
            project TEXT NOT NULL,
            platform TEXT NOT NULL,
            keyword TEXT NOT NULL,
            session_path TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            discovered_count INTEGER DEFAULT 0,
            new_content_count INTEGER DEFAULT 0,
            new_comment_count INTEGER DEFAULT 0,
            total_comment_count INTEGER DEFAULT 0,
            source_key TEXT DEFAULT '',
            source_kind TEXT DEFAULT '',
            source_value TEXT DEFAULT '',
            source_role TEXT DEFAULT '',
            error TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS platform_state (
            project TEXT NOT NULL,
            platform TEXT NOT NULL,
            keyword TEXT NOT NULL,
            last_successful_scrape_at TEXT,
            last_run_id TEXT,
            since_id TEXT DEFAULT '',
            total_content INTEGER DEFAULT 0,
            total_comments INTEGER DEFAULT 0,
            PRIMARY KEY (project, platform, keyword)
        );
        CREATE TABLE IF NOT EXISTS content_items (
            platform TEXT NOT NULL,
            content_key TEXT NOT NULL,
            project TEXT NOT NULL,
            keyword TEXT NOT NULL,
            video_id TEXT DEFAULT '',
            media_id TEXT DEFAULT '',
            url TEXT DEFAULT '',
            creator TEXT DEFAULT '',
            caption TEXT DEFAULT '',
            description TEXT DEFAULT '',
            published_at TEXT DEFAULT '',
            content_type TEXT DEFAULT '',
            view_count INTEGER DEFAULT 0,
            like_count INTEGER DEFAULT 0,
            share_count INTEGER DEFAULT 0,
            save_count INTEGER DEFAULT 0,
            follower_count INTEGER DEFAULT 0,
            reported_comment_count INTEGER DEFAULT 0,
            transcript TEXT DEFAULT '',
            transcript_available INTEGER DEFAULT 0,
            transcript_language TEXT DEFAULT '',
            transcript_language_name TEXT DEFAULT '',
            transcript_is_auto_generated INTEGER DEFAULT 0,
            transcript_segment_count INTEGER DEFAULT 0,
            transcript_segments_json TEXT DEFAULT '[]',
            transcript_error TEXT DEFAULT '',
            transcript_status TEXT DEFAULT '',
            transcript_source TEXT DEFAULT '',
            transcript_attempt_count INTEGER DEFAULT 0,
            subtitle_tracks_json TEXT DEFAULT '[]',
            subtitle_selected_track_json TEXT DEFAULT '{}',
            subtitle_no_caption_reason TEXT DEFAULT '',
            subtitle_manifest_source TEXT DEFAULT '',
            first_seen_at TEXT NOT NULL,
            last_scraped_at TEXT NOT NULL,
            matched_keywords_json TEXT DEFAULT '[]',
            error TEXT DEFAULT '',
            raw_json TEXT NOT NULL,
            PRIMARY KEY (platform, content_key)
        );
        CREATE TABLE IF NOT EXISTS comments (
            platform TEXT NOT NULL,
            content_key TEXT NOT NULL,
            comment_key TEXT NOT NULL,
            project TEXT NOT NULL,
            keyword TEXT NOT NULL,
            parent_comment_id TEXT DEFAULT '',
            author TEXT DEFAULT '',
            author_id TEXT DEFAULT '',
            text TEXT DEFAULT '',
            likes INTEGER DEFAULT 0,
            comment_time TEXT DEFAULT '',
            reply_count TEXT DEFAULT '',
            is_reply INTEGER DEFAULT 0,
            first_seen_at TEXT NOT NULL,
            scraped_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            raw_json TEXT NOT NULL,
            PRIMARY KEY (platform, content_key, comment_key)
        );
        CREATE INDEX IF NOT EXISTS idx_comments_project_scraped
            ON comments(project, scraped_at);
        CREATE INDEX IF NOT EXISTS idx_comments_project_platform_scraped
            ON comments(project, platform, scraped_at);
        CREATE INDEX IF NOT EXISTS idx_comments_project_platform_content_scraped
            ON comments(project, platform, content_key, scraped_at);
        CREATE INDEX IF NOT EXISTS idx_content_project_platform
            ON content_items(project, platform);
        CREATE INDEX IF NOT EXISTS idx_content_project_platform_key
            ON content_items(project, platform, content_key);
        CREATE TABLE IF NOT EXISTS content_metric_snapshots (
            project TEXT NOT NULL,
            platform TEXT NOT NULL,
            content_key TEXT NOT NULL,
            scraped_at TEXT NOT NULL,
            view_count INTEGER DEFAULT 0,
            like_count INTEGER DEFAULT 0,
            comment_count INTEGER DEFAULT 0,
            share_count INTEGER DEFAULT 0,
            save_count INTEGER DEFAULT 0,
            follower_count INTEGER DEFAULT 0,
            raw_json TEXT NOT NULL,
            PRIMARY KEY (project, platform, content_key, scraped_at)
        );
        CREATE INDEX IF NOT EXISTS idx_metric_snapshots_project_platform
            ON content_metric_snapshots(project, platform, scraped_at);
        CREATE TABLE IF NOT EXISTS discovery_sources (
            project TEXT NOT NULL,
            platform TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_kind TEXT NOT NULL,
            source_value TEXT NOT NULL,
            source_label TEXT DEFAULT '',
            source_role TEXT DEFAULT '',
            first_run_at TEXT NOT NULL,
            last_run_at TEXT NOT NULL,
            last_run_id TEXT DEFAULT '',
            last_status TEXT DEFAULT '',
            run_count INTEGER DEFAULT 0,
            candidate_count INTEGER DEFAULT 0,
            new_candidate_count INTEGER DEFAULT 0,
            error TEXT DEFAULT '',
            plan_digest TEXT DEFAULT '',
            planned_order INTEGER DEFAULT 0,
            active INTEGER DEFAULT 1,
            PRIMARY KEY (project, platform, source_key)
        );
        CREATE TABLE IF NOT EXISTS candidate_ledger (
            project TEXT NOT NULL,
            platform TEXT NOT NULL,
            content_key TEXT NOT NULL,
            video_id TEXT DEFAULT '',
            media_id TEXT DEFAULT '',
            url TEXT DEFAULT '',
            creator TEXT DEFAULT '',
            caption TEXT DEFAULT '',
            published_at TEXT DEFAULT '',
            content_type TEXT DEFAULT '',
            first_discovered_at TEXT NOT NULL,
            last_discovered_at TEXT NOT NULL,
            discovery_count INTEGER DEFAULT 0,
            source_keys_json TEXT DEFAULT '[]',
            last_decision TEXT DEFAULT '',
            last_score INTEGER DEFAULT 0,
            extraction_status TEXT DEFAULT '',
            raw_json TEXT NOT NULL,
            PRIMARY KEY (project, platform, content_key)
        );
        CREATE INDEX IF NOT EXISTS idx_candidate_ledger_project_platform
            ON candidate_ledger(project, platform, last_discovered_at);
        CREATE TABLE IF NOT EXISTS candidate_observations (
            observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            project TEXT NOT NULL,
            platform TEXT NOT NULL,
            source_key TEXT NOT NULL,
            content_key TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            decision TEXT DEFAULT '',
            score INTEGER DEFAULT 0,
            candidate_rank INTEGER DEFAULT 0,
            raw_json TEXT NOT NULL,
            UNIQUE(run_id, source_key, content_key)
        );
        CREATE INDEX IF NOT EXISTS idx_candidate_observations_project_source
            ON candidate_observations(project, platform, source_key, observed_at);
        """
    )
    content_columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(content_items)").fetchall()
    }
    for column, definition in {
        "transcript": "TEXT DEFAULT ''",
        "description": "TEXT DEFAULT ''",
        "transcript_available": "INTEGER DEFAULT 0",
        "transcript_language": "TEXT DEFAULT ''",
        "transcript_language_name": "TEXT DEFAULT ''",
        "transcript_is_auto_generated": "INTEGER DEFAULT 0",
        "transcript_segment_count": "INTEGER DEFAULT 0",
        "transcript_segments_json": "TEXT DEFAULT '[]'",
        "transcript_error": "TEXT DEFAULT ''",
        "transcript_status": "TEXT DEFAULT ''",
        "transcript_source": "TEXT DEFAULT ''",
        "transcript_attempt_count": "INTEGER DEFAULT 0",
        "subtitle_tracks_json": "TEXT DEFAULT '[]'",
        "subtitle_selected_track_json": "TEXT DEFAULT '{}'",
        "subtitle_no_caption_reason": "TEXT DEFAULT ''",
        "subtitle_manifest_source": "TEXT DEFAULT ''",
        "published_at": "TEXT DEFAULT ''",
        "content_type": "TEXT DEFAULT ''",
        "view_count": "INTEGER DEFAULT 0",
        "like_count": "INTEGER DEFAULT 0",
        "share_count": "INTEGER DEFAULT 0",
        "save_count": "INTEGER DEFAULT 0",
        "follower_count": "INTEGER DEFAULT 0",
        "reported_comment_count": "INTEGER DEFAULT 0",
    }.items():
        if column not in content_columns:
            conn.execute(f"ALTER TABLE content_items ADD COLUMN {column} {definition}")

    run_columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(runs)").fetchall()
    }
    for column, definition in {
        "source_key": "TEXT DEFAULT ''",
        "source_kind": "TEXT DEFAULT ''",
        "source_value": "TEXT DEFAULT ''",
        "source_role": "TEXT DEFAULT ''",
    }.items():
        if column not in run_columns:
            conn.execute(f"ALTER TABLE runs ADD COLUMN {column} {definition}")

    source_columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(discovery_sources)").fetchall()
    }
    for column, definition in {
        "plan_digest": "TEXT DEFAULT ''",
        "planned_order": "INTEGER DEFAULT 0",
        "active": "INTEGER DEFAULT 1",
    }.items():
        if column not in source_columns:
            conn.execute(f"ALTER TABLE discovery_sources ADD COLUMN {column} {definition}")

    platform_state_columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(platform_state)").fetchall()
    }
    if "since_id" not in platform_state_columns:
        conn.execute("ALTER TABLE platform_state ADD COLUMN since_id TEXT DEFAULT ''")

    comment_columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(comments)").fetchall()
    }
    if "last_seen_at" not in comment_columns:
        conn.execute("ALTER TABLE comments ADD COLUMN last_seen_at TEXT DEFAULT ''")
        conn.execute(
            "UPDATE comments SET last_seen_at = COALESCE(NULLIF(scraped_at, ''), first_seen_at) "
            "WHERE last_seen_at = '' OR last_seen_at IS NULL"
        )
    from tiktok_scraper.storage.google_drive import ensure_archive_schema
    from tiktok_scraper.analysis_workflow import ensure_analysis_schema
    from tiktok_scraper.profile_enrichment import init_profile_schema

    ensure_archive_schema(conn)
    ensure_analysis_schema(conn)
    init_profile_schema(conn)
    conn.commit()


def project_paths(project: str) -> dict[str, Path]:
    root = COMMENTS_ROOT / slugify(project)
    paths = {
        "root": root,
        "raw_runs": root / "raw_runs",
        "state": root / "state",
        "compiled": root / "compiled",
        "latest": root / "compiled" / "latest",
        "reports": root / "compiled" / "reports",
        "logs": root / "logs",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return paths


def incomplete_raw_run(paths: dict[str, Path], platform: str) -> Path | None:
    """Return the latest raw run if it has saved data but is not complete."""
    combined_name = COMBINED_COMMENT_FILE_BY_PLATFORM.get(platform)
    if not combined_name or not paths["raw_runs"].exists():
        return None

    raw_runs = sorted(
        (
            path
            for path in paths["raw_runs"].glob(f"{platform}_*")
            if path.is_dir()
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not raw_runs:
        return None

    latest = raw_runs[0]
    comments_dir = latest / "comments"
    combined_path = comments_dir / combined_name
    try:
        if combined_path.exists():
            payload = load_json(combined_path)
            if isinstance(payload, dict) and not payload.get("complete"):
                videos = as_list(payload.get("videos"))
                expected = int_value(payload.get("expected_posts") or payload.get("expected_videos"))
                completed = int_value(payload.get("completed_posts") or payload.get("completed_videos") or len(videos))
                if videos or expected or completed:
                    return latest
        elif any(comments_dir.glob("video_*_comments.json")):
            return latest
    except Exception:
        return latest
    return None


def db_path(project: str) -> Path:
    return project_paths(project)["state"] / "scrape_state.sqlite"


def write_campaign_state(spec: dict[str, Any], paths: dict[str, Path]) -> tuple[Path, Path]:
    from campaign_spec import campaign_spec_digest
    from tiktok_scraper.relevance import default_anchor_terms, extract_topic_tokens

    digest = campaign_spec_digest(spec)
    normalized_path = paths["state"] / f"campaign_spec_{digest[:12]}.json"
    if not normalized_path.exists():
        atomic_json_dump(normalized_path, {
            key: value for key, value in spec.items() if key != "source_path"
        })

    trusted_sources = []
    for accounts in spec.get("accounts", {}).values():
        for account in accounts:
            value = text_value(account.get("value"))
            if value:
                trusted_sources.append(value.rstrip("/").split("/")[-1].lstrip("@"))
    aliases = []
    seen_aliases = set()
    for values in spec["queries"].values():
        for value in values:
            key = value.casefold()
            if key not in seen_aliases:
                seen_aliases.add(key)
                aliases.append(value)
    ambiguous_terms = list(spec["relevance"].get("ambiguous_terms") or [])
    anchor_terms = list(spec["relevance"].get("anchor_terms") or [])
    if not anchor_terms:
        anchor_terms = default_anchor_terms(
            spec["keywords"]["core"],
            spec["keywords"]["hashtags"],
            aliases,
            ambiguous_terms,
        )
    profile = {
        "name": spec["name"],
        "core_terms": spec["keywords"]["core"],
        "anchor_terms": anchor_terms,
        "ambiguous_terms": ambiguous_terms,
        "token_terms": extract_topic_tokens(anchor_terms),
        "hashtags": spec["keywords"]["hashtags"],
        "aliases": aliases,
        "entities": spec["keywords"]["entities"],
        "activation_terms": spec["keywords"]["campaign"],
        "context_terms": spec["relevance"].get("context_terms") or [],
        "generic_terms": spec["keywords"]["optional"],
        "exclusion_terms": spec["keywords"]["exclusions"],
        "trusted_sources": trusted_sources,
        "require_anchor": bool(spec["relevance"].get("require_anchor", True)),
        "accept_threshold": spec["relevance"]["accept_threshold"],
        "review_threshold": spec["relevance"]["review_threshold"],
    }
    profile_path = paths["state"] / "campaign_relevance_profile.json"
    atomic_json_dump(profile_path, profile)
    return normalized_path, profile_path


def ingest_videos(
    conn: sqlite3.Connection,
    *,
    project: str,
    keyword: str,
    platform: str,
    videos: list[dict[str, Any]],
    run_id: str,
    scraped_at: str,
) -> dict[str, int]:
    from tiktok_scraper.profile_enrichment import (
        enqueue_comment_profile,
        enqueue_content_profile,
    )

    new_content = 0
    new_comments = 0
    flat_comments = 0
    queued_profiles: set[tuple[str, str]] = set()

    for index, raw_video in enumerate(videos, start=1):
        video = normalize_video(platform, raw_video, index, reference_time=scraped_at)
        creator_profile_key = enqueue_content_profile(
            conn,
            project=project,
            platform=platform,
            record=raw_video,
            observed_at=scraped_at,
        )
        if creator_profile_key:
            queued_profiles.add((platform, creator_profile_key))
        key = content_key(platform, raw_video, fallback=f"{run_id}:{index}")
        if not key:
            key = f"{run_id}:{index}"

        existing = conn.execute(
            "SELECT 1 FROM content_items WHERE platform = ? AND content_key = ?",
            (platform, key),
        ).fetchone()
        if not existing:
            new_content += 1

        conn.execute(
            """
            INSERT INTO content_items (
                platform, content_key, project, keyword, video_id, media_id, url,
                creator, caption, description, published_at, content_type,
                view_count, like_count, share_count, save_count, follower_count,
                reported_comment_count, transcript, transcript_available,
                transcript_language, transcript_language_name,
                transcript_is_auto_generated, transcript_segment_count,
                transcript_segments_json,
                transcript_error, transcript_status, transcript_source,
                transcript_attempt_count, subtitle_tracks_json,
                subtitle_selected_track_json, subtitle_no_caption_reason,
                subtitle_manifest_source, first_seen_at, last_scraped_at,
                matched_keywords_json, error, raw_json
            )
            VALUES (
                :platform, :content_key, :project, :keyword, :video_id, :media_id, :url,
                :creator, :caption, :description, :published_at, :content_type,
                :view_count, :like_count, :share_count, :save_count, :follower_count,
                :reported_comment_count, :transcript, :transcript_available,
                :transcript_language, :transcript_language_name,
                :transcript_is_auto_generated, :transcript_segment_count,
                :transcript_segments_json,
                :transcript_error, :transcript_status, :transcript_source,
                :transcript_attempt_count, :subtitle_tracks_json,
                :subtitle_selected_track_json, :subtitle_no_caption_reason,
                :subtitle_manifest_source, :first_seen_at, :last_scraped_at,
                :matched_keywords_json, :error, :raw_json
            )
            ON CONFLICT(platform, content_key) DO UPDATE SET
                keyword = excluded.keyword,
                video_id = COALESCE(NULLIF(excluded.video_id, ''), content_items.video_id),
                media_id = COALESCE(NULLIF(excluded.media_id, ''), content_items.media_id),
                url = COALESCE(NULLIF(excluded.url, ''), content_items.url),
                creator = COALESCE(NULLIF(excluded.creator, ''), content_items.creator),
                caption = COALESCE(NULLIF(excluded.caption, ''), content_items.caption),
                description = COALESCE(NULLIF(excluded.description, ''), content_items.description),
                published_at = COALESCE(NULLIF(excluded.published_at, ''), content_items.published_at),
                content_type = COALESCE(NULLIF(excluded.content_type, ''), content_items.content_type),
                view_count = CASE WHEN excluded.view_count > 0 THEN excluded.view_count ELSE content_items.view_count END,
                like_count = CASE WHEN excluded.like_count > 0 THEN excluded.like_count ELSE content_items.like_count END,
                share_count = CASE WHEN excluded.share_count > 0 THEN excluded.share_count ELSE content_items.share_count END,
                save_count = CASE WHEN excluded.save_count > 0 THEN excluded.save_count ELSE content_items.save_count END,
                follower_count = CASE WHEN excluded.follower_count > 0 THEN excluded.follower_count ELSE content_items.follower_count END,
                reported_comment_count = CASE
                    WHEN excluded.reported_comment_count > 0
                    THEN excluded.reported_comment_count
                    ELSE content_items.reported_comment_count
                END,
                transcript = COALESCE(NULLIF(excluded.transcript, ''), content_items.transcript),
                transcript_available = CASE
                    WHEN NULLIF(excluded.transcript, '') IS NOT NULL
                    THEN excluded.transcript_available
                    ELSE content_items.transcript_available
                END,
                transcript_language = COALESCE(NULLIF(excluded.transcript_language, ''), content_items.transcript_language),
                transcript_language_name = COALESCE(NULLIF(excluded.transcript_language_name, ''), content_items.transcript_language_name),
                transcript_is_auto_generated = CASE
                    WHEN NULLIF(excluded.transcript, '') IS NOT NULL
                    THEN excluded.transcript_is_auto_generated
                    ELSE content_items.transcript_is_auto_generated
                END,
                transcript_segment_count = COALESCE(NULLIF(excluded.transcript_segment_count, 0), content_items.transcript_segment_count),
                transcript_segments_json = CASE
                    WHEN excluded.transcript_segments_json <> '[]'
                    THEN excluded.transcript_segments_json
                    ELSE content_items.transcript_segments_json
                END,
                transcript_error = CASE
                    WHEN NULLIF(excluded.transcript, '') IS NOT NULL
                    THEN excluded.transcript_error
                    ELSE COALESCE(NULLIF(excluded.transcript_error, ''), content_items.transcript_error)
                END,
                transcript_status = COALESCE(NULLIF(excluded.transcript_status, ''), content_items.transcript_status),
                transcript_source = COALESCE(NULLIF(excluded.transcript_source, ''), content_items.transcript_source),
                transcript_attempt_count = CASE
                    WHEN excluded.transcript_attempt_count > 0
                    THEN excluded.transcript_attempt_count
                    ELSE content_items.transcript_attempt_count
                END,
                subtitle_tracks_json = CASE
                    WHEN excluded.subtitle_tracks_json <> '[]'
                    THEN excluded.subtitle_tracks_json
                    ELSE content_items.subtitle_tracks_json
                END,
                subtitle_selected_track_json = CASE
                    WHEN excluded.subtitle_selected_track_json <> '{}'
                    THEN excluded.subtitle_selected_track_json
                    ELSE content_items.subtitle_selected_track_json
                END,
                subtitle_no_caption_reason = COALESCE(NULLIF(excluded.subtitle_no_caption_reason, ''), content_items.subtitle_no_caption_reason),
                subtitle_manifest_source = COALESCE(NULLIF(excluded.subtitle_manifest_source, ''), content_items.subtitle_manifest_source),
                last_scraped_at = excluded.last_scraped_at,
                matched_keywords_json = excluded.matched_keywords_json,
                error = excluded.error,
                raw_json = excluded.raw_json
            """,
            {
                "platform": platform,
                "content_key": key,
                "project": project,
                "keyword": keyword,
                "video_id": video["video_id"],
                "media_id": video["media_id"],
                "url": video["url"],
                "creator": video["content_creator"],
                "caption": video["caption"],
                "description": video["description"],
                "published_at": video["published_at"],
                "content_type": video["content_type"],
                "view_count": video["view_count"],
                "like_count": video["like_count"],
                "share_count": video["share_count"],
                "save_count": video["save_count"],
                "follower_count": video["follower_count"],
                "reported_comment_count": video["reported_comment_count"],
                "transcript": video["transcript"],
                "transcript_available": video["transcript_available"],
                "transcript_language": video["transcript_language"],
                "transcript_language_name": video["transcript_language_name"],
                "transcript_is_auto_generated": video["transcript_is_auto_generated"],
                "transcript_segment_count": video["transcript_segment_count"],
                "transcript_segments_json": json.dumps(video["transcript_segments"], ensure_ascii=False),
                "transcript_error": video["transcript_error"],
                "transcript_status": video["transcript_status"],
                "transcript_source": video["transcript_source"],
                "transcript_attempt_count": video["transcript_attempt_count"],
                "subtitle_tracks_json": json.dumps(video["subtitle_tracks"], ensure_ascii=False),
                "subtitle_selected_track_json": json.dumps(video["subtitle_selected_track"], ensure_ascii=False),
                "subtitle_no_caption_reason": video["subtitle_no_caption_reason"],
                "subtitle_manifest_source": video["subtitle_manifest_source"],
                "first_seen_at": scraped_at,
                "last_scraped_at": scraped_at,
                "matched_keywords_json": json.dumps(video["matched_keywords"], ensure_ascii=False),
                "error": video["error"],
                "raw_json": json.dumps(raw_video, ensure_ascii=False),
            },
        )

        conn.execute(
            """
            INSERT OR REPLACE INTO content_metric_snapshots (
                project, platform, content_key, scraped_at, view_count,
                like_count, comment_count, share_count, save_count,
                follower_count, raw_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project,
                platform,
                key,
                scraped_at,
                video["view_count"],
                video["like_count"],
                video["reported_comment_count"] or len(video["comments"]),
                video["share_count"],
                video["save_count"],
                video["follower_count"],
                json.dumps({
                    "view_count": video["view_count"],
                    "like_count": video["like_count"],
                    "comment_count": video["reported_comment_count"] or len(video["comments"]),
                    "share_count": video["share_count"],
                    "save_count": video["save_count"],
                    "follower_count": video["follower_count"],
                }, ensure_ascii=False),
            ),
        )

        for comment, parent_id in iter_comments(video["comments"]):
            flat_comments += 1
            author_profile_key = enqueue_comment_profile(
                conn,
                project=project,
                platform=platform,
                record=comment,
                observed_at=scraped_at,
            )
            if author_profile_key:
                queued_profiles.add((platform, author_profile_key))
            ckey = comment_key(platform, key, comment, parent_id)
            inserted = conn.execute(
                """
                INSERT OR IGNORE INTO comments (
                    platform, content_key, comment_key, project, keyword,
                    parent_comment_id, author, author_id, text, likes,
                    comment_time, reply_count, is_reply, first_seen_at,
                    scraped_at, last_seen_at, raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    platform,
                    key,
                    ckey,
                    project,
                    keyword,
                    parent_id,
                    comment_author(comment),
                    comment_author_id(comment),
                    text_value(comment.get("text")),
                    comment_likes(comment),
                    comment_time(platform, comment, reference_time=scraped_at),
                    text_value(comment.get("reply_count")),
                    1 if parent_id or comment.get("is_reply") else 0,
                    scraped_at,
                    scraped_at,
                    scraped_at,
                    json.dumps(comment, ensure_ascii=False),
                ),
            ).rowcount
            if inserted:
                new_comments += 1
            else:
                conn.execute(
                    """
                    UPDATE comments SET
                        keyword = ?,
                        parent_comment_id = COALESCE(NULLIF(?, ''), parent_comment_id),
                        author = COALESCE(NULLIF(?, ''), author),
                        author_id = COALESCE(NULLIF(?, ''), author_id),
                        text = COALESCE(NULLIF(?, ''), text),
                        likes = MAX(likes, ?),
                        comment_time = COALESCE(NULLIF(?, ''), comment_time),
                        reply_count = COALESCE(NULLIF(?, ''), reply_count),
                        is_reply = ?,
                        last_seen_at = ?,
                        raw_json = ?
                    WHERE platform = ? AND content_key = ? AND comment_key = ?
                    """,
                    (
                        keyword,
                        parent_id,
                        comment_author(comment),
                        comment_author_id(comment),
                        text_value(comment.get("text")),
                        comment_likes(comment),
                        comment_time(platform, comment, reference_time=scraped_at),
                        text_value(comment.get("reply_count")),
                        1 if parent_id or comment.get("is_reply") else 0,
                        scraped_at,
                        json.dumps(comment, ensure_ascii=False),
                        platform,
                        key,
                        ckey,
                    ),
                )

    conn.commit()
    return {
        "discovered_count": len(videos),
        "new_content_count": new_content,
        "new_comment_count": new_comments,
        "total_comment_count": flat_comments,
        "queued_profile_count": len(queued_profiles),
    }


def sort_video_file(path: Path) -> int:
    for part in path.stem.split("_"):
        if part.isdigit():
            return int(part)
    return 0


def load_session_videos(session_path: Path, platform: str) -> list[dict[str, Any]]:
    comments_dir = session_path / "comments"
    if platform == "youtube":
        combined = comments_dir / "youtube_comments.json"
        if combined.exists():
            return as_list(load_json(combined).get("videos"))
    if platform == "instagram":
        combined = comments_dir / "instagram_comments.json"
        if combined.exists():
            return as_list(load_json(combined).get("videos"))
    if platform == "facebook":
        combined = comments_dir / "facebook_comments.json"
        if combined.exists():
            return as_list(load_json(combined).get("videos"))
    if platform == "x":
        combined = comments_dir / "x_comments.json"
        if combined.exists():
            return as_list(load_json(combined).get("videos"))

    videos = []
    for path in sorted(comments_dir.glob("video_*_comments.json"), key=sort_video_file):
        payload = load_json(path)
        if isinstance(payload, dict):
            videos.append(payload)
    return videos


def _candidate_metadata_only(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in candidate.items()
        if key not in {"comments", "replies"} and not key.startswith("_")
    }


def collect_run_candidates(
    session_path: Path,
    platform: str,
    videos: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}

    def merge(candidate: dict[str, Any], **metadata: Any) -> None:
        key = content_key(platform, candidate)
        if not key:
            return
        record = records.setdefault(key, {
            "content_key": key,
            "candidate": _candidate_metadata_only(candidate),
            "decision": "",
            "score": 0,
            "extraction_status": "discovered",
            "audit": {},
        })
        current = record["candidate"]
        incoming = _candidate_metadata_only(candidate)
        for field in (
            "video_id", "media_id", "url", "video_url", "title", "caption",
            "username", "content_creator", "creator", "published", "published_at",
            "create_time", "content_type",
        ):
            if not current.get(field) and incoming.get(field) not in (None, ""):
                current[field] = incoming[field]
        if metadata.get("decision"):
            record["decision"] = metadata["decision"]
        if metadata.get("score") is not None:
            record["score"] = int_value(metadata.get("score"))
        if metadata.get("extraction_status"):
            record["extraction_status"] = metadata["extraction_status"]
        audit_name = metadata.get("audit_name")
        if audit_name:
            record["audit"][audit_name] = metadata.get("audit_payload") or {}

    date_path = session_path / "logs" / f"date_{platform}_candidates.json"
    if date_path.exists():
        payload = load_json(date_path)
        for item in as_list(payload.get("candidates")):
            if not isinstance(item, dict) or not isinstance(item.get("candidate"), dict):
                continue
            decision = text_value(item.get("decision"))
            candidate = dict(item["candidate"])
            if item.get("published_at"):
                candidate.setdefault("published_at", item["published_at"])
            merge(
                candidate,
                extraction_status="outside_date" if decision == "outside_date" else "discovered",
                audit_name="date_window",
                audit_payload={key: value for key, value in item.items() if key != "candidate"},
            )

    relevance_path = session_path / "logs" / f"relevance_{platform}_candidates.json"
    if relevance_path.exists():
        payload = load_json(relevance_path)
        for item in as_list(payload.get("candidates")):
            if not isinstance(item, dict) or not isinstance(item.get("candidate"), dict):
                continue
            decision = text_value(item.get("decision"))
            merge(
                item["candidate"],
                decision=decision,
                score=item.get("score"),
                extraction_status="quarantined" if decision == "reject" else decision or "discovered",
                audit_name="relevance",
                audit_payload={key: value for key, value in item.items() if key != "candidate"},
            )

    known_path = session_path / "logs" / f"known_content_{platform}_candidates.json"
    if known_path.exists():
        payload = load_json(known_path)
        for item in as_list(payload.get("candidates")):
            if not isinstance(item, dict) or not isinstance(item.get("candidate"), dict):
                continue
            decision = text_value(item.get("decision"))
            merge(
                item["candidate"],
                extraction_status="known" if decision == "skip_known" else "queued",
                audit_name="known_content",
                audit_payload={key: value for key, value in item.items() if key != "candidate"},
            )

    for video in videos:
        if isinstance(video, dict):
            merge(video, extraction_status="extracted")
    return list(records.values())


def register_source_plan(
    conn: sqlite3.Connection,
    project: str,
    sources: list[dict[str, Any]],
    plan_digest: str,
) -> dict[str, int]:
    planned_at = iso_now()
    planned_platforms = {source["platform"] for source in sources}
    for platform in planned_platforms:
        conn.execute(
            """
            UPDATE discovery_sources SET active = 0
            WHERE project = ? AND platform = ? AND source_role != 'refresh'
            """,
            (project, platform),
        )
    for ordinal, source in enumerate(sources, start=1):
        conn.execute(
            """
            INSERT INTO discovery_sources (
                project, platform, source_key, source_kind, source_value,
                source_label, source_role, first_run_at, last_run_at,
                last_run_id, last_status, run_count, candidate_count,
                new_candidate_count, error, plan_digest, planned_order, active
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '', 'pending', 0, 0, 0, '', ?, ?, 1)
            ON CONFLICT(project, platform, source_key) DO UPDATE SET
                source_kind = excluded.source_kind,
                source_value = excluded.source_value,
                source_label = excluded.source_label,
                source_role = excluded.source_role,
                last_status = 'pending',
                candidate_count = 0,
                new_candidate_count = 0,
                error = '',
                plan_digest = excluded.plan_digest,
                planned_order = excluded.planned_order,
                active = 1
            """,
            (
                project,
                source["platform"],
                source["source_key"],
                source.get("kind") or "query",
                source.get("value") or "",
                source.get("label") or source.get("value") or "",
                source.get("role") or "",
                planned_at,
                planned_at,
                plan_digest,
                ordinal,
            ),
        )
    conn.commit()
    return {
        "planned_sources": len(sources),
        "planned_platforms": len(planned_platforms),
    }


def record_discovery_run(
    conn: sqlite3.Connection,
    *,
    project: str,
    platform: str,
    source: dict[str, Any],
    run_meta: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> dict[str, int]:
    source_key = text_value(source.get("source_key") or stable_hash(platform, source.get("kind"), source.get("value")))
    observed_at = text_value(run_meta.get("finished_at") or iso_now())
    new_candidates = 0
    inserted_observations = 0

    for rank, record in enumerate(candidates, start=1):
        key = text_value(record.get("content_key"))
        candidate = record.get("candidate") if isinstance(record.get("candidate"), dict) else {}
        if not key:
            continue
        raw_observation = {
            "candidate": candidate,
            "audit": record.get("audit") or {},
            "extraction_status": record.get("extraction_status") or "discovered",
        }
        inserted = conn.execute(
            """
            INSERT OR IGNORE INTO candidate_observations (
                run_id, project, platform, source_key, content_key, observed_at,
                decision, score, candidate_rank, raw_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_meta["run_id"],
                project,
                platform,
                source_key,
                key,
                observed_at,
                text_value(record.get("decision")),
                int_value(record.get("score")),
                rank,
                json.dumps(raw_observation, ensure_ascii=False),
            ),
        )
        if not inserted.rowcount:
            continue
        inserted_observations += 1

        existing = conn.execute(
            """
            SELECT source_keys_json
            FROM candidate_ledger
            WHERE project = ? AND platform = ? AND content_key = ?
            """,
            (project, platform, key),
        ).fetchone()
        source_keys = []
        if existing:
            try:
                source_keys = json.loads(existing["source_keys_json"] or "[]")
            except (TypeError, json.JSONDecodeError):
                source_keys = []
        else:
            new_candidates += 1
        if source_key not in source_keys:
            source_keys.append(source_key)

        normalized = normalize_video(platform, candidate, rank)
        conn.execute(
            """
            INSERT INTO candidate_ledger (
                project, platform, content_key, video_id, media_id, url,
                creator, caption, published_at, content_type,
                first_discovered_at, last_discovered_at, discovery_count,
                source_keys_json, last_decision, last_score,
                extraction_status, raw_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
            ON CONFLICT(project, platform, content_key) DO UPDATE SET
                video_id = COALESCE(NULLIF(excluded.video_id, ''), candidate_ledger.video_id),
                media_id = COALESCE(NULLIF(excluded.media_id, ''), candidate_ledger.media_id),
                url = COALESCE(NULLIF(excluded.url, ''), candidate_ledger.url),
                creator = COALESCE(NULLIF(excluded.creator, ''), candidate_ledger.creator),
                caption = COALESCE(NULLIF(excluded.caption, ''), candidate_ledger.caption),
                published_at = COALESCE(NULLIF(excluded.published_at, ''), candidate_ledger.published_at),
                content_type = COALESCE(NULLIF(excluded.content_type, ''), candidate_ledger.content_type),
                last_discovered_at = excluded.last_discovered_at,
                discovery_count = candidate_ledger.discovery_count + 1,
                source_keys_json = excluded.source_keys_json,
                last_decision = COALESCE(NULLIF(excluded.last_decision, ''), candidate_ledger.last_decision),
                last_score = excluded.last_score,
                extraction_status = COALESCE(NULLIF(excluded.extraction_status, ''), candidate_ledger.extraction_status),
                raw_json = excluded.raw_json
            """,
            (
                project,
                platform,
                key,
                normalized["video_id"],
                normalized["media_id"],
                normalized["url"],
                normalized["content_creator"],
                normalized["caption"],
                normalized["published_at"],
                normalized["content_type"],
                observed_at,
                observed_at,
                json.dumps(source_keys, ensure_ascii=False),
                text_value(record.get("decision")),
                int_value(record.get("score")),
                text_value(record.get("extraction_status")),
                json.dumps(_candidate_metadata_only(candidate), ensure_ascii=False),
            ),
        )

    conn.execute(
        """
        INSERT INTO discovery_sources (
            project, platform, source_key, source_kind, source_value,
            source_label, source_role, first_run_at, last_run_at,
            last_run_id, last_status, run_count, candidate_count,
            new_candidate_count, error
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
        ON CONFLICT(project, platform, source_key) DO UPDATE SET
            last_run_at = excluded.last_run_at,
            last_run_id = excluded.last_run_id,
            last_status = excluded.last_status,
            run_count = discovery_sources.run_count + 1,
            candidate_count = excluded.candidate_count,
            new_candidate_count = excluded.new_candidate_count,
            error = excluded.error,
            active = 1
        """,
        (
            project,
            platform,
            source_key,
            text_value(source.get("kind") or "query"),
            text_value(source.get("value")),
            text_value(source.get("label")),
            text_value(source.get("role")),
            text_value(run_meta.get("started_at") or observed_at),
            observed_at,
            text_value(run_meta.get("run_id")),
            text_value(run_meta.get("status")),
            inserted_observations,
            new_candidates,
            text_value(run_meta.get("error")),
        ),
    )
    conn.commit()
    return {
        "observed_candidates": inserted_observations,
        "new_candidates": new_candidates,
    }


def load_combined_videos(path: Path) -> list[dict[str, Any]]:
    payload = load_json(path)
    return as_list(payload.get("videos"))


def infer_platform(video: dict[str, Any]) -> str:
    platform = text_value(video.get("platform")).lower()
    if platform:
        return platform
    url = text_value(video.get("url") or video.get("video_url"))
    if "youtube.com" in url or "youtu.be" in url:
        return "youtube"
    if "instagram.com" in url:
        return "instagram"
    if "facebook.com" in url or "fb.watch" in url:
        return "facebook"
    if "tiktok.com" in url:
        return "tiktok"
    return "unknown"


def bootstrap_combined(conn: sqlite3.Connection, project: str, keyword: str, path: Path) -> dict[str, dict[str, int]]:
    if not path.exists():
        raise FileNotFoundError(path)
    scraped_at = dt.datetime.fromtimestamp(path.stat().st_mtime).replace(microsecond=0).isoformat()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for video in load_combined_videos(path):
        platform = infer_platform(video)
        grouped.setdefault(platform, []).append(video)

    results = {}
    for platform, videos in grouped.items():
        if platform == "unknown":
            continue
        run_id = f"bootstrap_{path.stem}_{platform}"
        conn.execute(
            """
            INSERT OR IGNORE INTO runs (
                run_id, project, platform, keyword, session_path, started_at,
                finished_at, status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'bootstrap')
            """,
            (run_id, project, platform, keyword, str(path), scraped_at, scraped_at),
        )
        stats = ingest_videos(
            conn,
            project=project,
            keyword=keyword,
            platform=platform,
            videos=videos,
            run_id=run_id,
            scraped_at=scraped_at,
        )
        conn.execute(
            """
            UPDATE runs SET discovered_count = ?, new_content_count = ?,
                new_comment_count = ?, total_comment_count = ?
            WHERE run_id = ?
            """,
            (
                stats["discovered_count"],
                stats["new_content_count"],
                stats["new_comment_count"],
                stats["total_comment_count"],
                run_id,
            ),
        )
        update_platform_state(conn, project, platform, keyword, run_id, scraped_at)
        results[platform] = stats
    conn.commit()
    return results


def update_platform_state(
    conn: sqlite3.Connection,
    project: str,
    platform: str,
    keyword: str,
    run_id: str,
    finished_at: str,
    since_id: str = "",
) -> None:
    totals = conn.execute(
        """
        SELECT
            COUNT(DISTINCT content_key),
            COUNT(*)
        FROM comments
        WHERE project = ? AND platform = ?
        """,
        (project, platform),
    ).fetchone()
    content_total = conn.execute(
        """
        SELECT COUNT(*)
        FROM content_items
        WHERE project = ? AND platform = ?
        """,
        (project, platform),
    ).fetchone()[0]
    total_comments = totals[1] if totals else 0
    previous_state = conn.execute(
        "SELECT since_id FROM platform_state WHERE project = ? AND platform = ? AND keyword = ?",
        (project, platform, keyword),
    ).fetchone()
    previous_since_id = text_value(previous_state[0]) if previous_state else ""
    numeric_ids = [
        int(value)
        for value in (previous_since_id, text_value(since_id))
        if value.isdigit()
    ]
    resolved_since_id = str(max(numeric_ids)) if numeric_ids else (text_value(since_id) or previous_since_id)
    conn.execute(
        """
        INSERT INTO platform_state (
            project, platform, keyword, last_successful_scrape_at, last_run_id,
            since_id, total_content, total_comments
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(project, platform, keyword) DO UPDATE SET
            last_successful_scrape_at = excluded.last_successful_scrape_at,
            last_run_id = excluded.last_run_id,
            since_id = excluded.since_id,
            total_content = excluded.total_content,
            total_comments = excluded.total_comments
        """,
        (
            project,
            platform,
            keyword,
            finished_at,
            run_id,
            resolved_since_id,
            content_total,
            total_comments,
        ),
    )
    conn.commit()


def export_known_content_file(
    conn: sqlite3.Connection,
    project: str,
    platform: str,
    paths: dict[str, Path],
    *,
    refresh_limit: int = 0,
    refresh_after_hours: float = 6.0,
) -> Path:
    from tiktok_scraper.known_content import candidate_content_keys

    rows = conn.execute(
        """
        SELECT content_key, video_id, media_id, url, creator, caption,
               first_seen_at, last_scraped_at
        FROM content_items
        WHERE project = ? AND platform = ?
        ORDER BY first_seen_at, content_key
        """,
        (project, platform),
    ).fetchall()
    state_row = conn.execute(
        """
        SELECT MAX(last_successful_scrape_at) AS last_successful_scrape_at,
               COUNT(*) AS state_rows,
               SUM(total_content) AS total_content,
               SUM(total_comments) AS total_comments
        FROM platform_state
        WHERE project = ? AND platform = ?
        """,
        (project, platform),
    ).fetchone()

    refresh_limit = max(0, int(refresh_limit or 0))
    refresh_cutoff = now_local() - dt.timedelta(hours=max(0.0, float(refresh_after_hours or 0.0)))
    refresh_candidates = sorted(
        rows,
        key=lambda row: (text_value(row["first_seen_at"]), row["content_key"]),
        reverse=True,
    )
    refresh_content_keys: set[str] = set()
    for row in refresh_candidates:
        if len(refresh_content_keys) >= refresh_limit:
            break
        last_scraped = parse_stored_datetime(row["last_scraped_at"])
        if last_scraped <= refresh_cutoff:
            refresh_content_keys.add(row["content_key"])

    keys: set[str] = set()
    items = []
    for row in rows:
        candidate = {
            "content_key": row["content_key"],
            "video_id": row["video_id"],
            "media_id": row["media_id"],
            "url": row["url"],
            "caption": row["caption"],
            "content_creator": row["creator"],
        }
        candidate_keys = candidate_content_keys(platform, candidate)
        refresh_scheduled = row["content_key"] in refresh_content_keys
        if not refresh_scheduled:
            keys.update(candidate_keys)
        items.append(
            {
                "content_key": row["content_key"],
                "video_id": row["video_id"],
                "media_id": row["media_id"],
                "url": row["url"],
                "first_seen_at": row["first_seen_at"],
                "last_scraped_at": row["last_scraped_at"],
                "keys": candidate_keys,
                "refresh_scheduled": refresh_scheduled,
            }
        )

    last_successful_scrape_at = text_value(state_row["last_successful_scrape_at"]) if state_row else ""
    state_rows = int(state_row["state_rows"] or 0) if state_row else 0
    total_content = int(state_row["total_content"] or 0) if state_row else 0
    total_comments = int(state_row["total_comments"] or 0) if state_row else 0

    output_path = paths["state"] / f"known_{platform}_content.json"
    payload = {
        "project": project,
        "platform": platform,
        "generated_at": iso_now(),
        "last_successful_scrape_at": last_successful_scrape_at,
        "known_content_count": len(rows),
        "known_key_count": len(keys),
        "refresh_scheduled_count": len(refresh_content_keys),
        "refresh_after_hours": refresh_after_hours,
        "refresh_content_keys": sorted(refresh_content_keys),
        "state_rows": state_rows,
        "total_content": total_content,
        "total_comments": total_comments,
        "keys": sorted(keys),
        "items": items,
    }
    atomic_json_dump(output_path, payload)
    print(
        f"[INFO] Exported known {platform} content: "
        f"items={len(rows)} keys={len(keys)} refresh={len(refresh_content_keys)} "
        f"file={output_path}",
        flush=True,
    )
    return output_path


def export_direct_refresh_candidates(
    conn: sqlite3.Connection,
    project: str,
    platform: str,
    paths: dict[str, Path],
    *,
    limit: int,
    after_hours: float,
) -> tuple[Path | None, int]:
    limit = max(0, int(limit or 0))
    if not limit:
        return None, 0
    cutoff = (now_local() - dt.timedelta(hours=max(0.0, float(after_hours or 0.0)))).isoformat()
    rows = conn.execute(
        """
        SELECT content_key, video_id, media_id, url, creator, caption,
               published_at, content_type, last_scraped_at
        FROM content_items
        WHERE project = ? AND platform = ?
          AND url != ''
          AND last_scraped_at <= ?
        ORDER BY last_scraped_at, first_seen_at, content_key
        LIMIT ?
        """,
        (project, platform, cutoff, limit),
    ).fetchall()
    if not rows:
        return None, 0

    videos = [
        {
            "platform": platform,
            "content_key": row["content_key"],
            "video_id": row["video_id"],
            "id": row["video_id"],
            "media_id": row["media_id"],
            "url": row["url"],
            "content_creator": row["creator"],
            "caption": row["caption"],
            "published_at": row["published_at"],
            "content_type": row["content_type"],
            "last_scraped_at": row["last_scraped_at"],
        }
        for row in rows
    ]
    output_path = paths["state"] / f"direct_refresh_{platform}_candidates.json"
    atomic_json_dump(output_path, {
        "project": project,
        "platform": platform,
        "generated_at": iso_now(),
        "refresh_after_hours": after_hours,
        "videos": videos,
    })
    return output_path, len(videos)


def row_to_comment(row: sqlite3.Row) -> dict[str, Any]:
    try:
        raw = json.loads(row["raw_json"])
    except Exception:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    # Replies are stored as their own rows; keeping nested reply blobs here makes
    # report generation re-flatten the same comments and bloats large exports.
    for nested_key in ("replies", "reply_comments", "children"):
        raw.pop(nested_key, None)
    raw.setdefault("comment_id", row["comment_key"])
    raw.setdefault("parent_comment_id", row["parent_comment_id"])
    raw.setdefault("text", row["text"])
    raw.setdefault("author", row["author"])
    raw.setdefault("author_id", row["author_id"])
    raw.setdefault("likes", row["likes"])
    raw.setdefault("time", row["comment_time"])
    raw.setdefault("reply_count", row["reply_count"])
    raw.setdefault("first_seen_at", row["first_seen_at"])
    raw["last_seen_at"] = row["last_seen_at"] or row["scraped_at"]
    raw["is_reply"] = bool(row["is_reply"])
    return raw


def export_datetime_sort_value(value: Any) -> float:
    parsed = parse_stored_datetime(value)
    if parsed.year <= 1:
        return float("-inf")
    try:
        return parsed.astimezone(dt.timezone.utc).timestamp()
    except (OverflowError, OSError, ValueError):
        return float("-inf")


def comment_export_sort_key(comment: dict[str, Any]) -> tuple[float, str]:
    evidence = comment.get("comment_evidence") if isinstance(comment.get("comment_evidence"), dict) else {}
    publication = evidence.get("publication") if isinstance(evidence.get("publication"), dict) else {}
    timestamp = (
        comment.get("comment_created_at")
        or publication.get("normalized_at")
        or comment.get("time")
        or comment.get("create_time")
        or comment.get("first_seen_at")
    )
    comment_id = text_value(comment.get("comment_id") or comment.get("id") or comment.get("cid"))
    return export_datetime_sort_value(timestamp), comment_id


def ordered_comment_threads(comments: Iterable[dict[str, Any]]) -> list[tuple[dict[str, Any], int, str]]:
    rows = [comment for comment in comments if isinstance(comment, dict)]
    ids = {
        text_value(comment.get("comment_id") or comment.get("id") or comment.get("cid"))
        for comment in rows
    }
    ids.discard("")
    children: dict[str, list[dict[str, Any]]] = {}
    roots: list[dict[str, Any]] = []
    for comment in rows:
        comment_id = text_value(comment.get("comment_id") or comment.get("id") or comment.get("cid"))
        parent_id = text_value(comment.get("parent_comment_id"))
        if parent_id and parent_id in ids and parent_id != comment_id:
            children.setdefault(parent_id, []).append(comment)
        else:
            roots.append(comment)

    ordered: list[tuple[dict[str, Any], int, str]] = []
    visited: set[int] = set()

    def visit(comment: dict[str, Any], depth: int, root_id: str) -> None:
        object_id = id(comment)
        if object_id in visited:
            return
        visited.add(object_id)
        comment_id = text_value(comment.get("comment_id") or comment.get("id") or comment.get("cid"))
        effective_root = root_id or comment_id
        ordered.append((comment, depth, effective_root))
        for child in sorted(children.get(comment_id, []), key=comment_export_sort_key):
            visit(child, depth + 1, effective_root)

    for root in sorted(roots, key=comment_export_sort_key):
        visit(root, 0, "")
    for comment in sorted(rows, key=comment_export_sort_key):
        if id(comment) not in visited:
            visit(comment, 0, "")
    return ordered


def normalized_comment_export(
    comment: dict[str, Any],
    *,
    platform: str,
    content_id: str,
    thread_depth: int,
    thread_root_id: str,
    comment_index: int,
) -> dict[str, Any]:
    evidence = comment.get("comment_evidence") if isinstance(comment.get("comment_evidence"), dict) else {}
    publication = evidence.get("publication") if isinstance(evidence.get("publication"), dict) else {}
    profile_reference = (
        comment.get("author_profile_reference")
        if isinstance(comment.get("author_profile_reference"), dict)
        else {}
    )
    return {
        "schema_version": "1.0",
        "platform": platform,
        "content_id": content_id,
        "comment_index": comment_index,
        "comment_id": text_value(comment.get("comment_id") or comment.get("id") or comment.get("cid")),
        "parent_comment_id": text_value(comment.get("parent_comment_id")),
        "thread_root_id": thread_root_id,
        "thread_depth": thread_depth,
        "is_reply": bool(comment.get("is_reply") or comment.get("parent_comment_id")),
        "text": text_value(comment.get("text") or comment.get("comment") or comment.get("content")),
        "author": comment_author(comment),
        "author_id": comment_author_id(comment),
        "author_display_name": text_value(comment.get("author_display_name")),
        "author_verified": comment.get("author_verified"),
        "author_profile_reference": profile_reference,
        "likes": comment_likes(comment),
        "reply_count": int_value(comment.get("reply_count")),
        "retweet_count": int_value(comment.get("retweet_count")),
        "quote_count": int_value(comment.get("quote_count")),
        "published_at": text_value(comment.get("comment_created_at") or publication.get("normalized_at")),
        "published_at_raw": publication.get("raw_value"),
        "language": text_value(comment.get("lang") or comment.get("language")),
        "url": text_value(comment.get("url")),
        "collection_method": text_value(comment.get("collection_method")),
        "observed_at": text_value(comment.get("observed_at")),
        "first_seen_at": text_value(comment.get("first_seen_at")),
        "last_seen_at": text_value(comment.get("last_seen_at")),
        "comment_evidence": evidence,
    }


def normalized_video_export(video: dict[str, Any], dataset_index: int) -> dict[str, Any]:
    platform = text_value(video.get("platform"))
    content_id = text_value(video.get("video_id") or video.get("media_id"))
    comments = [
        normalized_comment_export(
            comment,
            platform=platform,
            content_id=content_id,
            thread_depth=depth,
            thread_root_id=root_id,
            comment_index=index,
        )
        for index, (comment, depth, root_id) in enumerate(
            ordered_comment_threads(video.get("comments") or []),
            start=1,
        )
    ]
    return {
        "schema_version": "1.0",
        "dataset_index": dataset_index,
        "platform": platform,
        "content_key": text_value(video.get("content_key")),
        "video_number": video.get("video_number"),
        "video_id": text_value(video.get("video_id")),
        "media_id": text_value(video.get("media_id")),
        "url": text_value(video.get("url")),
        "caption": text_value(video.get("caption")),
        "description": text_value(video.get("description")),
        "published_at": text_value(video.get("published_at")),
        "content_type": text_value(video.get("content_type")),
        "content_creator": text_value(video.get("content_creator")),
        "creator_profile_reference": video.get("creator_profile_reference") or {},
        "view_count": video.get("view_count"),
        "like_count": video.get("like_count"),
        "share_count": video.get("share_count"),
        "save_count": video.get("save_count"),
        "follower_count": video.get("follower_count"),
        "reported_comment_count": video.get("reported_comment_count"),
        "top_level_comment_count": video.get("top_level_comment_count"),
        "comment_count": len(comments),
        "transcript": text_value(video.get("transcript")),
        "transcript_available": bool(video.get("transcript_available")),
        "transcript_language": text_value(video.get("transcript_language")),
        "transcript_language_name": text_value(video.get("transcript_language_name")),
        "transcript_is_auto_generated": bool(video.get("transcript_is_auto_generated")),
        "transcript_segment_count": int_value(video.get("transcript_segment_count")),
        "transcript_segments": as_list(video.get("transcript_segments")),
        "transcript_error": text_value(video.get("transcript_error")),
        "transcript_status": text_value(video.get("transcript_status")),
        "transcript_source": text_value(video.get("transcript_source")),
        "transcript_attempt_count": int_value(video.get("transcript_attempt_count")),
        "subtitle_tracks": as_list(video.get("subtitle_tracks")),
        "subtitle_selected_track": video.get("subtitle_selected_track") or {},
        "subtitle_no_caption_reason": text_value(video.get("subtitle_no_caption_reason")),
        "subtitle_manifest_source": text_value(video.get("subtitle_manifest_source")),
        "matched_keywords": as_list(video.get("matched_keywords")),
        "source_provenance": video.get("source_provenance") or {},
        "transport_provenance": video.get("transport_provenance") or {},
        "collection_status": video.get("collection_status") or {},
        "metric_availability": video.get("metric_availability") or {},
        "field_availability": video.get("field_availability") or {},
        "analysis_readiness": video.get("analysis_readiness") or {},
        "quality_flags": as_list(video.get("quality_flags")),
        "error": text_value(video.get("error")),
        "observed_at": text_value(video.get("observed_at")),
        "first_seen_at": text_value(video.get("first_seen_at")),
        "last_scraped_at": text_value(video.get("last_scraped_at")),
        "comments": comments,
    }


def normalized_profile_index(profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "platform": text_value(profile.get("platform")),
        "profile_key": text_value(profile.get("profile_key")),
        "user_id": text_value(profile.get("user_id")),
        "username": text_value(profile.get("username")),
        "display_name": text_value(profile.get("display_name")),
        "profile_url": text_value(profile.get("profile_url")),
        "verified": profile.get("verified"),
        "account_type": text_value(profile.get("account_type")),
        "category": text_value(profile.get("category")),
        "follower_count": profile.get("follower_count"),
        "profile_geography": profile.get("profile_geography") or {},
        "self_declared": profile.get("self_declared") or {},
        "status": text_value(profile.get("status")),
        "observed_at": text_value(profile.get("observed_at")),
    }


def normalized_compiled_videos(videos: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(
        (video for video in videos if isinstance(video, dict)),
        key=lambda video: (
            export_datetime_sort_value(video.get("published_at") or video.get("first_seen_at")),
            text_value(video.get("platform")),
            text_value(video.get("video_id") or video.get("media_id")),
        ),
        reverse=True,
    )
    return [normalized_video_export(video, index) for index, video in enumerate(ordered, start=1)]


def project_date_windows(conn: sqlite3.Connection, project: str) -> list[dict[str, Any]]:
    from tiktok_scraper.date_filter import normalize_windows

    row = conn.execute(
        "SELECT value FROM project_meta WHERE key = 'date_windows'"
    ).fetchone()
    if not row:
        return []
    return normalize_windows(row[0])


def effective_content_published_at(content: sqlite3.Row, platform: str) -> str:
    try:
        raw = json.loads(content["raw_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    reference_time = content["first_seen_at"]
    effective = content_published_at(raw, reference_time=reference_time)
    if effective:
        return effective
    if platform != "tiktok":
        return content_published_at(
            {"published_at": content["published_at"]},
            reference_time=reference_time,
        )
    return ""


def filter_content_rows_by_date(
    content_rows: list[sqlite3.Row],
    windows: list[dict[str, Any]],
) -> tuple[list[sqlite3.Row], int, dict[str, str]]:
    from tiktok_scraper.date_filter import filter_candidates_by_date

    eligible = []
    excluded = 0
    effective_dates: dict[str, str] = {}
    for row in content_rows:
        effective_date = effective_content_published_at(row, row["platform"])
        effective_dates[row["content_key"]] = effective_date
        candidate = {
            "video_id": row["video_id"],
            "media_id": row["media_id"],
            "url": row["url"],
            "published_at": effective_date,
        }
        kept, audit = filter_candidates_by_date([candidate], windows) if windows else ([candidate], [])
        if audit and audit[0]["decision"] == "outside_date":
            excluded += 1
        else:
            eligible.extend([row] if kept else [])
    return eligible, excluded, effective_dates


PUBLIC_METRIC_NAMES = ("views", "likes", "reported_comments", "shares", "saves", "followers")
FIRST_PARTY_METRIC_NAMES = (
    "reach",
    "impressions",
    "traffic",
    "link_clicks",
    "conversions",
    "geography",
    "demographics",
)


def build_metric_coverage_report(
    videos: list[dict[str, Any]],
    *,
    project: str,
    compiled_at: str,
) -> dict[str, Any]:
    def increment(target: dict[str, int], key: str) -> None:
        key = key or "unknown"
        target[key] = target.get(key, 0) + 1

    def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
        metrics: dict[str, Any] = {}
        for metric in PUBLIC_METRIC_NAMES:
            status_counts: dict[str, int] = {}
            available_values: list[int] = []
            for video in rows:
                availability = video.get("metric_availability") or {}
                evidence = video.get("brief_evidence") or {}
                snapshot = evidence.get("engagement_snapshot") or {}
                status = text_value(availability.get(metric) or "missing_from_public_response")
                increment(status_counts, status)
                value = snapshot.get(metric)
                if status.startswith("available") and isinstance(value, (int, float)):
                    available_values.append(max(0, int(value)))
            available_records = sum(
                count for status, count in status_counts.items() if status.startswith("available")
            )
            entry: dict[str, Any] = {
                "records": len(rows),
                "available_records": available_records,
                "availability_percent": round(available_records * 100 / len(rows), 2) if rows else 0.0,
                "records_with_nonzero_value": sum(value > 0 for value in available_values),
                "status_counts": status_counts,
            }
            if metric != "followers":
                entry["sum_of_available_values"] = sum(available_values)
            metrics[metric] = entry

        completion_counts: dict[str, int] = {}
        collected_comments = 0
        for video in rows:
            status = text_value((video.get("collection_status") or {}).get("completion_status") or "unknown")
            increment(completion_counts, status)
            collected_comments += len(video.get("comments") or [])
        metrics["comments"] = {
            "records": len(rows),
            "records_with_comments": sum(bool(video.get("comments")) for video in rows),
            "comments_collected": collected_comments,
            "completion_status_counts": completion_counts,
            "complete_records": completion_counts.get("complete", 0),
            "incomplete_records": sum(
                completion_counts.get(status, 0) for status in ("partial", "truncated", "error")
            ),
            "unknown_records": completion_counts.get("unknown", 0),
        }

        comments = [
            comment
            for video in rows
            for comment in (video.get("comments") or [])
            if isinstance(comment, dict)
        ]
        normalized = sum(bool(text_value(comment.get("comment_created_at"))) for comment in comments)
        relative_estimates = 0
        for comment in comments:
            evidence = comment.get("comment_evidence") if isinstance(comment.get("comment_evidence"), dict) else {}
            publication = evidence.get("publication") if isinstance(evidence.get("publication"), dict) else {}
            raw = text_value(publication.get("raw_value"))
            if publication.get("normalized_at") and raw and not re.fullmatch(r"\d+(?:\.\d+)?", raw):
                try:
                    dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
                except ValueError:
                    relative_estimates += 1
        return {
            "records": len(rows),
            "metrics": metrics,
            "comment_timestamps": {
                "comments": len(comments),
                "normalized": normalized,
                "missing": len(comments) - normalized,
                "normalized_percent": round(normalized * 100 / len(comments), 2) if comments else 0.0,
                "estimated_from_relative_text": relative_estimates,
            },
        }

    platform_names = sorted({text_value(video.get("platform")) for video in videos if video.get("platform")})
    by_platform = {
        platform: summarize([video for video in videos if video.get("platform") == platform])
        for platform in platform_names
    }
    overall = summarize(videos)
    return {
        "schema_version": "1.0",
        "project": project,
        "compiled_at": compiled_at,
        "overall": overall,
        "by_platform": by_platform,
        "first_party_only": {
            metric: {
                "status": "first_party_required",
                "public_comment_inference_allowed": False,
            }
            for metric in FIRST_PARTY_METRIC_NAMES
        },
        "metric_definitions": {
            "comments": "Public comments collected for each content item; completeness is reported separately.",
            "reported_comments": "Platform-reported public comment count when exposed; it may include filtered or unavailable comments.",
            "likes": "Platform-specific public like or reaction action at observation time.",
            "views": "Platform-specific public play/view counter at observation time; qualification rules differ by platform.",
            "shares": "Public share or repost counter only where the platform response exposes it.",
            "saves": "Public save/favorite counter only where exposed; most platforms keep this private.",
            "followers": "Creator/profile follower or subscriber snapshot, not reach for the individual post.",
            "reach": "Unique-account exposure; requires owned-account or first-party analytics.",
            "impressions": "Total exposures; requires owned-account or first-party analytics.",
            "traffic_and_conversions": "Requires platform insights, web analytics, campaign tags, or conversion APIs.",
        },
        "cross_platform_comparability": {
            "comments": "limited: compare collected volume only alongside completeness status",
            "reported_comments": "limited: platform counters can include comments not publicly retrievable",
            "likes": "limited: reactions and likes are different platform actions",
            "views": "not_directly_comparable: view qualification and eligible content types differ",
            "shares": "limited_and_sparse: public exposure differs substantially",
            "saves": "not_comparable_when_missing: usually private",
            "followers": "creator_level_only: deduplicate creators before using profile-size analysis",
            "reach": "not_available_from_public_scraping",
            "impressions": "not_available_from_public_scraping",
            "traffic_and_conversions": "not_available_from_public_scraping",
        },
        "interpretation": (
            "Null means unavailable, not zero. Aggregate only values whose status is available, "
            "and use cross-platform results as indexed or normalized indicators rather than identical units."
        ),
    }


def compile_outputs(conn: sqlite3.Connection, project: str, keyword: str, paths: dict[str, Path], hours: int | None = None) -> dict[str, Any]:
    from campaign_spec import normalize_analysis_config
    from tiktok_scraper.analysis_workflow import export_analysis_artifacts
    from tiktok_scraper.profile_contract import extract_profile_candidate
    from tiktok_scraper.profile_enrichment import (
        export_profiles,
        load_profile_lookup,
        profile_coverage,
        profile_reference_for,
    )

    if hours:
        output_dir = paths["reports"] / f"last_{hours}h"
        cutoff = (now_local() - dt.timedelta(hours=hours)).isoformat()
        title = f"{keyword} last {hours}h"
    else:
        output_dir = paths["latest"]
        cutoff = None
        title = keyword
    output_dir.mkdir(parents=True, exist_ok=True)

    conn.row_factory = sqlite3.Row
    profile_lookup = load_profile_lookup(conn, project)
    videos = []
    summary_by_platform: dict[str, dict[str, int]] = {}
    platforms = ["youtube", "tiktok", "instagram", "facebook", "x"]
    date_windows = project_date_windows(conn, project)

    for platform in platforms:
        if cutoff:
            content_rows = conn.execute(
                """
                WITH changed_content AS (
                    SELECT DISTINCT content_key
                    FROM comments
                    WHERE project = ? AND platform = ? AND scraped_at >= ?
                    UNION
                    SELECT content_key
                    FROM content_items
                    WHERE project = ? AND platform = ? AND last_scraped_at >= ?
                )
                SELECT ci.*
                FROM content_items ci
                JOIN changed_content changed
                    ON changed.content_key = ci.content_key
                WHERE ci.project = ? AND ci.platform = ?
                ORDER BY ci.first_seen_at, ci.content_key
                """,
                (project, platform, cutoff, project, platform, cutoff, project, platform),
            ).fetchall()
        else:
            content_rows = conn.execute(
                """
                SELECT *
                FROM content_items
                WHERE project = ? AND platform = ?
                ORDER BY first_seen_at, content_key
                """,
                (project, platform),
            ).fetchall()
        content_rows, outside_date_count, effective_dates = filter_content_rows_by_date(content_rows, date_windows)

        if cutoff:
            all_comment_rows = conn.execute(
                """
                SELECT *
                FROM comments
                WHERE project = ? AND platform = ? AND scraped_at >= ?
                ORDER BY content_key, first_seen_at, comment_key
                """,
                (project, platform, cutoff),
            ).fetchall()
        else:
            all_comment_rows = conn.execute(
                """
                SELECT *
                FROM comments
                WHERE project = ? AND platform = ?
                ORDER BY content_key, first_seen_at, comment_key
                """,
                (project, platform),
            ).fetchall()
        comments_by_content: dict[str, list[sqlite3.Row]] = {}
        for row in all_comment_rows:
            comments_by_content.setdefault(row["content_key"], []).append(row)

        platform_videos = []
        flat_count = 0
        top_count = 0
        for index, content in enumerate(content_rows, start=1):
            comment_rows = comments_by_content.get(content["content_key"], [])
            comments = [row_to_comment(row) for row in comment_rows]
            for comment in comments:
                comment["author_profile_reference"] = profile_reference_for(
                    profile_lookup,
                    platform=platform,
                    user_id=comment_author_id(comment),
                    username=comment_author(comment),
                )
            try:
                raw_content = json.loads(content["raw_json"] or "{}")
            except (TypeError, ValueError):
                raw_content = {}
            if not isinstance(raw_content, dict):
                raw_content = {}
            raw_collection_status = raw_content.get("collection_status")
            if not isinstance(raw_collection_status, dict):
                raw_collection_status = {}
            if "reported_comment_count" in raw_collection_status:
                compiled_reported_comment_count = raw_collection_status.get("reported_comment_count")
            elif "reported_comment_count" in raw_content:
                compiled_reported_comment_count = raw_content.get("reported_comment_count")
            else:
                compiled_reported_comment_count = content["reported_comment_count"]
            creator_candidate = extract_profile_candidate(
                {
                    **raw_content,
                    "content_creator": content["creator"],
                },
                platform=platform,
                role="creator",
            )
            creator_profile_reference = profile_reference_for(
                profile_lookup,
                platform=platform,
                user_id=creator_candidate.get("user_id"),
                username=creator_candidate.get("username") or content["creator"],
            )
            video = {
                "platform": platform,
                "content_key": content["content_key"],
                "video_number": index,
                "video_id": content["video_id"],
                "media_id": content["media_id"],
                "url": content["url"],
                "caption": content["caption"],
                "description": content["description"],
                "published_at": effective_dates.get(content["content_key"], content["published_at"]),
                "content_type": content["content_type"],
                "view_count": content["view_count"],
                "like_count": content["like_count"],
                "share_count": content["share_count"],
                "save_count": content["save_count"],
                "follower_count": content["follower_count"],
                "reported_comment_count": compiled_reported_comment_count,
                "transcript": content["transcript"],
                "transcript_available": bool(content["transcript_available"]),
                "transcript_language": content["transcript_language"],
                "transcript_language_name": content["transcript_language_name"],
                "transcript_is_auto_generated": bool(content["transcript_is_auto_generated"]),
                "transcript_segment_count": content["transcript_segment_count"],
                "transcript_segments": as_list(
                    json_value(content["transcript_segments_json"], [])
                ),
                "transcript_error": content["transcript_error"],
                "transcript_status": content["transcript_status"],
                "transcript_source": content["transcript_source"],
                "transcript_attempt_count": content["transcript_attempt_count"],
                "subtitle_tracks": as_list(
                    json_value(content["subtitle_tracks_json"], [])
                ),
                "subtitle_selected_track": json_value(
                    content["subtitle_selected_track_json"], {}
                ),
                "subtitle_no_caption_reason": content["subtitle_no_caption_reason"],
                "subtitle_manifest_source": content["subtitle_manifest_source"],
                "content_creator": content["creator"],
                "creator_profile_reference": creator_profile_reference,
                "top_level_comment_count": 0,
                "comment_count": len(comment_rows),
                "matched_keywords": json.loads(content["matched_keywords_json"] or "[]"),
                "error": content["error"],
                "comments": comments,
                "first_seen_at": content["first_seen_at"],
                "last_scraped_at": content["last_scraped_at"],
            }
            from tiktok_scraper.raw_contract import finalize_content_record

            contract_input = {**raw_content, **video, "comments": comments}
            metric_aliases = {
                "view_count": ("view_count", "views", "play_count"),
                "like_count": ("like_count", "likes", "digg_count"),
                "share_count": ("share_count", "shares"),
                "save_count": ("save_count", "saves", "collect_count"),
                "follower_count": ("follower_count", "followers", "subscriber_count"),
            }
            raw_metric_status = raw_content.get("metric_availability") if isinstance(raw_content.get("metric_availability"), dict) else {}
            raw_metric_containers = [raw_content]
            raw_metric_containers.extend(
                raw_content.get(name)
                for name in ("stats", "statistics", "metrics", "engagement")
                if isinstance(raw_content.get(name), dict)
            )
            metric_status_names = {
                "view_count": "views",
                "like_count": "likes",
                "share_count": "shares",
                "save_count": "saves",
                "follower_count": "followers",
            }
            for field, aliases in metric_aliases.items():
                raw_has_value = any(
                    any(alias in container and container.get(alias) not in (None, "") for alias in aliases)
                    for container in raw_metric_containers
                )
                has_status = bool(raw_metric_status.get(metric_status_names[field]))
                if not raw_has_value and not has_status and not video.get(field):
                    contract_input.pop(field, None)

            contract_record = finalize_content_record(
                contract_input,
                platform=platform,
                observed_at=raw_content.get("observed_at") or content["last_scraped_at"],
            )
            video["comments"] = contract_record.get("comments", comments)
            engagement_snapshot = (contract_record.get("brief_evidence") or {}).get("engagement_snapshot") or {}
            for field, metric in (
                ("view_count", "views"),
                ("like_count", "likes"),
                ("share_count", "shares"),
                ("save_count", "saves"),
                ("follower_count", "followers"),
                ("reported_comment_count", "reported_comments"),
            ):
                video[field] = engagement_snapshot.get(metric)
            top_level = top_level_comment_count(video["comments"], platform=platform)
            video["top_level_comment_count"] = top_level
            video["comment_count"] = len(video["comments"])
            top_count += top_level
            flat_count += len(video["comments"])
            for field in (
                "raw_schema_version",
                "observed_at",
                "source_provenance",
                "transport_provenance",
                "collection_status",
                "metric_availability",
                "field_availability",
                "analysis_readiness",
                "quality_flags",
                "brief_evidence",
            ):
                video[field] = contract_record.get(field)
            platform_videos.append(video)
            videos.append(video)

        summary_by_platform[platform] = {
            "videos": len(platform_videos),
            "videos_with_comments": sum(1 for video in platform_videos if video["comment_count"] > 0),
            "videos_with_transcript": sum(1 for video in platform_videos if video["transcript_available"]),
            "videos_with_published_at": sum(1 for video in platform_videos if video["published_at"]),
            "videos_with_public_engagement": sum(
                1
                for video in platform_videos
                if any(
                    text_value((video.get("metric_availability") or {}).get(metric)).startswith("available")
                    for metric in ("views", "likes", "reported_comments", "shares", "saves")
                )
            ),
            "videos_with_transcript_error": sum(
                1
                for video in platform_videos
                if video["transcript_status"] not in {"", "ok", "unavailable", "disabled"}
            ),
            "zero_comment_or_error_videos": sum(1 for video in platform_videos if video["comment_count"] == 0),
            "top_level_comments": top_count,
            "flat_comments": flat_count,
            "outside_date_videos_excluded": outside_date_count,
            "videos_using_api_data": sum(
                bool(video.get("transport_provenance", {}).get("api_data_used"))
                for video in platform_videos
            ),
            "videos_using_dom": sum(
                bool(video.get("transport_provenance", {}).get("dom_used"))
                for video in platform_videos
            ),
            "videos_with_truncated_comments": sum(
                video.get("collection_status", {}).get("completion_status") == "truncated"
                for video in platform_videos
            ),
            "videos_with_partial_comments": sum(
                video.get("collection_status", {}).get("completion_status") == "partial"
                for video in platform_videos
            ),
            "videos_with_unknown_comment_completeness": sum(
                video.get("collection_status", {}).get("completion_status") == "unknown"
                for video in platform_videos
            ),
        }

    flat_rows = []
    for video in videos:
        source_provenance = video.get("source_provenance") or {}
        transport_provenance = video.get("transport_provenance") or {}
        collection_status = video.get("collection_status") or {}
        metric_availability = video.get("metric_availability") or {}
        creator_profile = video.get("creator_profile_reference") or {}
        creator_geo = creator_profile.get("declared_geography") or {}
        creator_self_declared = creator_profile.get("self_declared") or {}
        for index, comment in enumerate(video["comments"], start=1):
            parent_id = text_value(comment.get("parent_comment_id"))
            author_profile = comment.get("author_profile_reference") or {}
            author_geo = author_profile.get("declared_geography") or {}
            author_self_declared = author_profile.get("self_declared") or {}
            raw_comment_time = text_value(comment.get("time") or comment.get("create_time"))
            normalized_comment_time = text_value(comment.get("comment_created_at")) or comment_time(
                video["platform"],
                comment,
                reference_time=video.get("observed_at") or video.get("last_scraped_at"),
            )
            flat_rows.append(
                {
                    "platform": video["platform"],
                    "video_number": video["video_number"],
                    "video_id": video["video_id"],
                    "video_url": video["url"],
                    "content_creator": video["content_creator"],
                    "creator_profile_key": creator_profile.get("profile_key", ""),
                    "creator_profile_status": creator_profile.get("status", ""),
                    "creator_declared_location": creator_geo.get("raw", ""),
                    "creator_country_code": creator_geo.get("country_code", ""),
                    "creator_region": creator_geo.get("region", ""),
                    "creator_city": creator_geo.get("city", ""),
                    "creator_self_declared_pronouns": "|".join(
                        creator_self_declared.get("pronouns") or []
                    ),
                    "creator_self_declared_age_statement": creator_self_declared.get(
                        "age_statement", ""
                    ),
                    "caption": video["caption"],
                    "description": video["description"],
                    "published_at": video["published_at"],
                    "content_type": video["content_type"],
                    "view_count": video["view_count"],
                    "like_count": video["like_count"],
                    "share_count": video["share_count"],
                    "save_count": video["save_count"],
                    "follower_count": video["follower_count"],
                    "reported_comment_count": video["reported_comment_count"],
                    "view_count_status": metric_availability.get("views", ""),
                    "like_count_status": metric_availability.get("likes", ""),
                    "reported_comment_count_status": metric_availability.get("reported_comments", ""),
                    "share_count_status": metric_availability.get("shares", ""),
                    "save_count_status": metric_availability.get("saves", ""),
                    "follower_count_status": metric_availability.get("followers", ""),
                    "transcript_available": video["transcript_available"],
                    "transcript_language": video["transcript_language"],
                    "transcript_is_auto_generated": video["transcript_is_auto_generated"],
                    "transcript_segment_count": video["transcript_segment_count"],
                    "transcript_error": video["transcript_error"],
                    "transcript_status": video["transcript_status"],
                    "transcript_source": video["transcript_source"],
                    "transcript_attempt_count": video["transcript_attempt_count"],
                    "source_key": source_provenance.get("source_key", ""),
                    "source_kind": source_provenance.get("source_kind", ""),
                    "source_value": source_provenance.get("source_value", ""),
                    "source_role": source_provenance.get("source_role", ""),
                    "discovery_method": transport_provenance.get("discovery_method", ""),
                    "metadata_method": transport_provenance.get("metadata_method", ""),
                    "comment_method": transport_provenance.get("comment_method", ""),
                    "dom_used": bool(transport_provenance.get("dom_used")),
                    "fallback_used": bool(transport_provenance.get("fallback_used")),
                    "comment_completion_status": collection_status.get("completion_status", ""),
                    "comments_complete": collection_status.get("comments_complete"),
                    "quality_flags": "|".join(video.get("quality_flags") or []),
                    "comment_index": index,
                    "comment_id": text_value(comment.get("comment_id") or comment.get("id") or comment.get("cid")),
                    "parent_comment_id": parent_id,
                    "author": comment_author(comment),
                    "author_id": comment_author_id(comment),
                    "author_profile_key": author_profile.get("profile_key", ""),
                    "author_profile_status": author_profile.get("status", ""),
                    "author_declared_location": author_geo.get("raw", ""),
                    "author_country_code": author_geo.get("country_code", ""),
                    "author_region": author_geo.get("region", ""),
                    "author_city": author_geo.get("city", ""),
                    "author_self_declared_pronouns": "|".join(
                        author_self_declared.get("pronouns") or []
                    ),
                    "author_self_declared_age_statement": author_self_declared.get(
                        "age_statement", ""
                    ),
                    "text": text_value(comment.get("text")),
                    "likes": comment_likes(comment),
                    "time": normalized_comment_time,
                    "time_raw": raw_comment_time,
                    "time_status": "normalized" if normalized_comment_time else "missing",
                    "reply_count": text_value(comment.get("reply_count")),
                    "is_reply": bool(parent_id or comment.get("is_reply")),
                    "first_seen_at": text_value(comment.get("first_seen_at")),
                    "last_seen_at": text_value(comment.get("last_seen_at")),
                }
            )

    totals: dict[str, int] = {
        "videos": len(videos),
        "videos_with_comments": sum(1 for video in videos if video["comment_count"] > 0),
        "videos_with_transcript": sum(1 for video in videos if video["transcript_available"]),
        "videos_with_published_at": sum(1 for video in videos if video["published_at"]),
        "videos_with_public_engagement": sum(
            1
            for video in videos
            if any(
                text_value((video.get("metric_availability") or {}).get(metric)).startswith("available")
                for metric in ("views", "likes", "reported_comments", "shares", "saves")
            )
        ),
        "videos_with_transcript_error": sum(
            1
            for video in videos
            if video["transcript_status"] not in {"", "ok", "unavailable", "disabled"}
        ),
        "zero_comment_or_error_videos": sum(1 for video in videos if video["comment_count"] == 0),
        "top_level_comments": sum(video["top_level_comment_count"] for video in videos),
        "flat_comments": len(flat_rows),
        "outside_date_videos_excluded": sum(
            value["outside_date_videos_excluded"]
            for value in summary_by_platform.values()
        ),
        "videos_using_api_data": sum(
            bool(video.get("transport_provenance", {}).get("api_data_used")) for video in videos
        ),
        "videos_using_dom": sum(
            bool(video.get("transport_provenance", {}).get("dom_used")) for video in videos
        ),
        "videos_with_truncated_comments": sum(
            video.get("collection_status", {}).get("completion_status") == "truncated"
            for video in videos
        ),
        "videos_with_partial_comments": sum(
            video.get("collection_status", {}).get("completion_status") == "partial"
            for video in videos
        ),
        "videos_with_unknown_comment_completeness": sum(
            video.get("collection_status", {}).get("completion_status") == "unknown"
            for video in videos
        ),
    }
    profiles_coverage = profile_coverage(conn, project)
    totals.update(
        {
            "profiles_queued": profiles_coverage["totals"]["queued"],
            "profiles_available": profiles_coverage["totals"]["profiles_available"],
            "profiles_with_declared_geography": profiles_coverage["totals"][
                "profiles_with_declared_geography"
            ],
            "profiles_with_self_declared_demographic_text": profiles_coverage["totals"][
                "profiles_with_self_declared_demographic_text"
            ],
        }
    )
    summary = {
        "project": project,
        "topic": title,
        "keyword": keyword,
        "compiled_at": iso_now(),
        "window_hours": hours,
        "publication_windows": [
            {
                "name": window["name"],
                "start": window["start_raw"],
                "end": window["end_raw"],
            }
            for window in date_windows
        ],
        "summary_by_platform": summary_by_platform,
        "profile_coverage": profiles_coverage,
        "totals": totals,
    }
    metrics_coverage = build_metric_coverage_report(
        videos,
        project=project,
        compiled_at=summary["compiled_at"],
    )

    readiness_capabilities = (
        "sentiment",
        "entity_analysis",
        "publisher_classification",
        "audience_interest_classification",
        "activation_theme_aggregation",
        "monthly_reporting",
        "report_qa",
        "geography",
        "demographics",
        "reach_and_traffic",
    )
    readiness = {}
    for capability in readiness_capabilities:
        statuses: dict[str, int] = {}
        for video in videos:
            status = text_value(video.get("analysis_readiness", {}).get(capability) or "unknown")
            statuses[status] = statuses.get(status, 0) + 1
        readiness[capability] = statuses

    discovery_methods: dict[str, int] = {}
    comment_methods: dict[str, int] = {}
    for video in videos:
        transport = video.get("transport_provenance") or {}
        discovery_method = text_value(transport.get("discovery_method") or "unknown")
        comment_method = text_value(transport.get("comment_method") or "unknown")
        discovery_methods[discovery_method] = discovery_methods.get(discovery_method, 0) + 1
        comment_methods[comment_method] = comment_methods.get(comment_method, 0) + 1

    brief_readiness = {
        "schema_version": "1.0",
        "project": project,
        "compiled_at": summary["compiled_at"],
        "window_hours": hours,
        "records_assessed": len(videos),
        "comments_assessed": len(flat_rows),
        "capability_status_counts": readiness,
        "transport": {
            "discovery_method_counts": discovery_methods,
            "comment_method_counts": comment_methods,
            "records_using_api_data": totals["videos_using_api_data"],
            "records_using_dom": totals["videos_using_dom"],
        },
        "quality": {
            "records_with_known_publication_time": totals["videos_with_published_at"],
            "records_with_public_engagement": totals["videos_with_public_engagement"],
            "records_with_truncated_comments": totals["videos_with_truncated_comments"],
            "records_with_partial_comments": totals["videos_with_partial_comments"],
            "records_with_unknown_comment_completeness": totals["videos_with_unknown_comment_completeness"],
            "outside_date_records_excluded": totals["outside_date_videos_excluded"],
        },
        "first_party_only": [
            "geography",
            "demographics",
            "reach",
            "impressions",
            "traffic",
            "link_clicks",
            "conversions",
        ],
        "public_profile_evidence": {
            "declared_geography": (
                "Available when a platform exposes structured or self-declared profile location. "
                "It is not audience geography."
            ),
            "self_declared_demographic_text": (
                "Pronouns and explicit age statements may be preserved as text, without "
                "gender, ethnicity, or name/photo-based inference."
            ),
            "coverage": profiles_coverage["totals"],
        },
        "metric_availability": metrics_coverage["overall"]["metrics"],
        "cross_platform_comparability": metrics_coverage["cross_platform_comparability"],
        "interpretation": (
            "Ready means the collected record contains evidence for downstream classification. "
            "It does not prove complete platform-wide recall or analytical accuracy."
        ),
    }

    combined_path = output_dir / "combined_all_platforms_comments.json"
    technical_bundle_path = output_dir / "technical_bundle.json"
    manifest_path = output_dir / "dataset_manifest.json"
    posts_jsonl_path = output_dir / "posts.jsonl"
    comments_jsonl_path = output_dir / "comments.jsonl"
    profiles_jsonl_path = output_dir / "user_profiles.jsonl"
    flat_path = output_dir / "combined_all_platforms_comments_flat.csv"
    summary_path = output_dir / "summary.json"
    readiness_path = output_dir / "brief_readiness.json"
    metrics_path = output_dir / "metrics_coverage.json"
    fieldnames = [
        "platform",
        "video_number",
        "video_id",
        "video_url",
        "content_creator",
        "creator_profile_key",
        "creator_profile_status",
        "creator_declared_location",
        "creator_country_code",
        "creator_region",
        "creator_city",
        "creator_self_declared_pronouns",
        "creator_self_declared_age_statement",
        "caption",
        "description",
        "published_at",
        "content_type",
        "view_count",
        "like_count",
        "share_count",
        "save_count",
        "follower_count",
        "reported_comment_count",
        "view_count_status",
        "like_count_status",
        "reported_comment_count_status",
        "share_count_status",
        "save_count_status",
        "follower_count_status",
        "transcript_available",
        "transcript_language",
        "transcript_is_auto_generated",
        "transcript_segment_count",
        "transcript_error",
        "transcript_status",
        "transcript_source",
        "transcript_attempt_count",
        "source_key",
        "source_kind",
        "source_value",
        "source_role",
        "discovery_method",
        "metadata_method",
        "comment_method",
        "dom_used",
        "fallback_used",
        "comment_completion_status",
        "comments_complete",
        "quality_flags",
        "comment_index",
        "comment_id",
        "parent_comment_id",
        "author",
        "author_id",
        "author_profile_key",
        "author_profile_status",
        "author_declared_location",
        "author_country_code",
        "author_region",
        "author_city",
        "author_self_declared_pronouns",
        "author_self_declared_age_statement",
        "text",
        "likes",
        "time",
        "time_raw",
        "time_status",
        "reply_count",
        "is_reply",
        "first_seen_at",
        "last_seen_at",
    ]
    profile_export = export_profiles(conn, project, output_dir)
    readable_videos = normalized_compiled_videos(videos)
    analysis_config_row = conn.execute(
        "SELECT value FROM project_meta WHERE key = 'analysis_config'"
    ).fetchone()
    try:
        stored_analysis_config = json.loads(analysis_config_row[0]) if analysis_config_row else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        stored_analysis_config = {}
    analysis_config = normalize_analysis_config(stored_analysis_config)
    analysis_export = export_analysis_artifacts(
        conn,
        project,
        output_dir,
        analysis_config,
        content_keys=[video["content_key"] for video in readable_videos],
    )
    analysis_lookup = analysis_export["lookup"]
    for video in readable_videos:
        video["analysis"] = analysis_lookup.get(
            (video["platform"], video["content_key"]),
            {
                "status": "not_queued",
                "analyzed": False,
                "stale": False,
                "score": None,
                "confidence": None,
                "data_completeness": None,
            },
        )
    profile_index = [
        normalized_profile_index(profile)
        for profile in profile_lookup["profiles"]
        if isinstance(profile, dict)
    ]
    artifact_index = {
        "manifest": manifest_path.name,
        "readable_combined": combined_path.name,
        "posts_jsonl": posts_jsonl_path.name,
        "comments_jsonl": comments_jsonl_path.name,
        "profiles_jsonl": profiles_jsonl_path.name,
        "technical_bundle": technical_bundle_path.name,
        "flat_csv": flat_path.name,
        "summary": summary_path.name,
        "brief_readiness": readiness_path.name,
        "metrics_coverage": metrics_path.name,
        "analysis_summary": Path(analysis_export["summary_path"]).name,
        "analysis_queue": Path(analysis_export["analysis_queue_path"]).name,
        "analyses": Path(analysis_export["analyses_path"]).name,
        "publication_queue": Path(analysis_export["publication_queue_path"]).name,
        "profiles": Path(profile_export["profiles_json"]).name,
        "profile_coverage": Path(profile_export["profile_coverage_json"]).name,
    }
    readable_payload = {
        "schema_version": "3.0",
        "dataset": {
            "project": project,
            "topic": title,
            "compiled_at": summary["compiled_at"],
            "window_hours": hours,
            "record_counts": {
                "posts": len(readable_videos),
                "comments": sum(len(video["comments"]) for video in readable_videos),
                "profiles": len(profile_index),
            },
            "ordering": {
                "posts": "published_at_descending",
                "comments": "thread_root_then_chronological_depth_first",
            },
            "analysis": analysis_export["summary"],
            "artifacts": artifact_index,
            "compatibility": {
                "videos_key": "Retained for existing consumers; records may be any social post type.",
                "technical_bundle": (
                    "Contains the previous full-fidelity platform payload, including raw extensions."
                ),
            },
        },
        "summary": summary,
        "videos": readable_videos,
        "profiles": profile_index,
    }
    atomic_json_dump(
        combined_path,
        readable_payload,
    )
    atomic_json_dump(
        technical_bundle_path,
        {
            "summary": summary,
            "profiles": profile_lookup["profiles"],
            "videos": videos,
        },
        compact=True,
    )
    post_rows = [
        {key: value for key, value in video.items() if key != "comments"}
        for video in readable_videos
    ]
    comment_rows = []
    for video in readable_videos:
        for comment in video["comments"]:
            comment_rows.append(
                {
                    "schema_version": "1.0",
                    "project": project,
                    "topic": title,
                    "post_dataset_index": video["dataset_index"],
                    "platform": video["platform"],
                    "video_id": video["video_id"],
                    "video_url": video["url"],
                    "content_creator": video["content_creator"],
                    "source_provenance": video["source_provenance"],
                    **{
                        key: value
                        for key, value in comment.items()
                        if key not in {"schema_version", "platform"}
                    },
                }
            )
    profile_rows = [
        {"project": project, **profile}
        for profile in profile_lookup["profiles"]
        if isinstance(profile, dict)
    ]
    atomic_jsonl_write(posts_jsonl_path, post_rows)
    atomic_jsonl_write(comments_jsonl_path, comment_rows)
    atomic_jsonl_write(profiles_jsonl_path, profile_rows)
    atomic_csv_write(flat_path, fieldnames, flat_rows)
    atomic_json_dump(summary_path, summary)
    atomic_json_dump(readiness_path, brief_readiness)
    atomic_json_dump(metrics_path, metrics_coverage)
    atomic_json_dump(
        manifest_path,
        {
            "schema_version": "1.0",
            "project": project,
            "topic": title,
            "compiled_at": summary["compiled_at"],
            "window_hours": hours,
            "record_counts": readable_payload["dataset"]["record_counts"],
            "ordering": readable_payload["dataset"]["ordering"],
            "primary_keys": {
                "post": ["platform", "content_key"],
                "comment": ["platform", "content_id", "comment_id"],
                "profile": ["platform", "profile_key"],
                "analysis": ["analysis_id"],
                "publication": ["publication_id"],
            },
            "artifacts": {
                "readable_combined": {
                    "file": combined_path.name,
                    "format": "pretty_json",
                    "purpose": "Human-readable normalized posts with threaded comments and a profile index.",
                },
                "posts": {
                    "file": posts_jsonl_path.name,
                    "format": "jsonl",
                    "purpose": "One normalized post per line without embedded comments.",
                },
                "comments": {
                    "file": comments_jsonl_path.name,
                    "format": "jsonl",
                    "purpose": "One normalized comment per line with post and thread identifiers.",
                },
                "profiles": {
                    "file": profiles_jsonl_path.name,
                    "format": "jsonl",
                    "purpose": "One full normalized public profile per line.",
                },
                "technical_bundle": {
                    "file": technical_bundle_path.name,
                    "format": "compact_json",
                    "purpose": "Full-fidelity compatibility bundle with platform-specific raw extensions.",
                },
                "flat_csv": {
                    "file": flat_path.name,
                    "format": "csv",
                    "purpose": "Wide row-per-comment table for spreadsheets and BI tools.",
                },
                "summary": {"file": summary_path.name, "format": "json"},
                "brief_readiness": {"file": readiness_path.name, "format": "json"},
                "metrics_coverage": {"file": metrics_path.name, "format": "json"},
                "analysis_summary": {
                    "file": Path(analysis_export["summary_path"]).name,
                    "format": "json",
                    "purpose": "Analysis state, native-text policy, and queue counts.",
                },
                "analysis_queue": {
                    "file": Path(analysis_export["analysis_queue_path"]).name,
                    "format": "jsonl",
                    "purpose": "Pending evidence packets and prompts for AI or human analysis.",
                },
                "analyses": {
                    "file": Path(analysis_export["analyses_path"]).name,
                    "format": "jsonl",
                    "purpose": "Completed versioned fact-check and scoring records.",
                },
                "publication_queue": {
                    "file": Path(analysis_export["publication_queue_path"]).name,
                    "format": "jsonl",
                    "purpose": "Approval-gated, idempotent public comment drafts and status.",
                },
                "user_profiles": {
                    "file": Path(profile_export["profiles_json"]).name,
                    "format": "pretty_json",
                },
                "profile_coverage": {
                    "file": Path(profile_export["profile_coverage_json"]).name,
                    "format": "json",
                },
            },
            "raw_data_location": "../../../raw_runs" if hours else "../../raw_runs",
            "notes": [
                "Null means unavailable; it must not be interpreted as zero.",
                "Use JSONL artifacts for scalable processing and the technical bundle for raw-field audits.",
                "The readable combined file omits platform-specific raw payload objects by design.",
            ],
        },
    )

    return {
        "output_dir": str(output_dir),
        "combined_json": str(combined_path),
        "technical_bundle_json": str(technical_bundle_path),
        "dataset_manifest_json": str(manifest_path),
        "posts_jsonl": str(posts_jsonl_path),
        "comments_jsonl": str(comments_jsonl_path),
        "profiles_jsonl": str(profiles_jsonl_path),
        "flat_csv": str(flat_path),
        "summary_json": str(summary_path),
        "brief_readiness_json": str(readiness_path),
        "metrics_coverage_json": str(metrics_path),
        "analysis_summary_json": analysis_export["summary_path"],
        "analysis_queue_jsonl": analysis_export["analysis_queue_path"],
        "analyses_jsonl": analysis_export["analyses_path"],
        "publication_queue_jsonl": analysis_export["publication_queue_path"],
        "profiles_json": profile_export["profiles_json"],
        "profiles_csv": profile_export["profiles_csv"],
        "profile_coverage_json": profile_export["profile_coverage_json"],
        **totals,
        "summary_by_platform": summary_by_platform,
    }


def compile_coverage_report(
    conn: sqlite3.Connection,
    project: str,
    paths: dict[str, Path],
) -> dict[str, Any]:
    conn.row_factory = sqlite3.Row
    platforms: dict[str, Any] = {}
    source_rows = conn.execute(
        """
        SELECT * FROM discovery_sources
        WHERE project = ?
        ORDER BY platform, first_run_at, source_key
        """,
        (project,),
    ).fetchall()
    date_windows = project_date_windows(conn, project)

    for platform in ("youtube", "tiktok", "instagram", "facebook", "x"):
        candidates = conn.execute(
            "SELECT * FROM candidate_ledger WHERE project = ? AND platform = ?",
            (project, platform),
        ).fetchall()
        stored_content_rows = conn.execute(
            "SELECT * FROM content_items WHERE project = ? AND platform = ?",
            (project, platform),
        ).fetchall()
        eligible_content_rows, outside_date_content, effective_dates = filter_content_rows_by_date(
            stored_content_rows,
            date_windows,
        )
        eligible_content_keys = {row["content_key"] for row in eligible_content_rows}
        comment_keys = conn.execute(
            "SELECT content_key FROM comments WHERE project = ? AND platform = ?",
            (project, platform),
        ).fetchall()
        stored_comment_count = len(comment_keys)
        comment_count = sum(1 for row in comment_keys if row["content_key"] in eligible_content_keys)
        observation_count = conn.execute(
            "SELECT COUNT(*) FROM candidate_observations WHERE project = ? AND platform = ?",
            (project, platform),
        ).fetchone()[0]

        decisions: dict[str, int] = {}
        extraction: dict[str, int] = {}
        multi_source_candidates = 0
        for candidate in candidates:
            decision = text_value(candidate["last_decision"]) or "unclassified"
            decisions[decision] = decisions.get(decision, 0) + 1
            status = text_value(candidate["extraction_status"]) or "unknown"
            extraction[status] = extraction.get(status, 0) + 1
            try:
                if len(json.loads(candidate["source_keys_json"] or "[]")) > 1:
                    multi_source_candidates += 1
            except (TypeError, json.JSONDecodeError):
                pass

        content_total = len(eligible_content_rows)
        with_published_at = sum(
            1 for row in eligible_content_rows if effective_dates.get(row["content_key"])
        )
        with_creator = sum(1 for row in eligible_content_rows if row["creator"])
        with_caption = sum(1 for row in eligible_content_rows if row["caption"])
        with_engagement = sum(
            1
            for row in eligible_content_rows
            if row["view_count"] or row["like_count"] or row["share_count"]
        )
        with_error = sum(1 for row in eligible_content_rows if row["error"])

        def ratio(value: Any) -> float:
            return round((int(value or 0) / content_total) * 100, 2) if content_total else 0.0

        active_source_rows = [
            row
            for row in source_rows
            if row["platform"] == platform
            and int(row["active"] or 0) == 1
            and row["source_role"] != "refresh"
        ]
        source_run_count = sum(
            1 for row in active_source_rows
            if int(row["run_count"] or 0) > 0 and row["last_status"] != "pending"
        )
        source_success_count = sum(1 for row in active_source_rows if row["last_status"] == "success")
        source_failed_count = sum(1 for row in active_source_rows if row["last_status"] == "failed")
        source_pending_count = sum(1 for row in active_source_rows if row["last_status"] == "pending")
        platform_sources = [
            {
                "source_key": row["source_key"],
                "kind": row["source_kind"],
                "value": row["source_value"],
                "role": row["source_role"],
                "last_status": row["last_status"],
                "run_count": row["run_count"],
                "candidate_count": row["candidate_count"],
                "new_candidate_count": row["new_candidate_count"],
                "marginal_new_ratio": round(
                    (row["new_candidate_count"] / row["candidate_count"]) * 100,
                    2,
                ) if row["candidate_count"] else 0.0,
                "error": row["error"],
            }
            for row in active_source_rows
        ]
        planned_source_count = len(active_source_rows)
        refresh_run_count = sum(
            int(row["run_count"] or 0)
            for row in source_rows
            if row["platform"] == platform and row["source_role"] == "refresh"
        )
        platforms[platform] = {
            "configured_sources": planned_source_count,
            "configured_sources_run": source_run_count,
            "configured_sources_successful": source_success_count,
            "configured_sources_failed": source_failed_count,
            "configured_sources_pending": source_pending_count,
            "source_completion_percent": round(
                (source_run_count / planned_source_count) * 100,
                2,
            ) if planned_source_count else 0.0,
            "direct_refresh_runs": refresh_run_count,
            "candidate_observations": observation_count,
            "unique_candidates": len(candidates),
            "repeat_observations": max(0, observation_count - len(candidates)),
            "multi_source_candidates": multi_source_candidates,
            "deduped_content": content_total,
            "deduped_comments": comment_count,
            "stored_deduped_content": len(stored_content_rows),
            "stored_deduped_comments": stored_comment_count,
            "outside_date_content": outside_date_content,
            "candidate_decisions": decisions,
            "extraction_status": extraction,
            "metadata_completeness_percent": {
                "published_at": ratio(with_published_at),
                "creator": ratio(with_creator),
                "caption": ratio(with_caption),
                "public_engagement": ratio(with_engagement),
            },
            "content_with_error": with_error,
            "sources": platform_sources,
        }

    report = {
        "project": project,
        "generated_at": iso_now(),
        "coverage_contract": (
            "Counts describe observed public results from configured discovery sources. "
            "They are not a claim of complete platform-wide coverage."
        ),
        "platforms": platforms,
    }
    output_path = paths["latest"] / "coverage.json"
    atomic_json_dump(output_path, report)
    return {"path": str(output_path), **report}


def run_platform_scraper(
    args: argparse.Namespace,
    project_paths_map: dict[str, Path],
    platform: str,
    known_content_file: Path | None = None,
    source: dict[str, Any] | None = None,
    candidate_file: Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    from campaign_spec import source_slug

    timestamp = now_local().strftime("%Y%m%d_%H%M%S_%f")
    source = source or {
        "source_key": stable_hash(platform, "query", args.primary_keyword),
        "kind": "query",
        "value": args.primary_keyword,
        "label": args.primary_keyword,
        "role": "legacy",
        "target_mode": "search",
    }
    source_name = source_slug(source)
    run_id = f"{platform}_{source_name}_{timestamp}"
    resume_path = None
    if platform == "facebook" and not args.no_resume_incomplete and source.get("role") == "legacy":
        resume_path = incomplete_raw_run(project_paths_map, platform)

    if resume_path:
        session_name = str(resume_path.relative_to(COMMENTS_ROOT))
        run_id = f"{platform}_{source_name}_{timestamp}_resume"
        print(f"[INFO] Resuming incomplete Facebook raw run: {resume_path}", flush=True)
    else:
        session_name = str(
            Path(project_paths_map["root"].name)
            / "raw_runs"
            / f"{platform}_{source_name}_{timestamp}"
        )

    command = [
        sys.executable,
        "run_scraper.py",
        "--platform",
        platform,
        "--videos",
        str(args.videos),
        "--discovery-videos",
        str(args.discovery_videos),
        "--comments",
        str(args.comments),
        "--session-name",
        session_name,
        "--transport-mode",
        args.transport_mode,
        "--source-key",
        text_value(source.get("source_key")),
        "--source-kind",
        text_value(source.get("kind")),
        "--source-value",
        text_value(source.get("value")),
        "--source-label",
        text_value(source.get("label")),
        "--source-role",
        text_value(source.get("role")),
        "--source-target-mode",
        text_value(source.get("target_mode")),
        "--source-taxonomy-json",
        json.dumps(source.get("taxonomy") or {}, ensure_ascii=False),
    ]
    if args.campaign_spec_digest:
        command.extend(["--campaign-spec-digest", args.campaign_spec_digest])
    if args.relevance_profile:
        command.extend(["--relevance-profile", args.relevance_profile])
    if source.get("kind") != "refresh" and args.relevance_mode and args.relevance_mode != "off":
        command.extend(["--relevance-mode", args.relevance_mode])
        command.extend(["--relevance-review-action", args.relevance_review_action])
    if args.relevance_accept_threshold:
        command.extend(["--relevance-accept-threshold", str(args.relevance_accept_threshold)])
    if args.relevance_review_threshold:
        command.extend(["--relevance-review-threshold", str(args.relevance_review_threshold)])
    if known_content_file:
        command.extend(["--known-content-file", str(known_content_file)])

    source_value = text_value(source.get("value") or args.primary_keyword)
    if candidate_file:
        command.extend(["--candidate-file", str(candidate_file)])
    elif source.get("target_mode") == "url":
        command.extend(["--url", source_value])
    elif platform == "instagram":
        command.extend(["--keywords", source_value])
    else:
        command.extend(["--search", source_value])

    if platform == "tiktok":
        if args.tiktok_user_data_dir:
            command.extend(["--tiktok-user-data-dir", args.tiktok_user_data_dir])
        if args.tiktok_profile_directory:
            command.extend(["--tiktok-profile-directory", args.tiktok_profile_directory])
        if args.tiktok_browser_channel:
            command.extend(["--tiktok-browser-channel", args.tiktok_browser_channel])
        if args.tiktok_cdp_url:
            command.extend(["--tiktok-cdp-url", args.tiktok_cdp_url])
        if args.tiktok_cookie_curl:
            command.extend(["--tiktok-cookie-curl", args.tiktok_cookie_curl])
        if args.tiktok_require_auth:
            command.append("--tiktok-require-auth")
        if args.tiktok_transcript_langs:
            command.extend(["--tiktok-transcript-langs", args.tiktok_transcript_langs])
        if args.tiktok_transcript_timeout is not None:
            command.extend(["--tiktok-transcript-timeout", str(args.tiktok_transcript_timeout)])
        if args.tiktok_transcript_retries is not None:
            command.extend(["--tiktok-transcript-retries", str(args.tiktok_transcript_retries)])
        if args.no_tiktok_transcripts:
            command.append("--no-tiktok-transcripts")
    if platform == "instagram":
        if args.per_keyword_videos is not None:
            command.extend(["--per-keyword-videos", str(args.per_keyword_videos)])
        if args.instagram_storage_state:
            command.extend(["--instagram-storage-state", args.instagram_storage_state])
        if args.instagram_user_data_dir:
            command.extend(["--instagram-user-data-dir", args.instagram_user_data_dir])
        if args.instagram_profile_directory:
            command.extend(["--instagram-profile-directory", args.instagram_profile_directory])
        if args.instagram_browser_channel:
            command.extend(["--instagram-browser-channel", args.instagram_browser_channel])
        if args.instagram_cdp_url:
            command.extend(["--instagram-cdp-url", args.instagram_cdp_url])
        if args.instagram_headed:
            command.append("--instagram-headed")
        if args.instagram_search_rounds:
            command.extend(["--instagram-search-rounds", str(args.instagram_search_rounds)])
        if args.instagram_stall_rounds:
            command.extend(["--instagram-stall-rounds", str(args.instagram_stall_rounds)])
    if platform == "youtube":
        if args.youtube_transcript_method:
            command.extend(["--youtube-transcript-method", args.youtube_transcript_method])
        if args.youtube_transcript_langs:
            command.extend(["--youtube-transcript-langs", args.youtube_transcript_langs])
        if args.youtube_transcript_delay is not None:
            command.extend(["--youtube-transcript-delay", str(args.youtube_transcript_delay)])
        if args.youtube_transcript_retries is not None:
            command.extend(["--youtube-transcript-retries", str(args.youtube_transcript_retries)])
        if args.youtube_transcript_cooldown is not None:
            command.extend(["--youtube-transcript-cooldown", str(args.youtube_transcript_cooldown)])
        if args.no_youtube_transcripts:
            command.append("--no-youtube-transcripts")
    if platform == "facebook":
        if args.facebook_storage_state:
            command.extend(["--facebook-storage-state", args.facebook_storage_state])
        if args.facebook_user_data_dir:
            command.extend(["--facebook-user-data-dir", args.facebook_user_data_dir])
        if args.facebook_profile_directory:
            command.extend(["--facebook-profile-directory", args.facebook_profile_directory])
        if args.facebook_browser_channel:
            command.extend(["--facebook-browser-channel", args.facebook_browser_channel])
        if args.facebook_cdp_url:
            command.extend(["--facebook-cdp-url", args.facebook_cdp_url])
        if args.facebook_headed:
            command.append("--facebook-headed")
        if args.facebook_extraction_mode:
            command.extend(["--facebook-extraction-mode", args.facebook_extraction_mode])
        if args.facebook_search_rounds:
            command.extend(["--facebook-search-rounds", str(args.facebook_search_rounds)])
        if args.facebook_stall_rounds:
            command.extend(["--facebook-stall-rounds", str(args.facebook_stall_rounds)])
    if platform == "x":
        if args.x_extraction_mode:
            command.extend(["--x-extraction-mode", args.x_extraction_mode])
        if args.x_storage_state:
            command.extend(["--x-storage-state", args.x_storage_state])
        if args.x_user_data_dir:
            command.extend(["--x-user-data-dir", args.x_user_data_dir])
        if args.x_profile_directory:
            command.extend(["--x-profile-directory", args.x_profile_directory])
        if args.x_browser_channel:
            command.extend(["--x-browser-channel", args.x_browser_channel])
        if args.x_cdp_url:
            command.extend(["--x-cdp-url", args.x_cdp_url])
        if args.x_headed:
            command.append("--x-headed")
        if args.x_search_rounds:
            command.extend(["--x-search-rounds", str(args.x_search_rounds)])
        if args.x_stall_rounds:
            command.extend(["--x-stall-rounds", str(args.x_stall_rounds)])
        if args.x_comment_rounds:
            command.extend(["--x-comment-rounds", str(args.x_comment_rounds)])
        if args.x_comment_stall_rounds:
            command.extend(["--x-comment-stall-rounds", str(args.x_comment_stall_rounds)])
        if args.x_bearer_token_file:
            command.extend(["--x-bearer-token-file", args.x_bearer_token_file])
        if args.x_search_mode:
            command.extend(["--x-search-mode", args.x_search_mode])
        x_since_id = text_value(source.get("since_id") or args.x_since_id)
        if x_since_id:
            command.extend(["--x-since-id", x_since_id])
        if args.x_api_read_limit > 0:
            command.extend(["--x-api-read-limit", str(args.x_api_read_limit)])
        if args.x_max_rate_limit_wait is not None:
            command.extend(["--x-max-rate-limit-wait", str(args.x_max_rate_limit_wait)])

    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    if args.campaign_timezone:
        env["SCRAPER_TIMEZONE"] = args.campaign_timezone
    if args.date_windows_json:
        env["SCRAPER_DATE_WINDOWS_JSON"] = args.date_windows_json
    else:
        env.pop("SCRAPER_DATE_WINDOWS_JSON", None)
    if platform == "tiktok" and not args.tiktok_cdp_url:
        env.setdefault("TIKTOK_FORCE_PLAYWRIGHT", "1")
    if platform == "instagram":
        env.setdefault("INSTAGRAM_API_FIRST", "1")
        env.setdefault("INSTAGRAM_DIRECT_ONLY", "1")

    started_at = iso_now()
    session_path = COMMENTS_ROOT / session_name
    log_path = project_paths_map["logs"] / f"{run_id}.log"
    print(f"[INFO] Running {platform}: {' '.join(command)}", flush=True)
    attach_retry_count = 0
    max_attach_retries = max(0, int(getattr(args, "browser_attach_retries", 1)))
    retry_delay = max(0.0, float(getattr(args, "browser_attach_retry_delay", 3.0)))
    while True:
        mode = "a" if attach_retry_count else "w"
        with log_path.open(mode, encoding="utf-8") as log:
            if attach_retry_count:
                marker = f"\n[ORCHESTRATOR] Browser attach retry {attach_retry_count}/{max_attach_retries}\n"
                log.write(marker)
                log.flush()
                print(marker, end="", flush=True)
            proc = subprocess.Popen(
                command,
                cwd=Path.cwd(),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                errors="replace",
            )
            try:
                if proc.stdout is not None:
                    for line in proc.stdout:
                        log.write(line)
                        log.flush()
                        print(f"[{platform}] {line}", end="", flush=True)
                returncode = proc.wait()
            except KeyboardInterrupt:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                raise
        if returncode == 0:
            break
        if attach_retry_count >= max_attach_retries or not is_transient_cdp_attach_failure(log_path):
            break
        attach_retry_count += 1
        print(
            f"[WARN] {platform} hit a transient shared-browser attach collision; "
            f"retrying in {retry_delay:g}s ({attach_retry_count}/{max_attach_retries})",
            flush=True,
        )
        if retry_delay:
            time.sleep(retry_delay)
    finished_at = iso_now()
    status = "success" if returncode == 0 else "failed"
    print(f"[INFO] {platform} finished with status={status}; log={log_path}", flush=True)
    return session_path, {
        "run_id": run_id,
        "platform": platform,
        "started_at": started_at,
        "finished_at": finished_at,
        "status": status,
        "returncode": returncode,
        "log_path": str(log_path),
        "known_content_file": str(known_content_file) if known_content_file else "",
        "candidate_file": str(candidate_file) if candidate_file else "",
        "source": source,
        "browser_attach_retry_count": attach_retry_count,
        "error": "" if status == "success" else f"returncode={returncode}",
    }


def run_and_ingest_platform(
    args: argparse.Namespace,
    conn: sqlite3.Connection,
    paths: dict[str, Path],
    platform: str,
    source: dict[str, Any] | None = None,
    candidate_file: Path | None = None,
    archive_manager: Any | None = None,
) -> dict[str, Any]:
    source = dict(source or {
        "source_key": stable_hash(platform, "query", args.primary_keyword),
        "kind": "query",
        "value": args.primary_keyword,
        "label": args.primary_keyword,
        "role": "legacy",
        "target_mode": "search",
    })
    source_value = text_value(source.get("value") or args.keywords)
    if (
        platform == "x"
        and source.get("kind") != "refresh"
        and not candidate_file
        and not args.x_since_id
    ):
        state_row = conn.execute(
            "SELECT since_id FROM platform_state WHERE project = ? AND platform = ? AND keyword = ?",
            (args.project, platform, source_value),
        ).fetchone()
        if state_row and text_value(state_row[0]).strip():
            source["since_id"] = text_value(state_row[0]).strip()
    known_content_file = None
    if not args.rescrape_known and source.get("kind") != "refresh" and not candidate_file:
        known_content_file = export_known_content_file(
            conn,
            args.project,
            platform,
            paths,
            refresh_limit=0 if source.get("role") != "legacy" else args.refresh_known_limit,
            refresh_after_hours=args.refresh_known_after_hours,
        )
    session_path, run_meta = run_platform_scraper(
        args,
        paths,
        platform,
        known_content_file=known_content_file,
        source=source,
        candidate_file=candidate_file,
    )
    conn.execute(
        """
        INSERT OR REPLACE INTO runs (
            run_id, project, platform, keyword, session_path, started_at,
            finished_at, status, source_key, source_kind, source_value,
            source_role, error
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_meta["run_id"],
            args.project,
            platform,
            source_value,
            str(session_path),
            run_meta["started_at"],
            run_meta["finished_at"],
            run_meta["status"],
            text_value(source.get("source_key")),
            text_value(source.get("kind")),
            source_value,
            text_value(source.get("role")),
            "" if run_meta["status"] == "success" else f"returncode={run_meta['returncode']}",
        ),
    )
    conn.commit()
    if run_meta["status"] != "success":
        discovery_stats = record_discovery_run(
            conn,
            project=args.project,
            platform=platform,
            source=source,
            run_meta=run_meta,
            candidates=[],
        )
        archive_result: dict[str, Any] = {}
        if (
            archive_manager
            and archive_manager.config.archive_raw_runs
            and archive_manager.config.raw_run_archive_timing == "immediate"
            and session_path.exists()
        ):
            try:
                archive_result = archive_manager.archive_raw_run(
                    session_path,
                    run_id=run_meta["run_id"],
                    platform=platform,
                    run_metadata=run_meta,
                    allow_delete=False,
                )
            except Exception as exc:
                archive_result = {"status": "failed", "error": str(exc)}
                if archive_manager.config.required:
                    raise
        return {**run_meta, **discovery_stats, "ingested": False, "archive": archive_result}

    videos = load_session_videos(session_path, platform)
    run_candidates = collect_run_candidates(session_path, platform, videos)
    x_since_id = max_x_post_id(run_candidates) if platform == "x" else ""
    discovery_stats = record_discovery_run(
        conn,
        project=args.project,
        platform=platform,
        source=source,
        run_meta=run_meta,
        candidates=run_candidates,
    )
    if not args.skip_ingestion:
        stats = ingest_videos(
            conn,
            project=args.project,
            keyword=source_value,
            platform=platform,
            videos=videos,
            run_id=run_meta["run_id"],
            scraped_at=run_meta["finished_at"],
        )
    else:
        print(f"[INFO] Skipping ingestion for {platform} due to --skip-ingestion", flush=True)
        stats = {
            "discovered_count": len(videos),
            "new_content_count": 0,
            "new_comment_count": 0,
            "total_comment_count": 0,
            "queued_profile_count": 0,
        }
    conn.execute(
        """
        UPDATE runs SET discovered_count = ?, new_content_count = ?,
            new_comment_count = ?, total_comment_count = ?
        WHERE run_id = ?
        """,
        (
            stats["discovered_count"],
            stats["new_content_count"],
            stats["new_comment_count"],
            stats["total_comment_count"],
            run_meta["run_id"],
        ),
    )
    update_platform_state(
        conn,
        args.project,
        platform,
        source_value,
        run_meta["run_id"],
        run_meta["finished_at"],
        since_id=x_since_id,
    )
    conn.commit()
    print(f"[INFO] Ingested {platform}: {stats}", flush=True)
    archive_result = {}
    if (
        archive_manager
        and archive_manager.config.archive_raw_runs
        and archive_manager.config.raw_run_archive_timing == "immediate"
    ):
        try:
            archive_result = archive_manager.archive_raw_run(
                session_path,
                run_id=run_meta["run_id"],
                platform=platform,
                run_metadata={**run_meta, **discovery_stats, **stats},
                allow_delete=True,
            )
            print(f"[INFO] Archived {platform} raw run: {archive_result}", flush=True)
        except Exception as exc:
            archive_result = {"status": "failed", "error": str(exc)}
            if archive_manager.config.required:
                raise
            print(f"[WARN] Drive archival failed for {run_meta['run_id']}: {exc}", flush=True)
    return {**run_meta, **discovery_stats, **stats, "ingested": True, "archive": archive_result}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run incremental project scrape and regenerate reports.")
    parser.add_argument("--skip-ingestion", action="store_true", help="Skip SQLite ingestion of scraped videos (defer until later).")
    parser.add_argument("--campaign-spec", default="", help="Reusable campaign specification JSON.")
    parser.add_argument("--project", default="", help="Project name under comments_data/project_<name>.")
    parser.add_argument("--keywords", default="", help="Keyword list. Use comma/semicolon/pipe/newline separators.")
    parser.add_argument("--candidate-file", type=str, default="", help="Path to a candidates JSON file to deeply scrape.")
    parser.add_argument("--platform", choices=["youtube", "tiktok", "instagram", "facebook", "x", "all"], default="all")
    parser.add_argument(
        "--transport-mode",
        choices=["api-first", "api-only", "hybrid", "dom"],
        default=None,
        help="Transport policy passed to every platform worker.",
    )
    parser.add_argument("--videos", type=int, help="Maximum posts per discovery source. 0 = unlimited.")
    parser.add_argument(
        "--discovery-videos",
        type=int,
        help="Candidate metadata records per source before gates. 0/omitted = auto when posts are capped.",
    )
    parser.add_argument("--comments", type=int, help="Maximum comments per post where supported. 0 = unlimited.")
    parser.add_argument(
        "--profile-enrichment",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enrich queued creator/comment-author profiles before report compilation.",
    )
    parser.add_argument(
        "--profile-limit",
        type=int,
        default=None,
        help="Maximum due profiles to enrich in this run.",
    )
    parser.add_argument(
        "--profile-cache-days",
        type=int,
        default=None,
        help="Refresh successful public profiles after this many days.",
    )
    parser.add_argument(
        "--profile-browser-fallback",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Use the configured social-browser CDP session when an official profile API is unavailable.",
    )
    parser.add_argument(
        "--profile-cdp-url",
        default="",
        help="Optional CDP URL dedicated to profile enrichment; defaults to the shared social browser.",
    )
    parser.add_argument(
        "--analysis",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Build versioned native-text analysis tasks after collection.",
    )
    parser.add_argument(
        "--analysis-mode",
        choices=["off", "shadow", "review", "publish"],
        default="",
        help="Analysis workflow mode. Shadow mode never publishes.",
    )
    parser.add_argument(
        "--analysis-provider",
        choices=["none", "manual", "codex", "gemini"],
        default="",
        help="AI provider. manual/codex exports tasks without making model API calls.",
    )
    parser.add_argument("--analysis-model", default="", help="Optional provider model name.")
    parser.add_argument(
        "--analysis-limit",
        type=int,
        default=None,
        help="Maximum pending posts sent to an AI provider in this run.",
    )
    parser.add_argument(
        "--analysis-import",
        default="",
        help="JSON or JSONL analysis results to validate and import after queue synchronization.",
    )
    parser.add_argument(
        "--publication-mode",
        choices=["disabled", "shadow", "capture", "live"],
        default="",
        help="Outbound queue policy. No live publication adapter is enabled by this option alone.",
    )
    parser.add_argument("--max-sources-per-platform", type=int, default=0, help="Optional campaign source cap for diagnostics.")
    parser.add_argument("--per-keyword-videos", type=int, default=None)
    parser.add_argument(
        "--social-browser",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Use the saved designated social browser. Browser-backed platform runs "
            "enable and start it automatically unless --no-social-browser is supplied."
        ),
    )
    parser.add_argument(
        "--social-browser-state",
        default=str(Path("comments_data") / "social_browser" / "state.json"),
        help="State file written by social_browser.py start.",
    )
    parser.add_argument(
        "--social-browser-cdp-url",
        default="",
        help="Shared CDP URL for TikTok, Instagram, Facebook, and X.",
    )
    parser.add_argument(
        "--social-browser-startup-timeout",
        type=float,
        default=60.0,
        help="Seconds allowed to attach and verify the designated Edge profile.",
    )
    parser.add_argument(
        "--browser-attach-retries",
        type=int,
        default=1,
        help="Retries for the known transient Playwright shared-browser attach collision.",
    )
    parser.add_argument(
        "--browser-attach-retry-delay",
        type=float,
        default=3.0,
        help="Seconds to wait before retrying a transient shared-browser attachment.",
    )
    parser.add_argument("--tiktok-user-data-dir", default="")
    parser.add_argument("--tiktok-profile-directory", default="")
    parser.add_argument("--tiktok-browser-channel", default="msedge")
    parser.add_argument("--tiktok-cdp-url", default="")
    parser.add_argument("--tiktok-cookie-curl", default="")
    parser.add_argument("--tiktok-require-auth", action="store_true")
    parser.add_argument("--tiktok-transcript-langs", default="")
    parser.add_argument("--tiktok-transcript-timeout", type=float)
    parser.add_argument("--tiktok-transcript-retries", type=int)
    parser.add_argument("--no-tiktok-transcripts", action="store_true")
    parser.add_argument(
        "--youtube-transcript-method",
        choices=["api", "auto", "innertube", "timedtext", "watch"],
        default="api",
    )
    parser.add_argument("--youtube-transcript-langs", default="")
    parser.add_argument("--youtube-transcript-delay", type=float)
    parser.add_argument("--youtube-transcript-retries", type=int)
    parser.add_argument("--youtube-transcript-cooldown", type=float)
    parser.add_argument("--no-youtube-transcripts", action="store_true")
    parser.add_argument("--instagram-storage-state", default="")
    parser.add_argument("--instagram-user-data-dir", default="")
    parser.add_argument("--instagram-profile-directory", default="")
    parser.add_argument("--instagram-browser-channel", default="msedge")
    parser.add_argument("--instagram-cdp-url", default="")
    parser.add_argument("--instagram-headed", action="store_true")
    parser.add_argument("--instagram-search-rounds", type=int, default=0)
    parser.add_argument("--instagram-stall-rounds", type=int, default=0)
    parser.add_argument("--facebook-storage-state", default="")
    parser.add_argument("--facebook-user-data-dir", default="")
    parser.add_argument("--facebook-profile-directory", default="")
    parser.add_argument("--facebook-browser-channel", default="msedge")
    parser.add_argument("--facebook-cdp-url", default="")
    parser.add_argument("--facebook-headed", action="store_true")
    parser.add_argument("--facebook-extraction-mode", choices=["hybrid", "browser-api", "dom"], default="browser-api")
    parser.add_argument("--facebook-search-rounds", type=int, default=0)
    parser.add_argument("--facebook-stall-rounds", type=int, default=0)
    parser.add_argument("--x-extraction-mode", choices=["browser-api", "official"], default="browser-api")
    parser.add_argument("--x-storage-state", default="")
    parser.add_argument("--x-user-data-dir", default="")
    parser.add_argument("--x-profile-directory", default="")
    parser.add_argument("--x-browser-channel", default="msedge")
    parser.add_argument("--x-cdp-url", default="")
    parser.add_argument("--x-headed", action="store_true")
    parser.add_argument("--x-search-rounds", type=int, default=0)
    parser.add_argument("--x-stall-rounds", type=int, default=0)
    parser.add_argument("--x-comment-rounds", type=int, default=0)
    parser.add_argument("--x-comment-stall-rounds", type=int, default=0)
    parser.add_argument("--x-bearer-token-file", default="")
    parser.add_argument("--x-search-mode", choices=["recent", "all"], default="recent")
    parser.add_argument("--x-since-id", default="")
    parser.add_argument("--x-api-read-limit", type=int, default=1000)
    parser.add_argument("--x-max-rate-limit-wait", type=float, default=60.0)
    parser.add_argument("--relevance-profile", default="", help="JSON topic profile for pre-comment candidate filtering.")
    parser.add_argument("--relevance-mode", choices=["off", "audit", "filter"])
    parser.add_argument("--relevance-review-action", choices=["skip", "scrape"])
    parser.add_argument("--relevance-accept-threshold", type=int)
    parser.add_argument("--relevance-review-threshold", type=int)
    parser.add_argument("--bootstrap-combined", action="append", default=[], help="Existing combined JSON to seed the project store.")
    parser.add_argument("--report-hours", help="Comma-separated rolling report windows in hours.")
    parser.add_argument("--compile-only", action="store_true", help="Do not run scrapers; only regenerate project reports.")
    parser.add_argument("--rescrape-known", action="store_true", help="Do not skip already-known posts before scraping comments.")
    parser.add_argument(
        "--refresh-known-limit",
        type=int,
        help="Refresh comments on up to this many recently discovered known posts per platform (default: 20).",
    )
    parser.add_argument(
        "--refresh-known-after-hours",
        type=float,
        help="Only schedule a known post refresh after this many hours since its last scrape (default: 6).",
    )
    parser.add_argument("--no-direct-refresh", action="store_true", help="Skip campaign direct-URL refresh batches.")
    parser.add_argument("--no-resume-incomplete", action="store_true", help="Start a fresh Facebook raw run even if the latest one is incomplete.")
    parser.add_argument("--archive-provider", choices=["none", "google-drive"], default=None)
    parser.add_argument("--drive-credentials", default="", help="Desktop OAuth client JSON file.")
    parser.add_argument("--drive-token", default="", help="OAuth token cache file.")
    parser.add_argument("--drive-account-hint", default="", help="Expected Drive email or account label.")
    parser.add_argument("--drive-mount-path", default="", help="Optional Drive for desktop mount check, such as I:\\My Drive.")
    parser.add_argument("--drive-root-folder-name", default="")
    parser.add_argument("--drive-root-folder-id", default="")
    parser.add_argument("--drive-chunk-size-mb", type=int)
    parser.add_argument("--drive-max-retries", type=int)
    parser.add_argument("--drive-archive-raw-runs", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--drive-archive-backlog", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--drive-raw-run-archive-timing", choices=["immediate", "final"], default="")
    parser.add_argument("--drive-upload-compiled", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--drive-upload-logs", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--drive-snapshot-database", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument(
        "--drive-delete-local-after-upload",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Delete a raw run only after every file and its manifest are checksum-verified on Drive.",
    )
    parser.add_argument("--drive-required", action=argparse.BooleanOptionalAction, default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    campaign = None
    campaign_spec_path = None
    campaign_profile_path = None
    if args.campaign_spec:
        from campaign_spec import (
            campaign_keywords,
            campaign_spec_digest,
            expand_campaign_sources,
            load_campaign_spec,
            normalize_analysis_config,
        )

        campaign = load_campaign_spec(args.campaign_spec)
        args.campaign_spec_digest = campaign_spec_digest(campaign)
        args.campaign_timezone = campaign["timezone"]
        os.environ["SCRAPER_TIMEZONE"] = args.campaign_timezone
        args.date_windows_json = json.dumps(campaign["date_windows"], ensure_ascii=False)
        args.project = args.project or campaign["project"]
        args.keywords = args.keywords or ",".join(campaign_keywords(campaign))
        args.videos = campaign["collection"]["videos_per_source"] if args.videos is None else args.videos
        args.discovery_videos = (
            campaign["collection"]["discovery_candidates_per_source"]
            if args.discovery_videos is None
            else args.discovery_videos
        )
        args.comments = campaign["collection"]["comments_per_post"] if args.comments is None else args.comments
        profile_config = campaign.get("profiles") or {}
        args.profile_enrichment = (
            bool(profile_config.get("enabled", False))
            if args.profile_enrichment is None
            else args.profile_enrichment
        )
        args.profile_limit = (
            int(profile_config.get("limit_per_run", 100))
            if args.profile_limit is None
            else args.profile_limit
        )
        args.profile_cache_days = (
            int(profile_config.get("cache_days", 14))
            if args.profile_cache_days is None
            else args.profile_cache_days
        )
        args.profile_browser_fallback = (
            bool(profile_config.get("browser_fallback", True))
            if args.profile_browser_fallback is None
            else args.profile_browser_fallback
        )
        args.transport_mode = args.transport_mode or campaign["collection"]["transport_mode"]
        args.report_hours = args.report_hours or ",".join(str(value) for value in campaign["collection"]["report_hours"])
        args.refresh_known_limit = (
            campaign["collection"]["refresh_limit_per_platform"]
            if args.refresh_known_limit is None
            else args.refresh_known_limit
        )
        args.refresh_known_after_hours = (
            campaign["collection"]["refresh_after_hours"]
            if args.refresh_known_after_hours is None
            else args.refresh_known_after_hours
        )
        args.relevance_mode = args.relevance_mode or campaign["relevance"]["mode"]
        args.relevance_review_action = args.relevance_review_action or campaign["relevance"]["review_action"]
        args.relevance_accept_threshold = (
            campaign["relevance"]["accept_threshold"]
            if args.relevance_accept_threshold is None
            else args.relevance_accept_threshold
        )
        args.relevance_review_threshold = (
            campaign["relevance"]["review_threshold"]
            if args.relevance_review_threshold is None
            else args.relevance_review_threshold
        )
        analysis_payload = json.loads(json.dumps(campaign.get("analysis") or {}))
        if args.analysis is not None:
            analysis_payload["enabled"] = args.analysis
        if args.analysis_mode:
            analysis_payload["mode"] = args.analysis_mode
        if args.analysis_provider:
            analysis_payload["provider"] = args.analysis_provider
        if args.analysis_model:
            analysis_payload["model"] = args.analysis_model
        if args.analysis_limit is not None:
            analysis_payload["max_posts_per_run"] = args.analysis_limit
        if args.publication_mode:
            analysis_payload.setdefault("publication", {})["mode"] = args.publication_mode
        args.analysis_config = normalize_analysis_config(analysis_payload)
    else:
        from campaign_spec import normalize_analysis_config

        args.campaign_spec_digest = ""
        args.campaign_timezone = os.environ.get("SCRAPER_TIMEZONE", "")
        args.date_windows_json = ""
        args.videos = 0 if args.videos is None else args.videos
        args.discovery_videos = 0 if args.discovery_videos is None else args.discovery_videos
        args.comments = 0 if args.comments is None else args.comments
        args.profile_enrichment = (
            False if args.profile_enrichment is None else args.profile_enrichment
        )
        args.profile_limit = 100 if args.profile_limit is None else args.profile_limit
        args.profile_cache_days = (
            14 if args.profile_cache_days is None else args.profile_cache_days
        )
        args.profile_browser_fallback = (
            True
            if args.profile_browser_fallback is None
            else args.profile_browser_fallback
        )
        args.transport_mode = args.transport_mode or "api-first"
        args.report_hours = args.report_hours or "6,12"
        args.refresh_known_limit = 20 if args.refresh_known_limit is None else args.refresh_known_limit
        args.refresh_known_after_hours = 6.0 if args.refresh_known_after_hours is None else args.refresh_known_after_hours
        args.relevance_mode = args.relevance_mode or "off"
        args.relevance_review_action = args.relevance_review_action or "skip"
        args.relevance_accept_threshold = args.relevance_accept_threshold or 0
        args.relevance_review_threshold = args.relevance_review_threshold or 0
        args.analysis_config = normalize_analysis_config({
            "enabled": bool(args.analysis or args.analysis_import),
            "mode": args.analysis_mode or "shadow",
            "provider": args.analysis_provider or "manual",
            "model": args.analysis_model,
            "max_posts_per_run": 20 if args.analysis_limit is None else args.analysis_limit,
            "publication": {
                "mode": args.publication_mode or "shadow",
                "require_approval": True,
            },
        })

    if args.social_browser is None:
        args.social_browser = social_browser_is_required(args, campaign)
    shared_cdp_url = ""
    if args.social_browser:
        shared_cdp_url = ensure_social_browser_session(args)
        args.social_browser_cdp_url = shared_cdp_url
        apply_shared_cdp_url(args, shared_cdp_url)

    if args.discovery_videos <= 0 and args.videos > 0:
        args.discovery_videos = max(20, args.videos * 5)

    if not args.project:
        raise SystemExit("--project is required unless provided by --campaign-spec")
    if not args.keywords:
        raise SystemExit("--keywords is required unless provided by --campaign-spec")

    keywords = split_keywords(args.keywords)
    args.primary_keyword = keywords[0] if keywords else args.keywords
    paths = project_paths(args.project)
    db_file = db_path(args.project)
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    if campaign:
        campaign_spec_path, campaign_profile_path = write_campaign_state(campaign, paths)
        if not args.relevance_profile:
            args.relevance_profile = str(campaign_profile_path)
    conn.execute(
        "INSERT OR REPLACE INTO project_meta(key, value) VALUES ('project', ?), ('keywords', ?)",
        (args.project, json.dumps(keywords, ensure_ascii=False)),
    )
    conn.execute(
        "INSERT OR REPLACE INTO project_meta(key, value) VALUES ('analysis_config', ?)",
        (json.dumps(args.analysis_config, ensure_ascii=False),),
    )
    conn.commit()
    if campaign:
        from campaign_spec import campaign_spec_digest

        conn.execute(
            "INSERT OR REPLACE INTO project_meta(key, value) VALUES ('campaign_spec', ?), ('campaign_spec_digest', ?), ('date_windows', ?), ('timezone', ?)",
            (
                str(campaign_spec_path),
                campaign_spec_digest(campaign),
                json.dumps(campaign["date_windows"], ensure_ascii=False),
                campaign["timezone"],
            ),
        )
        conn.commit()

    from tiktok_scraper.storage.google_drive import (
        DriveArchiveConfig,
        DriveArchiveError,
        GoogleDriveArchiveManager,
    )

    drive_config = DriveArchiveConfig.from_sources(
        vars(args),
        campaign.get("storage") if campaign else {},
        cwd=Path.cwd(),
    )
    archive_manager = None
    archive_startup: dict[str, Any] = {"configuration": drive_config.public_dict()}
    if drive_config.enabled:
        archive_manager = GoogleDriveArchiveManager(
            drive_config,
            conn,
            args.project,
            paths["root"].name,
            paths,
        )
        try:
            archive_startup["connection"] = archive_manager.initialize(interactive=False)
            if (
                drive_config.archive_raw_runs
                and drive_config.archive_backlog
                and drive_config.raw_run_archive_timing == "immediate"
            ):
                print("[INFO] Reconciling completed local raw runs with Google Drive...", flush=True)
                archive_startup["backlog"] = archive_manager.archive_successful_backlog()
        except DriveArchiveError as exc:
            archive_startup["error"] = str(exc)
            if drive_config.required:
                raise SystemExit(f"Google Drive archival is required but unavailable: {exc}") from exc
            print(f"[WARN] Google Drive archival disabled for this run: {exc}", flush=True)
            archive_manager = None

    bootstrap_results = {}
    for raw_path in args.bootstrap_combined:
        path = Path(raw_path)
        marker = f"bootstrap:{path.resolve()}"
        existing = conn.execute("SELECT value FROM project_meta WHERE key = ?", (marker,)).fetchone()
        if existing:
            print(f"[INFO] Bootstrap already ingested: {path}", flush=True)
            continue
        print(f"[INFO] Bootstrapping existing combined file: {path}", flush=True)
        bootstrap_results[str(path)] = bootstrap_combined(conn, args.project, args.keywords, path)
        conn.execute("INSERT OR REPLACE INTO project_meta(key, value) VALUES (?, ?)", (marker, iso_now()))
        conn.commit()

    from tiktok_scraper.profile_enrichment import backfill_profile_queue

    profile_queue_backfill = backfill_profile_queue(conn, args.project)

    run_results = []
    if not args.compile_only:
        platforms = selected_platforms(args, campaign)
        if campaign:
            sources = expand_campaign_sources(campaign, selected_platforms=platforms)
            if args.max_sources_per_platform > 0:
                limited_sources = []
                source_counts: dict[str, int] = {}
                for source in sources:
                    count = source_counts.get(source["platform"], 0)
                    if count >= args.max_sources_per_platform:
                        continue
                    source_counts[source["platform"]] = count + 1
                    limited_sources.append(source)
                sources = limited_sources
            source_plan = register_source_plan(
                conn,
                args.project,
                sources,
                campaign_spec_digest(campaign),
            )
            print(f"[INFO] Registered campaign source plan: {source_plan}", flush=True)
            for index, source in enumerate(sources, start=1):
                print(
                    f"[INFO] Campaign source {index}/{len(sources)}: "
                    f"{source['platform']} {source['kind']} {source['value']}",
                    flush=True,
                )
                run_results.append(
                    run_and_ingest_platform(
                        args,
                        conn,
                        paths,
                        source["platform"],
                        source=source,
                        archive_manager=archive_manager,
                    )
                )

            if not args.no_direct_refresh:
                for platform in platforms:
                    candidate_file, candidate_count = export_direct_refresh_candidates(
                        conn,
                        args.project,
                        platform,
                        paths,
                        limit=args.refresh_known_limit,
                        after_hours=args.refresh_known_after_hours,
                    )
                    if not candidate_file:
                        continue
                    refresh_source = {
                        "platform": platform,
                        "kind": "refresh",
                        "value": f"direct_refresh:{candidate_count}",
                        "label": f"Direct refresh ({candidate_count})",
                        "role": "refresh",
                        "target_mode": "batch",
                        "source_key": stable_hash(platform, "refresh", str(candidate_file)),
                    }
                    run_results.append(
                        run_and_ingest_platform(
                            args,
                            conn,
                            paths,
                            platform,
                            source=refresh_source,
                            candidate_file=candidate_file,
                            archive_manager=archive_manager,
                        )
                    )
        else:
            for platform in platforms:
                run_results.append(
                    run_and_ingest_platform(
                        args,
                        conn,
                        paths,
                        platform,
                        candidate_file=Path(args.candidate_file) if args.candidate_file else None,
                        archive_manager=archive_manager,
                    )
                )

    profile_enrichment_result: dict[str, Any] = {"status": "disabled"}
    if args.profile_enrichment:
        import asyncio

        from tiktok_scraper.profile_enrichment import enrich_queued_profiles

        profile_cdp_url = args.profile_cdp_url.strip() or shared_cdp_url
        print(
            f"[INFO] Enriching up to {max(0, args.profile_limit)} queued profiles...",
            flush=True,
        )
        try:
            profile_enrichment_result = asyncio.run(
                enrich_queued_profiles(
                    conn,
                    args.project,
                    limit=max(0, args.profile_limit),
                    cache_days=max(1, args.profile_cache_days),
                    cdp_url=profile_cdp_url,
                    browser_fallback=args.profile_browser_fallback,
                )
            )
            print(
                f"[INFO] Profile enrichment: {profile_enrichment_result}",
                flush=True,
            )
        except Exception as exc:
            profile_enrichment_result = {
                "status": "failed",
                "error": str(exc),
            }
            print(
                f"[WARN] Profile enrichment failed; report compilation will continue: {exc}",
                flush=True,
            )

    from tiktok_scraper.analysis_workflow import (
        import_analysis_results,
        queue_publication_drafts,
        run_analysis_workflow,
    )

    if args.analysis_import:
        args.analysis_config["enabled"] = True
    analysis_workflow_result = run_analysis_workflow(
        conn,
        args.project,
        args.analysis_config,
    )
    if args.analysis_import:
        import_result = import_analysis_results(
            conn,
            args.project,
            Path(args.analysis_import),
            args.analysis_config,
            provider=args.analysis_provider or "manual",
            model=args.analysis_model,
        )
        publication_result = queue_publication_drafts(
            conn,
            args.project,
            args.analysis_config,
        )
        analysis_workflow_result["import"] = import_result
        analysis_workflow_result["publication_after_import"] = publication_result

    latest = compile_outputs(conn, args.project, args.keywords, paths, hours=None)
    coverage = compile_coverage_report(conn, args.project, paths)
    reports = {}
    for value in split_keywords(args.report_hours):
        try:
            hours = int(value)
        except ValueError:
            continue
        if hours > 0:
            reports[f"last_{hours}h"] = compile_outputs(conn, args.project, args.keywords, paths, hours=hours)

    archive_final: dict[str, Any] = {}
    if archive_manager:
        try:
            if (
                drive_config.archive_raw_runs
                and drive_config.archive_backlog
                and drive_config.raw_run_archive_timing == "final"
            ):
                print("[INFO] Archiving completed local raw runs with Google Drive...", flush=True)
                archive_final["raw_runs"] = archive_manager.archive_successful_backlog()
            archive_final["outputs"] = archive_manager.archive_outputs()
            if drive_config.snapshot_database:
                archive_final["database_snapshot"] = archive_manager.archive_database_snapshot(db_file)
            archive_final["summary"] = archive_manager.status_summary()
        except Exception as exc:
            archive_final["error"] = str(exc)
            if drive_config.required:
                raise
            print(f"[WARN] Final Drive archival failed: {exc}", flush=True)

    output = {
        "project": args.project,
        "project_dir": str(paths["root"]),
        "database": str(db_file),
        "bootstrap_results": bootstrap_results,
        "profile_queue_backfill": profile_queue_backfill,
        "run_results": run_results,
        "profile_enrichment": profile_enrichment_result,
        "analysis_workflow": analysis_workflow_result,
        "latest": latest,
        "coverage": coverage,
        "reports": reports,
        "campaign_spec": str(campaign_spec_path) if campaign_spec_path else "",
        "campaign_relevance_profile": str(campaign_profile_path) if campaign_profile_path else "",
        "archive": {"startup": archive_startup, "final": archive_final},
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
