from __future__ import annotations

import asyncio
import json

import pytest

from tiktok_scraper import top_content_browser as top_content


SOURCE = "https://ads.tiktok.com/creative/forpartners/creator/top-content?region=row"


class _CountLocator:
    def __init__(self, count: int):
        self._count = count

    async def count(self):
        return self._count


class _TextItem:
    def __init__(self, page, label: str):
        self.page = page
        self.label = label

    async def is_visible(self):
        return (
            self.page.menu_open
            or self.label == top_content.RANKING_LABELS[self.page.current_ranking]
        )

    async def click(self):
        self.page.clicks.append(f"ranking:{self.label}")
        if self.label == top_content.RANKING_LABELS[self.page.current_ranking]:
            self.page.menu_open = True
            return
        if not self.page.menu_open:
            raise AssertionError("ranking option clicked while menu was closed")
        for key, value in top_content.RANKING_LABELS.items():
            if value == self.label:
                self.page.current_ranking = key
                self.page.menu_open = False
                return
        raise AssertionError("unknown ranking label")


class _TextLocator:
    def __init__(self, page, label: str):
        self.page = page
        self.label = label

    async def count(self):
        return 1

    def nth(self, _index: int):
        return _TextItem(self.page, self.label)


class _CardItem:
    def __init__(self, page):
        self.page = page

    async def click(self):
        self.page.clicks.append("view-details")
        self.page.modal_position = 0


class _CardLocator:
    def __init__(self, page):
        self.page = page

    @property
    def first(self):
        return _CardItem(self.page)

    async def count(self):
        return min(
            self.page.loaded[self.page.current_ranking],
            len(self.page.rows[self.page.current_ranking]),
        )


class _AnchorItem:
    def __init__(self, page):
        self.page = page

    async def get_attribute(self, name: str):
        assert name == "href"
        rows = self.page.rows[self.page.current_ranking]
        if self.page.modal_position is None:
            return ""
        return rows[self.page.modal_position]


class _AnchorLocator:
    def __init__(self, page):
        self.page = page

    @property
    def first(self):
        return _AnchorItem(self.page)

    async def count(self):
        return int(self.page.modal_position is not None)


class _PaginationItem:
    def __init__(self, page, direction: str):
        self.page = page
        self.direction = direction

    async def get_attribute(self, name: str):
        if name == "aria-label":
            return f"{self.direction} page"
        if name == "aria-disabled":
            return "false"
        return ""

    def locator(self, _selector: str):
        return _CountLocator(1 if self.direction == "right" else 0)

    async def click(self):
        self.page.clicks.append(f"pagination:{self.direction}")
        if self.direction != "right":
            raise AssertionError("left pagination was clicked")
        assert self.page.modal_position is not None
        self.page.modal_position += 1


class _PaginationLocator:
    def __init__(self, page):
        self.page = page

    async def count(self):
        return 2

    def nth(self, index: int):
        return _PaginationItem(self.page, "left" if index == 0 else "right")


class _CloseItem:
    def __init__(self, page):
        self.page = page

    async def click(self):
        self.page.clicks.append("modal-close")
        self.page.modal_position = None


class _CloseLocator:
    def __init__(self, page):
        self.page = page

    @property
    def first(self):
        return _CloseItem(self.page)

    async def count(self):
        return 1


class _EmptyLocator:
    @property
    def first(self):
        raise AssertionError("empty locator has no first item")

    async def count(self):
        return 0


class FakePage:
    def __init__(self, rows, *, initial_load=20):
        self.rows = rows
        self.current_ranking = next(iter(rows))
        self.loaded = {
            ranking: min(initial_load, len(values)) for ranking, values in rows.items()
        }
        self.menu_open = False
        self.modal_position = None
        self.clicks = []
        self.closed = False
        self.gotos = []
        self.route_actions = []

    def get_by_text(self, label: str, exact: bool):
        assert exact is True
        return _TextLocator(self, label)

    def locator(self, selector: str):
        if selector == top_content.TOP_CONTENT_CARD_SELECTOR:
            return _CardLocator(self)
        if selector == top_content.TOP_CONTENT_LINK_SELECTOR:
            return _AnchorLocator(self)
        if selector == top_content.TOP_CONTENT_PAGINATION_SELECTOR:
            return _PaginationLocator(self)
        if selector == top_content.TOP_CONTENT_CLOSE_SELECTOR:
            return _CloseLocator(self)
        return _EmptyLocator()

    async def evaluate(self, script: str):
        if "TOP_CONTENT_FILTER_SNAPSHOT" in script:
            return {
                "country": "Indonesia",
                "period": "Last 30 days",
                "selected_content_tags": ["Beauty", "Food"],
                "organic_only": True,
                "last_updated_label": "Last updated yesterday",
                "cookie": "cookie-secret",
                "access_token": "token-secret",
                "raw_html": "html-secret",
            }
        if "TOP_CONTENT_SCROLL" in script:
            ranking = self.current_ranking
            self.loaded[ranking] = min(
                len(self.rows[ranking]), self.loaded[ranking] + 20
            )
        return None

    async def wait_for_timeout(self, _milliseconds: int):
        return None

    async def route(self, pattern: str, handler):
        assert pattern == "**/*"

        class Request:
            def __init__(self, resource_type):
                self.resource_type = resource_type

        class Route:
            def __init__(self, outer, resource_type):
                self.request = Request(resource_type)
                self.outer = outer

            async def abort(self):
                self.outer.route_actions.append((self.request.resource_type, "abort"))

            async def continue_(self):
                self.outer.route_actions.append(
                    (self.request.resource_type, "continue")
                )

        for resource_type in ("document", "script", "xhr", "media", "image", "font"):
            await handler(Route(self, resource_type))

    async def goto(self, url: str, **_kwargs):
        self.gotos.append(url)

    def is_closed(self):
        return self.closed

    async def close(self):
        self.closed = True


