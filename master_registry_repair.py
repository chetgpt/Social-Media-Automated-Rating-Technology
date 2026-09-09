"""Offline, backup-bound restoration of accidentally rewritten source IDs.

This maintenance command repairs source identity rows only. Existing run,
snapshot, evidence, approval and publication rows are never rewritten. Plans
must prove each historical identity from its path hash and existing references.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import pickle
import sqlite3
import sys
from typing import Any, Callable

from tiktok_scraper.source_identity import (
    SOURCE_ALIAS_TABLE, insert_source_path_alias, resolve_registered_source,
    source_id_for_path,
)

SCHEMA = "tiktok-master-source-repair-v3"
RECEIPTS = "tiktok_master_source_repairs"
_FINGERPRINT_PROGRESS_ROWS = 250_000


class RegistryRepairError(RuntimeError):
    pass


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def stable_id(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:32]


def readonly(path: str | Path) -> sqlite3.Connection:
    path = Path(path).resolve()
    if not path.is_file():
        raise RegistryRepairError("database does not exist")
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def tables(conn: sqlite3.Connection) -> list[str]:
    names = [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )]
    if any(not name.replace("_", "").isalnum() for name in names):
        raise RegistryRepairError("unsupported table identifier")
    if "tiktok_master_sources" not in names or "tiktok_master_runs" not in names:
        raise RegistryRepairError("not a compatible TikTok master registry")
    return names


def fingerprint_serialization() -> dict:
    """Bind nonportable binary serialization to this exact CPython version."""
    if sys.implementation.name != "cpython":
        raise RegistryRepairError("registry fingerprints require CPython")
    return {
        "algorithm": "sha256-sorted-row-sha256-v1",
        "row_serialization": "pickle",
        "protocol": 5,
        "fast": True,
        "memo": False,
        "python_implementation": sys.implementation.name,
        "python_version": list(sys.version_info),
    }


class _RowDigest:
    """Hash SQLite primitive values; serialized bytes are never unpickled."""

    def __init__(self) -> None:
        self.buffer = io.BytesIO()
        self.serializer = pickle.Pickler(self.buffer, protocol=5)
        # Disabling memoization removes dependence on prior rows and whether
        # equal values happen to share a Python object identity.
        self.serializer.fast = True

    def digest(self, row: Any) -> bytes:
        self.buffer.seek(0)
        self.buffer.truncate()
        self.serializer.dump(tuple(row))
        with self.buffer.getbuffer() as serialized:
            return hashlib.sha256(serialized).digest()


def fingerprint(conn: sqlite3.Connection, progress: Callable[[str], None] | None = None) -> dict:
    """Hash every stored value without exporting evidence or credential payloads."""
    fingerprint_serialization()
    result = {}
    row_digest = _RowDigest()
    for name in tables(conn):
        # Scan table pages sequentially; sorting compact row digests avoids
        # random primary-index reads of multi-gigabyte evidence payloads.
        sha = hashlib.sha256()
        schema_sql = conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()[0]
        sha.update(str(schema_sql).encode())
        rows = []
        for row in conn.execute(f'SELECT * FROM "{name}" NOT INDEXED'):
            rows.append(row_digest.digest(row))
            if progress and len(rows) % _FINGERPRINT_PROGRESS_ROWS == 0:
                progress(f"{name}: {len(rows)} rows hashed")
        rows.sort()
        for value in rows:
            sha.update(value)
        result[name] = {"rows": len(rows), "sha256": sha.hexdigest()}
        if progress:
            progress(name)
    return result


def _source_references(conn: sqlite3.Connection) -> dict[str, set[str]]:
    refs: dict[str, set[str]] = defaultdict(set)
    for table in tables(conn):
        if table in {"tiktok_master_sources", SOURCE_ALIAS_TABLE, RECEIPTS}:
            continue
        cols = [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]
        for col in ("source_id", "first_source_id", "last_source_id"):
            if col not in cols:
                continue
            for row in conn.execute(f'SELECT DISTINCT "{col}" FROM "{table}"'):
                if row[0]:
                    refs[str(row[0])].add(f"{table}.{col}")
    return refs


def _local_binding(conn: sqlite3.Connection, source_id: str, path: str) -> dict:
    """Prove the current file belongs to this historical source using all runs/posts."""
    master_runs = {row["local_run_id"]: dict(row) for row in conn.execute(
        "SELECT local_run_id,project,workflow FROM tiktok_master_runs WHERE source_id=?", (source_id,)
    )}
    master_post_rows = list(conn.execute(
        "SELECT local_run_id,post_id,evidence_hash FROM tiktok_master_snapshots WHERE source_id=?",
        (source_id,),
    ))
    master_posts = {(row[0], row[1]): row[2] for row in master_post_rows}
    if not master_runs or len(master_posts) != len(master_post_rows):
        raise RegistryRepairError("relocated source lacks unique historical run/evidence identity")
    local = readonly(path)
    try:
        local.execute("BEGIN")
        run_rows = list(local.execute(
            "SELECT run_id,project,workflow FROM engage_tiktok_runs"
        ))
        runs = {row["run_id"]: dict(row) for row in run_rows}
        if len(runs) != len(run_rows):
            raise RegistryRepairError("relocated source has duplicate local run identities")
        post_rows = list(local.execute(
            "SELECT run_id,post_id,evidence_hash FROM engage_tiktok_posts WHERE evidence_ready=1"
        ))
        posts = {(row[0], row[1]): row[2] for row in post_rows}
        if len(posts) != len(post_rows):
            raise RegistryRepairError("relocated source has duplicate local evidence identities")
        if set(runs) != set(master_runs) or posts != master_posts:
            raise RegistryRepairError("relocated source run/evidence set does not match registry")
        if any(runs[key][field] != row[field] for key, row in master_runs.items()
               for field in ("project", "workflow")):
            raise RegistryRepairError("relocated source run identity does not match registry")
        return {"runs": len(runs), "posts": len(posts), "binding_hash": digest({
            "runs": sorted((key, row["project"], row["workflow"]) for key, row in runs.items()),
            "posts": sorted((run, post, sha) for (run, post), sha in posts.items()),
        })}
    finally:
        local.close()


def _proven_repairs(conn: sqlite3.Connection, current_root: Path, previous_root: Path,
                    progress: Callable[[str], None] | None = None) -> list[dict]:
    if current_root == previous_root:
        raise RegistryRepairError("relocation roots must differ")
    sources = [dict(row) for row in conn.execute("SELECT * FROM tiktok_master_sources ORDER BY source_id")]
    registered = {row["source_id"] for row in sources}
    references = _source_references(conn)
    repairs = []
    for row in sources:
        current_path = str(Path(row["database_path"]).resolve())
        if row["source_id"] == source_id_for_path(current_path):
            continue
        malformed = hashlib.md5(os.path.normcase(current_path).encode()).hexdigest()
        if row["source_id"] != malformed or row["source_id"] in references:
            raise RegistryRepairError("source damage is outside the supported unreferenced MD5 rewrite")
        candidates = {current_path}
        try:
            candidates.add(str(previous_root / Path(current_path).relative_to(current_root)))
        except ValueError:
            pass
        matches = [path for path in candidates if source_id_for_path(path) in references
                   and source_id_for_path(path) not in registered]
        if len(matches) != 1:
            raise RegistryRepairError("historical source identity is missing or ambiguous")
        identity_path = matches[0]
        source_id = source_id_for_path(identity_path)
        for run in conn.execute("SELECT master_run_id,local_run_id FROM tiktok_master_runs WHERE source_id=?", (source_id,)):
            if run[0] != stable_id(source_id, run[1]):
                raise RegistryRepairError("historical master run ID binding is invalid")
        relocated = os.path.normcase(identity_path) != os.path.normcase(current_path)
        exists = Path(current_path).is_file()
        # A missing copy cannot authorize a relocation. Restore its original
        # identity only and leave that historical location authoritative.
        local = _local_binding(conn, source_id, current_path) if relocated and exists else None
        repairs.append({"before": row, "source_id": source_id,
                        "identity_path": identity_path, "database_path": current_path,
                        "relocated": relocated and exists,
                        "missing_relocated_copy": relocated and not exists,
                        "local_binding": local})
        if progress:
            progress(f"source {len(repairs)} verified")
    targets = [repair["source_id"] for repair in repairs]
    if len(targets) != len(set(targets)):
        raise RegistryRepairError("multiple damaged rows map to one source")
    unexplained = set(references) - registered - set(targets)
    if unexplained:
        raise RegistryRepairError("unexplained orphan source references remain")
    return repairs


def make_plan(database: str | Path, *, current_root: str | Path,
              previous_root: str | Path, progress: Callable[[str], None] | None = None) -> dict:
    serialization = fingerprint_serialization()
    database = Path(database).resolve()
    current_root, previous_root = Path(current_root).resolve(), Path(previous_root).resolve()
    conn = readonly(database)
    try:
        conn.execute("BEGIN")
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' LIMIT 1").fetchone():
            raise RegistryRepairError("master triggers require separate review")
        repairs = _proven_repairs(conn, current_root, previous_root, progress)
        plan = {"schema": SCHEMA, "database": str(database),
                "fingerprint_serialization": serialization,
                "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "current_root": str(current_root), "previous_root": str(previous_root),
                "repairs": repairs, "tables_before": fingerprint(conn, progress)}
        plan["plan_hash"] = digest(plan)
        return plan
    finally:
        conn.close()


def validate_plan(plan: dict) -> None:
    if plan.get("schema") != SCHEMA or plan.get("plan_hash") != digest({
            key: value for key, value in plan.items() if key != "plan_hash"}):
        raise RegistryRepairError("repair plan checksum/schema mismatch")
    if plan.get("fingerprint_serialization") != fingerprint_serialization():
        raise RegistryRepairError("repair fingerprint serialization/runtime mismatch")


def apply_plan(plan: dict, *, database: str | Path, backup: str | Path,
               rehearsal: bool = False, progress: Callable[[str], None] | None = None) -> dict:
    validate_plan(plan)
    database, backup = Path(database).resolve(), Path(backup).resolve()
    original = Path(plan["database"]).resolve()
    if not database.is_file() or not backup.is_file() or database.samefile(backup):
        raise RegistryRepairError("distinct existing target and consistent backup files are required")
    if rehearsal and original.exists() and database.samefile(original):
        raise RegistryRepairError("rehearsal cannot target the original through a hardlink")
    if rehearsal == (database == original):
        raise RegistryRepairError("rehearsal must target a copy; actual apply must target the frozen original")
    backup_conn = readonly(backup)
    try:
        backup_conn.execute("BEGIN")
        if fingerprint(backup_conn, progress) != plan["tables_before"]:
            raise RegistryRepairError("backup does not match the frozen pre-repair database")
    finally:
        backup_conn.close()
    conn = sqlite3.connect(database.as_uri() + "?mode=rw", uri=True, timeout=5, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' LIMIT 1").fetchone():
            raise RegistryRepairError("master triggers require separate review")
        if RECEIPTS in tables(conn):
            receipt = conn.execute(f'SELECT receipt_json FROM "{RECEIPTS}" WHERE plan_hash=?',
                                   (plan["plan_hash"],)).fetchone()
            if receipt:
                stored = json.loads(receipt[0])
                current = fingerprint(conn, progress)
                current.pop(RECEIPTS, None)
                expected = dict(stored["tables_after"])
                expected.pop(RECEIPTS, None)
                if current != expected:
                    raise RegistryRepairError("database changed after repair; receipt is not current verification")
                conn.rollback()
                return {**stored, "already_applied": True}
        if fingerprint(conn, progress) != plan["tables_before"]:
            raise RegistryRepairError("master changed after planning; no repair was applied")
        proven = _proven_repairs(conn, Path(plan["current_root"]).resolve(),
                                 Path(plan["previous_root"]).resolve(), progress)
        if proven != plan["repairs"]:
            raise RegistryRepairError("repair plan does not match independently proven source identities")
        prior_aliases = [dict(row) for row in conn.execute(f'SELECT * FROM "{SOURCE_ALIAS_TABLE}"')] if SOURCE_ALIAS_TABLE in tables(conn) else []
        for repair in plan["repairs"]:
            if repair["local_binding"] is not None:
                current = _local_binding(conn, repair["source_id"], repair["database_path"])
                if current != repair["local_binding"]:
                    raise RegistryRepairError("relocated project changed after planning")
            changed = conn.execute(
                "UPDATE tiktok_master_sources SET source_id=?,database_path=? WHERE source_id=? AND database_path=?",
                (repair["source_id"], repair["identity_path"], repair["before"]["source_id"], repair["before"]["database_path"]),
            ).rowcount
            if changed != 1:
                raise RegistryRepairError("source row changed during repair")
            if repair["relocated"]:
                insert_source_path_alias(conn, "main", source_id=repair["source_id"],
                    identity_path=repair["identity_path"], database_path=repair["database_path"],
                    migration_id=plan["plan_hash"])
        after = fingerprint(conn, progress)
        for name, before in plan["tables_before"].items():
            if name not in {"tiktok_master_sources", SOURCE_ALIAS_TABLE} and after.get(name) != before:
                raise RegistryRepairError(f"protected table changed: {name}")
        aliases_after = [dict(row) for row in conn.execute(f'SELECT * FROM "{SOURCE_ALIAS_TABLE}"')] if SOURCE_ALIAS_TABLE in tables(conn) else []
        if any(row not in aliases_after for row in prior_aliases) or len(aliases_after) != len(prior_aliases) + sum(row["relocated"] for row in plan["repairs"]):
            raise RegistryRepairError("unexpected source location alias changes")
        if after["tiktok_master_sources"]["rows"] != plan["tables_before"]["tiktok_master_sources"]["rows"]:
            raise RegistryRepairError("source count changed")
        for repair in plan["repairs"]:
            location = repair["database_path"] if repair["relocated"] else repair["identity_path"]
            resolved = resolve_registered_source(conn, "main", location)
            if not resolved or resolved["source_id"] != repair["source_id"]:
                raise RegistryRepairError("repaired location failed verification")
        registered = {row[0] for row in conn.execute("SELECT source_id FROM tiktok_master_sources")}
        if set(_source_references(conn)) - registered:
            raise RegistryRepairError("orphan source references remain")
        receipt = {"schema": SCHEMA, "plan_hash": plan["plan_hash"], "database": str(database),
                   "fingerprint_serialization": plan["fingerprint_serialization"],
                   "backup": str(backup), "rehearsal": rehearsal,
                   "source_rows_restored": len(plan["repairs"]),
                   "location_aliases": sum(row["relocated"] for row in plan["repairs"]),
                   "missing_copies_left_at_historical_location": sum(row["missing_relocated_copy"] for row in plan["repairs"]),
                   "protected_tables_unchanged": True, "orphan_source_references": 0,
                   "applied_at": dt.datetime.now(dt.timezone.utc).isoformat(), "tables_after": after}
        conn.execute(f'CREATE TABLE IF NOT EXISTS "{RECEIPTS}" (plan_hash TEXT PRIMARY KEY, receipt_json TEXT NOT NULL)')
        conn.execute(f'INSERT INTO "{RECEIPTS}" VALUES (?,?)', (plan["plan_hash"], canonical(receipt)))
        conn.commit()
        return receipt
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--database", required=True)
    plan.add_argument("--current-root", required=True)
    plan.add_argument("--previous-root", required=True)
    plan.add_argument("--output", required=True)
    apply = sub.add_parser("apply")
    apply.add_argument("--plan", required=True)
    apply.add_argument("--database", required=True)
    apply.add_argument("--backup", required=True)
    apply.add_argument("--rehearsal", action="store_true")
    apply.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    if output.exists():
        parser.error("output already exists; preserve the existing plan/receipt")
    progress = lambda item: print(json.dumps({"progress": item}), flush=True)
    if args.command == "plan":
        result = make_plan(args.database, current_root=args.current_root,
                           previous_root=args.previous_root, progress=progress)
    else:
        result = apply_plan(json.loads(Path(args.plan).read_text(encoding="utf-8")),
                            database=args.database, backup=args.backup,
                            rehearsal=args.rehearsal, progress=progress)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"status": "planned" if args.command == "plan" else "applied", "output": str(output)}))


if __name__ == "__main__":
    main()
