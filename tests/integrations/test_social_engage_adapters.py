"""Injected HTTP fixtures; never authenticate or contact a real platform."""
import copy

import pytest

from social_engage.adapters import AdapterError, HTTP, ThreadsAdapter, LinkedInAdapter, eligible_time


TOKEN = "fixture-secret-token"


class Session:
    def __init__(self, responses):
        self.responses = list(responses); self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        status, body = value if isinstance(value, tuple) else (200, value)
        class Response:
            status_code = status
            def json(self):
                return body
        return Response()


def post(pid="123", **extra):
    return {"id": pid, "username": "creator", "text": "A hand-built bowl", "timestamp": "2026-09-11T00:00:00+0000",
            "permalink": "https://www.threads.net/@" + extra.get("username", "creator") + "/post/ABC?secret=discard", "media_type": "TEXT_POST", "has_replies": True, **extra}


def test_threads_identity_and_permission_errors_never_expose_token():
    session = Session([{"id": "88", "username": "operator"}, (403, {"error": TOKEN})])
    a = ThreadsAdapter(TOKEN, session=session)
    assert a.identity("@operator") == {"id": "88", "username": "operator"}
    with pytest.raises(AdapterError) as exc:
        a.identity("operator")
    assert TOKEN not in str(exc.value)
    assert session.calls[0][2]["headers"]["Authorization"] == "Bearer " + TOKEN
    assert all(TOKEN not in url for _, url, _ in session.calls)


def test_threads_projects_no_raw_media_or_transport_and_marks_unavailable():
    raw = post(text="caption " + TOKEN, media_url="https://cdn.invalid/signed", raw={"access_token": TOKEN})
    a = ThreadsAdapter(TOKEN, session=Session([raw, (403, {})]))
    result = a.collect("123", {"source": "topic", "comments": 20}, {"id": "88", "username": "operator"})
    assert "media_url" not in str(result) and TOKEN not in str(result)
    assert result["url"] == "https://www.threads.com/@creator/post/ABC"
    assert result["comments_complete"] is False
    assert result["comments_status"] == "permission_denied"
    assert result["music"]["status"] == "unsupported"


def test_cursor_paging_dedup_never_follows_signed_next_url():
    session = Session([{"data": [post()], "paging": {"cursors": {"after": "cursor-one"}, "next": "https://evil.invalid?access_token=" + TOKEN}},
                       {"data": [post(), post("124")]}])
    a = ThreadsAdapter(TOKEN, session=session)
    rows, complete = a.conversation("123", 10)
    assert [r["id"] for r in rows] == ["123", "124"] and complete
    assert session.calls[1][1].startswith("https://graph.threads.net/v1.0/123/conversation")
    assert session.calls[1][2]["params"]["after"] == "cursor-one"


@pytest.mark.parametrize("terminal", [False, True])
def test_comment_cap_is_honest(terminal):
    payload = {"data": [post()]}
    if not terminal:
        payload["paging"] = {"cursors": {"after": "next"}}
    a = ThreadsAdapter(TOKEN, session=Session([payload]))
    assert a.conversation("123", 1)[1] is terminal


def test_threads_topic_query_and_window_are_exact():
    session = Session([{"data": [post(), post("124", timestamp="2025-01-01T00:00:00Z")]}])
    a = ThreadsAdapter(TOKEN, session=session)
    scope = {"source": "topic", "target": "hand built pottery", "candidate_limit": 100, "since": 1788998400, "until": 1789171200}
    assert a.discover(scope) == ["123"]
    sent = session.calls[0][2]["params"]
    assert sent["q"] == "hand built pottery" and sent["search_type"] == "RECENT"


def test_threads_refuses_unknown_or_end_boundary_publication_timestamp():
    assert not eligible_time({"published_at": "unknown"}, {"since": 1, "until": 2})
    assert not eligible_time({"published_at": "1970-01-01T00:00:02Z"}, {"since": 1, "until": 2})


def test_threads_rejects_creator_mismatch_before_counting():
    a = ThreadsAdapter(TOKEN, session=Session([{"data": [post()]}]))
    with pytest.raises(AdapterError, match="creator_mismatch"):
        a.discover({"source": "creator", "target": "someone_else", "candidate_limit": 5})


@pytest.mark.parametrize("body", [{"data": [post(username="operator")]}, {"data": [post()], "paging": {"cursors": {"after": "again"}}}])
def test_threads_preflight_requires_complete_duplicate_free_conversation(body):
    # Repeat cursor stops bounded traversal without claiming exhaustion.
    a = ThreadsAdapter(TOKEN, session=Session([{"id": "88", "username": "operator"}, post(), body, body]))
    e = a._post(post())
    with pytest.raises(AdapterError, match="already_replied|duplicate_check_incomplete"):
        a.preflight(e, {"account": "operator"}, {"id": "88", "username": "operator"})