def _video(video_id: int, creator: str = "creator") -> str:
    return f"https://www.tiktok.com/@{creator}/video/{video_id}?lang=en"


def test_source_url_normalization_is_strict():
    assert top_content.normalize_top_content_source_url(SOURCE) == (SOURCE, "row")
    assert top_content.normalize_top_content_source_url(
        "https://ads.tiktok.com/creative/forpartners/creator/top-content/?region=ROW"
    ) == (SOURCE, "row")
    assert top_content.normalize_top_content_source_url(
        "https://ads.tiktok.com:443/creative/forpartners/creator/top-content"
    ) == (SOURCE, "row")

    invalid = (
        "http://ads.tiktok.com/creative/forpartners/creator/top-content?region=row",
        "https://evil.example/creative/forpartners/creator/top-content?region=row",
        "https://ads.tiktok.com/creative/forpartners/home?region=row",
        SOURCE + "&filter=all",
        SOURCE + "#invite",
        "https://user@ads.tiktok.com/creative/forpartners/creator/top-content?region=row",
    )
    for value in invalid:
        with pytest.raises(top_content.TopContentBrowserError):
            top_content.normalize_top_content_source_url(value)


def test_tiktok_link_normalization_accepts_only_canonical_video_identity():
    assert top_content.normalize_tiktok_video_url(
        "https://tiktok.com/@A_creator/video/12345?lang=en"
    ) == {
        "video_id": "12345",
        "creator": "A_creator",
        "url": "https://www.tiktok.com/@A_creator/video/12345",
    }
    assert (
        top_content.normalize_tiktok_video_url(
            "https://www.tiktok.com/@creator/photo/12345"
        )
        is None
    )
    assert (
        top_content.normalize_tiktok_video_url(
            "https://example.com/@creator/video/12345"
        )
        is None
    )
    assert (
        top_content.normalize_tiktok_video_url(
            "https://user@www.tiktok.com/@creator/video/12345"
        )
        is None
    )


def test_page_discovery_preserves_ranking_order_dedupes_and_skips_exclusions():
    rows = {
        "video_views": [_video(index, "views") for index in range(1, 101)],
        "engagement": [
            _video(1, "views"),
            _video(201, "engage"),
            *[_video(index, "engage") for index in range(202, 301)],
        ],
    }
    page = FakePage(rows)
    result = asyncio.run(
        top_content._discover_from_page(
            page,
            normalized_source_url=SOURCE,
            region="row",
            requested_count=102,
            rankings=("video_views", "engagement"),
            excluded_ids={"2", "201"},
            account_binding_hash="a" * 64,
        )
    )

    assert len(result["links"]) == 102
    assert [row["ordinal"] for row in result["links"]] == list(range(1, 103))
    assert result["links"][0]["video_id"] == "1"
    assert result["links"][98]["video_id"] == "100"
    assert result["links"][99] == {
        "ordinal": 100,
        "video_id": "202",
        "url": "https://www.tiktok.com/@engage/video/202",
        "creator": "engage",
        "ranking": "engagement",
        "ranking_position": 3,
    }
    assert result["rankings_exhausted"] == ["video_views"]
    assert result["frontier_verified"] is True
    assert result["list_snapshot"]["excluded_video_ids"] == 2
    assert result["list_snapshot"]["duplicate_video_ids"] == 1
    assert "excluded_video_ids:2" in result["reasons"]
    assert "duplicate_video_ids:1" in result["reasons"]
    assert page.clicks.count("view-details") == 2
    assert page.clicks.count("modal-close") == 2
    assert not any(
        forbidden in click.casefold()
        for click in page.clicks
        for forbidden in ("invite", "bookmark", "save", "contact")
    )


