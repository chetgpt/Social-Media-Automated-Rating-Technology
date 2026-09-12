import json
import sqlite3

import pytest

from sonic_audit.contracts import (
    FEATURE_RECORD_SCHEMA,
    SonicAuditContractError,
    bind_feature_record,
    bind_hash,
    build_initial_state,
    build_run_manifest,
    canonical_json,
    load_creator_registry_candidates,
    select_non_overlapping_candidates,
    select_pilot_candidates,
    validate_feature_record,
    verify_hash,
)


def candidate(post_id, *, reference_id="", creator="bankbca"):
    return {
        "post_id": str(post_id),
        "canonical_url": f"https://www.tiktok.com/@{creator}/video/{post_id}",
        "creator_handle": creator,
        "content_type": "video",
        "base_snapshot_id": f"snapshot-{post_id}",
        "base_evidence_hash": "a" * 64,
        "base_observed_at": "2026-08-15T00:00:00+00:00",
        "music_observed_at": "2026-08-15T01:00:00+00:00",
        "music_observation_status": "completed",
        "reference_kind": "apple_track_id" if reference_id else "unresolved",
        "reference_id": reference_id,
        "reference_title": "Song" if reference_id else "",
        "reference_artist": "Artist" if reference_id else "",
        "reference_basis": "tt2dsp_exact_apple_id_resolution" if reference_id else "none",
        "platform_music_id": f"music-{post_id}",
        "platform_music_title": "original sound - BankBCA",
        "platform_music_author": "BankBCA",
        "platform_music_original": True,
    }


def test_pilot_selection_is_deterministic_and_balanced_for_sixty():
    rows = []
    for group, size in enumerate((4, 3, 2, 2, 2, 2, 2, 2, 2), start=1):
        rows.extend(candidate(f"1{group:02d}{i:03d}", reference_id=f"repeat-{group}") for i in range(size))
    rows.extend(candidate(f"2{i:05d}", reference_id=f"single-{i}") for i in range(50))
    rows.extend(candidate(f"3{i:05d}") for i in range(50))

    first = select_pilot_candidates(rows, 60, creator="@bankbca")
    second = select_pilot_candidates(list(reversed(rows)), 60, creator="bankbca")

    assert [row["post_id"] for row in first] == [row["post_id"] for row in second]
    assert sum(row["selection_bucket"] == "repeated_catalog_reference" for row in first) == 21
    assert sum(row["selection_bucket"] == "singleton_catalog_reference" for row in first) == 30
    assert sum(row["selection_bucket"] == "unresolved_platform_sound" for row in first) == 9


def test_pilot_selection_rejects_owner_mismatch_and_oversize():
    with pytest.raises(SonicAuditContractError, match="owner mismatch"):
        select_pilot_candidates([candidate("1", creator="someone")], 1, creator="bankbca")
    with pytest.raises(SonicAuditContractError, match="between 1 and 60"):
        select_pilot_candidates([], 61, creator="bankbca")


def test_non_overlapping_selection_prioritizes_all_remaining_labels():
    rows = [
        candidate("101", reference_id="apple-a"),
        candidate("102", reference_id="apple-b"),
        candidate("103", reference_id="apple-c"),
        candidate("104", reference_id="apple-d"),
        candidate("201"),
        candidate("202"),
        candidate("203"),
    ]

    first = select_non_overlapping_candidates(
        rows,
        5,
        creator="@bankbca",
        excluded_post_ids={"102"},
    )
    second = select_non_overlapping_candidates(
        list(reversed(rows)),
        5,
        creator="bankbca",
        excluded_post_ids=["102"],
    )

    assert [row["post_id"] for row in first] == [row["post_id"] for row in second]
    assert "102" not in {row["post_id"] for row in first}
    assert {row["post_id"] for row in first[:3]} == {"101", "103", "104"}
    assert all(row["reference_id"] for row in first[:3])
    assert all(not row["reference_id"] for row in first[3:])


def test_non_overlapping_selection_fails_closed_on_shortage():
    with pytest.raises(SonicAuditContractError, match="non-overlapping"):
        select_non_overlapping_candidates(
            [candidate("101"), candidate("102")],
            2,
            creator="bankbca",
            excluded_post_ids={"101"},
        )


