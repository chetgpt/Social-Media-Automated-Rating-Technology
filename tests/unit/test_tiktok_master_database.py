import json
import sqlite3

import pytest

import tiktok_master_database as master


def create_source(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE engage_tiktok_runs (
            run_id TEXT PRIMARY KEY,
            project TEXT,
            topic TEXT,
            source_mode TEXT,
            creator_handle TEXT,
            creator_identity_json TEXT,
            cardinality_mode TEXT,
            profile_inventory_count INTEGER,
            profile_inventory_terminal INTEGER,
            profile_inventory_hash TEXT,
            requested_count INTEGER,
            max_comments INTEGER,
            max_pages INTEGER,
            mode TEXT,
            workflow TEXT,
            collection_policy TEXT,
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
        CREATE TABLE engage_tiktok_posts (
            run_id TEXT,
            post_id TEXT,
            evidence_ready INTEGER,
            evidence_json TEXT,
            evidence_hash TEXT,
            created_at TEXT
        );
        """
    )
    return conn


def evidence(post_id, *, observed_at, views=100, comments=()):
    return {
        "schema_version": "tiktok-engage-evidence-v1",
        "platform": "tiktok",
        "topic": "3D printing",
        "post_id": str(post_id),
        "url": f"https://www.tiktok.com/@maker/video/{post_id}",
        "creator": "maker",
        "caption": "A useful print setup",
        "metrics": {"views": views, "likes": 10},
        "transcript": "",
        "transcript_status": "unavailable",
        "comments": list(comments),
        "comments_status": {"ok": True, "complete": True},
        "observed_at": observed_at,
        "evidence_ready": True,
        "readiness_issues": [],
    }


def music_evidence(schema_version, *, status="matched"):
    document = {
        "schema_version": schema_version,
        "platform_music": {"status": "available"},
        "catalogs": {"musicbrainz": {"status": status}},
    }
    document["music_evidence_hash"] = master.json_hash(document)
    return document


def add_run(
    conn,
    run_id,
    packet,
    *,
    workflow="listen",
    collection_policy="new_only",
):
    conn.execute(
        """
        INSERT INTO engage_tiktok_runs (
            run_id, project, topic, source_mode, cardinality_mode,
            requested_count, max_comments, max_pages,
            mode, workflow, collection_policy, expected_account,
            observed_account, status, requested, unique_collected,
            evidence_ready, analyzed, drafted, reviewed, stored, authorized,
            published, skipped, failed, error, created_at, updated_at
        ) VALUES (
            ?, 'test', '3D printing', 'topic', 'fixed',
            1, 20, 5, 'shadow', ?, ?, 'agson',
            'agson', 'collection_complete', 1, 1, 1, 0, 0, 0, 0, 0,
            0, 0, 0, '', ?, ?
        )
        """,
        (
            run_id,
            workflow,
            collection_policy,
            packet["observed_at"],
            packet["observed_at"],
        ),
    )
    packet_hash = master.json_hash(packet)
    conn.execute(
        """
        INSERT INTO engage_tiktok_posts (
            run_id, post_id, evidence_ready, evidence_json,
            evidence_hash, created_at
        ) VALUES (?, ?, 1, ?, ?, ?)
        """,
        (
            run_id,
            packet["post_id"],
            json.dumps(packet),
            packet_hash,
            packet["observed_at"],
        ),
    )
    return packet_hash


def add_master_music_post(
    conn,
    source,
    post_id,
    *,
    observed_at,
    music=None,
    creator="maker",
):
    packet = evidence(post_id, observed_at=observed_at)
    packet["creator"] = creator
    packet["url"] = f"https://www.tiktok.com/@{creator}/video/{post_id}"
    if music is not None:
        packet["music_evidence"] = music
    run_id = f"music-source-{post_id}-{observed_at}"
    packet_hash = add_run(conn, run_id, packet)
    return master.record_evidence_snapshot(
        conn,
        "main",
        source_path=source,
        run_id=run_id,
        post_id=post_id,
        evidence=packet,
        evidence_hash=packet_hash,
    )


def add_audit_report(conn, run_id, *, source_mode="topic"):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS engage_tiktok_audit_reports (
            run_id TEXT PRIMARY KEY,
            schema_version TEXT NOT NULL,
            rubric_version TEXT NOT NULL,
            analysis_set_hash TEXT NOT NULL,
            report_json TEXT NOT NULL,
            report_hash TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    generated_at = "2026-08-09T09:00:00+07:00"
    analysis_set_hash = master.json_hash(
        [{"post_id": "123", "analysis_hash": "analysis-123"}]
    )
    rubric_version = (
        "creator-portfolio-v1"
        if source_mode == "creator"
        else "topic-portfolio-v1"
    )
    report = {
        "schema_version": "tiktok-audit-report-v1",
        "rubric_version": rubric_version,
        "run_id": run_id,
        "source_mode": source_mode,
        "project": "test",
        "topic": "3D printing",
        "collection_policy": "new_only",
        "cardinality_mode": "fixed",
        "mode": "shadow",
        "requested_count": 1,
        "analyzed_count": 1,
        "analysis_set_hash": analysis_set_hash,
        "ratings": {"portfolio_score": 6.4},
        "coverage": {"analyzed": 1, "requested": 1},
    }
    if source_mode == "creator":
        report.update(
            {
                "creator_handle": "maker",
                "profile_inventory_hash": "inventory-123",
            }
        )
    report_hash = master.json_hash(report)
    conn.execute(
        """
        INSERT INTO engage_tiktok_audit_reports (
            run_id, schema_version, rubric_version, analysis_set_hash,
            report_json, report_hash, generated_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            "tiktok-audit-report-v1",
            rubric_version,
            analysis_set_hash,
            master.canonical_json(report),
            report_hash,
            generated_at,
            generated_at,
        ),
    )
    return report, report_hash


def confirmed_comment_attempt(
    conn,
    *,
    post_id,
    publication_id,
    remote_comment_id,
    account="commenter",
    source_path="comment-project.sqlite",
):
    attempt_id = master.register_publication_claim(
        conn,
        "main",
        account=account,
        post_id=post_id,
        publication_id=publication_id,
        source_path=source_path,
        text_hash=f"text-{publication_id}",
    )
    master.mark_submit_intent(conn, "main", attempt_id)
    assert (
        master.register_publication_outcome(
            conn,
            "main",
            attempt_id=attempt_id,
            outcome="published",
            receipt_id=f"receipt-{publication_id}",
            remote_comment_id=remote_comment_id,
            remote_comment_url=(
                f"https://www.tiktok.com/@{account}/video/"
                f"{post_id}?comment={remote_comment_id}"
            ),
        )
        == "confirmed"
    )
    return attempt_id


def test_snapshots_track_metric_and_new_comment_deltas(tmp_path):
    source = tmp_path / "project.sqlite"
    conn = create_source(source)
    master.ensure_master_schema(conn)
    first = evidence(
        "123",
        observed_at="2026-07-28T10:00:00+07:00",
        views=100,
        comments=[{"cid": "c1", "text": "first"}],
    )
    first_hash = add_run(conn, "run-1", first)
    first_result = master.record_evidence_snapshot(
        conn,
        "main",
        source_path=source,
        run_id="run-1",
        post_id="123",
        evidence=first,
        evidence_hash=first_hash,
        account="agson",
    )

    second = evidence(
        "123",
        observed_at="2026-07-29T10:00:00+07:00",
        views=175,
        comments=[
            {"cid": "c1", "text": "first"},
            {"cid": "c2", "text": "new reply"},
        ],
    )
    second_hash = add_run(
        conn,
        "run-2",
        second,
        collection_policy="refresh_known",
    )
    second_result = master.record_evidence_snapshot(
        conn,
        "main",
        source_path=source,
        run_id="run-2",
        post_id="123",
        evidence=second,
        evidence_hash=second_hash,
        account="agson",
    )

    assert first_result["new_comment_count"] == 1
    assert second_result["is_changed"] is True
    assert second_result["new_comment_count"] == 1
    assert second_result["change"]["metric_changes"]["views"]["delta"] == 75
    assert master.master_summary(conn)["known_comments"] == 2
    conn.close()


def test_unchanged_refresh_still_appends_an_observation(tmp_path):
    source = tmp_path / "project.sqlite"
    conn = create_source(source)
    master.ensure_master_schema(conn)
    first = evidence(
        "123",
        observed_at="2026-07-28T10:00:00+07:00",
        comments=[{"cid": "c1", "text": "same"}],
    )
    first_hash = add_run(conn, "run-1", first)
    master.record_evidence_snapshot(
        conn,
        "main",
        source_path=source,
        run_id="run-1",
        post_id="123",
        evidence=first,
        evidence_hash=first_hash,
    )
    unchanged = {
        **first,
        "observed_at": "2026-07-29T10:00:00+07:00",
    }
    unchanged_hash = add_run(
        conn,
        "run-2",
        unchanged,
        collection_policy="refresh_known",
    )
    result = master.record_evidence_snapshot(
        conn,
        "main",
        source_path=source,
        run_id="run-2",
        post_id="123",
        evidence=unchanged,
        evidence_hash=unchanged_hash,
    )

    assert result["is_changed"] is False
    assert result["new_comment_count"] == 0
    assert master.master_summary(conn)["snapshots"] == 2
    conn.close()


def test_import_is_idempotent_and_deduplicates_across_sources(tmp_path):
    master_path = tmp_path / "master.sqlite"
    for index in (1, 2):
        source = tmp_path / f"source-{index}.sqlite"
        conn = create_source(source)
        packet = evidence(
            "123",
            observed_at=f"2026-07-2{index}T10:00:00+07:00",
            views=100 + index,
        )
        add_run(conn, "same-local-run-id", packet)
        conn.commit()
        conn.close()
        first_import = master.import_legacy_database(master_path, source)
        second_import = master.import_legacy_database(master_path, source)
        assert first_import["snapshots"] == 1
        assert second_import["snapshots"] == 0

    conn = master.connect_master(master_path)
    summary = master.master_summary(conn)
    assert summary["sources"] == 2
    assert summary["runs"] == 2
    assert summary["unique_posts"] == 1
    assert summary["snapshots"] == 2
    conn.close()


def test_audit_run_and_hash_bound_report_sync_idempotently(tmp_path):
    source = tmp_path / "audit-project.sqlite"
    conn = create_source(source)
    master.ensure_master_schema(conn)
    packet = evidence(
        "123",
        observed_at="2026-08-09T08:00:00+07:00",
    )
    add_run(conn, "audit-run", packet, workflow="audit")
    conn.execute(
        """
        UPDATE engage_tiktok_runs
        SET status='audit_complete', analyzed=1
        WHERE run_id='audit-run'
        """
    )
    report, report_hash = add_audit_report(conn, "audit-run")

    first = master.sync_local_database(conn, "main", source)
    second = master.sync_local_database(conn, "main", source)

    assert first["audit_reports"] == 1
    assert second["audit_reports"] == 0
    run_row = conn.execute(
        """
        SELECT workflow, analyzed, drafted, reviewed, published
        FROM tiktok_master_runs
        WHERE local_run_id='audit-run'
        """
    ).fetchone()
    assert tuple(run_row) == ("audit", 1, 0, 0, 0)
    saved = master.audit_report_for_run(
        conn,
        run_id="audit-run",
        source_path=source,
    )
    assert saved is not None
    assert saved["report"] == report
    assert saved["report_hash"] == report_hash
    summary = master.master_summary(conn)
    assert summary["runs_by_workflow"]["audit"] == 1
    assert summary["audit_reports"] == 1
    conn.close()


def test_direct_post_url_syncs_from_a_new_local_run(tmp_path):
    source = tmp_path / "direct-url-project.sqlite"
    conn = create_source(source)
    conn.execute(
        "ALTER TABLE engage_tiktok_runs "
        "ADD COLUMN direct_post_url TEXT NOT NULL DEFAULT ''"
    )
    master.ensure_master_schema(conn)
    target_url = "https://www.tiktok.com/@maker/video/123"
    packet = evidence(
        "123",
        observed_at="2026-08-13T08:00:00+07:00",
    )
    add_run(conn, "direct-url-run", packet, workflow="listen")
    conn.execute(
        """
        UPDATE engage_tiktok_runs
        SET topic='', source_mode='url', direct_post_url=?
        WHERE run_id='direct-url-run'
        """,
        (target_url,),
    )

    first = master.sync_local_database(conn, "main", source)
    second = master.sync_local_database(conn, "main", source)

    assert first["runs"] == 1
    assert second["runs"] == 1
    stored = conn.execute(
        """
        SELECT source_mode, topic, topic_key, direct_post_url
        FROM tiktok_master_runs
        WHERE local_run_id='direct-url-run'
        """
    ).fetchone()
    assert tuple(stored) == ("url", "", "", target_url)
    conn.close()


def test_master_schema_upgrade_adds_direct_post_url_without_losing_runs():
    conn = sqlite3.connect(":memory:")
    master.ensure_master_schema(conn)
    conn.execute(
        """
        INSERT INTO tiktok_master_sources (
            source_id, database_path, first_seen_at, last_seen_at
        ) VALUES (
            'source', 'legacy.sqlite',
            '2026-08-12T00:00:00+00:00',
            '2026-08-12T00:00:00+00:00'
        )
        """
    )
    conn.execute(
        """
        INSERT INTO tiktok_master_runs (
            master_run_id, source_id, local_run_id, project,
            source_mode, status, synced_at
        ) VALUES (
            'master-run', 'source', 'legacy-run', 'legacy-project',
            'topic', 'collection_complete',
            '2026-08-12T00:00:00+00:00'
        )
        """
    )
    conn.execute("ALTER TABLE tiktok_master_runs DROP COLUMN direct_post_url")
    conn.execute(
        "UPDATE tiktok_master_meta SET value='4' WHERE key='schema_version'"
    )

    master.ensure_master_schema(conn)

    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(tiktok_master_runs)")
    }
    assert "direct_post_url" in columns
    stored = conn.execute(
        """
        SELECT local_run_id, project, status, direct_post_url
        FROM tiktok_master_runs
        WHERE master_run_id='master-run'
        """
    ).fetchone()
    assert stored == (
        "legacy-run",
        "legacy-project",
        "collection_complete",
        "",
    )
    assert conn.execute(
        "SELECT value FROM tiktok_master_meta WHERE key='schema_version'"
    ).fetchone()[0] == master.MASTER_SCHEMA_VERSION
    conn.close()


