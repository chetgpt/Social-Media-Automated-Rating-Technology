"""Pure, allowlisted projections of observed Threads browser JSON.

These helpers never retain a provider response, session value, or media URL.
Browser transport and pagination belong to the adapter, not this module.
"""
from __future__ import annotations

import datetime as dt
import re
from urllib.parse import urlsplit

from .adapters import AdapterError, unsupported_blocks


_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
_HOSTS = {"threads.com", "www.threads.com", "threads.net", "www.threads.net"}
_MAX_NODES = 100_000
_MAX_DEPTH = 40
_EXCLUDED_BRANCHES = {
    "post", "quoted_post", "quoted_posts", "quote_post", "attachments", "attachment",
    "image_versions2", "video_versions", "carousel_media", "CurrentUserInitialData",
}


def _numeric(value):
    if type(value) is int:
        value = str(value)
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,39}", value):
        raise AdapterError("invalid_browser_numeric_id")
    return value


def _username(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.]{1,64}", value):
        raise AdapterError("invalid_browser_username")
    return value.lower()


def id_to_shortcode(value):
    """Convert a positive browser post PK to its canonical URL-safe shortcode."""
    number = int(_numeric(value))
    code = ""
    while number:
        number, remainder = divmod(number, 64)
        code = _ALPHABET[remainder] + code
    return code


def shortcode_to_id(code):
    """Decode a canonical shortcode; reject padding and alternate encodings."""
    if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,23}", code):
        raise AdapterError("invalid_browser_shortcode")
    number = 0
    for character in code:
        number = number * 64 + _ALPHABET.index(character)
    result = _numeric(number)
    if id_to_shortcode(result) != code:
        raise AdapterError("invalid_browser_shortcode")
    return result


def _post_url(value):
    if (not isinstance(value, str) or value != value.strip()
            or any(ord(character) < 32 or ord(character) == 127 for character in value)):
        raise AdapterError("invalid_browser_post_url")
    try:
        parts = urlsplit(value)
        if (parts.scheme != "https" or parts.hostname not in _HOSTS
                or parts.username is not None or parts.password is not None
                or parts.port is not None or "\\" in value):
            raise ValueError
    except (ValueError, TypeError):
        raise AdapterError("invalid_browser_post_url") from None
    match = re.fullmatch(r"/@([A-Za-z0-9_.]{1,64})/post/([A-Za-z0-9_-]{1,23})/?", parts.path)
    if match:
        return _username(match[1]), match[2], shortcode_to_id(match[2])
    match = re.fullmatch(r"/t/([A-Za-z0-9_-]{1,23})/?", parts.path)
    if match:
        return None, match[1], shortcode_to_id(match[1])
    raise AdapterError("invalid_browser_post_url")


def browser_post_id(value):
    """Accept a numeric PK or an exact Threads post URL; discard URL queries."""
    if type(value) is int or isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,39}", value):
        return _numeric(value)
    return _post_url(value)[2]


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _bool(value):
    return value if type(value) is bool else None