def test_manifest_requires_authorization_and_is_hash_bound(tmp_path):
    rows = [candidate("123")]
    with pytest.raises(SonicAuditContractError, match="authorization"):
        build_run_manifest(
            run_id="sonic_x",
            project="pilot",
            creator="bankbca",
            requested_count=1,
            master_database=tmp_path / "master.sqlite",
            candidates=rows,
            transient_audio_authorized=False,
        )
    manifest = build_run_manifest(
        run_id="sonic_x",
        project="pilot",
        creator="bankbca",
        requested_count=1,
        master_database=tmp_path / "master.sqlite",
        candidates=rows,
        transient_audio_authorized=True,
    )
    assert verify_hash(manifest, "manifest_hash")
    assert verify_hash(build_initial_state(manifest), "state_hash")
    assert manifest["retention"]["raw_media_persisted"] is False


def test_manifest_freezes_separately_authorized_symbolic_analysis(tmp_path):
    config = bind_hash(
        {
            "schema_version": "tiktok-sonic-mirelo-config-v1",
            "provider": "mirelo",
            "model": "audio-to-midi-v1.0",
            "endpoint_version": "v2",
            "timing": "performance",
            "input_basis": "transient_decoded_wav",
            "max_run_credits": 25,
            "preflight_required": True,
            "server_asset_retention": "up_to_24_hours",
            "credential_environment_variable": "MIRELO_API_KEY",
            "persist_raw_notes": False,
            "persist_midi": False,
            "persist_musicxml": False,
            "changes_primary_recording_score": False,
        },
        "config_hash",
    )
    with pytest.raises(SonicAuditContractError, match="third-party"):
        build_run_manifest(
            run_id="sonic_x",
            project="pilot",
            creator="bankbca",
            requested_count=1,
            master_database=tmp_path / "master.sqlite",
            candidates=[candidate("123")],
            transient_audio_authorized=True,
            symbolic_analysis=config,
        )

    manifest = build_run_manifest(
        run_id="sonic_x",
        project="pilot",
        creator="bankbca",
        requested_count=1,
        master_database=tmp_path / "master.sqlite",
        candidates=[candidate("123")],
        transient_audio_authorized=True,
        third_party_audio_upload_authorized=True,
        symbolic_analysis=config,
    )
    config["max_run_credits"] = 999

    assert manifest["symbolic_analysis"]["max_run_credits"] == 25
    assert manifest["authorization"]["third_party_audio_upload"] is True
    assert manifest["authorization"]["third_party_provider"] == "mirelo"
    assert manifest["retention"]["provider_midi_persisted"] is False
    assert manifest["retention"]["provider_musicxml_persisted"] is False
    assert manifest["retention"]["provider_raw_notes_persisted"] is False
    assert verify_hash(manifest, "manifest_hash")


def test_manifest_freezes_optional_selection_context(tmp_path):
    context = {
        "excluded_runs": [
            {
                "run_id": "sonic_0123456789abcdef",
                "manifest_hash": "b" * 64,
                "candidate_count": 1,
                "candidate_set_hash": "c" * 64,
            }
        ],
        "excluded_candidate_count": 1,
        "excluded_candidate_set_hash": "c" * 64,
    }
    manifest = build_run_manifest(
        run_id="sonic_x",
        project="pilot",
        creator="bankbca",
        requested_count=1,
        master_database=tmp_path / "master.sqlite",
        candidates=[candidate("123")],
        transient_audio_authorized=True,
        selection_method="externally-labelled-first-non-overlapping-v1",
        selection_context=context,
    )

    context["excluded_candidate_count"] = 99
    assert manifest["selection_method"] == "externally-labelled-first-non-overlapping-v1"
    assert manifest["selection_context"]["excluded_candidate_count"] == 1
    assert verify_hash(manifest, "manifest_hash")


def test_feature_record_is_bound_to_frozen_post(tmp_path):
    rows = [candidate("123")]
    manifest = build_run_manifest(
        run_id="sonic_x",
        project="pilot",
        creator="bankbca",
        requested_count=1,
        master_database=tmp_path / "master.sqlite",
        candidates=rows,
        transient_audio_authorized=True,
    )
    record = bind_feature_record(
        {
            "run_id": "sonic_x",
            "manifest_hash": manifest["manifest_hash"],
            "post_id": "123",
            "base_snapshot_id": "snapshot-123",
            "base_evidence_hash": "a" * 64,
            "status": "unavailable",
            "error": "transport_unavailable",
        }
    )
    assert record["schema_version"] == FEATURE_RECORD_SCHEMA
    validate_feature_record(record, manifest=manifest)
    record["post_id"] = "999"
    with pytest.raises(SonicAuditContractError):
        validate_feature_record(record, manifest=manifest)