def test_provider_request_slots_are_reserved_workspace_wide():
    conn = sqlite3.connect(":memory:")
    master.ensure_master_schema(conn)
    conn.commit()

    first_delay = master.reserve_provider_request_slot(
        conn,
        "main",
        provider="musicbrainz",
        minimum_interval_seconds=1.05,
    )
    second_delay = master.reserve_provider_request_slot(
        conn,
        "main",
        provider="musicbrainz",
        minimum_interval_seconds=1.05,
    )

    assert first_delay == 0.0
    assert 1.0 <= second_delay <= 1.1
    assert conn.execute(
        "SELECT COUNT(*) FROM tiktok_master_provider_rate_limits "
        "WHERE provider='musicbrainz'"
    ).fetchone()[0] == 1

    master.defer_provider_requests(
        conn,
        "main",
        provider="musicbrainz",
        delay_seconds=30,
    )
    deferred_delay = master.reserve_provider_request_slot(
        conn,
        "main",
        provider="musicbrainz",
        minimum_interval_seconds=1.05,
    )
    assert 29 <= deferred_delay <= 31
    conn.close()


def test_music_backfill_schema_migrates_v6_without_replacing_history(tmp_path):
    source = tmp_path / "music-migration.sqlite"
    conn = create_source(source)
    master.ensure_master_schema(conn)
    add_master_music_post(
        conn,
        source,
        "101",
        observed_at="2026-08-01T01:00:00+00:00",
        music=music_evidence("tiktok-music-evidence-v1"),
    )
    snapshot_before = conn.execute(
        "SELECT snapshot_id, evidence_hash FROM tiktok_master_snapshots"
    ).fetchall()
    conn.execute("DROP TABLE tiktok_master_music_backfill_observations")
    conn.execute("DROP TABLE tiktok_master_music_backfill_runs")
    conn.execute(
        "UPDATE tiktok_master_meta SET value='6' WHERE key='schema_version'"
    )

    master.ensure_master_schema(conn)

    assert conn.execute(
        "SELECT value FROM tiktok_master_meta WHERE key='schema_version'"
    ).fetchone()[0] == master.MASTER_SCHEMA_VERSION
    assert conn.execute(
        "SELECT COUNT(*) FROM tiktok_master_music_backfill_runs"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM tiktok_master_music_backfill_observations"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT snapshot_id, evidence_hash FROM tiktok_master_snapshots"
    ).fetchall() == snapshot_before
    conn.close()