def _timestamp(value):
    if type(value) is not int or value <= 0:
        raise AdapterError("invalid_browser_post_timestamp")
    try:
        return dt.datetime.fromtimestamp(value, dt.timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        raise AdapterError("invalid_browser_post_timestamp") from None


def _explicit_related_id(raw, info, kind):
    names = ("replied_to", "reply_to_post", "reply_to_id", "reply_to_post_id", "parent_id") if kind == "parent" else ("root_post", "root_post_id", "root_id")
    found = set()
    for container in (raw, info):
        for name in names:
            value = container.get(name)
            if value is None or value == "":
                continue
            if isinstance(value, dict):
                values = [value[key] for key in ("pk", "id") if value.get(key) is not None]
                if not values:
                    continue
                found.update(_numeric(item) for item in values)
            else:
                found.add(_numeric(value))
    if len(found) > 1:
        raise AdapterError("browser_reply_identity_mismatch")
    return next(iter(found), "")


def project_post(raw):
    """Project one thread-item post after validating its redundant identities."""
    if not isinstance(raw, dict):
        raise AdapterError("invalid_browser_post")
    post_id = _numeric(raw.get("pk"))
    user = raw.get("user")
    if not isinstance(user, dict):
        raise AdapterError("invalid_browser_post_author")
    owner = _username(user.get("username"))
    owner_id = _numeric(user.get("pk", user.get("id")))
    if user.get("pk") is not None and user.get("id") is not None and _numeric(user["id"]) != owner_id:
        raise AdapterError("browser_post_author_mismatch")
    raw_id = raw.get("id")
    if raw_id is not None:
        if isinstance(raw_id, str) and "_" in raw_id:
            parts = raw_id.split("_")
            if len(parts) != 2 or _numeric(parts[0]) != post_id or _numeric(parts[1]) != owner_id:
                raise AdapterError("browser_post_identity_mismatch")
        elif _numeric(raw_id) != post_id:
            raise AdapterError("browser_post_identity_mismatch")
    code = raw.get("code")
    if shortcode_to_id(code) != post_id:
        raise AdapterError("browser_post_identity_mismatch")
    # The live site returns canonical_url=null for ordinary posts. Its redundant
    # PK/shortcode and user identity still define the exact canonical permalink.
    if raw.get("canonical_url") is not None:
        url_owner, url_code, url_id = _post_url(raw["canonical_url"])
        if url_owner != owner or url_code != code or url_id != post_id:
            raise AdapterError("browser_post_permalink_mismatch")
    info = raw.get("text_post_app_info") or {}
    if not isinstance(info, dict):
        raise AdapterError("invalid_browser_post_metadata")
    if info.get("is_post_unavailable") is not None and info["is_post_unavailable"] is not False:
        raise AdapterError("browser_post_unavailable")
    caption = raw.get("caption")
    if caption is not None and not isinstance(caption, dict):
        raise AdapterError("invalid_browser_post_text")
    text = (caption or {}).get("text")
    if text is None:
        text = ""
    if not isinstance(text, str):
        raise AdapterError("invalid_browser_post_text")
    text = "".join(character for character in text if character >= " " or character == "\n")[:20000]
    replies = _count(info.get("direct_reply_count"))
    metrics = {}
    for key, value in (("likes", raw.get("like_count")), ("views", raw.get("view_count")),
                       ("replies", replies), ("quotes", info.get("quote_count")), ("reposts", info.get("repost_count"))):
        value = _count(value)
        if value is not None:
            metrics[key] = value
    reply_author = info.get("reply_to_author")
    if isinstance(reply_author, dict):
        reply_author = reply_author.get("username")
    if reply_author is not None:
        reply_author = _username(reply_author)
    unsupported = unsupported_blocks()
    for block in unsupported.values():
        block["reason"] = "not_exposed_by_this_browser_adapter"
    return {
        "id": post_id, "author": owner,
        "url": f"https://www.threads.com/@{owner}/post/{code}", "text": text,
        "url_identity_basis": "declared" if raw.get("canonical_url") else "constructed_from_api_identity",
        "published_at": _timestamp(raw.get("taken_at")),
        "media_type": {1: "IMAGE", 2: "VIDEO", 8: "CAROUSEL_ALBUM", 19: "TEXT_POST"}.get(raw.get("media_type")) if type(raw.get("media_type")) is int else None,
        "has_replies": replies > 0 if replies is not None else None,
        "parent_id": _explicit_related_id(raw, info, "parent"),
        "root_id": _explicit_related_id(raw, info, "root"),
        "metrics": metrics, "metrics_status": "available" if metrics else "not_provided",
        "direct_reply_count": replies, "has_viewer_replied": _bool(info.get("has_viewer_replied")),
        "can_reply": _bool(info.get("can_reply")), "is_reply": _bool(info.get("is_reply")),
        "reply_to_author": reply_author, **unsupported,
    }


def _walk(payload):
    """Bound traversal and avoid descending into post bodies or media branches."""
    stack, seen, count = [(payload, 0)], set(), 0
    while stack:
        value, depth = stack.pop()
        if not isinstance(value, (dict, list)):
            continue
        identity = id(value)
        if identity in seen:
            continue
        seen.add(identity)
        count += 1
        if count > _MAX_NODES or depth > _MAX_DEPTH:
            raise AdapterError("browser_payload_traversal_limit")
        yield value
        children = [child for key, child in value.items() if key not in _EXCLUDED_BRANCHES and key != "thread_items"] if isinstance(value, dict) else value
        stack.extend((child, depth + 1) for child in reversed(children))


def post_page_connections(payload, root_hint=None):
    """Bind data.media reply fragments to a validated local or prior raw root.

    Callers may supply a prior root only for an observed, exact-post reply query.
    The hint stays in memory, is revalidated, and supplies no unobserved fields
    to a fragment. A hint alone never produces a connection or metadata result.
    """
    carriers, carrier_ids, roots, validated, ambiguous = [], set(), {}, {}, set()
    for node in _walk(payload):
        data = node.get("data") if isinstance(node, dict) else None
        raw = data.get("media") if isinstance(data, dict) else None
        if not isinstance(raw, dict) or id(raw) in carrier_ids:
            continue
        carrier_ids.add(id(raw))
        carriers.append(raw)
        try:
            post = project_post(raw)
        except AdapterError:
            continue
        owner_id = _numeric(raw["user"].get("pk", raw["user"].get("id")))
        identity = post["id"]
        if identity in roots and (roots[identity][1] != post or roots[identity][2] != owner_id):
            ambiguous.add(identity)
        else:
            roots.setdefault(identity, (raw, post, owner_id))
        validated[id(raw)] = identity

    hint = None
    if root_hint is not None:
        try:
            post = project_post(root_hint)
            owner_id = _numeric(root_hint["user"].get("pk", root_hint["user"].get("id")))
            hint = (root_hint, post, owner_id)
        except AdapterError:
            pass

    count, connected = 0, set()
    for fragment in carriers:
        info = fragment.get("text_post_app_info") or {}
        if not isinstance(info, dict):
            continue
        replies = info.get("direct_replies")
        if not isinstance(replies, dict) or not isinstance(replies.get("edges"), list):
            continue
        try:
            identity = validated.get(id(fragment))
            if identity is None:
                raw_id = fragment.get("id")
                parts = raw_id.split("_") if isinstance(raw_id, str) else [raw_id]
                if len(parts) not in (1, 2):
                    continue
                identity = _numeric(parts[0])
                composite_owner = _numeric(parts[1]) if len(parts) == 2 else None
            else:
                composite_owner = None  # project_post already checked the full root.
            if identity in ambiguous:
                continue
            if identity in roots:
                root, post, owner_id = roots[identity]
                if (hint is not None and hint[1]["id"] == identity
                        and (hint[1]["author"] != post["author"] or hint[2] != owner_id)):
                    continue
            elif hint is not None and hint[1]["id"] == identity:
                root, post, owner_id = hint
            else:
                continue
            if composite_owner is not None and composite_owner != owner_id:
                continue
            if "pk" in fragment and _numeric(fragment["pk"]) != identity:
                continue
            user = fragment.get("user")
            if user is not None:
                if not isinstance(user, dict):
                    continue
                if any(_numeric(user[key]) != owner_id for key in ("pk", "id") if key in user):
                    continue
                if "username" in user and _username(user["username"]) != post["author"]:
                    continue
        except AdapterError:
            continue
        threads, thread_page_info = [], []
        for edge in replies["edges"]:
            count += 1
            if count > _MAX_NODES:
                raise AdapterError("browser_payload_traversal_limit")
            thread = edge.get("node") if isinstance(edge, dict) else None
            posts = thread.get("posts") if isinstance(thread, dict) else None
            edges = posts.get("edges") if isinstance(posts, dict) else None
            page = posts.get("page_info") if isinstance(posts, dict) else None
            page = page if isinstance(page, dict) else {}
            cursor = page.get("end_cursor")
            thread_page_info.append({"has_next_page": _bool(page.get("has_next_page")),
                                     "end_cursor": cursor if isinstance(cursor, str) and len(cursor) <= 4096 else None})
            if not isinstance(edges, list):
                threads.append([{"post": None}])
                continue
            count += len(edges)
            if count > _MAX_NODES:
                raise AdapterError("browser_payload_traversal_limit")
            # Keep every position: a missing/invalid predecessor cannot make a
            # later post appear to be a direct reply to the root.
            threads.append([{"post": item.get("node") if isinstance(item, dict) else None}
                            for item in edges])
        flags = [container.get(key) for container in (replies, info)
                 for key in ("show_unavailable_replies_disclaimer", "has_unavailable_replies")]
        observed = [flag for flag in flags if type(flag) is bool]
        connected.add(identity)
        yield {"root": root, "threads": threads, "page_info": replies.get("page_info"),
               "thread_page_info": thread_page_info, "has_reply_connection": True,
               "root_unavailable_replies": _bool(info.get("has_unavailable_replies")),
               "unavailable_replies": any(observed) if observed else None}
    for identity, (root, _, _) in roots.items():
        if identity not in connected and identity not in ambiguous:
            yield {"root": root, "threads": [], "page_info": None, "thread_page_info": [],
                   "has_reply_connection": False, "root_unavailable_replies": None, "unavailable_replies": None}


def collect_posts(payload, root_hint=None):
    """Return unique legacy thread items and observed post-page connection posts."""
    results, seen, count = [], set(), 0
    for node in _walk(payload):
        items = node.get("thread_items") if isinstance(node, dict) else None
        if not isinstance(items, list):
            continue
        for item in items:
            count += 1
            if count > _MAX_NODES:
                raise AdapterError("browser_payload_traversal_limit")
            if not isinstance(item, dict):
                continue
            try:
                post = project_post(item.get("post"))
            except AdapterError:
                continue
            if post["id"] not in seen:
                seen.add(post["id"])
                results.append(post)
    for connection in post_page_connections(payload, root_hint=root_hint):
        raw_posts = [connection["root"]]
        raw_posts.extend(item["post"] for chain in connection["threads"] for item in chain)
        for raw in raw_posts:
            count += 1
            if count > _MAX_NODES:
                raise AdapterError("browser_payload_traversal_limit")
            try:
                post = project_post(raw)
            except AdapterError:
                continue
            if post["id"] not in seen:
                seen.add(post["id"])
                results.append(post)
    return results


def find_viewer(payload):
    """Bind only an explicit viewer identity, rejecting absent or ambiguous ones."""
    candidates = set()
    for node in _walk(payload):
        viewer = node.get("viewer") if isinstance(node, dict) else None
        if not isinstance(viewer, dict):
            continue
        for user in (viewer, viewer.get("user")):
            if not isinstance(user, dict):
                continue
            try:
                account_id = _numeric(user.get("id", user.get("pk")))
                name = _username(user.get("username"))
                if user.get("pk") is not None and _numeric(user["pk"]) != account_id:
                    raise AdapterError("browser_viewer_identity_mismatch")
            except AdapterError:
                continue
            candidates.add((account_id, name))
    if len(candidates) != 1:
        raise AdapterError("browser_viewer_identity_ambiguous" if candidates else "browser_viewer_identity_unavailable")
    account_id, name = next(iter(candidates))
    return {"id": account_id, "username": name}
