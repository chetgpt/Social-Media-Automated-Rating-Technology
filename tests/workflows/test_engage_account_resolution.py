"""Account-resolution timeouts never become invented authenticated handles."""
import asyncio
import pytest

from engage_tiktok import active_tiktok_account, _tiktok_handle_from_profile_href


class EmptyLocator:
    async def count(self):
        return 0


class PageWithoutProfile:
    def locator(self, selector):
        return EmptyLocator()

    async def evaluate(self, script):
        return ""


def test_unresolved_account_is_empty_and_cannot_satisfy_identity_gate():
    result = asyncio.run(active_tiktok_account(PageWithoutProfile(), timeout_ms=0))
    assert result == ""
    assert not result


class HandleLocator:
    async def count(self): return 1
    def nth(self, index): return self
    async def get_attribute(self, name): return "/@kitascore"
    async def is_visible(self): return True


class HomePage(PageWithoutProfile):
    def __init__(self): self.closed = False
    async def goto(self, url, **kwargs): self.url = url
    def locator(self, selector): return HandleLocator()
    def is_closed(self): return self.closed
    async def close(self): self.closed = True


def test_post_without_navigation_checks_same_context_home_and_closes_only_its_tab():
    home = HomePage()
    class Context:
        async def new_page(self): return home
    page = PageWithoutProfile()
    page.context = Context()
    page.url = "https://www.tiktok.com/@creator/video/123"
    assert asyncio.run(active_tiktok_account(page, timeout_ms=0)) == "kitascore"
    assert home.closed
    assert page.url.endswith("/video/123")


@pytest.mark.parametrize('url', [
    'https://evil.test/@kitascore', 'https://www.tiktok.com.evil.test/@kitascore',
    'https://secret@www.tiktok.com/@kitascore', '/@creator/video/123',
    'prefix/@kitascore', 'javascript:/@kitascore', 'http://www.tiktok.com/@kitascore',
])
def test_account_identity_requires_exact_tiktok_profile_url(url):
    assert _tiktok_handle_from_profile_href(url) == ''


@pytest.mark.parametrize('url', ['/@KitaScore', 'https://www.tiktok.com/@kitascore', 'https://tiktok.com/@kitascore/'])
def test_canonical_account_profile_urls_are_accepted(url):
    assert _tiktok_handle_from_profile_href(url) == 'kitascore'


def test_hidden_profile_and_ordinary_creator_links_are_not_account_identity():
    class Hidden(HandleLocator):
        async def is_visible(self): return False
    class Page(PageWithoutProfile):
        def locator(self, selector):
            assert 'header a' not in selector and 'aria-label' not in selector
            return Hidden()
        async def evaluate(self, script):
            assert 'header a' not in script and 'aria-label' not in script
            return ''
    assert asyncio.run(active_tiktok_account(Page(), timeout_ms=0, allow_home_fallback=False)) == ''


def test_home_fallback_does_not_recurse_when_identity_is_still_unavailable():
    class MissingHome(PageWithoutProfile):
        closed = False
        async def goto(self, url, **kwargs): self.url = url
        def is_closed(self): return self.closed
        async def close(self): self.closed = True
    home = MissingHome()
    class Context:
        calls = 0
        async def new_page(self):
            self.calls += 1
            return home
    page = PageWithoutProfile()
    page.context = Context()
    page.url = "https://www.tiktok.com/@creator/video/123"
    assert asyncio.run(active_tiktok_account(page, timeout_ms=0)) == ""
    assert page.context.calls == 1
    assert home.closed
