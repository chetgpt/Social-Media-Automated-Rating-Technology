import hashlib
import json
import os
import shutil
import sqlite3

import pytest

import master_registry_repair as repair
import tiktok_master_database as master
from tiktok_scraper.source_identity import resolve_registered_source


def damaged_registry(tmp_path, *, missing=False):
    old_root, current_root = tmp_path / "old", tmp_path / "current"
    old = old_root / "project" / "state.sqlite"
    current = current_root / "project" / "state.sqlite"
    current.parent.mkdir(parents=True)
    database = tmp_path / "master.sqlite"
    stamp = "2026-09-08T00:00:00+00:00"
    packet = '{"post_id":"101"}'
    evidence_hash = hashlib.sha256(packet.encode()).hexdigest()
    with sqlite3.connect(current) as local:
        local.executescript("""
            CREATE TABLE engage_tiktok_runs (run_id TEXT, project TEXT, workflow TEXT);
            CREATE TABLE engage_tiktok_posts (run_id TEXT, post_id TEXT, evidence_hash TEXT, evidence_ready INTEGER);
            INSERT INTO engage_tiktok_runs VALUES ('run-1','test','listen');
        """)
        local.execute("INSERT INTO engage_tiktok_posts VALUES ('run-1','101',?,1)", (evidence_hash,))
    local.close()
    with sqlite3.connect(database) as conn:
        master.ensure_master_schema(conn)
        sid = master.register_source(conn, "main", old)
        run_id = master.stable_id(sid, "run-1")
        conn.execute("""INSERT INTO tiktok_master_runs
            (master_run_id,source_id,local_run_id,project,workflow,synced_at)
            VALUES (?,?,'run-1','test','listen',?)""", (run_id, sid, stamp))
        conn.execute("""INSERT INTO tiktok_master_snapshots
            (snapshot_id,post_id,source_id,master_run_id,local_run_id,observed_at,
             evidence_hash,content_hash,evidence_json,created_at)
            VALUES ('snapshot-1','101',?,?,'run-1',?,?,?,?,?)""",
            (sid, run_id, stamp, evidence_hash, evidence_hash, packet, stamp))
        conn.execute("CREATE TABLE publication_sentinel (body TEXT, approved_at TEXT)")
        conn.execute("INSERT INTO publication_sentinel VALUES ('unchanged exact text','original approval')")
        bad_id = hashlib.md5(os.path.normcase(str(current.resolve())).encode()).hexdigest()
        conn.execute("UPDATE tiktok_master_sources SET source_id=?,database_path=? WHERE source_id=?",
                     (bad_id, str(current.resolve()), sid))
    conn.close()
    if missing:
        current.unlink()
    return database, old_root, current_root, current, sid


def plan_for(paths):
    database, old, current, _, _ = paths
    return repair.make_plan(database, previous_root=old, current_root=current)


def backup(database, target):
    with sqlite3.connect(database) as source, sqlite3.connect(target) as dest:
        source.backup(dest)
    source.close()
    dest.close()
    return target


def test_restores_only_source_rows_preserving_all_other_tables_and_ids(tmp_path):
    paths = damaged_registry(tmp_path)
    database, _, _, current, sid = paths
    plan = plan_for(paths)
    original = backup(database, tmp_path / "backup.sqlite")
    receipt = repair.apply_plan(plan, database=database, backup=original)
    assert receipt["source_rows_restored"] == receipt["location_aliases"] == 1
    assert receipt["protected_tables_unchanged"]
    assert receipt["orphan_source_references"] == 0
    with sqlite3.connect(database) as conn:
        resolved = resolve_registered_source(conn, "main", current)
        assert resolved["source_id"] == sid and resolved["aliased"]
        assert conn.execute("SELECT snapshot_id,source_id FROM tiktok_master_snapshots").fetchone() == ("snapshot-1", sid)
    assert repair.apply_plan(plan, database=database, backup=original)["already_applied"]


