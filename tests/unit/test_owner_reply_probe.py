from tiktok_scraper.author_activity_trace import AuthorIdentity
from tiktok_scraper.owner_reply_probe import (
    OwnerReplyProbe,
    build_schema_analysis,
    is_reply_record,
    learn_profile_surface_identity,
    owner_reply_status,
    platform_reply_model,
    record_schema_paths,
)


def test_record_schema_paths_never_include_values():
    paths = record_schema_paths(
        {
            "text": "private-looking value",
            "author": {
                "username": "target",
                "public_identifiers": ["123"],
            },
        }
    )

    assert paths == [
        "author",
        "author.public_identifiers",
        "author.username",
        "text",
    ]
    assert "private-looking value" not in paths
    assert "target" not in paths
    assert "123" not in paths


def test_reply_detection_uses_explicit_or_parent_markers():
    assert is_reply_record({"is_reply": True})
    assert is_reply_record({"parent_comment_id": "parent-1"})
    assert is_reply_record({"in_reply_to_user_id": "user-1"})
    assert not is_reply_record({"comment_id": "top-1"})


def test_consistent_direct_profile_owner_id_is_learned():
    identity = AuthorIdentity.from_target(
        platform="youtube",
        target="@target",
    )

    result = learn_profile_surface_identity(
        identity,
        [
            {"creator_id": "UCstable", "username": "Display Name"},
            {"creator_id": "UCstable", "username": "Display Name"},
        ],
    )

    assert result["binding_basis"] == (
        "single_consistent_owner_id_on_direct_profile_surface"
    )
    assert identity.identifiers == {"UCstable"}


def test_mixed_profile_owner_ids_are_not_learned_without_username_match():
    identity = AuthorIdentity.from_target(
        platform="youtube",
        target="@target",
    )

    result = learn_profile_surface_identity(
        identity,
        [
            {"creator_id": "UCone", "username": "Display One"},
            {"creator_id": "UCtwo", "username": "Display Two"},
        ],
    )

    assert result["binding_basis"] == "no_stable_binding_observed"
    assert identity.identifiers == set()


def test_profile_binding_does_not_add_id_conflicting_with_known_target():
    identity = AuthorIdentity.from_target(
        platform="youtube",
        target="@target",
        user_id="UCknown",
    )

    result = learn_profile_surface_identity(
        identity,
        [{"creator_id": "UCother", "username": "Display Name"}],
    )

    assert result["binding_basis"] == "conflicting_owner_id_not_learned"
    assert identity.identifiers == {"UCknown"}


def test_tiktok_schema_classifies_author_id_as_verification_only():
    reply = {
        "comment_id": "c1",
        "parent_comment_id": "p1",
        "text": "reply",
        "author": {
            "username": "target",
            "public_identifiers": ["uid-1", "sec-1"],
        },
    }

    analysis = build_schema_analysis(
        platform="tiktok",
        owner_replies=[reply],
        raw_reply_records=[reply],
        comment_methods=["tiktok_comment_list_api"],
        posts_with_live_comment_feeds=1,
    )

    assert analysis["live_observation"] == "target_owner_reply_observed"
    assert (
        analysis["discovery_capability"]["classification"]
        == "post_scoped_only"
    )
    assert not analysis["discovery_capability"][
        "can_query_comments_by_author"
    ]
    assert analysis["observed_target_public_identifiers"] == [
        "sec-1",
        "uid-1",
    ]


def test_x_model_has_native_author_filter():
    model = platform_reply_model("x")

    assert model["capability"] == "native_author_query"
    assert model["accepted_author_filter_fields"] == ["user_id"]


def test_profile_urls_remove_about_tab_for_post_inventory():
    youtube = OwnerReplyProbe(
        platform="youtube",
        target="https://www.youtube.com/@target/about",
    )
    facebook = OwnerReplyProbe(
        platform="facebook",
        target="https://www.facebook.com/target/about",
    )

    assert youtube._profile_url() == "https://www.youtube.com/@target"
    assert facebook._profile_url() == "https://www.facebook.com/target"


def test_owner_reply_status_distinguishes_empty_partial_and_available():
    assert owner_reply_status(
        candidate_count=0,
        available_posts=0,
        failed_posts=0,
        reply_count=0,
    ) == "no_owner_posts_observed"
    assert owner_reply_status(
        candidate_count=2,
        available_posts=1,
        failed_posts=1,
        reply_count=1,
    ) == "partial"
    assert owner_reply_status(
        candidate_count=1,
        available_posts=1,
        failed_posts=0,
        reply_count=1,
    ) == "available"


def test_instagram_reply_flag_survives_general_tree_traversal():
    from tiktok_scraper.scrapers.instagram_scraper import InstagramScraper

    scraper = InstagramScraper()
    comments = {}
    scraper._collect_comments_from_json(
        {
            "id": "parent",
            "text": "top",
            "user": {"username": "other", "pk": "1"},
            "replies": [
                {
                    "id": "reply",
                    "text": "owner reply",
                    "user": {"username": "target", "pk": "2"},
                }
            ],
        },
        "post-1",
        comments,
    )

    assert comments["reply"]["is_reply"] is True


