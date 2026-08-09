from tiktok_scraper.username_comment_trace import (
    candidate_from_search_result,
    normalize_target_comment,
    normalize_username,
    target_author_match,
)


def test_username_normalization_is_exact_and_platform_friendly():
    assert normalize_username(" @Example.User ") == "example.user"
    assert normalize_username("ＦＯＯ") == "foo"


def test_exact_username_match_learns_stable_public_identifiers():
    identifiers: set[str] = set()
    comment = {
        "cid": "comment-1",
        "text": "ordinary comment",
        "user": {
            "uid": "123",
            "sec_uid": "sec-123",
            "unique_id": "Target.User",
            "nickname": "Target",
        },
    }

    matched, author = target_author_match(
        comment,
        "target.user",
        identifiers,
    )

    assert matched is True
    assert author["username"] == "Target.User"
    assert identifiers == {"123", "sec-123"}


def test_stable_id_matches_after_username_changes():
    identifiers = {"123"}
    comment = {
        "cid": "comment-2",
        "user": {
            "uid": "123",
            "unique_id": "renamed.user",
        },
    }

    matched, _ = target_author_match(comment, "target.user", identifiers)

    assert matched is True


def test_normalized_comment_keeps_full_text_and_source_context():
    comment = {
        "cid": "comment-3",
        "text": "This is not an explicit profile statement.",
        "create_time": 123456,
        "digg_count": 7,
    }
    author = {
        "username": "target",
        "display_name": "Target",
        "identifiers": ["123"],
    }
    post = {
        "post_id": "video-1",
        "url": "https://www.tiktok.com/@other/video/1",
        "owner_username": "other",
        "caption": "Source post",
        "pre_discovery_seed": "target",
        "search_query": "target",
        "discovery_method": "tiktok_username_search",
    }

    result = normalize_target_comment(comment, author=author, post=post)

    assert result["text"] == "This is not an explicit profile statement."
    assert result["source_post"]["owner_username"] == "other"
    assert result["discovery"]["pre_discovery_seed"] == "target"


def test_candidate_records_username_as_primary_pre_discovery_seed():
    candidate = candidate_from_search_result(
        {
            "id": "123",
            "url": "https://www.tiktok.com/@other/video/123",
            "username": "other",
        },
        username_seed="target",
    )

    assert candidate["pre_discovery_seed"] == "target"
    assert candidate["search_query"] == "target"
    assert candidate["owner_username"] == "other"
