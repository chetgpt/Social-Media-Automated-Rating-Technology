from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from tiktok_scraper.run_lineage import (
    LINEAGE_SCHEMA_VERSION,
    RunLineageError,
    canonical_json,
    canonical_sha256,
    load_archive_run_posts,
    load_completed_run_lineage,
)
from tiktok_scraper.source_identity import insert_source_path_alias


LOCAL_RUN_SCHEMA = """
CREATE TABLE engage_tiktok_runs (
    run_id TEXT PRIMARY KEY,
    project TEXT,
    topic TEXT,
    requested_count INTEGER,
    workflow TEXT,
    collection_policy TEXT,
    source_mode TEXT,
    creator_handle TEXT,
    direct_post_url TEXT,
    cardinality_mode TEXT,
    master_database TEXT,
    expected_account TEXT,
    observed_account TEXT,
    status TEXT,
    requested INTEGER,
    unique_collected INTEGER,
    evidence_ready INTEGER,
    analyzed INTEGER,
    drafted INTEGER,
    reviewed INTEGER,
    stored INTEGER,
    authorized INTEGER,
    published INTEGER,
    skipped INTEGER,
    failed INTEGER,
    profile_inventory_count INTEGER,
    profile_inventory_terminal INTEGER,
    profile_inventory_hash TEXT,
    mode TEXT,
    collection_attempt_id TEXT,
    error TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE engage_tiktok_posts (
    run_id TEXT,
    post_id TEXT,
    url TEXT,
    evidence_ready INTEGER,
    evidence_json TEXT,
    evidence_hash TEXT
);
"""


MASTER_SCHEMA = """
CREATE TABLE tiktok_master_sources (
    source_id TEXT PRIMARY KEY,
    database_path TEXT
);
CREATE TABLE tiktok_master_runs (
    master_run_id TEXT PRIMARY KEY,
    source_id TEXT,
    local_run_id TEXT,
    project TEXT,
    workflow TEXT,
    topic TEXT,
    source_mode TEXT,
    direct_post_url TEXT,
    creator_handle TEXT,
    cardinality_mode TEXT,
    profile_inventory_count INTEGER,
    profile_inventory_terminal INTEGER,
    profile_inventory_hash TEXT,
    requested_count INTEGER,
    collection_policy TEXT,
    mode TEXT,
    expected_account TEXT,
    observed_account TEXT,
    status TEXT,
    requested INTEGER,
    unique_collected INTEGER,
    evidence_ready INTEGER,
    analyzed INTEGER,
    drafted INTEGER,
    reviewed INTEGER,
    stored INTEGER,
    authorized INTEGER,
    published INTEGER,
    skipped INTEGER,
    failed INTEGER,
    error TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE tiktok_master_run_posts (
    master_run_id TEXT,
    post_id TEXT,
    snapshot_id TEXT,
    collection_policy TEXT,
    position INTEGER
);
CREATE TABLE tiktok_master_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    post_id TEXT,
    source_id TEXT,
    master_run_id TEXT,
    local_run_id TEXT,
    evidence_hash TEXT,
    evidence_json TEXT
);
CREATE TABLE tiktok_master_posts (
    post_id TEXT PRIMARY KEY,
    latest_snapshot_id TEXT,
    latest_evidence_hash TEXT
);
"""


def stable_id(*parts: object, length: int = 32) -> str:
    payload = "\x1f".join(str(part or "") for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]


def evidence(post_id: str, creator: str, content_type: str = "video") -> dict:
    return {
        "schema_version": "tiktok-engage-evidence-v1",
        "platform": "tiktok",
        "post_id": post_id,
        "url": f"https://www.tiktok.com/@{creator}/{content_type}/{post_id}",
        "creator": creator,
        "content_type": content_type,
        "caption": f"post {post_id}",
        "evidence_ready": True,
    }


