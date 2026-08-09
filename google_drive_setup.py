#!/usr/bin/env python
"""Authorize the archive account and initialize its Drive API folder tree."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from tiktok_scraper.storage.google_drive import (
    DriveArchiveConfig,
    DriveArchiveError,
    DriveRestClient,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Authorize and validate Google Drive archival.")
    parser.add_argument("--credentials", dest="drive_credentials", default="")
    parser.add_argument("--token", dest="drive_token", default="")
    parser.add_argument("--account", dest="drive_account_hint", default="")
    parser.add_argument("--mount-path", dest="drive_mount_path", default="")
    parser.add_argument("--root-folder-name", dest="drive_root_folder_name", default="")
    parser.add_argument("--root-folder-id", dest="drive_root_folder_id", default="")
    parser.add_argument("--project", default="", help="Optional project folder to initialize.")
    parser.add_argument("--check-only", action="store_true", help="Do not open an OAuth browser flow.")
    return parser.parse_args()


def slugify_project(value: str) -> str:
    import re

    slug = re.sub(r"[^a-zA-Z0-9]+", "_", value.strip()).strip("_").lower()
    return f"project_{slug or 'default'}"


def main() -> None:
    args = parse_args()
    values = vars(args) | {"archive_provider": "google-drive"}
    config = DriveArchiveConfig.from_sources(values, cwd=Path.cwd())
    client = DriveRestClient(config)
    try:
        about = client.authenticate(interactive=not args.check_only)
        folders = {}
        if args.project:
            folders = client.ensure_project_folders(slugify_project(args.project))
        else:
            root_id = config.root_folder_id or client.ensure_folder(
                config.root_folder_name,
                "root",
                app_properties={"slArchiveRoot": "v1"},
            )
            folders = {"root": root_id}
    except DriveArchiveError as exc:
        raise SystemExit(f"Google Drive setup failed: {exc}") from exc

    user = about.get("user") or {}
    print(
        json.dumps(
            {
                "status": "ready",
                "account": {
                    "display_name": user.get("displayName") or "",
                    "email": user.get("emailAddress") or "",
                },
                "mount_path": str(config.mount_path) if config.mount_path else "",
                "root_folder_name": config.root_folder_name,
                "folders": folders,
                "token_file": str(config.token_file),
                "scope": "drive.file",
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
