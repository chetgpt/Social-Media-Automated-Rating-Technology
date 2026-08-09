#!/usr/bin/env python
"""Backfill and run the canonical public-profile enrichment stage."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sqlite3

from incremental_project import db_path, init_db, project_paths
from tiktok_scraper.profile_enrichment import (
    backfill_profile_queue,
    enrich_queued_profiles,
    export_profiles,
)


def _cdp_url(explicit: str, state_path: str, browser_fallback: bool) -> str:
    if explicit.strip() or not browser_fallback:
        return explicit.strip()
    path = Path(state_path)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return str(state.get("cdp_url") or "").strip() if isinstance(state, dict) else ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Queue existing creators/comment authors, enrich due public profiles, "
            "and regenerate profile exports."
        )
    )
    parser.add_argument("--project", required=True)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--cache-days", type=int, default=14)
    parser.add_argument(
        "--platform",
        action="append",
        choices=["youtube", "tiktok", "instagram", "facebook", "x"],
        default=[],
        help="Optional repeatable platform filter.",
    )
    parser.add_argument("--cdp-url", default="")
    parser.add_argument(
        "--social-browser-state",
        default=str(Path("comments_data") / "social_browser" / "state.json"),
    )
    parser.add_argument(
        "--browser-fallback",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument(
        "--force-backfill",
        action="store_true",
        help="Re-scan stored content and comments even if the one-time backfill marker exists.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    database = db_path(args.project)
    if not database.exists():
        raise SystemExit(f"Project database not found: {database}")

    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    backfill = backfill_profile_queue(
        conn,
        args.project,
        force=args.force_backfill,
    )
    result = asyncio.run(
        enrich_queued_profiles(
            conn,
            args.project,
            limit=max(0, args.limit),
            cache_days=max(1, args.cache_days),
            platforms=args.platform,
            cdp_url=_cdp_url(
                args.cdp_url,
                args.social_browser_state,
                args.browser_fallback,
            ),
            browser_fallback=args.browser_fallback,
        )
    )
    exports = export_profiles(
        conn,
        args.project,
        project_paths(args.project)["latest"],
    )
    conn.close()
    print(
        json.dumps(
            {
                "project": args.project,
                "database": str(database),
                "backfill": backfill,
                "enrichment": result,
                "exports": exports,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
