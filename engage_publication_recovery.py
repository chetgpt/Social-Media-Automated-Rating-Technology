"""Offline, ownership-checked recovery of an interrupted ENGAGE comment worker.

Never starts a browser and never retries a submission. Old reservations without
durable worker identity require separate investigation; this command cannot
invent that identity or turn an uncertain attempt into a retryable one.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import sqlite3

import psutil


def ensure_owner_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS engage_publication_attempt_owners (
        attempt_id TEXT PRIMARY KEY, publication_id TEXT NOT NULL,
        worker_pid INTEGER NOT NULL, worker_created_at REAL NOT NULL,
        recorded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )""")


def record_claim_owner(conn: sqlite3.Connection, attempt_id: str, publication_id: str) -> None:
    """Called in the same transaction as the master reservation and local claim."""
    proc = psutil.Process(os.getpid())
    conn.execute(
        "INSERT INTO engage_publication_attempt_owners "
        "(attempt_id,publication_id,worker_pid,worker_created_at) VALUES (?,?,?,?)",
        (attempt_id, publication_id, proc.pid, proc.create_time()),
    )


def original_worker_state(pid: int, created_at: float) -> str:
    if pid <= 0 or not math.isfinite(created_at) or created_at <= 0:
        return "unknown"
    try:
        proc = psutil.Process(pid)
        # PID reuse proves that the old worker is gone, not that the new one
        # may be stopped. Recovery never sends a signal to either process.
        return "alive" if abs(proc.create_time() - created_at) < 0.001 else "dead"
    except psutil.NoSuchProcess:
        return "dead"
    except (psutil.AccessDenied, OSError):
        return "unknown"


@contextmanager
def _readonly(path: Path):
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    try:
        yield conn
    finally:
        conn.close()


def _bound_master(database: Path, publication: dict, requested: Path | None) -> Path:
    with _readonly(database) as conn:
        row = conn.execute("SELECT master_database FROM engage_tiktok_runs WHERE run_id=?",
                           (publication.get("engage_run_id", ""),)).fetchone()
    if not row or not str(row[0] or "").strip():
        raise RuntimeError("Recovery requires the existing ENGAGE master binding")
    master = Path(row[0]).resolve()
    if requested is not None and requested.resolve() != master:
        raise RuntimeError("Recovery cannot change the ENGAGE master binding")
    return master


def inspect_attempt(database: Path, publication_id: str, master_database: Path | None = None) -> dict:
    with _readonly(database) as conn:
        row = conn.execute("SELECT * FROM publication_queue WHERE publication_id=?", (publication_id,)).fetchone()
        if row is None:
            raise ValueError("Publication does not exist; no state was changed")
        publication = dict(row)
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name='engage_publication_attempt_owners'").fetchone()
        owner = conn.execute(
            "SELECT * FROM engage_publication_attempt_owners WHERE attempt_id=? AND publication_id=?",
            (publication.get("master_attempt_id", ""), publication_id),
        ).fetchone() if exists else None
    # This binding check reads the stored run's registry path; it does not
    # initialize another registry or accept a replacement registry argument.
    master = _bound_master(database, publication, master_database)
    with _readonly(Path(master)) as conn:
        attempt = conn.execute(
            "SELECT * FROM tiktok_master_comment_attempts WHERE attempt_id=? AND publication_id=?",
            (publication.get("master_attempt_id", ""), publication_id),
        ).fetchone()
    state = original_worker_state(int(owner["worker_pid"]), float(owner["worker_created_at"])) if owner else "unknown"
    attempt = dict(attempt) if attempt else {}
    if attempt and (attempt.get("post_id") != publication.get("content_key")
                    or attempt.get("account_key") != str(publication.get("expected_account", "")).lstrip("@").casefold()
                    or attempt.get("target_url") != publication.get("target_url")
                    or attempt.get("text_hash") != publication.get("draft_hash")):
        raise RuntimeError("Master/local attempt identity mismatch; recovery is blocked")
    submit = bool(attempt.get("submit_intent_at")) or attempt.get("state") in {"submit_intent", "uncertain", "confirmed"}
    action = "manual_investigation_required"
    if publication["status"] == "published" and attempt.get("state") == "confirmed":
        action = "confirmed_capture_recovery_only"
    elif publication["status"] == "publishing" and attempt and owner:
        if state == "dead":
            action = "preserve_uncertain" if submit else "release_pre_submit"
        elif state == "alive":
            action = "wait_for_worker"
    elif publication["status"] == "uncertain":
        action = "remote_reconciliation_required"
    return {
        "publication_id": publication_id,
        "attempt_id": publication.get("master_attempt_id", ""),
        "publication_status": publication["status"],
        "master_state": attempt.get("state", "missing"),
        "worker_state": state, "submit_intent": submit,
        "action": action, "publication_enabled": False,
    }


def reconcile_interrupted(database: Path, publication_id: str, master_database: Path | None = None) -> dict:
    from tiktok_publication_adapter import (
        prepare_final_publication_text, store_receipt,
    )

    result = inspect_attempt(database, publication_id, master_database)
    if result["action"] not in {"release_pre_submit", "preserve_uncertain"}:
        return result
    with _readonly(database) as conn:
        row = conn.execute("SELECT pq.*, ar.score AS analysis_score FROM publication_queue pq "
                           "LEFT JOIN analysis_runs ar ON ar.analysis_id=pq.analysis_id WHERE publication_id=?", (publication_id,)).fetchone()
        publication = dict(row)
        if publication["status"] != "publishing" or publication["master_attempt_id"] != result["attempt_id"]:
            raise RuntimeError("Publication attempt changed during recovery")
        owner = conn.execute("SELECT * FROM engage_publication_attempt_owners WHERE attempt_id=? AND publication_id=?", (result["attempt_id"], publication_id)).fetchone()
        if not owner or original_worker_state(owner["worker_pid"], owner["worker_created_at"]) != "dead":
            raise RuntimeError("Original publication worker is not proven dead")
    master = _bound_master(database, publication, master_database)
    # No freshness override: release only resolves the interrupted reservation.
    # load_approved_publication must recheck all gates before any future retry.
    final_text, final_hash, rating = prepare_final_publication_text(publication)
    publication.update(final_text=final_text, final_text_hash=final_hash, public_rating=rating,
                       observed_account=publication.get("expected_account", ""))
    publication["_submit_intent"] = result["submit_intent"]
    uncertain = result["submit_intent"]
    receipt = store_receipt(
        database, publication, success=False, capture_records=[], remote_comment_id="",
        visible=False, persisted=False, verification_path="",
        error=("Owned publication worker ended after submit intent; remote reconciliation required"
               if uncertain else "Owned publication worker ended before submit intent; normal gates apply to retry"),
        outcome="uncertain" if uncertain else "failed", master_database=master,
    )
    result = inspect_attempt(database, publication_id, Path(master))
    result.update(receipt_id=receipt, action="remote_reconciliation_required" if result["master_state"] == "uncertain" else "gated_retry_available")
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status", "reconcile"))
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--publication-id", required=True)
    parser.add_argument("--master-database", type=Path)
    args = parser.parse_args(argv)
    try:
        operation = inspect_attempt if args.command == "status" else reconcile_interrupted
        result = operation(args.database, args.publication_id, args.master_database)
    except Exception as exc:
        print(json.dumps({"status": "blocked", "error_type": type(exc).__name__, "publication_enabled": False}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
