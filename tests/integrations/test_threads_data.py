"""Synthetic browser JSON only; no live browser, network, or operational data."""
import copy
import json

import pytest

from social_engage.adapters import AdapterError
from social_engage.threads_data import (
    browser_post_id, collect_posts, find_viewer, id_to_shortcode, post_page_connections,
    project_post, shortcode_to_id,
)


def post(post_id="1234567890123456789", owner="creator", **extra):
    code = id_to_shortcode(post_id)
    return {
        "pk": post_id, "id": post_id + "_42", "code": code,
        "canonical_url": f"https://www.threads.com/@{owner}/post/{code}?discard=signed-value",
        "caption": {"text": "Public caption\nSecond line"}, "taken_at": 1789084800,
        "user": {"pk": "42", "username": owner}, "media_type": 19, "like_count": 12,
        "text_post_app_info": {"direct_reply_count": 3, "quote_count": 0, "repost_count": 2,
            "has_viewer_replied": False, "can_reply": True, "is_reply": False, "is_post_unavailable": False},
        **extra,
    }


@pytest.mark.parametrize("value", ["1", "63", "64", "1234567890123456789", "9" * 40])
def test_shortcode_round_trip(value):
    assert shortcode_to_id(id_to_shortcode(value)) == value


@pytest.mark.parametrize("value", ["", "A", "AB", "B=", "B/", "B%20", 1, None, "_" * 24])
def test_noncanonical_shortcode_rejected(value):
    with pytest.raises(AdapterError):
        shortcode_to_id(value)


@pytest.mark.parametrize("value", [0, True, -1, "0", "01", "1_42", " 1", "1.0", "9" * 41])
def test_invalid_numeric_id_rejected(value):
    with pytest.raises(AdapterError):
        id_to_shortcode(value)


@pytest.mark.parametrize("host", ["threads.com", "www.threads.com", "threads.net", "www.threads.net"])
def test_post_url_and_short_link_identity(host):
    code = id_to_shortcode("12345")
    for path in (f"/@Creator/post/{code}", f"/t/{code}/"):
        assert browser_post_id(f"https://{host}{path}?access_token=ignored#ignored") == "12345"
    assert browser_post_id(12345) == "12345"
    assert browser_post_id("12345") == "12345"


def test_live_null_canonical_url_uses_redundant_api_identity():
    raw = post(canonical_url=None)
    result = project_post(raw)
    assert result["url"] == "https://www.threads.com/@creator/post/" + raw["code"]
    assert result["url_identity_basis"] == "constructed_from_api_identity"


@pytest.mark.parametrize("value", [
    "http://www.threads.com/@creator/post/B", "https://www.threads.com.evil.invalid/@creator/post/B",
    "https://user:secret@www.threads.com/@creator/post/B", "https://www.threads.com:443/@creator/post/B",
    "https://www.threads.com:invalid/@creator/post/B", "https://www.threads.com/@creator/post/B/extra",
    "https://www.threads.com/@creator/post/%42", "https://www.threads.com/@creator/post/B\n",
    "https://www.threads.com\\@evil.invalid/@creator/post/B", "https://www.threads.com/@creator/post/AB",
    "https://www.threads.com/@creator/post/B?x=\tsecret", None, True, "B",
])
def test_unsafe_or_ambiguous_urls_rejected(value):
    with pytest.raises(AdapterError):
        browser_post_id(value)


