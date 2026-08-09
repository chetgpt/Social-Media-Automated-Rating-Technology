import asyncio

import pytest

from tiktok_scraper.scrapers.x_scraper import XAPIError, XScraper


class FakeResponse:
    def __init__(self, payload, status_code=200, headers=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.text = ""

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, *, headers, params, timeout):
        self.calls.append({"url": url, "headers": headers, "params": dict(params or {}), "timeout": timeout})
        if not self.responses:
            raise AssertionError("unexpected X API request")
        return self.responses.pop(0)


def post(post_id, author_id, text, *, parent_id="", reply_count=0, created_at="2026-07-14T08:00:00.000Z"):
    payload = {
        "id": post_id,
        "author_id": author_id,
        "conversation_id": "100",
        "created_at": created_at,
        "text": text,
        "lang": "id",
        "public_metrics": {
            "like_count": 4,
            "reply_count": reply_count,
            "retweet_count": 2,
            "quote_count": 1,
            "bookmark_count": 3,
            "impression_count": 90,
        },
    }
    if parent_id:
        payload["referenced_tweets"] = [{"type": "replied_to", "id": parent_id}]
    return payload


def user(user_id, username):
    return {
        "id": user_id,
        "username": username,
        "name": username.title(),
        "location": "Jakarta",
        "verified": False,
        "public_metrics": {"followers_count": 120},
    }


def test_x_search_uses_official_api_pagination_and_normalizes_metadata(monkeypatch):
    monkeypatch.delenv("SCRAPER_DATE_WINDOWS_JSON", raising=False)
    session = FakeSession([
        FakeResponse({
            "data": [post("100", "u1", "Campaign launch"), post("101", "u2", "Campaign trailer")],
            "includes": {"users": [user("u1", "official"), user("u2", "publisher")]},
            "meta": {"next_token": "NEXT"},
        }),
        FakeResponse({
            "data": [post("102", "u1", "Campaign interview")],
            "includes": {"users": [user("u1", "official")]},
            "meta": {},
        }),
    ])
    scraper = XScraper(bearer_token="test-token", session=session, search_mode="recent")

    results = asyncio.run(scraper.search("Campaign", max_videos=3))

    assert [item["video_id"] for item in results] == ["100", "101", "102"]
    assert results[0]["url"] == "https://x.com/official/status/100"
    assert results[0]["creator_profile_location"] == "Jakarta"
    assert results[0]["view_count"] == 90
    assert results[0]["reported_comment_count"] == 0
    assert results[0]["discovery_method"] == "x_api_v2_recent_search"
    assert session.calls[0]["params"]["query"] == "Campaign -is:reply -is:retweet"
    assert session.calls[1]["params"]["next_token"] == "NEXT"
    assert scraper.usage_summary()["post_reads"] == 3


def test_x_account_url_becomes_from_operator(monkeypatch):
    monkeypatch.delenv("SCRAPER_DATE_WINDOWS_JSON", raising=False)
    session = FakeSession([FakeResponse({"meta": {}})])
    scraper = XScraper(bearer_token="test-token", session=session)

    assert asyncio.run(scraper.search("https://twitter.com/official", max_videos=2)) == []
    assert session.calls[0]["params"]["query"] == "from:official -is:reply -is:retweet"


def test_x_conversation_search_preserves_nested_reply_tree(monkeypatch):
    monkeypatch.delenv("SCRAPER_DATE_WINDOWS_JSON", raising=False)
    session = FakeSession([
        FakeResponse({
            "data": [
                post("202", "u3", "Nested reply", parent_id="201", created_at="2026-07-14T08:02:00.000Z"),
                post("201", "u2", "Direct reply", parent_id="100", reply_count=1, created_at="2026-07-14T08:01:00.000Z"),
            ],
            "includes": {"users": [user("u2", "reader"), user("u3", "second_reader")]},
            "meta": {},
        })
    ])
    scraper = XScraper(bearer_token="test-token", session=session, search_mode="all")

    result = asyncio.run(scraper.extract_comments({
        "video_id": "100",
        "url": "https://x.com/official/status/100",
        "title": "Root post",
        "username": "official",
        "published_at": "2026-06-01T00:00:00Z",
        "reported_comment_count": 2,
    }, max_comments=0))

    assert result["comment_method"] == "x_api_v2_conversation_search"
    assert result["comment_history_scope"] == "full_archive"
    assert result["comments_seen_in_response"] == 2
    assert result["comments_exhausted"] is True
    assert len(result["comments"]) == 1
    assert result["comments"][0]["comment_id"] == "201"
    assert result["comments"][0]["parent_comment_id"] == ""
    assert result["comments"][0]["replies"][0]["comment_id"] == "202"
    assert result["comments"][0]["replies"][0]["parent_comment_id"] == "201"


def test_x_missing_credentials_fails_before_network(monkeypatch):
    monkeypatch.delenv("X_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("TWITTER_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("X_BEARER_TOKEN_FILE", raising=False)
    monkeypatch.delenv("TWITTER_BEARER_TOKEN_FILE", raising=False)
    session = FakeSession([])
    scraper = XScraper(session=session)

    with pytest.raises(XAPIError, match="credentials are missing"):
        asyncio.run(scraper.search("Campaign", max_videos=2))
    assert session.calls == []


def test_x_recent_search_rejects_entirely_historical_window(monkeypatch):
    monkeypatch.setenv(
        "SCRAPER_DATE_WINDOWS_JSON",
        '[{"start":"2020-01-01T00:00:00Z","end":"2020-01-02T00:00:00Z"}]',
    )
    session = FakeSession([])
    scraper = XScraper(bearer_token="test-token", session=session, search_mode="recent")

    with pytest.raises(XAPIError, match="older than X recent search"):
        asyncio.run(scraper.search("Campaign", max_videos=2))
    assert session.calls == []
