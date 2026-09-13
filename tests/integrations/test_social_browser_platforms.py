"""Synthetic platform checks; no browser, credentials, or runtime state needed."""

import asyncio
import json
from urllib.parse import urlparse

import pytest

import social_browser


class ScopedCookieContext:
    def __init__(self, cookies):
        self.stored_cookies = cookies

    async def cookies(self, urls):
        hosts = [(urlparse(url).hostname or "") for url in urls]
        return [
            cookie
            for cookie in self.stored_cookies
            if any(
                host == cookie["domain"].lstrip(".")
                or (
                    cookie["domain"].startswith(".")
                    and host.endswith(cookie["domain"])
                )
                for host in hosts
            )
        ]


@pytest.mark.parametrize(
    "platform,domain,cookie_name",
    [
        ("threads", ".threads.com", "sessionid"),
        ("threads", ".threads.net", "sessionid"),
        ("linkedin", ".linkedin.com", "li_at"),
        ("linkedin", ".www.linkedin.com", "li_at"),
    ],
)
def test_new_platform_session_indicators_are_scoped_and_value_free(
    platform, domain, cookie_name
):
    context = ScopedCookieContext(
        [{"domain": domain, "name": cookie_name, "value": "synthetic-secret"}]
    )

    result = asyncio.run(social_browser.platform_authentication(context, platform))

    assert result == {
        "authenticated": True,
        "auth_cookie_names_present": [cookie_name],
    }
    assert "synthetic-secret" not in json.dumps(result)


@pytest.mark.parametrize("platform", ["threads", "linkedin"])
def test_other_platform_cookies_do_not_establish_new_platform_login(platform):
    context = ScopedCookieContext(
        [
            {"domain": ".instagram.com", "name": "sessionid", "value": "synthetic-ig"},
            {"domain": ".example.com", "name": "li_at", "value": "synthetic-other"},
        ]
    )

    result = asyncio.run(social_browser.platform_authentication(context, platform))

    assert result == {"authenticated": False, "auth_cookie_names_present": []}


@pytest.mark.parametrize(
    "platform,domain,cookie_name",
    [("threads", ".threads.com", "csrftoken"), ("linkedin", ".linkedin.com", "liap")],
)
def test_non_session_cookie_does_not_establish_login(platform, domain, cookie_name):
    context = ScopedCookieContext(
        [{"domain": domain, "name": cookie_name, "value": "synthetic-non-session"}]
    )

    result = asyncio.run(social_browser.platform_authentication(context, platform))

    assert result == {"authenticated": False, "auth_cookie_names_present": []}


def test_status_renders_every_registered_platform_without_session_values(monkeypatch, capsys):
    monkeypatch.setitem(
        social_browser.AUTH_COOKIE_RULES,
        "synthetic_platform",
        {"urls": ["https://example.com/"], "required_all": ["synthetic_session"]},
    )
    payload = {
        "reachable": True,
        "cdp_url": "ws://127.0.0.1:9223/devtools/browser/synthetic-session-uuid",
        "profile": {"verified": True, "expected_profile_path": "synthetic/Profile 7"},
        "platforms": {
            "threads": {"authenticated": True, "auth_cookie_names_present": ["sessionid"]},
            "linkedin": {"authenticated": True, "auth_cookie_names_present": ["li_at"]},
        },
    }

    social_browser.print_status(payload, as_json=False)

    output = capsys.readouterr().out
    for platform in social_browser.AUTH_COOKIE_RULES:
        assert f"[STATUS] {platform}:" in output
    assert "threads: logged in (auth cookies present: sessionid)" in output
    assert "linkedin: logged in (auth cookies present: li_at)" in output
    assert "synthetic_platform: login needed (auth cookies present: none)" in output
    assert "synthetic-session-uuid" not in output
