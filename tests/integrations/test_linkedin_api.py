import json
import urllib.parse

import pytest

from tiktok_scraper.linkedin_api import (
    LinkedInAPIClient,
    LinkedInForbiddenError,
    LinkedInRateLimitedError,
    LinkedInUnavailableError,
    LinkedInValidationError,
    validate_organization_urn,
    validate_post_urn,
)


TOKEN = "SECRET_OAUTH_TOKEN"
ORG = "urn:li:organization:2414183"
OTHER_ORG = "urn:li:organization:987654"
POST_1 = "urn:li:share:6844785523593134080"
POST_2 = "urn:li:ugcPost:6844785523593134081"
COMMENT_1 = "urn:li:comment:(urn:li:activity:6844785523593134080,1001)"
COMMENT_2 = "urn:li:comment:(urn:li:activity:6844785523593134080,1002)"
REPLY_1 = "urn:li:comment:(urn:li:activity:6844785523593134080,2001)"


class QueueTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, *, headers, timeout):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "timeout": timeout,
            }
        )
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def post_payload(post_urn=POST_1, author=ORG, commentary="Page update"):
    return {
        "id": post_urn,
        "author": author,
        "commentary": commentary,
        "lifecycleState": "PUBLISHED",
        "visibility": "PUBLIC",
        "createdAt": 1_700_000_000_000,
        "publishedAt": 1_700_000_000_001,
        "lastModifiedAt": 1_700_000_000_002,
        "lifecycleStateInfo": {"isEditedByAuthor": False},
        "isReshareDisabledByAuthor": True,
        "distribution": {
            "feedDistribution": "MAIN_FEED",
            "thirdPartyDistributionChannels": [],
            "targetEntities": [{"raw": "must not survive"}],
        },
        "content": {
            "media": {
                "id": "urn:li:video:C5F10AQGKQg_6y2a4sQ",
                "title": "Launch video",
                "downloadUrl": "https://signed.example/video?token=SECRET",
            }
        },
        "rawPayload": {"authorization": f"Bearer {TOKEN}"},
    }


def comment_payload(
    comment_urn,
    comment_id,
    *,
    actor="urn:li:person:A8xe03Qt10",
    parent=None,
    message="Useful comment",
):
    payload = {
        "commentUrn": comment_urn,
        "id": str(comment_id),
        "actor": actor,
        "object": "urn:li:activity:6844785523593134080",
        "message": {"text": message},
        "created": {"time": 1_700_000_000_100},
        "lastModified": {"time": 1_700_000_000_200},
        "likesSummary": {
            "aggregatedTotalLikes": 3,
            "likedByCurrentUser": False,
            "selectedLikes": [{"secret": TOKEN}],
        },
        "content": [{"raw": f"Bearer {TOKEN}"}],
    }
    if parent is not None:
        payload["parentComment"] = parent
    return payload


def test_validators_are_closed_and_reject_person_or_activity_urns():
    assert validate_organization_urn(ORG) == ORG
    assert validate_post_urn(POST_1) == POST_1

    for invalid in (
        "urn:li:person:A8xe03Qt10",
        "urn:li:organizationBrand:12",
        "urn:li:organization:abc",
        "urn:li:organization:0",
        f"{ORG}?q=author",
    ):
        with pytest.raises(LinkedInValidationError):
            validate_organization_urn(invalid)

    for invalid in (
        "urn:li:activity:6844785523593134080",
        "urn:li:share:not-numeric",
        "urn:li:share:0",
        f"{POST_1}/comments",
    ):
        with pytest.raises(LinkedInValidationError):
            validate_post_urn(invalid)