def test_music_backfill_selection_scopes_and_uses_snapshot_evidence(tmp_path):
    source = tmp_path / "music-selection.sqlite"
    conn = create_source(source)
    master.ensure_master_schema(conn)
    add_master_music_post(
        conn,
        source,
        "101",
        observed_at="2026-08-01T01:00:00+00:00",
        music=music_evidence("tiktok-music-evidence-v1"),
    )
    add_master_music_post(
        conn,
        source,
        "102",
        observed_at="2026-08-01T02:00:00+00:00",
        music=music_evidence("tiktok-music-evidence-v3"),
    )
    add_master_music_post(
        conn,
        source,
        "103",
        observed_at="2026-08-01T03:00:00+00:00",
        music=music_evidence(
            "tiktok-music-evidence-v3", status="rate_limited"
        ),
    )
    add_master_music_post(
        conn,
        source,
        "104",
        observed_at="2026-08-01T04:00:00+00:00",
        creator="other",
    )
    # Candidate selection must never trust the denormalized convenience copy.
    deceptive = evidence("101", observed_at="2026-08-01T01:00:00+00:00")
    deceptive["music_evidence"] = music_evidence("tiktok-music-evidence-v3")
    conn.execute(
        "UPDATE tiktok_master_posts SET latest_evidence_json=? WHERE post_id='101'",
        (master.canonical_json(deceptive),),
    )

    creator_rows = master.select_music_backfill_candidates(
        conn, creator_handle="@Maker"
    )
    assert [row["post_id"] for row in creator_rows] == ["101", "103"]
    assert creator_rows[0]["base_music_schema"] == "tiktok-music-evidence-v1"
    assert creator_rows[1]["base_music_status"] == "retryable:rate_limited"
    assert [
        row["post_id"]
        for row in master.select_music_backfill_candidates(
            conn, topic="3D PRINTING"
        )
    ] == ["101", "103", "104"]
    assert [
        row["post_id"]
        for row in master.select_music_backfill_candidates(
            conn, post_ids=("104", "102", "101")
        )
    ] == ["104", "101"]
    assert [
        row["post_id"]
        for row in master.select_music_backfill_candidates(
            conn,
            direct_url="https://www.tiktok.com/@maker/video/101?is_from_webapp=1",
        )
    ] == ["101"]
    assert [
        row["post_id"]
        for row in master.select_music_backfill_candidates(
            conn, creator_handle="maker", force=True
        )
    ] == ["101", "102", "103"]
    with pytest.raises(ValueError, match="exactly one selection scope"):
        master.select_music_backfill_candidates(
            conn, creator_handle="maker", post_ids=("101",)
        )
    conn.close()


def test_music_backfill_is_append_only_and_preserves_master_evidence(tmp_path):
    source = tmp_path / "music-append-only.sqlite"
    conn = create_source(source)
    master.ensure_master_schema(conn)
    add_master_music_post(
        conn,
        source,
        "101",
        observed_at="2026-08-01T01:00:00+00:00",
        music=music_evidence("tiktok-music-evidence-v1"),
    )
    candidates = master.select_music_backfill_candidates(
        conn, creator_handle="maker"
    )
    candidate = candidates[0]
    post_before = tuple(
        conn.execute(
            """
            SELECT latest_snapshot_id, latest_evidence_hash, last_seen_at,
                   snapshot_count, latest_evidence_json
            FROM tiktok_master_posts WHERE post_id='101'
            """
        ).fetchone()
    )
    snapshot_before = tuple(
        conn.execute(
            """
            SELECT evidence_hash, observed_at, evidence_json
            FROM tiktok_master_snapshots WHERE snapshot_id=?
            """,
            (candidate["base_snapshot_id"],),
        ).fetchone()
    )
    registered = master.register_music_backfill_run(
        conn,
        run_id="music-backfill-1",
        project="legacy music",
        scope_mode="creator",
        scope_value="maker",
        target_schema_version="tiktok-music-evidence-v3",
        retryable_statuses=("unavailable", "rate_limited", "provider_error"),
        candidates=candidates,
        expected_account="collector",
        observed_account="collector",
        created_at="2026-08-02T00:00:00+00:00",
    )
    assert registered["created"] is True
    assert registered["selected_count"] == 1
    assert registered["configured_catalogs"] == ["musicbrainz"]
    assert master.register_music_backfill_run(
        conn,
        run_id="music-backfill-1",
        project="legacy music",
        scope_mode="creator",
        scope_value="maker",
        target_schema_version="tiktok-music-evidence-v3",
        retryable_statuses=("unavailable", "rate_limited", "provider_error"),
        candidates=candidates,
        expected_account="collector",
        observed_account="collector",
        created_at="2026-08-02T00:00:00+00:00",
    )["created"] is False

    upgraded = music_evidence("tiktok-music-evidence-v3")
    observation = master.record_music_backfill_observation(
        conn,
        run_id="music-backfill-1",
        post_id="101",
        base_snapshot_id=candidate["base_snapshot_id"],
        base_evidence_hash=candidate["base_evidence_hash"],
        base_observed_at=candidate["base_observed_at"],
        music_observed_at="2026-08-02T01:00:00+00:00",
        music_evidence=upgraded,
        music_evidence_hash=upgraded["music_evidence_hash"],
    )
    assert observation["created"] is True
    assert len(observation["observation_hash"]) == 64
    repeated = master.record_music_backfill_observation(
        conn,
        run_id="music-backfill-1",
        post_id="101",
        base_snapshot_id=candidate["base_snapshot_id"],
        base_evidence_hash=candidate["base_evidence_hash"],
        base_observed_at=candidate["base_observed_at"],
        music_observed_at="2026-08-02T01:00:00+00:00",
        music_evidence=upgraded,
        music_evidence_hash=upgraded["music_evidence_hash"],
    )
    assert repeated["created"] is False
    with pytest.raises(RuntimeError, match="append-only"):
        master.record_music_backfill_observation(
            conn,
            run_id="music-backfill-1",
            post_id="101",
            base_snapshot_id=candidate["base_snapshot_id"],
            base_evidence_hash=candidate["base_evidence_hash"],
            base_observed_at=candidate["base_observed_at"],
            music_observed_at="2026-08-02T02:00:00+00:00",
            music_evidence=upgraded,
            music_evidence_hash=upgraded["music_evidence_hash"],
        )

    final = master.finalize_music_backfill_run(conn, run_id="music-backfill-1")
    assert final["status"] == "backfill_complete"
    assert final["completed_count"] == 1
    stored = master.music_backfill_observations(
        conn, run_id="music-backfill-1"
    )
    assert len(stored) == 1
    assert tuple(
        conn.execute(
            """
            SELECT latest_snapshot_id, latest_evidence_hash, last_seen_at,
                   snapshot_count, latest_evidence_json
            FROM tiktok_master_posts WHERE post_id='101'
            """
        ).fetchone()
    ) == post_before
    assert tuple(
        conn.execute(
            """
            SELECT evidence_hash, observed_at, evidence_json
            FROM tiktok_master_snapshots WHERE snapshot_id=?
            """,
            (candidate["base_snapshot_id"],),
        ).fetchone()
    ) == snapshot_before
    # A successful current v3 observation prevents redundant future backfill.
    assert master.select_music_backfill_candidates(
        conn, creator_handle="maker"
    ) == []
    conn.close()


def test_music_backfill_checkpoint_validates_only_its_bound_base_snapshot(
    tmp_path, monkeypatch
):
    source = tmp_path / "music-checkpoint-scaling.sqlite"
    conn = create_source(source)
    master.ensure_master_schema(conn)
    for index, post_id in enumerate(("101", "102"), start=1):
        add_master_music_post(
            conn,
            source,
            post_id,
            observed_at=f"2026-08-01T0{index}:00:00+00:00",
            music=music_evidence("tiktok-music-evidence-v1"),
        )
    candidates = master.select_music_backfill_candidates(
        conn, creator_handle="maker"
    )
    master.register_music_backfill_run(
        conn,
        run_id="music-checkpoint-scaling",
        project="scaling",
        scope_mode="creator",
        scope_value="maker",
        target_schema_version="tiktok-music-evidence-v3",
        retryable_statuses=("unavailable",),
        candidates=candidates,
    )

    original = master._validated_base_snapshot
    validated_post_ids = []

    def tracked(*args, **kwargs):
        validated_post_ids.append(str(kwargs["post_id"]))
        return original(*args, **kwargs)

    monkeypatch.setattr(master, "_validated_base_snapshot", tracked)
    candidate = candidates[0]
    upgraded = music_evidence("tiktok-music-evidence-v3")
    master.record_music_backfill_observation(
        conn,
        run_id="music-checkpoint-scaling",
        post_id=candidate["post_id"],
        base_snapshot_id=candidate["base_snapshot_id"],
        base_evidence_hash=candidate["base_evidence_hash"],
        base_observed_at=candidate["base_observed_at"],
        music_observed_at="2026-08-02T00:00:00+00:00",
        music_evidence=upgraded,
        music_evidence_hash=upgraded["music_evidence_hash"],
    )

    assert validated_post_ids == [candidate["post_id"]]
    conn.close()


