"""Offline regression coverage for current-attempt publication confirmation."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlencode

import pytest

import tiktok_publication_adapter as adapter


@pytest.fixture(autouse=True)
def no_challenge_in_confirmation_fakes(monkeypatch):
    # These fake pages model request correlation; challenge DOM has dedicated tests.
    monkeypatch.setattr(adapter, "ensure_no_challenge", AsyncMock())
    monkeypatch.setattr(adapter, "update_receipt_verification", lambda *_a, **_k: None)


POST_ID = "7654321098765432109"
TARGET = f"https://www.tiktok.com/@creator/video/{POST_ID}"
TEXT = "An exact AI-assisted observation."
ACCOUNT = "publisher"


def response_record():
    return {
        "method": "POST",
        "request_url": "https://www.tiktok.com/api/comment/publish/",
        "request_body_template": {"aweme_id": POST_ID, "text": TEXT},
        "response_status": 200,
        "response": {
            "status_code": 0,
            "comment": {
                "cid": "new-comment",
                "aweme_id": POST_ID,
                "text": TEXT,
                "user": {"unique_id": ACCOUNT},
            },
        },
    }


def confirms(record):
    return adapter.capture_confirms_submission(
        record, content_key=POST_ID, final_text=TEXT, observed_account=ACCOUNT
    )


def test_correlated_success_requires_target_exact_text_and_comment_identity():
    assert confirms(response_record())
    record = response_record()
    record["request_url"] += "?" + urlencode(record.pop("request_body_template"))
    assert confirms(record)
    # The pre-submit browser/account check binds the request when TikTok omits
    # optional author and echoed text fields from its successful response.
    record["response"]["comment"] = {"cid": "new-comment"}
    assert confirms(record)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda r: r.update(method="GET"),
        lambda r: r.update(response_status=500),
        lambda r: r.update(request_url="https://example.com/api/comment/publish/"),
        lambda r: r.update(request_url="https://secret@www.tiktok.com/api/comment/publish/"),
        lambda r: r.update(request_url="https://www.tiktok.com:443/api/comment/publish/"),
        lambda r: r["request_body_template"].update(text="Unapproved text"),
        lambda r: r["request_body_template"].pop("text"),
        lambda r: r["request_body_template"].update(aweme_id="different-post"),
        lambda r: r["request_body_template"].update(video_id="different-post"),
        lambda r: r.update(request_url=r["request_url"] + "?text=conflicting"),
        lambda r: r["response"].update(status_code=1),
        lambda r: r["response"]["comment"].pop("cid"),
        lambda r: r["response"]["comment"].update(cid={"untrusted": "shape"}),
        lambda r: r["response"]["comment"].update(cid=True),
        lambda r: r["response"]["comment"].update(cid="   "),
        lambda r: r["response"]["comment"].update(cid="comment id"),
        lambda r: r["response"]["comment"].update(cid="comment\tid"),
        lambda r: r["response"]["comment"].update(cid="comment\x00id"),
        lambda r: r["response"]["comment"].update(cid="comment\x85id"),
        lambda r: r["response"].update(comment={"reply": {"cid": "old-reply"}}),
        lambda r: r["response"]["comment"].update(comment_id="different-comment"),
        lambda r: r["response"]["comment"].update(text="Other text"),
        lambda r: r["response"]["comment"].update(aweme_id="other-post"),
        lambda r: r["response"]["comment"]["user"].update(unique_id="other-account"),
        lambda r: r["response"]["comment"].update(author={"unique_id": "other-account"}),
    ],
)
def test_unbound_or_conflicting_response_cannot_confirm(mutation):
    record = response_record()
    mutation(record)
    assert not confirms(record)


class Comment:
    def __init__(self, text=TEXT, remote_id="older-comment"):
        self.text = text
        self.remote_id = remote_id

    async def is_visible(self):
        return True

    async def inner_text(self):
        return self.text

    async def evaluate(self, _script):
        return f'<div data-comment-id="{self.remote_id}">{self.text}</div>'

    def locator(self, _selector):
        return SimpleNamespace(count=AsyncMock(return_value=0))


class CommentPage:
    def __init__(self, comments, *, url=TARGET):
        self.comments = comments
        self.url = url
        self.reload = AsyncMock()

    def locator(self, _selector):
        return SimpleNamespace(
            count=AsyncMock(return_value=len(self.comments)),
            nth=lambda index: self.comments[index],
        )


@pytest.mark.parametrize(
    "remote_id,account,comment,url,expected",
    [
        ("", ACCOUNT, Comment(), TARGET, False),
        ("new-comment", "", Comment(), TARGET, False),
        ("new-comment", ACCOUNT, Comment(), TARGET, False),
        ("new-comment", ACCOUNT, Comment(remote_id="new-comment"), TARGET, True),
        ("new-comment", ACCOUNT, Comment(text="other", remote_id="new-comment"), TARGET, False),
        ("new-comment", ACCOUNT, Comment(remote_id="new-comment"), TARGET + "0", False),
        ("new-comment", "different-account", Comment(remote_id="new-comment"), TARGET, False),
    ],
)
def test_persisted_confirmation_never_uses_text_only(
    monkeypatch, remote_id, account, comment, url, expected
):
    monkeypatch.setattr(adapter, "wait_for_first_visible", AsyncMock())
    monkeypatch.setattr(adapter, "open_comments_panel", AsyncMock())
    monkeypatch.setattr(adapter, "active_tiktok_account", AsyncMock(return_value=ACCOUNT))
    actual_locator = adapter.exact_published_comment_locator

    async def no_wait_locator(*args, **kwargs):
        return await actual_locator(*args, **{**kwargs, "timeout_ms": 0})

    monkeypatch.setattr(adapter, "exact_published_comment_locator", no_wait_locator)
    page = CommentPage([comment], url=url)
    result = asyncio.run(
        adapter.verify_persisted_comment(
            page, TEXT, POST_ID, remote_comment_id=remote_id, observed_account=account
        )
    )
    assert result is expected
    if not remote_id or not account:
        page.reload.assert_not_called()


@pytest.mark.parametrize(
    "scenario,expected",
    [
        ("current", "published"),
        ("request_before_submit", "uncertain"),
        ("no_response", "uncertain"),
        ("wrong_text", "uncertain"),
        ("wrong_author", "uncertain"),
        ("missing_comment_id", "uncertain"),
        ("never_finishing_body", "uncertain"),
        ("never_finishing_headers", "uncertain"),
        ("auxiliary_error", "published"),
        ("auxiliary_cancel", "published"),
        ("captcha_after_confirmation", "published"),
    ],
)
def test_live_orchestration_requires_own_correlated_creation_without_real_io(
    tmp_path, monkeypatch, scenario, expected
):
    record = response_record()
    if scenario == "wrong_text":
        record["request_body_template"]["text"] = "not the approved response"
    elif scenario == "wrong_author":
        record["response"]["comment"]["user"]["unique_id"] = "other-account"
    elif scenario == "missing_comment_id":
        record["response"]["comment"].pop("cid")
    request = SimpleNamespace(
        method="POST", url=record["request_url"],
        post_data=urlencode(record["request_body_template"]),
        all_headers=AsyncMock(return_value={"content-type": "application/x-www-form-urlencoded"}),
    )
    response = SimpleNamespace(
        request=request, status=200, headers={"content-type": "application/json"},
        json=AsyncMock(return_value=deepcopy(record["response"])),
    )
    monkeypatch.setattr(adapter, "PUBLICATION_CAPTURE_SETTLE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(adapter, "PUBLICATION_CAPTURE_CANCEL_TIMEOUT_SECONDS", 0.1)

    async def never_finishes():
        await asyncio.Event().wait()

    if scenario == "never_finishing_body":
        response.json = AsyncMock(side_effect=never_finishes)
    elif scenario == "never_finishing_headers":
        request.all_headers = AsyncMock(side_effect=never_finishes)
    captures = []
    original_capture = adapter._PublicationAttemptCapture

    def make_capture(*args):
        instance = original_capture(*args)
        captures.append(instance)
        return instance

    monkeypatch.setattr(adapter, "_PublicationAttemptCapture", make_capture)
    listeners = {}

    class ResponseContext:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            if scenario == "no_response":
                raise adapter.PlaywrightTimeoutError("offline simulated timeout")
            return False

        @property
        def value(self):
            return AsyncMock(return_value=response)()

    class Page:
        url = TARGET
        keyboard = SimpleNamespace(
            insert_text=AsyncMock(), type=AsyncMock(), press=AsyncMock()
        )
        screenshot = AsyncMock()
        wait_for_timeout = AsyncMock()
        bring_to_front = AsyncMock()

        def on(self, event, callback):
            listeners[event] = callback

        def remove_listener(self, event, callback):
            assert listeners[event] == callback
            del listeners[event]

        async def goto(self, *_args, **_kwargs):
            if scenario == "request_before_submit":
                listeners["request"](request)

        def expect_response(self, *_args, **_kwargs):
            return ResponseContext()

    async def click_submit(**_kwargs):
        if scenario != "request_before_submit":
            listeners["request"](request)
        if scenario != "no_response":
            listeners["response"](response)

    editor_text = ""

    async def fill_editor(value):
        nonlocal editor_text
        editor_text = value

    async def read_editor(_script):
        return {"text": editor_text, "native_labels": [], "links": []}

    input_control = SimpleNamespace(
        focus=AsyncMock(), fill=fill_editor, click=AsyncMock(), evaluate=read_editor
    )
    submit_control = SimpleNamespace(is_disabled=AsyncMock(return_value=False), click=click_submit)
    monkeypatch.setattr(adapter, "first_visible", AsyncMock(side_effect=[(input_control, "input"), (submit_control, "submit")]))
    publication = {
        "publication_id": "offline-test", "target_url": TARGET,
        "content_key": POST_ID, "expected_account": ACCOUNT,
        "final_text": TEXT, "final_text_hash": "offline-hash", "public_rating": "",
    }
    monkeypatch.setattr(adapter, "load_approved_publication", lambda *_args: dict(publication))
    monkeypatch.setattr(adapter, "active_tiktok_account", AsyncMock(return_value=ACCOUNT))
    monkeypatch.setattr(adapter, "open_comments_panel", AsyncMock(return_value={}))
    monkeypatch.setattr(adapter, "inspect_controls", AsyncMock(return_value={}))
    monkeypatch.setattr(adapter, "claim_publication", lambda *_args, **_kwargs: None)

    def mark_intent(_database, item, **_kwargs):
        item["_submit_intent"] = True

    monkeypatch.setattr(adapter, "mark_publication_submit_intent", mark_intent)
    # Even a falsely positive persisted-text signal cannot independently
    # authorize a confirmed receipt when current-attempt API proof is absent.
    monkeypatch.setattr(adapter, "verify_persisted_comment", AsyncMock(return_value=True))
    monkeypatch.setattr(adapter, "wait_for_published_comment", AsyncMock(return_value=True))
    monkeypatch.setattr(adapter, "exact_published_comment_locator", AsyncMock(return_value=None))
    monkeypatch.setattr(adapter, "capture_exact_published_comment", AsyncMock(side_effect=RuntimeError("offline capture unavailable")))
    monkeypatch.setattr(adapter, "attach_comment_showcase_capture", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(adapter, "write_capture_files", lambda *_args: tmp_path / "not-written.json")
    monkeypatch.setattr(adapter, "store_captures", lambda *_args: None)
    receipts = []

    def store_receipt(_database, item, **kwargs):
        assert item["_submit_intent"] is True
        receipts.append(kwargs)
        return "offline-receipt"

    monkeypatch.setattr(adapter, "store_receipt", store_receipt)
    if scenario in {"auxiliary_error", "auxiliary_cancel"}:
        async def interrupted_verification(*_a, **_k):
            assert len(receipts) == 1 and receipts[0]["outcome"] == "published"
            if scenario == "auxiliary_cancel":
                raise asyncio.CancelledError()
            raise RuntimeError("auxiliary failure")
        monkeypatch.setattr(adapter, "verify_persisted_comment", interrupted_verification)
    elif scenario == "captcha_after_confirmation":
        async def challenge_after_submit(_page, phase):
            if phase == "after_submit":
                assert len(receipts) == 1 and receipts[0]["outcome"] == "published"
                raise adapter.HumanVerificationRequired(phase)
        monkeypatch.setattr(adapter, "ensure_no_challenge", challenge_after_submit)
    recorder = adapter.PublicationCapture("tiktok", TARGET, "offline")
    async def exercise():
        result = await adapter._run_on_page(
            Page(),
            SimpleNamespace(execute=True, publication_id="offline-test", output_dir=str(tmp_path), daily_limit=0, max_attempts=3, master_database=None),
            tmp_path / "not-created.sqlite", dict(publication), recorder,
        )
        # Check before asyncio.run's own shutdown can hide leaked tasks.
        assert all(
            task.done()
            for capture in captures
            for task in [*capture.request_tasks.values(), *capture.response_tasks.values()]
        )
        return result

    result = asyncio.run(exercise())
    assert result["status"] == expected
    assert receipts[0]["outcome"] == expected
    assert receipts[0]["success"] is (expected == "published")
    if scenario == "captcha_after_confirmation":
        assert result["human_verification"]["tab_preserved"] is True
        assert result["human_verification"]["submit_intent_recorded"] is True
    if scenario.startswith("never_finishing"):
        assert result["capture_settlement_complete"] is False
        assert any(
            task.cancelled()
            for capture in captures
            for task in [*capture.request_tasks.values(), *capture.response_tasks.values()]
        )
    if scenario == "request_before_submit":
        assert recorder.records == []
    assert listeners == {}
    assert not list(tmp_path.iterdir())


def test_slow_response_event_and_explicit_result_share_one_capture_task():
    async def exercise():
        response_started = asyncio.Event()
        release_response = asyncio.Event()
        record = response_record()
        request = SimpleNamespace(
            method="POST", url=record["request_url"],
            post_data=urlencode(record["request_body_template"]),
            all_headers=AsyncMock(return_value={"content-type": "application/x-www-form-urlencoded"}),
        )

        async def slow_json():
            response_started.set()
            await release_response.wait()
            return record["response"]

        response = SimpleNamespace(
            request=request, status=200, headers={"content-type": "application/json"},
            json=AsyncMock(side_effect=slow_json),
        )
        foreign_request_listener = lambda _request: None
        foreign_response_listener = lambda _response: None
        listeners = {
            "request": [foreign_request_listener],
            "response": [foreign_response_listener],
        }
        page = SimpleNamespace(
            on=lambda event, callback: listeners[event].append(callback),
            remove_listener=lambda event, callback: listeners[event].remove(callback),
        )
        recorder = adapter.PublicationCapture("tiktok", TARGET, "offline")
        capture = adapter._PublicationAttemptCapture(page, recorder)
        capture.attach()
        capture.active = True
        listeners["request"][-1](request)
        event_task = listeners["response"][-1](response)
        await response_started.wait()
        explicit_task = capture.capture_response(response)
        assert explicit_task is event_task
        finish_task = asyncio.create_task(capture.finish())
        await asyncio.sleep(0)
        assert not finish_task.done()
        assert listeners == {
            "request": [foreign_request_listener],
            "response": [foreign_response_listener],
        }
        release_response.set()
        await finish_task
        recorder.flush_unanswered()
        assert len(recorder.records) == 1
        assert confirms(recorder.records[0])
        response.json.assert_awaited_once()
        assert recorder.pending == {}

    asyncio.run(exercise())


def test_page_error_removes_only_attempt_listeners(tmp_path):
    foreign_listener = lambda _event: None
    listeners = {"request": [foreign_listener], "response": [foreign_listener]}
    page = SimpleNamespace(
        on=lambda event, callback: listeners[event].append(callback),
        remove_listener=lambda event, callback: listeners[event].remove(callback),
        goto=AsyncMock(side_effect=RuntimeError("offline navigation failure")),
    )
    recorder = adapter.PublicationCapture("tiktok", TARGET, "offline")
    with pytest.raises(RuntimeError, match="offline navigation failure"):
        asyncio.run(
            adapter._run_on_page(
                page, SimpleNamespace(), tmp_path / "not-created.sqlite",
                {"target_url": TARGET}, recorder,
            )
        )
    assert listeners == {
        "request": [foreign_listener], "response": [foreign_listener]
    }
    assert not list(tmp_path.iterdir())
