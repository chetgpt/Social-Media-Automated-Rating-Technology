import json
import socket
import sqlite3
import subprocess

import pytest

from music_discovery_sources import _hash, iter_tiktok_records


def packet(post_id="1234567890123456789", *, creator="musician", time="2026-08-01T09:00:00+00:00", views=0):
    return {
        "schema_version": "tiktok-engage-evidence-v1", "platform": "tiktok",
        "post_id": post_id, "url": f"https://www.tiktok.com/@{creator}/video/{post_id}",
        "creator": creator, "creator_display_name": "Penyanyi Émerging",
        "creator_identity": {"id": "42", "sec_uid": "stable-public-id"},
        "caption": "Single perdana saya", "caption_status": "available",
        "transcript": "Lagu baru", "transcript_status": "ok", "transcript_language": "id",
        "observed_at": time, "published_at": "2026-07-29T10:00:00+07:00",
        "evidence_ready": True, "metrics": {"views": views, "followers": 0},
        "metric_availability": {"views": "available", "followers": "missing_from_public_response"},
        "comments": [{"cid": "c1", "text": "Siapa penyanyinya?", "user": {"private": "secret"},
                      "reply_comment": [{"cid": "c2", "text": "Musician", "reply_id": "c1"}]}],
        "comments_status": {"ok": True, "complete": True},
        "music_evidence": {"platform_music": {"status": "available", "music_id": "123", "title": "Original sound", "author": "musician", "is_original": True},
                           "platform_contained_recording": {"status": "available", "artist": "Actual Singer", "title": "Track", "recording_id": "r1"},
                           "catalogs": {"musicbrainz": {"status": "matched", "result": {"candidates": [{"artist_credit": "Artist", "artists": [{"artist_mbid": "mbid", "name": "Artist"}]}]}}},
                           "tt2dsp_resolution": {"status": "resolved", "selected": {"provider_track_id": "23", "artist": "Singer", "title": "Tune"}},
                           "acoustic_verification": {"status": "not_attempted", "verified": False}},
    }


def project(path):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE engage_tiktok_runs (run_id TEXT PRIMARY KEY, project TEXT, topic TEXT, status TEXT);
        CREATE TABLE engage_tiktok_posts (run_id TEXT, post_id TEXT, evidence_ready INTEGER,
            evidence_json TEXT, evidence_hash TEXT, status TEXT, url TEXT,
            PRIMARY KEY (run_id,post_id));
    """)
    return conn


def put_project(conn, evidence=None, *, run="run1", project_name="indie", topic="Bandung", ready=1, status="collected", stored_hash=None):
    evidence = packet() if evidence is None else evidence
    conn.execute("INSERT OR IGNORE INTO engage_tiktok_runs VALUES(?,?,?,'collection_incomplete')", (run, project_name, topic))
    conn.execute("INSERT INTO engage_tiktok_posts VALUES(?,?,?,?,?,?,?)", (
        run, evidence.get("post_id", "1234567890123456789"), ready, json.dumps(evidence),
        _hash(evidence) if stored_hash is None else stored_hash, status, evidence.get("url", ""),
    ))
    conn.commit()


def master(path):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE tiktok_master_posts (post_id TEXT PRIMARY KEY, canonical_url TEXT,
            creator_handle TEXT, latest_evidence_json TEXT, latest_evidence_hash TEXT,
            latest_snapshot_id TEXT, last_seen_at TEXT, last_run_id TEXT);
        CREATE TABLE tiktok_master_snapshots (snapshot_id TEXT PRIMARY KEY, post_id TEXT,
            source_id TEXT, local_run_id TEXT, master_run_id TEXT, observed_at TEXT,
            evidence_hash TEXT, evidence_json TEXT);
        CREATE TABLE tiktok_master_sources (source_id TEXT PRIMARY KEY, database_path TEXT);
        CREATE TABLE tiktok_master_runs (master_run_id TEXT PRIMARY KEY, local_run_id TEXT, project TEXT, topic TEXT);
        CREATE TABLE tiktok_master_run_posts (master_run_id TEXT, post_id TEXT, snapshot_id TEXT);
        CREATE TABLE tiktok_master_post_topics (post_id TEXT, topic TEXT);
        CREATE TABLE tiktok_master_music_backfill_observations (observation_id TEXT PRIMARY KEY,
            run_id TEXT, post_id TEXT, base_snapshot_id TEXT, base_evidence_hash TEXT,
            base_observed_at TEXT, music_observed_at TEXT, music_evidence_json TEXT,
            music_evidence_hash TEXT, status TEXT, error TEXT, observation_hash TEXT);
    """)
    conn.execute("INSERT INTO tiktok_master_sources VALUES('source1', '/do-not-open/original.sqlite')")
    return conn