def test_rehearsal_changes_only_copy_and_rejects_actual_database(tmp_path):
    paths = damaged_registry(tmp_path)
    database = paths[0]
    plan = plan_for(paths)
    original = backup(database, tmp_path / "backup.sqlite")
    copy = backup(database, tmp_path / "rehearsal.sqlite")
    with pytest.raises(repair.RegistryRepairError, match="rehearsal"):
        repair.apply_plan(plan, database=database, backup=original, rehearsal=True)
    repair.apply_plan(plan, database=copy, backup=original, rehearsal=True)
    with repair.readonly(database) as conn:
        assert repair.fingerprint(conn) == plan["tables_before"]


@pytest.mark.parametrize("target", ["master", "local", "backup", "plan"])
def test_changed_inputs_fail_before_any_repair(tmp_path, target):
    paths = damaged_registry(tmp_path)
    database, _, _, current, _ = paths
    plan = plan_for(paths)
    original = backup(database, tmp_path / "backup.sqlite")
    if target in {"master", "backup"}:
        with sqlite3.connect(database if target == "master" else original) as conn:
            conn.execute("UPDATE publication_sentinel SET body='changed'")
    elif target == "local":
        with sqlite3.connect(current) as conn:
            conn.execute("UPDATE engage_tiktok_posts SET evidence_hash='changed'")
    else:
        plan["repairs"][0]["source_id"] = "fabricated"
    with repair.readonly(database) as conn:
        before = repair.fingerprint(conn)
    with pytest.raises(repair.RegistryRepairError):
        repair.apply_plan(plan, database=database, backup=original)
    with repair.readonly(database) as conn:
        assert repair.fingerprint(conn) == before


def test_missing_copy_restores_historical_identity_without_alias(tmp_path):
    paths = damaged_registry(tmp_path, missing=True)
    plan = plan_for(paths)
    original = backup(paths[0], tmp_path / "backup.sqlite")
    receipt = repair.apply_plan(plan, database=paths[0], backup=original)
    assert receipt["source_rows_restored"] == 1
    assert receipt["location_aliases"] == 0
    assert receipt["missing_copies_left_at_historical_location"] == 1


def test_mismatched_existing_project_cannot_be_relocated(tmp_path):
    paths = damaged_registry(tmp_path)
    with sqlite3.connect(paths[3]) as conn:
        conn.execute("UPDATE engage_tiktok_runs SET project='different'")
    with pytest.raises(repair.RegistryRepairError, match="identity"):
        plan_for(paths)


def test_rehashed_plan_cannot_omit_required_repairs(tmp_path):
    paths = damaged_registry(tmp_path)
    plan = plan_for(paths)
    original = backup(paths[0], tmp_path / "backup.sqlite")
    plan["repairs"] = []
    plan["plan_hash"] = repair.digest({key: value for key, value in plan.items() if key != "plan_hash"})
    with pytest.raises(repair.RegistryRepairError, match="independently proven"):
        repair.apply_plan(plan, database=paths[0], backup=original)
    with repair.readonly(paths[0]) as conn:
        assert repair.fingerprint(conn) == plan["tables_before"]


def test_hardlinks_cannot_bypass_copy_or_backup_separation(tmp_path):
    paths = damaged_registry(tmp_path)
    plan = plan_for(paths)
    original = backup(paths[0], tmp_path / "backup.sqlite")
    link = tmp_path / "linked.sqlite"
    os.link(paths[0], link)
    with pytest.raises(repair.RegistryRepairError, match="hardlink"):
        repair.apply_plan(plan, database=link, backup=original, rehearsal=True)
    with pytest.raises(repair.RegistryRepairError, match="distinct"):
        repair.apply_plan(plan, database=paths[0], backup=link)


def test_existing_receipt_does_not_hide_later_database_changes(tmp_path):
    paths = damaged_registry(tmp_path)
    plan = plan_for(paths)
    original = backup(paths[0], tmp_path / "backup.sqlite")
    repair.apply_plan(plan, database=paths[0], backup=original)
    with sqlite3.connect(paths[0]) as conn:
        conn.execute("UPDATE publication_sentinel SET body='later change'")
    with pytest.raises(repair.RegistryRepairError, match="changed after repair"):
        repair.apply_plan(plan, database=paths[0], backup=original)