def test_stable_stall_below_top_100_is_not_a_verified_frontier():
    page = FakePage({"video_views": [_video(index) for index in range(1, 41)]})
    result = asyncio.run(
        top_content._discover_from_page(
            page,
            normalized_source_url=SOURCE,
            region="row",
            requested_count=50,
            rankings=("video_views",),
            excluded_ids=set(),
            account_binding_hash="b" * 64,
        )
    )

    assert len(result["links"]) == 40
    assert result["frontier_verified"] is False
    assert result["rankings_exhausted"] == []
    observation = result["list_snapshot"]["ranking_observations"][0]
    assert observation["loaded_cards"] == 40
    assert observation["card_frontier_verified"] is False
    assert observation["reason"] == "stable_card_stall_below_100"
    assert "unverified_frontier_before_requested_count" in result["reasons"]


def test_authenticated_profile7_wrapper_closes_only_temporary_page_and_sanitizes(
    tmp_path,
    monkeypatch,
    capsys,
):
    rows = {
        "video_views": [_video(index) for index in range(1, 101)],
        "engagement": [_video(index) for index in range(101, 201)],
        "six_second_views": [_video(index) for index in range(201, 301)],
    }
    page = FakePage(rows)
    lifecycle = []

    class FakeContext:
        async def new_page(self):
            lifecycle.append("context.new_page")
            return page

        async def close(self):
            lifecycle.append("context.close")

    context = FakeContext()

    class FakeBrowser:
        async def close(self):
            lifecycle.append("browser.close")

    browser = FakeBrowser()

    class FakeChromium:
        async def connect_over_cdp(self, value):
            lifecycle.append("connect_over_cdp")
            assert value == "ws://127.0.0.1:9223/devtools/browser/profile7"
            return browser

    class FakePlaywright:
        async def __aenter__(self):
            lifecycle.append("playwright.enter")
            return type("Playwright", (), {"chromium": FakeChromium()})()

        async def __aexit__(self, *_args):
            lifecycle.append("playwright.exit")
            assert page.closed is True
            return False

    import engage_tiktok
    import playwright.async_api
    import social_browser

    monkeypatch.setattr(
        playwright.async_api, "async_playwright", lambda: FakePlaywright()
    )

    def ensure_profile7(runtime_dir, **_kwargs):
        print("cookie-secret token-secret")
        return {
            "designation": {
                "profile_directory": "Profile 7",
                "runtime_dir": str(runtime_dir),
            }
        }

    monkeypatch.setattr(
        social_browser,
        "ensure_engage_profile7_browser",
        ensure_profile7,
    )
    monkeypatch.setattr(
        social_browser,
        "load_verified_profile7_state",
        lambda _runtime_dir, _designation: {
            "cdp_url": "ws://127.0.0.1:9223/devtools/browser/profile7"
        },
    )

    async def verified(received_browser, designation):
        assert received_browser is browser
        assert designation["profile_directory"] == "Profile 7"
        return context, {"verified": True}

    async def authenticated(received_context, platform):
        assert received_context is context
        assert platform == "tiktok"
        return {"authenticated": True}

    async def account(received_page, **_kwargs):
        assert received_page is page
        return "Bound.Account"

    monkeypatch.setattr(social_browser, "verified_profile_context", verified)
    monkeypatch.setattr(social_browser, "platform_authentication", authenticated)
    monkeypatch.setattr(engage_tiktok, "active_tiktok_account", account)

    result = asyncio.run(
        top_content.discover_top_content_links(
            SOURCE,
            2,
            social_browser_runtime_dir=tmp_path,
            expected_account="@bound.account",
        )
    )

    assert len(result["links"]) == 2
    assert result["account_binding_hash"] != "bound.account"
    assert len(result["account_binding_hash"]) == 64
    serialized = json.dumps(result)
    assert "bound.account" not in serialized.casefold()
    assert "cookie-secret" not in serialized
    assert "token-secret" not in serialized
    assert "html-secret" not in serialized
    captured = capsys.readouterr()
    assert "cookie-secret" not in captured.out
    assert "token-secret" not in captured.out
    assert "cookie-secret" not in captured.err
    assert "token-secret" not in captured.err
    assert page.gotos == ["https://www.tiktok.com/", SOURCE]
    assert page.closed is True
    assert "browser.close" not in lifecycle
    assert "context.close" not in lifecycle
    assert page.route_actions == [
        ("document", "continue"),
        ("script", "continue"),
        ("xhr", "continue"),
        ("media", "abort"),
        ("image", "abort"),
        ("font", "abort"),
    ]
    assert not any(
        forbidden in click.casefold()
        for click in page.clicks
        for forbidden in ("invite", "bookmark", "save", "contact")
    )


def test_argument_validation_fails_before_browser_use():
    for count in (0, -1, True, 1.5):
        with pytest.raises(top_content.TopContentBrowserError):
            asyncio.run(top_content.discover_top_content_links(SOURCE, count))
    with pytest.raises(top_content.TopContentBrowserError):
        asyncio.run(
            top_content.discover_top_content_links(
                SOURCE,
                1,
                rankings=("unknown",),
            )
        )
    with pytest.raises(top_content.TopContentBrowserError):
        asyncio.run(
            top_content.discover_top_content_links(
                SOURCE,
                1,
                exclude_video_ids={"not-a-video-id"},
            )
        )
