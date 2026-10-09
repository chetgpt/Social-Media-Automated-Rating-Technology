"""Nested reply reads use only verified conversation members and observed queries."""
import asyncio
import copy
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode

import pytest

from social_engage.adapters import AdapterError
from social_engage.threads_browser import ThreadsBrowserAdapter, comment_coverage, read_template
from social_engage.threads_data import id_to_shortcode, project_post


URL = "https://www.threads.com/graphql/query"
OPERATION = "BarcelonaPostPageDirectRepliesRefetchQuery"


def raw_post(identity, author, *, reply_to=None, replies=0):
    info = {"direct_reply_count": replies, "has_unavailable_replies": False}
    if reply_to:
        info.update(is_reply=True, reply_to_author={"username": reply_to})
    return {"pk": identity, "id": identity + "_88", "code": id_to_shortcode(identity),
            "user": {"pk": "88", "username": author}, "taken_at": 1789000000,
            "caption": {"text": "Synthetic reply " + identity}, "media_type": 19,
            "text_post_app_info": info}


def reply_page(raw_root, replies, *, cursor=None, thread_pending=False, partial=False):
    root = {"id": raw_root["id"]} if partial else copy.deepcopy(raw_root)
    root.setdefault("text_post_app_info", {})["direct_replies"] = {
        "edges": [{"node": {"posts": {
            "edges": [{"node": reply}],
            "page_info": {"has_next_page": thread_pending, "end_cursor": None},
        }}} for reply in replies],
        "page_info": {"has_next_page": cursor is not None, "end_cursor": cursor},
        "show_unavailable_replies_disclaimer": False,
    }
    return {"data": {"media": root}}


def response(body, payload):
    async def text():
        return json.dumps(payload)
    return SimpleNamespace(url=URL, status=200, text=text,
                           request=SimpleNamespace(method="POST", post_data=body, headers={}))


async def conversation_fixture(*, outer_cursor=None, child_count=2):
    root = raw_post("123", "creator", replies=1)
    child = raw_post("124", "responder", reply_to="creator", replies=child_count)
    original_variables = {"postID": "123", "after": "observed-outer-cursor",
                          "first": 7, "__relay_internal__pv__example": True}
    body = urlencode({"doc_id": "123456", "fb_api_req_friendly_name": OPERATION,
                      "variables": json.dumps(original_variables)})
    adapter = ThreadsBrowserAdapter.__new__(ThreadsBrowserAdapter)
    adapter.generation, adapter.kind, adapter.post_target = 1, "post", "123"
    adapter.root_hint, adapter.page_root_hint = None, None
    adapter.reply_roots, adapter.events, adapter.replay_waiter = {}, [], None

    async def guard():
        pass

    async def unexpected_read(*args, **kwargs):
        pytest.fail("no browser or network read was expected")

    adapter._page_guard = guard
    adapter.page = SimpleNamespace(url="https://www.threads.com/@creator/post/" + id_to_shortcode("123"),
                                   evaluate=unexpected_read)
    await adapter._response(response(body, reply_page(root, [child], cursor=outer_cursor,
                                                    thread_pending=True)), 1)
    assert {post["id"] for post in adapter._posts()} == {"123", "124"}
    return adapter, root, child, original_variables


def install_child_pages(adapter, pages):
    requests = []

    async def evaluate(script, args):
        form = parse_qs(args["body"])
        variables = json.loads(form["variables"][0])
        requests.append(variables)
        key = (variables["postID"], variables["after"])
        assert key in pages, "unexpected child query or repeated cursor"
        await adapter._response(response(args["body"], pages[key]), adapter.generation)
        return {"status": 200, "body_read": True}

    adapter.page.evaluate = evaluate
    return requests


@pytest.mark.parametrize("child_visibility", [True, False])
def test_child_pages_preserve_lineage_query_parameters_and_visibility_coverage(child_visibility):
    async def scenario():
        adapter, root, child, original = await conversation_fixture()
        template_before = copy.deepcopy(adapter.events[0]["template"])
        first = raw_post("125", "first_reply", reply_to="responder")
        second = raw_post("126", "second_reply", reply_to="responder")
        pages = {
            ("124", None): reply_page(child, [first], cursor="child-next", partial=True),
            ("124", "child-next"): reply_page(child, [second], partial=True),
        }
        if not child_visibility:
            for page in pages.values():
                del page["data"]["media"]["text_post_app_info"]["direct_replies"]["show_unavailable_replies_disclaimer"]
        requests = install_child_pages(adapter, pages)
        comments_before = [post for post in adapter._posts() if post["id"] != "123"]
        before = comment_coverage(project_post(root), comments_before, adapter.events, 10)
        assert before["outer_pagination_terminal"] is True
        assert before["nested_threads_with_more"] == 1

        await adapter._collect_nested_replies(10)

        posts = {post["id"]: post for post in adapter._posts()}
        assert [(posts[identity]["parent_id"], posts[identity]["root_id"])
                for identity in ("124", "125", "126")] == [
                    ("123", "123"), ("124", "123"), ("124", "123")]
        assert requests == [{**original, "postID": "124", "after": cursor}
                            for cursor in (None, "child-next")]
        assert adapter.events[0]["template"] == template_before
        coverage = comment_coverage(project_post(root), [p for p in posts.values() if p["id"] != "123"],
                                    adapter.events, 10)
        assert coverage["outer_pagination_terminal"] is True
        assert coverage["nested_threads_with_more"] == 0
        assert coverage["nested_threads_queried"] == 1
        assert coverage["complete"] is child_visibility
        assert coverage["visibility_verified"] is child_visibility
        assert coverage["reasons"] == ([] if child_visibility else ["reply_visibility_unverified"])

    asyncio.run(scenario())