def test_projection_whitelist_utc_and_known_metrics():
    raw = post(access_token="BACKEND_SECRET", media_url="SIGNED_MEDIA", image_versions2={"candidates": ["SIGNED_MEDIA"]},
               unknown={"cookie": "BACKEND_SECRET"}, video_versions=[{"url": "SIGNED_MEDIA"}])
    raw["user"]["sessionid"] = "BACKEND_SECRET"
    raw["text_post_app_info"]["quote_post"] = post("123")
    raw["caption"]["unknown"] = "BACKEND_SECRET"
    result = project_post(raw)
    assert result["id"] == raw["pk"] and result["author"] == "creator"
    assert result["url"] == raw["canonical_url"].split("?")[0]
    assert result["published_at"] == "2026-09-11T00:00:00+00:00"
    assert result["media_type"] == "TEXT_POST"
    assert result["metrics"] == {"likes": 12, "replies": 3, "quotes": 0, "reposts": 2}
    assert result["direct_reply_count"] == 3 and result["has_replies"] is True
    assert result["has_viewer_replied"] is False and result["can_reply"] is True
    assert result["parent_id"] == "" and result["root_id"] == ""
    assert result["music"]["status"] == result["transcript"]["status"] == "unsupported"
    serialized = json.dumps(result)
    for secret in ("BACKEND_SECRET", "SIGNED_MEDIA", "signed-value", "access_token", "image_versions2", "sessionid"):
        assert secret not in serialized


def test_caption_is_public_text_not_backend_secret_material():
    raw = post(caption={"text": "Public author text about access_token and sessionid."}, access_token="private")
    assert project_post(raw)["text"] == raw["caption"]["text"]


@pytest.mark.parametrize("change", [
    {"pk": "123"}, {"id": "124_42"}, {"id": "1234567890123456789_43"}, {"id": "123_42_99"},
    {"id": True}, {"code": "B"}, {"user": {"pk": "42", "username": "other"}},
    {"user": {"pk": "42", "id": "43", "username": "creator"}},
    {"canonical_url": "https://www.threads.com/@other/post/" + id_to_shortcode("1234567890123456789")},
    {"canonical_url": "https://www.threads.com/t/" + id_to_shortcode("1234567890123456789")},
    {"canonical_url": "https://www.threads.com/@creator/post/B"},
])
def test_mismatched_post_or_owner_rejected(change):
    with pytest.raises(AdapterError):
        project_post(post(**change))


@pytest.mark.parametrize("timestamp", [None, True, "1789084800", 1789084800.0, 0, -1, 10 ** 40, {}, []])
def test_malformed_timestamp_never_becomes_valid_evidence(timestamp):
    with pytest.raises(AdapterError, match="invalid_browser_post_timestamp"):
        project_post(post(taken_at=timestamp))


def test_missing_metrics_and_unknown_booleans_stay_unknown():
    raw = post(like_count=True, view_count=-1, text_post_app_info={"direct_reply_count": "0", "has_viewer_replied": 0,
                  "can_reply": "true", "quote_count": None, "repost_count": 1.2})
    result = project_post(raw)
    assert result["metrics"] == {} and result["metrics_status"] == "not_provided"
    for key in ("direct_reply_count", "has_replies", "has_viewer_replied", "can_reply", "is_reply"):
        assert result[key] is None


@pytest.mark.parametrize("unavailable", [True, "false", 1, {}])
def test_unavailable_or_malformed_availability_rejected(unavailable):
    raw = post()
    raw["text_post_app_info"]["is_post_unavailable"] = unavailable
    with pytest.raises(AdapterError, match="browser_post_unavailable"):
        project_post(raw)


def test_parent_author_alone_does_not_invent_parent_post_identity():
    raw = post()
    raw["text_post_app_info"].update(is_reply=True, reply_to_author={"username": "Parent", "pk": "99"})
    projected = project_post(raw)
    assert projected["reply_to_author"] == "parent" and projected["parent_id"] == ""
    raw["text_post_app_info"].update(reply_to_post_id="123", root_post={"pk": "456"})
    projected = project_post(raw)
    assert projected["parent_id"] == "123" and projected["root_id"] == "456"
    raw["parent_id"] = "124"
    with pytest.raises(AdapterError, match="browser_reply_identity_mismatch"):
        project_post(raw)


