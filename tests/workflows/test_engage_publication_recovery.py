"""Interrupted workers cannot cause either stranded pre-submit claims or retries after submit."""
import sqlite3

import pytest

import engage_publication_recovery as recovery
import tiktok_publication_adapter as adapter
from test_engage_tiktok import approved_publication_for_master_test


def claimed(tmp_path):
    database, master = tmp_path / "local.sqlite", tmp_path / "master.sqlite"
    publication = approved_publication_for_master_test(database, tmp_path, run_id="recovery-run", project="recovery")
    adapter.claim_publication(database, publication, daily_limit=0, max_attempts=3,
                              master_database=master, observed_account="creator")
    publication["observed_account"] = "creator"
    return database, master, publication


def test_claim_owner_is_durable_and_live_worker_is_never_released(tmp_path):
    database, master, publication = claimed(tmp_path)
    result = recovery.reconcile_interrupted(database, publication["publication_id"], master)
    assert result["action"] == "wait_for_worker"
    assert result["worker_state"] == "alive"
    assert adapter.publication_status(database, publication["publication_id"]) == "publishing"


def test_dead_pre_submit_claim_is_released_with_receipt_and_same_history(tmp_path, monkeypatch):
    database, master, publication = claimed(tmp_path)
    monkeypatch.setattr(recovery, "original_worker_state", lambda *_: "dead")
    result = recovery.reconcile_interrupted(database, publication["publication_id"], master)
    assert result["action"] == "gated_retry_available"
    assert result["master_state"] == "retryable"
    assert adapter.publication_status(database, publication["publication_id"]) == "approved"
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT attempts FROM publication_queue").fetchone()[0] == 1
        assert conn.execute("SELECT status FROM publication_receipts").fetchone()[0] == "failed"
        assert conn.execute("SELECT COUNT(*) FROM engage_publication_attempt_owners").fetchone()[0] == 1


def test_dead_post_submit_worker_becomes_uncertain_never_retryable(tmp_path, monkeypatch):
    database, master, publication = claimed(tmp_path)
    adapter.mark_publication_submit_intent(database, publication, master_database=master)
    monkeypatch.setattr(recovery, "original_worker_state", lambda *_: "dead")
    result = recovery.reconcile_interrupted(database, publication["publication_id"], master)
    assert result["action"] == "remote_reconciliation_required"
    assert result["master_state"] == "uncertain"
    assert adapter.publication_status(database, publication["publication_id"]) == "uncertain"
    assert recovery.reconcile_interrupted(database, publication["publication_id"], master)["action"] == "remote_reconciliation_required"
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM publication_receipts").fetchone()[0] == 1


@pytest.mark.parametrize("condition", ["missing_owner", "unknown_worker"])
def test_unproven_legacy_or_unknown_worker_is_not_released(tmp_path, monkeypatch, condition):
    database, master, publication = claimed(tmp_path)
    if condition == "missing_owner":
        with sqlite3.connect(database) as conn:
            conn.execute("DELETE FROM engage_publication_attempt_owners")
    else:
        monkeypatch.setattr(recovery, "original_worker_state", lambda *_: "unknown")
    assert recovery.reconcile_interrupted(database, publication["publication_id"], master)["action"] == "manual_investigation_required"
    assert adapter.publication_status(database, publication["publication_id"]) == "publishing"


def test_status_read_does_not_write_and_rejects_wrong_master(tmp_path):
    database, master, publication = claimed(tmp_path)
    before = database.read_bytes(), master.read_bytes()
    recovery.inspect_attempt(database, publication["publication_id"], master)
    assert before == (database.read_bytes(), master.read_bytes())
    with pytest.raises(RuntimeError, match="cannot change"):
        recovery.inspect_attempt(database, publication["publication_id"], tmp_path / "wrong.sqlite")
    assert not (tmp_path / "wrong.sqlite").exists()


def test_early_confirmation_survives_auxiliary_failure_and_recovery(tmp_path, monkeypatch):
    database, master, publication = claimed(tmp_path)
    adapter.mark_publication_submit_intent(database, publication, master_database=master)
    receipt = adapter.store_receipt(database, publication, success=True, capture_records=[],
                                    remote_comment_id="confirmed", visible=False, persisted=False,
                                    verification_path="", master_database=master)
    monkeypatch.setattr(recovery, "original_worker_state", lambda *_: "dead")
    result = recovery.reconcile_interrupted(database, publication["publication_id"], master)
    assert result["action"] == "confirmed_capture_recovery_only"
    adapter.update_receipt_verification(database, publication, receipt, visible=True, persisted=True,
                                        verification_path="verified.png", native_mention_proof={"status": "verified"})
    assert adapter.publication_status(database, publication["publication_id"]) == "published"
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM publication_receipts").fetchone()[0] == 1
