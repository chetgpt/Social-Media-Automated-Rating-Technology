"""Synthetic LinkedIn transport tests: no browser, network, or runtime artifacts."""
import asyncio
import json

import pytest

import engage_social as cli
from social_engage.adapters import AdapterError
from social_engage.linkedin_browser import LinkedInBrowserAdapter, html_flight
from social_engage.linkedin_data import FlightData


ACTOR = {'id': 'urn:li:fsd_profile:ACoABCDEFGHIJKLMN', 'username': 'operator'}
POST_ID = 'urn:li:activity:12345'
POST_URL = f'https://www.linkedin.com/feed/update/{POST_ID}/'


def scope(**updates):
    result = {'source': 'post', 'target': POST_ID, 'account': 'operator', 'comments': 10,
              'candidate_limit': 1, 'since': None, 'until': None}
    result.update(updates)
    return result


def stream(*, activity='12345', owner='creator', viewer='ACoABCDEFGHIJKLMN', count='4', comments=()):
    rows = {'0': {'currentUserNonIterableProfileId': viewer},
            '1': {'role': 'listitem', 'componentkey': 'update-card-focus-test-only', 'children': [
                {'activityUrn': {'activityId': activity}},
                {'vanityName': owner, 'updateKeyContainer': {'tracking': 'not evidence'}},
                {'isExpandableTextV2Enabled': True, 'textProps': {'children': 'Exact observed caption'}},
                {'aria-label': 'Comment', 'children': count},
            ]}}
    for index, comment_id in enumerate(comments, 2):
        rows[f'{index:x}'] = {'componentkey': f'CommentComponentReference_urn:li:comment:(activity:{activity},{comment_id})',
                              'children': [
                                  {'contentRef': 'not evidence', 'vanityName': 'audience'},
                                  {'commentUrn': {'thread': f'urn:li:activity:{activity}', 'commentId': comment_id}},
                                  {'isExpandableTextV2Enabled': True, 'textProps': {'children': f'Comment {comment_id}'}},
                              ]}
    return FlightData(''.join(f'{key}:{json.dumps(value)}\n' for key, value in rows.items()))


class FakeLocator:
    def __init__(self, count=0, text=''):
        self.number = count
        self.text = text
        self.clicks = 0

    async def count(self):
        return self.number

    async def inner_text(self):
        return self.text

    async def is_visible(self):
        return bool(self.number)

    async def click(self):
        self.clicks += 1

    def get_by_role(self, *args, **kwargs):
        return FakeLocator(1, self.text)


class FakePage:
    def __init__(self):
        self.url = POST_URL
        self.scrolls = 0
        self.more = FakeLocator()

    def get_by_role(self, *args, **kwargs):
        return self.more

    def locator(self, *args, **kwargs):
        return FakeLocator()

    async def evaluate(self, script):
        assert script.startswith('window.scrollBy(')
        self.scrolls += 1

    async def close(self):
        pass


@pytest.fixture
def adapter(monkeypatch):
    instance = LinkedInBrowserAdapter()
    instance.page = FakePage()
    instance.actor = dict(ACTOR)
    instance.test_flight = stream(comments=('456', '457'))
    instance.redirect = None
    instance.card = FakeLocator(1, '4')

    async def navigate(url):
        instance.page.url = instance.redirect or url
        from urllib.parse import urlsplit
        instance.streams = [{'path': urlsplit(instance.page.url).path, 'flight': instance.test_flight}]
        return instance.test_flight

    async def settle():
        pass

    async def no_connect(*args):
        raise AssertionError('real browser connection forbidden')

    monkeypatch.setattr(instance, '_navigate', navigate)
    monkeypatch.setattr(instance, '_settle', settle)
    monkeypatch.setattr(instance, '_connect', no_connect)
    monkeypatch.setattr(instance, '_card_locator', lambda key: instance.card)
    yield instance
    instance.close()


