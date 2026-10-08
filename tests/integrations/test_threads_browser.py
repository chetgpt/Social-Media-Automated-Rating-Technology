"""Synthetic browser transport boundaries; no Edge, credentials, or network."""
import asyncio
import copy
import json
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from social_engage.adapters import AdapterError
from social_engage.threads_browser import ThreadsBrowserAdapter, connection_info, conversation_posts, query_targets_post, read_template, verified_reply
from social_engage.threads_data import id_to_shortcode


URL = "https://www.threads.com/graphql/query"
OPERATION = "BarcelonaPostQuery"


def form(**extra):
    return urlencode({"fb_api_req_friendly_name": OPERATION, "doc_id": "123456",
                      "variables": json.dumps({"postID": "123", "after": None}), **extra})


def raw_post(post_id="123", owner="creator", parent=None):
    info = {"direct_reply_count": 0}
    if parent is not None:
        info["parent_id"] = parent
    return {"pk": post_id, "id": post_id + "_88", "code": id_to_shortcode(post_id),
            "canonical_url": "https://www.threads.com/@" + owner + "/post/" + id_to_shortcode(post_id),
            "user": {"pk": "88", "username": owner}, "taken_at": 1789000000,
            "caption": {"text": "Synthetic thread"}, "media_type": 19, "text_post_app_info": info}


def connection(post_id="123", *, terminal=True):
    return {"edges": [{"node": {"thread_items": [{"post": raw_post(post_id)}]}}],
            "page_info": {"has_next_page": not terminal, "end_cursor": None if terminal else "next-page"}}


def reply_connection():
    payload = connection()
    payload["show_unavailable_replies_disclaimer"] = False
    reply = raw_post("124", "responder")
    reply["text_post_app_info"].update(is_reply=True, reply_to_author={"username":"creator"})
    payload["edges"].append({"node":{"thread_items":[{"post":reply}]}})
    return payload


def test_post_connection_binds_reply_without_inventing_a_feed_parent():
    payload = reply_connection()
    result = conversation_posts(payload, "123")
    assert [(p["id"],p["parent_id"],p["root_id"]) for p in result] == [("124","123","123")]
    assert conversation_posts(payload, "999") == []
    del payload["show_unavailable_replies_disclaimer"]
    assert conversation_posts(payload, "123") == []


@pytest.mark.parametrize("change", ["wrong_author", "wrong_parent", "not_reply", "missing_item", "quoted"])
def test_post_connection_rejects_ambiguous_or_unrelated_reply_chains(change):
    payload = reply_connection()
    item = payload["edges"][1]["node"]["thread_items"][0]
    info = item["post"]["text_post_app_info"]
    if change == "wrong_author": info["reply_to_author"] = {"username":"other"}
    elif change == "wrong_parent": info["parent_id"] = "999"
    elif change == "not_reply": info["is_reply"] = False
    elif change == "missing_item": payload["edges"][1]["node"]["thread_items"].insert(0,{"post":None})
    elif change == "quoted": payload = {"quoted_post":payload}
    assert conversation_posts(payload,"123") == []


def test_pagination_hint_needs_an_exact_matching_query_target():
    assert query_targets_post({"variables":{"input":{"postID":"123"}}}, "123")
    assert not query_targets_post({"variables":{"query":"123", "postID":"999"}}, "123")


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("network or browser startup is forbidden")
    monkeypatch.setattr("requests.sessions.Session.request", forbidden)


def test_query_template_is_bound_to_observed_endpoint_and_header_allowlist():
    headers = {"Content-Type": "application/x-www-form-urlencoded", "X-FB-Friendly-Name": OPERATION,
               "X-IG-App-ID": "synthetic-app", "Cookie": "must-not-leave-browser",
               "Authorization": "must-not-be-replayed", "X-Arbitrary": "drop"}
    template = read_template(URL, "POST", form(), headers)
    assert template["url"] == URL and template["operation"] == OPERATION
    assert template["variables"] == {"postID": "123", "after": None}
    assert template["headers"] == {"content-type": "application/x-www-form-urlencoded",
                                   "x-fb-friendly-name": OPERATION, "x-ig-app-id": "synthetic-app"}


@pytest.mark.parametrize("url, method", [
    ("http://www.threads.com/graphql/query", "POST"),
    ("https://threads.com.evil.example/graphql/query", "POST"),
    ("https://www.instagram.com/graphql/query", "POST"),
    ("https://operator:secret@www.threads.com/graphql/query", "POST"),
    ("https://www.threads.com:444/graphql/query", "POST"),
    ("https://www.threads.com/api/v1/media/configure_text_only_post/", "POST"),
    (URL + "?query=mutation", "POST"), (URL + "#other", "POST"),
    (URL, "GET"), (URL, "PUT"),
])
def test_template_rejects_other_origins_endpoints_and_methods(url, method):
    assert read_template(url, method, form(), {}) is None


