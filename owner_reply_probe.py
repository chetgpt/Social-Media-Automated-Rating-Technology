"""CLI for the public owner-reply schema bootstrap."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

from tiktok_scraper.owner_reply_probe import OwnerReplyProbe
from tiktok_scraper.author_activity_trace import SUPPORTED_PLATFORMS


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Open a target's public profile, inventory its own posts, retain "
            "replies authored by the target, and report the public "
            "comment-request schema without storing credentials."
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
        help="Username, account ID, or public profile URL.",
    )
    parser.add_argument("--username", default="")
    parser.add_argument("--user-id", default="")
    parser.add_argument("--cdp-url", default="")
    parser.add_argument("--browser-channel", default="msedge")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--max-posts", type=int, default=5)
    parser.add_argument("--max-comments-per-post", type=int, default=200)
    parser.add_argument("--discovery-timeout", type=float, default=90)
    parser.add_argument("--post-timeout", type=float, default=180)
    parser.add_argument(
        "--trace-other-posts",
        action="store_true",
        help=(
            "After the bootstrap, pass learned public IDs into the existing "
            "live other-post author tracer."
        ),
    )
    parser.add_argument("--trace-max-posts", type=int, default=10)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    probe = OwnerReplyProbe(
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
        trace_other_posts=args.trace_other_posts,
        trace_max_posts=args.trace_max_posts,
    )
    payload = asyncio.run(probe.run())
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    sys.stdout.buffer.write(rendered.encode("utf-8", "replace"))
    if payload.get("status") == "credentials_required":
        return 3
    return 2 if payload.get("status") == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