def test_exact_post_collection_persists_only_normalized_evidence(adapter):
    evidence = adapter.collect(POST_ID, scope(), ACTOR)
    assert evidence['id'] == POST_ID
    assert evidence['url'] == POST_URL
    assert evidence['author'] == 'creator'
    assert evidence['text'] == 'Exact observed caption'
    assert sorted(row['id'] for row in evidence['comments']) == [
        'urn:li:comment:(activity:12345,456)', 'urn:li:comment:(activity:12345,457)']
    serialized = json.dumps(evidence)
    for raw_field in ('component_key', 'componentkey', 'updateKeyContainer', 'contentRef', 'tracking'):
        assert raw_field not in serialized
    assert evidence['authority'] == 'linkedin_authenticated_browser_website'
    assert evidence['comments_complete'] is False
    assert evidence['comments_status'] == 'bounded_or_parent_link_unavailable'
    assert all(evidence[key]['status'] == 'unsupported' for key in ('music', 'transcript', 'subtitles', 'catalog'))


def test_collect_rejects_post_outside_frozen_exact_scope(adapter):
    with pytest.raises(AdapterError):
        adapter.collect(POST_ID, scope(target='urn:li:activity:99999'), ACTOR)


def test_collect_rejects_redirect_to_a_different_activity(adapter):
    adapter.redirect = 'https://www.linkedin.com/feed/update/urn:li:activity:99999/'
    with pytest.raises(AdapterError, match='exact_post_page_identity_mismatch'):
        adapter.collect(POST_ID, scope(), ACTOR)


def test_collect_rejects_post_identity_absent_from_observed_payload(adapter):
    adapter.test_flight = stream(activity='99999')
    with pytest.raises(AdapterError, match='exact_linkedin_post_not_available'):
        adapter.collect(POST_ID, scope(), ACTOR)


def test_collect_rejects_changed_active_account(adapter):
    adapter.test_flight = stream(viewer='ACoANOTHERACCOUNT')
    with pytest.raises(AdapterError, match='account_mismatch'):
        adapter.collect(POST_ID, scope(), ACTOR)


def test_scope_account_mismatch_stops_before_navigation(adapter, monkeypatch):
    async def no_navigate(url):
        raise AssertionError('mismatched scope must fail before navigation')

    monkeypatch.setattr(adapter, '_navigate', no_navigate)
    with pytest.raises(AdapterError, match='account_mismatch'):
        adapter.collect(POST_ID, scope(account='other-operator'), ACTOR)


def test_account_change_during_comment_pagination_is_rejected(adapter, monkeypatch):
    async def account_changed():
        adapter.streams.append({'path': '/flagship-web/rsc-action/actions/pagination',
                                'flight': stream(viewer='ACoANOTHERACCOUNT')})

    monkeypatch.setattr(adapter, '_settle', account_changed)
    with pytest.raises(AdapterError, match='account_mismatch'):
        adapter.collect(POST_ID, scope(), ACTOR)


@pytest.mark.parametrize('source,target', [('creator', 'other-creator'), ('own', 'operator')])
def test_collect_rejects_post_owner_outside_frozen_scope(adapter, source, target):
    with pytest.raises(AdapterError, match='creator_mismatch'):
        adapter.collect(POST_ID, scope(source=source, target=target), ACTOR)


@pytest.mark.parametrize('since', [0, 1])
def test_unknown_publication_date_cannot_satisfy_a_date_window(adapter, since):
    with pytest.raises(AdapterError, match='publication_time_outside_scope'):
        adapter.collect(POST_ID, scope(since=since, until=2000000000), ACTOR)


def test_unknown_publication_dates_do_not_fill_discovery_quota(adapter):
    found = adapter.discover(scope(source='topic', target='music', since=1, until=2000000000))
    assert found == []
    assert adapter.page.scrolls <= 3


def test_comment_count_matching_observations_does_not_prove_parent_coverage(adapter):
    adapter.card.text = '2'
    evidence = adapter.collect(POST_ID, scope(), ACTOR)
    assert evidence['comment_count'] == len(evidence['comments']) == 2
    assert evidence['comments_complete'] is False
    assert all(row['parent_binding'] == 'not_exposed' for row in evidence['comments'])
    assert adapter.page.scrolls == 2


def test_comment_cap_bounds_output_without_inventing_completion(adapter):
    adapter.test_flight = stream(comments=('456', '457', '458'))
    evidence = adapter.collect(POST_ID, scope(comments=1), ACTOR)
    assert len(evidence['comments']) == 1
    assert evidence['comments_complete'] is False
    assert adapter.page.scrolls == 0


