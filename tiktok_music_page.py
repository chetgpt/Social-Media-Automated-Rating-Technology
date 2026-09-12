#!/usr/bin/env python
"""Offline TikTok sound-link utilities for the local master registry.

This command never starts a browser, contacts TikTok, downloads media, or
writes the master database.  It is not a canonical MUSIC AUDIT source.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any, Sequence

from tiktok_scraper.music_page_locator import (
    TikTokMusicPageURLValueError,
    build_tiktok_music_page_locator,
    normalize_tiktok_music_page_url,
)


SCHEMA_VERSION = "tiktok-music-page-local-lookup-v1"
DEFAULT_MASTER_DATABASE = (
    Path("comments_data") / "tiktok_master" / "state" / "tiktok_master.sqlite"
)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _readonly_connection(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"master database not found: {resolved}")
    connection = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tiktok_master_posts'"
    ).fetchone()
    if table is None:
        connection.close()
        raise RuntimeError("master database is missing tiktok_master_posts")
    return connection


def inspect_local_master(
    *,
    url: str,
    master_database: Path = DEFAULT_MASTER_DATABASE,
    limit: int = 50,
) -> dict[str, Any]:
    target = normalize_tiktok_music_page_url(url)
    bounded_limit = max(1, min(int(limit), 500))
    connection = _readonly_connection(master_database)
    try:
        predicate = (
            "json_valid(latest_evidence_json) "
            "AND CAST(json_extract(latest_evidence_json, "
            "'$.music_evidence.platform_music.music_id') AS TEXT)=?"
        )
        known_count = int(
            connection.execute(
                f"SELECT COUNT(*) FROM tiktok_master_posts WHERE {predicate}",
                (target["music_id"],),
            ).fetchone()[0]
        )
        rows = connection.execute(
            f"""
            SELECT post_id, canonical_url, creator_handle, last_seen_at,
                   latest_evidence_hash,
                   json_extract(latest_evidence_json,
                     '$.music_evidence.platform_music.title') AS music_title,
                   json_extract(latest_evidence_json,
                     '$.music_evidence.platform_music.author') AS music_author
            FROM tiktok_master_posts
            WHERE {predicate}
            ORDER BY last_seen_at DESC, post_id
            LIMIT ?
            """,
            (target["music_id"], bounded_limit),
        ).fetchall()
    finally:
        connection.close()
    posts = [
        {
            "post_id": str(row["post_id"] or ""),
            "canonical_url": str(row["canonical_url"] or ""),
            "creator": str(row["creator_handle"] or ""),
            "last_seen_at": str(row["last_seen_at"] or ""),
            "evidence_hash": str(row["latest_evidence_hash"] or ""),
            "music_title": str(row["music_title"] or ""),
            "music_author": str(row["music_author"] or ""),
        }
        for row in rows
    ]
    document = {
        "schema_version": SCHEMA_VERSION,
        "status": "matched" if known_count else "not_found",
        "music_page": target,
        "scope": "workspace_master_registry_only",
        "online_lookup_performed": False,
        "master_write_performed": False,
        "known_post_count": known_count,
        "returned_post_count": len(posts),
        "result_limit": bounded_limit,
        "truncated": known_count > len(posts),
        "posts": posts,
        "limitations": [
            "Results include only posts already stored in this workspace master registry.",
            "The TikTok sound ID is not proof of a commercial master recording.",
            "The sound page was not resolved online.",
        ],
        "ai_actions": [],
        "outbound_actions": [],
    }
    document["lookup_hash"] = _hash(document)
    return document


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Parse TikTok sound links and query local known-post evidence."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    normalize = commands.add_parser("normalize", help="Normalize one exact sound URL offline.")
    normalize.add_argument("--url", required=True)
    derive = commands.add_parser("derive", help="Derive a non-verified locator from music fields.")
    derive.add_argument("--music-id", required=True)
    derive.add_argument("--title", default="")
    inspect = commands.add_parser(
        "inspect-local",
        help="Find already-known master-registry posts with this exact sound ID.",
    )
    inspect.add_argument("--url", required=True)
    inspect.add_argument("--master-database", type=Path, default=DEFAULT_MASTER_DATABASE)
    inspect.add_argument("--limit", type=int, default=50)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "normalize":
            result = normalize_tiktok_music_page_url(args.url)
        elif args.command == "derive":
            result = build_tiktok_music_page_locator(args.music_id, args.title)
        else:
            result = inspect_local_master(
                url=args.url,
                master_database=args.master_database,
                limit=args.limit,
            )
    except TikTokMusicPageURLValueError as exc:
        print(
            _canonical_json(
                {
                    "status": "invalid",
                    "reason": exc.code,
                    "message": str(exc),
                    "ai_actions": [],
                    "outbound_actions": [],
                }
            )
        )
        return 2
    except (FileNotFoundError, RuntimeError, sqlite3.Error) as exc:
        print(
            _canonical_json(
                {
                    "status": "unavailable",
                    "reason": "local_master_unavailable",
                    "message": str(exc),
                    "ai_actions": [],
                    "outbound_actions": [],
                }
            )
        )
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

