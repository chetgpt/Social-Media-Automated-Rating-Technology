import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import Error as PlaywrightError
from tiktok_scraper.api_integration import TikTokAPIIntegration

URL='https://www.tiktok.com/@creator/video/61'

def item(owner='creator',post='61'):
    return {'id':post,'desc':'Fresh piano tutorial','author':{'uniqueId':owner},'stats':{'playCount':10,'diggCount':2,'commentCount':1}}

def html(value):
    return '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">'+json.dumps({'__DEFAULT_SCOPE__':{'webapp.video-detail':{'itemInfo':{'itemStruct':value}}}})+'</script>'

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
        assert candidate['metadata_method']=='tiktok_browser_rehydration'
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
        goto=AsyncMock(return_value=SimpleNamespace(ok=True)),route=AsyncMock(),close=AsyncMock(),
        content=AsyncMock(return_value=html(item()) if scenario=='exact' else '<html>empty</html>'),
        wait_for_timeout=AsyncMock())
    if scenario=='timeout':tab.goto.side_effect=TimeoutError()
    context=SimpleNamespace(new_page=AsyncMock(return_value=tab))
    page=SimpleNamespace(context=context)
    api=TikTokAPIIntegration(enable_api=False)
    if scenario=='timeout':
        with pytest.raises(TimeoutError):asyncio.run(api._refresh_item_from_browser(page,URL,'61',1))
    else:
        result=asyncio.run(api._refresh_item_from_browser(page,URL,'61',1))
        assert (result is not None)==(scenario=='exact')
    tab.close.assert_awaited_once()
    tab.goto.assert_awaited_once_with(URL,wait_until='domcontentloaded',timeout=1)


@pytest.mark.parametrize('reason', ['page is navigating', 'Execution context was destroyed'])
def test_browser_document_navigation_race_is_retried_within_same_attempt(reason):
    tab=SimpleNamespace(url=URL, goto=AsyncMock(return_value=SimpleNamespace(ok=True)),
        route=AsyncMock(), close=AsyncMock(), wait_for_timeout=AsyncMock(),
        content=AsyncMock(side_effect=[PlaywrightError(reason),html(item())]))
    api=TikTokAPIIntegration(enable_api=False)
    page=SimpleNamespace(context=SimpleNamespace(new_page=AsyncMock(return_value=tab)))
    result=asyncio.run(api._refresh_item_from_browser(page,URL,'61',8000))
    assert result['id']=='61'
    assert tab.content.await_count==2
    tab.goto.assert_awaited_once()
    tab.wait_for_timeout.assert_awaited_once_with(250)
    tab.close.assert_awaited_once()


def test_persistent_document_navigation_race_expires_without_new_navigation(monkeypatch):
    tab=SimpleNamespace(url=URL, goto=AsyncMock(return_value=SimpleNamespace(ok=True)),
        route=AsyncMock(), close=AsyncMock(), wait_for_timeout=AsyncMock(),
        content=AsyncMock(side_effect=PlaywrightError('page is navigating')))
    ticks=iter([0,0.5,1.1])
    monkeypatch.setattr('tiktok_scraper.api_integration.time',SimpleNamespace(monotonic=lambda:next(ticks)))
    api=TikTokAPIIntegration(enable_api=False)
    page=SimpleNamespace(context=SimpleNamespace(new_page=AsyncMock(return_value=tab)))
    result=asyncio.run(api._refresh_item_from_browser(page,URL,'61',1000))
    assert result is None
    tab.goto.assert_awaited_once()
    tab.close.assert_awaited_once()


def test_unrelated_browser_content_error_is_not_retried():
    tab=SimpleNamespace(url=URL, goto=AsyncMock(return_value=SimpleNamespace(ok=True)),
        route=AsyncMock(), close=AsyncMock(), wait_for_timeout=AsyncMock(),
        content=AsyncMock(side_effect=PlaywrightError('Target page, context or browser has been closed')))
    api=TikTokAPIIntegration(enable_api=False)
    page=SimpleNamespace(context=SimpleNamespace(new_page=AsyncMock(return_value=tab)))
    with pytest.raises(PlaywrightError):
        asyncio.run(api._refresh_item_from_browser(page,URL,'61',8000))
    tab.wait_for_timeout.assert_not_awaited()
    tab.close.assert_awaited_once()
