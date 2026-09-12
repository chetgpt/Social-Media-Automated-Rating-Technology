import asyncio
import json

import pytest

from tiktok_scraper import api_integration as api_module
from tiktok_scraper.api_integration import (
    TikTokAPIIntegration,
    TikTokSearchSessionError,
)


def _search_payload(post_id: str = "123456789") -> dict:
    return {
        "status_code": 0,
        "has_more": False,
        "item_list": [
            {
                "id": post_id,
                "desc": "mr diy result",
                "author": {"unique_id": "fixture_creator"},
                "stats": {"commentCount": 2},
            }
        ],
    }


class _Mouse:
    async def wheel(self, _x, _y):
        return None


class _Response:
    def __init__(self, url, *, payload=None, text_body=None):
        self.url = url
        self.status = 200
        self.headers = {}
        self._payload = payload
        self._text_body = text_body
        self.json_calls = 0
        self.text_calls = 0

    async def json(self):
        self.json_calls += 1
        if self._payload is None:
            raise ValueError("not json")
        return self._payload

    async def text(self):
        self.text_calls += 1
        if self._text_body is None:
            raise AssertionError("response.text() should not follow successful json()")
        return self._text_body


class _CapturePage:
    def __init__(self, response_batches):
        self.response_batches = list(response_batches)
        self.callbacks = []
        self.goto_urls = []
        self.url = ""
        self.mouse = _Mouse()

    def on(self, event, callback):
        assert event == "response"
        self.callbacks.append(callback)

    def remove_listener(self, event, callback):
        assert event == "response"
        self.callbacks.remove(callback)

    async def goto(self, url, **_kwargs):
        self.url = url
        self.goto_urls.append(url)
        batch_index = min(len(self.goto_urls) - 1, len(self.response_batches) - 1)
        for response in self.response_batches[batch_index]:
            for callback in list(self.callbacks):
                callback(response)
            await asyncio.sleep(0)

    async def evaluate(self, _script):
        return None


def test_search_capture_uses_json_before_text():
    response = _Response(
        "https://www.tiktok.com/api/search/item/full/?keyword=mr+diy&cursor=0",
        payload=_search_payload(),
    )
    page = _CapturePage([[response]])

    packets = asyncio.run(
        TikTokAPIIntegration(enable_api=False)._capture_search_api_pages(
            page,
            "mr diy",
            1,
        )
    )

    assert [packet["data"] for packet in packets] == [_search_payload()]
    assert response.json_calls == 1
    assert response.text_calls == 0


def test_search_capture_binds_exact_keyword_and_redacts_request_tokens():
    stale = _Response(
        "https://www.tiktok.com/api/search/item/full/"
        "?keyword=travel&cursor=0&msToken=secret-a",
        payload=_search_payload("111"),
        text_body=json.dumps(_search_payload("111")),
    )
    target = _Response(
        "https://www.tiktok.com/api/search/item/full/"
        "?keyword=mr+diy&cursor=0&msToken=secret-b&X-Bogus=secret-c",
        payload=_search_payload("222"),
        text_body=json.dumps(_search_payload("222")),
    )
    page = _CapturePage([[stale, target]])

    packets = asyncio.run(
        TikTokAPIIntegration(enable_api=False)._capture_search_api_pages(
            page,
            "  MR   DIY  ",
            1,
        )
    )

    assert [packet["data"]["item_list"][0]["id"] for packet in packets] == [
        "222"
    ]
    assert packets[0]["request_query"]["keyword"] == "mr diy"
    assert "secret-a" not in repr(packets)
    assert "secret-b" not in repr(packets)
    assert "secret-c" not in repr(packets)


def test_search_request_metadata_rejects_userinfo_ports_and_path_suffixes():
    integration = TikTokAPIIntegration(enable_api=False)
    suspicious = (
        "https://embedded-secret@www.tiktok.com:443/"
        "api/search/item/full/extra?keyword=mr+diy&msToken=secret-token"
    )

    metadata = integration._search_request_metadata(suspicious)

    assert metadata == {"url": "", "query": {}}
    assert integration._search_request_matches_keyword(suspicious, "mr diy") is False
    assert "embedded-secret" not in repr(metadata)
    assert "secret-token" not in repr(metadata)


def test_invalid_packet_does_not_consume_valid_page_budget():
    invalid = _Response(
        "https://www.tiktok.com/api/search/item/full/?keyword=mr+diy&cursor=0",
        text_body="<html>search unavailable</html>",
    )
    valid = _Response(
        "https://www.tiktok.com/api/search/item/full/?keyword=mr+diy&cursor=0",
        payload=_search_payload("333"),
        text_body=json.dumps(_search_payload("333")),
    )
    page = _CapturePage([[invalid, valid]])

    packets = asyncio.run(
        TikTokAPIIntegration(enable_api=False)._capture_search_api_pages(
            page,
            "mr diy",
            1,
        )
    )

    valid_ids = [
        packet["data"]["item_list"][0]["id"]
        for packet in packets
        if isinstance(packet.get("data"), dict)
    ]
    assert valid_ids == ["333"]


