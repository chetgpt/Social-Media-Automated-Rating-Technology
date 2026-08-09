import asyncio
import json
from types import SimpleNamespace

import pytest

from tiktok_scraper.api_integration import (
    TikTokAPIIntegration,
    TikTokCreatorProfileCollectionError,
    normalize_tiktok_creator_target,
)


def _item(
    post_id,
    *,
    handle="Creator.Name",
    creator_id="creator-1",
    sec_uid="sec-creator-1",
    photo=False,
    pinned=False,
    caption=None,
):
    author = {
        "id": creator_id,
        "secUid": sec_uid,
        "nickname": "Creator",
    }
    if handle is not None:
        author["uniqueId"] = handle
    item = {
        "id": str(post_id),
        "desc": f"Post {post_id}" if caption is None else caption,
        "author": author,
        "stats": {
            "playCount": 100,
            "diggCount": 20,
            "commentCount": 3,
            "shareCount": 2,
            "collectCount": 1,
        },
        "video": {"duration": 9, "cover": "https://cdn.example/cover.jpg"},
        "isPinnedItem": pinned,
    }
    if photo:
        item["imagePost"] = {"images": [{"imageURL": {"urlList": []}}]}
    return item


def _post_page(
    rows,
    has_more,
    *,
    status=0,
    http=200,
    sec_uid="sec-creator-1",
    request_cursor="0",
    next_cursor=None,
):
    request_cursor = str(request_cursor)
    if next_cursor is None:
        next_cursor = (
            str(int(request_cursor) + max(1, len(rows)))
            if has_more
            else request_cursor
        )
    query = {
        "sec_uid": sec_uid,
        "cursor": request_cursor,
        "count": "35",
    }
    return {
        "http": http,
        "url": (
            "https://www.tiktok.com/api/post/item_list/"
            f"?secUid={sec_uid}&cursor={request_cursor}&count=35"
        ),
        "query": query,
        "data": {
            "statusCode": status,
            "itemList": list(rows),
            "hasMore": has_more,
            "cursor": str(next_cursor),
        },
    }


def _identity_page(handle="Creator.Name", creator_id="creator-1"):
    return {
        "http": 200,
        "data": {
            "statusCode": 0,
            "userInfo": {
                "user": {
                    "uniqueId": handle,
                    "id": creator_id,
                    "secUid": "sec-creator-1",
                }
            },
        },
    }


def _install_capture(monkeypatch, integration, *, pages, identities=None, stop_reason="fixture"):
    async def capture(page, **kwargs):
        assert page is not None
        assert kwargs["profile_url"].startswith("https://www.tiktok.com/@")
        return {
            "post_pages": list(pages),
            "identity_pages": list(identities or []),
            "capture_diagnostics": {
                "stop_reason": stop_reason,
                "post_responses": len(pages),
            },
        }

    monkeypatch.setattr(integration, "_capture_creator_profile_api_pages", capture)


def test_creator_target_accepts_exact_handle_and_profile_url_only():
    assert normalize_tiktok_creator_target("@Creator.Name") == (
        "Creator.Name",
        "https://www.tiktok.com/@Creator.Name",
    )
    assert normalize_tiktok_creator_target(
        "https://www.tiktok.com/@Creator.Name/?lang=en"
    ) == (
        "Creator.Name",
        "https://www.tiktok.com/@Creator.Name",
    )

    with pytest.raises(ValueError, match="profile URL"):
        normalize_tiktok_creator_target(
            "https://www.tiktok.com/@Creator.Name/video/123"
        )
    with pytest.raises(ValueError, match="tiktok.com"):
        normalize_tiktok_creator_target("https://example.com/@Creator.Name")
    with pytest.raises(ValueError, match="exact handle"):
        normalize_tiktok_creator_target("Creator.Name/video/123")