def put_master(conn, evidence=None, *, snapshot="s1", run="r1", project_name="indie", topic="Bandung", latest=True):
    evidence = evidence or packet()
    conn.execute("INSERT OR IGNORE INTO tiktok_master_runs VALUES(?,?,?,?)", ("m" + run, run, project_name, topic))
    conn.execute("INSERT INTO tiktok_master_snapshots VALUES(?,?,'source1',?,?,?,?,?)", (
        snapshot, evidence["post_id"], run, "m" + run, evidence["observed_at"], _hash(evidence), json.dumps(evidence),
    ))
    conn.execute("INSERT INTO tiktok_master_run_posts VALUES(?,?,?)", ("m" + run, evidence["post_id"], snapshot))
    conn.execute("INSERT INTO tiktok_master_post_topics VALUES(?,?)", (evidence["post_id"], topic))
    if latest:
        conn.execute("INSERT OR REPLACE INTO tiktok_master_posts VALUES(?,?,?,?,?,?,?,?)", (
            evidence["post_id"], evidence["url"], evidence["creator"], json.dumps(evidence), _hash(evidence), snapshot, evidence["observed_at"], run,
        ))
    conn.commit()


def put_backfill(conn, evidence, *, base_hash=None, music_hash=None, observation_hash=None, status="completed"):
    music = {"schema_version": "tiktok-music-evidence-v3", "platform_music": {"status": "available", "music_id": "55", "title": "New", "author": "Musician"}}
    music["music_evidence_hash"] = _hash(music)
    values = {"schema_version": "tiktok-music-backfill-observation-v1", "run_id": "backfill1", "post_id": evidence["post_id"],
              "base_snapshot_id": "s1", "base_evidence_hash": base_hash or _hash(evidence), "base_observed_at": evidence["observed_at"],
              "music_observed_at": "2026-08-02T09:00:00+00:00", "music_evidence_hash": music_hash or music["music_evidence_hash"], "status": status, "error": ""}
    conn.execute("INSERT INTO tiktok_master_music_backfill_observations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
        "o1", "backfill1", evidence["post_id"], "s1", values["base_evidence_hash"], evidence["observed_at"],
        values["music_observed_at"], json.dumps(music), values["music_evidence_hash"], status, "", observation_hash or _hash(values),
    ))
    conn.commit()


def test_project_safe_projection_readonly_and_no_network_or_process(tmp_path, monkeypatch):
    path = tmp_path / "source.sqlite"
    conn = project(path)
    evidence = packet()
    evidence["headers"] = {"Authorization": "SUPER_SECRET"}
    evidence["music_evidence"]["platform_music"]["play_url"] = "https://media.tiktokcdn.com/a?token=SUPER_SECRET"
    evidence["music_evidence"]["tt2dsp_resolution"]["selected"]["artwork_url"] = "SUPER_SECRET"
    evidence["caption"] += " https://media.tiktokcdn.com/a?token=SUPER_SECRET"
    put_project(conn, evidence)
    conn.close()
    before = path.read_bytes()
    def forbidden(*args, **kwargs):
        raise AssertionError("No network/process access is permitted")
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    records = list(iter_tiktok_records(path))
    assert path.read_bytes() == before
    assert len(records) == 1 and records[0]["status"] == "usable"
    projected = records[0]["observations"][0]["projection"]
    assert projected["creator_identity"] == {"id": "42", "sec_uid": "stable-public-id"}
    assert projected["metrics"]["views"] == 0
    assert projected["metrics"]["followers"] is None
    assert projected["metric_availability"]["likes"] == "missing_from_source"
    assert len(projected["comments"]) == 2
    assert projected["comments"][1]["parent_id"] == "c1"
    assert "SUPER_SECRET" not in json.dumps(records)
    assert "private" not in json.dumps(projected)
    assert projected["music_evidence"]["catalogs"]["musicbrainz"]["result"]["candidates"][0]["artist_credit"] == "Artist"
    assert projected["music_evidence"]["tt2dsp_resolution"]["selected"]["artist"] == "Singer"