def test_music_backfill_rejects_binding_hash_and_stored_tampering(tmp_path):
    source = tmp_path / "music-tamper.sqlite"
    conn = create_source(source)
    master.ensure_master_schema(conn)
    add_master_music_post(
        conn,
        source,
        "101",
        observed_at="2026-08-01T01:00:00+00:00",
    )
    candidate = master.select_music_backfill_candidates(
        conn, post_ids=("101",)
    )[0]
    master.register_music_backfill_run(
        conn,
        run_id="music-tamper-run",
        project="tamper",
        scope_mode="post_ids",
        scope_value="101",
        target_schema_version="tiktok-music-evidence-v3",
        retryable_statuses=("unavailable",),
        candidates=(candidate,),
    )
    upgraded = music_evidence("tiktok-music-evidence-v3")
    with pytest.raises(RuntimeError, match="base_evidence_hash binding mismatch"):
        master.record_music_backfill_observation(
            conn,
            run_id="music-tamper-run",
            post_id="101",
            base_snapshot_id=candidate["base_snapshot_id"],
            base_evidence_hash="0" * 64,
            base_observed_at=candidate["base_observed_at"],
            music_observed_at="2026-08-02T00:00:00+00:00",
            music_evidence=upgraded,
            music_evidence_hash=upgraded["music_evidence_hash"],
        )
    with pytest.raises(ValueError, match="evidence hash mismatch"):
        master.record_music_backfill_observation(
            conn,
            run_id="music-tamper-run",
            post_id="101",
            base_snapshot_id=candidate["base_snapshot_id"],
            base_evidence_hash=candidate["base_evidence_hash"],
            base_observed_at=candidate["base_observed_at"],
            music_observed_at="2026-08-02T00:00:00+00:00",
            music_evidence=upgraded,
            music_evidence_hash="1" * 64,
        )
    master.record_music_backfill_observation(
        conn,
        run_id="music-tamper-run",
        post_id="101",
        base_snapshot_id=candidate["base_snapshot_id"],
        base_evidence_hash=candidate["base_evidence_hash"],
        base_observed_at=candidate["base_observed_at"],
        music_observed_at="2026-08-02T00:00:00+00:00",
        music_evidence=upgraded,
        music_evidence_hash=upgraded["music_evidence_hash"],
    )
    conn.execute(
        """
        UPDATE tiktok_master_music_backfill_observations
        SET observation_hash=? WHERE run_id='music-tamper-run'
        """,
        ("2" * 64,),
    )
    with pytest.raises(RuntimeError, match="observation hash mismatch"):
        master.music_backfill_observations(conn, run_id="music-tamper-run")
    conn.close()


def test_incomplete_music_backfill_can_resume_and_complete(tmp_path):
    source = tmp_path / "music-resume.sqlite"
    conn = create_source(source)
    master.ensure_master_schema(conn)
    add_master_music_post(
        conn,
        source,
        "101",
        observed_at="2026-08-01T01:00:00+00:00",
    )
    candidate = master.select_music_backfill_candidates(
        conn, post_ids=("101",)
    )[0]
    master.register_music_backfill_run(
        conn,
        run_id="music-resume-run",
        project="resume",
        scope_mode="post_ids",
        scope_value="101",
        target_schema_version="tiktok-music-evidence-v3",
        retryable_statuses=("unavailable",),
        candidates=(candidate,),
    )

    incomplete = master.finalize_music_backfill_run(
        conn, run_id="music-resume-run", error="interrupted"
    )
    assert incomplete["status"] == "backfill_incomplete"
    assert incomplete["completed_at"] == ""
    resumed = master.update_music_backfill_run(
        conn, run_id="music-resume-run", status="running", error=""
    )
    assert resumed["status"] == "running"
    upgraded = music_evidence("tiktok-music-evidence-v3")
    master.record_music_backfill_observation(
        conn,
        run_id="music-resume-run",
        post_id="101",
        base_snapshot_id=candidate["base_snapshot_id"],
        base_evidence_hash=candidate["base_evidence_hash"],
        base_observed_at=candidate["base_observed_at"],
        music_observed_at="2026-08-02T00:00:00+00:00",
        music_evidence=upgraded,
        music_evidence_hash=upgraded["music_evidence_hash"],
    )
    completed = master.finalize_music_backfill_run(
        conn, run_id="music-resume-run"
    )
    assert completed["status"] == "backfill_complete"
    assert completed["completed_count"] == 1
    assert completed["completed_at"]
    conn.close()


def test_audit_report_rejects_tampering_and_immutable_drift(tmp_path):
    source = tmp_path / "audit-project.sqlite"
    conn = create_source(source)
    master.ensure_master_schema(conn)
    packet = evidence(
        "123",
        observed_at="2026-08-09T08:00:00+07:00",
    )
    add_run(conn, "audit-run", packet, workflow="audit")
    conn.execute(
        "UPDATE engage_tiktok_runs SET analyzed=1 WHERE run_id='audit-run'"
    )
    report, _ = add_audit_report(conn, "audit-run")
    master.sync_local_database(conn, "main", source)

    tampered = {**report, "ratings": {"portfolio_score": 9.9}}
    conn.execute(
        """
        UPDATE engage_tiktok_audit_reports
        SET report_json=?
        WHERE run_id='audit-run'
        """,
        (master.canonical_json(tampered),),
    )
    with pytest.raises(ValueError, match="hash does not match"):
        master.register_audit_report_from_local(
            conn,
            "main",
            "audit-run",
            source,
        )

    conn.execute(
        """
        UPDATE engage_tiktok_audit_reports
        SET report_hash=?
        WHERE run_id='audit-run'
        """,
        (master.json_hash(tampered),),
    )
    with pytest.raises(RuntimeError, match="binding drift"):
        master.register_audit_report_from_local(
            conn,
            "main",
            "audit-run",
            source,
        )

    conn.execute(
        """
        UPDATE tiktok_master_audit_reports
        SET report_json='{}'
        WHERE local_run_id='audit-run'
        """
    )
    with pytest.raises(RuntimeError, match="hash mismatch"):
        master.audit_report_for_run(
            conn,
            run_id="audit-run",
            source_path=source,
        )
    conn.close()


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("project", "other-project"),
        ("topic", "other topic"),
        ("collection_policy", "refresh_known"),
        ("cardinality_mode", "all"),
        ("mode", "live"),
    ],
)
def test_audit_report_rejects_local_scope_mismatch(tmp_path, field, bad_value):
    source = tmp_path / f"scope-{field}.sqlite"
    conn = create_source(source)
    master.ensure_master_schema(conn)
    packet = evidence(
        "123",
        observed_at="2026-08-09T08:00:00+07:00",
    )
    add_run(conn, "audit-run", packet, workflow="audit")
    conn.execute(
        "UPDATE engage_tiktok_runs SET status='audit_complete', analyzed=1 "
        "WHERE run_id='audit-run'"
    )
    report, _ = add_audit_report(conn, "audit-run")
    report[field] = bad_value
    conn.execute(
        """
        UPDATE engage_tiktok_audit_reports
        SET report_json=?, report_hash=?
        WHERE run_id='audit-run'
        """,
        (master.canonical_json(report), master.json_hash(report)),
    )
    with pytest.raises(ValueError, match=field):
        master.register_audit_report_from_local(
            conn,
            "main",
            "audit-run",
            source,
        )
    conn.close()


def test_master_audit_report_read_rejects_master_scope_drift(tmp_path):
    source = tmp_path / "master-scope.sqlite"
    conn = create_source(source)
    master.ensure_master_schema(conn)
    packet = evidence(
        "123",
        observed_at="2026-08-09T08:00:00+07:00",
    )
    add_run(conn, "audit-run", packet, workflow="audit")
    conn.execute(
        "UPDATE engage_tiktok_runs SET status='audit_complete', analyzed=1 "
        "WHERE run_id='audit-run'"
    )
    add_audit_report(conn, "audit-run")
    master.sync_local_database(conn, "main", source)
    conn.execute(
        "UPDATE tiktok_master_runs SET topic='scope drift' "
        "WHERE local_run_id='audit-run'"
    )
    with pytest.raises(RuntimeError, match="binding mismatch"):
        master.audit_report_for_run(
            conn,
            run_id="audit-run",
            source_path=source,
        )
    conn.close()


def test_audit_report_rejects_non_audit_and_incomplete_runs(tmp_path):
    source = tmp_path / "listen-project.sqlite"
    conn = create_source(source)
    master.ensure_master_schema(conn)
    packet = evidence(
        "123",
        observed_at="2026-08-09T08:00:00+07:00",
    )
    add_run(conn, "listen-run", packet, workflow="listen")
    add_audit_report(conn, "listen-run")
    with pytest.raises(ValueError, match="workflow=audit"):
        master.register_audit_report_from_local(
            conn,
            "main",
            "listen-run",
            source,
        )

    conn.execute(
        "UPDATE engage_tiktok_runs SET workflow='audit' WHERE run_id='listen-run'"
    )
    with pytest.raises(ValueError, match="exact completed"):
        master.register_audit_report_from_local(
            conn,
            "main",
            "listen-run",
            source,
        )
    conn.close()


def test_collection_leases_block_other_runs_but_allow_same_run_resume():
    conn = sqlite3.connect(":memory:")
    master.ensure_master_schema(conn)

    assert master.reserve_collection_candidate(
        conn,
        "main",
        post_id="999",
        run_id="run-1",
        attempt_id="attempt-1",
        policy="new_only",
        source_path="one.sqlite",
    )
    assert not master.reserve_collection_candidate(
        conn,
        "main",
        post_id="999",
        run_id="run-2",
        attempt_id="attempt-2",
        policy="new_only",
        source_path="two.sqlite",
    )
    assert master.reserve_collection_candidate(
        conn,
        "main",
        post_id="999",
        run_id="run-1",
        attempt_id="attempt-resume",
        policy="new_only",
        source_path="one.sqlite",
    )
    assert master.release_collection_candidate(
        conn,
        "main",
        post_id="999",
        run_id="run-1",
        attempt_id="attempt-resume",
    )
    conn.close()


