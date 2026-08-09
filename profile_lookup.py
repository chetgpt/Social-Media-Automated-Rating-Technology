"""Collect normalized public geo/demographic fields for explicit profile targets.

The command uses official APIs when configured, then the dedicated authenticated
social browser to observe the same first-party responses and visible About
fields that a signed-in viewer can legitimately access. It never stores raw
response bodies, cookies, or inferred demographics.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
from pathlib import Path
import sys
from typing import Any
from urllib.parse import parse_qs, urlparse

from tiktok_scraper.profile_contract import (
    SUPPORTED_PLATFORMS,
    canonical_profile_url,
    normalize_platform,
    normalize_user_id,
    normalize_username,
    profile_key,
    text_value,
)
from tiktok_scraper.profile_enrichment import (
    ProfileCollectionError,
    PublicProfileCollector,
)


HOST_PLATFORMS = {
    "tiktok.com": "tiktok",
    "instagram.com": "instagram",
    "x.com": "x",
    "twitter.com": "x",
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "facebook.com": "facebook",
}

RESERVED_PROFILE_PATHS = {
    "facebook": {
        "about",
        "business",
        "events",
        "groups",
        "help",
        "marketplace",
        "pages",
        "reel",
        "reels",
        "search",
        "share",
        "watch",
    },
    "instagram": {
        "accounts",
        "direct",
        "explore",
        "p",
        "reel",
        "reels",
        "stories",
    },
    "tiktok": {"discover", "foryou", "live", "search", "tag"},
    "x": {
        "compose",
        "explore",
        "home",
        "i",
        "messages",
        "notifications",
        "search",
        "settings",
    },
}


def _platform_from_host(hostname: str) -> str:
    host = hostname.casefold().split(":", 1)[0]
    if host.startswith("www."):
        host = host[4:]
    for domain, platform in HOST_PLATFORMS.items():
        if host == domain or host.endswith(f".{domain}"):
            return platform
    return ""


def candidate_from_url(value: str) -> dict[str, Any]:
    parsed = urlparse(value.strip())
    platform = _platform_from_host(parsed.netloc)
    if platform not in SUPPORTED_PLATFORMS:
        raise ValueError(f"Unsupported profile URL: {value}")
    parts = [part for part in parsed.path.split("/") if part]
    username = ""
    user_id = ""

    if platform == "youtube":
        if len(parts) >= 2 and parts[0].casefold() == "channel":
            user_id = parts[1]
        elif parts and parts[0].startswith("@"):
            username = parts[0][1:]
        elif len(parts) >= 2 and parts[0].casefold() in {"c", "user"}:
            username = parts[1]
    elif platform == "facebook" and parts:
        if parts[0].casefold() == "profile.php":
            user_id = text_value(parse_qs(parsed.query).get("id", [""])[0])
        elif parts[0].casefold() not in RESERVED_PROFILE_PATHS["facebook"]:
            username = parts[0]
    elif parts and parts[0].casefold() not in RESERVED_PROFILE_PATHS.get(
        platform, set()
    ):
        username = parts[0].lstrip("@")

    username = normalize_username(platform, username)
    user_id = normalize_user_id(platform, user_id)
    key = profile_key(platform, user_id, username)
    if not key:
        raise ValueError(f"URL does not identify a supported profile: {value}")
    return {
        "platform": platform,
        "profile_key": key,
        "user_id": user_id,
        "username": username,
        "profile_url": canonical_profile_url(platform, username, user_id),
    }


def candidate_from_target(value: str) -> dict[str, Any]:
    target = value.strip()
    if target.startswith(("http://", "https://")):
        return candidate_from_url(target)
    if ":" not in target:
        raise ValueError(
            "Target must be PLATFORM:IDENTIFIER or a supported profile URL"
        )
    raw_platform, identity = target.split(":", 1)
    platform = normalize_platform(raw_platform)
    identity = identity.strip()
    if platform not in SUPPORTED_PLATFORMS:
        raise ValueError(f"Unsupported platform: {raw_platform}")
    if not identity:
        raise ValueError(f"Missing identity for {platform}")
    if identity.startswith(("http://", "https://")):
        candidate = candidate_from_url(identity)
        if candidate["platform"] != platform:
            raise ValueError(
                f"Target platform {platform} does not match URL platform "
                f"{candidate['platform']}"
            )
        return candidate

    normalized_identity = identity.lstrip("@").strip().rstrip("/")
    user_id = ""
    username = ""
    if platform == "youtube" and normalized_identity.startswith("UC"):
        user_id = normalize_user_id(platform, normalized_identity)
    elif platform == "facebook" and normalized_identity.isdigit():
        user_id = normalize_user_id(platform, normalized_identity)
    else:
        username = normalize_username(platform, normalized_identity)
    key = profile_key(platform, user_id, username)
    profile_url = canonical_profile_url(platform, username, user_id)
    if not key or not profile_url:
        raise ValueError(
            f"Identity cannot be navigated for {platform}: {identity}"
        )
    return {
        "platform": platform,
        "profile_key": key,
        "user_id": user_id,
        "username": username,
        "profile_url": profile_url,
    }


def candidate_from_mapping(value: dict[str, Any]) -> dict[str, Any]:
    profile_url = text_value(value.get("profile_url") or value.get("url"))
    if profile_url:
        candidate = candidate_from_url(profile_url)
        requested_platform = normalize_platform(value.get("platform"))
        if requested_platform and requested_platform != candidate["platform"]:
            raise ValueError(
                f"Mapped platform {requested_platform} does not match URL "
                f"platform {candidate['platform']}"
            )
    else:
        platform = normalize_platform(value.get("platform"))
        user_id = normalize_user_id(platform, value.get("user_id"))
        username = normalize_username(
            platform,
            value.get("username") or value.get("handle"),
        )
        key = profile_key(platform, user_id, username)
        profile_url = canonical_profile_url(platform, username, user_id)
        if platform not in SUPPORTED_PLATFORMS or not key or not profile_url:
            raise ValueError(f"Invalid target mapping: {value}")
        candidate = {
            "platform": platform,
            "profile_key": key,
            "user_id": user_id,
            "username": username,
            "profile_url": profile_url,
        }
    if value.get("display_name"):
        candidate["display_name"] = text_value(value.get("display_name"))
    return candidate


def candidates_from_file(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        payload = payload.get("targets")
    if not isinstance(payload, list):
        raise ValueError("Target file must be a JSON array or {'targets': [...]} object")
    candidates: list[dict[str, Any]] = []
    for item in payload:
        if isinstance(item, str):
            candidates.append(candidate_from_target(item))
        elif isinstance(item, dict):
            candidates.append(candidate_from_mapping(item))
        else:
            raise ValueError(f"Unsupported target entry: {item!r}")
    return candidates


def dedupe_candidates(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for candidate in values:
        marker = (
            text_value(candidate.get("platform")),
            text_value(candidate.get("profile_key")),
        )
        if marker in seen:
            continue
        seen.add(marker)
        output.append(candidate)
    return output


async def collect_targets(
    candidates: list[dict[str, Any]],
    *,
    cdp_url: str,
    timeout: float,
    browser_fallback: bool,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    async with PublicProfileCollector(
        cdp_url=cdp_url,
        browser_fallback=browser_fallback,
        timeout_seconds=timeout,
    ) as collector:
        for candidate in candidates:
            identity = {
                key: candidate.get(key)
                for key in (
                    "platform",
                    "profile_key",
                    "user_id",
                    "username",
                    "profile_url",
                )
            }
            try:
                profile = await asyncio.wait_for(
                    collector.collect(candidate),
                    timeout=max(30.0, timeout * 4),
                )
            except asyncio.TimeoutError:
                results.append(
                    {
                        "target": identity,
                        "status": "timeout",
                        "error": (
                            "Profile collection exceeded the whole-operation "
                            f"timeout of {max(30.0, timeout * 4):g} seconds"
                        ),
                        "diagnostics": list(collector.last_diagnostics),
                    }
                )
            except ProfileCollectionError as exc:
                results.append(
                    {
                        "target": identity,
                        "status": exc.status,
                        "error": str(exc),
                        "diagnostics": list(collector.last_diagnostics),
                    }
                )
            except Exception as exc:
                results.append(
                    {
                        "target": identity,
                        "status": "error",
                        "error": str(exc),
                        "diagnostics": list(collector.last_diagnostics),
                    }
                )
            else:
                results.append(
                    {
                        "target": identity,
                        "status": "available",
                        "profile": profile,
                        "diagnostics": list(collector.last_diagnostics),
                    }
                )
    return {
        "schema_version": "1.0",
        "generated_at": dt.datetime.now()
        .astimezone()
        .replace(microsecond=0)
        .isoformat(),
        "target_count": len(candidates),
        "available_count": sum(
            result.get("status") == "available" for result in results
        ),
        "privacy_scope": (
            "public_or_account_visible_profile_fields_only; "
            "no_name_photo_or_hidden_field_demographic_inference"
        ),
        "results": results,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Navigate explicit social profiles and collect normalized public "
            "geo/demographic evidence."
        )
    )
    parser.add_argument(
        "--target",
        action="append",
        default=[],
        help=(
            "Repeatable PLATFORM:IDENTIFIER or profile URL, for example "
            "instagram:openai or youtube:UC..."
        ),
    )
    parser.add_argument(
        "--targets-file",
        type=Path,
        help="JSON array of target strings or profile identity objects.",
    )
    parser.add_argument("--cdp-url", default="http://127.0.0.1:9223")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument(
        "--no-browser-fallback",
        action="store_true",
        help="Use configured official APIs only.",
    )
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        candidates = [candidate_from_target(value) for value in args.target]
        if args.targets_file:
            candidates.extend(candidates_from_file(args.targets_file))
        candidates = dedupe_candidates(candidates)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if not candidates:
        print("ERROR: provide --target or --targets-file", file=sys.stderr)
        return 2
    payload = asyncio.run(
        collect_targets(
            candidates,
            cdp_url=args.cdp_url,
            timeout=max(5.0, args.timeout),
            browser_fallback=not args.no_browser_fallback,
        )
    )
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    sys.stdout.buffer.write(rendered.encode("utf-8", "replace"))
    return 0 if payload["available_count"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