def test_find_author_posts_pages_deduplicates_and_returns_only_safe_projection():
    first = post_payload(commentary=f"Public text Bearer {TOKEN}")
    first["unknown"] = {"access_token": TOKEN}
    duplicate = post_payload()
    second = post_payload(POST_2, commentary="Second post")
    transport = QueueTransport(
        (
            200,
            {
                "elements": [first],
                "paging": {
                    "start": 0,
                    "count": 1,
                    "links": [{"rel": "next", "href": "?start=1&count=2"}],
                },
            },
        ),
        (200, {"elements": [duplicate, second], "paging": {"links": []}}),
    )
    client = LinkedInAPIClient(TOKEN, transport=transport)

    posts = client.find_author_posts(ORG, page_size=2)

    assert [post["post_urn"] for post in posts] == [POST_1, POST_2]
    assert posts[0]["author_type"] == "organization"
    assert posts[0]["commentary"] == "Public text [redacted]"
    assert posts[0]["content"] == {
        "type": "media",
        "title": "Launch video",
        "description": "",
        "asset_urns": ["urn:li:video:C5F10AQGKQg_6y2a4sQ"],
    }
    serialized = json.dumps(posts)
    assert TOKEN not in serialized
    assert "rawPayload" not in serialized
    assert "downloadUrl" not in serialized
    assert "targetEntities" not in serialized
    assert len(transport.calls) == 2
    first_call = transport.calls[0]
    assert first_call["method"] == "GET"
    assert first_call["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert first_call["headers"]["Linkedin-Version"] == "202608"
    assert first_call["headers"]["X-Restli-Protocol-Version"] == "2.0.0"
    assert first_call["headers"]["X-RestLi-Method"] == "FINDER"
    second_query = urllib.parse.parse_qs(
        urllib.parse.urlsplit(transport.calls[1]["url"]).query
    )
    assert second_query["start"] == ["1"]
    assert TOKEN not in transport.calls[0]["url"]


def test_find_author_posts_rejects_person_input_without_transport_call():
    transport = QueueTransport()
    client = LinkedInAPIClient(TOKEN, transport=transport)

    with pytest.raises(LinkedInValidationError):
        client.find_author_posts("urn:li:person:A8xe03Qt10")

    assert transport.calls == []


def test_get_exact_post_fails_closed_for_person_or_wrong_organization_author():
    person_transport = QueueTransport(
        (200, post_payload(author="urn:li:person:A8xe03Qt10"))
    )
    with pytest.raises(LinkedInUnavailableError) as person_error:
        LinkedInAPIClient(TOKEN, transport=person_transport).get_post(POST_1)
    assert person_error.value.kind == "unavailable"

    wrong_org_transport = QueueTransport((200, post_payload(author=OTHER_ORG)))
    with pytest.raises(LinkedInUnavailableError):
        LinkedInAPIClient(TOKEN, transport=wrong_org_transport).get_post(
            POST_1,
            organization_urn=ORG,
        )


def test_social_metadata_is_allowlisted_and_normalized():
    transport = QueueTransport(
        (
            200,
            {
                "entity": "urn:li:activity:6844785523593134080",
                "commentsState": "OPEN",
                "commentSummary": {"count": 4, "topLevelCount": 3},
                "reactionSummaries": {
                    "EMPATHY": {"reactionType": "EMPATHY", "count": 2},
                    "UNKNOWN_RAW_TYPE": {"count": 999},
                },
                "raw": {"authorization": TOKEN},
            },
        )
    )

    metadata = LinkedInAPIClient(TOKEN, transport=transport).get_social_metadata(
        POST_1
    )

    assert metadata == {
        "entity_urn": POST_1,
        "comments_state": "OPEN",
        "comment_count": 4,
        "top_level_comment_count": 3,
        "reaction_counts": {"EMPATHY": 2},
        "provenance": {
            "authority": "linkedin_official_api",
            "api_version": "202608",
            "resource": "socialMetadata",
        },
    }
    assert TOKEN not in json.dumps(metadata)


def test_comments_page_and_collect_nested_replies_without_returning_raw_fields():
    transport = QueueTransport(
        (
            200,
            {
                "elements": [
                    comment_payload(
                        COMMENT_1,
                        1001,
                        message=f"Top level access_token={TOKEN}",
                    )
                ],
                "paging": {
                    "count": 1,
                    "links": [{"rel": "next", "href": "?start=1&count=1"}],
                },
            },
        ),
        (
            200,
            {
                "elements": [comment_payload(COMMENT_2, 1002)],
                "paging": {"links": []},
            },
        ),
        (
            200,
            {
                "elements": [
                    comment_payload(
                        REPLY_1,
                        2001,
                        parent=COMMENT_1,
                        actor=ORG,
                        message="Organization reply",
                    )
                ],
                "paging": {"links": []},
            },
        ),
        (200, {"elements": [], "paging": {"links": []}}),
    )

    comments = LinkedInAPIClient(TOKEN, transport=transport).get_comments(
        POST_1,
        page_size=1,
        include_replies=True,
        max_reply_depth=1,
    )

    assert [comment["comment_urn"] for comment in comments] == [COMMENT_1, COMMENT_2]
    assert comments[0]["message"] == "Top level [redacted]"
    assert comments[0]["actor_type"] == "person"
    assert comments[0]["replies"][0]["comment_urn"] == REPLY_1
    assert comments[0]["replies"][0]["actor_type"] == "organization"
    assert comments[1]["replies"] == []
    serialized = json.dumps(comments)
    assert TOKEN not in serialized
    assert "selectedLikes" not in serialized
    assert '"content"' not in serialized
    assert len(transport.calls) == 4


@pytest.mark.parametrize(
    ("response", "error_type", "kind", "retry_after"),
    [
        ((403, {"message": f"Bearer {TOKEN}"}), LinkedInForbiddenError, "forbidden", None),
        (
            (429, {"message": TOKEN}, {"Retry-After": "17"}),
            LinkedInRateLimitedError,
            "rate_limited",
            17,
        ),
        ((503, {"raw": TOKEN}), LinkedInUnavailableError, "unavailable", None),
    ],
)
def test_http_errors_are_typed_and_never_echo_provider_payload(
    response,
    error_type,
    kind,
    retry_after,
):
    client = LinkedInAPIClient(TOKEN, transport=QueueTransport(response))

    with pytest.raises(error_type) as caught:
        client.get_post(POST_1)

    assert caught.value.kind == kind
    assert caught.value.retry_after_seconds == retry_after
    assert TOKEN not in str(caught.value)
    assert TOKEN not in repr(caught.value)


def test_transport_exception_is_replaced_by_sanitized_unavailable_error():
    transport = QueueTransport(RuntimeError(f"Authorization: Bearer {TOKEN}"))
    client = LinkedInAPIClient(TOKEN, transport=transport)

    with pytest.raises(LinkedInUnavailableError) as caught:
        client.get_post(POST_1)

    assert str(caught.value) == "LinkedIn API unavailable during get_post"
    assert TOKEN not in repr(caught.value)


def test_api_version_and_timeout_are_read_only_after_validation():
    client = LinkedInAPIClient(TOKEN, transport=QueueTransport())

    assert client.api_version == "202608"
    assert client.timeout == 30.0
    with pytest.raises(AttributeError):
        client.api_version = "bad\r\nAuthorization: injected"
    with pytest.raises(AttributeError):
        client.timeout = -1


def test_collect_post_observation_combines_only_normalized_resources():
    transport = QueueTransport(
        (200, post_payload()),
        (
            200,
            {
                "commentsState": "OPEN",
                "commentSummary": {"count": 0, "topLevelCount": 0},
                "reactionSummaries": {},
            },
        ),
        (200, {"elements": [], "paging": {"links": []}}),
    )
    observation = LinkedInAPIClient(TOKEN, transport=transport).collect_post_observation(
        ORG,
        POST_1,
        include_replies=False,
    )

    assert observation["schema_version"] == "linkedin-post-observation-v1"
    assert observation["post_urn"] == POST_1
    assert observation["social_metadata"]["comment_count"] == 0
    assert observation["comments"] == {
        "status": "available",
        "top_level_retrieved": 0,
        "total_retrieved": 0,
        "items": [],
    }
    assert TOKEN not in json.dumps(observation)


def test_orchestration_surfaces_return_frozen_urns_and_state_checkpoint_shape():
    inventory_transport = QueueTransport(
        (
            200,
            {
                "elements": [post_payload(POST_1), post_payload(POST_2)],
                "paging": {"links": []},
            },
        )
    )
    inventory_client = LinkedInAPIClient(TOKEN, transport=inventory_transport)
    assert inventory_client.list_author_post_urns(ORG) == [POST_1, POST_2]

    collection_transport = QueueTransport(
        (200, post_payload()),
        (
            200,
            {
                "commentsState": "OPEN",
                "commentSummary": {"count": 2, "topLevelCount": 1},
                "reactionSummaries": {
                    "LIKE": {"reactionType": "LIKE", "count": 4},
                    "EMPATHY": {"reactionType": "EMPATHY", "count": 1},
                },
            },
        ),
        (
            200,
            {
                "elements": [comment_payload(COMMENT_1, 1001)],
                "paging": {"links": []},
            },
        ),
        (
            200,
            {
                "elements": [
                    comment_payload(REPLY_1, 2001, parent=COMMENT_1)
                ],
                "paging": {"links": []},
            },
        ),
    )
    collection_client = LinkedInAPIClient(TOKEN, transport=collection_transport)
    observation = collection_client.collect_organization_post(
        POST_1,
        ORG,
        10,
    )

    assert observation["post_urn"] == POST_1
    assert observation["organization_urn"] == ORG
    assert observation["canonical_url"] == (
        f"https://www.linkedin.com/feed/update/{POST_1}/"
    )
    assert observation["published_at"] == "2023-11-14T22:13:20+00:00"
    assert observation["content"]["type"] == "media"
    assert observation["content"]["visibility"] == "PUBLIC"
    assert observation["content"]["lifecycle_state"] == "PUBLISHED"
    assert observation["content"]["last_modified_at"].endswith("+00:00")
    assert (
        observation["content"]["distribution"]["feed_distribution"]
        == "MAIN_FEED"
    )
    assert observation["metrics"]["reactions"] == 5
    assert observation["collection"]["total_comments_retrieved"] == 2
    assert [item["comment_urn"] for item in observation["comments"]] == [
        COMMENT_1,
        REPLY_1,
    ]
    assert observation["comments"][1]["parent_comment_urn"] == COMMENT_1
    assert observation["comments"][0]["likes"] == 3
    assert observation["comments"][0]["created_at"].endswith("+00:00")
    assert TOKEN not in json.dumps(observation)


def test_pagination_link_cannot_redirect_transport_to_another_host():
    transport = QueueTransport(
        (
            200,
            {
                "elements": [post_payload()],
                "paging": {
                    "links": [
                        {
                            "rel": "next",
                            "href": "https://evil.example/rest/posts?start=1",
                        }
                    ]
                },
            },
        )
    )
    client = LinkedInAPIClient(TOKEN, transport=transport)

    with pytest.raises(LinkedInUnavailableError):
        client.find_author_posts(ORG)

    assert len(transport.calls) == 1
    assert urllib.parse.urlsplit(transport.calls[0]["url"]).hostname == "api.linkedin.com"


def test_official_absolute_next_link_is_used_only_as_a_safe_cursor():
    transport = QueueTransport(
        (
            200,
            {
                "elements": [post_payload(POST_1)],
                "paging": {
                    "links": [
                        {
                            "rel": "next",
                            "href": "https://api.linkedin.com/rest/posts?start=1&count=1",
                        }
                    ]
                },
            },
        ),
        (200, {"elements": [post_payload(POST_2)], "paging": {"links": []}}),
    )

    posts = LinkedInAPIClient(TOKEN, transport=transport).find_author_posts(ORG)

    assert [post["post_urn"] for post in posts] == [POST_1, POST_2]
    assert len(transport.calls) == 2
    assert all(
        urllib.parse.urlsplit(call["url"]).hostname == "api.linkedin.com"
        for call in transport.calls
    )


def test_comment_thread_mismatch_fails_closed():
    wrong_thread = comment_payload(
        "urn:li:comment:(urn:li:activity:999999,1001)",
        1001,
    )
    wrong_thread["object"] = "urn:li:activity:999999"
    transport = QueueTransport(
        (200, {"elements": [wrong_thread], "paging": {"links": []}})
    )

    with pytest.raises(LinkedInUnavailableError):
        LinkedInAPIClient(TOKEN, transport=transport).get_comments(
            POST_1,
            include_replies=False,
        )
