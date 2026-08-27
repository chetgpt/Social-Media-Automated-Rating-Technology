from tiktok_scraper.scrapers.x_browser_scraper import XBrowserScraper
from tiktok_scraper.scrapers.x_scraper import XScraper


def graphql_tweet(
    post_id,
    text,
    *,
    username="official",
    parent_id="",
    conversation_id="100",
    created_at="Tue Jul 14 08:00:00 +0000 2026",
):
    legacy = {
        "id_str": post_id,
        "full_text": text,
        "created_at": created_at,
        "conversation_id_str": conversation_id,
        "favorite_count": 4,
        "retweet_count": 2,
        "quote_count": 1,
        "bookmark_count": 3,
        "reply_count": 2,
        "lang": "id",
        "entities": {"hashtags": [{"text": "Campaign"}]},
        "extended_entities": {
            "media": [{
                "id_str": "m1",
                "type": "video",
                "media_url_https": "https://pbs.twimg.com/media/example.jpg",
                "expanded_url": f"https://x.com/{username}/status/{post_id}/video/1",
                "original_info": {"width": 1280, "height": 720},
                "video_info": {"duration_millis": 12000, "variants": []},
            }]
        },
    }
    if parent_id:
        legacy["in_reply_to_status_id_str"] = parent_id
        legacy["in_reply_to_user_id_str"] = "u1"
    return {
        "__typename": "Tweet",
        "rest_id": post_id,
        "core": {
            "user_results": {
                "result": {
                    "__typename": "User",
                    "rest_id": "u1",
                    "is_blue_verified": True,
                    "core": {"name": "Official Account", "screen_name": username},
                    "legacy": {
                        "name": "Official Account",
                        "screen_name": username,
                        "location": "Jakarta",
                        "followers_count": 321,
                    },
                }
            }
        },
        "legacy": legacy,
        "note_tweet": {
            "note_tweet_results": {
                "result": {
                    "text": f"{text} with note text",
                    "entity_set": {"urls": []},
                }
            }
        },
        "views": {"count": "99"},
    }


def timeline_payload(*tweets, bottom_cursor=True):
    entries = [
        {
            "entryId": f"tweet-{tweet['rest_id']}",
            "content": {"itemContent": {"tweet_results": {"result": tweet}}},
        }
        for tweet in tweets
    ]
    if bottom_cursor:
        entries.append({
            "entryId": "cursor-bottom-1",
            "content": {"cursorType": "Bottom", "value": "NEXT"},
        })
    return {
        "data": {
            "search_by_raw_query": {
                "search_timeline": {
                    "timeline": {"instructions": [{"entries": entries}]}
                }
            }
        }
    }


def test_x_browser_graphql_parser_normalizes_post_and_provenance():
    scraper = XBrowserScraper()
    payload = timeline_payload(graphql_tweet("100", "Short text"))
    output = {}

    added = scraper.collect_candidates(
        payload,
        output,
        discovery_method="x_search_timeline_graphql",
    )

    assert added == 1
    candidate = output["100"]
    assert candidate["url"] == "https://x.com/official/status/100"
    assert candidate["caption"] == "Short text with note text"
    assert candidate["creator_profile_location"] == "Jakarta"
    assert candidate["follower_count"] == 321
    assert candidate["view_count"] == "99"
    assert candidate["content_type"] == "x_video_post"
    assert candidate["metadata_method"] == "x_graphql_response"
    assert candidate["discovery_method"] == "x_search_timeline_graphql"
    assert scraper._has_bottom_cursor(payload) is True


def test_x_browser_target_url_keeps_keywords_profiles_search_urls_and_ids_distinct():
    assert XBrowserScraper._target_url("pharmacy") == (
        "https://x.com/search?q=pharmacy&src=typed_query&f=live"
    )
    assert XBrowserScraper._target_url("@pharmacy") == "https://x.com/pharmacy"
    assert XBrowserScraper._target_url("https://x.com/pharmacy") == "https://x.com/pharmacy"
    assert XBrowserScraper._target_url("https://x.com/search?q=pharmacy&f=live") == (
        "https://x.com/search?q=pharmacy&f=live"
    )
    assert XBrowserScraper._target_url("1234567890") == "https://x.com/i/status/1234567890"


def test_x_browser_tweet_detail_rebuilds_nested_reply_tree():
    direct = graphql_tweet(
        "201",
        "Direct reply",
        username="reader",
        parent_id="100",
        created_at="Tue Jul 14 08:01:00 +0000 2026",
    )
    nested = graphql_tweet(
        "202",
        "Nested reply",
        username="second_reader",
        parent_id="201",
        created_at="Tue Jul 14 08:02:00 +0000 2026",
    )
    payload = timeline_payload(graphql_tweet("100", "Root"), direct, nested, bottom_cursor=False)
    comments = [
        XBrowserScraper.tweet_to_comment(tweet, "100")
        for tweet in XBrowserScraper._tweet_results(payload)
        if tweet["rest_id"] != "100"
    ]

    tree = XScraper._nest_comments(comments, "100")

    assert len(tree) == 1
    assert tree[0]["comment_id"] == "201"
    assert tree[0]["parent_comment_id"] == ""
    assert tree[0]["replies"][0]["comment_id"] == "202"
    assert tree[0]["replies"][0]["parent_comment_id"] == "201"
    assert XBrowserScraper._has_bottom_cursor(payload) is False


def test_x_browser_operation_detection_and_zero_official_api_cost():
    url = "https://x.com/i/api/graphql/query-id/SearchTimeline?variables=%7B%7D"
    scraper = XBrowserScraper()
    scraper.graphql_response_count = 3
    scraper.graphql_operations.update({"SearchTimeline", "TweetDetail"})

    assert scraper._operation_name(url) == "SearchTimeline"
    assert scraper._is_graphql_response(url, {"SearchTimeline"}) is True
    assert scraper.usage_summary() == {
        "transport": "authenticated_web_graphql",
        "official_api": False,
        "official_api_post_reads": 0,
        "estimated_official_api_cost_usd": 0.0,
        "graphql_response_count": 3,
        "graphql_operations": ["SearchTimeline", "TweetDetail"],
    }


def test_x_browser_discovery_excludes_replies_and_retweets_by_default(monkeypatch):
    monkeypatch.delenv("X_INCLUDE_REPLIES_IN_DISCOVERY", raising=False)
    monkeypatch.delenv("X_INCLUDE_RETWEETS_IN_DISCOVERY", raising=False)
    reply = XBrowserScraper.tweet_to_candidate(
        graphql_tweet("201", "Reply", parent_id="100")
    )
    retweet_data = graphql_tweet("203", "RT @someone: shared")
    retweet = XBrowserScraper.tweet_to_candidate(retweet_data)
    root = XBrowserScraper.tweet_to_candidate(graphql_tweet("100", "Root"))

    assert XBrowserScraper._eligible_discovery(root) is True
    assert XBrowserScraper._eligible_discovery(reply) is False
    assert XBrowserScraper._eligible_discovery(retweet) is False
