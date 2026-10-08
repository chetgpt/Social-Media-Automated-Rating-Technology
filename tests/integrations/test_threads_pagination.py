"""Read-only pagination regressions with a delayed synthetic website response."""
import asyncio
from types import SimpleNamespace

from social_engage.threads_browser import ThreadsBrowserAdapter


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