@pytest.mark.parametrize("operation", [
    "BarcelonaPostMutation", "BarcelonaPostMutationQuery", "SomeOtherPostQuery",
    "BarcelonaMessagesQuery", "BarcelonaThreadNotificationQuery", "BarcelonaProfilePromotionQuery",
])
def test_template_rejects_mutations_and_non_content_queries(operation):
    assert read_template(URL, "POST", form(fb_api_req_friendly_name=operation), {}) is None


@pytest.mark.parametrize("extra", [
    {"query": "mutation Publish { publish_post }"}, {"mutation": "Publish"},
    {"queries": '{"a":{"query":"mutation Publish { publish_post }"}}'},
])
def test_read_query_cannot_carry_alternate_mutation_or_batch_body(extra):
    assert read_template(URL, "POST", form(**extra), {}) is None


def test_conflicting_operation_header_cannot_be_replayed():
    assert read_template(URL, "POST", form(), {"x-fb-friendly-name": "BarcelonaPostMutation"}) is None


def test_ambiguous_duplicate_doc_id_is_rejected():
    assert read_template(URL, "POST", form() + "&doc_id=987654", {}) is None


@pytest.mark.parametrize("variables", ["[]", "null", '"text"', "{invalid"])
def test_template_rejects_invalid_variable_object(variables):
    assert read_template(URL, "POST", form(variables=variables), {}) is None


def test_terminal_connection_marker_is_bound_to_its_own_post_ids():
    payload = {"data": {"replies": connection("124", terminal=False), "unrelated_feed": connection("999")}}
    projected = connection_info(payload)
    assert {tuple(row["ids"]): row["has_next_page"] for row in projected} == {("124",): True, ("999",): False}
    assert next(row for row in projected if row["ids"] == ["124"])["cursor"] == "next-page"


@pytest.mark.parametrize("branch", ["quoted_post", "quoted_posts", "quote_post", "attachment", "attachments"])
def test_quoted_or_attached_connections_cannot_establish_terminal_evidence(branch):
    payload = {"data": {"replies": connection("124", terminal=False), branch: connection("999")}}
    assert connection_info(payload) == [{"has_next_page": True, "cursor": "next-page", "ids": ["124"]}]


def test_non_post_connection_does_not_establish_terminal_evidence():
    payload = {"profile_cards": {"edges": [{"node": {"username": "operator"}}],
                                 "page_info": {"has_next_page": False, "end_cursor": "unused"}}}
    assert connection_info(payload) == []


@pytest.mark.parametrize("cursor", [None, 123, "x" * 4097])
def test_invalid_or_oversized_cursor_cannot_be_replayed(cursor):
    payload = connection(terminal=False)
    payload["page_info"]["end_cursor"] = cursor
    result = connection_info(payload)
    assert result[0]["cursor"] is None and result[0]["has_next_page"] is True


def reply_context():
    actor = {"id": "88", "username": "operator"}
    evidence = {"id": "123"}
    reply = {"id": "456", "parent_id": "123", "author": "operator", "text": "Exact approved reply",
             "url": "https://www.threads.com/@operator/post/" + id_to_shortcode("456")}
    return reply, [copy.deepcopy(reply)], evidence, actor, reply["text"]


def test_verified_reply_requires_exact_receipt_and_readback():
    result = verified_reply(*reply_context())
    assert result["id"] == "456" and result["parent_id"] == "123"
    assert result["author"] == "operator" and result["text"] == "Exact approved reply"
    assert result["authority"] == "threads_browser_api_readback" and result["verified_at"]


@pytest.mark.parametrize("field, value", [("id", "999"), ("author", "other"), ("text", "Different text"), ("parent_id", "999")])
def test_conflicting_receipt_identity_text_or_parent_is_rejected(field, value):
    receipt, comments, evidence, actor, text = reply_context()
    receipt[field] = value
    with pytest.raises(AdapterError):
        verified_reply(receipt, comments, evidence, actor, text)


@pytest.mark.parametrize("field, value", [("id", "999"), ("author", "other"), ("text", "Different text"), ("parent_id", "999")])
def test_conflicting_readback_identity_text_or_parent_is_rejected(field, value):
    receipt, comments, evidence, actor, text = reply_context()
    comments[0][field] = value
    with pytest.raises(AdapterError):
        verified_reply(receipt, comments, evidence, actor, text)


@pytest.mark.parametrize("kind", ["missing", "duplicate"])
def test_missing_or_ambiguous_readback_is_rejected(kind):
    receipt, comments, evidence, actor, text = reply_context()
    comments = [] if kind == "missing" else comments * 2
    with pytest.raises(AdapterError):
        verified_reply(receipt, comments, evidence, actor, text)