def build_lineage_databases(
    tmp_path: Path,
    *,
    workflow: str = "listen",
    status: str | None = None,
    post_specs: tuple[tuple[str, str, str], ...] = (
        ("202", "Second.Creator", "photo"),
        ("101", "first_creator", "video"),
    ),
) -> tuple[Path, str, Path]:
    source = tmp_path / "project_state.sqlite"
    master = tmp_path / "tiktok_master.sqlite"
    run_id = "engage_source123"
    status = status or ("collection_complete" if workflow == "listen" else "stored")
    timestamp = "2026-08-29T01:02:03+00:00"
    count = len(post_specs)

    # Materialize both paths before writing the immutable path binding.
    sqlite3.connect(master).close()
    local_run = {
        "run_id": run_id,
        "project": "music_audit_test_new_topic_music_2p",
        "topic": "music",
        "requested_count": count,
        "workflow": workflow,
        "collection_policy": "new_only",
        "source_mode": "topic",
        "creator_handle": "",
        "direct_post_url": "",
        "cardinality_mode": "fixed",
        "master_database": str(master.resolve()),
        "expected_account": "kitascore",
        "observed_account": "kitascore",
        "status": status,
        "requested": count,
        "unique_collected": count,
        "evidence_ready": count,
        "analyzed": count if status != "collection_complete" else 0,
        "drafted": count if status == "stored" else 0,
        "reviewed": count if status == "stored" else 0,
        "stored": count if status == "stored" else 0,
        "authorized": 0,
        "published": 0,
        "skipped": 0,
        "failed": 0,
        "profile_inventory_count": 0,
        "profile_inventory_terminal": 0,
        "profile_inventory_hash": "",
        "mode": "shadow",
        "collection_attempt_id": "",
        "error": "",
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    with sqlite3.connect(source) as conn:
        conn.executescript(LOCAL_RUN_SCHEMA)
        columns = tuple(local_run)
        conn.execute(
            f"INSERT INTO engage_tiktok_runs ({','.join(columns)}) "
            f"VALUES ({','.join('?' for _ in columns)})",
            tuple(local_run[column] for column in columns),
        )
        for post_id, creator, content_type in post_specs:
            packet = evidence(post_id, creator, content_type)
            conn.execute(
                "INSERT INTO engage_tiktok_posts VALUES (?, ?, ?, 1, ?, ?)",
                (
                    run_id,
                    post_id,
                    packet["url"],
                    canonical_json(packet),
                    canonical_sha256(packet),
                ),
            )

    source_id = stable_id("tiktok-engage-source", str(source.resolve()))
    master_run_id = stable_id(source_id, run_id)
    master_run = {
        key: value
        for key, value in local_run.items()
        if key not in {"run_id", "master_database", "collection_attempt_id"}
    }
    master_run = {
        "master_run_id": master_run_id,
        "source_id": source_id,
        "local_run_id": run_id,
        **master_run,
    }
    with sqlite3.connect(master) as conn:
        conn.executescript(MASTER_SCHEMA)
        conn.execute(
            "INSERT INTO tiktok_master_sources VALUES (?, ?)",
            (source_id, str(source.resolve())),
        )
        columns = tuple(master_run)
        conn.execute(
            f"INSERT INTO tiktok_master_runs ({','.join(columns)}) "
            f"VALUES ({','.join('?' for _ in columns)})",
            tuple(master_run[column] for column in columns),
        )
        for position, (post_id, creator, content_type) in enumerate(post_specs, 1):
            packet = evidence(post_id, creator, content_type)
            packet_hash = canonical_sha256(packet)
            snapshot_id = f"immutable-snapshot-{post_id}"
            conn.execute(
                "INSERT INTO tiktok_master_snapshots VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    snapshot_id,
                    post_id,
                    source_id,
                    master_run_id,
                    run_id,
                    packet_hash,
                    canonical_json(packet),
                ),
            )
            conn.execute(
                "INSERT INTO tiktok_master_run_posts VALUES (?, ?, ?, ?, ?)",
                (master_run_id, post_id, snapshot_id, "new_only", position),
            )
            # A deliberately unrelated mutable latest pointer proves that the
            # loader follows run_posts.snapshot_id instead.
            conn.execute(
                "INSERT INTO tiktok_master_posts VALUES (?, ?, ?)",
                (post_id, f"later-snapshot-{post_id}", "f" * 64),
            )
    return source, run_id, master


def load(paths: tuple[Path, str, Path]) -> dict:
    return load_completed_run_lineage(*paths)


