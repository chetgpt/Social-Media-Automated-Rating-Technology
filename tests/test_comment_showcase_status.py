from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3

import comment_showcase_status as status


def _database(tmp_path: Path) -> Path:
    database = tmp_path / "engage.sqlite"
    conn = sqlite3.connect(database)
    conn.executescript(
        """
        CREATE TABLE publication_queue (
            publication_id TEXT PRIMARY KEY,
            platform TEXT NOT NULL,
            status TEXT NOT NULL,
            target_url TEXT NOT NULL,
            expected_account TEXT NOT NULL
        );
        CREATE TABLE publication_receipts (
            receipt_id TEXT PRIMARY KEY,
            publication_id TEXT NOT NULL,
            status TEXT NOT NULL,
            response_json TEXT NOT NULL
        );
        CREATE TABLE tiktok_comment_showcase_jobs (
            showcase_id TEXT PRIMARY KEY,
            source_publication_id TEXT NOT NULL,
            source_receipt_id TEXT NOT NULL,
            source_post_id TEXT NOT NULL,
            canonical_url TEXT NOT NULL,
            posting_account TEXT NOT NULL,
            status TEXT NOT NULL,
            publish_media_public_url TEXT NOT NULL DEFAULT '',
            publish_media_sha256 TEXT NOT NULL DEFAULT '',
            caption_hash TEXT NOT NULL DEFAULT '',
            review_hash TEXT NOT NULL DEFAULT '',
            presentation_hash TEXT NOT NULL DEFAULT '',
            presented_to TEXT NOT NULL DEFAULT '',
            remote_post_id TEXT NOT NULL DEFAULT '',
            remote_post_url TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()
    return database


def _insert_publication(
    database: Path,
    publication_id: str,
    *,
    screenshot: bool = False,
) -> None:
    receipt_id = publication_id.replace("publication", "receipt")
    response = {
        "target_url": "https://www.tiktok.com/@creator/video/12345",
        "observed_account": "publisher",
    }
    if screenshot:
        response["exact_comment_screenshot"] = {
            "path": "capture.png",
            "sha256": "a" * 64,
        }
    conn = sqlite3.connect(database)
    conn.execute(
        "INSERT INTO publication_queue VALUES (?, 'tiktok', 'published', ?, ?)",
        (
            publication_id,
            "https://www.tiktok.com/@creator/video/12345",
            "publisher",
        ),
    )
    conn.execute(
        "INSERT INTO publication_receipts VALUES (?, ?, 'published', ?)",
        (receipt_id, publication_id, json.dumps(response)),
    )
    conn.commit()
    conn.close()


def _insert_job(
    database: Path,
    showcase_id: str,
    publication_id: str,
    job_status: str,
) -> None:
    receipt_id = publication_id.replace("publication", "receipt")
    conn = sqlite3.connect(database)
    conn.execute(
        """
        INSERT INTO tiktok_comment_showcase_jobs (
            showcase_id, source_publication_id, source_receipt_id,
            source_post_id, canonical_url, posting_account, status,
            publish_media_public_url, publish_media_sha256, caption_hash,
            review_hash, presentation_hash, presented_to, remote_post_id,
            remote_post_url, error, created_at
        ) VALUES (?, ?, ?, '12345', ?, 'publisher', ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?)
        """,
        (
            showcase_id,
            publication_id,
            receipt_id,
            "https://www.tiktok.com/@creator/video/12345",
            job_status,
            "https://media.example.test/tiktok/showcases/comment.jpg",
            "a" * 64,
            "b" * 64,
            "c" * 64,
            "d" * 64,
            "Agson",
            "999" if job_status == "published" else "",
            (
                "https://www.tiktok.com/@publisher/photo/999"
                if job_status == "published"
                else ""
            ),
            f"2026-08-02T12:00:{showcase_id[-1]}+07:00",
        ),
    )
    conn.commit()
    conn.close()


def _args(database: Path, **updates) -> argparse.Namespace:
    values = {
        "database": str(database),
        "master_database": str(database.parent / "master.sqlite"),
        "auto_prepare_config": "",
        "publication_id": None,
        "showcase_id": None,
        "all_pending": False,
    }
    values.update(updates)
    return argparse.Namespace(**values)


def _ready_config(tmp_path: Path) -> Path:
    config = tmp_path / "showcase-auto-prepare.json"
    capture_root = tmp_path / "capture"
    public_directory = tmp_path / "public"
    ffmpeg = tmp_path / "ffmpeg.exe"
    capture_root.mkdir()
    public_directory.mkdir()
    ffmpeg.write_bytes(b"test executable")
    config.write_text(
        json.dumps(
            {
                "schema_version": "tiktok-showcase-auto-prepare-v1",
                "enabled": True,
                "public_media_verified": True,
                "capture_root": str(capture_root),
                "public_directory": str(public_directory),
                "public_base_url": "https://media.example.test/showcases/",
                "ffmpeg_path": str(ffmpeg),
            }
        ),
        encoding="utf-8",
    )
    return config


def test_publication_resume_is_read_only_and_resolves_missing_capture(tmp_path):
    database = _database(tmp_path)
    _insert_publication(database, "publication-1")
    conn = sqlite3.connect(database)
    before = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    conn.close()

    report = status.build_report(
        _args(database, publication_id="publication-1")
    )

    assert report["read_only"] is True
    item = report["items"][0]
    assert item["receipt_id"] == "receipt-1"
    assert item["next_stage"] == "exact_comment_screenshot_recovery"
    assert "recover_tiktok_comment_capture.py" in item["next_command"]
    assert "--publication-id 'publication-1'" in item["next_command"]
    conn = sqlite3.connect(database)
    after = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    conn.close()
    assert after == before


def test_captured_job_reports_configured_deterministic_resume(tmp_path):
    database = _database(tmp_path)
    _insert_publication(database, "publication-1", screenshot=True)
    _insert_job(database, "showcase-1", "publication-1", "captured")
    config = _ready_config(tmp_path)

    report = status.build_report(
        _args(
            database,
            showcase_id="showcase-1",
            auto_prepare_config=str(config),
        )
    )
    item = report["items"][0]
    assert item["next_stage"] == "deterministic_media_preparation"
    assert item["auto_prepare"]["ready"] is True
    assert "--showcase-auto-prepare-config" in item["next_command"]
    assert "--execute" not in item["next_command"]


def test_reviewed_and_presented_reports_bind_music_consent(tmp_path):
    database = _database(tmp_path)
    _insert_publication(database, "publication-1", screenshot=True)
    _insert_job(database, "showcase-1", "publication-1", "reviewed")
    reviewed = status.build_report(
        _args(database, showcase_id="showcase-1")
    )["items"][0]
    assert reviewed["next_stage"] == "named_human_presentation"
    shown = reviewed["presentation_must_show"]
    assert shown["post_settings"]["auto_add_music"] is False
    assert "Music Usage Confirmation" in shown["music_usage_consent"]

    conn = sqlite3.connect(database)
    conn.execute(
        "UPDATE tiktok_comment_showcase_jobs SET status='presented' "
        "WHERE showcase_id='showcase-1'"
    )
    conn.commit()
    conn.close()
    presented = status.build_report(
        _args(database, showcase_id="showcase-1")
    )["items"][0]
    assert presented["next_stage"] == "separate_named_human_authorization"
    assert "--authorized-by 'Agson'" in presented["command_template"]
    assert "Music Usage Confirmation" in (
        presented["approval_binds"]["music_usage_consent"]
    )


def test_authorized_and_uncertain_reports_only_safe_dry_run_commands(tmp_path):
    database = _database(tmp_path)
    _insert_publication(database, "publication-1", screenshot=True)
    _insert_job(database, "showcase-1", "publication-1", "authorized")
    authorized = status.build_report(
        _args(database, showcase_id="showcase-1")
    )["items"][0]
    assert authorized["next_stage"] == "guarded_publication_dry_run"
    assert "publish_comment_showcase.py" in authorized["next_command"]
    assert "--public-media-base-url" in authorized["next_command"]
    assert "--execute" not in authorized["next_command"]

    conn = sqlite3.connect(database)
    conn.execute(
        "UPDATE tiktok_comment_showcase_jobs SET status='uncertain' "
        "WHERE showcase_id='showcase-1'"
    )
    conn.commit()
    conn.close()
    uncertain = status.build_report(
        _args(database, showcase_id="showcase-1")
    )["items"][0]
    assert uncertain["next_stage"] == "interrupted_post_submit_reconciliation"
    assert " 'reconcile' " in uncertain["next_command"]
    assert "--execute" not in uncertain["next_command"]


def test_all_pending_includes_orphans_and_excludes_published_jobs(tmp_path):
    database = _database(tmp_path)
    for number in range(1, 4):
        _insert_publication(
            database,
            f"publication-{number}",
            screenshot=number != 3,
        )
    _insert_job(database, "showcase-1", "publication-1", "captured")
    _insert_job(database, "showcase-2", "publication-2", "published")

    report = status.build_report(_args(database, all_pending=True))

    assert report["mode"] == "all_pending"
    assert report["count"] == 2
    assert {item["publication_id"] for item in report["items"]} == {
        "publication-1",
        "publication-3",
    }
    assert all(item["next_stage"] != "complete" for item in report["items"])


def test_all_pending_reports_one_ambiguous_receipt_without_hiding_others(tmp_path):
    database = _database(tmp_path)
    _insert_publication(database, "publication-1")
    _insert_publication(database, "publication-2")
    conn = sqlite3.connect(database)
    conn.execute(
        "INSERT INTO publication_receipts VALUES "
        "('receipt-1-duplicate', 'publication-1', 'published', '{}')"
    )
    conn.commit()
    conn.close()

    report = status.build_report(_args(database, all_pending=True))

    assert report["count"] == 2
    by_publication = {
        item["publication_id"]: item for item in report["items"]
    }
    assert by_publication["publication-1"]["next_stage"] == "operator_attention"
    assert by_publication["publication-2"]["next_stage"] == (
        "exact_comment_screenshot_recovery"
    )