def test_finite_profile_inventory_dedupes_pinned_and_rejects_other_owners(monkeypatch):
    integration = TikTokAPIIntegration(enable_api=False)
    pages = [
        _post_page(
            [
                _item("100", pinned=True),
                _item("100", pinned=True, caption="Pinned duplicate"),
                _item("999", handle="SomeoneElse"),
                _item("101", photo=True),
            ],
            True,
        )
    ]
    _install_capture(
        monkeypatch,
        integration,
        pages=pages,
        identities=[_identity_page()],
        stop_reason="requested_limit_observed",
    )

    records = asyncio.run(
        integration.discover_creator_profile_posts(
            object(),
            "@creator.name",
            limit=2,
        )
    )

    assert [record["id"] for record in records] == ["100", "101"]
    assert records[0]["url"] == (
        "https://www.tiktok.com/@creator.name/video/100"
    )
    assert records[0]["is_pinned"] is True
    assert records[0]["creator_user_id"] == "creator-1"
    assert records[0]["creator_sec_uid"] == "sec-creator-1"
    assert records[1]["url"] == (
        "https://www.tiktok.com/@creator.name/photo/101"
    )
    assert records[1]["content_type"] == "photo"
    assert records[1]["visual_slide_count"] == 1
    assert records[1]["visual_evidence_status"] == "unavailable"
    assert records[1]["visual_evidence_terminal"] is True
    assert [record["profile_inventory_position"] for record in records] == [1, 2]

    diagnostics = integration.last_creator_profile_diagnostics
    assert diagnostics["limit_reached"] is True
    assert diagnostics["terminal_verified"] is False
    assert diagnostics["duplicate_rows_deduped"] == 1
    assert diagnostics["rejected_handle_mismatch"] == 1
    assert diagnostics["bound_creator_identity"] == {
        "id": "creator-1",
        "sec_uid": "sec-creator-1",
    }


def test_all_profile_inventory_requires_and_records_terminal_has_more_false(monkeypatch):
    integration = TikTokAPIIntegration(enable_api=False)
    pages = [
        _post_page(
            [_item("100", pinned=True), _item("101", photo=True)],
            True,
            next_cursor="20",
        ),
        _post_page(
            [_item("100", pinned=True), _item("102")],
            False,
            request_cursor="20",
        ),
    ]
    _install_capture(
        monkeypatch,
        integration,
        pages=pages,
        identities=[_identity_page()],
        stop_reason="source_exhausted",
    )

    records = asyncio.run(
        integration.discover_creator_profile_posts(
            object(),
            "https://www.tiktok.com/@Creator.Name",
            limit="ALL",
        )
    )

    assert [record["id"] for record in records] == ["100", "101", "102"]
    diagnostics = integration.last_creator_profile_diagnostics
    assert diagnostics["mode"] == "all"
    assert diagnostics["terminal_verified"] is True
    assert diagnostics["source_exhausted"] is True
    assert diagnostics["inventory_complete"] is True
    assert diagnostics["stop_reason"] == "source_exhausted"
    assert diagnostics["duplicate_rows_deduped"] == 1


def test_all_profile_inventory_never_calls_a_stalled_frontier_complete(monkeypatch):
    integration = TikTokAPIIntegration(enable_api=False)
    _install_capture(
        monkeypatch,
        integration,
        pages=[_post_page([_item("100")], True)],
        identities=[_identity_page()],
        stop_reason="pagination_stalled",
    )

    records = asyncio.run(
        integration.discover_creator_profile_posts(
            object(),
            "Creator.Name",
            collect_all=True,
        )
    )

    assert [record["id"] for record in records] == ["100"]
    diagnostics = integration.last_creator_profile_diagnostics
    assert diagnostics["has_more"] is True
    assert diagnostics["terminal_verified"] is False
    assert diagnostics["source_exhausted"] is False
    assert diagnostics["inventory_complete"] is False
    assert diagnostics["stop_reason"] == "pagination_stalled"


def test_owner_can_be_verified_by_bound_stable_id_when_item_omits_handle(monkeypatch):
    integration = TikTokAPIIntegration(enable_api=False)
    _install_capture(
        monkeypatch,
        integration,
        pages=[_post_page([_item("100", handle=None)], False)],
        identities=[_identity_page()],
        stop_reason="source_exhausted",
    )

    records = asyncio.run(
        integration.discover_creator_profile_posts(
            object(),
            "Creator.Name",
            collect_all=True,
        )
    )

    assert records[0]["username"] == "Creator.Name"
    assert records[0]["creator_id"] == "creator-1"
    assert integration.last_creator_profile_diagnostics[
        "rejected_owner_unverified"
    ] == 0