def test_loads_hash_bound_ordered_lineage_from_immutable_run_snapshots(tmp_path):
    result = load(build_lineage_databases(tmp_path))

    assert result["schema_version"] == LINEAGE_SCHEMA_VERSION
    assert result["source_run"]["run_id"] == "engage_source123"
    assert result["source_run"]["requested_count"] == 2
    assert [candidate["post_id"] for candidate in result["candidates"]] == [
        "202",
        "101",
    ]
    assert result["candidates"][0] == {
        "ordinal": 1,
        "post_id": "202",
        "canonical_url": "https://www.tiktok.com/@second.creator/photo/202",
        "creator_handle": "second.creator",
        "content_type": "photo",
        "snapshot_id": "immutable-snapshot-202",
        "evidence_hash": canonical_sha256(evidence("202", "Second.Creator", "photo")),
    }
    unhashed = dict(result)
    lineage_hash = unhashed.pop("lineage_hash")
    assert lineage_hash == canonical_sha256(unhashed)


@pytest.mark.parametrize(
    ("workflow", "status"),
    (("audit", "audit_complete"), ("engage", "stored")),
)
def test_accepts_post_collection_audit_and_engage_runs(tmp_path, workflow, status):
    result = load(
        build_lineage_databases(tmp_path, workflow=workflow, status=status)
    )

    assert result["source_run"]["workflow"] == workflow
    assert result["source_run"]["status"] == status


@pytest.mark.parametrize(
    ("workflow", "status"),
    (
        ("listen", "stored"),
        ("audit", "collecting"),
        ("audit", "analysis_pending"),
        ("audit", "analysis_complete"),
        ("engage", "browser_blocked"),
        ("engage", "collection_incomplete"),
        ("engage", "collection_failed"),
        ("engage", "draft_pending"),
        ("engage", "review_pending"),
    ),
)
def test_rejects_nonterminal_or_failed_source_statuses(
    tmp_path, workflow, status
):
    paths = build_lineage_databases(tmp_path, workflow=workflow, status=status)

    with pytest.raises(RunLineageError, match="status"):
        load(paths)


def test_rejects_active_collection_attempt(tmp_path):
    paths = build_lineage_databases(tmp_path)
    with sqlite3.connect(paths[0]) as conn:
        conn.execute(
            "UPDATE engage_tiktok_runs SET collection_attempt_id='attempt-live'"
        )

    with pytest.raises(RunLineageError, match="active collection attempt"):
        load(paths)


def test_rejects_relative_database_paths(tmp_path, monkeypatch):
    source, run_id, master = build_lineage_databases(tmp_path)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(RunLineageError, match="absolute canonical path"):
        load_completed_run_lineage(source.name, run_id, master)


def test_rejects_symlinked_source_database_path(tmp_path):
    source, run_id, master = build_lineage_databases(tmp_path)
    alias = tmp_path / "source-alias.sqlite"
    try:
        alias.symlink_to(source)
    except OSError:
        pytest.skip("file symlinks are not available on this Windows host")

    with pytest.raises(RunLineageError, match="symlink|reparse"):
        load_completed_run_lineage(alias, run_id, master)


def test_rejects_tampered_local_evidence(tmp_path):
    paths = build_lineage_databases(tmp_path)
    with sqlite3.connect(paths[0]) as conn:
        packet = json.loads(
            conn.execute(
                "SELECT evidence_json FROM engage_tiktok_posts WHERE post_id='101'"
            ).fetchone()[0]
        )
        packet["caption"] = "tampered after hashing"
        conn.execute(
            "UPDATE engage_tiktok_posts SET evidence_json=? WHERE post_id='101'",
            (canonical_json(packet),),
        )

    with pytest.raises(RunLineageError, match="local evidence hash mismatch"):
        load(paths)


def test_rejects_noncontiguous_master_positions(tmp_path):
    paths = build_lineage_databases(tmp_path)
    with sqlite3.connect(paths[2]) as conn:
        conn.execute(
            "UPDATE tiktok_master_run_posts SET position=3 WHERE post_id='101'"
        )

    with pytest.raises(RunLineageError, match="contiguous"):
        load(paths)


def test_rejects_master_snapshot_drift(tmp_path):
    paths = build_lineage_databases(tmp_path)
    with sqlite3.connect(paths[2]) as conn:
        conn.execute(
            "UPDATE tiktok_master_snapshots SET evidence_hash=? "
            "WHERE post_id='101'",
            ("a" * 64,),
        )

    with pytest.raises(RunLineageError, match="master/local evidence hash mismatch"):
        load(paths)


