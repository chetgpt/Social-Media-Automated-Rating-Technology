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


def relay_post_page():
    root = raw_post()
    reply = raw_post("124", "responder")
    reply["text_post_app_info"].update(is_reply=True, reply_to_author={"username": "creator"})
    followup = raw_post("125", "responder")
    followup["text_post_app_info"].update(is_reply=True, reply_to_author={"username": "responder"})
    root["text_post_app_info"]["direct_replies"] = {
        "edges": [{"node": {"posts": {
            "edges": [{"node": reply}, {"node": followup}],
            "page_info": {"has_next_page": False, "end_cursor": None},
        }}}],
        "page_info": {"has_next_page": True, "end_cursor": "next-reply-thread"},
    }
    return {"data": {"media": root}}


def test_relay_post_page_binds_chains_to_exact_root_and_uses_outer_pagination():
    payload = relay_post_page()
    replies = conversation_posts(payload, "123")
    assert [(p["id"], p["parent_id"], p["root_id"]) for p in replies] == [
        ("124", "123", "123"), ("125", "124", "123"),
    ]
    assert conversation_posts(payload, "999") == []
    assert connection_info(payload) == [
        {"has_next_page": True, "cursor": "next-reply-thread", "ids": ["123", "124", "125"]},
    ]


@pytest.mark.parametrize("change", ["wrong_author", "wrong_parent", "missing_predecessor", "quoted"])
def test_relay_post_page_does_not_bind_ambiguous_replies(change):
    payload = relay_post_page()
    edges = payload["data"]["media"]["text_post_app_info"]["direct_replies"]["edges"][0]["node"]["posts"]["edges"]
    if change == "wrong_author":
        edges[0]["node"]["text_post_app_info"]["reply_to_author"] = {"username": "someone_else"}
    elif change == "wrong_parent":
        edges[0]["node"]["text_post_app_info"]["parent_id"] = "999"
    elif change == "missing_predecessor":
        edges[0] = {"node": None}
    else:
        payload = {"quoted_post": payload}
    assert conversation_posts(payload, "123") == []


def test_relay_missing_visibility_flag_is_not_complete_visibility_evidence():
    from social_engage.threads_browser import reply_visibility
    payload = relay_post_page()
    assert reply_visibility(payload) == {"unavailable_replies": False, "reply_visibility_unknown": True}
    payload["data"]["media"]["text_post_app_info"]["direct_replies"]["show_unavailable_replies_disclaimer"] = False
    assert reply_visibility(payload) == {"unavailable_replies": False, "reply_visibility_unknown": False}


def coverage_fixture():
    from social_engage.threads_browser import comment_coverage, post_reply_state
    from social_engage.threads_data import project_post
    payload = relay_post_page()
    raw = payload["data"]["media"]
    raw["text_post_app_info"].update(direct_reply_count=1, has_unavailable_replies=False)
    replies = raw["text_post_app_info"]["direct_replies"]
    replies["page_info"] = {"has_next_page": False, "end_cursor": None}
    replies["edges"][0]["node"]["posts"]["edges"].pop()
    root = project_post(raw)
    comments = conversation_posts(payload, "123")
    state = post_reply_state(payload, "123")
    return payload, root, comments, state, comment_coverage


def test_target_root_visibility_survives_omitted_fields_and_unrelated_metadata():
    from social_engage.threads_browser import post_reply_state
    payload, root, comments, states, coverage = coverage_fixture()
    raw = payload["data"]["media"]
    partial = {"data": {"media": {"id": raw["id"], "text_post_app_info": {
        "direct_replies": raw["text_post_app_info"]["direct_replies"]}}}}
    payload["unrelated"] = {"data": {"media": raw_post("999")}}
    assert len(post_reply_state(payload, "123")) == 1
    assert post_reply_state({"data": {"media": raw_post()}}, "123") == []
    later = post_reply_state(partial, "123", page_root_hint=raw)
    assert later[0]["root_unavailable_replies"] is None
    result = coverage(root, comments, [{"reply_state": states}, {"reply_state": later}], 10)
    assert result["complete"] and result["visibility_verified"]
    assert result["reasons"] == []