def test_explicit_zero_comments_can_be_complete_when_none_observed(adapter):
    adapter.test_flight = stream(count='0')
    adapter.card.text = '0'
    evidence = adapter.collect(POST_ID, scope(), ACTOR)
    assert evidence['comments'] == []
    assert evidence['comments_complete'] is True


def test_zero_count_with_observed_comments_remains_incomplete(adapter):
    adapter.card.text = '0'
    evidence = adapter.collect(POST_ID, scope(), ACTOR)
    assert evidence['comments']
    assert evidence['comments_complete'] is False


def test_preflight_blocks_before_browser_access_or_publication_reservation(adapter, monkeypatch):
    class ReadyState:
        def ready(self, *args):
            return {'scope': scope(), 'actor': ACTOR}, {'evidence': {'id': POST_ID}}

        def reserve(self, *args):
            raise AssertionError('publication must not be reserved')

    def no_browser(*args):
        raise AssertionError('publication preflight must not access a browser')

    adapter.prepared = ('stale', 'preparation', 'must be cleared')
    monkeypatch.setattr(adapter, '_run', no_browser)
    with pytest.raises(AdapterError, match='linkedin_native_publication_not_yet_verified'):
        cli.publish(ReadyState(), 'synthetic-run', POST_ID, adapter)
    assert adapter.prepared is None


def test_bootstrap_reads_json_without_evaluating_trailing_script():
    payload = json.dumps(['a:{"children":"Observed"}\n'])
    flight = html_flight('window.__como_rehydration__ = ' + payload + '; maliciousFunction();')
    assert flight.visible_text('$a') == 'Observed'


def test_speculative_prefetch_cannot_supply_the_selected_post(adapter):
    adapter.streams = [{'path': '/flagship-web/feed/', 'flight': adapter.test_flight}]
    assert adapter._posts() == []
    assert adapter._comments(POST_ID) == []

@pytest.mark.parametrize('code', ['linkedin_human_verification_required', 'linkedin_access_denied_or_rate_limited', 'account_mismatch'])
def test_browser_blocker_stays_blocked_for_remaining_calls(code):
    """A caught discovery/collection failure cannot silently try another page."""
    adapter = LinkedInBrowserAdapter()
    adapter.page = object()  # Already attached; no live connection is possible.
    calls = []

    async def blocked_read():
        calls.append('first')
        raise AdapterError(code)

    async def another_read():
        calls.append('second')
        return ['must not be read']

    try:
        with pytest.raises(AdapterError, match=code):
            adapter._run(blocked_read)
        with pytest.raises(AdapterError, match=code):
            adapter._run(another_read)
        assert calls == ['first']
    finally:
        adapter.page = None
        adapter.close()


def test_unrelated_response_traffic_cannot_fill_capture_slots():
    adapter = LinkedInBrowserAdapter()
    class Response:
        url = 'https://www.linkedin.com/assets/unrelated.js'
    try:
        for _ in range(100):
            adapter._schedule(Response())
        assert not adapter.tasks and not adapter.streams
        adapter.tasks = set(range(30))
        Response.url = 'https://www.linkedin.com/flagship-web/rsc-action/actions/pagination'
        adapter._schedule(Response())
        assert adapter.streams == [{'error': 'linkedin_capture_overflow'}]
    finally:
        adapter.tasks.clear()
        adapter.close()


def test_slow_capture_is_reported_and_cannot_claim_zero_comment_completeness(adapter, monkeypatch):
    async def no_delay(*args):
        pass
    async def unfinished(tasks, **kwargs):
        return set(), set(tasks)
    async def settle_once():
        task = asyncio.create_task(asyncio.Event().wait())
        adapter.tasks.add(task)
        await LinkedInBrowserAdapter._settle(adapter)
        assert task.cancelled()
        adapter.tasks.clear()
    monkeypatch.setattr(adapter.page, 'wait_for_timeout', no_delay, raising=False)
    monkeypatch.setattr(asyncio, 'wait', unfinished)
    adapter.loop.run_until_complete(settle_once())
    assert adapter.capture_timeouts == 1
    adapter.test_flight = stream(count='0')
    adapter.card.text = '0'
    evidence = adapter.collect(POST_ID, scope(), ACTOR)
    assert evidence['comments_complete'] is False
    assert evidence['comment_pagination']['capture_timeouts'] == 1