def test_rejects_zero_post_source_run(tmp_path):
    paths = build_lineage_databases(tmp_path, post_specs=())

    with pytest.raises(RunLineageError, match="at least one post"):
        load(paths)


def test_rejects_a_different_master_database_path(tmp_path):
    source, run_id, _ = build_lineage_databases(tmp_path)
    other_master = tmp_path / "other_master.sqlite"
    sqlite3.connect(other_master).close()

    with pytest.raises(RunLineageError, match="immutable path"):
        load_completed_run_lineage(source, run_id, other_master)


def test_archive_loader_accepts_incomplete_run_and_ignores_master_binding(tmp_path):
    source, run_id, _ = build_lineage_databases(tmp_path)
    with sqlite3.connect(source) as connection:
        connection.execute(
            "UPDATE engage_tiktok_runs SET status=?, master_database=?, error=? "
            "WHERE run_id=?",
            (
                "collection_incomplete",
                r"D:\old\moved\master.sqlite",
                "collection_incomplete: 2/3",
                run_id,
            ),
        )

    result = load_archive_run_posts(source, run_id)
    assert result["source_run"]["status"] == "collection_incomplete"
    assert [row["post_id"] for row in result["candidates"]] == ["202", "101"]
    assert result["skipped"] == []


def test_archive_loader_accepts_relative_and_moved_project_database(
    tmp_path,
    monkeypatch,
):
    source, run_id, _ = build_lineage_databases(tmp_path)
    moved = tmp_path / "restored" / "state.sqlite"
    moved.parent.mkdir()
    shutil.copy2(source, moved)
    monkeypatch.chdir(tmp_path)

    result = load_archive_run_posts(Path("restored") / "state.sqlite", run_id)
    assert result["source_run"]["database_path"] == str(moved.resolve())
    assert len(result["candidates"]) == 2


def test_archive_loader_skips_bad_row_without_blocking_good_rows(tmp_path):
    source, run_id, _ = build_lineage_databases(tmp_path)
    with sqlite3.connect(source) as connection:
        connection.execute(
            "UPDATE engage_tiktok_posts SET url=? WHERE post_id=?",
            ("https://example.com/not-tiktok", "202"),
        )

    result = load_archive_run_posts(source, run_id)
    assert [row["post_id"] for row in result["candidates"]] == ["101"]
    assert result["skipped"] == [
        {"post_id": "202", "reason": "invalid_tiktok_post_url"}
    ]


def register_relocated_copy(source: Path, master: Path) -> Path:
    relocated = source.parent / "restored" / source.name
    relocated.parent.mkdir()
    shutil.copy2(source, relocated)
    conn = sqlite3.connect(master)
    try:
        source_id, identity_path = conn.execute(
            "SELECT source_id,database_path FROM tiktok_master_sources"
        ).fetchone()
        insert_source_path_alias(
            conn, "main", source_id=source_id, identity_path=identity_path,
            database_path=relocated, migration_id="verified-offline-test-migration",
        )
        conn.commit()
    finally:
        conn.close()
    return relocated


def test_verified_relocation_preserves_completed_run_and_snapshot_lineage(tmp_path):
    source, run_id, master = build_lineage_databases(tmp_path)
    original = load_completed_run_lineage(source, run_id, master)
    relocated = register_relocated_copy(source, master)

    restored = load_completed_run_lineage(relocated, run_id, master)

    assert restored["candidates"] == original["candidates"]
    assert restored["source_run"]["run_id"] == original["source_run"]["run_id"]
    assert restored["source_run"]["database_path"] == str(relocated.resolve())
    with pytest.raises(RunLineageError, match="relocated"):
        load_completed_run_lineage(source, run_id, master)


def test_relocated_lineage_still_rejects_changed_local_evidence(tmp_path):
    source, run_id, master = build_lineage_databases(tmp_path)
    relocated = register_relocated_copy(source, master)
    conn = sqlite3.connect(relocated)
    try:
        packet = evidence("101", "first_creator")
        packet["caption"] = "changed after relocation"
        conn.execute(
            "UPDATE engage_tiktok_posts SET evidence_json=?,evidence_hash=? WHERE post_id='101'",
            (canonical_json(packet), canonical_sha256(packet)),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(RunLineageError, match="master/local evidence hash mismatch"):
        load_completed_run_lineage(relocated, run_id, master)