def test_conflicting_stable_creator_id_is_rejected_instead_of_widening_owner(monkeypatch):
    integration = TikTokAPIIntegration(enable_api=False)
    _install_capture(
        monkeypatch,
        integration,
        pages=[
            _post_page(
                [
                    _item(
                        "100",
                        handle="Creator.Name",
                        creator_id="different-id",
                    )
                ],
                False,
            )
        ],
        identities=[_identity_page()],
        stop_reason="source_exhausted",
    )

    with pytest.raises(
        TikTokCreatorProfileCollectionError,
        match="could not be verified",
    ):
        asyncio.run(
            integration.discover_creator_profile_posts(
                object(),
                "Creator.Name",
                collect_all=True,
            )
        )

    diagnostics = integration.last_creator_profile_diagnostics
    assert diagnostics["rejected_identity_conflict"] == 1
    assert diagnostics["stop_reason"] == (
        "all_profile_rows_rejected_by_owner_fence"
    )


def test_verified_empty_profile_is_distinct_from_missing_inventory(monkeypatch):
    integration = TikTokAPIIntegration(enable_api=False)
    _install_capture(
        monkeypatch,
        integration,
        pages=[_post_page([], False)],
        identities=[_identity_page()],
        stop_reason="source_exhausted",
    )

    records = asyncio.run(
        integration.discover_creator_profile_posts(
            object(),
            "Creator.Name",
            collect_all=True,
        )
    )

    assert records == []
    assert integration.last_creator_profile_diagnostics["inventory_complete"] is True

    missing = TikTokAPIIntegration(enable_api=False)
    _install_capture(
        monkeypatch,
        missing,
        pages=[],
        identities=[_identity_page()],
        stop_reason="pagination_stalled",
    )
    with pytest.raises(
        TikTokCreatorProfileCollectionError,
        match="not a verified empty profile",
    ):
        asyncio.run(
            missing.discover_creator_profile_posts(
                object(),
                "Creator.Name",
                collect_all=True,
            )
        )


def test_unrelated_item_list_terminal_cannot_complete_target_cursor_chain(monkeypatch):
    integration = TikTokAPIIntegration(enable_api=False)
    pages = [
        _post_page([_item("100")], True, next_cursor="20"),
        _post_page(
            [
                _item(
                    "900",
                    handle="OtherCreator",
                    creator_id="other-id",
                    sec_uid="other-secuid",
                )
            ],
            False,
            sec_uid="other-secuid",
        ),
    ]
    _install_capture(
        monkeypatch,
        integration,
        pages=pages,
        identities=[_identity_page()],
        stop_reason="pagination_stalled",
    )

    records = asyncio.run(
        integration.discover_creator_profile_posts(
            object(),
            "Creator.Name",
            collect_all=True,
        )
    )

    assert [record["id"] for record in records] == ["100"]
    diagnostics = integration.last_creator_profile_diagnostics
    assert diagnostics["terminal_verified"] is False
    assert diagnostics["inventory_complete"] is False
    assert diagnostics["cursor_chain"]["unrelated_identity_pages"] == 1
    assert diagnostics["cursor_chain"]["cursor_chain"] == [
        {
            "request_cursor": "0",
            "next_cursor": "20",
            "has_more": True,
            "sequence": 0,
        }
    ]


