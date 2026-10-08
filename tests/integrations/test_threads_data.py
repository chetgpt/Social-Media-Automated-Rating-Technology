"""Synthetic browser JSON only; no live browser, network, or operational data."""
import copy
import json

import pytest

from social_engage.adapters import AdapterError
from social_engage.threads_data import (
    browser_post_id, collect_posts, find_viewer, id_to_shortcode, project_post, shortcode_to_id,
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