@pytest.mark.parametrize("change, reason", [
    ("missing_visibility", "reply_visibility_unverified"),
    ("hidden", "unavailable_replies"),
    ("child_open", "nested_reply_pages_remaining"),
    ("child_unknown", "nested_reply_pagination_unverified"),
    ("later_outer_open", "outer_reply_pagination_unverified"),
])
def test_coverage_retains_actual_unknown_hidden_nested_and_latest_outer_limits(change, reason):
    _, root, comments, states, coverage = coverage_fixture()
    states = copy.deepcopy(states)
    events = [{"reply_state": states}]
    if change == "missing_visibility":
        states[0]["root_unavailable_replies"] = states[0]["unavailable_replies"] = None
    elif change == "hidden":
        hidden = copy.deepcopy(states)
        hidden[0]["unavailable_replies"] = True
        events.insert(0, {"reply_state": hidden})
    elif change.startswith("child_"):
        states[0]["nested_pages"][0]["has_next_page"] = True if change == "child_open" else None
    else:
        later = copy.deepcopy(states)
        later[0]["has_next_page"] = True
        events.append({"reply_state": later})
    result = coverage(root, comments, events, 10)
    assert not result["complete"] and reason in result["reasons"]


def test_only_target_outer_connection_controls_post_pagination():
    from social_engage.threads_browser import post_connections, post_reply_state
    payload = relay_post_page()
    unrelated = raw_post("999")
    unrelated["text_post_app_info"]["direct_replies"] = {"edges": [], "page_info": {"has_next_page": False}}
    payload["other"] = {"data": {"media": unrelated}}
    states = post_reply_state(payload, "123")
    assert post_connections(states) == [{"has_next_page": True, "cursor": "next-reply-thread", "ids": ["123"]}]
    assert states[0]["nested_pages"] == [{"id": "124", "has_next_page": False}]


def test_legacy_reply_page_needs_root_or_exact_query_hint():
    from social_engage.threads_browser import post_connections, post_reply_state
    from social_engage.threads_data import project_post
    payload = reply_connection()
    root = project_post(payload["edges"][0]["node"]["thread_items"][0]["post"])
    assert post_connections(post_reply_state(payload, "123"))[0]["ids"] == ["123"]
    payload["edges"].pop(0)
    assert post_reply_state(payload, "123") == []
    assert post_reply_state(payload, "123", root_hint=root)[0]["unavailable_replies"] is False
    del payload["show_unavailable_replies_disclaimer"]
    assert post_reply_state(payload, "123", root_hint=root) == []


def test_ancillary_legacy_terminal_cannot_override_current_outer_connection():
    from social_engage.threads_browser import comment_coverage, post_reply_state
    from social_engage.threads_data import project_post
    payload, root, comments, _, _ = coverage_fixture()
    raw = payload["data"]["media"]
    raw["text_post_app_info"]["direct_replies"]["page_info"] = {"has_next_page": True, "end_cursor": "outer-next"}
    ancillary = {"show_unavailable_replies_disclaimer": False, "edges": [],
                 "page_info": {"has_next_page": False, "end_cursor": None}}
    payload["ancillary"] = ancillary
    states = post_reply_state(payload, "123", root_hint=root)
    assert len(states) == 1 and states[0]["has_next_page"] is True
    assert not comment_coverage(root, comments, [{"reply_state": states}], 10)["complete"]
    assert post_reply_state(ancillary, "123", root_hint=root) == []
    unrelated = reply_connection()
    unrelated["edges"][0]["node"]["thread_items"][0]["post"] = raw_post("999", "someone_else")
    unrelated["edges"][1]["node"]["thread_items"][0]["post"]["text_post_app_info"]["reply_to_author"] = {"username": "someone_else"}
    assert post_reply_state(unrelated, "123", root_hint=project_post(raw)) == []


