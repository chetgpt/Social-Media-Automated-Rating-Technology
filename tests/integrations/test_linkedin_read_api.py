"""Bounded website-read fixtures; no browser, credentials or network calls."""
import asyncio
from copy import deepcopy
import json

import pytest

from social_engage.adapters import AdapterError
from social_engage.linkedin_data import FlightData
from social_engage.linkedin_read_api import (
    PAGINATION_PATH, PAGINATION_TYPE, comment_pagers, fetch_comments_page,
    observed_context, pagination_body,
)


POST_ID = 'urn:li:activity:12345'
POST_URL = f'https://www.linkedin.com/feed/update/{POST_ID}/'


def pager():
    return {
        '$type': PAGINATION_TYPE, 'pagerId': 'observed-comments-pager',
        'requestedArguments': {'payload': {
            'pageToken': 'opaque-observed-token', 'pageSize': 10, 'subsequentPageSize': 10,
            'threadUrn': {'threadUrnActivityThreadUrn': {'activityUrn': {'activityId': '12345'}}},
            'updateKey': {'items': [
                {'feedUpdateUrn': {'updateUrnActivityUrn': {'activityUrn': {'activityId': '12345'}}}},
            ]},
        }},
    }


def flight(value):
    return FlightData('0:' + json.dumps(value) + '\n')


def context():
    return {'post_id': POST_ID, 'headers': {'csrf-token': 'fixture-token'},
            'client': {'screenId': 'fixture-screen', 'states': {'fixture': []}}}


def test_comment_pager_requires_exact_thread_update_key_and_known_read_schema():
    valid = pager()
    assert comment_pagers(flight(valid), POST_ID) == [valid]
    invalid = []
    wrong_thread = pager()
    wrong_thread['requestedArguments']['payload']['threadUrn']['threadUrnActivityThreadUrn']['activityUrn']['activityId'] = '99999'
    invalid.append(wrong_thread)
    wrong_update = pager()
    wrong_update['requestedArguments']['payload']['updateKey']['items'][0]['feedUpdateUrn']['updateUrnActivityUrn']['activityUrn']['activityId'] = '99999'
    invalid.append(wrong_update)
    empty_update = pager()
    empty_update['requestedArguments']['payload']['updateKey']['items'] = []
    invalid.append(empty_update)
    unknown_action = pager()
    unknown_action['$type'] = 'unverified.action'
    invalid.append(unknown_action)
    unknown_field = pager()
    unknown_field['action'] = 'unverified-action'
    invalid.append(unknown_field)
    unknown_payload = pager()
    unknown_payload['requestedArguments']['payload']['unverifiedAction'] = 'unverified-action'
    invalid.append(unknown_payload)
    unknown_argument = pager()
    unknown_argument['requestedArguments']['unverifiedAction'] = 'unverified-action'
    invalid.append(unknown_argument)
    for value in invalid:
        assert comment_pagers(flight(value), POST_ID) == []


def test_observed_context_whitelists_headers_and_copies_only_bound_client_context():
    payload = pager()['requestedArguments']['payload']
    body = {'clientArguments': {'payload': payload, 'screenId': 'fixture-screen',
                                'states': {'fixture': []}, 'knownTemplateIds': ['fixture-template']}}
    headers = {'Cookie': 'not-retained', 'Authorization': 'not-retained',
               'CSRF-Token': 'fixture-token', 'Content-Type': 'application/json',
               'X-Unverified-Header': 'not-retained'}
    actual = observed_context('https://www.linkedin.com' + PAGINATION_PATH, 'POST', headers, body, POST_ID)
    assert actual['headers'] == {'csrf-token': 'fixture-token', 'content-type': 'application/json'}
    assert set(actual) == {'post_id', 'headers', 'client', 'path'}
    assert actual['path'] == PAGINATION_PATH
    traced = observed_context('https://www.linkedin.com' + PAGINATION_PATH + '?sduiid=fixture&parentSpanId=fixture',
                              'POST', headers, body, POST_ID)
    assert traced['path'] == PAGINATION_PATH + '?sduiid=fixture&parentSpanId=fixture'
    assert 'payload' not in actual['client']
    body['clientArguments']['states']['fixture'].append('later mutation')
    assert actual['client']['states'] == {'fixture': []}
    for url, method, target in [
        ('https://other.invalid' + PAGINATION_PATH, 'POST', POST_ID),
        ('https://www.linkedin.com' + PAGINATION_PATH + '?changed=1', 'POST', POST_ID),
        ('https://www.linkedin.com' + PAGINATION_PATH, 'GET', POST_ID),
        ('https://www.linkedin.com' + PAGINATION_PATH, 'POST', 'urn:li:activity:99999'),
    ]:
        assert observed_context(url, method, headers, body, target) is None