def test_http_post_needs_guard_and_redirects_are_disabled():
    session = Session([(302, {})]); h = HTTP(TOKEN, "https://graph.threads.net/v1.0", session=session)
    with pytest.raises(AdapterError, match="guard"):
        h.request("POST", "/me/threads")
    assert not session.calls
    with pytest.raises(AdapterError):
        h.request("GET", "/me")
    assert session.calls[0][2]["allow_redirects"] is False


def test_threads_write_exact_text_parent_and_readback_receipt():
    reply = post("999", username="operator", text="AI-assisted exact text", replied_to={"id": "123"})
    session = Session([{"id": "777"}, {"id": "999"}, reply])
    a = ThreadsAdapter(TOKEN, session=session); guards = []
    receipt = a.publish({"id": "123"}, {"id": "88", "username": "operator"}, "AI-assisted exact text", lambda: guards.append(True))
    assert receipt["verified"] and guards == [True, True]
    assert session.calls[0][2]["data"] == {"media_type": "TEXT", "text": "AI-assisted exact text", "reply_to_id": "123"}
    assert "auto_publish_text" not in session.calls[0][2]["data"]


def test_threads_mismatched_receipt_is_uncertain():
    a = ThreadsAdapter(TOKEN, session=Session([{"id": "777"}, {"id": "999"}, post("999", replied_to={"id": "555"})]))
    with pytest.raises(AdapterError, match="receipt"):
        a.publish({"id": "123"}, {"id": "88", "username": "operator"}, "AI text", lambda: None)


def test_linkedin_identity_requires_frozen_organization_admin():
    raw = {"elements": [{"role": "ADMINISTRATOR", "organization": "urn:li:organization:123", "roleAssignee": "urn:li:person:OP", "state": "APPROVED"}]}
    session = Session([raw])
    a = LinkedInAdapter(TOKEN, session=session, reader=object())
    assert a.identity("urn:li:organization:123")["operator_id"] == "urn:li:person:OP"
    assert session.calls[0][2]["params"]["q"] == "roleAssignee"


def test_linkedin_publish_uses_exact_organization_and_urn_then_verifies():
    session = Session([{"id": "999"}, {"id": "999", "actor": "urn:li:organization:123", "message": {"text": "AI exact text"}}])
    a = LinkedInAdapter(TOKEN, session=session, reader=object())
    a.publish({"id": "urn:li:share:1234", "url": "https://www.linkedin.com/feed/update/urn:li:share:1234"},
              {"id": "urn:li:organization:123"}, "AI exact text", lambda: None)
    assert session.calls[0][2]["json"] == {"actor": "urn:li:organization:123", "object": "urn:li:share:1234", "message": {"text": "AI exact text"}}
    assert "/socialActions/urn%3Ali%3Ashare%3A1234/comments" in session.calls[0][1]


class LinkedInReader:
    def __init__(self, total=1, author="urn:li:person:OTHER"):
        self.total, self.author = total, author

    def collect_organization_post(self, post_id, owner, comments_limit):
        return {"post_urn": post_id, "organization_urn": owner, "commentary": "Caption " + TOKEN,
                "published_at": "2026-09-10T00:00:00Z", "observed_at": "2026-09-12T00:00:00Z",
                "canonical_url": "https://www.linkedin.com/feed/update/" + post_id,
                "comments": [{"comment_urn": "urn:li:comment:(urn:li:activity:1234,999)", "parent_comment_urn": "", "actor_urn": self.author,
                              "message": "Audience " + TOKEN, "created_at": "2026-09-11T00:00:00Z", "likes": 0}],
                "metrics": {"comments": self.total}}


def test_linkedin_projection_redacts_token_and_unknown_count_is_incomplete():
    reader = LinkedInReader(total=None)
    a = LinkedInAdapter(TOKEN, session=Session([]), reader=reader)
    e = a.collect("urn:li:share:1234", {"account": "urn:li:organization:123", "comments": 20}, {"id": "urn:li:organization:123"})
    assert TOKEN not in str(e) and not e["comments_complete"]
    assert e["transcript"]["status"] == "unsupported"


@pytest.mark.parametrize("total,author,expected", [(2, "urn:li:person:OTHER", "incomplete"), (1, "urn:li:organization:123", "already_commented")])
def test_linkedin_preflight_rejects_incomplete_or_prior_own_comment(total, author, expected):
    org = "urn:li:organization:123"
    session = Session([{"elements": [{"organization": org, "role": "ADMINISTRATOR", "state": "APPROVED", "roleAssignee": "urn:li:person:OP"}]}])
    a = LinkedInAdapter(TOKEN, session=session, reader=LinkedInReader(total, author))
    scope = {"account": org, "comments": 20}
    actor = {"id": org, "username": org, "operator_id": "urn:li:person:OP"}
    e = a.collect("urn:li:share:1234", scope, actor)
    with pytest.raises(AdapterError, match=expected):
        a.preflight(e, scope, actor)


def test_missing_cursor_with_next_url_cannot_claim_exhaustion():
    a = ThreadsAdapter(TOKEN, session=Session([{"data": [post()], "paging": {"next": "https://evil.invalid"}}]))
    assert a.conversation("123", 10)[1] is False
