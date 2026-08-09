"""Trace target-authored comments from a username-discovered public post pool."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

from tiktok_scraper.username_comment_trace import TikTokUsernameCommentTracer


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Use a TikTok username as the primary pre-discovery seed, scan the "
            "resulting live public comment pools, and retain every exact "
            "target-author match under other users' posts."
        )
    )
    parser.add_argument("--username", required=True)
    parser.add_argument("--cdp-url", default="http://127.0.0.1:9234")
    parser.add_argument("--max-search-pages", type=int, default=3)
    parser.add_argument("--max-posts", type=int, default=20)
    parser.add_argument(
        "--max-comments-per-post",
        type=int,
        default=0,
        help="Zero requests complete pagination; positive values impose a cap.",
    )
    parser.add_argument("--max-comment-pages", type=int, default=100)
    parser.add_argument("--max-reply-pages", type=int, default=100)
    parser.add_argument("--request-timeout", type=float, default=20)
    parser.add_argument("--navigation-timeout", type=float, default=45)
    parser.add_argument("--discovery-timeout", type=float, default=90)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    tracer = TikTokUsernameCommentTracer(
        username=args.username,
        cdp_url=args.cdp_url,
        max_search_pages=args.max_search_pages,
        max_posts=args.max_posts,
        max_comments_per_post=args.max_comments_per_post,
        max_comment_pages=args.max_comment_pages,
        max_reply_pages=args.max_reply_pages,
        request_timeout_seconds=args.request_timeout,
        navigation_timeout_seconds=args.navigation_timeout,
        discovery_timeout_seconds=args.discovery_timeout,
    )
    payload = asyncio.run(tracer.run())
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    sys.stdout.buffer.write(rendered.encode("utf-8", "replace"))
    return 2 if payload.get("status") == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
