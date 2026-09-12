"""Mocked worker failures must preserve durable uncertainty through final JSON."""

import asyncio
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import engage_publication_worker as worker
import publish_pending
import social_browser
import tiktok_publication_adapter as adapter


@pytest.fixture
def failure_worker(tmp_path, monkeypatch):
    publication = {
        "publication_id": "publication-1", "project": "test", "expected_account": "publisher",
        "target_url": "https://www.tiktok.com/@creator/video/7654321098765432109",
    }
    state = SimpleNamespace(submitted=True, status="publishing", promoted=False, receipt_error=False,
                            error=RuntimeError("failure may contain PRIVATE response data"), publication=publication)
    page = SimpleNamespace(is_closed=lambda: False, close=AsyncMock())
    context = SimpleNamespace(new_page=AsyncMock(return_value=page))
    browser = SimpleNamespace(contexts=[context])
    manager = SimpleNamespace(
        start=AsyncMock(return_value=SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=AsyncMock(return_value=browser)))),
        __aexit__=AsyncMock(),
    )
    monkeypatch.setattr(adapter, "async_playwright", lambda: manager)
    monkeypatch.setattr(adapter, "SocialBrowserPreflight", lambda **_: SimpleNamespace(ensure_ready=AsyncMock()))
    monkeypatch.setattr(adapter, "ensure_no_challenge", AsyncMock())
    monkeypatch.setattr(adapter, "load_approved_publication", lambda *_: publication)
    monkeypatch.setattr(adapter, "bind_engage_master_database", lambda *_: tmp_path / "never-opened-master.sqlite")
    monkeypatch.setattr(adapter, "PublicationCapture", lambda *_: SimpleNamespace(records=[], flush_unanswered=Mock()))
    monkeypatch.setattr(social_browser, "load_engage_profile7_designation", lambda *_: {})
    monkeypatch.setattr(social_browser, "load_verified_profile7_state", lambda *_: {"cdp_url": "ws://offline"})
    monkeypatch.setattr(social_browser, "verified_profile_context", AsyncMock(return_value=(context, {})))
    monkeypatch.setattr(social_browser, "platform_authentication", AsyncMock(return_value={"authenticated": True}))
    monkeypatch.setattr(adapter, "publication_status", lambda *_: state.status)

    def store(*_args, **kwargs):
        if state.receipt_error:
            raise RuntimeError("PRIVATE storage diagnostic")
        state.status = "uncertain" if kwargs["outcome"] == "uncertain" or state.promoted else "approved"
        return "receipt-1"

    state.store = Mock(side_effect=store)
    monkeypatch.setattr(adapter, "store_receipt", state.store)

    async def fail(_page, _args, _database, item, _recorder):
        item.update(_claimed=True, _submit_intent=state.submitted)
        raise state.error

    monkeypatch.setattr(adapter, "_run_on_page", fail)
    state.args = SimpleNamespace(
        database=str(tmp_path / "never-opened.sqlite"), publication_id="publication-1", execute=True,
        output_dir=str(tmp_path), social_browser_state=str(tmp_path / "state.json"),
    )
    monkeypatch.setattr(adapter, "parse_args", lambda: state.args)
    monkeypatch.setenv(worker.STATE_ENV, "mocked-worker")
    monkeypatch.setattr(worker.WorkerProgress, "from_environment", Mock(return_value=Mock()))
    state.page = page
    state.manager = manager
    return state


@pytest.mark.parametrize("failure", [RuntimeError("PRIVATE"), adapter.BrowserOperationTimeout("submit_click", 6), asyncio.CancelledError()])
def test_post_submit_failure_remains_uncertain_in_final_worker_json(failure_worker, capsys, failure):
    failure_worker.error = failure
    with pytest.raises(SystemExit) as stopped:
        adapter.main()
    assert stopped.value.code == 1
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["status"] == "uncertain"
    assert result["receipt_id"] == "receipt-1"
    assert result["submission_possible"] is True
    assert result["action"] == "remote_reconciliation_required"
    assert failure_worker.store.call_args.kwargs["outcome"] == "uncertain"
    assert "PRIVATE" not in output
    failure_worker.page.close.assert_awaited_once()
    failure_worker.manager.__aexit__.assert_awaited_once()


def test_master_promoted_uncertainty_survives_missing_in_memory_intent(failure_worker):
    failure_worker.submitted = False
    failure_worker.promoted = True
    result = asyncio.run(adapter.run(failure_worker.args))
    assert failure_worker.store.call_args.kwargs["outcome"] == "failed"
    assert result["status"] == "uncertain"
    assert result["submission_possible"] is True
    assert result["action"] == "remote_reconciliation_required"


