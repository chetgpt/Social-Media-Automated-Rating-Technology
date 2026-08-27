from tiktok_scraper.author_activity_trace import (
    AuthorActivityTracer,
    AuthorIdentity,
    _dedupe_candidates,
    _parent_post_id,
    all_attempted_posts_complete,
    candidate_pool_status,
    is_target_owned,
    match_author,
    normalize_comment,
)


def test_tiktok_profile_url_resolves_username():
    identity = AuthorIdentity.from_target(
        platform="tiktok",
        target="https://www.tiktok.com/@sample.user",
    )

    assert identity.username == "sample.user"
    assert identity.usernames == {"sample.user"}


def test_youtube_channel_url_resolves_stable_id():
    identity = AuthorIdentity.from_target(
        platform="youtube",
        target="https://www.youtube.com/channel/UC123456789/about",
    )

    assert identity.username == ""
    assert identity.user_id == "UC123456789"
    assert identity.identifiers == {"UC123456789"}


def test_facebook_profile_query_resolves_stable_id():
    identity = AuthorIdentity.from_target(
        platform="facebook",
        target="https://www.facebook.com/profile.php?id=12345",
    )

    assert identity.user_id == "12345"


def test_exact_username_match_learns_public_ids():
    identity = AuthorIdentity.from_target(
        platform="instagram",
        target="target_user",
    )

    basis, author = match_author(
        identity,
        {
            "author": "target_user",
            "author_profile": {
                "username": "target_user",
                "user_id": "9988",
            },
        },
    )

    assert basis == "exact_username"
    assert author["identifiers"] == ["9988"]
    assert identity.identifiers == {"9988"}


def test_stable_id_matches_after_username_change():
    identity = AuthorIdentity.from_target(
        platform="youtube",
        target="@old_handle",
        user_id="UCstable",
    )

    basis, author = match_author(
        identity,
        {
            "author": "@new_handle",
            "author_id": "UCstable",
        },
    )

    assert basis == "stable_id"
    assert "new_handle" in author["usernames"]
    assert "new_handle" in identity.usernames


def test_target_owned_candidate_matches_id_or_username():
    identity = AuthorIdentity.from_target(
        platform="instagram",
        target="target_user",
        user_id="9988",
    )

    assert is_target_owned(
        identity,
        {"creator_id": "9988", "username": "renamed_user"},
    )
    assert is_target_owned(
        identity,
        {"username": "target_user"},
    )
    assert not is_target_owned(
        identity,
        {"creator_id": "1122", "username": "other_user"},
    )


def test_normalized_comment_keeps_full_text_and_source():
    identity = AuthorIdentity.from_target(
        platform="youtube",
        target="@target",
        user_id="UCtarget",
    )
    source = {
        "video_id": "video-1",
        "url": "https://www.youtube.com/watch?v=video-1",
        "username": "other-channel",
        "creator_id": "UCother",
    }

    comment = normalize_comment(
        {
            "comment_id": "comment-1",
            "text": "ordinary complete comment text",
            "author": "@target",
            "author_id": "UCtarget",
            "is_reply": False,
        },
        identity=identity,
        author={
            "username": "target",
            "display_name": "",
            "identifiers": ["UCtarget"],
        },
        match_basis="stable_id",
        source_post=source,
    )

    assert comment["text"] == "ordinary complete comment text"
    assert comment["match_basis"] == "stable_id"
    assert comment["source_post"]["post_id"] == "video-1"
    assert comment["source_post"]["owner_id"] == "UCother"


def test_candidate_dedupe_and_x_parent_resolution():
    candidates = _dedupe_candidates(
        [
            {"video_id": "1", "url": "https://x.com/a/status/1"},
            {"video_id": "1", "url": "https://x.com/a/status/1"},
            {"video_id": "2", "url": "https://x.com/a/status/2"},
        ]
    )
    parent_id = _parent_post_id(
        {
            "referenced_tweets": [
                {"type": "quoted", "id": "8"},
                {"type": "replied_to", "id": "9"},
            ]
        }
    )

    assert [item["video_id"] for item in candidates] == ["1", "2"]
    assert parent_id == "9"


def test_youtube_search_keeps_username_as_primary_seed():
    tracer = AuthorActivityTracer(
        platform="youtube",
        target="@target",
        user_id="UCtarget",
        max_posts=0,
    )

    assert tracer._search_seed() == "target"
    assert tracer._search_queries() == ["@target", "target", "UCtarget"]
    assert tracer.max_posts == 0


def test_candidate_pool_status_distinguishes_partial_and_blocked():
    assert candidate_pool_status(
        candidate_count=2,
        available_posts=1,
        error_posts=1,
    ) == "partial"
    assert candidate_pool_status(
        candidate_count=0,
        available_posts=0,
        error_posts=0,
        discovery_error="login gate",
    ) == "blocked"
    assert candidate_pool_status(
        candidate_count=0,
        available_posts=0,
        error_posts=0,
    ) == "no_candidates_observed"


def test_all_attempted_posts_complete_rejects_mixed_errors():
    assert all_attempted_posts_complete(
        [
            {"status": "available", "complete": True},
            {"status": "self_owned_excluded", "complete": False},
        ]
    )
    assert not all_attempted_posts_complete(
        [
            {"status": "available", "complete": True},
            {"status": "error", "complete": False},
        ]
    )
