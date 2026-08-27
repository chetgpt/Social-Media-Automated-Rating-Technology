from __future__ import annotations

from dataclasses import asdict

import pytest

from tiktok_content_posting_api import (
    CREATOR_INFO_URL,
    PHOTO_INIT_URL,
    STATUS_FETCH_URL,
    TikTokAPIError,
    TikTokContentPostingClient,
    TikTokCreatorMismatchError,
    TikTokProtocolError,
    TikTokStatusTimeout,
    TikTokTransportError,
    TikTokValidationError,
    utf16_code_units,
    validate_https_media_url,
)


TOKEN = "secret-access-token"
USERNAME = "kitascore"


class FakeResponse:
    def __init__(self, payload, *, status_code=200, json_error=None):
        self.payload = payload
        self.status_code = status_code
        self.json_error = json_error

    def json(self):
        if self.json_error is not None:
            raise self.json_error
        return self.payload


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError("unexpected HTTP request")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def ok(data):
    return FakeResponse(
        {
            "data": data,
            "error": {"code": "ok", "message": "", "log_id": "safe-log-id"},
        }
    )


def creator_data(
    *,
    username=USERNAME,
    privacy=("PUBLIC_TO_EVERYONE", "SELF_ONLY"),
    comment_disabled=False,
):
    return {
        "creator_username": username,
        "creator_nickname": "Kita Score",
        "privacy_level_options": list(privacy),
        "comment_disabled": comment_disabled,
    }


def client(session, **kwargs):
    return TikTokContentPostingClient(
        session=session,
        access_token=TOKEN,
        expected_creator_username=USERNAME,
        **kwargs,
    )


def processing_status(**extra):
    return ok({"status": "PROCESSING_DOWNLOAD", **extra})


def test_query_creator_info_binds_exact_account_and_redacts_repr():
    session = FakeSession(ok(creator_data()))

    result = client(session).query_creator_info()

    assert result.creator_username == USERNAME
    assert result.privacy_level_options == (
        "PUBLIC_TO_EVERYONE",
        "SELF_ONLY",
    )
    assert session.calls == [
        (
            CREATOR_INFO_URL,
            {
                "headers": {
                    "Authorization": f"Bearer {TOKEN}",
                    "Content-Type": "application/json; charset=UTF-8",
                },
                "timeout": 30.0,
            },
        )
    ]
    assert TOKEN not in repr(client(FakeSession()))
    assert TOKEN not in repr(result)
    assert TOKEN not in repr(asdict(result))


def test_creator_username_match_is_exact_and_stops_before_init():
    session = FakeSession(ok(creator_data(username="KitaScore")))

    with pytest.raises(TikTokCreatorMismatchError) as captured:
        client(session).initialize_photo_direct_post(
            photo_urls=["https://media.example.test/comment.webp"],
            privacy_level="PUBLIC_TO_EVERYONE",
        )

    assert captured.value.expected == USERNAME
    assert captured.value.observed == "KitaScore"
    assert len(session.calls) == 1


def test_photo_direct_post_uses_fresh_creator_options_and_expected_payload():
    session = FakeSession(
        ok(creator_data()),
        ok({"publish_id": "p_pub_url~v2.123"}),
    )

    result = client(session).initialize_photo_direct_post(
        photo_urls=[
            "https://media.example.test/comments/one.webp?signature=abc",
            "https://media.example.test/comments/two.jpeg",
        ],
        privacy_level="PUBLIC_TO_EVERYONE",
        title="Comment follow-up",
        caption="Our analysis and comment on @creator's post.",
        photo_cover_index=1,
        allow_comments=True,
        auto_add_music=False,
    )

    assert result.publish_id == "p_pub_url~v2.123"
    assert result.creator_username == USERNAME
    assert result.photo_count == 2
    assert TOKEN not in repr(result)
    assert [call[0] for call in session.calls] == [
        CREATOR_INFO_URL,
        PHOTO_INIT_URL,
    ]
    payload = session.calls[1][1]["json"]
    assert payload == {
        "post_info": {
            "title": "Comment follow-up",
            "description": "Our analysis and comment on @creator's post.",
            "privacy_level": "PUBLIC_TO_EVERYONE",
            "disable_comment": False,
            "auto_add_music": False,
            "brand_content_toggle": False,
            "brand_organic_toggle": False,
        },
        "source_info": {
            "source": "PULL_FROM_URL",
            "photo_images": [
                "https://media.example.test/comments/one.webp?signature=abc",
                "https://media.example.test/comments/two.jpeg",
            ],
            "photo_cover_index": 1,
        },
        "post_mode": "DIRECT_POST",
        "media_type": "PHOTO",
    }


