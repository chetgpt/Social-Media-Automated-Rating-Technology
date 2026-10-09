"""Read-only pagination regressions with a delayed synthetic website response."""
import asyncio
import json
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from social_engage.adapters import AdapterError
from social_engage.threads_browser import ThreadsBrowserAdapter, read_template
from social_engage.threads_data import id_to_shortcode, project_post


def adapter_fixture():
    adapter = ThreadsBrowserAdapter.__new__(ThreadsBrowserAdapter)
    adapter.events = [{"posts": [{"id": "1"}], "connections": [{"has_next_page": True, "cursor": "a", "ids": ["1"]}], "template": None}]
    async def nothing(*args): pass
    adapter._page_guard = nothing
    adapter.page = SimpleNamespace(evaluate=nothing, mouse=SimpleNamespace(wheel=nothing))
    return adapter


def test_bootstrap_cursor_without_template_waits_for_delayed_search_batch():
    adapter = adapter_fixture()
    settled, replays = [], []
    async def settle():
        settled.append(1)
        if len(settled) == 3:
            adapter.events.append({"posts": [{"id": str(n)} for n in range(2, 22)], "connections": []})
    async def replay(event, cursor):
        replays.append(cursor)
        return False
    adapter._settle, adapter._replay = settle, replay
    asyncio.run(adapter._paginate(lambda posts: len(posts) >= 20))
    assert len(adapter._posts()) == 21
    assert len(settled) == 3
    assert replays == ["a", "a"]  # Failed replay was never marked consumed.


def test_repeated_cursor_after_replay_still_allows_native_scroll():
    adapter = adapter_fixture()
    replays = []
    async def replay(event, cursor):
        replays.append(cursor)
        return True
    async def settle():
        adapter.events.append({"posts": [{"id": "2"}], "connections": []})
    adapter._replay, adapter._settle = replay, settle
    asyncio.run(adapter._paginate(lambda posts: len(posts) >= 2))
    assert len(adapter._posts()) == 2 and replays == ["a"]


def test_stalled_search_is_bounded():
    adapter = adapter_fixture()
    rounds = []
    async def settle(): rounds.append(1)
    async def replay(*args): return False
    adapter._settle, adapter._replay = settle, replay
    asyncio.run(adapter._paginate(lambda posts: len(posts) >= 20))
    assert len(rounds) == 6 and len(adapter._posts()) == 1


def test_verified_terminal_connection_stops_without_replay_or_scroll():
    adapter = adapter_fixture()
    adapter.events[0]["connections"][0]["has_next_page"] = False
    async def fail(*args): raise AssertionError("terminal connection should stop")
    adapter._replay, adapter._settle = fail, fail
    asyncio.run(adapter._paginate(lambda posts: len(posts) >= 20))


def replay_fixture():
    def post(identity, owner_id, author):
        return {"pk": identity, "id": identity + "_" + owner_id,
                "code": id_to_shortcode(identity), "user": {"pk": owner_id, "username": author},
                "taken_at": 1700000000, "text_post_app_info": {"direct_reply_count": 0}}

    root, reply = post("123", "42", "creator"), post("124", "43", "responder")
    reply["text_post_app_info"].update(is_reply=True, reply_to_author={"username": "creator"})
    payload = {"data": {"media": {"id": "123_42", "text_post_app_info": {"direct_replies": {
        "edges": [{"node": {"posts": {"edges": [{"node": reply}]}}}],
        "page_info": {"has_next_page": False, "end_cursor": None}}}}}}
    url = "https://www.threads.com/graphql/query"
    body = urlencode({"doc_id": "12345", "fb_api_req_friendly_name": "BarcelonaPostPageDirectRepliesRefetchQuery",
                      "variables": json.dumps({"postID": "123", "after": None})})
    template = read_template(url, "POST", body, {})
    adapter = ThreadsBrowserAdapter.__new__(ThreadsBrowserAdapter)
    adapter.generation, adapter.kind, adapter.post_target = 1, "post", "123"
    adapter.root_hint, adapter.page_root_hint = project_post(root), root
    adapter.replay_waiter, adapter.events = None, []

    async def guard(): pass
    async def forbidden_settle(): raise AssertionError("replay must await its parsed response, not a fixed delay")
    adapter._page_guard, adapter._settle = guard, forbidden_settle

    def response(body, text=None, status=200):
        async def read(): return json.dumps(payload) if text is None else text
        return SimpleNamespace(url=url, status=status, text=read,
                               request=SimpleNamespace(method="POST", post_data=body, headers={}))
    return adapter, {"template": template}, response