def test_search_capture_uses_one_coherent_logical_cursor_chain():
    first = {
        **_search_payload("100"),
        "has_more": True,
        "cursor": 12,
    }
    stronger_duplicate = {
        "status_code": 0,
        "has_more": True,
        "cursor": 12,
        "item_list": [
            _search_payload("101")["item_list"][0],
            _search_payload("102")["item_list"][0],
        ],
    }
    out_of_order = _search_payload("124")
    terminal = _search_payload("112")
    responses = [
        _Response(
            "https://www.tiktok.com/api/search/item/full/"
            "?keyword=mr+diy&cursor=12&count=12&search_id=d",
            payload=terminal,
        ),
        _Response(
            "https://www.tiktok.com/api/search/item/full/"
            "?keyword=mr+diy&cursor=24&count=12&search_id=c",
            payload=out_of_order,
        ),
        _Response(
            "https://www.tiktok.com/api/search/item/full/"
            "?keyword=mr+diy&cursor=0&count=12&search_id=a",
            payload=first,
        ),
        _Response(
            "https://www.tiktok.com/api/search/item/full/"
            "?keyword=mr+diy&cursor=0&count=24&search_id=b",
            payload=stronger_duplicate,
        ),
    ]
    page = _CapturePage([responses])

    packets = asyncio.run(
        TikTokAPIIntegration(enable_api=False)._capture_search_api_pages(
            page,
            "mr diy",
            2,
        )
    )

    assert [packet["request_query"]["cursor"] for packet in packets] == [
        "0",
        "12",
    ]
    assert [
        row["id"]
        for packet in packets
        for row in packet["data"]["item_list"]
    ] == ["101", "102", "112"]


def test_search_capture_retries_same_exact_page_once_after_invalid(monkeypatch):
    monkeypatch.setattr(api_module, "TIKTOK_SEARCH_STALL_WAIT_SECONDS", 0.001)
    invalid = _Response(
        "https://www.tiktok.com/api/search/item/full/?keyword=mr+diy&cursor=0",
        text_body="<html>search unavailable</html>",
    )
    valid = _Response(
        "https://www.tiktok.com/api/search/item/full/?keyword=mr+diy&cursor=0",
        payload=_search_payload("444"),
        text_body=json.dumps(_search_payload("444")),
    )
    page = _CapturePage([[invalid], [valid]])

    packets = asyncio.run(
        TikTokAPIIntegration(enable_api=False)._capture_search_api_pages(
            page,
            "mr diy",
            1,
        )
    )

    assert len(page.goto_urls) == 2
    assert page.goto_urls[0] == page.goto_urls[1]
    assert page.goto_urls[0].endswith("q=mr%20diy")
    assert any(
        packet.get("data", {}).get("item_list", [{}])[0].get("id") == "444"
        for packet in packets
        if isinstance(packet.get("data"), dict)
    )


@pytest.mark.parametrize("noise_kind", ["duplicate", "invalid"])
def test_search_capture_noise_does_not_exhaust_wait_for_next_cursor(
    monkeypatch, noise_kind
):
    monkeypatch.setattr(api_module, "TIKTOK_SEARCH_STALL_WAIT_SECONDS", 0.1)
    first = _Response(
        "https://www.tiktok.com/api/search/item/full/?keyword=mr+diy&cursor=0",
        payload={**_search_payload("100"), "has_more": True, "cursor": 12},
    )
    next_page = _Response(
        "https://www.tiktok.com/api/search/item/full/?keyword=mr+diy&cursor=12",
        payload=_search_payload("112"),
    )
    noise = (
        first
        if noise_kind == "duplicate"
        else _Response(first.url, text_body="<html>temporarily unavailable</html>")
    )
    page = _CapturePage([[first]])

    async def collect():
        delayed_response = None

        def emit(response):
            for callback in list(page.callbacks):
                callback(response)

        class NoisyMouse:
            async def wheel(self, _x, _y):
                nonlocal delayed_response
                if delayed_response is None:
                    delayed_response = asyncio.get_running_loop().call_later(
                        0.03, emit, next_page
                    )
                emit(noise)
                await asyncio.sleep(0)

        page.mouse = NoisyMouse()
        try:
            return await TikTokAPIIntegration(enable_api=False)._capture_search_api_pages(
                page, "mr diy", 2
            )
        finally:
            if delayed_response is not None:
                delayed_response.cancel()

    packets = asyncio.run(collect())

    assert [
        packet["request_query"]["cursor"]
        for packet in packets
        if isinstance(packet.get("data"), dict)
    ] == ["0", "12"]
    assert len(page.goto_urls) == 1


