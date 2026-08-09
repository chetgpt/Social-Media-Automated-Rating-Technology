"""Durable storage backends for completed scraper artifacts."""

from .google_drive import (
    DriveArchiveConfig,
    DriveArchiveError,
    GoogleDriveArchiveManager,
    ensure_archive_schema,
)

__all__ = [
    "DriveArchiveConfig",
    "DriveArchiveError",
    "GoogleDriveArchiveManager",
    "ensure_archive_schema",
]