def test_registry_reader_uses_snapshot_and_latest_music_observation(tmp_path):
    database = tmp_path / "master.sqlite"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE tiktok_master_posts (
          post_id TEXT, canonical_url TEXT, creator_handle TEXT,
          creator_key TEXT, latest_snapshot_id TEXT, latest_evidence_hash TEXT
        );
        CREATE TABLE tiktok_master_snapshots (
          snapshot_id TEXT, post_id TEXT, observed_at TEXT, evidence_hash TEXT, evidence_json TEXT
        );
        CREATE TABLE tiktok_master_music_backfill_observations (
          observation_id TEXT, post_id TEXT, music_observed_at TEXT,
          created_at TEXT, music_evidence_hash TEXT, music_evidence_json TEXT,
          status TEXT
        );
        """
    )
    evidence = {"caption": "hello"}
    evidence_hash = __import__("hashlib").sha256(canonical_json(evidence).encode()).hexdigest()
    music = bind_hash(
        {
            "schema_version": "tiktok-music-evidence-v3",
            "platform_music": {
                "music_id": "m1", "title": "original sound", "author": "BankBCA",
                "is_original": True,
            },
            "tt2dsp_resolution": {
                "status": "resolved",
                "selected": {
                    "provider_track_id": "42", "title": "Hero", "artist": "Cash Cash",
                },
            },
        },
        "music_evidence_hash",
    )
    connection.execute(
        "INSERT INTO tiktok_master_posts VALUES (?,?,?,?,?,?)",
        ("123", "https://www.tiktok.com/@bankbca/video/123", "bankbca", "bankbca", "s1", evidence_hash),
    )
    connection.execute(
        "INSERT INTO tiktok_master_snapshots VALUES (?,?,?,?,?)",
        ("s1", "123", "2026-08-15T00:00:00+00:00", evidence_hash, canonical_json(evidence)),
    )
    connection.execute(
        "INSERT INTO tiktok_master_music_backfill_observations VALUES (?,?,?,?,?,?,?)",
        ("o1", "123", "2026-08-15T01:00:00+00:00", "2026-08-15T01:00:00+00:00", music["music_evidence_hash"], canonical_json(music), "completed"),
    )
    connection.commit()
    connection.close()

    rows = load_creator_registry_candidates(database, "bankbca")
    assert rows[0]["reference_id"] == "42"
    assert rows[0]["base_snapshot_id"] == "s1"


def test_registry_reader_supports_pre_backfill_master_schema(tmp_path):
    database = tmp_path / "legacy-master.sqlite"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE tiktok_master_posts (
          post_id TEXT, canonical_url TEXT, creator_handle TEXT,
          creator_key TEXT, latest_snapshot_id TEXT, latest_evidence_hash TEXT
        );
        CREATE TABLE tiktok_master_snapshots (
          snapshot_id TEXT, post_id TEXT, observed_at TEXT, evidence_hash TEXT, evidence_json TEXT
        );
        """
    )
    music = bind_hash(
        {
            "schema_version": "tiktok-music-evidence-v3",
            "platform_music": {
                "music_id": "m1",
                "title": "original sound",
                "author": "BankBCA",
                "is_original": True,
            },
            "tt2dsp_resolution": {
                "status": "resolved",
                "selected": {
                    "provider_track_id": "42",
                    "title": "Hero",
                    "artist": "Cash Cash",
                },
            },
        },
        "music_evidence_hash",
    )
    evidence = {"caption": "hello", "music_evidence": music}
    evidence_hash = __import__("hashlib").sha256(
        canonical_json(evidence).encode()
    ).hexdigest()
    connection.execute(
        "INSERT INTO tiktok_master_posts VALUES (?,?,?,?,?,?)",
        (
            "123",
            "https://www.tiktok.com/@bankbca/video/123",
            "bankbca",
            "bankbca",
            "s1",
            evidence_hash,
        ),
    )
    connection.execute(
        "INSERT INTO tiktok_master_snapshots VALUES (?,?,?,?,?)",
        (
            "s1",
            "123",
            "2026-08-15T00:00:00+00:00",
            evidence_hash,
            canonical_json(evidence),
        ),
    )
    connection.commit()
    connection.close()

    rows = load_creator_registry_candidates(database, "bankbca")
    assert rows[0]["reference_id"] == "42"
    assert rows[0]["music_observation_status"] == "base_evidence"
    assert rows[0]["music_observed_at"] == "2026-08-15T00:00:00+00:00"


def test_registry_reader_is_query_only(tmp_path):
    database = tmp_path / "empty.sqlite"
    sqlite3.connect(database).close()
    before = database.stat().st_mtime_ns
    with pytest.raises(sqlite3.OperationalError):
        load_creator_registry_candidates(database, "bankbca")
    assert database.stat().st_mtime_ns == before