def test_schema_upgrade_backfills_creator_key_without_replacing_post_history():
    conn = sqlite3.connect(":memory:")
    master.ensure_master_schema(conn)
    conn.execute("DROP TABLE tiktok_master_audit_reports")
    conn.execute(
        """
        UPDATE tiktok_master_meta SET value='3'
        WHERE key='schema_version'
        """
    )
    conn.execute("DROP INDEX idx_tiktok_master_runs_creator")
    for column in (
        "source_mode",
        "direct_post_url",
        "creator_handle",
        "cardinality_mode",
        "profile_inventory_count",
        "profile_inventory_terminal",
        "profile_inventory_hash",
    ):
        conn.execute(f"ALTER TABLE tiktok_master_runs DROP COLUMN {column}")
    conn.execute("DROP INDEX idx_tiktok_master_posts_creator")
    conn.execute("DROP INDEX idx_tiktok_master_posts_creator_identity")
    conn.execute("ALTER TABLE tiktok_master_posts DROP COLUMN creator_key")
    conn.execute("ALTER TABLE tiktok_master_posts DROP COLUMN creator_user_id")
    conn.execute("ALTER TABLE tiktok_master_posts DROP COLUMN creator_sec_uid")
    conn.execute("ALTER TABLE tiktok_master_snapshots DROP COLUMN creator_user_id")
    conn.execute("ALTER TABLE tiktok_master_snapshots DROP COLUMN creator_sec_uid")
    conn.execute(
        """
        INSERT INTO tiktok_master_posts (
            post_id, canonical_url, creator_handle,
            first_seen_at, last_seen_at, updated_at,
            snapshot_count, run_count
        ) VALUES (
            'legacy-1',
            'https://www.tiktok.com/@LegacyMaker/video/legacy-1',
            '',
            '2026-07-01T00:00:00+00:00',
            '2026-07-02T00:00:00+00:00',
            '2026-07-02T00:00:00+00:00',
            4,
            3
        )
        """
    )
    legacy_packet = evidence(
        "legacy-1",
        observed_at="2026-07-02T00:00:00+00:00",
    )
    legacy_packet["creator"] = "LegacyMaker"
    legacy_packet["url"] = (
        "https://www.tiktok.com/@LegacyMaker/video/legacy-1"
    )
    legacy_packet["creator_identity"] = {
        "id": "legacy-stable-user",
        "sec_uid": "legacy-stable-secuid",
    }
    conn.execute(
        """
        INSERT INTO tiktok_master_snapshots (
            snapshot_id, post_id, source_id, master_run_id, local_run_id,
            observed_at, evidence_hash, content_hash, evidence_json, created_at
        ) VALUES (?, 'legacy-1', 'source', 'master-run', 'local-run',
                  '2026-07-02T00:00:00+00:00', 'evidence-hash',
                  'content-hash', ?, '2026-07-02T00:00:00+00:00')
        """,
        ("legacy-snapshot", json.dumps(legacy_packet)),
    )

    master.ensure_master_schema(conn)

    row = conn.execute(
        """
        SELECT creator_key, creator_user_id, creator_sec_uid,
               snapshot_count, run_count, last_seen_at
        FROM tiktok_master_posts
        WHERE post_id='legacy-1'
        """
    ).fetchone()
    assert row == (
        "legacymaker",
        "legacy-stable-user",
        "legacy-stable-secuid",
        4,
        3,
        "2026-07-02T00:00:00+00:00",
    )
    assert conn.execute(
        "SELECT value FROM tiktok_master_meta WHERE key='schema_version'"
    ).fetchone()[0] == master.MASTER_SCHEMA_VERSION
    assert conn.execute(
        """
        SELECT creator_user_id, creator_sec_uid
        FROM tiktok_master_snapshots
        WHERE snapshot_id='legacy-snapshot'
        """
    ).fetchone() == ("legacy-stable-user", "legacy-stable-secuid")
    run_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(tiktok_master_runs)")
    }
    assert {
        "source_mode",
        "direct_post_url",
        "creator_handle",
        "cardinality_mode",
        "profile_inventory_count",
        "profile_inventory_terminal",
        "profile_inventory_hash",
    } <= run_columns
    audit_report_columns = {
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(tiktok_master_audit_reports)"
        )
    }
    assert {
        "master_run_id",
        "analysis_set_hash",
        "report_json",
        "report_hash",
    } <= audit_report_columns
    conn.close()


def test_comment_history_is_account_scoped_and_submit_intent_is_fail_closed():
    conn = sqlite3.connect(":memory:")
    master.ensure_master_schema(conn)
    attempt_id = master.register_publication_claim(
        conn,
        "main",
        account="agson",
        post_id="123",
        publication_id="pub-1",
        source_path="one.sqlite",
    )
    master.mark_submit_intent(conn, "main", attempt_id)
    state = master.register_publication_outcome(
        conn,
        "main",
        attempt_id=attempt_id,
        outcome="failed",
        error="click outcome unknown",
    )

    assert state == "uncertain"
    assert master.comment_target_guard(
        conn,
        "main",
        account="agson",
        post_id="123",
    )["blocked"]
    assert not master.comment_target_guard(
        conn,
        "main",
        account="another-account",
        post_id="123",
    )["blocked"]
    with pytest.raises(RuntimeError, match="already has"):
        master.register_publication_claim(
            conn,
            "main",
            account="agson",
            post_id="123",
            publication_id="pub-2",
            source_path="two.sqlite",
        )
    conn.close()


def test_pre_submit_failure_is_retryable_and_unknown_account_blocks_globally():
    conn = sqlite3.connect(":memory:")
    master.ensure_master_schema(conn)
    first = master.register_publication_claim(
        conn,
        "main",
        account="agson",
        post_id="321",
        publication_id="pub-1",
        source_path="one.sqlite",
    )
    assert (
        master.register_publication_outcome(
            conn,
            "main",
            attempt_id=first,
            outcome="pre_submit_failed",
        )
        == "retryable"
    )
    assert master.register_publication_claim(
        conn,
        "main",
        account="agson",
        post_id="321",
        publication_id="pub-2",
        source_path="one.sqlite",
    )

    unknown = master.register_publication_claim(
        conn,
        "main",
        account="",
        post_id="777",
        publication_id="legacy-pub",
        source_path="legacy.sqlite",
    )
    master.register_publication_outcome(
        conn,
        "main",
        attempt_id=unknown,
        outcome="published",
    )
    assert master.comment_target_guard(
        conn,
        "main",
        account="any-account",
        post_id="777",
    )["blocked"]

    assert master.normalize_account("unknown") == "*"
    assert master.normalize_account("unverified") == "*"
    conn.close()


def test_backfilled_snapshots_rebuild_timezone_safe_chronology(tmp_path):
    source = tmp_path / "project.sqlite"
    conn = create_source(source)
    master.ensure_master_schema(conn)

    newer = evidence(
        "123",
        observed_at="2026-07-29T03:00:00+00:00",
        views=200,
        comments=[{"cid": "c1", "text": "newer"}],
    )
    newer_hash = add_run(
        conn,
        "run-newer",
        newer,
        collection_policy="refresh_known",
    )
    newer_result = master.record_evidence_snapshot(
        conn,
        "main",
        source_path=source,
        run_id="run-newer",
        post_id="123",
        evidence=newer,
        evidence_hash=newer_hash,
    )

    older = evidence(
        "123",
        observed_at="2026-07-29T09:00:00+07:00",
        views=100,
        comments=[],
    )
    older_hash = add_run(conn, "run-older", older)
    older_result = master.record_evidence_snapshot(
        conn,
        "main",
        source_path=source,
        run_id="run-older",
        post_id="123",
        evidence=older,
        evidence_hash=older_hash,
    )

    rows = conn.execute(
        """
        SELECT snapshot_id, previous_snapshot_id, observed_at, change_json
        FROM tiktok_master_snapshots
        WHERE post_id='123'
        ORDER BY julianday(observed_at), snapshot_id
        """
    ).fetchall()
    assert [row[2] for row in rows] == [
        "2026-07-29T02:00:00+00:00",
        "2026-07-29T03:00:00+00:00",
    ]
    assert rows[0][0] == older_result["snapshot_id"]
    assert rows[0][1] == ""
    assert rows[1][0] == newer_result["snapshot_id"]
    assert rows[1][1] == older_result["snapshot_id"]
    newer_change = json.loads(rows[1][3])
    assert newer_change["metric_changes"]["views"]["delta"] == 100
    latest = conn.execute(
        """
        SELECT p.last_seen_at, s.evidence_json
        FROM tiktok_master_posts p
        JOIN tiktok_master_snapshots s
          ON s.snapshot_id=p.latest_snapshot_id
        WHERE p.post_id='123'
        """
    ).fetchone()
    assert latest[0] == "2026-07-29T03:00:00+00:00"
    assert json.loads(latest[1])["metrics"]["views"] == 200
    selected = master.select_refresh_candidates(
        conn,
        "main",
        post_ids=["123"],
    )
    assert len(selected) == 1
    assert json.loads(selected[0]["latest_evidence_json"])["metrics"][
        "views"
    ] == 200
    assert conn.execute(
        """
        SELECT latest_evidence_json
        FROM tiktok_master_posts WHERE post_id='123'
        """
    ).fetchone()[0] == "{}"
    conn.close()


