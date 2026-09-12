import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import social_browser
import tiktok_publication_adapter as adapter
from test_plain_comment_composition import Editor, Page, TARGET, TEXT


def test_challenge_before_submit_never_records_intent_or_clicks(tmp_path, monkeypatch):
    page = Page(Editor(initial=""))
    submit = SimpleNamespace(is_disabled=AsyncMock(return_value=False), click=AsyncMock())
    publication = dict(publication_id="pub", target_url=TARGET, content_key="7654321098765432109",
                       expected_account="publisher", final_text=TEXT, decision_json="{}")
    async def check(_page, phase):
        if phase == "before_submit":
            raise adapter.HumanVerificationRequired(phase)
    monkeypatch.setattr(adapter, "ensure_no_challenge", check)
    monkeypatch.setattr(adapter, "first_visible", AsyncMock(side_effect=[(page.editor, "input"), (submit, "submit")]))
    monkeypatch.setattr(adapter, "active_tiktok_account", AsyncMock(return_value="publisher"))
    monkeypatch.setattr(adapter, "open_comments_panel", AsyncMock(return_value={}))
    monkeypatch.setattr(adapter, "inspect_controls", AsyncMock(return_value={}))
    monkeypatch.setattr(adapter, "load_approved_publication", lambda *_: dict(publication))
    monkeypatch.setattr(adapter, "claim_publication", Mock())
    mark = Mock()
    monkeypatch.setattr(adapter, "mark_publication_submit_intent", mark)
    capture = SimpleNamespace(active=False)
    with pytest.raises(adapter.HumanVerificationRequired):
        asyncio.run(adapter._run_on_page_attempt(page, SimpleNamespace(execute=True, publication_id="pub",
            output_dir=str(tmp_path), daily_limit=0, max_attempts=3), tmp_path/"absent.sqlite", publication, None, capture))
    mark.assert_not_called()
    submit.click.assert_not_awaited()
    assert not capture.active


@pytest.mark.parametrize("submitted", [False, True])
def test_run_preserves_challenge_tab_and_records_correct_attempt_outcome(tmp_path, monkeypatch, submitted):
    publication = dict(publication_id="pub", target_url=TARGET, content_key="7654321098765432109",
                       expected_account="publisher", project="test")
    page = SimpleNamespace(is_closed=lambda: False, close=AsyncMock(), screenshot=AsyncMock())
    context = SimpleNamespace(new_page=AsyncMock(return_value=page))
    browser = SimpleNamespace(contexts=[context])
    manager = SimpleNamespace(start=AsyncMock(return_value=SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=AsyncMock(return_value=browser)))),
                              __aexit__=AsyncMock())
    monkeypatch.setattr(adapter, "async_playwright", lambda: manager)
    monkeypatch.setattr(adapter, "SocialBrowserPreflight", lambda **_: SimpleNamespace(ensure_ready=AsyncMock()))
    monkeypatch.setattr(adapter, "load_approved_publication", lambda *_: publication)
    monkeypatch.setattr(adapter, "bind_engage_master_database", lambda *_: tmp_path / "master.sqlite")
    monkeypatch.setattr(social_browser, "load_engage_profile7_designation", lambda *_: {})
    monkeypatch.setattr(social_browser, "load_verified_profile7_state", lambda *_: {"cdp_url": "ws://offline"})
    monkeypatch.setattr(social_browser, "verified_profile_context", AsyncMock(return_value=(context, {})))
    monkeypatch.setattr(social_browser, "platform_authentication", AsyncMock(return_value={"authenticated": True}))
    monkeypatch.setattr(adapter, "publication_status", lambda *_: "publishing")
    store = Mock(return_value="receipt")
    monkeypatch.setattr(adapter, "store_receipt", store)
    async def challenge(_page, _args, _database, item, _recorder):
        item.update(_claimed=True, _submit_intent=submitted)
        raise adapter.HumanVerificationRequired("after_submit" if submitted else "composition")
    monkeypatch.setattr(adapter, "_run_on_page", challenge)
    result = asyncio.run(adapter.run(SimpleNamespace(database=str(tmp_path/"absent.sqlite"), publication_id="pub",
        execute=True, output_dir=str(tmp_path), social_browser_state=str(tmp_path/"state.json"))))
    assert result["status"] == "human_verification_required"
    assert result["submit_intent_recorded"] is submitted
    assert store.call_args.kwargs["outcome"] == ("uncertain" if submitted else "failed")
    page.close.assert_not_awaited()
    manager.__aexit__.assert_awaited_once()
