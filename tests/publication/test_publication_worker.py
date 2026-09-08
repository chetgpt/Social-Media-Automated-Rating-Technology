from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time

import pytest

import engage_publication_worker as worker
import publish_pending


@pytest.fixture(autouse=True)
def clean_worker_environment(monkeypatch):
    monkeypatch.delenv(worker.STATE_ENV, raising=False)
    monkeypatch.delenv(worker.NONCE_ENV, raising=False)


def _scope(tmp_path):
    return {
        "database": tmp_path / "queue.sqlite", "publication_id": "pub-test",
        "social_browser_state": tmp_path / "social" / "state.json",
    }


def test_worker_lock_excludes_second_direct_worker_and_releases(tmp_path):
    scope = _scope(tmp_path)
    first = worker.WorkerProgress.from_environment(**scope)
    try:
        with pytest.raises(worker.WorkerBusyError):
            worker.WorkerProgress.from_environment(**scope)
        first.phase("submit", submit_intent=True)
        first.phase("confirmation")
        saved = worker._read(first.path)
        assert saved["submit_intent"] is True
        assert saved["worker"] == worker.process_identity()
        assert saved["progress_sequence"] == 2
    finally:
        first.close("failed")
    second = worker.WorkerProgress.from_environment(**scope)
    second.close()


def test_cancel_only_matches_current_worker_nonce(tmp_path):
    progress = worker.WorkerProgress.from_environment(**_scope(tmp_path))
    try:
        worker._atomic_write(progress.path.with_suffix(".cancel"), {"nonce": "old-worker"})
        progress.checkpoint()
        worker._atomic_write(progress.path.with_suffix(".cancel"), {"nonce": progress.nonce})
        with pytest.raises(worker.WorkerCancellationRequested):
            progress.checkpoint()
    finally:
        progress.close("cancelled")


def test_alive_child_of_dead_supervisor_still_blocks_new_worker(tmp_path, monkeypatch):
    scope = _scope(tmp_path)
    path = worker.worker_state_path(**scope)
    worker._atomic_write(path.with_suffix(".lock"), {
        "nonce": "prior", "owner": {"pid": 123, "create_time": 1},
        "worker": {"pid": 456, "create_time": 2},
    })
    monkeypatch.setattr(worker, "identity_alive", lambda value: value.get("pid") == 456)
    with pytest.raises(worker.WorkerBusyError):
        worker.WorkerProgress.from_environment(**scope)


def test_unverifiable_stale_owner_blocks_new_worker(tmp_path, monkeypatch):
    scope = _scope(tmp_path)
    path = worker.worker_state_path(**scope)
    worker._atomic_write(path.with_suffix(".lock"), {"nonce": "prior", "owner": {"pid": 123}})
    monkeypatch.setattr(worker, "identity_alive", lambda value: None)
    with pytest.raises(worker.WorkerBusyError):
        worker.WorkerProgress.from_environment(**scope)


def test_dead_owner_lock_can_be_reclaimed_without_modifying_databases(tmp_path, monkeypatch):
    scope = _scope(tmp_path)
    scope["database"].write_bytes(b"unaltered database")
    path = worker.worker_state_path(**scope)
    worker._atomic_write(path.with_suffix(".lock"), {"nonce": "prior", "owner": {"pid": 123}})
    monkeypatch.setattr(worker, "identity_alive", lambda value: False)
    progress = worker.WorkerProgress.from_environment(**scope)
    progress.close()
    assert scope["database"].read_bytes() == b"unaltered database"


@pytest.mark.parametrize("identity_status", [False, None])
def test_supervisor_never_terminates_unverified_process(monkeypatch, identity_status):
    class Unowned:
        pid = 123

        def poll(self):
            return None

        def terminate(self):
            pytest.fail("An unverified process must not be stopped")

    monkeypatch.setattr(worker, "identity_alive", lambda value: identity_status)
    assert not worker._stop_owned_child(Unowned(), {"pid": 123, "create_time": 1})


def test_safe_progress_stream_discards_transport_secrets(capsys):
    stream = io.StringIO(
        "CDP connection failed: https://host/?token=VERY_SECRET\n"
        + worker.PROGRESS_PREFIX + json.dumps({
            "schema": worker.SCHEMA, "phase": "composition", "submit_intent": False,
            "cookie": "VERY_SECRET", "progress_sequence": 4,
        }) + "\n"
        + worker.PROGRESS_PREFIX + json.dumps({"schema": worker.SCHEMA, "phase": "https://secret"}) + "\n"
    )
    worker._reader(stream, [], stdout=False)
    captured = capsys.readouterr()
    assert "composition" in captured.err
    assert "VERY_SECRET" not in captured.err
    assert "cookie" not in captured.err
    assert "https://secret" not in captured.err


def test_supervisor_real_child_returns_final_json_and_streams_safe_progress(tmp_path, capsys):
    program = """
import json, sys
from engage_publication_worker import WorkerProgress
progress = WorkerProgress.from_environment()
progress.phase('preflight')
print('signed_url?token=SHOULD_NOT_LEAK', file=sys.stderr, flush=True)
progress.close()
print(json.dumps({'status': 'dry_run'}), flush=True)
"""
    result = worker.supervise_adapter([sys.executable, "-u", "-c", program], **_scope(tmp_path),
                                       total_timeout=15, stall_timeout=10, cancel_grace=0.2)
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"status": "dry_run"}
    assert result.stderr == ""
    captured = capsys.readouterr()
    assert '"phase": "preflight"' in captured.err
    assert "SHOULD_NOT_LEAK" not in captured.err + captured.out


