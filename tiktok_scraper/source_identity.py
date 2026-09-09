"""Historical source identity and explicitly migrated database locations.

Source IDs remain hashes of their original canonical database paths.  Moving
a file must not regenerate its run, snapshot, or publication-attempt IDs.
Only offline maintenance may insert a hash-bound location alias; ordinary
registration and validation only resolve existing bindings.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any


SOURCE_ALIAS_SCHEMA_VERSION = "tiktok-master-source-path-alias-v1"
SOURCE_ALIAS_TABLE = "tiktok_master_source_path_aliases"


class SourceIdentityError(RuntimeError):
    """A stored source identity or its explicit location binding is invalid."""


def _schema(value: str) -> str:
    if value not in {"main", "master"}:
        raise ValueError("master schema must be main or master")
    return value


def _path(value: str | Path) -> str:
    if not str(value).strip():
        raise SourceIdentityError("source database path is required")
    return str(Path(value).resolve())


def _path_key(value: str | Path) -> str:
    return os.path.normcase(_path(value))


def source_id_for_path(path: str | Path) -> str:
    payload = "\x1f".join(("tiktok-engage-source", _path(path)))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def source_alias_binding_hash(binding: dict[str, Any]) -> str:
    """Hash the immutable alias fields, excluding the stored checksum itself."""
    payload = {
        "schema_version": SOURCE_ALIAS_SCHEMA_VERSION,
        **{
            field: str(binding[field])
            for field in (
                "source_id", "identity_path", "database_path", "migration_id"
            )
        },
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def ensure_source_alias_schema(conn: sqlite3.Connection, schema: str = "main") -> None:
    schema = _schema(schema)
    conn.execute(
        f'''CREATE TABLE IF NOT EXISTS "{schema}"."{SOURCE_ALIAS_TABLE}" (
            database_path TEXT PRIMARY KEY,
            source_id TEXT NOT NULL UNIQUE,
            identity_path TEXT NOT NULL,
            migration_id TEXT NOT NULL,
            binding_hash TEXT NOT NULL
        )'''
    )
    conn.execute(
        f'CREATE UNIQUE INDEX IF NOT EXISTS "{schema}".idx_source_alias_identity '
        f'ON "{SOURCE_ALIAS_TABLE}" (source_id)'
    )


def _source_rows(conn: sqlite3.Connection, schema: str) -> list[dict[str, str]]:
    return [
        {"source_id": str(row[0]), "database_path": str(row[1])}
        for row in conn.execute(
            f'SELECT source_id, database_path FROM "{schema}".tiktok_master_sources'
        ).fetchall()
    ]


def _alias_rows(conn: sqlite3.Connection, schema: str) -> list[dict[str, str]]:
    exists = conn.execute(
        f'SELECT 1 FROM "{schema}".sqlite_master WHERE type=\'table\' AND name=?',
        (SOURCE_ALIAS_TABLE,),
    ).fetchone()
    if exists is None:
        return []
    fields = ("database_path", "source_id", "identity_path", "migration_id", "binding_hash")
    return [
        dict(zip(fields, (str(value) for value in row)))
        for row in conn.execute(
            f'SELECT {", ".join(fields)} FROM "{schema}"."{SOURCE_ALIAS_TABLE}"'
        ).fetchall()
    ]


def _validate_source(source: dict[str, str]) -> None:
    if source["source_id"] != source_id_for_path(source["database_path"]):
        raise SourceIdentityError("master source ID does not match its identity path")


def _validate_alias(alias: dict[str, str], sources: list[dict[str, str]]) -> dict[str, str]:
    matches = [source for source in sources if source["source_id"] == alias["source_id"]]
    if len(matches) != 1:
        raise SourceIdentityError("source path alias has no unique registered source")
    source = matches[0]
    _validate_source(source)
    if alias["identity_path"] != source["database_path"]:
        raise SourceIdentityError("source path alias identity differs from its source")
    if not alias["migration_id"].strip():
        raise SourceIdentityError("source path alias has no migration receipt binding")
    if alias["database_path"] != _path(alias["database_path"]):
        raise SourceIdentityError("source path alias location is not canonical")
    if _path_key(alias["database_path"]) == _path_key(alias["identity_path"]):
        raise SourceIdentityError("source path alias must identify a relocated path")
    if alias["binding_hash"] != source_alias_binding_hash(alias):
        raise SourceIdentityError("source path alias checksum mismatch")
    return source


def resolve_registered_source(
    conn: sqlite3.Connection, schema: str, path: str | Path
) -> dict[str, Any] | None:
    """Read a unique direct or explicitly migrated binding, without writes.

    Historical ``database_path`` remains the identity path.  The requested
    current location is returned separately as ``resolved_database_path``.
    A checksum is an integrity check, not a signature or authorization.
    """
    schema = _schema(schema)
    location = _path(path)
    key = _path_key(location)
    sources = _source_rows(conn, schema)
    direct = [source for source in sources if _path_key(source["database_path"]) == key]
    all_aliases = _alias_rows(conn, schema)
    aliases = [alias for alias in all_aliases if _path_key(alias["database_path"]) == key]
    if len(direct) + len(aliases) > 1:
        raise SourceIdentityError("master source location binding is ambiguous")
    if direct:
        source = direct[0]
        _validate_source(source)
        if any(alias["source_id"] == source["source_id"] for alias in all_aliases):
            raise SourceIdentityError("source database was relocated; use its designated location")
        return {
            **source, "identity_path": source["database_path"],
            "resolved_database_path": location, "aliased": False, "migration_id": "",
        }
    if aliases:
        alias = aliases[0]
        if sum(item["source_id"] == alias["source_id"] for item in all_aliases) != 1:
            raise SourceIdentityError("source has more than one designated location")
        source = _validate_alias(alias, sources)
        return {
            **source, "identity_path": source["database_path"],
            "resolved_database_path": location, "aliased": True,
            "migration_id": alias["migration_id"],
        }
    return None


def insert_source_path_alias(
    conn: sqlite3.Connection,
    schema: str,
    *,
    source_id: str,
    identity_path: str,
    database_path: str | Path,
    migration_id: str,
) -> dict[str, str]:
    """Record one preverified offline migration; never relocate automatically.

    The maintenance caller owns backups, file/evidence verification, and the
    transaction.  Repeating the identical binding is idempotent; replacing
    an existing location, source, or migration receipt is rejected.
    """
    schema = _schema(schema)
    binding = {
        "source_id": str(source_id), "identity_path": str(identity_path),
        "database_path": _path(database_path), "migration_id": str(migration_id),
    }
    binding["binding_hash"] = source_alias_binding_hash(binding)
    sources = _source_rows(conn, schema)
    _validate_alias(binding, sources)
    key = _path_key(binding["database_path"])
    if any(_path_key(source["database_path"]) == key for source in sources):
        raise SourceIdentityError("source alias location already has a direct source")
    all_aliases = _alias_rows(conn, schema)
    existing = [alias for alias in all_aliases if _path_key(alias["database_path"]) == key]
    if existing:
        if len(existing) != 1 or existing[0] != binding:
            raise SourceIdentityError("source alias location already has a different binding")
        _validate_alias(existing[0], sources)
        return binding
    if any(alias["source_id"] == source_id for alias in all_aliases):
        raise SourceIdentityError("source already has a different designated location")
    ensure_source_alias_schema(conn, schema)
    fields = tuple(binding)
    conn.execute(
        f'INSERT INTO "{schema}"."{SOURCE_ALIAS_TABLE}" ({", ".join(fields)}) '
        f'VALUES ({", ".join("?" for _ in fields)})',
        tuple(binding[field] for field in fields),
    )
    return binding
