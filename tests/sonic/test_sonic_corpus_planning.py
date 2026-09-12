import json
import sqlite3
from pathlib import Path

import pytest

from sonic_audit.contracts import bind_hash, canonical_json, canonical_sha256, verify_hash
from sonic_audit.corpus import (
    CORPUS_PLAN_SCHEMA,
    CorpusPlanningError,
    plan_corpus,
    validate_corpus_plan,
    validate_corpus_plan_batch,
)


def _stable_id(*parts, length=40):
    import hashlib

    return hashlib.sha256("\x1f".join(str(value or "") for value in parts).encode()).hexdigest()[:length]


def _create_schema(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE tiktok_master_posts (
          post_id TEXT PRIMARY KEY, canonical_url TEXT, creator_handle TEXT,
          creator_key TEXT, unavailable_at TEXT, latest_snapshot_id TEXT,
          latest_evidence_hash TEXT
        );
        CREATE TABLE tiktok_master_snapshots (
          snapshot_id TEXT PRIMARY KEY, post_id TEXT, observed_at TEXT,
          evidence_hash TEXT, evidence_json TEXT
        );
        CREATE TABLE tiktok_master_music_backfill_runs (
          run_id TEXT PRIMARY KEY, project TEXT, master_database_path TEXT,
          scope_mode TEXT, scope_value TEXT, target_schema_version TEXT,
          configured_catalogs_json TEXT, retryable_statuses_json TEXT,
          force INTEGER, candidate_set_json TEXT, candidate_set_hash TEXT,
          run_hash TEXT, selected_count INTEGER, completed_count INTEGER,
          unavailable_count INTEGER, failed_count INTEGER, created_at TEXT,
          status TEXT
        );
        CREATE TABLE tiktok_master_music_backfill_observations (
          observation_id TEXT PRIMARY KEY, run_id TEXT, post_id TEXT,
          base_snapshot_id TEXT, base_evidence_hash TEXT, base_observed_at TEXT,
          music_observed_at TEXT, music_evidence_json TEXT,
          music_evidence_hash TEXT, status TEXT, error TEXT,
          observation_hash TEXT, created_at TEXT
        );
        """
    )
    return connection


def _resolution(track_id: str, *, storefront: str = "ID") -> dict:
    if track_id is None:
        return bind_hash(
            {
                "schema_version": "tiktok-tt2dsp-resolution-v1",
                "provider": "apple_itunes_lookup",
                "status": "unsupported",
                "input_links": [],
                "storefront": storefront,
                "selected": None,
                "decision": {"reason": "no_supported_apple_link"},
                "error": None,
                "identification_basis": "tiktok_tt2dsp_external_catalog_resolution",
                "acoustic_recognition_performed": False,
            },
            "resolution_hash",
        )
    return bind_hash(
        {
            "schema_version": "tiktok-tt2dsp-resolution-v1",
            "provider": "apple_itunes_lookup",
            "status": "resolved",
            "input_links": [
                {
                    "meta_song_id": "777",
                    "song_id": track_id,
                    "platform": "1",
                    "button_type": "2",
                }
            ],
            "storefront": storefront,
            "selected": {
                "provider_track_id": track_id,
                "title": f"Track {track_id}",
                "artist": "Artist",
                "album": "Album",
                "collection_id": "900",
                "duration_ms": 180000,
                "release_date": "2026-01-01T00:00:00Z",
                "genre": "Pop",
                "explicitness": "notExplicit",
            },
            "decision": {"reason": "exact_provider_track_id"},
            "error": None,
            "identification_basis": "tiktok_tt2dsp_external_catalog_resolution",
            "acoustic_recognition_performed": False,
        },
        "resolution_hash",
    )


def _add_posts(connection, rows, *, creator="maker"):
    snapshots = {}
    for post_id, _ in rows:
        snapshot_id = f"snapshot-{post_id}"
        observed_at = "2026-08-01T00:00:00+00:00"
        evidence = {"post_id": post_id, "content_type": "video"}
        evidence_hash = canonical_sha256(evidence)
        snapshots[post_id] = (snapshot_id, observed_at, evidence_hash)
        connection.execute(
            "INSERT INTO tiktok_master_posts VALUES (?,?,?,?,?,?,?)",
            (
                post_id,
                f"https://www.tiktok.com/@{creator}/video/{post_id}",
                creator,
                creator,
                "",
                snapshot_id,
                evidence_hash,
            ),
        )
        connection.execute(
            "INSERT INTO tiktok_master_snapshots VALUES (?,?,?,?,?)",
            (snapshot_id, post_id, observed_at, evidence_hash, canonical_json(evidence)),
        )
    return snapshots


def _add_completed_run(
    connection,
    database,
    rows,
    snapshots,
    *,
    run_id="backfill-1",
    music_observed_at="2026-08-02T00:00:00+00:00",
    run_status="backfill_complete",
    creator="maker",
):
    candidates = []
    for post_id, _ in rows:
        snapshot_id, observed_at, evidence_hash = snapshots[post_id]
        candidates.append(
            {
                "post_id": post_id,
                "canonical_url": f"https://www.tiktok.com/@{creator}/video/{post_id}",
                "creator_handle": creator,
                "creator_key": creator,
                "base_snapshot_id": snapshot_id,
                "base_evidence_hash": evidence_hash,
                "base_observed_at": observed_at,
                "base_music_schema": "tiktok-music-evidence-v2",
                "base_music_status": "available",
                "eligibility_reason": "music_schema_upgrade_required",
            }
        )
    candidate_set_hash = canonical_sha256(candidates)
    created_at = music_observed_at
    run_values = {
        "run_id": run_id,
        "project": "corpus-fixture",
        "master_database_path": str(database.resolve()),
        "scope_mode": "creator",
        "scope_value": creator,
        "target_schema_version": "tiktok-music-evidence-v3",
        "configured_catalogs": ["musicbrainz"],
        "retryable_statuses": ["provider_error", "rate_limited", "unavailable"],
        "force": False,
        "candidate_set_hash": candidate_set_hash,
        "selected_count": len(candidates),
        "created_at": created_at,
    }
    connection.execute(
        "INSERT INTO tiktok_master_music_backfill_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            run_id,
            run_values["project"],
            run_values["master_database_path"],
            run_values["scope_mode"],
            run_values["scope_value"],
            run_values["target_schema_version"],
            canonical_json(run_values["configured_catalogs"]),
            canonical_json(run_values["retryable_statuses"]),
            0,
            canonical_json(candidates),
            candidate_set_hash,
            canonical_sha256(run_values),
            len(candidates),
            len(candidates),
            0,
            0,
            created_at,
            run_status,
        ),
    )
    for post_id, track_id in rows:
        snapshot_id, base_observed_at, evidence_hash = snapshots[post_id]
        music = bind_hash(
            {
                "schema_version": "tiktok-music-evidence-v3",
                "tt2dsp_resolution": _resolution(track_id),
            },
            "music_evidence_hash",
        )
        observation = {
            "schema_version": "tiktok-music-backfill-observation-v1",
            "run_id": run_id,
            "post_id": post_id,
            "base_snapshot_id": snapshot_id,
            "base_evidence_hash": evidence_hash,
            "base_observed_at": base_observed_at,
            "music_observed_at": music_observed_at,
            "music_evidence_hash": music["music_evidence_hash"],
            "status": "completed",
            "error": "",
        }
        connection.execute(
            "INSERT INTO tiktok_master_music_backfill_observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                _stable_id("tiktok-music-backfill-observation", run_id, post_id),
                run_id,
                post_id,
                snapshot_id,
                evidence_hash,
                base_observed_at,
                music_observed_at,
                canonical_json(music),
                music["music_evidence_hash"],
                "completed",
                "",
                canonical_sha256(observation),
                music_observed_at,
            ),
        )


def _database(tmp_path, rows, *, creator="maker"):
    database = tmp_path / "master.sqlite"
    connection = _create_schema(database)
    snapshots = _add_posts(connection, rows, creator=creator)
    _add_completed_run(
        connection, database, rows, snapshots, creator=creator
    )
    connection.commit()
    connection.close()
    return database


def _rebind_plan(plan):
    """Rebuild relationship hashes so semantic-tamper tests reach semantic gates."""

    candidate_rows = []
    for row in plan["candidates"]:
        body = dict(row)
        body.pop("candidate_hash", None)
        candidate_rows.append(bind_hash(body, "candidate_hash"))
    plan["candidates"] = candidate_rows

    group_rows = []
    for group in plan["groups"]:
        selected = [row for row in candidate_rows if row["group_id"] == group["group_id"]]
        body = dict(group)
        body.pop("group_hash", None)
        body["post_ids"] = [row["post_id"] for row in selected]
        body["candidate_set_hash"] = canonical_sha256(selected)
        if selected:
            body["reference_label"] = selected[0]["reference_label"]
        group_rows.append(bind_hash(body, "group_hash"))
    plan["groups"] = group_rows
    groups_by_id = {group["group_id"]: group for group in group_rows}

    batch_rows = []
    for batch in plan["batches"]:
        selected = sorted(
            (row for row in candidate_rows if row["batch_id"] == batch["batch_id"]),
            key=lambda row: row["batch_position"],
        )
        body = dict(batch)
        body.pop("batch_hash", None)
        body["post_ids"] = [row["post_id"] for row in selected]
        body["candidate_set_hash"] = canonical_sha256(selected)
        body["group_hashes"] = [
            groups_by_id[group_id]["group_hash"] for group_id in body["group_ids"]
        ]
        batch_rows.append(bind_hash(body, "batch_hash"))
    plan["batches"] = batch_rows

    body = dict(plan)
    body.pop("plan_hash", None)
    body["candidate_set_hash"] = canonical_sha256(candidate_rows)
    body["group_set_hash"] = canonical_sha256(group_rows)
    body["batch_set_hash"] = canonical_sha256(batch_rows)
    return bind_hash(body, "plan_hash")


def test_creator_plan_is_hash_bound_pair_targeted_and_offline(tmp_path, monkeypatch):
    rows = [
        *((str(100 + index), "110") for index in range(5)),
        *((str(200 + index), "220") for index in range(3)),
        ("301", "330"),
    ]
    database = _database(tmp_path, rows)
    monkeypatch.setattr("sonic_audit.corpus.utc_now", lambda: "2026-08-15T00:00:00+00:00")

    plan = plan_corpus(
        database,
        creators=["@maker"],
        min_repeated_groups=2,
        min_positive_pairs=5,
        max_posts=6,
        max_posts_per_reference=4,
    )

    assert plan["schema_version"] == CORPUS_PLAN_SCHEMA
    assert plan["selection_algorithm"] == "max-marginal-positive-pairs-v1"
    assert verify_hash(plan, "plan_hash")
    assert plan["selected_candidate_count"] == 6
    assert plan["selected_repeated_group_count"] == 2
    assert plan["selected_positive_pair_count"] == 7
    assert all(verify_hash(row, "candidate_hash") for row in plan["candidates"])
    assert all(verify_hash(row, "group_hash") for row in plan["groups"])
    assert all(verify_hash(row, "batch_hash") for row in plan["batches"])
    assert all(
        row["reference_label"].startswith("apple_itunes_lookup:ID:")
        for row in plan["candidates"]
    )
    assert all(row["selected_count"] <= 60 for row in plan["batches"])
    assert len({row["creator_handle"] for row in plan["batches"]}) == 1
    assert plan["boundaries"] == {
        "registry_access": "query_only",
        "browser_access_performed": False,
        "catalog_request_performed": False,
        "media_access_performed": False,
        "audio_acquisition_performed": False,
        "ai_analysis_performed": False,
        "sonic_run_created": False,
        "master_registry_mutated": False,
        "publication_eligible": False,
    }


def test_pair_target_uses_maximum_marginal_gain_within_post_budget(tmp_path):
    database = _database(
        tmp_path,
        [
            *((str(100 + index), "110") for index in range(4)),
            *((str(200 + index), "220") for index in range(4)),
        ],
    )

    plan = plan_corpus(
        database,
        creators=["maker"],
        min_repeated_groups=2,
        min_positive_pairs=7,
        max_posts=6,
        max_posts_per_reference=4,
    )

    assert plan["selected_candidate_count"] == 6
    assert plan["selected_positive_pair_count"] == 7
    assert sorted(group["selected_count"] for group in plan["groups"]) == [2, 4]


def test_pair_allocator_breaks_equal_gain_by_larger_capacity(tmp_path):
    database = _database(
        tmp_path,
        [
            *((str(100 + index), "110") for index in range(3)),
            *((str(200 + index), "220") for index in range(4)),
        ],
    )

    plan = plan_corpus(
        database,
        creators=["maker"],
        min_repeated_groups=2,
        min_positive_pairs=7,
        max_posts=6,
        max_posts_per_reference=6,
    )

    selected_by_reference = {
        group["reference_label"]: group["selected_count"]
        for group in plan["groups"]
    }
    assert selected_by_reference["apple_itunes_lookup:ID:110"] == 2
    assert selected_by_reference["apple_itunes_lookup:ID:220"] == 4
    assert plan["selected_positive_pair_count"] == 7


def test_exclusions_are_applied_before_grouping_and_hash_bound(tmp_path):
    rows = [("101", "110"), ("102", "110"), ("103", "110"), ("201", "220"), ("202", "220")]
    database = _database(tmp_path, rows)

    plan = plan_corpus(
        database,
        creators=["maker"],
        excluded_post_ids=["103"],
        min_repeated_groups=2,
        min_positive_pairs=2,
        max_posts=4,
        max_posts_per_reference=3,
    )

    assert "103" not in {row["post_id"] for row in plan["candidates"]}
    assert plan["source_scope"]["excluded_post_ids"] == ["103"]
    assert plan["source_scope"]["excluded_post_id_count"] == 1
    assert verify_hash(plan["source_scope"], "scope_hash")


def test_valid_unsupported_music_rows_are_ineligible_not_fatal(tmp_path):
    rows = [("101", "110"), ("102", "110")]
    rows.extend((str(200 + index), None) for index in range(12))
    database = _database(tmp_path, rows)

    plan = plan_corpus(database, creators=["maker"], max_posts=2)

    assert {row["post_id"] for row in plan["candidates"]} == {"101", "102"}
    assert plan["eligible_candidate_count"] == 2


def test_explicit_post_ids_are_exact_and_only_test_feasibility(tmp_path):
    rows = [("101", "110"), ("102", "110"), ("201", "220"), ("202", "220"), ("301", "330")]
    database = _database(tmp_path, rows)

    plan = plan_corpus(
        database,
        post_ids=["202", "101", "201", "102"],
        min_repeated_groups=2,
        min_positive_pairs=2,
        max_posts=4,
        max_posts_per_reference=2,
    )
    assert {row["post_id"] for row in plan["candidates"]} == {"101", "102", "201", "202"}
    assert plan["source_scope"]["mode"] == "post_ids"

    with pytest.raises(CorpusPlanningError, match="missing or ineligible"):
        plan_corpus(database, post_ids=["101", "999"], max_posts=4)
    with pytest.raises(CorpusPlanningError, match="singleton"):
        plan_corpus(database, post_ids=["101", "102", "301"], max_posts=4)
    with pytest.raises(CorpusPlanningError, match="intersects exclusions"):
        plan_corpus(
            database,
            post_ids=["101", "102"],
            excluded_post_ids=["101"],
            max_posts=2,
        )
    with pytest.raises(CorpusPlanningError, match="duplicate post ID"):
        plan_corpus(database, post_ids=["101", "101"], max_posts=2)


def test_tampered_music_evidence_is_rejected(tmp_path):
    database = _database(tmp_path, [("101", "110"), ("102", "110")])
    connection = sqlite3.connect(database)
    raw = connection.execute(
        "SELECT music_evidence_json FROM tiktok_master_music_backfill_observations WHERE post_id='101'"
    ).fetchone()[0]
    music = json.loads(raw)
    music["tt2dsp_resolution"]["storefront"] = "US"
    connection.execute(
        "UPDATE tiktok_master_music_backfill_observations SET music_evidence_json=? WHERE post_id='101'",
        (canonical_json(music),),
    )
    connection.commit()
    connection.close()

    with pytest.raises(CorpusPlanningError, match="music evidence hash mismatch"):
        plan_corpus(database, creators=["maker"], max_posts=2)


def test_latest_completed_v3_observation_wins(tmp_path):
    rows = [("101", "110"), ("102", "110")]
    database = tmp_path / "master.sqlite"
    connection = _create_schema(database)
    snapshots = _add_posts(connection, rows)
    _add_completed_run(
        connection,
        database,
        rows,
        snapshots,
        run_id="old-run",
        music_observed_at="2026-08-02T00:00:00+00:00",
    )
    newer = [("101", "990"), ("102", "990")]
    _add_completed_run(
        connection,
        database,
        newer,
        snapshots,
        run_id="new-run",
        music_observed_at="2026-08-03T00:00:00+00:00",
    )
    connection.commit()
    connection.close()

    plan = plan_corpus(database, creators=["maker"], max_posts=2)
    assert {row["reference_track_id"] for row in plan["candidates"]} == {"990"}
    assert {row["backfill_run_id"] for row in plan["candidates"]} == {"new-run"}


def test_current_latest_snapshot_is_verified_separately_from_backfill_base(tmp_path):
    database = _database(tmp_path, [("101", "110"), ("102", "110")])
    connection = sqlite3.connect(database)
    latest = {"post_id": "101", "content_type": "video", "revision": 2}
    latest_hash = canonical_sha256(latest)
    connection.execute(
        "INSERT INTO tiktok_master_snapshots VALUES (?,?,?,?,?)",
        (
            "snapshot-new-101",
            "101",
            "2026-08-04T00:00:00+00:00",
            latest_hash,
            canonical_json(latest),
        ),
    )
    connection.execute(
        "UPDATE tiktok_master_posts SET latest_snapshot_id=?, latest_evidence_hash=? WHERE post_id='101'",
        ("snapshot-new-101", latest_hash),
    )
    connection.commit()
    connection.close()

    plan = plan_corpus(database, creators=["maker"], max_posts=2)
    candidate = next(row for row in plan["candidates"] if row["post_id"] == "101")
    assert candidate["latest_snapshot_id"] == "snapshot-new-101"
    assert candidate["base_snapshot_id"] == "snapshot-101"

    connection = sqlite3.connect(database)
    other_hash = connection.execute(
        "SELECT evidence_hash FROM tiktok_master_snapshots WHERE snapshot_id='snapshot-102'"
    ).fetchone()[0]
    connection.execute(
        "UPDATE tiktok_master_posts SET latest_snapshot_id='snapshot-102', latest_evidence_hash=? WHERE post_id='101'",
        (other_hash,),
    )
    connection.commit()
    connection.close()
    with pytest.raises(CorpusPlanningError, match="latest-snapshot binding"):
        plan_corpus(database, creators=["maker"], max_posts=2)


def test_backfill_base_snapshot_post_binding_is_verified(tmp_path):
    database = _database(tmp_path, [("101", "110"), ("102", "110")])
    connection = sqlite3.connect(database)
    latest = {"post_id": "101", "content_type": "video", "revision": 2}
    latest_hash = canonical_sha256(latest)
    connection.execute(
        "INSERT INTO tiktok_master_snapshots VALUES (?,?,?,?,?)",
        (
            "snapshot-new-101",
            "101",
            "2026-08-04T00:00:00+00:00",
            latest_hash,
            canonical_json(latest),
        ),
    )
    connection.execute(
        "UPDATE tiktok_master_posts SET latest_snapshot_id=?, latest_evidence_hash=? "
        "WHERE post_id='101'",
        ("snapshot-new-101", latest_hash),
    )
    connection.execute(
        "UPDATE tiktok_master_snapshots SET post_id='102' "
        "WHERE snapshot_id='snapshot-101'"
    )
    connection.commit()
    connection.close()

    with pytest.raises(CorpusPlanningError, match="base-snapshot binding"):
        plan_corpus(database, creators=["maker"], max_posts=2)


def test_one_typed_group_can_span_single_creator_batches(tmp_path):
    database = tmp_path / "master.sqlite"
    connection = _create_schema(database)
    alpha = [("101", "110"), ("102", "110")]
    beta = [("201", "110"), ("202", "110")]
    alpha_snapshots = _add_posts(connection, alpha, creator="alpha")
    beta_snapshots = _add_posts(connection, beta, creator="beta")
    _add_completed_run(
        connection,
        database,
        alpha,
        alpha_snapshots,
        run_id="alpha-run",
        creator="alpha",
    )
    _add_completed_run(
        connection,
        database,
        beta,
        beta_snapshots,
        run_id="beta-run",
        creator="beta",
    )
    connection.commit()
    connection.close()

    plan = plan_corpus(
        database,
        creators=["beta", "alpha"],
        min_repeated_groups=1,
        min_positive_pairs=1,
        max_posts=2,
        max_posts_per_reference=2,
    )

    assert plan["groups"][0]["creator_handles"] == ["alpha", "beta"]
    assert len(plan["groups"][0]["batch_ids"]) == 2
    assert {batch["creator_handle"] for batch in plan["batches"]} == {"alpha", "beta"}
    assert all(batch["selected_count"] == 1 for batch in plan["batches"])


def test_large_plan_preserves_groups_in_single_creator_batches_of_60(tmp_path):
    rows = []
    for group in range(4):
        track_id = str(500 + group)
        rows.extend((str(1000 + group * 100 + index), track_id) for index in range(16))
    database = _database(tmp_path, rows)

    plan = plan_corpus(
        database,
        creators=["maker"],
        min_repeated_groups=4,
        min_positive_pairs=480,
        max_posts=64,
        max_posts_per_reference=16,
    )

    assert [batch["selected_count"] for batch in plan["batches"]] == [48, 16]
    group_batches = {group["group_id"]: group["batch_ids"] for group in plan["groups"]}
    assert all(candidate["batch_id"] in group_batches[candidate["group_id"]] for candidate in plan["candidates"])
    assert all(batch["creator_handle"] == "maker" for batch in plan["batches"])


def test_scope_gate_and_query_only_access(tmp_path):
    database = _database(tmp_path, [("101", "110"), ("102", "110")])
    before = database.stat().st_mtime_ns
    plan_corpus(database, creators=["maker"], max_posts=2)
    assert database.stat().st_mtime_ns == before
    assert not Path(str(database) + "-journal").exists()

    with pytest.raises(CorpusPlanningError, match="exactly one"):
        plan_corpus(database)
    with pytest.raises(CorpusPlanningError, match="exactly one"):
        plan_corpus(database, creators=["maker"], post_ids=["101"])


def test_plan_batch_revalidates_all_frozen_registry_bindings(tmp_path):
    database = _database(tmp_path, [("101", "110"), ("102", "110")])
    plan = plan_corpus(database, creators=["maker"], max_posts=2)
    batch = plan["batches"][0]

    validated = validate_corpus_plan(plan)
    binding = validate_corpus_plan_batch(
        plan,
        batch_id=batch["batch_id"],
        master_database=database,
    )

    assert validated["batches_by_id"][batch["batch_id"]]["batch_hash"] == batch["batch_hash"]
    assert [row["post_id"] for row in binding["candidates"]] == batch["post_ids"]
    assert binding["plan_hash"] == plan["plan_hash"]

    tampered = json.loads(json.dumps(plan))
    tampered["candidates"][0]["reference_title"] = "Changed"
    with pytest.raises(CorpusPlanningError, match="plan hash|candidate hash"):
        validate_corpus_plan(tampered)


def test_plan_batch_rejects_a_changed_current_snapshot_before_execution(tmp_path):
    database = _database(tmp_path, [("101", "110"), ("102", "110")])
    plan = plan_corpus(database, creators=["maker"], max_posts=2)
    batch_id = plan["batches"][0]["batch_id"]
    changed = {"post_id": "101", "content_type": "video", "changed": True}
    changed_hash = canonical_sha256(changed)
    connection = sqlite3.connect(database)
    connection.execute(
        "INSERT INTO tiktok_master_snapshots VALUES (?,?,?,?,?)",
        (
            "snapshot-current-101",
            "101",
            "2026-08-10T00:00:00+00:00",
            changed_hash,
            canonical_json(changed),
        ),
    )
    connection.execute(
        "UPDATE tiktok_master_posts SET latest_snapshot_id=?, latest_evidence_hash=? "
        "WHERE post_id='101'",
        ("snapshot-current-101", changed_hash),
    )
    connection.commit()
    connection.close()

    with pytest.raises(CorpusPlanningError, match="binding changed"):
        validate_corpus_plan_batch(
            plan,
            batch_id=batch_id,
            master_database=database,
        )


def test_plan_batch_rejects_when_frozen_music_observation_is_no_longer_latest(tmp_path):
    database = _database(tmp_path, [("101", "110"), ("102", "110")])
    plan = plan_corpus(database, creators=["maker"], max_posts=2)
    batch_id = plan["batches"][0]["batch_id"]
    candidate = next(row for row in plan["candidates"] if row["post_id"] == "101")
    connection = sqlite3.connect(database)
    connection.execute(
        "INSERT INTO tiktok_master_music_backfill_observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "newer-unavailable-101",
            "different-backfill-run",
            "101",
            candidate["base_snapshot_id"],
            candidate["base_evidence_hash"],
            candidate["base_observed_at"],
            "2026-08-12T00:00:00+00:00",
            canonical_json({}),
            canonical_sha256({}),
            "unavailable",
            "metadata_unavailable",
            "0" * 64,
            "2026-08-12T00:00:00+00:00",
        ),
    )
    connection.commit()
    connection.close()

    with pytest.raises(CorpusPlanningError, match="no longer latest"):
        validate_corpus_plan_batch(
            plan,
            batch_id=batch_id,
            master_database=database,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("reference_provider", "other_provider"),
        ("reference_storefront", "id"),
        ("reference_label", "apple_itunes_lookup:ID:999"),
    ],
)
def test_plan_semantics_reject_rehashed_reference_tampering(tmp_path, field, value):
    database = _database(tmp_path, [("101", "110"), ("102", "110")])
    plan = plan_corpus(database, creators=["maker"], max_posts=2)
    tampered = json.loads(json.dumps(plan))
    tampered["candidates"][0][field] = value
    tampered = _rebind_plan(tampered)

    with pytest.raises(CorpusPlanningError, match="candidate binding"):
        validate_corpus_plan(tampered)


def test_plan_semantics_reject_rehashed_algorithm_tampering(tmp_path):
    database = _database(tmp_path, [("101", "110"), ("102", "110")])
    plan = plan_corpus(database, creators=["maker"], max_posts=2)
    body = dict(plan)
    body.pop("plan_hash")
    body["selection_algorithm"] = "unreviewed-selection-v1"
    tampered = bind_hash(body, "plan_hash")

    with pytest.raises(CorpusPlanningError, match="selection algorithm"):
        validate_corpus_plan(tampered)
