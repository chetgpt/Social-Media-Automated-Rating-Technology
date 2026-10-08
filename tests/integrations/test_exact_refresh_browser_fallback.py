import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from playwright.async_api import Error as PlaywrightError
from engage_browser_guard import HumanVerificationRequired
from engage_browser_guard import BrowserOperationTimeout
from tiktok_scraper.api_integration import TikTokAPIIntegration

URL='https://www.tiktok.com/@creator/video/61'


@pytest.fixture(autouse=True)
def no_fixture_challenge(monkeypatch):
    guard = AsyncMock()
    monkeypatch.setattr('tiktok_scraper.api_integration.ensure_no_challenge', guard)
    return guard

def item(owner='creator',post='61'):
    return {'id':post,'desc':'Fresh piano tutorial','author':{'uniqueId':owner},'stats':{'playCount':10,'diggCount':2,'commentCount':1}}

def html(value):
    return '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">'+json.dumps({'__DEFAULT_SCOPE__':{'webapp.video-detail':{'itemInfo':{'itemStruct':value}}}})+'</script>'

def browser_page(tab):
    tab.media_session = SimpleNamespace(on=Mock(), remove_listener=Mock(), send=AsyncMock(), detach=AsyncMock())
    return SimpleNamespace(context=SimpleNamespace(new_page=AsyncMock(return_value=tab),
        new_cdp_session=AsyncMock(return_value=tab.media_session)))

@pytest.mark.parametrize('owner,accepted',[('creator',True),('wrong',False)])
def test_successful_http_shell_uses_exact_browser_item_and_checks_owner(owner,accepted):
    api=TikTokAPIIntegration(enable_api=False)
    api._refresh_item_from_browser=AsyncMock(return_value=item(owner))
    response=SimpleNamespace(ok=True,text=AsyncMock(return_value='<html>shell</html>'))
    page=SimpleNamespace(request=SimpleNamespace(get=AsyncMock(return_value=response)))
    candidate={'id':'61','url':URL,'username':'creator','caption':'stale','view_count':999}
    stats=asyncio.run(api.refresh_video_candidates_from_html(page,[candidate]))
    assert candidate['metadata_refresh_ok'] is accepted
    assert stats['hydrated']==int(accepted)
    api._refresh_item_from_browser.assert_awaited_once()
    if accepted:
        assert candidate['caption']=='Fresh piano tutorial' and candidate['view_count']==10
        assert candidate['metadata_method']=='tiktok_browser_post_metadata'
    else:assert 'caption' not in candidate


def test_http_refusal_does_not_attempt_browser_fallback():
    api=TikTokAPIIntegration(enable_api=False)
    api._refresh_item_from_browser=AsyncMock()
    page=SimpleNamespace(request=SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(ok=False))))
    candidate={'id':'61','url':URL,'caption':'stale'}
    stats=asyncio.run(api.refresh_video_candidates_from_html(page,[candidate]))
    assert stats['failed']==1 and not candidate['metadata_refresh_ok']
    api._refresh_item_from_browser.assert_not_awaited()


def test_direct_transport_failure_records_only_safe_phase_and_code(caplog):
    api=TikTokAPIIntegration(enable_api=False)
    request=SimpleNamespace(get=AsyncMock(side_effect=RuntimeError(
        'net::ERR_CONNECTION_RESET https://private.test/?token=secret Authorization: private')))
    candidate={'id':'61','url':URL,'caption':'stale'}
    stats=asyncio.run(api.refresh_video_candidates_from_html(SimpleNamespace(request=request),[candidate]))
    assert stats['failed']==1 and candidate['metadata_refresh_ok'] is False
    assert 'direct_request/net::ERR_CONNECTION_RESET' in caplog.text
    assert 'private' not in caplog.text and 'secret' not in caplog.text


