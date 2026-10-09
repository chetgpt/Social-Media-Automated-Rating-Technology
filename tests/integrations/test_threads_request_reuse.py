"""An observed native read must not become a duplicate browser API request."""
import asyncio
import json
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from social_engage.adapters import AdapterError
from social_engage.threads_browser import ThreadsBrowserAdapter, read_template
from social_engage.threads_data import id_to_shortcode, project_post


URL = "https://www.threads.com/graphql/query"
OPERATION = "BarcelonaPostPageDirectRepliesRefetchQuery"


def native_request(*, cursor="next", target="123", first=7, doc_id="12345",
                   operation=OPERATION, url=URL, method="POST", include_cursor=True):
    variables = {"postID": target, "first": first, "sortOrder": "RECENT"}
    if include_cursor:
        variables["after"] = cursor
    body = urlencode({"doc_id": doc_id, "fb_api_req_friendly_name": operation,
                      "variables": json.dumps(variables)})
    return SimpleNamespace(url=url, method=method, post_data=body, headers={})


async def fixture():
    def post(identity, owner, author):
        return {"pk": identity, "id": identity + "_" + owner,
                "code": id_to_shortcode(identity), "user": {"pk": owner, "username": author},
                "taken_at": 1789000000, "text_post_app_info": {"direct_reply_count": 0}}

    root, reply = post("123", "42", "creator"), post("124", "43", "responder")
    reply["text_post_app_info"].update(is_reply=True, reply_to_author={"username": "creator"})
    payload = {"data": {"media": {"id": "123_42", "text_post_app_info": {
        "direct_replies": {"edges": [{"node": {"posts": {"edges": [{"node": reply}]}}}],
                           "page_info": {"has_next_page": False, "end_cursor": None}}}}}}

    adapter = ThreadsBrowserAdapter.__new__(ThreadsBrowserAdapter)
    adapter.loop = asyncio.get_running_loop()
    adapter.generation, adapter.kind, adapter.post_target = 1, "post", "123"
    adapter.root_hint, adapter.page_root_hint = project_post(root), root
    adapter.events, adapter.observed_requests, adapter.reply_roots = [], [], {}
    adapter.replay_waiter, adapter.tasks = None, set()

    async def guard():
        pass

    async def unexpected_fetch(*args):
        pytest.fail("a matching native request must be reused without another fetch")

    adapter._page_guard = guard
    adapter.page = SimpleNamespace(url="https://www.threads.com/@creator/post/" + id_to_shortcode("123"),
                                   evaluate=unexpected_fetch)
    template_request = native_request(cursor=None)
    event = {"template": read_template(URL, "POST", template_request.post_data, {})}

    def response(request, status=200):
        async def text():
            return json.dumps(payload)
        return SimpleNamespace(url=request.url, request=request, status=status, text=text)

    return adapter, event, response


def test_pending_native_request_is_awaited_without_duplicate_fetch():
    async def scenario():
        adapter, event, response = await fixture()
        request = native_request()
        adapter._observe_request(request)
        replay = asyncio.create_task(adapter._replay(event, "next"))
        await asyncio.sleep(0)
        assert not replay.done()
        await adapter._response(response(request), adapter.generation)
        assert await replay is True
        assert [(post["id"], post["parent_id"]) for post in adapter._posts()] == [
            ("123", ""), ("124", "123")]
        assert adapter.replay_waiter is None

    asyncio.run(scenario())


def test_native_response_completed_before_replay_is_reused():
    async def scenario():
        adapter, event, response = await fixture()
        request = native_request()
        adapter._observe_request(request)
        assert adapter.replay_waiter is None
        await adapter._response(response(request), adapter.generation)
        assert await adapter._replay(event, "next") is True
        assert len(adapter.events) == 1

    asyncio.run(scenario())


def test_failed_native_request_does_not_trigger_an_automatic_duplicate():
    async def scenario():
        adapter, event, _ = await fixture()
        request = native_request()
        adapter._observe_request(request)
        adapter._request_failed(request)
        assert await adapter._replay(event, "next") is False
        assert adapter.events == []

    asyncio.run(scenario())


@pytest.mark.parametrize("different", [
    {"cursor": "other"}, {"target": "999"}, {"first": 3}, {"doc_id": "98765"},
])
def test_different_native_read_cannot_satisfy_requested_page(different):
    async def scenario():
        adapter, event, response = await fixture()
        adapter._observe_request(native_request(**different))
        fetched = []

        async def fetch(script, args):
            fetched.append(args["body"])
            exact = SimpleNamespace(url=args["url"], method="POST", post_data=args["body"], headers={})
            await adapter._response(response(exact), adapter.generation)
            return {"status": 200, "body_read": True}

        adapter.page.evaluate = fetch
        assert await adapter._replay(event, "next") is True
        assert len(fetched) == 1
        for record in adapter.observed_requests:
            if not record["future"].done():
                record["future"].cancel()

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [401, 403, 429])
def test_reusing_a_denied_native_read_surfaces_the_access_blocker(status):
    async def scenario():
        adapter, event, response = await fixture()
        request = native_request()
        adapter._observe_request(request)
        await adapter._response(response(request, status), adapter.generation)
        with pytest.raises(AdapterError, match="^threads_access_denied_or_rate_limited$"):
            await adapter._replay(event, "next")

    asyncio.run(scenario())


@pytest.mark.parametrize("invalid", [
    {"operation": "BarcelonaPostDeleteMutation"}, {"url": "https://unrelated.invalid/graphql/query"},
    {"method": "GET"}, {"include_cursor": False}, {"target": "999"},
])
def test_unusable_or_unrelated_requests_are_not_cached(invalid):
    async def scenario():
        adapter, _, _ = await fixture()
        adapter._observe_request(native_request(**invalid))
        assert adapter.observed_requests == []

    asyncio.run(scenario())


def test_navigation_discards_the_previous_post_request_and_wait():
    async def scenario():
        adapter, _, response = await fixture()
        request = native_request()
        adapter._observe_request(request)
        old_future = adapter.observed_requests[0]["future"]

        async def nothing(*args, **kwargs):
            pass

        async def bootstrap():
            return []

        adapter.page.goto = nothing
        adapter._settle, adapter._bootstrap = nothing, bootstrap
        await adapter._navigate("https://www.threads.com/@other/post/" + id_to_shortcode("999"), "post")
        assert adapter.post_target == "999"
        assert adapter.observed_requests == []
        assert old_future.done()
        adapter._schedule_response(response(request))
        assert adapter.tasks == set() and adapter.events == []

    asyncio.run(scenario())