def test_before_submit_runs_after_fresh_creator_check_and_before_init_post():
    events = []

    class OrderedSession(FakeSession):
        def post(self, url, **kwargs):
            events.append(("post", url))
            return super().post(url, **kwargs)

    session = OrderedSession(
        ok(creator_data()),
        ok({"publish_id": "ordered"}),
    )

    def before_submit():
        events.append(("callback", None))

    client(session).initialize_photo_direct_post(
        photo_urls=["https://media.example.test/comment.webp"],
        privacy_level="SELF_ONLY",
        before_submit=before_submit,
    )

    assert events == [
        ("post", CREATOR_INFO_URL),
        ("callback", None),
        ("post", PHOTO_INIT_URL),
    ]


def test_before_submit_failure_prevents_photo_init_request():
    session = FakeSession(ok(creator_data()))

    def before_submit():
        raise RuntimeError("durable fence failed")

    with pytest.raises(RuntimeError, match="durable fence failed"):
        client(session).initialize_photo_direct_post(
            photo_urls=["https://media.example.test/comment.webp"],
            privacy_level="SELF_ONLY",
            before_submit=before_submit,
        )

    assert [call[0] for call in session.calls] == [CREATOR_INFO_URL]


def test_unavailable_privacy_level_stops_before_init():
    session = FakeSession(ok(creator_data(privacy=("SELF_ONLY",))))

    with pytest.raises(TikTokValidationError, match="not currently available"):
        client(session).initialize_photo_direct_post(
            photo_urls=["https://media.example.test/comment.webp"],
            privacy_level="PUBLIC_TO_EVERYONE",
        )

    assert [call[0] for call in session.calls] == [CREATOR_INFO_URL]


def test_creator_comment_setting_cannot_be_bypassed():
    blocked = FakeSession(ok(creator_data(comment_disabled=True)))
    with pytest.raises(TikTokValidationError, match="cannot be enabled"):
        client(blocked).initialize_photo_direct_post(
            photo_urls=["https://media.example.test/comment.webp"],
            privacy_level="SELF_ONLY",
            allow_comments=True,
        )
    assert len(blocked.calls) == 1

    allowed = FakeSession(
        ok(creator_data(comment_disabled=True)),
        ok({"publish_id": "publish-private"}),
    )
    client(allowed).initialize_photo_direct_post(
        photo_urls=["https://media.example.test/comment.webp"],
        privacy_level="SELF_ONLY",
        allow_comments=False,
    )
    assert allowed.calls[1][1]["json"]["post_info"]["disable_comment"] is True


@pytest.mark.parametrize(
    "url",
    [
        "http://media.example.test/comment.webp",
        "//media.example.test/comment.webp",
        "https:///comment.webp",
        "https://user:password@media.example.test/comment.webp",
        "https://media.example.test/comment.webp#fragment",
        " https://media.example.test/comment.webp",
        "https://media.example.test/comment image.webp",
        "https://media.example.test:99999/comment.webp",
        "https://media.example.test/comment\u0000.webp",
    ],
)
def test_media_url_validation_rejects_unsafe_or_non_https_values(url):
    with pytest.raises(TikTokValidationError):
        validate_https_media_url(url)