def test_cursor_gap_and_cycle_never_become_verified_terminal(monkeypatch):
    gap = TikTokAPIIntegration(enable_api=False)
    _install_capture(
        monkeypatch,
        gap,
        pages=[
            _post_page([_item("100")], True, next_cursor="20"),
            _post_page([_item("102")], False, request_cursor="40"),
        ],
        identities=[_identity_page()],
        stop_reason="cursor_gap",
    )
    gap_records = asyncio.run(
        gap.discover_creator_profile_posts(
            object(),
            "Creator.Name",
            collect_all=True,
        )
    )
    assert [record["id"] for record in gap_records] == ["100"]
    assert gap.last_creator_profile_diagnostics["terminal_verified"] is False
    assert gap.last_creator_profile_diagnostics["stop_reason"] == "cursor_gap"

    cycle = TikTokAPIIntegration(enable_api=False)
    _install_capture(
        monkeypatch,
        cycle,
        pages=[
            _post_page([_item("100")], True, next_cursor="20"),
            _post_page(
                [_item("101")],
                True,
                request_cursor="20",
                next_cursor="0",
            ),
            _post_page([_item("102")], False, request_cursor="0"),
        ],
        identities=[_identity_page()],
        stop_reason="cursor_cycle",
    )
    cycle_records = asyncio.run(
        cycle.discover_creator_profile_posts(
            object(),
            "Creator.Name",
            collect_all=True,
        )
    )
    assert [record["id"] for record in cycle_records] == ["100", "101"]
    assert cycle.last_creator_profile_diagnostics["terminal_verified"] is False
    assert cycle.last_creator_profile_diagnostics["stop_reason"] == "cursor_cycle"


def test_missing_request_identity_binding_is_not_a_verified_empty_profile(monkeypatch):
    integration = TikTokAPIIntegration(enable_api=False)
    page = _post_page([], False)
    page["query"].pop("sec_uid")
    page["url"] = (
        "https://www.tiktok.com/api/post/item_list/?cursor=0&count=35"
    )
    _install_capture(
        monkeypatch,
        integration,
        pages=[page],
        identities=[_identity_page()],
        stop_reason="pagination_stalled",
    )

    with pytest.raises(
        TikTokCreatorProfileCollectionError,
        match="target-bound cursor chain",
    ):
        asyncio.run(
            integration.discover_creator_profile_posts(
                object(),
                "Creator.Name",
                collect_all=True,
            )
        )
    diagnostics = integration.last_creator_profile_diagnostics
    assert diagnostics["terminal_verified"] is False
    assert diagnostics["cursor_chain"]["missing_request_binding_pages"] == 1


def test_pinned_abbreviation_is_upgraded_when_duplicate_proves_photo(monkeypatch):
    integration = TikTokAPIIntegration(enable_api=False)
    _install_capture(
        monkeypatch,
        integration,
        pages=[
            _post_page(
                [_item("100", pinned=True)],
                True,
                next_cursor="20",
            ),
            _post_page(
                [_item("100", photo=True)],
                False,
                request_cursor="20",
            ),
        ],
        identities=[_identity_page()],
        stop_reason="source_exhausted",
    )

    records = asyncio.run(
        integration.discover_creator_profile_posts(
            object(),
            "Creator.Name",
            collect_all=True,
        )
    )

    assert len(records) == 1
    assert records[0]["is_pinned"] is True
    assert records[0]["content_type"] == "photo"
    assert records[0]["url"].endswith("/@Creator.Name/photo/100")
    assert records[0]["visual_slide_count"] == 1
    assert records[0]["visual_evidence_status"] == "unavailable"


@pytest.mark.parametrize("fresh_item_has_photo_structure", [True, False])
def test_direct_html_refresh_preserves_or_detects_photo_media_type(
    fresh_item_has_photo_structure,
):
    post_id = "700"
    item = _item(
        post_id,
        handle="Creator.Name",
        photo=fresh_item_has_photo_structure,
        caption="",
    )
    if fresh_item_has_photo_structure:
        item["imagePost"]["images"].append(
            {"imageURL": {"urlList": []}}
        )
    payload = {
        "__DEFAULT_SCOPE__": {
            "webapp.video-detail": {"itemInfo": {"itemStruct": item}}
        }
    }
    html = (
        '<html><script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">'
        + json.dumps(payload)
        + "</script></html>"
    )

    class FakeResponse:
        ok = True

        async def text(self):
            return html

    class FakeRequest:
        async def get(self, url, **_kwargs):
            assert url == (
                "https://www.tiktok.com/@Creator.Name/photo/700"
            )
            return FakeResponse()

    candidate = {
        "id": post_id,
        "url": "https://www.tiktok.com/@Creator.Name/photo/700",
        "username": "Creator.Name",
        "content_type": "photo",
        "visual_slide_count": 1,
        "visual_evidence_status": "unavailable",
    }
    integration = TikTokAPIIntegration(enable_api=False)
    stats = asyncio.run(
        integration.refresh_video_candidates_from_html(
            SimpleNamespace(request=FakeRequest()),
            [candidate],
        )
    )

    assert stats["hydrated"] == 1
    assert candidate["metadata_refresh_ok"] is True
    assert candidate["content_type"] == "photo"
    assert candidate["url"] == (
        "https://www.tiktok.com/@Creator.Name/photo/700"
    )
    assert candidate["visual_slide_count"] == (
        2 if fresh_item_has_photo_structure else 1
    )
    assert candidate["visual_evidence_status"] == "unavailable"
    assert candidate["visual_evidence_terminal"] is True
    assert candidate["creator_user_id"] == "creator-1"
    assert candidate["creator_sec_uid"] == "sec-creator-1"