def test_copies_are_one_observation_and_fresh_snapshots_stay_separate(tmp_path):
    path = tmp_path / "source.sqlite"
    conn = project(path)
    put_project(conn)
    put_project(conn, run="copy")
    put_project(conn, packet(time="2026-08-02T09:00:00+00:00", views=150), run="refresh")
    conn.close()
    record, = iter_tiktok_records(path)
    assert len(record["references"]) == 3
    assert len(record["observations"]) == 2
    assert len({obs["observation_id"] for obs in record["observations"]}) == 2


@pytest.mark.parametrize("change,reason", [
    ({"evidence_ready": False}, "evidence_not_ready"),
    ({"url": "https://evil.test/@musician/video/1234567890123456789"}, "invalid_canonical_identity"),
    ({"url": "https://www.tiktok.com/@other/video/1234567890123456789"}, "invalid_canonical_identity"),
    ({"platform": "instagram"}, "unsupported_platform"),
    ({"observed_at": "2026-08-01"}, "missing_observation_time"),
])
def test_invalid_evidence_is_retained_with_reason(tmp_path, change, reason):
    path = tmp_path / "source.sqlite"
    conn = project(path)
    evidence = packet()
    evidence.update(change)
    put_project(conn, evidence)
    conn.close()
    record, = iter_tiktok_records(path)
    assert record["status"] == "unusable"
    assert reason in record["reasons"]
    assert record["observations"] == []


@pytest.mark.parametrize("kwargs,reason", [
    ({"stored_hash": "0" * 64}, "evidence_hash_mismatch"),
    ({"ready": 0}, "row_not_evidence_ready"),
    ({"status": "failed"}, "incompatible_post_status"),
])
def test_row_validation_never_repairs_source(tmp_path, kwargs, reason):
    path = tmp_path / "source.sqlite"
    conn = project(path)
    put_project(conn, **kwargs)
    conn.close()
    before = path.read_bytes()
    record, = iter_tiktok_records(path)
    assert reason in record["reasons"]
    assert record["status"] == "unusable"
    assert path.read_bytes() == before


def test_unstored_hash_is_computed_and_marked(tmp_path):
    path = tmp_path / "source.sqlite"
    conn = project(path)
    put_project(conn, stored_hash="")
    conn.close()
    record, = iter_tiktok_records(path)
    assert record["status"] == "usable"
    assert record["references"][0]["integrity"] == "computed_unstored_hash"
    assert record["observations"][0]["evidence_hash"] == _hash(packet())


def test_master_recovers_older_saved_snapshots_not_arbitrary_source_paths(tmp_path, monkeypatch):
    path = tmp_path / "master.sqlite"
    conn = master(path)
    put_master(conn)
    conn.execute("UPDATE tiktok_master_posts SET latest_evidence_json='{}'")
    conn.commit()
    conn.close()
    real_connect = sqlite3.connect
    calls = []
    def guarded_connect(database, **kwargs):
        calls.append(database)
        assert "do-not-open" not in str(database)
        return real_connect(database, **kwargs)
    monkeypatch.setattr(sqlite3, "connect", guarded_connect)
    record, = iter_tiktok_records(path)
    assert record["status"] == "usable"
    assert len(record["observations"]) == 1
    assert "empty_or_invalid_evidence" in record["reasons"]
    assert record["references"][0]["origin_database"] == "/do-not-open/original.sqlite"
    assert len(calls) == 1 and "mode=ro" in calls[0]


