from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

import tiktok_publication_adapter as adapter


class _Collection:
    def __init__(self, items):
        self._items = list(items)

    async def count(self):
        return len(self._items)

    def nth(self, index):
        return self._items[index]


class _MissingAncestor:
    async def count(self):
        return 0

    async def is_visible(self):
        return False


class _Comment:
    def __init__(self, text: str, remote_id: str):
        self.text = text
        self.remote_id = remote_id
        self.scrolled = False

    async def is_visible(self):
        return True

    async def inner_text(self):
        return self.text

    async def evaluate(self, _script):
        return f'<div data-comment-id="{self.remote_id}">{self.text}</div>'

    def locator(self, _selector):
        return _MissingAncestor()

    async def scroll_into_view_if_needed(self):
        self.scrolled = True

    async def screenshot(self, *, path):
        # capture_exact_published_comment only needs a valid PNG signature/IHDR
        # header to bind dimensions and bytes; a real browser writes the image.
        header = (
            b"\x89PNG\r\n\x1a\n"
            + b"\x00\x00\x00\rIHDR"
            + (640).to_bytes(4, "big")
            + (320).to_bytes(4, "big")
        )
        Path(path).write_bytes(header)


class _Page:
    def __init__(self, comments):
        self.comments = list(comments)

    def locator(self, selector):
        assert selector == '[data-e2e="comment-level-1"]'
        return _Collection(self.comments)