def test_timeout_stops_only_owned_child_and_requires_reconciliation(tmp_path):
    program = """
import time
from engage_publication_worker import WorkerProgress
progress = WorkerProgress.from_environment()
progress.phase('submit', submit_intent=True)
while True:
    time.sleep(1)
"""
    scope = _scope(tmp_path)
    started = time.monotonic()
    result = worker.supervise_adapter([sys.executable, "-u", "-c", program], **scope,
                                       total_timeout=1.5, stall_timeout=10, cancel_grace=0.2)
    assert time.monotonic() - started < 10
    output = json.loads(result.stdout)
    assert result.returncode == 1
    assert output["status"] == "reconcile_required"
    assert output["reason"] == "worker_total_deadline_exceeded"
    assert output["worker_stopped"] is True
    saved = worker._read(worker.worker_state_path(**scope))
    assert saved["submit_intent"] is True
    assert worker.identity_alive(saved["worker"]) is False
    assert not worker.worker_state_path(**scope).with_suffix(".lock").exists()


def test_phase_deadline_survives_live_heartbeat_and_preserves_confirmed_flag(tmp_path):
    program = """
import time
from engage_publication_worker import WorkerProgress
progress = WorkerProgress.from_environment()
progress.phase('capture', submit_intent=True, publication_confirmed=True)
while True:
    time.sleep(1)
"""
    scope = _scope(tmp_path)
    result = worker.supervise_adapter([sys.executable, "-u", "-c", program], **scope,
                                       total_timeout=15, stall_timeout=2.5, cancel_grace=0.2)
    output = json.loads(result.stdout)
    assert output["status"] == "reconcile_required"
    assert output["reason"] == "worker_phase_deadline_exceeded"
    assert output["publication_confirmed"] is True
    saved = worker._read(worker.worker_state_path(**scope))
    assert saved["heartbeat_at"] > saved["phase_started_at"]


def test_supervisor_busy_does_not_start_another_child(tmp_path, monkeypatch):
    scope = _scope(tmp_path)
    progress = worker.WorkerProgress.from_environment(**scope)
    monkeypatch.setattr(worker.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("must not start a child"))
    try:
        result = worker.supervise_adapter([sys.executable, "-c", "pass"], **scope)
        assert json.loads(result.stdout)["status"] == "worker_busy"
    finally:
        progress.close()


@pytest.mark.parametrize("option", ["--worker-timeout", "--worker-stall-timeout", "--worker-cancel-grace"])
@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf"])
def test_worker_cli_rejects_unbounded_deadlines(option, value):
    with pytest.raises(SystemExit):
        publish_pending.parse_args(["--database", "test.sqlite", "--publication-id", "pub", option, value])


@pytest.mark.parametrize("status", ["human_verification_required", "reconcile_required", "uncertain"])
def test_batch_always_stops_for_human_or_reconciliation_blocker(tmp_path, monkeypatch, status):
    scope = _scope(tmp_path)
    scope["database"].touch()
    rows = [{"publication_id": "one", "target_url": "https://www.tiktok.com/@a/video/1", "mode": "live"},
            {"publication_id": "two", "target_url": "https://www.tiktok.com/@a/video/2", "mode": "live"}]
    monkeypatch.setattr(publish_pending, "select_approved_publications", lambda *args, **kwargs: rows)
    calls = []

    def supervise(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, json.dumps({"status": status}), "")

    monkeypatch.setattr(publish_pending, "supervise_adapter", supervise)
    assert publish_pending.main(["--database", str(scope["database"]), "--all-approved", "--engage-run-id",
                                 "run", "--execute", "--continue-on-error"]) == 1
    assert len(calls) == 1


def test_batch_invalid_final_output_never_echoes_secrets(tmp_path, monkeypatch, capsys):
    scope = _scope(tmp_path)
    scope["database"].touch()
    monkeypatch.setattr(publish_pending, "select_approved_publications", lambda *args, **kwargs: [
        {"publication_id": "one", "target_url": "https://www.tiktok.com/@a/video/1", "mode": "live"}])
    monkeypatch.setattr(publish_pending, "supervise_adapter", lambda command, **kwargs:
                        subprocess.CompletedProcess(command, 1, "token=PRIVATE", "cookie=PRIVATE"))
    assert publish_pending.main(["--database", str(scope["database"]), "--publication-id", "one", "--execute"]) == 1
    captured = capsys.readouterr()
    assert "PRIVATE" not in captured.out + captured.err


def test_confirmed_comment_with_captcha_stops_before_next_publication(tmp_path, monkeypatch):
    scope = _scope(tmp_path)
    scope["database"].touch()
    monkeypatch.setattr(publish_pending, "select_approved_publications", lambda *a, **k: [
        {"publication_id": item, "target_url": "https://www.tiktok.com/@a/video/1", "mode": "live"}
        for item in ("one", "two")])
    calls = []
    def publish(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, json.dumps({"status": "published", "receipt_id": "receipt",
            "human_verification": {"status": "human_verification_required", "submit_intent_recorded": True}}), "")
    monkeypatch.setattr(publish_pending, "supervise_adapter", publish)
    assert publish_pending.main(["--database", str(scope["database"]), "--all-approved", "--engage-run-id",
                                 "run", "--execute", "--continue-on-error"]) == 1
    assert len(calls) == 1