@pytest.mark.parametrize("kind", ["master", "project"])
def test_exact_filters_select_posts_and_retain_all_their_saved_history(tmp_path, kind):
    path = tmp_path / "source.sqlite"
    if kind == "master":
        conn = master(path)
        put_master(conn)
        put_master(conn, packet(time="2026-08-02T09:00:00+00:00"), snapshot="s2", run="r2", project_name="other", topic="Jakarta")
        put_master(conn, packet("222", creator="other"), snapshot="s3", run="r3", project_name="other", topic="Jakarta")
        run_id = "r1"
    else:
        conn = project(path)
        put_project(conn)
        put_project(conn, packet(time="2026-08-02T09:00:00+00:00"), run="r2", project_name="other", topic="Jakarta")
        put_project(conn, packet("222", creator="other"), run="r3", project_name="other", topic="Jakarta")
        run_id = "run1"
    conn.close()
    record, = iter_tiktok_records(path, projects=["INDIE"], topics=["bandung"], run_ids=[run_id])
    assert len(record["observations"]) == 2
    assert list(iter_tiktok_records(path, projects=["indie"], topics=["Jakarta"])) == []
    assert len(list(iter_tiktok_records(path, creator="https://www.tiktok.com/@other"))) == 1
    assert len(list(iter_tiktok_records(path, topics=["bandung"]))) == 1
    assert list(iter_tiktok_records(path, run_ids=["missing"])) == []


def test_valid_backfill_keeps_separate_date_and_base_binding(tmp_path):
    path = tmp_path / "master.sqlite"
    conn = master(path)
    evidence = packet()
    put_master(conn, evidence)
    put_backfill(conn, evidence)
    conn.close()
    record, = iter_tiktok_records(path)
    assert len(record["observations"]) == 1
    music, = record["music_backfills"]
    assert music["integrity"] == "verified"
    assert music["base_evidence_hash"] == _hash(evidence)
    assert music["music_observed_at"] != record["observations"][0]["observed_at"]


@pytest.mark.parametrize("kwargs,reason", [
    ({"base_hash": "0" * 64}, "music_base_binding_mismatch"),
    ({"music_hash": "0" * 64}, "music_hash_mismatch"),
    ({"observation_hash": "0" * 64}, "music_observation_hash_mismatch"),
    ({"status": "failed"}, "music_observation_not_terminal"),
])
def test_conflicting_backfills_do_not_contaminate_artist_evidence(tmp_path, kwargs, reason):
    path = tmp_path / "master.sqlite"
    conn = master(path)
    evidence = packet()
    put_master(conn, evidence)
    put_backfill(conn, evidence, **kwargs)
    conn.close()
    record, = iter_tiktok_records(path)
    assert record["status"] == "usable"
    assert reason in record["reasons"]
    assert record["music_backfills"][0]["music_evidence"] == {}


def test_empty_evidence_still_counts_and_can_be_filtered_by_saved_creator(tmp_path):
    path = tmp_path / "master.sqlite"
    conn = master(path)
    put_master(conn)
    conn.execute("DELETE FROM tiktok_master_snapshots")
    conn.execute("UPDATE tiktok_master_posts SET latest_evidence_json='{}'")
    conn.commit()
    conn.close()
    record, = iter_tiktok_records(path, creator="musician")
    assert record["status"] == "unusable"


def test_read_transaction_freezes_rows_without_hidden_limit(tmp_path):
    path = tmp_path / "source.sqlite"
    conn = project(path)
    conn.execute("PRAGMA journal_mode=WAL")
    for index in range(151):
        put_project(conn, packet(str(index + 1000)))
    iterator = iter_tiktok_records(path)
    first = next(iterator)
    put_project(conn, packet("9999"))
    remaining = list(iterator)
    assert len([first, *remaining]) == 151
    assert len(list(iter_tiktok_records(path))) == 152
    conn.close()