def test_photo_list_is_bounded_unique_and_cover_index_must_exist():
    api = client(FakeSession())
    base = "https://media.example.test/{index}.webp"

    with pytest.raises(TikTokValidationError, match="between 1 and 35"):
        api.initialize_photo_direct_post(
            photo_urls=[],
            privacy_level="SELF_ONLY",
        )
    with pytest.raises(TikTokValidationError, match="between 1 and 35"):
        api.initialize_photo_direct_post(
            photo_urls=[base.format(index=index) for index in range(36)],
            privacy_level="SELF_ONLY",
        )
    with pytest.raises(TikTokValidationError, match="duplicates"):
        api.initialize_photo_direct_post(
            photo_urls=[base.format(index=1), base.format(index=1)],
            privacy_level="SELF_ONLY",
        )
    with pytest.raises(TikTokValidationError, match="photo_cover_index"):
        api.initialize_photo_direct_post(
            photo_urls=[base.format(index=1)],
            privacy_level="SELF_ONLY",
            photo_cover_index=1,
        )


def test_utf16_limits_count_astral_characters_as_two_units():
    assert utf16_code_units("a" * 88 + "😀") == 90
    assert utf16_code_units("😀" * 2_000) == 4_000
    valid = FakeSession(
        ok(creator_data()),
        ok({"publish_id": "boundary"}),
    )
    client(valid).initialize_photo_direct_post(
        photo_urls=["https://media.example.test/comment.webp"],
        privacy_level="SELF_ONLY",
        title="a" * 88 + "😀",
        caption="😀" * 2_000,
    )

    no_network = client(FakeSession())
    with pytest.raises(TikTokValidationError, match="title exceeds"):
        no_network.initialize_photo_direct_post(
            photo_urls=["https://media.example.test/comment.webp"],
            privacy_level="SELF_ONLY",
            title="a" * 89 + "😀",
        )
    with pytest.raises(TikTokValidationError, match="caption exceeds"):
        no_network.initialize_photo_direct_post(
            photo_urls=["https://media.example.test/comment.webp"],
            privacy_level="SELF_ONLY",
            caption="😀" * 2_001,
        )
    with pytest.raises(TikTokValidationError, match="unpaired Unicode"):
        utf16_code_units("\ud800")


def test_tiktok_error_envelope_is_typed_and_token_is_redacted():
    session = FakeSession(
        FakeResponse(
            {
                "data": {},
                "error": {
                    "code": "access_token_invalid",
                    "message": f"invalid bearer {TOKEN}",
                    "log_id": f"log-{TOKEN}",
                },
            },
            status_code=401,
        )
    )

    with pytest.raises(TikTokAPIError) as captured:
        client(session).query_creator_info()

    assert captured.value.code == "access_token_invalid"
    assert captured.value.http_status == 401
    assert TOKEN not in str(captured.value)
    assert TOKEN not in captured.value.message
    assert TOKEN not in captured.value.log_id


def test_http_200_tiktok_error_envelope_is_still_an_error():
    session = FakeSession(
        FakeResponse(
            {
                "data": {},
                "error": {
                    "code": "spam_risk_too_many_posts",
                    "message": "Daily creator cap reached",
                    "log_id": "limit-log",
                },
            }
        )
    )

    with pytest.raises(TikTokAPIError) as captured:
        client(session).query_creator_info()

    assert captured.value.code == "spam_risk_too_many_posts"
    assert captured.value.http_status == 200


def test_server_controlled_success_fields_cannot_return_the_token():
    unsafe_creator = FakeSession(
        ok(creator_data(username=f"different-{TOKEN}"))
    )
    with pytest.raises(TikTokCreatorMismatchError) as mismatch:
        client(unsafe_creator).query_creator_info()
    assert TOKEN not in str(mismatch.value)
    assert TOKEN not in mismatch.value.observed

    unsafe_publish_id = FakeSession(
        ok(creator_data()),
        ok({"publish_id": f"publish-{TOKEN}"}),
    )
    with pytest.raises(TikTokProtocolError, match="unsafe publish_id"):
        client(unsafe_publish_id).initialize_photo_direct_post(
            photo_urls=["https://media.example.test/comment.webp"],
            privacy_level="SELF_ONLY",
        )


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse([], status_code=200),
        FakeResponse({"data": {}}, status_code=200),
        FakeResponse(
            {"data": {}, "error": {"code": "ok", "message": ""}},
            status_code=500,
        ),
        FakeResponse(None, json_error=ValueError("bad JSON")),
    ],
)
def test_malformed_envelopes_fail_closed(response):
    with pytest.raises(TikTokProtocolError):
        client(FakeSession(response)).query_creator_info()