def test_creator_scoped_selection_is_normalized_indexed_and_account_independent(
    tmp_path,
):
    source = tmp_path / "creator-project.sqlite"
    conn = create_source(source)
    master.ensure_master_schema(conn)

    packets = []
    for run_id, post_id, creator, observed_at in (
        ("run-maker-old", "101", "Maker", "2026-07-27T10:00:00+00:00"),
        ("run-maker-new", "102", "@maker", "2026-07-29T10:00:00+00:00"),
        ("run-other", "201", "other", "2026-07-26T10:00:00+00:00"),
    ):
        packet = evidence(post_id, observed_at=observed_at)
        packet["creator"] = creator
        packet["url"] = (
            f"https://www.tiktok.com/@{creator.lstrip('@')}/video/{post_id}"
        )
        packet_hash = add_run(conn, run_id, packet)
        master.record_evidence_snapshot(
            conn,
            "main",
            source_path=source,
            run_id=run_id,
            post_id=post_id,
            evidence=packet,
            evidence_hash=packet_hash,
            account="collector",
        )
        packets.append(packet)

    assert master.normalize_creator_handle("@MAKER") == "maker"
    assert (
        master.normalize_creator_handle(
            "https://www.tiktok.com/@Maker/video/101?lang=en"
        )
        == "maker"
    )
    assert master.normalize_creator_handle("https://www.tiktok.com/t/abc") == ""
    selected = master.select_refresh_candidates(
        conn,
        "main",
        creator_handle="https://www.tiktok.com/@MAKER",
    )
    assert [item["post_id"] for item in selected] == ["101", "102"]
    assert {item["creator_key"] for item in selected} == {"maker"}
    assert master.known_creator_post_ids(
        conn,
        creator_handle="@maker",
    ) == {"101", "102"}

    stale = master.select_refresh_candidates(
        conn,
        creator_handle="maker",
        stale_before="2026-07-28T00:00:00+00:00",
    )
    assert [item["post_id"] for item in stale] == ["101"]
    explicit = master.select_refresh_candidates(
        conn,
        topic="topic is ignored for explicit IDs",
        creator_handle="maker",
        post_ids=["201", "102", "101"],
    )
    assert [item["post_id"] for item in explicit] == ["102", "101"]

    # Existing topic-only behavior remains unchanged.
    topic_selected = master.select_refresh_candidates(
        conn,
        topic="  3D   PRINTING ",
    )
    assert {item["post_id"] for item in topic_selected} == {
        "101",
        "102",
        "201",
    }
    creator_index = conn.execute(
        "PRAGMA index_info('idx_tiktok_master_posts_creator')"
    ).fetchall()
    assert [row[2] for row in creator_index] == [
        "creator_key",
        "last_seen_at",
        "post_id",
    ]

    conn.execute(
        """
        UPDATE engage_tiktok_runs SET
            source_mode='creator_profile',
            creator_handle='@Maker',
            cardinality_mode='all',
            profile_inventory_count=2,
            profile_inventory_terminal=1,
            profile_inventory_hash='inventory-hash-101-102'
        WHERE run_id='run-maker-old'
        """
    )
    master.register_run_from_local(
        conn,
        "main",
        "run-maker-old",
        source,
    )
    run_metadata = conn.execute(
        """
        SELECT source_mode, creator_handle, cardinality_mode,
               profile_inventory_count, profile_inventory_terminal,
               profile_inventory_hash
        FROM tiktok_master_runs
        WHERE local_run_id='run-maker-old'
        """
    ).fetchone()
    assert tuple(run_metadata) == (
        "creator_profile",
        "Maker",
        "all",
        2,
        1,
        "inventory-hash-101-102",
    )

    confirmed_comment_attempt(
        conn,
        post_id="101",
        publication_id="pub-maker-agson",
        remote_comment_id="comment-101",
        account="Agson",
    )
    confirmed_comment_attempt(
        conn,
        post_id="102",
        publication_id="pub-maker-other",
        remote_comment_id="comment-102",
        account="other-account",
    )
    confirmed_comment_attempt(
        conn,
        post_id="201",
        publication_id="pub-legacy",
        remote_comment_id="comment-201",
        account="unknown",
    )
    assert master.confirmed_comment_post_ids(
        conn,
        account="@AGSON",
        post_ids=["101", "102", "201"],
    ) == {"101", "201"}
    assert master.confirmed_comment_post_ids(
        conn,
        account="other-account",
    ) == {"102", "201"}
    assert master.blocked_comment_post_ids(
        conn,
        account="@AGSON",
        creator_handle="@maker",
    ) == {"101"}
    assert master.blocked_comment_post_ids(
        conn,
        account="other-account",
        creator_handle="https://www.tiktok.com/@Maker",
    ) == {"102"}
    assert master.blocked_comment_post_ids(
        conn,
        account="agson",
    ) == {"101", "201"}
    confirmed_comment_attempt(
        conn,
        post_id="legacy-comment-only",
        publication_id="pub-comment-only",
        remote_comment_id="comment-only",
        account="agson",
    )
    assert master.blocked_comment_post_ids(
        conn,
        account="agson",
    ) == {"101", "201", "legacy-comment-only"}
    # A legacy comment-only target has no verified creator owner to join, so a
    # creator-filtered inspection excludes it. The account-wide guard above
    # remains fail-closed and is the path used by collection.
    assert master.blocked_comment_post_ids(
        conn,
        account="agson",
        creator_handle="maker",
    ) == {"101"}
    with pytest.raises(ValueError, match="verified TikTok posting account"):
        master.confirmed_comment_post_ids(conn, account="unverified")
    with pytest.raises(ValueError, match="verified TikTok posting account"):
        master.blocked_comment_post_ids(conn, account="")
    conn.close()


def test_creator_scope_rename_requires_matching_stable_identity(tmp_path):
    source = tmp_path / "creator-owner.sqlite"
    conn = create_source(source)
    master.ensure_master_schema(conn)

    def checkpoint(run_id, creator, observed_at, identity=None):
        packet = evidence("owner-post", observed_at=observed_at)
        packet["creator"] = creator
        packet["url"] = (
            f"https://www.tiktok.com/@{creator}/video/owner-post"
        )
        if identity is not None:
            packet["creator_identity"] = identity
        packet_hash = add_run(
            conn,
            run_id,
            packet,
            collection_policy=(
                "new_only" if run_id == "owner-first" else "refresh_known"
            ),
        )
        return master.record_evidence_snapshot(
            conn,
            "main",
            source_path=source,
            run_id=run_id,
            post_id="owner-post",
            evidence=packet,
            evidence_hash=packet_hash,
        )

    stable_identity = {"id": "stable-user-1", "sec_uid": "stable-sec-1"}
    checkpoint(
        "owner-first",
        "old.handle",
        "2026-07-01T00:00:00+00:00",
        stable_identity,
    )
    checkpoint(
        "owner-rename",
        "new.handle",
        "2026-07-02T00:00:00+00:00",
        stable_identity,
    )

    owner = conn.execute(
        """
        SELECT creator_handle, creator_key, creator_user_id, creator_sec_uid,
               snapshot_count
        FROM tiktok_master_posts WHERE post_id='owner-post'
        """
    ).fetchone()
    assert tuple(owner) == (
        "new.handle",
        "new.handle",
        "stable-user-1",
        "stable-sec-1",
        2,
    )
    assert master.select_refresh_candidates(
        conn,
        creator_handle="old.handle",
    ) == []
    renamed = master.select_refresh_candidates(
        conn,
        creator_handle="@NEW.HANDLE",
    )
    assert [item["post_id"] for item in renamed] == ["owner-post"]
    assert renamed[0]["creator_user_id"] == "stable-user-1"
    assert renamed[0]["creator_sec_uid"] == "stable-sec-1"

    conflicting_url = evidence(
        "owner-post",
        observed_at="2026-07-02T12:00:00+00:00",
    )
    conflicting_url["creator"] = "new.handle"
    conflicting_url["url"] = (
        "https://www.tiktok.com/@different.handle/video/owner-post"
    )
    conflicting_url["creator_identity"] = stable_identity
    conflicting_url_hash = add_run(
        conn,
        "owner-conflicting-url",
        conflicting_url,
        collection_policy="refresh_known",
    )
    with pytest.raises(
        master.CreatorIdentityConflictError,
        match="conflicting TikTok creator handles",
    ):
        master.record_evidence_snapshot(
            conn,
            "main",
            source_path=source,
            run_id="owner-conflicting-url",
            post_id="owner-post",
            evidence=conflicting_url,
            evidence_hash=conflicting_url_hash,
        )

    with pytest.raises(master.CreatorIdentityConflictError, match="user ID drift"):
        checkpoint(
            "owner-impostor",
            "impostor",
            "2026-07-03T00:00:00+00:00",
            {"id": "different-user", "sec_uid": "different-sec"},
        )
    unchanged = conn.execute(
        """
        SELECT creator_key, snapshot_count
        FROM tiktok_master_posts WHERE post_id='owner-post'
        """
    ).fetchone()
    assert tuple(unchanged) == ("new.handle", 2)
    assert conn.execute(
        """
        SELECT COUNT(*) FROM tiktok_master_snapshots
        WHERE post_id='owner-post'
        """
    ).fetchone()[0] == 2
    conn.close()


