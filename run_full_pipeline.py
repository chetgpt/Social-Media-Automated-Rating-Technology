"""Compatibility wrapper for the canonical incremental project pipeline."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys


def project_name(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", (value or "").strip()).strip("_").lower()
    return slug or "social_scrape"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the stateful multi-platform scraper and compile project reports."
    )
    parser.add_argument("--search", required=True, help="Search topic or keyword list.")
    parser.add_argument("--project", default="", help="Persistent project name; defaults to the search topic.")
    parser.add_argument(
        "--platform",
        choices=["youtube", "tiktok", "instagram", "facebook", "x", "all"],
        default="all",
    )
    parser.add_argument("--videos", type=int, default=2, help="Maximum posts per platform; 0 is unlimited.")
    parser.add_argument("--comments", type=int, default=0, help="Maximum comments per post; 0 is unlimited.")
    parser.add_argument("--report-hours", default="6,12")
    parser.add_argument("--refresh-known-limit", type=int, default=20)
    parser.add_argument("--refresh-known-after-hours", type=float, default=6.0)
    args, passthrough = parser.parse_known_args()

    command = [
        sys.executable,
        "incremental_project.py",
        "--project",
        args.project or project_name(args.search),
        "--keywords",
        args.search,
        "--platform",
        args.platform,
        "--videos",
        str(args.videos),
        "--comments",
        str(args.comments),
        "--report-hours",
        args.report_hours,
        "--refresh-known-limit",
        str(args.refresh_known_limit),
        "--refresh-known-after-hours",
        str(args.refresh_known_after_hours),
        *passthrough,
    ]
    print("[INFO] run_full_pipeline.py now delegates to incremental_project.py", flush=True)
    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
