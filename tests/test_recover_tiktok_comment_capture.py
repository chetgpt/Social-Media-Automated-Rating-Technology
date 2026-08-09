from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

import recover_tiktok_comment_capture as recovery
import tiktok_master_database as master_registry


POST_ID = "7654321098765432109"
TARGET = f"https://www.tiktok.com/@creator/video/{POST_ID}"
TEXT = "An exact AI-assisted comment."
TEXT_HASH = hashlib.sha256(TEXT.encode("utf-8")).hexdigest()


def _database(
    tmp_path: Path,
    *,
    queue_status: str = "published",
    receipt_status: str = "published",
    expected_account: str = "publisher",
    observed_account: str = "publisher",
    queue_comment_id: str = "comment-88",
    receipt_comment_id: str = "comment-88",
    response_updates: dict | None = None,
) -> Path:
    path = tmp_path / "engage.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE publication_queue (
            publication_id TEXT PRIMARY KEY,
            platform TEXT NOT NULL,
            status TEXT NOT NULL,
            content_key TEXT NOT NULL,
            target_url TEXT NOT NULL,
            draft_text TEXT NOT NULL,
            draft_hash TEXT NOT NULL,
            expected_account TEXT NOT NULL,
            remote_comment_id TEXT NOT NULL,
            master_attempt_id TEXT NOT NULL
        );
        CREATE TABLE publication_receipts (
            receipt_id TEXT PRIMARY KEY,
            publication_id TEXT NOT NULL,
            status TEXT NOT NULL,
            remote_comment_id TEXT NOT NULL,
            master_attempt_id TEXT NOT NULL,
            response_json TEXT NOT NULL
        );
        """
    )
    response = {
        "target_url": TARGET,
        "submitted_text": TEXT,
        "final_text_hash": TEXT_HASH,
        "observed_account": observed_account,
    }
    response.update(response_updates or {})
    conn.execute(
        """
        INSERT INTO publication_queue VALUES (?, 'tiktok', ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "publication-1",
            queue_status,
            POST_ID,
            TARGET,
            TEXT,
            TEXT_HASH,
            expected_account,
            queue_comment_id,
            "master-attempt-1",
        ),
    )
    conn.execute(
        """
        INSERT INTO publication_receipts VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            "receipt-1",
            "publication-1",
            receipt_status,
            receipt_comment_id,
            "master-attempt-1",
            json.dumps(response),
        ),
    )
    conn.commit()
    conn.close()
    return path


def _source(database: Path) -> recovery.ConfirmedCommentSource:
    return recovery.load_confirmed_comment_source(
        database,
        publication_id="publication-1",
        receipt_id="receipt-1",
    )


def test_legacy_receipt_binds_only_to_one_exact_confirmed_master_attempt(tmp_path):
    database = tmp_path / "legacy.sqlite"
    master_database = tmp_path / "master.sqlite"
    conn = sqlite3.connect(database)
    conn.executescript(
        """
        CREATE TABLE publication_queue (
            publication_id TEXT PRIMARY KEY, platform TEXT, status TEXT,
            content_key TEXT, target_url TEXT, draft_text TEXT,
            draft_hash TEXT, expected_account TEXT, remote_comment_id TEXT
        );
        CREATE TABLE publication_receipts (
            receipt_id TEXT PRIMARY KEY, publication_id TEXT, status TEXT,
            remote_comment_id TEXT, response_json TEXT
        );
        """
    )
    response = {
        "target_url": TARGET,
        "submitted_text": TEXT,
        "final_text_hash": TEXT_HASH,
        "observed_account": "publisher",
    }
    conn.execute(
        "INSERT INTO publication_queue VALUES "
        "('publication-1', 'tiktok', 'published', ?, ?, ?, ?, "
        "'publisher', 'comment-88')",
        (POST_ID, TARGET, TEXT, TEXT_HASH),
    )
    conn.execute(
        "INSERT INTO publication_receipts VALUES "
        "('receipt-1', 'publication-1', 'published', 'comment-88', ?)",
        (json.dumps(response),),
    )
    conn.commit()
    conn.close()
    master = sqlite3.connect(master_database)
    master.execute(
        """
        CREATE TABLE tiktok_master_comment_attempts (
            attempt_id TEXT, account_key TEXT, post_id TEXT,
            publication_id TEXT, text_hash TEXT, state TEXT,
            remote_comment_id TEXT
        )
        """
    )
    master.execute(
        "INSERT INTO tiktok_master_comment_attempts VALUES "
        "('master-confirmed-1', 'publisher', ?, 'publication-1', ?, "
        "'confirmed', 'comment-88')",
        (POST_ID, TEXT_HASH),
    )
    master.commit()
    master.close()

    attempt_id = recovery.migrate_legacy_master_attempt_binding(
        database,
        publication_id="publication-1",
        receipt_id="receipt-1",
        master_database=master_database,
    )

    assert attempt_id == "master-confirmed-1"
    conn = sqlite3.connect(database)
    assert conn.execute(
        "SELECT master_attempt_id FROM publication_queue"
    ).fetchone()[0] == attempt_id
    receipt_attempt, stored_response = conn.execute(
        "SELECT master_attempt_id, response_json FROM publication_receipts"
    ).fetchone()
    conn.close()
    assert receipt_attempt == attempt_id
    assert json.loads(stored_response)["master_attempt_id"] == attempt_id


def _screenshot(tmp_path: Path) -> dict:
    media = tmp_path / "comment.png"
    media.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR"
        + (640).to_bytes(4, "big")
        + (320).to_bytes(4, "big")
    )
    media_hash = hashlib.sha256(media.read_bytes()).hexdigest()
    return {
        "path": str(media),
        "sha256": media_hash,
        "size_bytes": media.stat().st_size,
        "mime_type": "image/png",
        "proof": {
            "schema_version": "tiktok-exact-comment-screenshot-v1",
            "capture_kind": "exact_comment_element",
            "unique_match": True,
            "source_post_id": POST_ID,
            "canonical_url": TARGET,
            "remote_comment_id": "comment-88",
            "comment_text_hash": TEXT_HASH,
            "observed_account": "publisher",
            "media_sha256": media_hash,
            "locator_strategy": "remote_comment_id",
            "captured_at": "2026-08-02T12:00:00+07:00",
        },
    }


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"queue_status": "approved"}, "published publication queue"),
        ({"receipt_status": "uncertain"}, "confirmed published receipt"),
        ({"observed_account": "wrong"}, "different TikTok account"),
        ({"receipt_comment_id": "other"}, "comment IDs do not match"),
        (
            {"response_updates": {"submitted_text": "different"}},
            "exact reviewed comment text",
        ),
        (
            {"response_updates": {"target_url": TARGET.replace(POST_ID, "1")}},
            "target URL does not match",
        ),
    ],
)
def test_source_loader_fails_closed_on_binding_drift(tmp_path, kwargs, message):
    database = _database(tmp_path, **kwargs)
    with pytest.raises(recovery.CommentCaptureRecoveryError, match=message):
        _source(database)


def test_capture_proof_accepts_remote_id_or_verified_exact_text_fallback(tmp_path):
    source = _source(_database(tmp_path))
    screenshot = _screenshot(tmp_path)
    recovery._validate_capture_proof(screenshot, source)
    screenshot["proof"]["locator_strategy"] = (
        "unique_exact_text_with_confirmed_publish_id"
    )
    recovery._validate_capture_proof(screenshot, source)
    screenshot["proof"]["remote_comment_id"] = "different"
    with pytest.raises(recovery.CommentCaptureRecoveryError, match="remote_comment_id"):
        recovery._validate_capture_proof(screenshot, source)


def test_missing_receipt_id_requires_unique_text_and_extracts_stable_dom_id(
    tmp_path,
    monkeypatch,
):
    source = _source(
        _database(tmp_path, queue_comment_id="", receipt_comment_id="")
    )
    unique_comment = object()

    async def fake_locator(page, text):
        assert page == "page"
        assert text == TEXT
        return unique_comment

    async def fake_markup(comment):
        assert comment is unique_comment
        return (
            '<div data-comment-id="998877665544">'
            '<a href="/@publisher">publisher</a>comment</div>'
        )

    monkeypatch.setattr(
        recovery.publication_adapter,
        "exact_published_comment_locator",
        fake_locator,
    )
    monkeypatch.setattr(
        recovery.publication_adapter,
        "_comment_identity_html",
        fake_markup,
    )
    assert (
        asyncio.run(recovery._comment_id_for_capture("page", source))
        == "998877665544"
    )


def test_missing_receipt_id_rejects_ambiguous_exact_text(tmp_path, monkeypatch):
    source = _source(
        _database(tmp_path, queue_comment_id="", receipt_comment_id="")
    )

    async def no_unique_match(_page, _text):
        return None

    monkeypatch.setattr(
        recovery.publication_adapter,
        "exact_published_comment_locator",
        no_unique_match,
    )
    with pytest.raises(recovery.CommentCaptureRecoveryError, match="not unique"):
        asyncio.run(recovery._comment_id_for_capture("page", source))


def test_missing_receipt_id_requires_confirmed_comment_author(tmp_path, monkeypatch):
    source = _source(
        _database(tmp_path, queue_comment_id="", receipt_comment_id="")
    )

    async def unique_match(_page, _text):
        return object()

    async def other_author(_comment):
        return (
            '<div data-comment-id="998877665544">'
            '<a href="/@someone-else">someone else</a></div>'
        )

    monkeypatch.setattr(
        recovery.publication_adapter,
        "exact_published_comment_locator",
        unique_match,
    )
    monkeypatch.setattr(
        recovery.publication_adapter,
        "_comment_identity_html",
        other_author,
    )
    with pytest.raises(recovery.CommentCaptureRecoveryError, match="authored"):
        asyncio.run(recovery._comment_id_for_capture("page", source))


class _LoadingPage:
    def __init__(self, final_url: str = TARGET):
        self.url = final_url
        self.waits = []

    async def wait_for_timeout(self, timeout_ms):
        self.waits.append(timeout_ms)


def test_bounded_loader_rechecks_only_confirmed_remote_id(tmp_path, monkeypatch):
    source = _source(_database(tmp_path))
    page = _LoadingPage()
    calls = []
    resolved = object()

    async def fake_locator(candidate_page, text, *, remote_comment_id):
        calls.append((candidate_page, text, remote_comment_id))
        return resolved if len(calls) == 3 else None

    progress_round = 0

    async def fake_advance(candidate_page):
        nonlocal progress_round
        assert candidate_page is page
        progress_round += 1
        return {
            "comment_count": progress_round,
            "clicked_selector": "",
            "scroll_kind": "comment_ancestor",
            "scroll_before": progress_round - 1,
            "scroll_after": progress_round,
            "scroll_height": 100 + progress_round,
            "client_height": 10,
        }

    monkeypatch.setattr(
        recovery.publication_adapter,
        "exact_published_comment_locator",
        fake_locator,
    )
    monkeypatch.setattr(recovery, "_advance_comment_panel", fake_advance)

    result = asyncio.run(
        recovery._load_exact_confirmed_comment(
            page,
            source,
            remote_comment_id="comment-88",
            max_wait_ms=1000,
            max_rounds=8,
            stable_rounds=3,
            poll_ms=1,
        )
    )

    assert result is resolved
    assert len(calls) == 3
    assert all(call[0] is page for call in calls)
    assert all(call[1] == TEXT for call in calls)
    assert all(call[2] == "comment-88" for call in calls)


def test_bounded_loader_stops_after_stable_no_progress(tmp_path, monkeypatch):
    source = _source(_database(tmp_path))
    page = _LoadingPage()
    locator_calls = []
    advance_calls = []
    progress = {
        "comment_count": 12,
        "clicked_selector": "",
        "scroll_kind": "comment_ancestor",
        "scroll_before": 90,
        "scroll_after": 100,
        "scroll_height": 200,
        "client_height": 100,
    }

    async def no_match(_page, _text, *, remote_comment_id):
        locator_calls.append(remote_comment_id)
        return None

    async def no_progress(_page):
        advance_calls.append(True)
        return dict(progress)

    monkeypatch.setattr(
        recovery.publication_adapter,
        "exact_published_comment_locator",
        no_match,
    )
    monkeypatch.setattr(recovery, "_advance_comment_panel", no_progress)

    result = asyncio.run(
        recovery._load_exact_confirmed_comment(
            page,
            source,
            remote_comment_id="comment-88",
            max_wait_ms=1000,
            max_rounds=20,
            stable_rounds=2,
            poll_ms=1,
        )
    )

    assert result is None
    # Initial signature + two unchanged rounds, followed by one final lookup.
    assert len(advance_calls) == 3
    assert locator_calls == ["comment-88"] * 4


def test_bounded_loader_deadline_cancels_stalled_dom_lookup(tmp_path, monkeypatch):
    source = _source(_database(tmp_path))
    page = _LoadingPage()
    advanced = []

    async def stalled_locator(_page, _text, *, remote_comment_id):
        assert remote_comment_id == "comment-88"
        await asyncio.sleep(0.05)
        return object()

    async def forbidden_advance(_page):
        advanced.append(True)
        return {}

    monkeypatch.setattr(
        recovery.publication_adapter,
        "exact_published_comment_locator",
        stalled_locator,
    )
    monkeypatch.setattr(recovery, "_advance_comment_panel", forbidden_advance)

    result = asyncio.run(
        recovery._load_exact_confirmed_comment(
            page,
            source,
            remote_comment_id="comment-88",
            max_wait_ms=5,
            max_rounds=10,
        )
    )

    assert result is None
    assert advanced == []


def test_bounded_loader_rejects_missing_id_and_navigation(tmp_path, monkeypatch):
    source = _source(_database(tmp_path))
    with pytest.raises(recovery.CommentCaptureRecoveryError, match="stable remote"):
        asyncio.run(
            recovery._load_exact_confirmed_comment(
                _LoadingPage(),
                source,
                remote_comment_id="",
            )
        )

    async def forbidden_locator(*_args, **_kwargs):
        raise AssertionError("wrong-post DOM must not be inspected")

    monkeypatch.setattr(
        recovery.publication_adapter,
        "exact_published_comment_locator",
        forbidden_locator,
    )
    with pytest.raises(recovery.CommentCaptureRecoveryError, match="navigated away"):
        asyncio.run(
            recovery._load_exact_confirmed_comment(
                _LoadingPage("https://www.tiktok.com/@creator/video/1"),
                source,
                remote_comment_id="comment-88",
            )
        )


class _AdvanceCollection:
    def __init__(self, items):
        self.items = list(items)

    async def count(self):
        return len(self.items)

    def nth(self, index):
        return self.items[index]


class _VisibleControl:
    async def is_visible(self):
        return True


class _CountFailure:
    async def count(self):
        raise RuntimeError("execution context was destroyed")


class _FirstVisiblePage:
    def locator(self, selector):
        if selector == "transient":
            return _CountFailure()
        if selector == "ready":
            return _AdvanceCollection([_VisibleControl()])
        return _AdvanceCollection([])


def test_first_visible_ignores_transient_locator_count_failure():
    locator, selector = asyncio.run(
        recovery.publication_adapter.first_visible(
            _FirstVisiblePage(),
            ("transient", "ready"),
        )
    )
    assert locator is not None
    assert selector == "ready"


class _AdvanceButton:
    def __init__(self):
        self.clicked = []

    async def is_visible(self):
        return True

    async def click(self, **kwargs):
        self.clicked.append(kwargs)


class _DelayedPanelPage:
    def __init__(self):
        self.phase = 0
        self.waits = []
        self.tab = _AdvanceButton()

    def locator(self, selector):
        if (
            selector == recovery.publication_adapter.INPUT_SELECTORS[0]
            and self.phase >= 3
        ):
            return _AdvanceCollection([_VisibleControl()])
        if (
            selector == recovery.publication_adapter.COMMENTS_TAB_SELECTORS[0]
            and self.phase >= 2
        ):
            return _AdvanceCollection([self.tab])
        return _AdvanceCollection([])

    async def wait_for_timeout(self, timeout_ms):
        self.waits.append(timeout_ms)
        self.phase += 1
        if self.tab.clicked:
            self.phase = 3


def test_open_comments_panel_waits_for_delayed_transient_control():
    page = _DelayedPanelPage()
    result = asyncio.run(
        recovery.publication_adapter.open_comments_panel(
            page,
            timeout_ms=1000,
            poll_ms=1,
        )
    )
    assert result["opened"] is True
    assert result["input_selector"] == (
        recovery.publication_adapter.INPUT_SELECTORS[0]
    )
    assert page.tab.clicked == [{"timeout": 1500}]
    assert len(page.waits) >= 2


class _AdvancePage:
    def __init__(self):
        self.button = _AdvanceButton()
        self.evaluations = []

    def locator(self, selector):
        if selector == recovery.COMMENT_LOAD_MORE_SELECTORS[0]:
            return _AdvanceCollection([self.button])
        if selector == recovery.COMMENT_ITEM_SELECTOR:
            return _AdvanceCollection([object(), object()])
        return _AdvanceCollection([])

    async def evaluate(self, script, arguments):
        self.evaluations.append((script, arguments))
        return {
            "kind": "comment_ancestor",
            "before": 20,
            "after": 120,
            "scrollHeight": 220,
            "clientHeight": 100,
        }


def test_comment_panel_advance_uses_scoped_load_more_and_scroll_geometry():
    page = _AdvancePage()
    progress = asyncio.run(recovery._advance_comment_panel(page))

    assert page.button.clicked == [{"timeout": 1500}]
    assert progress == {
        "comment_count": 2,
        "clicked_selector": recovery.COMMENT_LOAD_MORE_SELECTORS[0],
        "scroll_kind": "comment_ancestor",
        "scroll_before": 20,
        "scroll_after": 120,
        "scroll_height": 220,
        "client_height": 100,
    }
    assert page.evaluations[0][1] == {
        "itemSelector": recovery.COMMENT_ITEM_SELECTOR,
        "listSelector": recovery.COMMENT_LIST_SELECTOR,
    }


class _Page:
    def __init__(self, final_url: str):
        self.url = final_url
        self.goto_calls = []

    async def goto(self, url, **kwargs):
        self.goto_calls.append((url, kwargs))


def test_page_capture_checks_post_account_and_passes_confirmed_remote_id(
    tmp_path,
    monkeypatch,
):
    source = _source(_database(tmp_path))
    screenshot = _screenshot(tmp_path)
    opened = []
    loaded = []
    captured = []

    async def fake_account(_page, *, timeout_ms):
        assert timeout_ms == 15000
        return "publisher"

    async def fake_open(page, *, timeout_ms):
        opened.append((page, timeout_ms))
        return {"opened": True}

    async def fake_load(page, loaded_source, *, remote_comment_id):
        loaded.append((page, loaded_source, remote_comment_id))
        return object()

    async def fake_capture(page, text, **kwargs):
        captured.append((page, text, kwargs))
        return screenshot

    monkeypatch.setattr(recovery, "active_tiktok_account", fake_account)
    monkeypatch.setattr(recovery.publication_adapter, "open_comments_panel", fake_open)
    monkeypatch.setattr(recovery, "_load_exact_confirmed_comment", fake_load)
    monkeypatch.setattr(
        recovery.publication_adapter,
        "capture_exact_published_comment",
        fake_capture,
    )
    page = _Page(TARGET)
    result = asyncio.run(
        recovery._capture_on_page(
            page,
            source,
            output_path=tmp_path / "output.png",
        )
    )

    assert result is screenshot
    assert opened == [(page, recovery.COMMENT_PANEL_OPEN_WAIT_MS)]
    assert loaded == [(page, source, "comment-88")]
    assert captured[0][1] == TEXT
    assert captured[0][2]["remote_comment_id"] == "comment-88"
    assert captured[0][2]["source_post_id"] == POST_ID


def test_page_capture_rejects_redirect_and_wrong_account(tmp_path, monkeypatch):
    source = _source(_database(tmp_path))
    with pytest.raises(recovery.CommentCaptureRecoveryError, match="different post"):
        asyncio.run(
            recovery._capture_on_page(
                _Page("https://www.tiktok.com/@creator/video/1"),
                source,
                output_path=tmp_path / "output.png",
            )
        )

    async def wrong_account(_page, *, timeout_ms):
        return "other-account"

    monkeypatch.setattr(recovery, "active_tiktok_account", wrong_account)
    with pytest.raises(recovery.CommentCaptureRecoveryError, match="active TikTok account"):
        asyncio.run(
            recovery._capture_on_page(
                _Page(TARGET),
                source,
                output_path=tmp_path / "output.png",
            )
        )


def test_recovery_preflights_attaches_and_enqueues_without_changing_receipt(
    tmp_path,
    monkeypatch,
):
    database = _database(tmp_path)
    screenshot = _screenshot(tmp_path)
    calls = []

    class FakePreflight:
        def __init__(self, **kwargs):
            calls.append(("preflight_init", kwargs))

        async def ensure_ready(self):
            calls.append(("preflight_ready",))
            return {"observed_account": "publisher"}

    async def fake_capture(source, **kwargs):
        calls.append(("capture", source, kwargs))
        return screenshot

    def fake_enqueue(path, source):
        calls.append(("enqueue", path, source))
        return {"created": True, "showcase_id": "showcase-1"}

    def fake_prepare(path, showcase, *, config_path):
        calls.append(("prepare", path, showcase, config_path))
        return {"attempted": False, "status": "not_configured"}

    monkeypatch.setattr(recovery, "SocialBrowserPreflight", FakePreflight)
    monkeypatch.setattr(recovery, "_capture_from_verified_profile", fake_capture)
    monkeypatch.setattr(recovery, "_enqueue_attached_capture", fake_enqueue)
    monkeypatch.setattr(recovery, "_prepare_enqueued_showcase", fake_prepare)

    result = asyncio.run(
        recovery.recover_capture(
            database=database,
            publication_id="publication-1",
            receipt_id="receipt-1",
            state_path=tmp_path / "state.json",
        )
    )

    assert result["status"] == "captured"
    assert result["showcase_id"] == "showcase-1"
    assert [item[0] for item in calls] == [
        "preflight_init",
        "preflight_ready",
        "capture",
        "enqueue",
        "prepare",
    ]
    assert calls[0][1]["expected_account"] == "publisher"
    conn = sqlite3.connect(database)
    status, payload = conn.execute(
        "SELECT status, response_json FROM publication_receipts"
    ).fetchone()
    queue_status = conn.execute(
        "SELECT status FROM publication_queue"
    ).fetchone()[0]
    conn.close()
    assert status == "published"
    assert queue_status == "published"
    assert json.loads(payload)["exact_comment_screenshot"] == screenshot


def test_text_fallback_atomically_binds_local_and_master_before_enqueue(
    tmp_path,
    monkeypatch,
):
    database = _database(
        tmp_path,
        queue_comment_id="",
        receipt_comment_id="",
    )
    master_database = tmp_path / "master.sqlite"
    master = master_registry.connect_master(master_database)
    attempt_id = master_registry.register_publication_claim(
        master,
        "main",
        account="publisher",
        post_id=POST_ID,
        publication_id="publication-1",
        source_path=database,
        target_url=TARGET,
        text_hash=TEXT_HASH,
    )
    master_registry.mark_submit_intent(master, "main", attempt_id)
    assert master_registry.register_publication_outcome(
        master,
        "main",
        attempt_id=attempt_id,
        outcome="published",
        receipt_id="receipt-1",
        remote_comment_id="",
        visible=True,
        persisted=True,
        text_hash=TEXT_HASH,
    ) == "confirmed"
    master.commit()
    master.close()

    local = sqlite3.connect(database)
    response = json.loads(
        local.execute(
            "SELECT response_json FROM publication_receipts"
        ).fetchone()[0]
    )
    response["master_attempt_id"] = attempt_id
    local.execute(
        "UPDATE publication_queue SET master_attempt_id=?",
        (attempt_id,),
    )
    local.execute(
        "UPDATE publication_receipts SET master_attempt_id=?, response_json=?",
        (attempt_id, json.dumps(response)),
    )
    local.commit()
    local.close()

    screenshot = _screenshot(tmp_path)
    observed = []

    class FakePreflight:
        def __init__(self, **_kwargs):
            pass

        async def ensure_ready(self):
            return {"observed_account": "publisher"}

    async def fake_capture(*_args, **_kwargs):
        return screenshot

    def fake_enqueue(path, source):
        local_check = sqlite3.connect(path)
        queue_id = local_check.execute(
            "SELECT remote_comment_id FROM publication_queue"
        ).fetchone()[0]
        receipt_id, payload = local_check.execute(
            "SELECT remote_comment_id, response_json FROM publication_receipts"
        ).fetchone()
        local_check.close()
        master_check = sqlite3.connect(master_database)
        attempt_remote_id = master_check.execute(
            "SELECT remote_comment_id FROM tiktok_master_comment_attempts "
            "WHERE attempt_id=?",
            (attempt_id,),
        ).fetchone()[0]
        target_remote_id = master_check.execute(
            "SELECT remote_comment_id FROM tiktok_master_comment_targets "
            "WHERE confirmed_attempt_id=?",
            (attempt_id,),
        ).fetchone()[0]
        master_check.close()
        observed.append(
            (
                source.remote_comment_id,
                queue_id,
                receipt_id,
                json.loads(payload)["remote_comment_id"],
                attempt_remote_id,
                target_remote_id,
            )
        )
        return {"created": True, "showcase_id": "showcase-1"}

    monkeypatch.setattr(recovery, "SocialBrowserPreflight", FakePreflight)
    monkeypatch.setattr(recovery, "_capture_from_verified_profile", fake_capture)
    monkeypatch.setattr(recovery, "_enqueue_attached_capture", fake_enqueue)
    monkeypatch.setattr(
        recovery,
        "_prepare_enqueued_showcase",
        lambda *_args, **_kwargs: {
            "attempted": False,
            "status": "not_configured",
        },
    )

    result = asyncio.run(
        recovery.recover_capture(
            database=database,
            publication_id="publication-1",
            receipt_id="receipt-1",
            master_database=master_database,
        )
    )

    assert result["status"] == "captured"
    assert observed == [("comment-88",) * 6]


def test_failed_recovery_records_retryable_capture_without_republishing(
    tmp_path,
    monkeypatch,
):
    database = _database(tmp_path)

    class FakePreflight:
        def __init__(self, **_kwargs):
            pass

        async def ensure_ready(self):
            raise RuntimeError("browser unavailable")

    monkeypatch.setattr(recovery, "SocialBrowserPreflight", FakePreflight)
    with pytest.raises(RuntimeError, match="browser unavailable"):
        asyncio.run(
            recovery.recover_capture(
                database=database,
                publication_id="publication-1",
                receipt_id="receipt-1",
            )
        )

    conn = sqlite3.connect(database)
    receipt_status, payload = conn.execute(
        "SELECT status, response_json FROM publication_receipts"
    ).fetchone()
    queue_status = conn.execute(
        "SELECT status FROM publication_queue"
    ).fetchone()[0]
    conn.close()
    assert receipt_status == "published"
    assert queue_status == "published"
    capture = json.loads(payload)["comment_showcase_capture"]
    assert capture["status"] == "capture_retryable"
    assert "browser unavailable" in capture["error"]


def test_existing_capture_is_enqueued_idempotently_without_browser(tmp_path, monkeypatch):
    screenshot = _screenshot(tmp_path)
    database = _database(
        tmp_path,
        response_updates={"exact_comment_screenshot": screenshot},
    )

    class ForbiddenPreflight:
        def __init__(self, **_kwargs):
            raise AssertionError("idempotent enqueue should not access TikTok")

    monkeypatch.setattr(recovery, "SocialBrowserPreflight", ForbiddenPreflight)
    monkeypatch.setattr(
        recovery,
        "_enqueue_attached_capture",
        lambda *_args: {"created": False, "showcase_id": "showcase-1"},
    )
    monkeypatch.setattr(
        recovery,
        "_prepare_enqueued_showcase",
        lambda *_args, **_kwargs: {
            "attempted": False,
            "status": "not_configured",
        },
    )
    result = asyncio.run(
        recovery.recover_capture(
            database=database,
            publication_id="publication-1",
            receipt_id="receipt-1",
        )
    )
    assert result["status"] == "already_captured"
    assert result["showcase_created"] is False
    assert result["showcase_preparation"]["status"] == "not_configured"


def test_invalid_unbound_existing_capture_is_recaptured(tmp_path, monkeypatch):
    screenshot = _screenshot(tmp_path)
    Path(screenshot["path"]).unlink()
    database = _database(
        tmp_path,
        response_updates={"exact_comment_screenshot": screenshot},
    )
    replacement_root = tmp_path / "replacement"
    replacement_root.mkdir()
    replacement = _screenshot(replacement_root)
    calls = []

    class FakePreflight:
        def __init__(self, **_kwargs):
            pass

        async def ensure_ready(self):
            return {"observed_account": "publisher"}

    async def fake_capture(*_args, **_kwargs):
        calls.append("capture")
        return replacement

    monkeypatch.setattr(recovery, "SocialBrowserPreflight", FakePreflight)
    monkeypatch.setattr(recovery, "_capture_from_verified_profile", fake_capture)
    monkeypatch.setattr(
        recovery,
        "_enqueue_attached_capture",
        lambda *_args: {"created": True, "showcase_id": "showcase-1"},
    )
    monkeypatch.setattr(
        recovery,
        "_prepare_enqueued_showcase",
        lambda *_args, **_kwargs: {
            "attempted": False,
            "status": "not_configured",
        },
    )

    result = asyncio.run(
        recovery.recover_capture(
            database=database,
            publication_id="publication-1",
            receipt_id="receipt-1",
        )
    )

    assert result["status"] == "captured"
    assert calls == ["capture"]