def test_only_thread_items_are_projected_ordered_and_deduplicated():
    first, second, quoted, malformed = post("123"), post("124"), post("125"), post("126", taken_at=None)
    first["text_post_app_info"]["quote_post"] = quoted
    first["thread_items"] = [{"post": quoted}]
    payload = {"data": {"feedData": {"edges": [
        {"node": {"thread_items": [{"post": first}, {"post": malformed}]}},
        {"node": {"thread_items": [{"post": second}, {"post": first}, None]}}]}},
        "post": quoted, "attachments": {"thread_items": [{"post": quoted}]}}
    original = copy.deepcopy(payload)
    assert [row["id"] for row in collect_posts(payload)] == ["123", "124"]
    assert payload == original


def post_page_payload():
    root = post("123", canonical_url=None)
    first = post("124", owner="responder", canonical_url=None)
    second = post("125", owner="other", canonical_url=None)
    for raw, author in ((first, "creator"), (second, "responder")):
        raw["text_post_app_info"].update(is_reply=True, reply_to_author={"username": author})
    inner = {"edges": [{"node": first}, {"node": second}],
             "page_info": {"has_next_page": False, "end_cursor": None}}
    outer = {"edges": [{"node": {"posts": inner}}],
             "page_info": {"has_next_page": True, "end_cursor": "synthetic-next-page"}}
    root["text_post_app_info"]["direct_replies"] = outer
    root["text_post_app_info"]["self_thread"] = {"posts": {"edges": [{"node": post("126")}]}}
    return {"require": [["module", None, {"__bbox": {"result": {"data": {"media": root}}}}]]}


def test_post_page_schema_collects_root_and_reply_chains_without_mutating_payload():
    payload = post_page_payload()
    original = copy.deepcopy(payload)
    connection = list(post_page_connections(payload))[0]
    assert connection["root"]["pk"] == "123"
    assert [item["post"]["pk"] for item in connection["threads"][0]] == ["124", "125"]
    assert connection["page_info"] == {"has_next_page": True, "end_cursor": "synthetic-next-page"}
    assert connection["thread_page_info"] == [{"has_next_page": False, "end_cursor": None}]
    assert connection["has_reply_connection"] is True
    assert connection["root_unavailable_replies"] is None
    assert connection["unavailable_replies"] is None
    assert [row["id"] for row in collect_posts(payload)] == ["123", "124", "125"]
    assert payload == original


def test_post_page_schema_deduplicates_legacy_root_and_repeated_reply():
    payload = post_page_payload()
    connection = list(post_page_connections(payload))[0]
    replies = connection["root"]["text_post_app_info"]["direct_replies"]
    replies["edges"].append({"node": {"posts": {"edges": [{"node": connection["threads"][0][0]["post"]}]}}})
    mixed = {"legacy": {"thread_items": [{"post": connection["root"]}]}, "post_page": payload}
    assert [row["id"] for row in collect_posts(mixed)] == ["123", "124", "125"]


@pytest.mark.parametrize("branch", ["quoted_post", "quoted_posts", "attachments"])
def test_post_page_schema_is_not_collected_inside_excluded_branches(branch):
    payload = {branch: post_page_payload()}
    assert list(post_page_connections(payload)) == []
    assert collect_posts(payload) == []


@pytest.mark.parametrize("change", ["invalid_root", "no_data_media_carrier"])
def test_post_page_schema_requires_valid_root_and_exact_carrier(change):
    payload = post_page_payload()
    root = list(post_page_connections(payload))[0]["root"]
    if change == "invalid_root":
        root["code"] = id_to_shortcode("999")
    else:
        payload = {"unrelated": root}
    assert list(post_page_connections(payload)) == []
    assert collect_posts(payload) == []