@pytest.mark.parametrize('scenario',['exact','redirect','timeout','missing'])
def test_browser_fallback_is_exact_bounded_and_closes_only_own_tab(scenario):
    tab=SimpleNamespace(url=URL if scenario!='redirect' else 'https://www.tiktok.com/@other/video/99',
        goto=AsyncMock(return_value=SimpleNamespace(ok=True)),route=AsyncMock(),close=AsyncMock(),on=Mock(),remove_listener=Mock(),
        content=AsyncMock(return_value=html(item()) if scenario=='exact' else '<html>empty</html>'),
        wait_for_timeout=AsyncMock())
    if scenario=='timeout':tab.goto.side_effect=TimeoutError()
    page=browser_page(tab)
    api=TikTokAPIIntegration(enable_api=False)
    if scenario=='timeout':
        with pytest.raises(TimeoutError):asyncio.run(api._refresh_item_from_browser(page,URL,'61',100))
    else:
        result=asyncio.run(api._refresh_item_from_browser(page,URL,'61',100))
        assert (result is not None)==(scenario=='exact')
    tab.close.assert_awaited_once()
    tab.goto.assert_awaited_once_with(URL,wait_until='commit',timeout=100)
    tab.route.assert_not_awaited()
    tab.media_session.send.assert_any_await('Fetch.enable', {
        'patterns': [{'resourceType': 'Media', 'requestStage': 'Request'}]})


@pytest.mark.parametrize('reason', ['page is navigating', 'Execution context was destroyed'])
def test_browser_document_navigation_race_is_retried_within_same_attempt(reason):
    tab=SimpleNamespace(url=URL, goto=AsyncMock(return_value=SimpleNamespace(ok=True)),
        route=AsyncMock(), close=AsyncMock(), on=Mock(), remove_listener=Mock(), wait_for_timeout=AsyncMock(),
        content=AsyncMock(side_effect=[PlaywrightError(reason),html(item())]))
    api=TikTokAPIIntegration(enable_api=False)
    page=browser_page(tab)
    result=asyncio.run(api._refresh_item_from_browser(page,URL,'61',8000))
    assert result['id']=='61'
    assert tab.content.await_count==2
    tab.goto.assert_awaited_once()
    tab.close.assert_awaited_once()


def test_persistent_document_navigation_race_expires_without_new_navigation(monkeypatch):
    tab=SimpleNamespace(url=URL, goto=AsyncMock(return_value=SimpleNamespace(ok=True)),
        route=AsyncMock(), close=AsyncMock(), on=Mock(), remove_listener=Mock(), wait_for_timeout=AsyncMock(),
        content=AsyncMock(side_effect=PlaywrightError('page is navigating')))
    api=TikTokAPIIntegration(enable_api=False)
    page=browser_page(tab)
    result=asyncio.run(api._refresh_item_from_browser(page,URL,'61',1000))
    assert result is None
    tab.goto.assert_awaited_once()
    tab.close.assert_awaited_once()


def test_unrelated_browser_content_error_is_not_retried():
    tab=SimpleNamespace(url=URL, goto=AsyncMock(return_value=SimpleNamespace(ok=True)),
        route=AsyncMock(), close=AsyncMock(), on=Mock(), remove_listener=Mock(), wait_for_timeout=AsyncMock(),
        content=AsyncMock(side_effect=PlaywrightError('Target page, context or browser has been closed')))
    api=TikTokAPIIntegration(enable_api=False)
    page=browser_page(tab)
    with pytest.raises(PlaywrightError):
        asyncio.run(api._refresh_item_from_browser(page,URL,'61',8000))
    tab.wait_for_timeout.assert_not_awaited()
    tab.close.assert_awaited_once()