def fake_resources(adapter, *, close_failure=False):
    calls = []
    async def close_page():
        calls.append("close_owned_page")
        if close_failure:
            raise RuntimeError("synthetic tab closure failure")
    async def stop_client():
        calls.append("stop_client")
    async def forbidden():
        pytest.fail("shared browser or context must never be closed")
    adapter.page = SimpleNamespace(close=close_page)
    adapter.pw = SimpleNamespace(stop=stop_client)
    adapter.browser = SimpleNamespace(close=forbidden)
    adapter.context = SimpleNamespace(close=forbidden)
    return calls


def test_close_releases_only_owned_page_and_client_and_is_idempotent():
    adapter = ThreadsBrowserAdapter()
    calls = fake_resources(adapter)
    adapter.events = [{"private_in_memory": "clear"}]
    adapter.prepared = ("prepared",)
    adapter.close()
    adapter.close()
    assert calls == ["close_owned_page", "stop_client"]
    assert adapter.closed and adapter.loop.is_closed() and not adapter.events and adapter.prepared is None
    with pytest.raises(AdapterError, match="browser_adapter_closed"):
        adapter.identity()


def test_close_still_disconnects_client_if_owned_tab_close_fails():
    adapter = ThreadsBrowserAdapter()
    calls = fake_resources(adapter, close_failure=True)
    async def pending():
        await asyncio.sleep(1000)
    task = adapter.loop.create_task(pending())
    adapter.tasks.add(task)
    try:
        adapter.close()
    except RuntimeError:
        pass
    assert calls == ["close_owned_page", "stop_client"]
    assert task.done() and adapter.closed and adapter.loop.is_closed()


def test_provider_exception_is_sanitized_before_leaving_adapter():
    adapter = ThreadsBrowserAdapter()
    fake_resources(adapter)
    async def failing():
        raise RuntimeError("private response and session must not escape")
    try:
        with pytest.raises(AdapterError, match="^threads_browser_operation_failed$"):
            adapter._run(failing)
    finally:
        adapter.close()


@pytest.mark.parametrize("code", [
    "threads_human_verification_required", "threads_login_or_human_verification_required",
    "threads_access_denied_or_rate_limited", "account_mismatch",
    "browser_viewer_identity_unavailable", "browser_viewer_identity_ambiguous",
    "threads_viewer_navigation_mismatch",
])
def test_browser_blocker_stays_blocked_for_remaining_calls(code):
    adapter = ThreadsBrowserAdapter()
    adapter.page = object()  # Already attached; no browser startup can occur.
    calls = []

    async def blocked_read():
        calls.append("first")
        raise AdapterError(code)

    async def another_read():
        calls.append("second")

    try:
        with pytest.raises(AdapterError, match=code):
            adapter._run(blocked_read)
        with pytest.raises(AdapterError, match=code):
            adapter._run(another_read)
        assert calls == ["first"]
    finally:
        adapter.page = None
        adapter.close()


@pytest.mark.parametrize("change, error", [
    ({"text": "changed"}, "target_changed_recollect_required"),
    ({"comments_complete": False}, "duplicate_check_incomplete"),
    ({"has_viewer_replied": None, "comments_complete": False}, "duplicate_check_incomplete"),
    ({"has_viewer_replied": True}, "account_already_replied"),
    ({"can_reply": False}, "threads_reply_access_unavailable"),
])
def test_publication_preflight_fails_closed_on_changed_or_incomplete_evidence(monkeypatch, change, error):
    adapter = ThreadsBrowserAdapter()
    actor = {"id": "88", "username": "operator"}
    evidence = {"id": "123", "author": "creator", "text": "approved target", "url": "https://www.threads.com/t/B7",
                "media_type": "TEXT_POST", "published_at": "2026-09-10T00:00:00+00:00"}
    current = {**evidence, "comments": [], "comments_complete": True, "has_viewer_replied": False, "can_reply": True, **change}
    monkeypatch.setattr(adapter, "identity", lambda expected: actor)
    monkeypatch.setattr(adapter, "collect", lambda *args: current)
    try:
        with pytest.raises(AdapterError, match=error):
            adapter.preflight(evidence, {"account": "operator"}, actor)
        assert adapter.prepared is None
    finally:
        adapter.close()


def test_publish_cannot_reach_any_browser_control_without_preflight():
    adapter = ThreadsBrowserAdapter()
    try:
        with pytest.raises(AdapterError, match="browser_publication_preflight_required"):
            adapter.loop.run_until_complete(adapter._publish({"id": "123"}, {"id": "88", "username": "operator"}, "text", lambda: pytest.fail("guard invoked")))
    finally:
        adapter.close()
