import sqlite3

import pytest

import tiktok_master_database as master
from tiktok_scraper.source_identity import (
    SourceIdentityError, insert_source_path_alias, resolve_registered_source,
)


def registered(tmp_path):
    conn = sqlite3.connect(":memory:")
    master.ensure_master_schema(conn)
    old, current = tmp_path / "old.sqlite", tmp_path / "current.sqlite"
    sid = master.register_source(conn, "main", old)
    return conn, old, current, sid


def alias(conn, old, current, sid):
    return insert_source_path_alias(conn, "main", source_id=sid,
        identity_path=str(old.resolve()), database_path=current, migration_id="verified-migration")


def test_relocated_registration_preserves_identity_and_blocks_old_location(tmp_path):
    conn, old, current, sid = registered(tmp_path)
    binding = alias(conn, old, current, sid)
    assert master.register_source(conn, "main", current) == sid
    assert conn.execute("SELECT COUNT(*) FROM tiktok_master_sources").fetchone()[0] == 1
    assert conn.execute("SELECT database_path FROM tiktok_master_sources").fetchone()[0] == str(old)
    assert resolve_registered_source(conn, "main", current)["aliased"]
    assert alias(conn, old, current, sid) == binding
    with pytest.raises(SourceIdentityError, match="relocated"):
        master.register_source(conn, "main", old)
    with pytest.raises(SourceIdentityError, match="different designated"):
        alias(conn, old, tmp_path / "third.sqlite", sid)
    conn.close()


def test_alias_tampering_and_conflicting_direct_sources_fail_closed(tmp_path):
    conn, old, current, sid = registered(tmp_path)
    alias(conn, old, current, sid)
    conn.execute("UPDATE tiktok_master_source_path_aliases SET migration_id='tampered'")
    with pytest.raises(SourceIdentityError, match="checksum"):
        resolve_registered_source(conn, "main", current)
    conn.close()
    conn, old, current, sid = registered(tmp_path)
    master.register_source(conn, "main", current)
    with pytest.raises(SourceIdentityError, match="direct source"):
        alias(conn, old, current, sid)
    conn.close()


def test_unregistered_copy_is_not_automatically_aliased(tmp_path):
    conn, old, current, sid = registered(tmp_path)
    assert resolve_registered_source(conn, "main", current) is None
    assert master.register_source(conn, "main", current) != sid
    assert conn.execute("SELECT COUNT(*) FROM tiktok_master_source_path_aliases").fetchone()[0] == 0
    conn.close()