@pytest.mark.parametrize('case', ['exact', 'wrong_id', 'wrong_owner', 'wrong_host', 'wrong_path', 'http_refused', 'api_refused'])
def test_photo_shell_uses_only_successful_exact_observed_item(case, monkeypatch, caplog):
    photo_url = URL.replace('/video/', '/photo/')
    photo_item = item(owner='other' if case == 'wrong_owner' else 'creator',
                      post='99' if case == 'wrong_id' else '61')
    photo_item['imagePost'] = {'images': [{}, {}]}
    response = SimpleNamespace(
        url='https://www.tiktok.com/api/item/detail/',
        status=403 if case == 'http_refused' else 200,
        json=AsyncMock(return_value={
            'status_code': 102 if case == 'api_refused' else 0,
            'itemInfo': {'itemStruct': photo_item},
        }),
    )
    if case == 'wrong_host':
        response.url = 'https://example.invalid/api/item/detail/'
    elif case == 'wrong_path':
        response.url = 'https://www.tiktok.com/api/recommend/item_list/'
    tab = SimpleNamespace(url=photo_url, route=AsyncMock(), close=AsyncMock(),
        on=Mock(), remove_listener=Mock(), content=AsyncMock(return_value='<html>settled shell</html>'),
        wait_for_timeout=AsyncMock())

    async def navigate(*args, **kwargs):
        tab.on.assert_called_once()
        tab.on.call_args.args[1](response)
        await asyncio.sleep(0)
        return SimpleNamespace(ok=True)

    tab.goto = AsyncMock(side_effect=navigate)
    page = browser_page(tab)
    api = TikTokAPIIntegration(enable_api=False)
    result = asyncio.run(api._refresh_item_from_browser(page, photo_url, '61', 1000))
    assert (result is not None) is (case == 'exact')
    if result:
        assert result['imagePost'] == {'images': [{}, {}]}
    else:
        assert 'browser_item_unavailable' in caplog.text
        assert 'browser_document_unsettled' not in caplog.text
    if case in {'wrong_host', 'wrong_path', 'http_refused'}:
        response.json.assert_not_awaited()
    tab.goto.assert_awaited_once()
    tab.remove_listener.assert_called_once_with('response', tab.on.call_args.args[1])
    tab.close.assert_awaited_once()
    tab.content.assert_not_awaited()


def test_pending_observed_response_is_cancelled_on_expiry(monkeypatch):
    cancelled = []

    async def pending_json():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    response = SimpleNamespace(url='https://www.tiktok.com/api/item/detail/', status=200, json=pending_json)
    tab = SimpleNamespace(url=URL, route=AsyncMock(), close=AsyncMock(), on=Mock(), remove_listener=Mock(),
        content=AsyncMock(return_value='<html>shell</html>'), wait_for_timeout=AsyncMock())

    async def navigate(*args, **kwargs):
        tab.on.call_args.args[1](response)
        await asyncio.sleep(0)
        return SimpleNamespace(ok=True)

    tab.goto = AsyncMock(side_effect=navigate)
    api = TikTokAPIIntegration(enable_api=False)
    page = browser_page(tab)
    assert asyncio.run(api._refresh_item_from_browser(page, URL, '61', 1000)) is None
    assert cancelled == [True]
    tab.remove_listener.assert_called_once()
    tab.close.assert_awaited_once()


def test_challenge_observation_timeout_fails_closed_without_retry(monkeypatch):
    guard = AsyncMock(side_effect=BrowserOperationTimeout('challenge_observation', 10))
    monkeypatch.setattr('tiktok_scraper.api_integration.ensure_no_challenge', guard)
    tab = SimpleNamespace(url=URL, goto=AsyncMock(return_value=SimpleNamespace(ok=True)),
        route=AsyncMock(), close=AsyncMock(), on=Mock(), remove_listener=Mock(),
        content=AsyncMock(return_value=html(item())), wait_for_timeout=AsyncMock())
    api = TikTokAPIIntegration(enable_api=False)
    page = browser_page(tab)
    with pytest.raises(BrowserOperationTimeout):
        asyncio.run(api._refresh_item_from_browser(page, URL, '61', 30000))
    guard.assert_awaited_once_with(tab, phase='exact_metadata_refresh', timeout=10)
    tab.wait_for_timeout.assert_not_awaited()
    tab.close.assert_awaited_once()


def test_challenge_propagates_through_refresh_and_preserves_owned_tab(no_fixture_challenge):
    no_fixture_challenge.side_effect = HumanVerificationRequired('exact_metadata_refresh')
    tab = SimpleNamespace(url=URL, goto=AsyncMock(return_value=SimpleNamespace(ok=True)),
        route=AsyncMock(), close=AsyncMock(), on=Mock(), remove_listener=Mock(),
        content=AsyncMock(return_value=html(item())), wait_for_timeout=AsyncMock())
    response = SimpleNamespace(ok=True, text=AsyncMock(return_value='<html>shell</html>'))
    page = browser_page(tab)
    page.request = SimpleNamespace(get=AsyncMock(return_value=response))
    api = TikTokAPIIntegration(enable_api=False)
    with pytest.raises(HumanVerificationRequired):
        asyncio.run(api.refresh_video_candidates_from_html(page, [{'id': '61', 'url': URL}]))
    tab.remove_listener.assert_called_once()
    tab.close.assert_not_awaited()