def test_observed_publish_request_preserves_uncertainty_without_memory_flag(failure_worker, monkeypatch):
    failure_worker.submitted = False
    monkeypatch.setattr(adapter, "PublicationCapture", lambda *_: SimpleNamespace(
        records=[{"request_url": "https://www.tiktok.com/api/comment/publish/"}],
        flush_unanswered=Mock(),
    ))
    monkeypatch.setattr(adapter, "safe_tiktok_publication_capture", lambda record, **_: dict(record))
    result = asyncio.run(adapter.run(failure_worker.args))
    assert result["status"] == "uncertain"
    assert result["submit_intent_recorded"] is False
    assert result["submission_possible"] is True
    assert failure_worker.store.call_args.kwargs["outcome"] == "uncertain"


@pytest.mark.parametrize("stored_status,expected_status,action", [
    ("uncertain", "uncertain", "remote_reconciliation_required"),
    ("published", "reconcile_required", "confirmed_capture_recovery_only"),
    ("approved", "reconcile_required", "inspect_durable_attempt_before_retry"),
])
def test_existing_outcome_is_never_overwritten_or_assumed_retryable(
    failure_worker, stored_status, expected_status, action,
):
    failure_worker.submitted = False
    failure_worker.status = stored_status
    result = asyncio.run(adapter.run(failure_worker.args))
    assert result["status"] == expected_status
    assert result["action"] == action
    assert result["publication_confirmed"] is (stored_status == "published")
    assert failure_worker.status == stored_status
    failure_worker.store.assert_not_called()


@pytest.mark.parametrize("submitted", [False, True])
def test_receipt_failure_requires_reconciliation_even_before_submit(failure_worker, submitted):
    failure_worker.submitted = submitted
    failure_worker.receipt_error = True
    result = asyncio.run(adapter.run(failure_worker.args))
    assert result["status"] == "reconcile_required"
    assert result["outcome_error"] == "durable_outcome_not_stored_or_verified"
    assert result["receipt_id"] == ""
    assert result["action"] != "gated_retry_available"


def test_only_durably_recorded_pre_submit_failure_is_retryable(failure_worker):
    failure_worker.submitted = False
    result = asyncio.run(adapter.run(failure_worker.args))
    assert result["status"] == "retryable_failure"
    assert result["receipt_id"] == "receipt-1"
    assert result["submission_possible"] is False
    assert result["action"] == "gated_retry_available"


@pytest.mark.parametrize("output,returncode", [
    ({"status": "blocked", "action": "inspect_durable_attempt_before_retry"}, 1),
    ({"status": "failed"}, 1),
    ({"status": "failed", "submit_intent_recorded": True}, 1),
    ({"status": "failed", "submission_possible": True}, 1),
    ({"status": "failed", "action": "remote_reconciliation_required"}, 1),
    ({"status": "published"}, 1),
    ({"status": "dry_run"}, 1),
    ({"status": "uncertain"}, 0),
    ({"status": "retryable_failure", "submission_possible": True}, 1),
    ({"status": "retryable_failure", "action": "inspect_durable_attempt_before_retry"}, 1),
    ({"status": "worker_start_failed", "submit_intent_recorded": True}, 1),
    (None, 1),
])
def test_batch_preserves_later_rows_after_ambiguous_failure(tmp_path, monkeypatch, output, returncode):
    database = tmp_path / "unused.sqlite"
    database.touch()
    rows = [{"publication_id": str(i), "target_url": f"https://www.tiktok.com/@a/video/{i}", "mode": "live"} for i in (1, 2)]
    monkeypatch.setattr(publish_pending, "select_approved_publications", lambda *_a, **_k: rows)

    def supervise(command, **_kwargs):
        if output is None:
            raise subprocess.CalledProcessError(1, command, output=json.dumps({"status": "uncertain"}))
        return subprocess.CompletedProcess(command, returncode, json.dumps(output), "")

    calls = Mock(side_effect=supervise)
    monkeypatch.setattr(publish_pending, "supervise_adapter", calls)
    result = publish_pending.main(["--database", str(database), "--all-approved", "--engage-run-id", "run",
                                   "--execute", "--continue-on-error"])
    assert result == 1
    calls.assert_called_once()


@pytest.mark.parametrize("output", [
    {"status": "retryable_failure", "receipt_id": "receipt-1", "submission_possible": False,
     "submit_intent_recorded": False, "action": "gated_retry_available"},
    {"status": "worker_start_failed", "error_class": "OSError"},
])
def test_batch_explicitly_continues_after_verified_pre_submit_failure(tmp_path, monkeypatch, output):
    database = tmp_path / "unused.sqlite"
    database.touch()
    rows = [{"publication_id": str(i), "target_url": f"https://www.tiktok.com/@a/video/{i}", "mode": "live"} for i in (1, 2)]
    monkeypatch.setattr(publish_pending, "select_approved_publications", lambda *_a, **_k: rows)
    calls = Mock(side_effect=[
        subprocess.CompletedProcess([], 1, json.dumps(output), ""),
        subprocess.CompletedProcess([], 0, json.dumps({"status": "published"}), ""),
    ])
    monkeypatch.setattr(publish_pending, "supervise_adapter", calls)
    result = publish_pending.main(["--database", str(database), "--all-approved", "--engage-run-id", "run",
                                   "--execute", "--continue-on-error"])
    assert result == 1
    assert calls.call_count == 2
