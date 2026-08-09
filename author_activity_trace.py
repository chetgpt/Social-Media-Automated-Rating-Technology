"""CLI for live author-first public comment discovery."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

from tiktok_scraper.author_activity_trace import (
    AuthorActivityTracer,
    SUPPORTED_PLATFORMS,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Start from a username, account ID, or profile URL; search the "
            "live platform; and retain comments or replies authored by that "
            "account under other users' posts."
        )
    )
    parser.add_argument(
        "--platform",
        required=True,
        choices=sorted(SUPPORTED_PLATFORMS),
    )
    parser.add_argument(
        "--target",
        required=True,
        help="Username, account ID label, or profile URL.",
    )
    parser.add_argument(
        "--username",
        default="",
        help="Explicit username when --target is an account ID.",
    )
    parser.add_argument(
        "--user-id",
        default="",
        help="Explicit stable account ID when already known.",
    )
    parser.add_argument(
        "--cdp-url",
        default="",
        help="Optional existing browser CDP endpoint.",
    )
    parser.add_argument("--browser-channel", default="msedge")
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Show the browser instead of launching headless.",
    )
    parser.add_argument(
        "--max-posts",
        type=int,
        default=20,
        help=(
            "Maximum search-discovered posts or timeline entries. "
            "Zero requests all available pagination."
        ),
    )
    parser.add_argument(
        "--max-comments-per-post",
        type=int,
        default=0,
        help=(
            "Maximum comments and replies per candidate post. "
            "Zero requests complete pagination."
        ),
    )
    parser.add_argument("--max-search-pages", type=int, default=3)
    parser.add_argument("--discovery-timeout", type=float, default=90)
    parser.add_argument("--post-timeout", type=float, default=180)
    parser.add_argument(
        "--x-search-mode",
        choices=("recent", "all"),
        default="recent",
    )
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    tracer = AuthorActivityTracer(
        platform=args.platform,
        target=args.target,
        username=args.username,
        user_id=args.user_id,
        cdp_url=args.cdp_url,
        browser_channel=args.browser_channel,
        headless=not args.headed,
        max_posts=args.max_posts,
        max_comments_per_post=args.max_comments_per_post,
        discovery_timeout_seconds=args.discovery_timeout,
        post_timeout_seconds=args.post_timeout,
        max_search_pages=args.max_search_pages,
        x_search_mode=args.x_search_mode,
    )
    payload = asyncio.run(tracer.run())
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    sys.stdout.buffer.write(rendered.encode("utf-8", "replace"))
    if payload.get("status") == "credentials_required":
        return 3
    return 2 if payload.get("status") == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