def test_exact_comment_capture_is_bound_to_remote_id_and_text(tmp_path):
    text = "A useful, AI-assisted observation."
    remote_id = "9988776655"
    comment = _Comment(text, remote_id)
    output = tmp_path / "exact-comment.png"

    result = asyncio.run(
        adapter.capture_exact_published_comment(
            _Page([comment]),
            text,
            output_path=output,
            remote_comment_id=remote_id,
            observed_account="publisher",
            source_post_id="1234567890",
            canonical_url=(
                "https://www.tiktok.com/@creator/video/1234567890"
            ),
        )
    )

    assert comment.scrolled is True
    assert result["path"] == str(output.resolve())
    assert result["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert result["width"] == 640
    assert result["height"] == 320
    assert result["proof"]["unique_match"] is True
    assert result["proof"]["remote_comment_id"] == remote_id
    assert result["proof"]["comment_text_hash"] == hashlib.sha256(
        text.encode("utf-8")
    ).hexdigest()
    assert result["proof"]["media_sha256"] == result["sha256"]


def test_exact_comment_capture_rejects_ambiguous_text(tmp_path):
    text = "Same comment"
    with_error = adapter.capture_exact_published_comment(
        _Page([_Comment(text, "1"), _Comment(text, "2")]),
        text,
        output_path=tmp_path / "ambiguous.png",
        remote_comment_id="missing",
        observed_account="publisher",
        source_post_id="123",
        canonical_url="https://www.tiktok.com/@creator/video/123",
    )

    try:
        asyncio.run(with_error)
    except RuntimeError as exc:
        assert "uniquely resolved" in str(exc)
    else:  # pragma: no cover - makes the safety property explicit.
        raise AssertionError("ambiguous comment capture unexpectedly succeeded")


def test_confirmed_remote_id_never_falls_back_to_a_text_only_match():
    text = "Identical comment text"
    resolved = asyncio.run(
        adapter.exact_published_comment_locator(
            _Page([_Comment(text, "actual-comment-id")]),
            text,
            remote_comment_id="different-confirmed-id",
        )
    )

    assert resolved is None


def test_capture_attachment_does_not_change_confirmed_receipt(tmp_path):
    database = tmp_path / "engage.sqlite"
    conn = sqlite3.connect(database)
    conn.execute(
        """
        CREATE TABLE publication_receipts (
            receipt_id TEXT PRIMARY KEY,
            publication_id TEXT NOT NULL,
            status TEXT NOT NULL,
            response_json TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO publication_receipts (
            receipt_id, publication_id, status, response_json
        ) VALUES ('receipt-1', 'publication-1', 'published', ?)
        """,
        (json.dumps({"submitted_text": "already published"}),),
    )
    conn.commit()
    conn.close()

    screenshot = {
        "path": str(tmp_path / "exact.png"),
        "sha256": "a" * 64,
        "size_bytes": 100,
        "proof": {"schema_version": "tiktok-exact-comment-screenshot-v1"},
    }
    adapter.attach_comment_showcase_capture(
        database,
        publication_id="publication-1",
        receipt_id="receipt-1",
        screenshot=screenshot,
    )

    conn = sqlite3.connect(database)
    status, payload = conn.execute(
        """
        SELECT status, response_json
        FROM publication_receipts
        WHERE receipt_id='receipt-1'
        """
    ).fetchone()
    conn.close()
    stored = json.loads(payload)
    assert status == "published"
    assert stored["exact_comment_screenshot"] == screenshot
    assert stored["comment_showcase_capture"]["status"] == "captured"


def test_capture_attachment_rejects_a_concurrent_different_screenshot(tmp_path):
    database = tmp_path / "engage.sqlite"
    conn = sqlite3.connect(database)
    conn.execute(
        """
        CREATE TABLE publication_receipts (
            receipt_id TEXT PRIMARY KEY,
            publication_id TEXT NOT NULL,
            status TEXT NOT NULL,
            response_json TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO publication_receipts VALUES "
        "('receipt-1', 'publication-1', 'published', '{}')"
    )
    conn.commit()
    conn.close()
    first = {"path": "first.png", "sha256": "a" * 64}
    second = {"path": "second.png", "sha256": "b" * 64}

    adapter.attach_comment_showcase_capture(
        database,
        publication_id="publication-1",
        receipt_id="receipt-1",
        screenshot=first,
    )
    with pytest.raises(RuntimeError, match="different exact-comment"):
        adapter.attach_comment_showcase_capture(
            database,
            publication_id="publication-1",
            receipt_id="receipt-1",
            screenshot=second,
        )

    conn = sqlite3.connect(database)
    payload = json.loads(
        conn.execute(
            "SELECT response_json FROM publication_receipts"
        ).fetchone()[0]
    )
    conn.close()
    assert payload["exact_comment_screenshot"] == first


def test_live_path_quarantines_exact_id_before_reload_and_attaches_after_receipt(
    tmp_path,
    monkeypatch,
):
    events = []
    post_id = "7654321098765432109"
    target = f"https://www.tiktok.com/@creator/video/{post_id}"
    text = "An exact AI-assisted comment."
    publication = {
        "publication_id": "publication-1",
        "target_url": target,
        "content_key": post_id,
        "expected_account": "publisher",
        "final_text": text,
        "final_text_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "public_rating": "",
    }

    class _Control:
        async def focus(self):
            pass

        async def fill(self, value):
            assert value == text

        async def is_disabled(self):
            return False

        async def click(self):
            events.append("submit")

    class _ResponseContext:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        @property
        def value(self):
            async def _value():
                return object()

            return _value()

    class _Keyboard:
        async def insert_text(self, _value):
            pass

        async def type(self, _value):
            pass

        async def press(self, _key):
            pass

    class _LivePage:
        def __init__(self):
            self.url = target
            self.keyboard = _Keyboard()

        def on(self, *_args):
            pass

        async def goto(self, url, **_kwargs):
            self.url = url

        async def screenshot(self, **_kwargs):
            pass

        async def wait_for_timeout(self, _milliseconds):
            pass

        def expect_response(self, *_args, **_kwargs):
            return _ResponseContext()

    class _Recorder:
        def __init__(self):
            self.records = [
                {
                    "capture_id": "capture-1",
                    "request_url": "https://www.tiktok.com/api/comment/publish/",
                    "response": {"comment": {"cid": "9988776655"}},
                }
            ]

        async def on_request(self, *_args):
            pass

        async def on_response(self, *_args):
            pass

        def flush_unanswered(self):
            pass

    controls = [_Control(), _Control()]

    async def fake_first_visible(*_args, **_kwargs):
        return controls.pop(0), "selector"

    async def fake_account(*_args, **_kwargs):
        return "publisher"

    async def fake_capture(*_args, output_path, capture_phase, **_kwargs):
        events.append("capture_pre_reload")
        assert capture_phase == "pre_reload_quarantine"
        assert ".comment_capture_quarantine" in str(output_path)
        return {
            "path": str(output_path),
            "sha256": "a" * 64,
            "size_bytes": 32,
            "proof": {
                "remote_comment_id": "9988776655",
                "media_sha256": "a" * 64,
                "capture_phase": capture_phase,
            },
        }

    async def fake_verify(*_args, **_kwargs):
        events.append("reload_verify")
        return True

    def fake_store_receipt(*_args, **_kwargs):
        events.append("receipt_committed")
        return "receipt-1"

    def fake_promote(screenshot, *, final_path):
        assert "receipt_committed" in events
        events.append("capture_promoted")
        return {**screenshot, "path": str(final_path)}

    def fake_attach(*_args, **_kwargs):
        assert "receipt_committed" in events
        events.append("capture_attached")

    class _ShowcaseConnection:
        def close(self):
            pass

    import tiktok_comment_showcase as showcase_state

    monkeypatch.setattr(adapter, "active_tiktok_account", fake_account)
    monkeypatch.setattr(adapter, "open_comments_panel", lambda *_args: _async({}))
    monkeypatch.setattr(adapter, "inspect_controls", lambda *_args: _async({}))
    monkeypatch.setattr(adapter, "load_approved_publication", lambda *_args: dict(publication))
    monkeypatch.setattr(adapter, "first_visible", fake_first_visible)
    monkeypatch.setattr(adapter, "claim_publication", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(adapter, "mark_publication_submit_intent", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(adapter, "response_success", lambda *_args: True)
    monkeypatch.setattr(adapter, "capture_targets_content", lambda *_args: True)
    monkeypatch.setattr(adapter, "capture_exact_published_comment", fake_capture)
    monkeypatch.setattr(adapter, "verify_persisted_comment", fake_verify)
    monkeypatch.setattr(adapter, "write_capture_files", lambda *_args: tmp_path / "capture.json")
    monkeypatch.setattr(adapter, "store_captures", lambda *_args: None)
    monkeypatch.setattr(adapter, "store_receipt", fake_store_receipt)
    monkeypatch.setattr(adapter, "promote_quarantined_comment_capture", fake_promote)
    monkeypatch.setattr(adapter, "attach_comment_showcase_capture", fake_attach)
    monkeypatch.setattr(showcase_state, "connect_database", lambda *_args: _ShowcaseConnection())
    monkeypatch.setattr(
        showcase_state,
        "enqueue_confirmed_comment",
        lambda *_args, **_kwargs: {"showcase_id": "showcase-1"},
    )
    monkeypatch.setattr(
        adapter,
        "auto_prepare_enqueued_comment_showcase",
        lambda *_args, **_kwargs: ({"showcase_id": "showcase-1"}, {}, ""),
    )

    result = asyncio.run(
        adapter._run_on_page(
            _LivePage(),
            SimpleNamespace(
                execute=True,
                publication_id="publication-1",
                output_dir=str(tmp_path),
                daily_limit=0,
                max_attempts=3,
                master_database=None,
                showcase_auto_prepare_config="",
            ),
            tmp_path / "engage.sqlite",
            dict(publication),
            _Recorder(),
        )
    )

    assert result["status"] == "published"
    assert result["comment_screenshot_captured_before_reload"] is True
    assert events.index("capture_pre_reload") < events.index("reload_verify")
    assert events.index("receipt_committed") < events.index("capture_promoted")
    assert events.index("receipt_committed") < events.index("capture_attached")


async def _async(value):
    return value