def test_transport_exception_is_not_chained_or_echoed():
    session = FakeSession(RuntimeError(f"request headers contained {TOKEN}"))

    with pytest.raises(TikTokTransportError) as captured:
        client(session).query_creator_info()

    assert TOKEN not in str(captured.value)
    assert captured.value.__cause__ is None


def test_fetch_publish_status_returns_only_validated_safe_fields():
    session = FakeSession(
        ok(
            {
                "status": "PUBLISH_COMPLETE",
                "fail_reason": "",
                "publicaly_available_post_id": [1234567890123456789, "987"],
                "downloaded_bytes": 20_000,
            }
        )
    )

    result = client(session).fetch_publish_status("p_pub_url~v2.123")

    assert result.successful is True
    assert result.terminal is True
    assert result.publicly_available_post_ids == (
        "1234567890123456789",
        "987",
    )
    assert result.downloaded_bytes == 20_000
    assert session.calls[0][0] == STATUS_FETCH_URL
    assert session.calls[0][1]["json"] == {
        "publish_id": "p_pub_url~v2.123"
    }
    assert TOKEN not in repr(result)


def test_fetch_publish_status_rejects_unknown_state_and_bad_public_ids():
    unknown = FakeSession(ok({"status": "MYSTERY_STATE"}))
    with pytest.raises(TikTokProtocolError, match="unknown status"):
        client(unknown).fetch_publish_status("publish-1")

    unsafe_id = FakeSession(
        ok(
            {
                "status": "PUBLISH_COMPLETE",
                "publicaly_available_post_id": ["not-a-post-id"],
            }
        )
    )
    with pytest.raises(TikTokProtocolError, match="invalid public post ID"):
        client(unsafe_id).fetch_publish_status("publish-1")


def test_polling_uses_injected_clock_and_stops_on_completion():
    fake_clock = FakeClock()
    session = FakeSession(
        processing_status(downloaded_bytes=10),
        processing_status(downloaded_bytes=20),
        ok(
            {
                "status": "PUBLISH_COMPLETE",
                "publicaly_available_post_id": ["7654321"],
            }
        ),
    )
    api = client(session, sleep=fake_clock.sleep, clock=fake_clock)

    result = api.poll_publish_status(
        "publish-1",
        timeout_seconds=10,
        interval_seconds=2,
        max_attempts=10,
    )

    assert result.successful
    assert fake_clock.sleeps == [2.0, 2.0]
    assert len(session.calls) == 3


def test_polling_is_attempt_bounded_even_if_time_does_not_advance():
    session = FakeSession(
        processing_status(),
        processing_status(),
        processing_status(),
    )
    api = client(session, sleep=lambda _seconds: None, clock=lambda: 0.0)

    with pytest.raises(TikTokStatusTimeout) as captured:
        api.poll_publish_status(
            "publish-1",
            timeout_seconds=999,
            interval_seconds=0,
            max_attempts=3,
        )

    assert captured.value.attempts == 3
    assert captured.value.last_status == "PROCESSING_DOWNLOAD"
    assert len(session.calls) == 3


def test_failed_status_is_terminal_and_returned_for_receipt_handling():
    session = FakeSession(
        ok(
            {
                "status": "FAILED",
                "fail_reason": "photo_pull_failed",
                "publicaly_available_post_id": [],
            }
        )
    )

    result = client(session).poll_publish_status(
        "publish-failed",
        timeout_seconds=0,
        interval_seconds=0,
        max_attempts=1,
    )

    assert result.terminal is True
    assert result.successful is False
    assert result.fail_reason == "photo_pull_failed"