def test_fingerprints_are_independent_of_insertion_order(tmp_path):
    paths = damaged_registry(tmp_path)
    with sqlite3.connect(paths[0]) as conn:
        conn.execute("CREATE TABLE unordered_values (value TEXT)")
        conn.executemany("INSERT INTO unordered_values VALUES (?)", [("b",), ("a",), ("a",)])
        first = repair.fingerprint(conn)
        conn.execute("DELETE FROM unordered_values")
        conn.executemany("INSERT INTO unordered_values VALUES (?)", [("a",), ("b",), ("a",)])
        assert repair.fingerprint(conn) == first
        conn.execute("DELETE FROM unordered_values WHERE rowid=(SELECT MIN(rowid) FROM unordered_values)")
        assert repair.fingerprint(conn) != first


def test_binary_row_hash_is_independent_of_prior_rows_and_object_sharing():
    shared = "equal text value " * 100
    copied = shared.encode().decode()
    assert copied == shared and copied is not shared
    rows = [(None,), (shared, shared), (shared, copied),
            ("frame boundary", "x" * 65536, b"\0" * 131072),
            ("large evidence", "x" * 1048576)]
    expected = [repair._RowDigest().digest(row) for row in rows]
    reused = repair._RowDigest()
    assert [reused.digest(row) for row in rows] == expected
    assert [reused.digest(row) for row in reversed(rows)] == list(reversed(expected))
    assert [reused.digest(row) for row in rows] == expected
    assert expected[1] == expected[2]


def test_binary_row_hash_distinguishes_sqlite_types_and_column_boundaries():
    rows = [(None,), (0,), (1,), (1.0,), ("1",), (b"1",), ("",), (b"",),
            (1, 2), (12,), (-0.0,), (0.0,), (2**63 - 1,), (-(2**63),),
            ("unicode \u00e9\U0001f3b5",), (b"\0\xff",)]
    row_digest = repair._RowDigest()
    assert len({row_digest.digest(row) for row in rows}) == len(rows)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [("protocol", 4), ("fast", False), ("memo", True),
     ("python_implementation", "pypy"), ("python_version", [3, 10, 0, "final", 0]),
     ("row_serialization", "json")],
)
def test_rehashed_serialization_descriptor_drift_fails_before_database_access(
    tmp_path, monkeypatch, field, replacement,
):
    paths = damaged_registry(tmp_path)
    plan = plan_for(paths)
    assert plan["schema"] == "tiktok-master-source-repair-v3"
    assert plan["fingerprint_serialization"] == repair.fingerprint_serialization()
    plan["fingerprint_serialization"][field] = replacement
    plan["plan_hash"] = repair.digest({key: value for key, value in plan.items() if key != "plan_hash"})

    def unexpected_open(*args, **kwargs):
        pytest.fail("descriptor validation must run before opening databases")

    monkeypatch.setattr(repair, "readonly", unexpected_open)
    with pytest.raises(repair.RegistryRepairError, match="serialization/runtime mismatch"):
        repair.apply_plan(plan, database=paths[0], backup=tmp_path / "unused-backup.sqlite")


def test_fingerprint_progress_contains_only_table_and_bounded_row_counts(tmp_path, monkeypatch):
    paths = damaged_registry(tmp_path)
    messages = []
    monkeypatch.setattr(repair, "_FINGERPRINT_PROGRESS_ROWS", 2)
    with sqlite3.connect(paths[0]) as conn:
        conn.execute("CREATE TABLE private_values (value TEXT)")
        conn.executemany("INSERT INTO private_values VALUES (?)",
                         [(f"private-payload-{number}",) for number in range(5)])
        result = repair.fingerprint(conn, progress=messages.append)
    assert result["private_values"]["rows"] == 5
    assert [message for message in messages if message.startswith("private_values")] == [
        "private_values: 2 rows hashed", "private_values: 4 rows hashed", "private_values",
    ]
    assert all("private-payload" not in message for message in messages)