def test_pagination_body_keeps_cursor_and_does_not_mutate_observed_request():
    original_context, original_pager = context(), pager()
    before_context, before_pager = deepcopy(original_context), deepcopy(original_pager)
    result = pagination_body(original_context, original_pager, POST_ID)
    assert result['clientArguments']['payload']['pageToken'] == 'opaque-observed-token'
    result['clientArguments']['payload']['pageToken'] = 'modified-copy'
    result['clientArguments']['states']['fixture'].append('modified-copy')
    result['paginationRequest']['requestedArguments']['payload']['pageToken'] = 'modified-copy'
    assert original_context == before_context
    assert original_pager == before_pager
    with pytest.raises(AdapterError, match='linkedin_read_scope_mismatch'):
        pagination_body(original_context, original_pager, 'urn:li:activity:99999')
    original_pager['requestedArguments']['payload']['unverifiedAction'] = 'unknown'
    with pytest.raises(AdapterError, match='linkedin_comment_pager_unavailable'):
        pagination_body(original_context, original_pager, POST_ID)


class FakePage:
    def __init__(self, result, url=POST_URL):
        self.url = url
        self.result = result
        self.calls = []

    async def evaluate(self, script, args):
        assert "redirect: 'manual'" in script
        assert "method: 'POST'" in script
        assert "credentials: 'include'" in script
        self.calls.append(args)
        return self.result


def test_fetch_reads_one_bound_page_and_rejects_changed_browser_post_before_request():
    page = FakePage({'status': 200, 'text': '0:{"children":"observed read response"}\n'})
    actual = asyncio.run(fetch_comments_page(page, context(), pager(), POST_ID))
    assert actual.visible_text('$0') == 'observed read response'
    assert len(page.calls) == 1
    assert page.calls[0]['path'] == PAGINATION_PATH
    page.url = 'https://www.linkedin.com/feed/update/urn:li:activity:99999/'
    with pytest.raises(AdapterError, match='linkedin_read_scope_mismatch'):
        asyncio.run(fetch_comments_page(page, context(), pager(), POST_ID))
    assert len(page.calls) == 1


def test_fetch_denial_and_redirect_stop_without_retry():
    for status, error in [(429, 'linkedin_access_denied_or_rate_limited'),
                          (0, 'linkedin_access_denied_or_rate_limited'),
                          (302, 'linkedin_comment_read_failed')]:
        page = FakePage({'status': status})
        with pytest.raises(AdapterError, match=error):
            asyncio.run(fetch_comments_page(page, context(), pager(), POST_ID))
        assert len(page.calls) == 1


def test_adapter_repeated_page_token_stops_without_another_request(monkeypatch):
    import social_engage.linkedin_browser as browser
    adapter = browser.LinkedInBrowserAdapter()
    adapter.page = FakePage({})
    adapter.comment_context = context()
    adapter.streams = [{'path': '/feed/update/' + POST_ID + '/', 'flight': flight(pager())}]
    calls = []
    async def guard(*args):
        pass
    async def read(*args):
        calls.append(True)
        return flight(pager())
    monkeypatch.setattr(adapter, '_page_guard', guard)
    monkeypatch.setattr(adapter, '_verify_viewer', guard)
    monkeypatch.setattr(browser, 'fetch_comments_page', read)
    try:
        assert adapter.loop.run_until_complete(adapter._read_comment_page(POST_ID, {})) == 'page'
        assert adapter.loop.run_until_complete(adapter._read_comment_page(POST_ID, {})) == 'repeated_cursor'
        assert len(calls) == adapter.comment_pages == 1
    finally:
        adapter.page = None
        adapter.close()