def test_post_page_schema_preserves_malformed_predecessors_for_reply_binding():
    payload = post_page_payload()
    connection = list(post_page_connections(payload))[0]
    posts = connection["root"]["text_post_app_info"]["direct_replies"]["edges"][0]["node"]["posts"]
    invalid = post("127", taken_at=None)
    posts["edges"] = [{"node": None}, {"node": invalid}, posts["edges"][1]]
    connection = list(post_page_connections(payload))[0]
    assert connection["threads"][0][0] == {"post": None}
    assert connection["threads"][0][1]["post"] is invalid
    assert len(connection["threads"][0]) == 3
    assert [row["id"] for row in collect_posts(payload)] == ["123", "125"]


@pytest.mark.parametrize("key", ["show_unavailable_replies_disclaimer", "has_unavailable_replies"])
def test_post_page_schema_retains_only_observed_boolean_reply_availability(key):
    payload = post_page_payload()
    root = list(post_page_connections(payload))[0]["root"]
    info = root["text_post_app_info"]
    info[key] = False
    assert list(post_page_connections(payload))[0]["unavailable_replies"] is False
    info["direct_replies"]["show_unavailable_replies_disclaimer"] = True
    assert list(post_page_connections(payload))[0]["unavailable_replies"] is True


@pytest.mark.parametrize("value, expected", [(False, False), (True, True), (None, None), (0, None), ("false", None)])
def test_post_page_root_visibility_requires_observed_boolean_on_current_carrier(value, expected):
    _, root, fragment = split_post_page_payload()
    root["text_post_app_info"]["has_unavailable_replies"] = True
    payload = {"data": {"media": fragment}}
    connection, = post_page_connections(payload, root_hint=root)
    assert connection["root_unavailable_replies"] is None
    assert connection["unavailable_replies"] is None
    fragment["text_post_app_info"]["has_unavailable_replies"] = value
    connection, = post_page_connections(payload, root_hint=root)
    assert connection["root_unavailable_replies"] is expected
    assert connection["unavailable_replies"] is expected


def test_post_page_inner_pagination_is_bounded_allowlisted_and_parallel_to_threads():
    payload = post_page_payload()
    root = list(post_page_connections(payload))[0]["root"]
    edges = root["text_post_app_info"]["direct_replies"]["edges"]
    edges[0]["node"]["posts"]["page_info"] = {
        "has_next_page": True, "end_cursor": "nested-next", "fetch_token": "must-not-project"}
    edges.extend([
        {"node": {"posts": {"edges": [], "page_info": {"has_next_page": 1, "end_cursor": "x" * 4097}}}},
        {"node": {"posts": {"edges": None, "page_info": {"has_next_page": False, "end_cursor": 7}}}},
        None,
    ])
    original = copy.deepcopy(payload)
    connection, = post_page_connections(payload)
    assert len(connection["threads"]) == len(connection["thread_page_info"]) == 4
    assert connection["thread_page_info"] == [
        {"has_next_page": True, "end_cursor": "nested-next"},
        {"has_next_page": None, "end_cursor": None},
        {"has_next_page": False, "end_cursor": None},
        {"has_next_page": None, "end_cursor": None},
    ]
    assert payload == original


def split_post_page_payload():
    root = list(post_page_connections(post_page_payload()))[0]["root"]
    fragment = {"id": root["id"], "text_post_app_info": {
        "direct_replies": root["text_post_app_info"].pop("direct_replies")}}
    # The observed bootstrap supplies the connection before the full root and
    # also includes another partial identity carrier for that same post.
    partial = {"pk": root["pk"], "id": root["id"], "user": copy.deepcopy(root["user"])}
    payload = {"require": [["module", None, {"__bbox": {"result": [
        {"data": {"media": fragment}}, {"data": {"media": partial}},
        {"data": {"media": root}}]}}]]}
    return payload, root, fragment