@pytest.mark.parametrize("known_owner", [True, False])
@pytest.mark.parametrize("visibility_verified", [True, False])
def test_collect_relay_exact_post_route_and_complete_visibility(known_owner, visibility_verified):
    from social_engage.threads_browser import reply_visibility
    from social_engage.threads_data import collect_posts
    payload = relay_post_page()
    raw_root = payload["data"]["media"]
    info = raw_root["text_post_app_info"]
    info["direct_reply_count"] = 1
    connection = info["direct_replies"]
    connection["page_info"] = {"has_next_page": False, "end_cursor": None}
    connection["edges"][0]["node"]["posts"]["edges"].pop()
    if visibility_verified:
        connection["show_unavailable_replies_disclaimer"] = False
    actor = {"id": "99", "username": "operator"}
    payload["viewer"] = actor.copy()
    adapter = ThreadsBrowserAdapter.__new__(ThreadsBrowserAdapter)
    adapter.page = SimpleNamespace(url="")
    visited = []

    async def navigate(url, kind):
        visited.append(url)
        adapter.page.url = url
        bound = {p["id"]: p for p in conversation_posts(payload, "123")}
        adapter.events = [{"posts": [bound.get(p["id"], p) for p in collect_posts(payload)],
                           "connections": connection_info(payload), **reply_visibility(payload)}]
        return payload

    async def paginate(enough):
        assert enough(adapter._posts())

    adapter._navigate, adapter._paginate = navigate, paginate
    scope = {"source": "post", "account": "operator", "target": "123", "comments": 1}
    if known_owner:
        scope["target_owner"] = "creator"
    result = asyncio.run(adapter._collect("123", scope, actor))
    path = "/@creator/post/" if known_owner else "/t/"
    assert visited == ["https://www.threads.com" + path + id_to_shortcode("123")]
    assert result["id"] == "123" and [p["id"] for p in result["comments"]] == ["124"]
    assert result["comments"][0]["parent_id"] == "123"
    assert result["comments_complete"] is visibility_verified


def test_collect_stops_at_comment_cap_including_nested_replies():
    from social_engage.threads_browser import reply_visibility
    from social_engage.threads_data import collect_posts

    payload = relay_post_page()
    info = payload["data"]["media"]["text_post_app_info"]
    info["direct_reply_count"] = 10
    info["direct_replies"]["edges"][0]["node"]["posts"]["edges"][0]["node"]["text_post_app_info"]["direct_reply_count"] = 1
    actor = {"id": "99", "username": "operator"}
    payload["viewer"] = actor.copy()
    adapter = ThreadsBrowserAdapter.__new__(ThreadsBrowserAdapter)

    async def forbidden_page_fetch(*args):
        pytest.fail("the verified direct and nested replies already fill the comment cap")

    async def guard():
        pass

    adapter.page = SimpleNamespace(url="", evaluate=forbidden_page_fetch,
                                   mouse=SimpleNamespace(wheel=forbidden_page_fetch))
    adapter._page_guard, adapter._replay = guard, forbidden_page_fetch

    async def navigate(url, kind):
        adapter.page.url = url
        bound = {p["id"]: p for p in conversation_posts(payload, "123")}
        adapter.events = [{"posts": [bound.get(p["id"], p) for p in collect_posts(payload)],
                           "connections": connection_info(payload), **reply_visibility(payload)}]
        return payload

    adapter._navigate = navigate
    scope = {"source": "post", "account": "operator", "target": "123", "comments": 2}
    result = asyncio.run(adapter._collect("123", scope, actor))
    assert [(p["id"], p["parent_id"]) for p in result["comments"]] == [("124", "123"), ("125", "124")]
    assert result["comments_complete"] is False


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