def test_replay_waits_for_exact_parsed_response_and_binds_partial_reply_page():
    async def scenario():
        adapter, event, response = replay_fixture()
        headers_received, release_body = asyncio.Event(), asyncio.Event()
        response_tasks = []

        async def evaluate(script, args):
            # A different cursor and an old navigation cannot satisfy this wait.
            await adapter._response(response(args["body"].replace("next", "other")), 1)
            await adapter._response(response(args["body"]), 0)
            exact = response(args["body"])
            original_read = exact.text

            async def delayed_body():
                headers_received.set()
                await release_body.wait()
                return await original_read()

            exact.text = delayed_body
            response_tasks.append(asyncio.create_task(adapter._response(exact, 1)))
            return {"status": 200}

        adapter.page = SimpleNamespace(url="https://www.threads.com/@creator/post/B7", evaluate=evaluate)
        replay = asyncio.create_task(adapter._replay(event, "next"))
        await headers_received.wait()
        assert not replay.done()
        release_body.set()
        assert await replay is True
        await asyncio.gather(*response_tasks)
        assert adapter.replay_waiter is None
        assert [(p["id"], p["parent_id"]) for p in adapter._posts()] == [("123", ""), ("124", "123")]
        assert adapter.events[-1]["connections"] == [{"has_next_page": False, "cursor": None, "ids": ["123"]}]

    asyncio.run(scenario())


@pytest.mark.parametrize("body, error", [("{invalid", None), ('{"errors":[{"message":"private"}]}', "threads_query_unavailable")])
def test_replay_does_not_consume_malformed_or_failed_response(body, error):
    async def scenario():
        adapter, event, response = replay_fixture()

        async def evaluate(script, args):
            await adapter._response(response(args["body"], body), 1)
            return {"status": 200}

        adapter.page = SimpleNamespace(url="https://www.threads.com/", evaluate=evaluate)
        if error:
            with pytest.raises(AdapterError, match=error):
                await adapter._replay(event, "next")
        else:
            assert await adapter._replay(event, "next") is False
        assert adapter.replay_waiter is None

    asyncio.run(scenario())


def test_replay_missing_response_times_out_without_consuming_cursor(monkeypatch):
    original_wait = asyncio.wait_for

    async def short_wait(future, timeout):
        return await original_wait(future, timeout=0.001)

    monkeypatch.setattr("social_engage.threads_browser.asyncio.wait_for", short_wait)

    async def scenario():
        adapter, event, _ = replay_fixture()

        async def evaluate(script, args): return {"status": 200}

        adapter.page = SimpleNamespace(url="https://www.threads.com/", evaluate=evaluate)
        assert await adapter._replay(event, "next") is False
        assert adapter.replay_waiter is None and adapter.events == []

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [401, 403, 429])
def test_replay_fetch_denial_preserves_the_access_blocker(status):
    async def scenario():
        adapter, event, _ = replay_fixture()

        async def evaluate(script, args): return {"status": status}

        adapter.page = SimpleNamespace(url="https://www.threads.com/", evaluate=evaluate)
        with pytest.raises(AdapterError, match="^threads_access_denied_or_rate_limited$"):
            await adapter._replay(event, "next")
        assert adapter.replay_waiter is None

    asyncio.run(scenario())


def test_failed_body_read_cancels_only_its_owned_parser():
    async def scenario():
        adapter, event, response = replay_fixture()
        started = asyncio.Event()
        parsers = []

        async def evaluate(script, args):
            exact = response(args["body"])

            async def unfinished_body():
                started.set()
                await asyncio.Event().wait()

            exact.text = unfinished_body
            parsers.append(asyncio.create_task(adapter._response(exact, 1)))
            await started.wait()
            return {"status": 200, "body_read": False}

        adapter.page = SimpleNamespace(url="https://www.threads.com/", evaluate=evaluate)
        assert await adapter._replay(event, "next") is False
        assert parsers[0].cancelled()
        assert adapter.replay_waiter is None and adapter.events == []

    asyncio.run(scenario())


def test_replay_retains_one_parser_slot_when_other_read_callbacks_are_busy():
    async def scenario():
        adapter, event, response = replay_fixture()
        adapter.loop = asyncio.get_running_loop()
        busy = {adapter.loop.create_future() for _ in range(40)}
        adapter.tasks = set(busy)

        async def evaluate(script, args):
            adapter._schedule_response(response(args["body"].replace("next", "other")))
            assert len(adapter.tasks) == 40
            adapter._schedule_response(response(args["body"]))
            assert len(adapter.tasks) == 41
            adapter._schedule_response(response(args["body"]))
            assert len(adapter.tasks) == 41
            return {"status": 200, "body_read": True}

        adapter.page = SimpleNamespace(url="https://www.threads.com/", evaluate=evaluate)
        try:
            assert await adapter._replay(event, "next") is True
            assert adapter.replay_waiter is None
        finally:
            for future in busy:
                future.cancel()

    asyncio.run(scenario())


def test_partial_reply_page_requires_exact_query_target_and_current_generation():
    async def scenario():
        adapter, event, response = replay_fixture()
        form = event["template"]["form"]
        other = urlencode({**form, "variables": json.dumps({"postID": "999", "after": "next"})})
        await adapter._response(response(other), 1)
        await adapter._response(response(urlencode(form)), 0)
        assert adapter.events == []
        adapter.events = [{"template": read_template(event["template"]["url"], "POST", other, {})}]
        assert await adapter._replay(adapter.events[0], "next") is False

    asyncio.run(scenario())