@pytest.mark.parametrize("fragment_id", ["123", 123, "123_42"])
def test_split_post_page_reply_first_joins_exact_identity_without_field_merge(fragment_id):
    payload, root, fragment = split_post_page_payload()
    fragment["id"] = fragment_id
    original = copy.deepcopy(payload)
    connections = list(post_page_connections(payload))
    assert len(connections) == 1
    connection = connections[0]
    assert connection["root"] is root
    assert "direct_replies" not in root["text_post_app_info"]
    assert [item["post"]["pk"] for item in connection["threads"][0]] == ["124", "125"]
    assert connection["page_info"]["has_next_page"] is True
    assert [row["id"] for row in collect_posts(payload)] == ["123", "124", "125"]
    assert payload == original


@pytest.mark.parametrize("change", [
    {"id": "999"}, {"id": "123_43"}, {"id": "123_42_9"}, {"id": "0123"},
    {"pk": "999"}, {"user": {"pk": "43"}}, {"user": {"id": "43"}},
    {"user": {"pk": "42", "id": "43"}}, {"user": {"username": "other"}},
])
def test_split_post_page_rejects_unmatched_or_conflicting_fragment_identity(change):
    payload, root, fragment = split_post_page_payload()
    fragment.update(change)
    assert list(post_page_connections(payload)) == [{
        "root": root, "threads": [], "page_info": None, "thread_page_info": [],
        "has_reply_connection": False, "root_unavailable_replies": None, "unavailable_replies": None}]
    assert [row["id"] for row in collect_posts(payload)] == ["123"]


def test_split_post_page_requires_full_validated_root_in_same_payload():
    _, _, fragment = split_post_page_payload()
    assert list(post_page_connections({"data": {"media": fragment}})) == []
    assert collect_posts({"data": {"media": fragment}}) == []


@pytest.mark.parametrize("malformed_connection", [False, True])
def test_bare_post_page_root_preserves_metadata_with_unknown_completeness(malformed_connection):
    root = post("123")
    if malformed_connection:
        root["text_post_app_info"]["direct_replies"] = {"edges": None,
            "page_info": {"has_next_page": False, "end_cursor": None}}
    payload = {"data": {"media": root}}
    assert list(post_page_connections(payload)) == [{
        "root": root, "threads": [], "page_info": None, "thread_page_info": [],
        "has_reply_connection": False, "root_unavailable_replies": None, "unavailable_replies": None}]
    assert [row["id"] for row in collect_posts(payload)] == ["123"]


def test_split_post_page_rejects_ambiguous_full_root_identity():
    payload, _, _ = split_post_page_payload()
    conflict = post("123", owner="other", canonical_url=None, id="123_43",
                    user={"pk": "43", "username": "other"})
    payload["other"] = {"data": {"media": conflict}}
    assert list(post_page_connections(payload)) == []
    assert collect_posts(payload) == []


@pytest.mark.parametrize("fragment_id", ["123", 123, "123_42"])
def test_post_page_hint_binds_partial_pagination_without_merging_or_mutation(fragment_id):
    _, root, fragment = split_post_page_payload()
    fragment["id"] = fragment_id
    fragment["text_post_app_info"]["id"] = "synthetic-info-id"
    payload = {"data": {"media": fragment}}
    original_payload, original_root = copy.deepcopy(payload), copy.deepcopy(root)
    connection, = post_page_connections(payload, root_hint=root)
    assert connection["root"] is root
    assert connection["page_info"]["has_next_page"] is True
    assert [item["post"]["pk"] for item in connection["threads"][0]] == ["124", "125"]
    assert [row["id"] for row in collect_posts(payload, root_hint=root)] == ["123", "124", "125"]
    assert list(post_page_connections(payload)) == []
    assert payload == original_payload and root == original_root


@pytest.mark.parametrize("change", [
    {"id": "999"}, {"id": "123_43"}, {"id": "123_42_9"}, {"pk": "999"},
    {"user": {"pk": "43"}}, {"user": {"username": "other"}},
])
def test_post_page_hint_rejects_unmatched_or_conflicting_fragment_identity(change):
    _, root, fragment = split_post_page_payload()
    fragment.update(change)
    payload = {"data": {"media": fragment}}
    assert list(post_page_connections(payload, root_hint=root)) == []
    assert collect_posts(payload, root_hint=root) == []