def test_instagram_comment_parser_rejects_text_bearing_non_comment_nodes():
    from tiktok_scraper.scrapers.instagram_scraper import InstagramScraper

    scraper = InstagramScraper()
    comments = {}
    scraper._collect_comments_from_json(
        {
            "data": {
                "xdt_api__v1__media__media_id__comments__connection": {
                    "edges": [
                        {
                            "node": {
                                "id": "comment-1",
                                "text": "real comment",
                                "owner": {"username": "viewer", "id": "1"},
                                "edge_liked_by": {"count": 0},
                            }
                        }
                    ],
                    "page_info": {"has_next_page": False, "end_cursor": None},
                },
                "media": {
                    "id": "media-1",
                    "text": "caption-like media text",
                    "owner": {"username": "creator", "id": "2"},
                },
            },
            "suggested_profile": {
                "id": "profile-1",
                "text": "profile biography",
                "owner": {"username": "someone", "id": "3"},
            },
        },
        "post-1",
        comments,
    )

    assert set(comments) == {"comment-1"}


def test_instagram_comment_parser_reads_rest_container_but_not_caption():
    from tiktok_scraper.scrapers.instagram_scraper import InstagramScraper

    scraper = InstagramScraper()
    comments = {}
    scraper._collect_comments_from_json(
        {
            "comments": [
                {
                    "pk": "comment-1",
                    "text": "real comment",
                    "user": {"username": "viewer", "pk": "1"},
                    "comment_like_count": 0,
                }
            ],
            "caption": {
                "pk": "caption-1",
                "text": "post caption",
                "user": {"username": "creator", "pk": "2"},
            },
        },
        "post-1",
        comments,
    )

    assert set(comments) == {"comment-1"}


def test_instagram_comment_parser_requires_provider_id_and_attribution():
    from tiktok_scraper.scrapers.instagram_scraper import InstagramScraper

    scraper = InstagramScraper()

    assert scraper._comment_from_node(
        {"id": "menu-1", "text": "snooze_suggested_posts"},
        "post-1",
    ) is None
    assert scraper._comment_from_node(
        {"text": "anonymous synthetic object", "user": {"username": "viewer"}},
        "post-1",
    ) is None
    assert scraper._comment_from_node(
        {
            "pk": "comment-1",
            "text": "real comment",
            "user": {"username": "viewer", "pk": "1"},
            "created_at": 123,
        },
        "post-1",
    )["comment_id"] == "comment-1"


def test_instagram_response_gate_accepts_only_comment_endpoints():
    from types import SimpleNamespace

    from tiktok_scraper.scrapers.instagram_scraper import InstagramScraper

    scraper = InstagramScraper()

    def graphql_response(friendly_name, doc_id):
        post_data = (
            f"fb_api_req_friendly_name={friendly_name}&doc_id={doc_id}"
            "&variables=%7B%22media_id%22%3A%22123%22%7D"
        )
        request = SimpleNamespace(
            method="POST",
            url="https://www.instagram.com/api/graphql",
            post_data=post_data,
            headers={},
        )
        return SimpleNamespace(url=request.url, request=request)

    assert scraper._is_comments_api_response(
        graphql_response("PolarisPostCommentsPaginationQuery", "other")
    ) is True
    assert scraper._is_comments_api_response(
        graphql_response("PolarisPostRootQuery", scraper.COMMENTS_DOC_ID)
    ) is True
    assert scraper._is_comments_api_response(
        graphql_response("PolarisPostRootQuery", scraper.POST_ROOT_DOC_ID)
    ) is False

    rest_request = SimpleNamespace(
        method="GET",
        url="https://www.instagram.com/api/v1/media/123/comments/",
        post_data="",
        headers={},
    )
    assert scraper._is_comments_api_response(
        SimpleNamespace(url=rest_request.url, request=rest_request)
    ) is True


def test_facebook_explicit_parent_is_preserved_as_reply():
    from tiktok_scraper.scrapers.facebook_scraper import FacebookScraper

    scraper = FacebookScraper()
    comments = {}
    scraper._collect_comments_from_json(
        {
            "__typename": "Comment",
            "id": "reply",
            "body": {"text": "owner reply"},
            "author": {"name": "Target", "id": "2"},
            "parent": {"id": "parent"},
        },
        "post-1",
        comments,
    )

    assert comments["reply"]["is_reply"] is True
    assert comments["reply"]["parent_comment_id"] == "parent"


def test_facebook_nested_reply_container_provides_parent_id():
    from tiktok_scraper.scrapers.facebook_scraper import FacebookScraper

    scraper = FacebookScraper()
    comments = {}
    scraper._collect_comments_from_json(
        {
            "__typename": "Comment",
            "id": "parent",
            "body": {"text": "top"},
            "author": {"name": "Other", "id": "1"},
            "replies": [
                {
                    "__typename": "Comment",
                    "id": "reply",
                    "body": {"text": "owner reply"},
                    "author": {"name": "Target", "id": "2"},
                }
            ],
        },
        "post-1",
        comments,
    )

    assert comments["reply"]["is_reply"] is True
    assert comments["reply"]["parent_comment_id"] == "parent"
