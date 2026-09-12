from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import run_audio_archive_batch as batch
from tiktok_scraper.run_lineage import canonical_sha256


def _project(
    comments_data: Path,
    name: str,
    run_id: str,
    *,
    status: str,
    evidence_ready: int,
) -> Path:
    database = comments_data / name / "state" / "engage_state.sqlite"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE engage_tiktok_runs (
                run_id TEXT PRIMARY KEY,
                project TEXT,
                workflow TEXT,
                status TEXT,
                created_at TEXT
            );
            CREATE TABLE engage_tiktok_posts (
                run_id TEXT,
                post_id TEXT,
                url TEXT,
                evidence_ready INTEGER
            );
            """
        )
        connection.execute(
            "INSERT INTO engage_tiktok_runs VALUES (?, ?, 'listen', ?, '')",
            (run_id, name, status),
        )
        for index in range(evidence_ready):
            post_id = str(70000 + index)
            connection.execute(
                "INSERT INTO engage_tiktok_posts VALUES (?, ?, ?, 1)",
                (
                    run_id,
                    post_id,
                    f"https://www.tiktok.com/@creator/video/{post_id}",
                ),
            )
    return database


def _frozen_candidate(post_id: str) -> dict[str, str]:
    url = f"https://www.tiktok.com/@creator/video/{post_id}"
    return {
        "post_id": post_id,
        "canonical_url": url,
        "evidence_hash": canonical_sha256({"post_id": post_id, "url": url}),
    }


def _archive(
    output_root: Path,
    archive_id: str,
    *,
    source_database: str,
    run_id: str,
    project: str,
    selection: list[dict[str, str]],
    status: str,
    terminal: int = 0,
) -> Path:
    run_directory = output_root / archive_id
    run_directory.mkdir(parents=True)
    (run_directory / "manifest.json").write_text(
        json.dumps(
            {
                "run_id": archive_id,
                "source": {
                    "source_database": source_database,
                    "local_run_id": run_id,
                    "project": project,
                },
                "selection": selection,
            }
        ),
        encoding="utf-8",
    )
    (run_directory / "state.json").write_text(
        json.dumps({"status": status, "terminal": terminal}),
        encoding="utf-8",
    )
    return run_directory


def test_discovers_evidence_ready_runs_from_all_projects_regardless_status(tmp_path):
    comments_data = tmp_path / "comments_data"
    _project(
        comments_data,
        "project_complete",
        "engage_complete",
        status="collection_complete",
        evidence_ready=2,
    )
    _project(
        comments_data,
        "project_incomplete",
        "engage_incomplete",
        status="collection_incomplete",
        evidence_ready=3,
    )

    runs, skipped = batch._discover_runs(comments_data, [], set())
    assert [(row["run_id"], row["evidence_ready"]) for row in runs] == [
        ("engage_complete", 2),
        ("engage_incomplete", 3),
    ]
    assert skipped == []


def test_dry_run_is_offline_and_accepts_any_project_filter(
    monkeypatch,
    tmp_path,
    capsys,
):
    comments_data = tmp_path / "comments_data"
    _project(
        comments_data,
        "project_legacy_brand",
        "engage_legacy",
        status="browser_blocked",
        evidence_ready=1,
    )
    monkeypatch.setattr(
        batch.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("dry-run must not execute an archive")
        ),
    )

    assert (
        batch.main(
            [
                "--comments-data",
                str(comments_data),
                "--project",
                "project_legacy_brand",
                "--dry-run",
            ]
        )
        == 0
    )
    packet = json.loads(capsys.readouterr().out)
    assert packet["mode"] == "selected-projects"
    assert packet["run_count"] == 1
    assert packet["runs"][0]["source_status"] == "browser_blocked"


def test_all_projects_batch_uses_minimal_archive_command_and_writes_report(
    monkeypatch,
    tmp_path,
):
    comments_data = tmp_path / "comments_data"
    output_root = tmp_path / "audio"
    _project(
        comments_data,
        "project_one",
        "engage_one",
        status="collection_complete",
        evidence_ready=1,
    )
    _project(
        comments_data,
        "project_two",
        "engage_two",
        status="collection_incomplete",
        evidence_ready=2,
    )
    commands = []

    def completed(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(batch.subprocess, "run", completed)
    assert (
        batch.main(
            [
                "--comments-data",
                str(comments_data),
                "--output-root",
                str(output_root),
            ]
        )
        == 0
    )
    assert len(commands) == 2
    flattened = " ".join(part for command in commands for part in command)
    for removed in (
        "--master-database",
        "--authorized-by",
        "--authorize-audio-storage",
        "--rights-basis",
        "--retention-days",
        "--storage-mode",
    ):
        assert removed not in flattened
    reports = list((output_root / "batches").glob("audio_archive_batch_*.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text(encoding="utf-8"))
    assert report["planned_runs"] == 2
    assert report["planned_evidence_ready"] == 3
    assert report["failed"] == 0


def test_batch_resumes_identity_bound_run_after_project_database_moves(
    monkeypatch,
    tmp_path,
):
    comments_data = tmp_path / "comments_data"
    output_root = tmp_path / "audio"
    run_id = "engage_moved"
    _project(
        comments_data,
        "project_restored",
        run_id,
        status="collection_incomplete",
        evidence_ready=1,
    )
    archive_id = "audio_archive_stored_old_1p_20260830_010203_12345678"
    _archive(
        output_root,
        archive_id,
        source_database=r"D:\old\workspace\project.sqlite",
        run_id=run_id,
        project="project_restored",
        selection=[_frozen_candidate("70000")],
        status="archive_incomplete",
    )
    commands = []
    monkeypatch.setattr(
        batch.subprocess,
        "run",
        lambda command, **kwargs: (
            commands.append(command) or SimpleNamespace(returncode=0)
        ),
    )

    assert (
        batch.main(
            [
                "--comments-data",
                str(comments_data),
                "--output-root",
                str(output_root),
            ]
        )
        == 0
    )
    assert len(commands) == 1
    assert "resume" in commands[0]
    assert archive_id in commands[0]


def test_completed_archive_only_covers_its_frozen_scope_when_source_grows(
    monkeypatch,
    tmp_path,
):
    comments_data = tmp_path / "comments_data"
    output_root = tmp_path / "audio"
    run_id = "engage_growing"
    database = _project(
        comments_data,
        "project_growing",
        run_id,
        status="collection_incomplete",
        evidence_ready=1,
    )
    _archive(
        output_root,
        "audio_archive_stored_growing_1p_20260830_010203_12345678",
        source_database=str(database.resolve()),
        run_id=run_id,
        project="project_growing",
        selection=[_frozen_candidate("70000")],
        status="archive_complete",
        terminal=1,
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO engage_tiktok_posts VALUES (?, ?, ?, 1)",
            (
                run_id,
                "70001",
                "https://www.tiktok.com/@creator/video/70001",
            ),
        )

    commands = []
    monkeypatch.setattr(
        batch.subprocess,
        "run",
        lambda command, **kwargs: (
            commands.append(command) or SimpleNamespace(returncode=0)
        ),
    )

    assert (
        batch.main(
            [
                "--comments-data",
                str(comments_data),
                "--output-root",
                str(output_root),
            ]
        )
        == 0
    )
    assert len(commands) == 1
    assert "run" in commands[0]
    assert commands[0].count("--post-id") == 1
    assert "70001" in commands[0]
    assert "70000" not in commands[0]


def test_large_delta_is_split_into_bounded_archive_commands(monkeypatch, tmp_path):
    comments_data = tmp_path / "comments_data"
    output_root = tmp_path / "audio"
    run_id = "engage_large_delta"
    database = _project(
        comments_data,
        "project_large_delta",
        run_id,
        status="collection_incomplete",
        evidence_ready=62,
    )
    _archive(
        output_root,
        "audio_archive_stored_large_delta_1p_20260830_010203_12345678",
        source_database=str(database.resolve()),
        run_id=run_id,
        project="project_large_delta",
        selection=[_frozen_candidate("70000")],
        status="archive_complete",
        terminal=1,
    )
    commands = []
    monkeypatch.setattr(
        batch.subprocess,
        "run",
        lambda command, **kwargs: (
            commands.append(command) or SimpleNamespace(returncode=0)
        ),
    )

    assert (
        batch.main(
            [
                "--comments-data",
                str(comments_data),
                "--output-root",
                str(output_root),
            ]
        )
        == 0
    )
    assert [command.count("--post-id") for command in commands] == [60, 1]
    assert all("70000" not in command for command in commands)


def test_moved_archive_does_not_match_same_run_id_from_another_project(
    monkeypatch,
    tmp_path,
):
    comments_data = tmp_path / "comments_data"
    output_root = tmp_path / "audio"
    run_id = "engage_reused_legacy_id"
    _project(
        comments_data,
        "project_current",
        run_id,
        status="collection_incomplete",
        evidence_ready=1,
    )
    _archive(
        output_root,
        "audio_archive_stored_other_1p_20260830_010203_12345678",
        source_database=r"D:\old\other\project.sqlite",
        run_id=run_id,
        project="project_other",
        selection=[_frozen_candidate("70000")],
        status="archive_complete",
        terminal=1,
    )
    commands = []
    monkeypatch.setattr(
        batch.subprocess,
        "run",
        lambda command, **kwargs: (
            commands.append(command) or SimpleNamespace(returncode=0)
        ),
    )

    assert (
        batch.main(
            [
                "--comments-data",
                str(comments_data),
                "--output-root",
                str(output_root),
            ]
        )
        == 0
    )
    assert len(commands) == 1
    assert "run" in commands[0]
    assert "resume" not in commands[0]


def test_multiple_complete_archives_cover_scope_deterministically(
    monkeypatch,
    tmp_path,
):
    comments_data = tmp_path / "comments_data"
    output_root = tmp_path / "audio"
    run_id = "engage_split_archive"
    _project(
        comments_data,
        "project_split",
        run_id,
        status="collection_incomplete",
        evidence_ready=2,
    )
    archive_ids = [
        "audio_archive_stored_split_a_1p_20260830_010203_12345678",
        "audio_archive_stored_split_b_1p_20260830_010203_12345678",
    ]
    for archive_id, post_id in zip(archive_ids, ("70000", "70001"), strict=True):
        _archive(
            output_root,
            archive_id,
            source_database=r"D:\old\split\project.sqlite",
            run_id=run_id,
            project="project_split",
            selection=[_frozen_candidate(post_id)],
            status="archive_complete",
            terminal=1,
        )
    monkeypatch.setattr(
        batch.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("fully covered scope must be skipped")
        ),
    )

    assert (
        batch.main(
            [
                "--comments-data",
                str(comments_data),
                "--output-root",
                str(output_root),
            ]
        )
        == 0
    )
    reports = list((output_root / "batches").glob("audio_archive_batch_*.json"))
    report = json.loads(reports[0].read_text(encoding="utf-8"))
    assert report["results"][0]["action"] == "skipped_complete"
    assert report["results"][0]["covering_archive_run_ids"] == archive_ids