class _FakeResponse:
    def __init__(self, url, payload):
        self.url = url
        self.status = 200
        self._payload = payload

    async def json(self):
        return self._payload


class _FakeMouse:
    def __init__(self, page):
        self.page = page

    async def wheel(self, _x, _y):
        self.page.scrolls += 1
        if self.page.scrolls == 1:
            self.page.emit(
                _FakeResponse(
                    "https://www.tiktok.com/api/post/item_list/"
                    "?secUid=sec-creator-1&cursor=1&msToken=secret",
                    _post_page(
                        [_item("101")],
                        False,
                        request_cursor="1",
                    )["data"],
                )
            )
            await asyncio.sleep(0)


class _FakePage:
    def __init__(self):
        self.listener = None
        self.listener_removed = False
        self.scrolls = 0
        self.mouse = _FakeMouse(self)

    def on(self, event, callback):
        assert event == "response"
        self.listener = callback

    def remove_listener(self, event, callback):
        assert event == "response"
        assert callback is self.listener
        self.listener_removed = True

    def emit(self, response):
        self.listener(response)

    async def goto(self, url, **_kwargs):
        assert url == "https://www.tiktok.com/@Creator.Name"
        self.emit(
            _FakeResponse(
                "https://www.tiktok.com/api/user/detail/?uniqueId=Creator.Name",
                _identity_page()["data"],
            )
        )
        self.emit(
            _FakeResponse(
                "https://www.tiktok.com/api/post/item_list/"
                "?secUid=sec-creator-1&cursor=0&msToken=secret",
                _post_page(
                    [_item("100")],
                    True,
                    next_cursor="1",
                )["data"],
            )
        )
        await asyncio.sleep(0)

    async def evaluate(self, _script):
        return None


def test_profile_capture_uses_page_scroll_and_stops_on_real_terminal_response():
    integration = TikTokAPIIntegration(enable_api=False)
    page = _FakePage()

    captured = asyncio.run(
        integration._capture_creator_profile_api_pages(
            page,
            handle="Creator.Name",
            profile_url="https://www.tiktok.com/@Creator.Name",
            target_count=None,
            collect_all=True,
            max_pages=None,
        )
    )

    assert len(captured["post_pages"]) == 2
    assert captured["capture_diagnostics"]["terminal_response_observed"] is True
    assert captured["capture_diagnostics"]["stop_reason"] == "source_exhausted"
    assert page.scrolls == 1
    assert page.listener_removed is True
    assert "secret" not in repr(captured["capture_diagnostics"])
    assert "secret" not in repr(captured["post_pages"])
    assert captured["post_pages"][0]["query"] == {
        "sec_uid": "sec-creator-1",
        "cursor": "0",
    }
    assert captured["post_pages"][0]["url"] == (
        "https://www.tiktok.com/api/post/item_list/"
        "?secUid=sec-creator-1&cursor=0"
    )
    assert captured["capture_diagnostics"]["cursor_chain"] == [
        {
            "request_cursor": "0",
            "next_cursor": "1",
            "has_more": True,
            "sequence": 2,
        },
        {
            "request_cursor": "1",
            "next_cursor": "1",
            "has_more": False,
            "sequence": 3,
        },
    ]