@pytest.mark.parametrize("parent_id, root_id, accepted", [
    ("124", "123", True),
    ("124", "124", True),
    ("124", "999", False),
    ("999", "123", False),
])
def test_child_page_accepts_only_explicit_parent_and_root_within_verified_lineage(parent_id, root_id, accepted):
    async def scenario():
        adapter, _, child, _ = await conversation_fixture(child_count=1)
        nested = raw_post("125", "nested", reply_to="responder")
        nested["text_post_app_info"].update(reply_to_post_id=parent_id, root_post_id=root_id)
        requests = install_child_pages(adapter, {
            ("124", None): reply_page(child, [nested], partial=True),
        })

        await adapter._collect_nested_replies(10)

        posts = {post["id"]: post for post in adapter._posts()}
        assert len(requests) == 1
        assert ("125" in posts) is accepted
        assert ("125" in adapter.reply_roots) is accepted
        if accepted:
            assert (posts["125"]["parent_id"], posts["125"]["root_id"]) == ("124", "123")

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["unknown_child", "wrong_author", "wrong_owner_id", "wrong_root"])
def test_child_query_rejects_unverified_conversation_members_before_fetch(change):
    async def scenario():
        adapter, _, child, _ = await conversation_fixture()
        candidate = copy.deepcopy(child)
        if change == "unknown_child":
            candidate = raw_post("999", "unknown", reply_to="creator", replies=1)
        elif change == "wrong_author":
            candidate["user"]["username"] = "someone_else"
        elif change == "wrong_owner_id":
            candidate["user"]["pk"] = "99"
            candidate["id"] = "124_99"
        else:
            for post in adapter.events[0]["posts"]:
                if post["id"] == "124":
                    post["root_id"] = "999"
        template = copy.deepcopy(adapter.events[0]["template"])
        template["variables"]["postID"] = candidate["pk"]
        template["form"]["variables"] = json.dumps(template["variables"])
        template = read_template(URL, "POST", urlencode(template["form"]), template["headers"])

        with pytest.raises(AdapterError, match="^unverified_nested_reply_root$"):
            await adapter._replay({"template": template}, None, reply_root=candidate)

    asyncio.run(scenario())


@pytest.mark.parametrize("root_id, accepted", [("123", True), ("999", False)])
def test_legacy_child_page_without_repeated_root_keeps_verified_conversation_scope(root_id, accepted):
    async def scenario():
        adapter, _, _, _ = await conversation_fixture(child_count=1)
        nested = raw_post("125", "nested", reply_to="responder")
        nested["text_post_app_info"].update(reply_to_post_id="124", root_post_id=root_id)
        page = {"edges": [{"node": {"thread_items": [{"post": nested}]}}],
                "page_info": {"has_next_page": False, "end_cursor": None},
                "show_unavailable_replies_disclaimer": False}
        install_child_pages(adapter, {("124", None): page})

        await adapter._collect_nested_replies(10)

        posts = {post["id"]: post for post in adapter._posts()}
        assert ("125" in posts) is accepted
        if accepted:
            assert (posts["125"]["parent_id"], posts["125"]["root_id"]) == ("124", "123")
            assert adapter.events[-1]["nested_reply_state"][0]["has_next_page"] is False

    asyncio.run(scenario())


def test_global_comment_cap_stops_child_pagination_after_nested_reply_fills_it():
    async def scenario():
        adapter, _, child, _ = await conversation_fixture()
        nested = raw_post("125", "nested", reply_to="responder")
        requests = install_child_pages(adapter, {
            ("124", None): reply_page(child, [nested], cursor="unread-child-page", partial=True),
        })

        await adapter._collect_nested_replies(2)

        assert len(requests) == 1
        assert [post["id"] for post in adapter._posts()] == ["123", "124", "125"]
        await adapter._collect_nested_replies(2)
        assert len(requests) == 1

    asyncio.run(scenario())


def test_child_terminal_page_cannot_terminate_original_root_pagination():
    async def scenario():
        adapter, root, child, _ = await conversation_fixture(outer_cursor="outer-next", child_count=1)
        nested = raw_post("125", "nested", reply_to="responder")
        install_child_pages(adapter, {("124", None): reply_page(child, [nested], partial=True)})

        await adapter._collect_nested_replies(10)

        event = adapter.events[-1]
        assert event["connections"] == [] and event["reply_state"] == []
        assert event["nested_reply_state"]
        comments = [post for post in adapter._posts() if post["id"] != "123"]
        coverage = comment_coverage(project_post(root), comments, adapter.events, 10)
        assert coverage["outer_pagination_terminal"] is False
        assert coverage["nested_threads_with_more"] == 0
        assert coverage["nested_threads_queried"] == 1
        assert coverage["reasons"] == ["outer_reply_pagination_unverified"]

    asyncio.run(scenario())