def test_creator_scope_rejects_unproven_handle_move_and_uses_run_identity(
    tmp_path,
):
    source = tmp_path / "creator-owner-fallback.sqlite"
    conn = create_source(source)
    master.ensure_master_schema(conn)

    first = evidence("unproven-post", observed_at="2026-07-01T00:00:00+00:00")
    first["creator"] = "original"
    first["url"] = "https://www.tiktok.com/@original/video/unproven-post"
    first_hash = add_run(conn, "unproven-first", first)
    master.record_evidence_snapshot(
        conn,
        "main",
        source_path=source,
        run_id="unproven-first",
        post_id="unproven-post",
        evidence=first,
        evidence_hash=first_hash,
    )
    moved = {
        **first,
        "creator": "unproven-move",
        "url": "https://www.tiktok.com/@unproven-move/video/unproven-post",
        "observed_at": "2026-07-02T00:00:00+00:00",
    }
    moved_hash = add_run(
        conn,
        "unproven-second",
        moved,
        collection_policy="refresh_known",
    )
    with pytest.raises(
        master.CreatorIdentityConflictError,
        match="handle drift lacks stable-ID continuity",
    ):
        master.record_evidence_snapshot(
            conn,
            "main",
            source_path=source,
            run_id="unproven-second",
            post_id="unproven-post",
            evidence=moved,
            evidence_hash=moved_hash,
        )
    assert master.known_creator_post_ids(
        conn,
        creator_handle="original",
    ) == {"unproven-post"}
    assert master.known_creator_post_ids(
        conn,
        creator_handle="unproven-move",
    ) == set()

    fallback = evidence(
        "run-identity-post",
        observed_at="2026-07-03T00:00:00+00:00",
    )
    fallback["creator"] = "maker"
    fallback["url"] = (
        "https://www.tiktok.com/@maker/video/run-identity-post"
    )
    fallback_hash = add_run(conn, "run-identity", fallback)
    conn.execute(
        """
        UPDATE engage_tiktok_runs SET
            source_mode='creator', creator_handle='maker',
            creator_identity_json=?
        WHERE run_id='run-identity'
        """,
        (json.dumps({"handle": "maker", "id": "run-user", "sec_uid": "run-sec"}),),
    )
    master.record_evidence_snapshot(
        conn,
        "main",
        source_path=source,
        run_id="run-identity",
        post_id="run-identity-post",
        evidence=fallback,
        evidence_hash=fallback_hash,
    )
    assert tuple(
        conn.execute(
            """
            SELECT creator_user_id, creator_sec_uid
            FROM tiktok_master_posts
            WHERE post_id='run-identity-post'
            """
        ).fetchone()
    ) == ("run-user", "run-sec")
    assert tuple(
        conn.execute(
            """
            SELECT creator_user_id, creator_sec_uid
            FROM tiktok_master_snapshots
            WHERE post_id='run-identity-post'
            """
        ).fetchone()
    ) == ("run-user", "run-sec")
    conn.close()


def test_delta_explains_comment_metadata_and_subtitle_changes(tmp_path):
    source = tmp_path / "project.sqlite"
    conn = create_source(source)
    master.ensure_master_schema(conn)
    first = evidence(
        "456",
        observed_at="2026-07-28T10:00:00+00:00",
        comments=[
            {
                "cid": "c1",
                "text": "same text",
                "digg_count": 1,
                "author_pin": False,
            }
        ],
    )
    first["subtitle_tracks"] = []
    first_hash = add_run(conn, "run-1", first)
    master.record_evidence_snapshot(
        conn,
        "main",
        source_path=source,
        run_id="run-1",
        post_id="456",
        evidence=first,
        evidence_hash=first_hash,
    )

    second = evidence(
        "456",
        observed_at="2026-07-29T10:00:00+00:00",
        comments=[
            {
                "cid": "c1",
                "text": "same text",
                "digg_count": 9,
                "author_pin": True,
            }
        ],
    )
    second["subtitle_tracks"] = [{"language": "en", "kind": "captions"}]
    second_hash = add_run(
        conn,
        "run-2",
        second,
        collection_policy="refresh_known",
    )
    result = master.record_evidence_snapshot(
        conn,
        "main",
        source_path=source,
        run_id="run-2",
        post_id="456",
        evidence=second,
        evidence_hash=second_hash,
    )

    change = result["change"]
    assert result["is_changed"] is True
    assert "subtitle_tracks" in change["changed_fields"]
    assert "comments_changed" in change["changed_components"]
    assert set(change["changed_comment_fields"]["id:c1"]) >= {
        "likes",
        "creator_pinned",
    }
    conn.close()


def test_publication_only_legacy_import_is_idempotent_and_upgrades(tmp_path):
    source = tmp_path / "publication-only.sqlite"
    master_path = tmp_path / "master.sqlite"
    conn = sqlite3.connect(source)
    conn.execute(
        """
        CREATE TABLE publication_queue (
            publication_id TEXT PRIMARY KEY,
            content_key TEXT,
            target_url TEXT,
            expected_account TEXT,
            status TEXT,
            draft_hash TEXT,
            remote_comment_id TEXT,
            published_at TEXT,
            engage_run_id TEXT,
            attempts INTEGER,
            error TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO publication_queue VALUES (
            'pub-1', '789',
            'https://www.tiktok.com/@maker/video/789',
            'unknown', 'uncertain', 'hash-1', '',
            '2026-07-29T10:00:00+00:00', '', 1, 'unverified'
        )
        """
    )
    conn.commit()
    conn.close()

    first = master.import_legacy_database(master_path, source)
    second = master.import_legacy_database(master_path, source)
    assert first["uncertain_comments"] == 1
    assert second["uncertain_comments"] == 0

    conn = sqlite3.connect(source)
    conn.execute(
        """
        UPDATE publication_queue
        SET status='published', remote_comment_id='remote-1', error=''
        WHERE publication_id='pub-1'
        """
    )
    conn.commit()
    conn.close()
    upgraded = master.import_legacy_database(master_path, source)
    assert upgraded["confirmed_comments"] == 1

    conn = sqlite3.connect(master_path)
    target = conn.execute(
        """
        SELECT account_key, state, attempt_count, remote_comment_id
        FROM tiktok_master_comment_targets
        WHERE post_id='789'
        """
    ).fetchone()
    attempt = conn.execute(
        """
        SELECT state FROM tiktok_master_comment_attempts
        WHERE publication_id='pub-1'
        """
    ).fetchone()
    assert target == ("*", "confirmed", 1, "remote-1")
    assert attempt == ("confirmed",)
    conn.close()


def test_attempted_legacy_failure_is_imported_fail_closed(tmp_path):
    source = tmp_path / "failed-publications.sqlite"
    master_path = tmp_path / "master.sqlite"
    conn = sqlite3.connect(source)
    conn.execute(
        """
        CREATE TABLE publication_queue (
            publication_id TEXT PRIMARY KEY,
            content_key TEXT,
            target_url TEXT,
            expected_account TEXT,
            status TEXT,
            attempts INTEGER,
            error TEXT
        )
        """
    )
    conn.executemany(
        """
        INSERT INTO publication_queue VALUES (?, ?, ?, ?, 'failed', ?, ?)
        """,
        (
            (
                "attempted-failure",
                "790",
                "https://www.tiktok.com/@maker/video/790",
                "maker",
                1,
                "legacy result could not prove whether submit occurred",
            ),
            (
                "never-attempted",
                "791",
                "https://www.tiktok.com/@maker/video/791",
                "maker",
                0,
                "validation failed before attempt",
            ),
        ),
    )
    conn.commit()
    conn.close()

    imported = master.import_legacy_database(master_path, source)
    repeated = master.import_legacy_database(master_path, source)

    assert imported["uncertain_comments"] == 1
    assert repeated["uncertain_comments"] == 0
    conn = sqlite3.connect(master_path)
    try:
        attempts = conn.execute(
            """
            SELECT publication_id, post_id, state
            FROM tiktok_master_comment_attempts
            ORDER BY publication_id
            """
        ).fetchall()
    finally:
        conn.close()
    assert attempts == [("attempted-failure", "790", "uncertain")]


