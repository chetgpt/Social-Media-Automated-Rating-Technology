import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from tiktok_scraper.storage.google_drive import (
    DriveArchiveConfig,
    DriveArchiveError,
    GoogleDriveArchiveManager,
    _resume_offset,
    ensure_archive_schema,
)


class FakeDriveClient:
    def __init__(self, *, fail_once=False):
        self.files = {}
        self.upload_calls = []
        self.fail_once = fail_once
        self.failed = False

    def authenticate(self, *, interactive=True):
        return {"user": {"displayName": "Archive User", "emailAddress": "archive@example.com"}}

    def ensure_project_folders(self, project_slug):
        return {
            "root": "root-id",
            "project": f"project:{project_slug}",
            "raw_runs": "section:raw_runs",
            "compiled": "section:compiled",
            "logs": "section:logs",
            "state": "section:state",
            "manifests": "section:manifests",
        }

    def ensure_folder(self, name, parent_id, *, app_properties=None):
        return f"{parent_id}/{name}"

    def upload_file(self, local_path, **kwargs):
        self.upload_calls.append({"path": str(local_path), **kwargs})
        progress = kwargs.get("progress")
        fingerprint = kwargs["fingerprint"]
        if progress:
            progress("https://upload.example/session", max(1, fingerprint["size_bytes"] // 2))
        if self.fail_once and not self.failed:
            self.failed = True
            raise DriveArchiveError("simulated interruption")
        file_id = kwargs.get("remote_file_id") or f"file:{kwargs['artifact_key'][:16]}"
        remote = {
            "id": file_id,
            "name": kwargs["remote_name"],
            "size": str(fingerprint["size_bytes"]),
            "md5Checksum": fingerprint["md5"],
            "trashed": False,
            "appProperties": {"slArtifactKey": kwargs["artifact_key"]},
        }
        self.files[file_id] = remote
        if progress:
            progress("https://upload.example/session", fingerprint["size_bytes"])
        return remote

    def get_file(self, file_id):
        if file_id not in self.files:
            raise DriveArchiveError("remote file missing")
        return self.files[file_id]


def memory_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_archive_schema(conn)
    return conn


def project_paths(tmp_path):
    paths = {
        "root": tmp_path,
        "raw_runs": tmp_path / "raw_runs",
        "state": tmp_path / "state",
        "compiled": tmp_path / "compiled",
        "logs": tmp_path / "logs",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return paths


def test_drive_config_precedence_and_machine_defaults(tmp_path):
    config = DriveArchiveConfig.from_sources(
        {
            "archive_provider": None,
            "drive_delete_local_after_upload": False,
            "drive_chunk_size_mb": None,
        },
        {
            "provider": "google-drive",
            "account_hint": "campaign@example.com",
            "mount_path": "I:\\My Drive",
            "raw_run_archive_timing": "final",
            "delete_local_after_upload": True,
            "chunk_size_mb": 8,
        },
        {
            "GOOGLE_DRIVE_ACCOUNT_HINT": "machine@example.com",
            "GOOGLE_DRIVE_CHUNK_SIZE_MB": "32",
        },
        cwd=tmp_path,
    )

    assert config.enabled is True
    assert config.account_hint == "machine@example.com"
    assert config.mount_path == Path("I:\\My Drive")
    assert config.raw_run_archive_timing == "final"
    assert config.delete_local_after_upload is False
    assert config.chunk_size_bytes == 32 * 1024 * 1024
    assert config.credentials_file == tmp_path / ".google_drive" / "client_secret.json"


def test_raw_run_upload_is_manifest_verified_before_local_delete(tmp_path):
    paths = project_paths(tmp_path)
    session = paths["raw_runs"] / "youtube_run_1"
    (session / "comments").mkdir(parents=True)
    (session / "comments" / "youtube_comments.json").write_text(
        json.dumps({"videos": [{"id": "abc", "comments": [{"text": "hello"}]}]}),
        encoding="utf-8",
    )
    (session / "logs").mkdir()
    (session / "logs" / "candidates.json").write_text("[]", encoding="utf-8")

    client = FakeDriveClient()
    manager = GoogleDriveArchiveManager(
        DriveArchiveConfig(provider="google-drive", delete_local_after_upload=True),
        memory_db(),
        "Campaign",
        "project_campaign",
        paths,
        client=client,
    )
    manager.initialize()
    result = manager.archive_raw_run(
        session,
        run_id="youtube_run_1",
        platform="youtube",
        run_metadata={"status": "success"},
    )

    assert result["status"] == "verified"
    assert result["files"] == 2
    assert result["local_deleted"] is True
    assert not session.exists()
    rows = manager.conn.execute(
        "SELECT artifact_type, status, session_uri FROM archive_uploads ORDER BY artifact_type"
    ).fetchall()
    assert len(rows) == 3
    assert {row["status"] for row in rows} == {"verified"}
    assert {row["session_uri"] for row in rows} == {""}
    manifest_row = next(row for row in rows if row["artifact_type"] == "raw_run_manifest")
    assert manifest_row is not None

    repeated = manager.archive_raw_run(
        session,
        run_id="youtube_run_1",
        platform="youtube",
    )
    assert repeated["status"] == "already_verified"
    assert len(client.upload_calls) == 3


def test_interrupted_upload_uses_persisted_session_and_offset(tmp_path):
    paths = project_paths(tmp_path)
    session = paths["raw_runs"] / "tiktok_run_1"
    session.mkdir()
    payload = b"a" * 100
    (session / "comments.json").write_bytes(payload)

    client = FakeDriveClient(fail_once=True)
    manager = GoogleDriveArchiveManager(
        DriveArchiveConfig(provider="google-drive"),
        memory_db(),
        "Campaign",
        "project_campaign",
        paths,
        client=client,
    )
    manager.initialize()

    with pytest.raises(DriveArchiveError, match="simulated interruption"):
        manager.archive_raw_run(session, run_id="tiktok_run_1", platform="tiktok")
    failed = manager.conn.execute(
        "SELECT status, session_uri, uploaded_bytes FROM archive_uploads"
    ).fetchone()
    assert failed["status"] == "failed"
    assert failed["session_uri"] == "https://upload.example/session"
    assert failed["uploaded_bytes"] == 50

    result = manager.archive_raw_run(session, run_id="tiktok_run_1", platform="tiktok")
    assert result["status"] == "verified"
    resumed_call = client.upload_calls[1]
    assert resumed_call["session_uri"] == "https://upload.example/session"
    assert resumed_call["uploaded_bytes"] == 50


def test_database_snapshot_is_uploaded_then_removed(tmp_path):
    paths = project_paths(tmp_path)
    conn = memory_db()
    conn.execute(
        "INSERT INTO archive_uploads (artifact_key, project, artifact_type, logical_path, local_path, "
        "remote_name, size_bytes, md5, sha256, status, created_at, updated_at) "
        "VALUES ('seed', 'Campaign', 'seed', 'seed', 'seed', 'seed', 0, ?, ?, 'verified', 'now', 'now')",
        (hashlib.md5(b"").hexdigest(), hashlib.sha256(b"").hexdigest()),
    )
    conn.commit()
    client = FakeDriveClient()
    manager = GoogleDriveArchiveManager(
        DriveArchiveConfig(provider="google-drive"),
        conn,
        "Campaign",
        "project_campaign",
        paths,
        client=client,
    )
    manager.initialize()

    result = manager.archive_database_snapshot(paths["state"] / "scrape_state.sqlite")

    assert result["status"] == "verified"
    assert result["size_bytes"] > 0
    assert not (paths["state"] / "drive_snapshots" / "scrape_state_latest.sqlite").exists()


def test_resume_offset_parses_drive_range_and_rejects_overflow():
    assert _resume_offset("bytes=0-262143", 1_000_000) == 262_144
    assert _resume_offset("", 1_000_000) == 0
    with pytest.raises(DriveArchiveError, match="invalid resumable offset"):
        _resume_offset("bytes=0-1000000", 1_000_000)
