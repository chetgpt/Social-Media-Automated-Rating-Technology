"""Known-content filtering for incremental scraping.

The project database is the source of truth for posts we have already seen.
This module keeps platform-specific ID/url matching in one place so candidate
filtering happens before expensive comment scraping.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import parse_qs, urlparse


def text_value(value: Any) -> str:
    return "" if value is None else str(value).strip()


def extract_instagram_shortcode(url: str) -> str:
    match = re.search(r"instagram\.com/(?:p|reel|tv)/([^/?#]+)/?", url or "")
    return match.group(1) if match else ""


def extract_tiktok_video_id(url: str) -> str:
    match = re.search(r"/video/(\d+)", url or "")
    return match.group(1) if match else ""


def extract_youtube_video_id(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if "youtu.be" in host:
        return parsed.path.strip("/").split("/")[0]
    query_id = parse_qs(parsed.query).get("v", [""])[0]
    if query_id:
        return query_id
    match = re.search(r"/(?:shorts|embed|live)/([^/?#]+)", parsed.path)
    return match.group(1) if match else ""


def extract_facebook_content_id(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    for key in ("v", "story_fbid", "fbid"):
        value = query.get(key, [""])[0]
        if value:
            return value
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    lowered = [part.lower() for part in parts]
    for marker in ("reel", "posts", "permalink"):
        if marker in lowered:
            index = lowered.index(marker)
            if index + 1 < len(parts):
                return parts[index + 1]
    if "videos" in lowered:
        index = lowered.index("videos")
        tail = parts[index + 1:]
        for part in reversed(tail):
            if re.fullmatch(r"\d{5,}", part):
                return part
        if tail:
            return tail[0]
    match = re.search(r"/groups/[^/]+/posts/([^/?#]+)", url)
    if match:
        return match.group(1)
    return ""


def extract_x_post_id(url: str) -> str:
    match = re.search(r"/(?:i/web/)?status(?:es)?/(\d+)", url or "", re.I)
    return match.group(1) if match else ""


def normalize_url(url: str) -> str:
    text = text_value(url)
    if not text:
        return ""
    parsed = urlparse(text)
    if not parsed.scheme or not parsed.netloc:
        return text.rstrip("/")
    return parsed._replace(query="", fragment="").geturl().rstrip("/")


def add_key(keys: list[str], seen: set[str], value: Any) -> None:
    key = text_value(value)
    if key and key not in seen:
        seen.add(key)
        keys.append(key)


def candidate_content_keys(platform: str, candidate: dict[str, Any]) -> list[str]:
    """Return stable key variants that can identify a candidate post/video."""
    keys: list[str] = []
    seen: set[str] = set()

    platform = (platform or "").lower()
    url = text_value(
        candidate.get("url")
        or candidate.get("video_url")
        or candidate.get("permalink")
        or candidate.get("link")
    )
    normalized_url = normalize_url(url)
    url_content_id = ""
    normalized_content_id = ""

    if platform == "instagram":
        url_content_id = extract_instagram_shortcode(url)
        normalized_content_id = extract_instagram_shortcode(normalized_url)
        add_key(keys, seen, url_content_id)
        add_key(keys, seen, normalized_content_id)
        add_key(keys, seen, candidate.get("shortcode"))
        add_key(keys, seen, candidate.get("code"))
    elif platform == "youtube":
        url_content_id = extract_youtube_video_id(url)
        normalized_content_id = extract_youtube_video_id(normalized_url)
        add_key(keys, seen, url_content_id)
        add_key(keys, seen, normalized_content_id)
    elif platform == "tiktok":
        url_content_id = extract_tiktok_video_id(url)
        normalized_content_id = extract_tiktok_video_id(normalized_url)
        add_key(keys, seen, url_content_id)
        add_key(keys, seen, normalized_content_id)
    elif platform == "facebook":
        url_content_id = extract_facebook_content_id(url)
        normalized_content_id = extract_facebook_content_id(normalized_url)
        add_key(keys, seen, url_content_id)
        add_key(keys, seen, normalized_content_id)
    elif platform == "x":
        url_content_id = extract_x_post_id(url)
        normalized_content_id = extract_x_post_id(normalized_url)
        add_key(keys, seen, url_content_id)
        add_key(keys, seen, normalized_content_id)

    add_key(keys, seen, candidate.get("content_key"))
    add_key(keys, seen, candidate.get("video_id"))
    add_key(keys, seen, candidate.get("media_id"))
    add_key(keys, seen, candidate.get("id"))
    add_key(keys, seen, candidate.get("aweme_id"))
    add_key(keys, seen, candidate.get("item_id"))

    # Query-based content URLs such as YouTube watch?v=... and Facebook
    # story.php?story_fbid=... collapse to a shared collection path when the
    # query is stripped. That path must never identify an individual post.
    if not url_content_id or normalized_content_id:
        add_key(keys, seen, normalized_url)
    add_key(keys, seen, url)
    return keys


def load_known_content_file(path: str | Path = "") -> tuple[set[str], dict[str, Any]]:
    if not path:
        return set(), {}

    file_path = Path(path)
    if not file_path.exists():
        return set(), {"path": str(file_path), "missing": True}

    with file_path.open("r", encoding="utf-8-sig") as handle:
        payload = json.load(handle)

    keys: set[str] = set()
    if isinstance(payload, dict):
        for key in payload.get("keys", []):
            text = text_value(key)
            if text:
                keys.add(text)
        meta = {key: value for key, value in payload.items() if key != "keys"}
        meta["path"] = str(file_path)
        return keys, meta

    if isinstance(payload, list):
        for key in payload:
            text = text_value(key)
            if text:
                keys.add(text)
    return keys, {"path": str(file_path)}


def known_content_from_env() -> tuple[set[str], dict[str, Any]]:
    return load_known_content_file(os.environ.get("SCRAPER_KNOWN_CONTENT_FILE", ""))


def filter_new_candidates(
    candidates: list[dict[str, Any]],
    platform: str,
    known_keys: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    if not known_keys:
        return candidates, [], []

    fresh: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []

    for candidate in candidates:
        keys = candidate_content_keys(platform, candidate)
        matched = [key for key in keys if key in known_keys]
        audit_item = {
            "decision": "skip_known" if matched else "scrape_new",
            "matched_keys": matched,
            "candidate_keys": keys,
            "candidate": {
                "video_id": candidate.get("video_id") or candidate.get("id") or candidate.get("aweme_id"),
                "media_id": candidate.get("media_id"),
                "url": candidate.get("url") or candidate.get("video_url"),
                "title": candidate.get("title") or candidate.get("caption") or candidate.get("desc") or "",
                "source": candidate.get("username") or candidate.get("content_creator") or candidate.get("author") or "",
            },
        }
        candidate["_known_content"] = {
            "decision": audit_item["decision"],
            "matched_keys": matched,
            "candidate_keys": keys,
        }
        audit.append(audit_item)
        if matched:
            skipped.append(candidate)
        else:
            fresh.append(candidate)

    return fresh, skipped, audit


def write_known_content_audit(
    folder: str | Path,
    platform: str,
    audit: list[dict[str, Any]],
    meta: dict[str, Any] | None = None,
) -> str:
    output_dir = Path(folder)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"known_content_{platform}_candidates.json"
    summary = {"scrape_new": 0, "skip_known": 0}
    for item in audit:
        summary[item["decision"]] = summary.get(item["decision"], 0) + 1
    payload = {
        "platform": platform,
        "summary": summary,
        "known_content": meta or {},
        "candidates": audit,
    }
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    return str(path)
