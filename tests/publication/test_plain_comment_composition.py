"""Offline composition failures must stop before any publication submit intent."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import tiktok_publication_adapter as adapter


@pytest.fixture(autouse=True)
def no_challenge_in_plain_composition_fakes(monkeypatch):
    monkeypatch.setattr(adapter, "ensure_no_challenge", AsyncMock())


TEXT = "AI-assisted perspective: 8.4/10. The slow chord changes give learners a useful reference."
TARGET = "https://www.tiktok.com/@creator/video/7654321098765432109"


class Editor:
    def __init__(self, scenario="", *, initial="Retained unapproved text"):
        self.text = initial
        self.scenario = scenario
        self.focus = AsyncMock()
        self.click = AsyncMock()
        self.fill_calls = []

    async def fill(self, value):
        self.fill_calls.append(value)
        if not value and self.scenario == "unclearable":
            raise RuntimeError("clear failed")
        if value and self.scenario in {"partial_fill", "fallback_bad_text", "dirty_partial"}:
            self.text = "Partial unapproved input"
            raise RuntimeError("fill changed text before failure")
        if not value and self.scenario == "dirty_partial" and len(self.fill_calls) > 2:
            raise RuntimeError("cannot clear partial input")
        self.text = value + (" Unapproved extra" if value and self.scenario == "wrong_fill" else "")

    async def evaluate(self, _script):
        return {"text": self.text, "native_labels": [], "links": []}


class Page:
    url = TARGET

    def __init__(self, editor):
        self.editor = editor
        self.selected = False
        self.goto = AsyncMock()
        self.screenshot = AsyncMock()
        self.wait_for_timeout = AsyncMock()
        self.bring_to_front = AsyncMock()
        self.keyboard = SimpleNamespace(
            press=AsyncMock(side_effect=self.press),
            type=AsyncMock(side_effect=self.type_text),
            insert_text=AsyncMock(side_effect=self.insert_text),
        )

    async def press(self, key):
        if key == "ControlOrMeta+A":
            self.selected = True
        elif key == "Backspace":
            if self.editor.scenario in {"unclearable", "dirty_partial"}:
                return
            self.editor.text = "" if self.selected else self.editor.text[:-1]
            self.selected = False
        else:
            raise AssertionError(f"Unexpected keyboard action {key}")

    async def type_text(self, value):
        self.editor.text += value

    async def insert_text(self, value):
        self.editor.text += value
        if self.editor.scenario == "fallback_bad_text":
            self.editor.text += " Unapproved extra"

    def expect_response(self, *_args, **_kwargs):
        page = self

        class Context:
            async def __aenter__(self):
                if page.editor.scenario == "context_mutation":
                    page.editor.text += " Changed after control lookup"
                return self

            async def __aexit__(self, *_args):
                return False

        return Context()


def test_failed_fill_reclears_partial_text_before_insert_fallback():
    editor = Editor("partial_fill")
    page = Page(editor)
    asyncio.run(adapter._compose_plain_comment(page, editor, TEXT))
    assert editor.fill_calls == ["", TEXT, ""]
    page.keyboard.insert_text.assert_awaited_once_with(TEXT)
    assert editor.text == TEXT
    assert all(call.args[0] != "Enter" for call in page.keyboard.press.await_args_list)


def test_editor_click_deadline_stops_before_composition_or_submit(tmp_path, monkeypatch):
    editor = Editor(initial="")
    editor.click.side_effect = adapter.BrowserOperationTimeout("editor_click", 4)
    page = Page(editor)
    publication = {
        "publication_id": "offline", "target_url": TARGET,
        "content_key": "7654321098765432109", "expected_account": "publisher",
        "final_text": TEXT, "decision_json": "{}",
    }
    first = AsyncMock(return_value=(editor, "input"))
    compose = AsyncMock()
    intent = Mock()
    monkeypatch.setattr(adapter, "first_visible", first)
    monkeypatch.setattr(adapter, "active_tiktok_account", AsyncMock(return_value="publisher"))
    monkeypatch.setattr(adapter, "open_comments_panel", AsyncMock(return_value={}))
    monkeypatch.setattr(adapter, "inspect_controls", AsyncMock(return_value={}))
    monkeypatch.setattr(adapter, "load_approved_publication", lambda *_args: dict(publication))
    monkeypatch.setattr(adapter, "claim_publication", Mock())
    monkeypatch.setattr(adapter, "_compose_plain_comment", compose)
    monkeypatch.setattr(adapter, "mark_publication_submit_intent", intent)
    capture = SimpleNamespace(active=False)
    with pytest.raises(adapter.BrowserOperationTimeout, match="editor_click"):
        asyncio.run(adapter._run_on_page_attempt(
            page,
            SimpleNamespace(execute=True, publication_id="offline", output_dir=str(tmp_path), daily_limit=0, max_attempts=3),
            tmp_path / "never-opened.sqlite", publication, None, capture,
        ))
    compose.assert_not_awaited()
    intent.assert_not_called()
    first.assert_awaited_once()
    page.keyboard.type.assert_not_awaited()
    assert capture.active is False


@pytest.mark.parametrize("scenario", [
    "unclearable", "dirty_partial", "wrong_fill", "fallback_bad_text",
    "control_mutation", "context_mutation", "disabled", "missing_submit",
])
def test_unsafe_composition_never_marks_intent_or_submits(tmp_path, monkeypatch, scenario):
    editor = Editor(scenario)
    page = Page(editor)
    submit = SimpleNamespace(is_disabled=AsyncMock(return_value=scenario == "disabled"), click=AsyncMock())
    publication = {
        "publication_id": "offline", "target_url": TARGET,
        "content_key": "7654321098765432109", "expected_account": "publisher",
        "final_text": TEXT, "decision_json": "{}",
    }
    first_calls = 0

    async def first_visible(*_args):
        nonlocal first_calls
        first_calls += 1
        if first_calls == 1:
            return editor, "input"
        if scenario == "control_mutation":
            editor.text += " Changed after composition"
        return (None, "") if scenario == "missing_submit" else (submit, "submit")

    def claim(*_args, **_kwargs):
        # Active-account fallback may focus a temporary tab. Restore the post
        # before either plain or native composition takes keyboard focus.
        page.bring_to_front.assert_awaited_once()

    mark_intent = Mock()
    monkeypatch.setattr(adapter, "first_visible", first_visible)
    monkeypatch.setattr(adapter, "active_tiktok_account", AsyncMock(return_value="publisher"))
    monkeypatch.setattr(adapter, "open_comments_panel", AsyncMock(return_value={}))
    monkeypatch.setattr(adapter, "inspect_controls", AsyncMock(return_value={}))
    monkeypatch.setattr(adapter, "load_approved_publication", lambda *_args: dict(publication))
    monkeypatch.setattr(adapter, "claim_publication", claim)
    monkeypatch.setattr(adapter, "mark_publication_submit_intent", mark_intent)
    capture = SimpleNamespace(active=False)
    with pytest.raises(RuntimeError, match="editor differs|No enabled"):
        asyncio.run(adapter._run_on_page_attempt(
            page,
            SimpleNamespace(execute=True, publication_id="offline", output_dir=str(tmp_path), daily_limit=0, max_attempts=3),
            tmp_path / "never-opened.sqlite", dict(publication), None, capture,
        ))
    mark_intent.assert_not_called()
    submit.click.assert_not_awaited()
    assert capture.active is False
    assert all(call.args[0] != "Enter" for call in page.keyboard.press.await_args_list)
    if scenario == "dirty_partial":
        page.keyboard.insert_text.assert_not_awaited()