def test_tables_not_views_and_unsupported_filters_fail_closed(tmp_path):
    path = tmp_path / "source.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE data(post_id TEXT, latest_evidence_json TEXT); CREATE VIEW tiktok_master_posts AS SELECT * FROM data;")
    conn.close()
    with pytest.raises(ValueError, match="ordinary tables"):
        list(iter_tiktok_records(path))
    path2 = tmp_path / "minimal.sqlite"
    conn = sqlite3.connect(path2)
    conn.execute("CREATE TABLE tiktok_master_posts(post_id TEXT, latest_evidence_json TEXT)")
    conn.close()
    with pytest.raises(ValueError, match="schema"):
        list(iter_tiktok_records(path2, projects=["indie"]))
    with pytest.raises(ValueError, match="creator"):
        list(iter_tiktok_records(path2, creator="https://evil.test/artist"))
    with pytest.raises(ValueError, match="sequence"):
        list(iter_tiktok_records(path2, topics="bandung"))


def test_missing_database_is_not_created_and_errors_are_sanitized(tmp_path):
    path = tmp_path / "missing.sqlite"
    with pytest.raises(ValueError, match="read-only") as error:
        list(iter_tiktok_records(path))
    assert not path.exists()
    assert str(tmp_path) not in str(error.value)


def test_query_only_and_untrusted_schema_are_set_before_read(tmp_path, monkeypatch):
    path = tmp_path / "source.sqlite"
    conn = project(path)
    put_project(conn)
    conn.close()
    original = sqlite3.connect
    statements = []
    def connect(*args, **kwargs):
        connection = original(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection
    monkeypatch.setattr(sqlite3, "connect", connect)
    list(iter_tiktok_records(path))
    assert statements[:3] == ["PRAGMA query_only=ON", "PRAGMA trusted_schema=OFF", "BEGIN"]
    assert all(query.startswith(("SELECT", "PRAGMA", "BEGIN")) for query in statements)


def test_canonical_url_query_is_removed_but_identity_hash_is_raw(tmp_path):
    path = tmp_path / "source.sqlite"
    conn = project(path)
    evidence = packet()
    evidence["url"] += "?share=abc#fragment"
    put_project(conn, evidence)
    conn.close()
    record, = iter_tiktok_records(path)
    observation, = record["observations"]
    assert observation["evidence_hash"] == _hash(evidence)
    assert "?" not in observation["projection"]["url"]


def test_timestamp_conflict_does_not_become_growth(tmp_path):
    path = tmp_path / "master.sqlite"
    conn = master(path)
    put_master(conn)
    conn.execute("UPDATE tiktok_master_snapshots SET observed_at='2026-08-03T09:00:00+00:00'")
    conn.commit()
    conn.close()
    record, = iter_tiktok_records(path)
    assert "observation_time_conflict" in record["reasons"]
    assert len(record["observations"]) == 1


@pytest.mark.parametrize("number", ["1e309", "Infinity", "NaN"])
def test_nonfinite_json_is_invalid_record_not_whole_source_failure(tmp_path, number):
    path = tmp_path / "source.sqlite"
    conn = project(path)
    put_project(conn)
    put_project(conn, packet("222"))
    conn.execute("UPDATE engage_tiktok_posts SET evidence_json=? WHERE post_id='222'", ('{"bad":' + number + '}',))
    conn.commit()
    conn.close()
    records = list(iter_tiktok_records(path))
    assert len(records) == 2
    assert {record["status"] for record in records} == {"usable", "unusable"}


def test_registry_last_seen_is_not_observation_time(tmp_path):
    path = tmp_path / "master.sqlite"
    conn = master(path)
    put_master(conn)
    conn.execute("DELETE FROM tiktok_master_snapshots")
    conn.execute("UPDATE tiktok_master_posts SET last_seen_at='2026-08-29T09:00:00+00:00'")
    conn.commit()
    conn.close()
    record, = iter_tiktok_records(path)
    assert record["status"] == "usable"
    assert record["observations"][0]["observed_at"] == packet()["observed_at"]
    assert record["references"][0]["registry_last_seen_at"].startswith("2026-08-29")