@pytest.mark.parametrize("invalid_hint", ["normalized", "incomplete", "wrong_code"])
def test_post_page_hint_requires_full_root_revalidation(invalid_hint):
    _, root, fragment = split_post_page_payload()
    if invalid_hint == "normalized":
        root = project_post(root)
    elif invalid_hint == "incomplete":
        root = {"pk": "123", "id": "123_42", "user": root["user"]}
    else:
        root["code"] = id_to_shortcode("999")
    payload = {"data": {"media": fragment}}
    assert list(post_page_connections(payload, root_hint=root)) == []
    assert collect_posts(payload, root_hint=root) == []


def test_post_page_hint_alone_does_not_add_unobserved_connection_or_root():
    root = list(post_page_connections(post_page_payload()))[0]["root"]
    assert list(post_page_connections({}, root_hint=root)) == []
    assert collect_posts({}, root_hint=root) == []


def test_post_page_hint_rejects_current_root_owner_conflict():
    payload = post_page_payload()
    current_root = list(post_page_connections(payload))[0]["root"]
    prior_root = post("123", owner="other", canonical_url=None)
    assert list(post_page_connections(payload, root_hint=prior_root)) == [{
        "root": current_root, "threads": [], "page_info": None, "thread_page_info": [],
        "has_reply_connection": False, "root_unavailable_replies": None, "unavailable_replies": None}]
    assert [row["id"] for row in collect_posts(payload, root_hint=prior_root)] == ["123"]


def test_post_page_hint_uses_current_valid_root_without_stale_metadata_merge():
    payload = post_page_payload()
    current_root = list(post_page_connections(payload))[0]["root"]
    prior_root = post("123", canonical_url=None, like_count=4)
    connection, = post_page_connections(payload, root_hint=prior_root)
    assert connection["root"] is current_root
    assert collect_posts(payload, root_hint=prior_root)[0]["metrics"]["likes"] == 12


def test_traversal_handles_cycle_and_fails_closed_at_depth_limit():
    payload = {"thread_items": [{"post": post()}]}
    payload["loop"] = payload
    assert len(collect_posts(payload)) == 1
    for _ in range(42):
        payload = {"nested": payload}
    with pytest.raises(AdapterError, match="browser_payload_traversal_limit"):
        collect_posts(payload)


def test_only_explicit_viewer_identity_is_accepted_and_sensitive_fields_dropped():
    payload = {"bootstrap": [{"viewer": {"id": "88", "username": "Operator", "fbid": "private", "token": "private"}},
                              {"viewer": {"user": {"pk": "88", "username": "operator"}}}],
               "user": {"id": "99", "username": "unrelated"},
               "CurrentUserInitialData": {"viewer": {"id": "99", "username": "untrusted"}},
               "post": {"viewer": {"id": "99", "username": "unrelated"}}}
    assert find_viewer(payload) == {"id": "88", "username": "operator"}


@pytest.mark.parametrize("payload", [
    {"CurrentUserInitialData": {"id": "88", "username": "operator"}},
    {"user": {"id": "88", "username": "operator"}},
    {"viewer": {"id": "0", "username": "operator"}},
    {"viewer": {"id": "88", "pk": "99", "username": "operator"}},
    {"viewer": {"id": True, "username": "operator"}},
])
def test_missing_or_malformed_viewer_is_not_guessed(payload):
    with pytest.raises(AdapterError, match="browser_viewer_identity_unavailable"):
        find_viewer(payload)


def test_conflicting_viewer_identity_fails_closed():
    payload = [{"viewer": {"id": "88", "username": "operator"}},
               {"viewer": {"id": "99", "username": "other"}}]
    with pytest.raises(AdapterError, match="browser_viewer_identity_ambiguous"):
        find_viewer(payload)