def test_collection_guard_requires_exact_unexpired_owner():
    conn = sqlite3.connect(":memory:")
    master.ensure_master_schema(conn)
    assert master.reserve_collection_candidate(
        conn,
        "main",
        post_id="999",
        run_id="run-1",
        attempt_id="attempt-1",
        policy="new_only",
    )
    assert master.collection_candidate_guard(
        conn,
        "main",
        post_id="999",
        run_id="run-1",
        attempt_id="attempt-1",
        policy="new_only",
    )["allowed"]
    wrong = master.collection_candidate_guard(
        conn,
        "main",
        post_id="999",
        run_id="run-1",
        attempt_id="attempt-old",
        policy="new_only",
    )
    assert wrong["allowed"] is False
    assert wrong["reason"] == "candidate_lease_not_owned"
    conn.execute(
        """
        UPDATE tiktok_master_collection_leases
        SET expires_at='2000-01-01T00:00:00+00:00'
        WHERE post_id='999'
        """
    )
    expired = master.collection_candidate_guard(
        conn,
        "main",
        post_id="999",
        run_id="run-1",
        attempt_id="attempt-1",
        policy="new_only",
    )
    assert expired["allowed"] is False
    assert expired["reason"] == "candidate_lease_expired"
    assert master.master_summary(conn)["active_collection_leases"] == 0
    conn.close()


def test_attached_master_uses_crash_atomic_rollback_journals(tmp_path):
    source = tmp_path / "project.sqlite"
    conn = create_source(source)
    schema = master.attach_master_database(conn, tmp_path / "master.sqlite")
    assert schema == "master"
    assert conn.execute("PRAGMA main.journal_mode").fetchone()[0] == "delete"
    assert conn.execute("PRAGMA master.journal_mode").fetchone()[0] == "delete"
    conn.close()


def test_showcase_claim_is_idempotent_and_blocks_cross_project():
    conn = sqlite3.connect(":memory:")
    master.ensure_master_schema(conn)
    source_attempt = confirmed_comment_attempt(
        conn,
        post_id="source-101",
        publication_id="comment-publication-101",
        remote_comment_id="comment-101",
    )
    claim = {
        "source_comment_attempt_id": source_attempt,
        "posting_account": "showcase-account",
        "source_post_id": "source-101",
        "source_comment_id": "comment-101",
        "source_publication_id": "comment-publication-101",
        "media_hash": "media-101",
        "caption_hash": "caption-101",
        "local_job_id": "showcase-job-101",
        "source_path": "showcase-project-one.sqlite",
    }

    first = master.register_showcase_claim(conn, "main", **claim)
    repeated = master.register_showcase_claim(conn, "main", **claim)

    assert repeated == first
    guard = master.showcase_target_guard(
        conn,
        "main",
        source_comment_attempt_id=source_attempt,
        posting_account="showcase-account",
        media_hash="media-101",
        caption_hash="caption-101",
    )
    assert guard["blocked"] is True
    assert guard["state"] == "reserved"
    with pytest.raises(RuntimeError, match="already has"):
        master.register_showcase_claim(
            conn,
            "main",
            **{
                **claim,
                "local_job_id": "other-project-job",
                "source_path": "showcase-project-two.sqlite",
            },
        )
    assert conn.execute(
        "SELECT COUNT(*) FROM tiktok_master_showcase_attempts"
    ).fetchone()[0] == 1
    summary = master.master_summary(conn)
    assert summary["showcase_attempts"] == 1
    assert summary["showcase_targets_by_state"] == {"reserved": 1}
    conn.close()


def test_showcase_outcome_fences_and_appends_retry_attempts():
    conn = sqlite3.connect(":memory:")
    master.ensure_master_schema(conn)
    source_attempt = confirmed_comment_attempt(
        conn,
        post_id="source-202",
        publication_id="comment-publication-202",
        remote_comment_id="comment-202",
    )
    binding = {
        "source_comment_attempt_id": source_attempt,
        "posting_account": "showcase-account",
        "source_post_id": "source-202",
        "source_comment_id": "comment-202",
        "source_publication_id": "comment-publication-202",
        "media_hash": "media-202",
        "caption_hash": "caption-202",
    }
    first = master.register_showcase_claim(
        conn,
        "main",
        **binding,
        local_job_id="showcase-job-202-a",
        source_path="showcase-project-a.sqlite",
    )
    assert (
        master.register_showcase_outcome(
            conn,
            "main",
            attempt_id=first,
            outcome="pre_submit_failed",
            error="upload form was unavailable",
        )
        == "retryable"
    )
    assert not master.showcase_target_guard(
        conn,
        "main",
        source_comment_attempt_id=source_attempt,
    )["blocked"]

    second = master.register_showcase_claim(
        conn,
        "main",
        **binding,
        local_job_id="showcase-job-202-b",
        source_path="showcase-project-b.sqlite",
    )
    assert second != first
    with pytest.raises(RuntimeError, match="already fenced"):
        master.register_showcase_outcome(
            conn,
            "main",
            attempt_id=first,
            outcome="confirmed",
            remote_post_id="must-not-be-accepted",
        )
    master.mark_showcase_submit_intent(conn, "main", second)
    assert (
        master.register_showcase_outcome(
            conn,
            "main",
            attempt_id=second,
            outcome="failed",
            error="post result was not observable",
        )
        == "uncertain"
    )
    assert master.showcase_target_guard(
        conn,
        "main",
        source_comment_attempt_id=source_attempt,
    )["blocked"]
    attempts = conn.execute(
        """
        SELECT attempt_id, state
        FROM tiktok_master_showcase_attempts
        WHERE source_comment_attempt_id=?
        ORDER BY reserved_at, attempt_id
        """,
        (source_attempt,),
    ).fetchall()
    assert {row[0]: row[1] for row in attempts} == {
        first: "retryable",
        second: "uncertain",
    }
    conn.close()


def test_showcase_rejects_account_source_and_hash_drift():
    conn = sqlite3.connect(":memory:")
    master.ensure_master_schema(conn)
    source_attempt = confirmed_comment_attempt(
        conn,
        post_id="source-303",
        publication_id="comment-publication-303",
        remote_comment_id="comment-303",
    )
    claim = {
        "source_comment_attempt_id": source_attempt,
        "posting_account": "showcase-account",
        "source_post_id": "source-303",
        "source_comment_id": "comment-303",
        "source_publication_id": "comment-publication-303",
        "media_hash": "media-303",
        "caption_hash": "caption-303",
        "local_job_id": "showcase-job-303",
        "source_path": "showcase-project.sqlite",
    }
    attempt = master.register_showcase_claim(conn, "main", **claim)

    with pytest.raises(RuntimeError, match="posting account drift"):
        master.register_showcase_claim(
            conn,
            "main",
            **{**claim, "posting_account": "different-account"},
        )
    with pytest.raises(RuntimeError, match="source drift"):
        master.register_showcase_claim(
            conn,
            "main",
            **{**claim, "source_post_id": "different-source-post"},
        )
    with pytest.raises(RuntimeError, match="hash drift"):
        master.register_showcase_claim(
            conn,
            "main",
            **{**claim, "caption_hash": "different-caption"},
        )
    with pytest.raises(RuntimeError, match="outcome hash drift"):
        master.register_showcase_outcome(
            conn,
            "main",
            attempt_id=attempt,
            outcome="pre_submit_failed",
            media_hash="different-media",
        )
    conn.close()


def test_confirmed_showcase_post_becomes_a_known_post_id():
    conn = sqlite3.connect(":memory:")
    master.ensure_master_schema(conn)
    source_attempt = confirmed_comment_attempt(
        conn,
        post_id="source-404",
        publication_id="comment-publication-404",
        remote_comment_id="comment-404",
    )
    attempt = master.register_showcase_claim(
        conn,
        "main",
        source_comment_attempt_id=source_attempt,
        posting_account="showcase-account",
        source_post_id="source-404",
        source_comment_id="comment-404",
        source_publication_id="comment-publication-404",
        media_hash="media-404",
        caption_hash="caption-404",
        local_job_id="showcase-job-404",
        source_path="showcase-project.sqlite",
    )
    master.mark_showcase_submit_intent(conn, "main", attempt)
    assert (
        master.register_showcase_outcome(
            conn,
            "main",
            attempt_id=attempt,
            outcome="published",
            receipt_id="showcase-receipt-404",
            remote_post_id="generated-showcase-post-404",
            remote_post_url=(
                "https://www.tiktok.com/@showcase-account/video/"
                "generated-showcase-post-404"
            ),
            media_hash="media-404",
            caption_hash="caption-404",
        )
        == "confirmed"
    )
    target = conn.execute(
        """
        SELECT state, remote_post_id, remote_post_url
        FROM tiktok_master_showcase_targets
        WHERE source_comment_attempt_id=?
        """,
        (source_attempt,),
    ).fetchone()
    assert target == (
        "confirmed",
        "generated-showcase-post-404",
        "https://www.tiktok.com/@showcase-account/video/"
        "generated-showcase-post-404",
    )
    assert "generated-showcase-post-404" in master.known_post_ids(conn)
    summary = master.master_summary(conn)
    assert summary["showcase_targets_by_state"] == {"confirmed": 1}
    with pytest.raises(RuntimeError, match="already has"):
        master.register_showcase_claim(
            conn,
            "main",
            source_comment_attempt_id=source_attempt,
            posting_account="showcase-account",
            source_post_id="source-404",
            source_comment_id="comment-404",
            source_publication_id="comment-publication-404",
            media_hash="media-404",
            caption_hash="caption-404",
            local_job_id="other-project-job-404",
            source_path="other-showcase-project.sqlite",
        )
    conn.close()