def test_search_capture_continuous_duplicates_keep_original_stall_budget(monkeypatch):
    monkeypatch.setattr(api_module, "TIKTOK_SEARCH_STALL_WAIT_SECONDS", 0.01)
    first = _Response(
        "https://www.tiktok.com/api/search/item/full/?keyword=mr+diy&cursor=0",
        payload={**_search_payload("100"), "has_more": True, "cursor": 12},
    )
    page = _CapturePage([[first]])
    scrolls = []

    class CountingMouse:
        async def wheel(self, _x, _y):
            scrolls.append(True)

    page.mouse = CountingMouse()

    async def collect():
        async def repeat_duplicate():
            while True:
                await asyncio.sleep(0.001)
                for callback in list(page.callbacks):
                    callback(first)

        noise_task = asyncio.create_task(repeat_duplicate())
        try:
            return await asyncio.wait_for(
                TikTokAPIIntegration(enable_api=False)._capture_search_api_pages(
                    page, "mr diy", 2
                ),
                timeout=1.0,
            )
        finally:
            noise_task.cancel()
            await asyncio.gather(noise_task, return_exceptions=True)

    packets = asyncio.run(collect())

    assert len(packets) == 1
    assert len(scrolls) == api_module.TIKTOK_SEARCH_STALL_ROUNDS
    assert len(page.goto_urls) == 1


def test_invalid_api_capture_uses_exact_search_rendered_links(monkeypatch):
    monkeypatch.delenv("SCRAPER_TRANSPORT_MODE", raising=False)

    class Integration(TikTokAPIIntegration):
        async def _capture_search_api_pages(self, page, keyword, max_pages):
            self._last_search_page_binding = {
                "page_id": id(page),
                "keyword": keyword,
                "target_url": page.url,
                "document_navigation_succeeded": True,
            }
            return [
                {
                    "url": (
                        "https://www.tiktok.com/api/search/item/full/"
                        "?keyword=mr+diy&cursor=0"
                    ),
                    "http": 200,
                    "bodyLen": 148,
                    "data": None,
                }
            ]

        async def _get_search_template_url(self, page, keyword):
            raise AssertionError("rendered same-page results should precede replay")

    class Locator:
        async def evaluate_all(self, _script):
            return [
                "https://www.tiktok.com/@fixture.creator/video/7654321?lang=en",
                "https://www.tiktok.com/@fixture.creator/video/7654321",
            ]

    class Page:
        url = "https://www.tiktok.com/search/video?q=mr%20diy"
        mouse = _Mouse()

        def locator(self, _selector):
            return Locator()

        async def wait_for_timeout(self, _timeout):
            return None

        async def evaluate(self, _script):
            return None

    integration = Integration(enable_api=False)
    videos = asyncio.run(
        integration.discover_search_videos(
            Page(),
            "mr diy",
            max_offsets=2,
            target_count=1,
        )
    )

    assert [video["id"] for video in videos] == ["7654321"]
    assert videos[0]["url"] == (
        "https://www.tiktok.com/@fixture.creator/video/7654321"
    )
    assert videos[0]["discovery_method"] == (
        "tiktok_search_dom_exact_query_fallback"
    )
    assert videos[0]["metadata_method"] == "tiktok_search_dom_link_only"
    assert videos[0]["fallback_used"] is True
    assert videos[0]["matched_queries"] == ["mr diy"]
    assert integration.last_search_diagnostics["method"] == (
        "rendered_search_fallback"
    )
    assert integration.last_search_diagnostics["fallback_used"] is True


def test_rendered_search_fallback_rejects_a_different_page_query():
    class Page:
        url = "https://www.tiktok.com/search/video?q=travel"

        def locator(self, _selector):
            raise AssertionError("mismatched pages must not be inspected")

    page = Page()
    integration = TikTokAPIIntegration(enable_api=False)
    integration._last_search_page_binding = {
        "page_id": id(page),
        "keyword": "mr diy",
        "target_url": "https://www.tiktok.com/search/video?q=mr%20diy",
        "document_navigation_succeeded": True,
    }
    candidates = asyncio.run(
        integration._discover_rendered_search_candidates(
            page,
            "mr diy",
            target_count=1,
            max_scroll_rounds=1,
        )
    )

    assert candidates == []


def test_api_only_mode_does_not_use_rendered_search_fallback(monkeypatch):
    monkeypatch.setenv("SCRAPER_TRANSPORT_MODE", "api-only")

    class Integration(TikTokAPIIntegration):
        async def _capture_search_api_pages(self, page, keyword, max_pages):
            return [
                {
                    "url": (
                        "https://www.tiktok.com/api/search/item/full/"
                        "?keyword=mr+diy&cursor=0"
                    ),
                    "http": 200,
                    "bodyLen": 148,
                    "data": None,
                }
            ]

        async def _search_session_diagnostics(self, page):
            return {
                "authenticated": True,
                "page_no_results": False,
                "login_prompt": False,
            }

    class Locator:
        async def evaluate_all(self, _script):
            return [
                "https://www.tiktok.com/@fixture.creator/video/7654321"
            ]

    class Page:
        url = "https://www.tiktok.com/search/video?q=mr%20diy"

        def locator(self, _selector):
            return Locator()

    with pytest.raises(TikTokSearchSessionError, match="invalid search session"):
        asyncio.run(
            Integration(enable_api=False).discover_search_videos(
                Page(),
                "mr diy",
                max_offsets=2,
                target_count=1,
            )
        )
