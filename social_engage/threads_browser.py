"""Threads website API transport in the user's verified, existing Edge Profile 7.

Responses and observed read-query templates live only in memory. This module
never exports cookies, downloads media, creates browser contexts, or closes Edge.
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from .adapters import AdapterError, eligible_time, now_iso, numeric, username
from .threads_data import collect_posts, find_viewer, id_to_shortcode, post_page_connections, project_post, _walk


HOSTS = {"www.threads.com", "threads.com", "www.threads.net", "threads.net"}
READ_PATHS = {"/graphql/query", "/api/graphql"}
HEADER_NAMES = {"content-type", "x-fb-friendly-name", "x-ig-app-id", "x-csrftoken", "x-asbd-id", "x-fb-lsd"}
FORM_NAMES = {"doc_id", "variables", "fb_api_req_friendly_name", "fb_api_caller_class", "server_timestamps",
              "fb_dtsg", "jazoest", "lsd", "av", "dpr", "__a", "__req", "__user", "__dyn", "__csr",
              "__hs", "__hsi", "__rev", "__spin_r", "__spin_b", "__spin_t", "__s", "__ccg", "__comet_req",
              "__hsdp", "__hblp", "__sjsp", "__crn", "__jssesw"}


def read_template(url, method, body, headers):
    """Only observed Threads content queries may be replayed, never mutations."""
    try:
        parts = urlsplit(url)
        if (method != "POST" or parts.scheme != "https" or parts.hostname not in HOSTS
                or parts.path not in READ_PATHS or parts.username or parts.password
                or parts.port not in (None, 443) or parts.query or parts.fragment):
            return None
        parsed_form = parse_qs(body or "", keep_blank_values=True)
        if any(len(values) != 1 for values in parsed_form.values()) or any(k in parsed_form for k in ("query", "queries", "mutation", "operationName")):
            return None
        form = {key: values[0] for key, values in parsed_form.items() if key in FORM_NAMES}
        operation = form.get("fb_api_req_friendly_name", "")
        headers = {k.lower(): v for k, v in headers.items()}
        if headers.get("x-fb-friendly-name", operation) != operation:
            return None
        if (not re.fullmatch(r"(?:use)?Barcelona[A-Za-z0-9_]*Query", operation)
                or "Mutation" in operation or not any(k in operation for k in ("Post", "Thread", "Profile", "Search", "Feed"))
                or any(k in operation for k in ("Messages", "Mailbox", "Notification", "Promotion", "Navigation", "Badge"))):
            return None
        if not re.fullmatch(r"[0-9]+", form.get("doc_id", "")):
            return None
        variables = json.loads(form.get("variables", "{}"))
        if not isinstance(variables, dict):
            return None
        return {"url": url, "operation": operation, "form": form, "variables": variables,
                "headers": {k.lower(): v for k, v in headers.items() if k.lower() in HEADER_NAMES}}
    except (ValueError, TypeError, IndexError):
        return None


def connection_info(payload, root_hint=None):
    """Project cursors only from connections containing Threads post items."""
    result = []
    for connection in post_page_connections(payload, root_hint):
        info = connection["page_info"]
        if isinstance(info, dict) and isinstance(info.get("has_next_page"), bool):
            root = project_post(connection["root"])
            replies = collect_posts([{"thread_items": items} for items in connection["threads"]])
            cursor = info.get("end_cursor")
            result.append({"has_next_page": info["has_next_page"],
                           "cursor": cursor if isinstance(cursor, str) and len(cursor) <= 4096 else None,
                           "ids": list(dict.fromkeys([root["id"], *(p["id"] for p in replies)]))})
    for obj in _walk(payload):
        if isinstance(obj, dict):
            info = obj.get("page_info")
            if isinstance(info, dict) and isinstance(info.get("has_next_page"), bool):
                posts = collect_posts(obj)
                if posts:
                    cursor = info.get("end_cursor")
                    result.append({"has_next_page": info["has_next_page"],
                                   "cursor": cursor if isinstance(cursor, str) and len(cursor) <= 4096 else None,
                                   "ids": [p["id"] for p in posts]})
    return result


def conversation_posts(payload, target_id, root_hint=None, page_root_hint=None, *, conversation_root_id=None):
    """Bind replies only inside the site's observed post-page connection.

    The observed connection includes the root thread and reply-thread edges,
    plus an explicit unavailable-replies flag. Other feeds/quoted attachments
    cannot assign a parent. A pagination caller must separately bind its query.
    """
    output, chains = {}, []
    for connection in post_page_connections(payload, page_root_hint):
        root = project_post(connection["root"])
        if root["id"] == target_id:
            chains.extend((root, items) for items in connection["threads"])
    for node in _walk(payload):
        if not isinstance(node, dict) or "show_unavailable_replies_disclaimer" not in node:
            continue
        edges = node.get("edges")
        if not isinstance(edges, list):
            continue
        posts = collect_posts(node)
        root = next((p for p in posts if p["id"] == target_id), root_hint)
        if not root or root["id"] != target_id:
            continue
        for edge in edges:
            items = (edge.get("node") or {}).get("thread_items") if isinstance(edge, dict) else None
            chains.append((root, items))
    for root, items in chains:
        if not isinstance(items, list) or not items:
            continue
        chain = collect_posts({"thread_items": items})
        if len(chain) != len(items):
            continue  # Never promote a child when an earlier item was unavailable.
        # A containing/ancestor thread does not establish direct replies.
        if any(p["id"] == target_id for p in chain):
            continue
        previous = root
        for index, post in enumerate(chain):
            item = items[index]
            if (post.get("is_reply") is not True or post.get("reply_to_author") != previous["author"]
                    or item.get("parent_post_unavailable_reason") not in (None, "")
                    or post.get("parent_id") not in ("", previous["id"])
                    or post.get("root_id") not in ("", target_id, conversation_root_id)):
                break
            post = {**post, "parent_id": previous["id"], "root_id": target_id,
                    "parent_binding": "observed_post_connection_thread_order"}
            output[post["id"]] = post
            previous = post
    return list(output.values())


def reply_visibility(payload, root_hint=None):
    """A missing visibility flag cannot prove that every reply was exposed."""
    connections = [c for c in post_page_connections(payload, root_hint) if c["has_reply_connection"]]
    return {
        "unavailable_replies": any(isinstance(n, dict) and n.get("show_unavailable_replies_disclaimer") is True for n in _walk(payload))
            or any(c["unavailable_replies"] is True for c in connections),
        "reply_visibility_unknown": any(c["unavailable_replies"] is None for c in connections),
    }


def post_reply_state(payload, target_id, root_hint=None, page_root_hint=None, *, conversation_root_id=None):
    """Exact-root outer pagination and coverage; child cursors never drive it."""
    result = []
    for connection in post_page_connections(payload, page_root_hint):
        root = project_post(connection["root"])
        if root["id"] != target_id or not connection["has_reply_connection"]:
            continue
        info = connection["page_info"] if isinstance(connection["page_info"], dict) else {}
        nested = []
        for chain, page in zip(connection["threads"], connection["thread_page_info"]):
            try:
                identity = project_post(chain[0]["post"])["id"]
            except (AdapterError, IndexError):
                identity = None
            nested.append({"id": identity, "has_next_page": page["has_next_page"]})
        cursor = info.get("end_cursor")
        result.append({"root_id": target_id, "has_next_page": info.get("has_next_page") if type(info.get("has_next_page")) is bool else None,
                       "cursor": cursor if isinstance(cursor, str) and len(cursor) <= 4096 else None,
                       "nested_pages": nested, "unavailable_replies": connection["unavailable_replies"],
                       "root_unavailable_replies": connection["root_unavailable_replies"]})
    # A validated current-format outer connection cannot be replaced by a
    # nested/ancillary legacy-shaped disclaimer elsewhere in the same payload.
    if result:
        return result
    legacy = [n for n in _walk(payload) if isinstance(n, dict)
              and "show_unavailable_replies_disclaimer" in n and isinstance(n.get("edges"), list)]
    for node in legacy:
        chains = [e["node"]["thread_items"] for e in node["edges"]
                  if isinstance(e, dict) and isinstance(e.get("node"), dict)
                  and isinstance(e["node"].get("thread_items"), list)]
        posts = collect_posts([{"thread_items": chain} for chain in chains])
        root = next((p for p in posts if p["id"] == target_id), None)
        # A query hint alone does not identify an empty/ambiguous connection.
        # Preserve the observed legacy nonempty reply-page path only when it
        # is the sole candidate and its direct chains actually bind to the root.
        if root is None and len(legacy) == 1 and root_hint and chains:
            candidate = {"edges": [{"node": {"thread_items": chain}} for chain in chains],
                         "show_unavailable_replies_disclaimer": node["show_unavailable_replies_disclaimer"]}
            if conversation_posts(candidate, target_id, root_hint, conversation_root_id=conversation_root_id):
                root = root_hint
        if not root or root["id"] != target_id:
            continue
        info = node.get("page_info") if isinstance(node.get("page_info"), dict) else {}
        cursor, unavailable = info.get("end_cursor"), node.get("show_unavailable_replies_disclaimer")
        result.append({"root_id": target_id, "has_next_page": info.get("has_next_page") if type(info.get("has_next_page")) is bool else None,
                       "cursor": cursor if isinstance(cursor, str) and len(cursor) <= 4096 else None,
                       "nested_pages": [], "unavailable_replies": unavailable if type(unavailable) is bool else None,
                       "root_unavailable_replies": None})
    return result


def post_connections(states):
    return [{"has_next_page": s["has_next_page"], "cursor": s["cursor"], "ids": [s["root_id"]]}
            for s in states if type(s["has_next_page"]) is bool]


def comment_coverage(root, comments, events, limit):
    """Explain bounded evidence without treating a child/stale cursor as final."""
    target_id = root["id"]
    states = [s for e in events for s in e.get("reply_state", []) if s["root_id"] == target_id]
    nested = {}
    if any("reply_state" in e for e in events):
        terminal = bool(states) and states[-1]["has_next_page"] is False
        hidden = any(s["unavailable_replies"] is True or s["root_unavailable_replies"] is True for s in states)
        declared_visible = any(s["root_unavailable_replies"] is False for s in states)
        unknown = not states or (not declared_visible and any(s["unavailable_replies"] is None for s in states))
    else:
        # Compatibility for legacy in-memory event producers. Only the latest
        # connection containing the root itself can establish terminality.
        infos = [i for e in events for i in e.get("connections", []) if target_id in i["ids"]]
        terminal = bool(infos) and infos[-1]["has_next_page"] is False
        hidden = any(e.get("unavailable_replies") for e in events)
        unknown = any(e.get("reply_visibility_unknown") for e in events)
    members = {p["id"] for p in comments if p.get("root_id") == target_id}
    child_states = {}
    for event in events:
        for state in event.get("reply_state", []):
            if state["root_id"] == target_id:
                for page in state["nested_pages"]:
                    # Keep chronological updates, including a native refresh
                    # that reports more replies after a child read finished.
                    key = page["id"] if page["id"] else ("unknown", len(nested))
                    nested[key] = page["has_next_page"]
        for state in event.get("nested_reply_state", []):
            identity = state["root_id"]
            if identity not in members:
                continue
            child_states.setdefault(identity, []).append(state)
            nested[identity] = state["has_next_page"]
            for page in state["nested_pages"]:
                key = page["id"] if page["id"] else ("unknown", len(nested))
                nested[key] = page["has_next_page"]
    for observed in child_states.values():
        hidden |= any(s["unavailable_replies"] is True or s["root_unavailable_replies"] is True for s in observed)
        visible = any(s["root_unavailable_replies"] is False for s in observed)
        unknown |= not visible and any(s["unavailable_replies"] is None for s in observed)
    direct = sum(p.get("parent_id") == target_id for p in comments)
    child_counts = {}
    for post in comments:
        parent = post.get("parent_id")
        child_counts[parent] = child_counts.get(parent, 0) + 1
    child_counts_match = all(type(p.get("direct_reply_count")) is int
                             and p["direct_reply_count"] == child_counts.get(p["id"], 0) for p in comments)
    pending, unverified = sum(v is True for v in nested.values()), sum(v is None for v in nested.values())
    count = root.get("direct_reply_count")
    reasons = []
    if len(comments) > limit or (len(comments) == limit and (not terminal or pending or unverified or not child_counts_match)):
        reasons.append("comment_limit_reached")
    if not terminal: reasons.append("outer_reply_pagination_unverified")
    if pending: reasons.append("nested_reply_pages_remaining")
    if unverified: reasons.append("nested_reply_pagination_unverified")
    if type(count) is not int: reasons.append("reply_count_unavailable")
    elif count != direct: reasons.append("reply_count_mismatch")
    if not child_counts_match: reasons.append("nested_reply_count_mismatch")
    if hidden: reasons.append("unavailable_replies")
    if unknown: reasons.append("reply_visibility_unverified")
    return {"complete": not reasons, "returned_comments": min(len(comments), limit),
            "observed_comments": len(comments), "observed_direct_replies": direct,
            "observed_nested_replies": len(comments) - direct, "declared_direct_reply_count": count,
            "outer_pagination_terminal": terminal, "nested_threads_with_more": pending,
            "nested_threads_queried": len(child_states),
            "nested_threads_unverified": unverified, "visibility_verified": not unknown,
            "unavailable_replies": hidden, "reasons": reasons}


def query_targets_post(template, target_id):
    code = id_to_shortcode(target_id)
    stack = [template["variables"]]
    while stack:
        obj = stack.pop()
        if isinstance(obj, dict):
            for key, value in obj.items():
                if any(k in key.lower() for k in ("post", "media", "thread", "shortcode")) and str(value) in {target_id, code}:
                    return True
                if isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(obj, list):
            stack.extend(obj)
    return False


def parse_json_payload(raw):
    value = raw.strip()
    if value.startswith("for (;;);"):
        value = value[9:].lstrip()
    try:
        return json.loads(value)
    except ValueError:
        lines = [json.loads(line) for line in value.splitlines() if line.strip()]
        if not lines:
            raise AdapterError("invalid_browser_api_response")
        return lines


def verified_reply(receipt, comments, evidence, actor, text):
    if not isinstance(receipt, dict):
        raise AdapterError("publication_receipt_missing")
    if receipt.get("parent_id") not in (None, "", evidence["id"]):
        raise AdapterError("publication_readback_mismatch")
    matches = [p for p in comments if p["id"] == receipt.get("id") and p["author"] == actor["username"]
               and p["text"] == text and p.get("parent_id") == evidence["id"]]
    if len(matches) != 1 or receipt.get("author") != actor["username"] or receipt.get("text") != text:
        raise AdapterError("publication_readback_mismatch")
    return {"id": matches[0]["id"], "parent_id": evidence["id"], "author": actor["username"],
            "text": text, "url": matches[0]["url"], "verified_at": now_iso(),
            "authority": "threads_browser_api_readback", "verified": True}


class ThreadsBrowserAdapter:
    platform = "threads"
    max_text = 500

    def __init__(self, runtime_dir=None):
        self.runtime_dir = Path(runtime_dir).resolve() if runtime_dir else None
        self.loop = asyncio.new_event_loop()
        self.pw = self.browser = self.context = self.page = None
        self.generation = 0
        self.tasks = set()
        self.events = []
        self.actor = None
        self.closed = False
        self.blocked = None
        self.prepared = None
        self.post_target = None
        self.root_hint = None
        self.page_root_hint = None
        self.replay_waiter = None
        self.reply_roots = {}

    def _run(self, method, *args):
        if self.closed:
            raise AdapterError("browser_adapter_closed")
        if self.blocked:
            raise AdapterError(self.blocked)
        try:
            if self.page is None:
                import social_browser as sb
                runtime = self.runtime_dir or sb.DEFAULT_RUNTIME_DIR
                designation = sb.load_engage_profile7_designation(runtime)
                try:
                    state = sb.load_verified_profile7_state(runtime, designation)
                except Exception:
                    # Only the ordinary launcher owns startup/reuse, never a fallback profile.
                    with contextlib.redirect_stdout(io.StringIO()):
                        ready = sb.ensure_engage_profile7_browser(runtime)
                    designation, state = ready["designation"], ready["state"]
                self.loop.run_until_complete(self._connect(designation, state))
            return self.loop.run_until_complete(method(*args))
        except AdapterError as exc:
            if any(part in str(exc) for part in ("verification", "access_denied", "login", "account_mismatch", "viewer")):
                self.blocked = str(exc)
            raise
        except Exception:
            raise AdapterError("threads_browser_operation_failed") from None

    async def _connect(self, designation, state):
        import social_browser as sb
        from playwright.async_api import async_playwright
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.connect_over_cdp(state["cdp_url"], timeout=120000)
        self.context, _ = await sb.verified_profile_context(self.browser, designation)
        if not (await sb.platform_authentication(self.context, "threads"))["authenticated"]:
            raise AdapterError("threads_login_required")
        self.page = await self.context.new_page()
        self.page.set_default_timeout(10000)
        self.page.on("response", self._schedule_response)

    def _schedule_response(self, response):
        request = response.request
        if (self.kind == "identity"
                or read_template(response.url, request.method, request.post_data, request.headers) is None):
            return
        waiter = self._matching_replay(response, self.generation)
        priority = waiter is not None and not waiter.get("scheduled")
        if len(self.tasks) >= 40 and not priority:
            return
        if priority:
            waiter["scheduled"] = True  # At most one reserved parser slot.
        task = self.loop.create_task(self._response(response, self.generation))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def _matching_replay(self, response, generation):
        pending = self.replay_waiter
        request = response.request
        if (pending and generation == pending["generation"] == self.generation
                and response.url == pending["url"] and request.method == "POST"
                and request.post_data == pending["body"]):
            return pending
        return None

    async def _response(self, response, generation):
        waiter, consumed = None, False
        try:
            request = response.request
            waiter = self._matching_replay(response, generation)
            if waiter:
                waiter["task"] = asyncio.current_task()
            template = read_template(response.url, request.method, request.post_data, request.headers)
            if template is None or self.kind == "identity" or generation != self.generation:
                return
            if response.status in (401, 403, 429):
                self.events.append({"error": "threads_access_denied_or_rate_limited"})
                return
            if response.status != 200:
                return
            raw = await response.text()
            if len(raw) > 8000000 or generation != self.generation:
                return
            payload = parse_json_payload(raw)
            if isinstance(payload, dict) and payload.get("errors"):
                self.events.append({"error": "threads_query_unavailable"})
                return
            child_raw = waiter.get("reply_root") if waiter else None
            child = project_post(child_raw) if child_raw is not None else None
            target = child["id"] if child else self.post_target
            exact_query = target and query_targets_post(template, target)
            page_hint = (child_raw if child else self.page_root_hint) if exact_query else None
            posts = collect_posts(payload, page_hint)
            hint = (child if child else self.root_hint) if exact_query else None
            if target:
                bound = {p["id"]: p for p in conversation_posts(payload, target, hint, page_hint,
                         conversation_root_id=self.post_target if child else None)}
                posts = [bound.get(p["id"], p) for p in posts]
                if child:
                    # Only an exact response to this verified descendant query
                    # may extend the original conversation's parent graph.
                    states = post_reply_state(payload, target, hint, page_hint, conversation_root_id=self.post_target)
                    if not states:
                        return
                    existing = {p["id"]: p for p in self._posts()}
                    posts = [{**p, "root_id": self.post_target} for p in bound.values()]
                    if any(p["id"] == self.post_target
                           or existing.get(p["id"], {}).get("parent_id") not in (None, "", p["parent_id"])
                           for p in posts):
                        return
                    if len(self.events) < 100:
                        self._remember_reply_roots(payload, target, page_hint, bound)
                        self.events.append({"posts": posts, "connections": [], "reply_state": [],
                                            "nested_reply_state": states, "template": template})
                        consumed = bool(post_connections(states))
                    return
                self._remember_reply_roots(payload, target, page_hint, bound)
                self.root_hint = next((p for p in posts if p["id"] == self.post_target), self.root_hint)
                if self.page_root_hint is None:
                    self.page_root_hint = next((c["root"] for c in post_page_connections(payload)
                                                if project_post(c["root"])["id"] == self.post_target), None)
            if posts and len(self.events) < 100:
                states = post_reply_state(payload, self.post_target, self.root_hint if exact_query else None, page_hint) if self.post_target else None
                event = {"posts": posts, "connections": post_connections(states) if states is not None else connection_info(payload), "template": template,
                         **reply_visibility(payload, page_hint)}
                if states is not None:
                    event["reply_state"] = states
                self.events.append(event)
                consumed = bool(event["connections"])
        except Exception:
            # No provider exception, raw request, response or header may escape.
            return
        finally:
            if waiter and not waiter["future"].done():
                waiter["future"].set_result(consumed)

    def _remember_reply_roots(self, payload, target, page_hint, bound):
        if not hasattr(self, "reply_roots"):
            self.reply_roots = {}
        for connection in post_page_connections(payload, page_hint):
            if project_post(connection["root"])["id"] != target:
                continue
            for chain in connection["threads"]:
                for item in chain:
                    try:
                        post = project_post(item["post"])
                    except AdapterError:
                        continue
                    if post["id"] in bound:
                        self.reply_roots[post["id"]] = item["post"]

    async def _settle(self):
        await self.page.wait_for_timeout(1500)
        pending = list(self.tasks)
        if pending:
            done, unfinished = await asyncio.wait(pending, timeout=12)
            for task in unfinished:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    async def _bootstrap(self):
        result = []
        for value in await self.page.locator('script[type="application/json"]').all_text_contents():
            if len(value) > 8000000:
                continue
            try:
                result.append(json.loads(value))
            except ValueError:
                pass
        return result

    async def _page_guard(self):
        parts = urlsplit(self.page.url)
        if parts.scheme != "https" or parts.hostname not in HOSTS:
            raise AdapterError("threads_origin_or_login_redirect")
        if any(x in parts.path.lower() for x in ("/login", "/challenge", "/checkpoint", "/accounts/")):
            raise AdapterError("threads_login_or_human_verification_required")
        if await self.page.locator('iframe[src*="captcha"], iframe[src*="recaptcha"], input[autocomplete="one-time-code"]').count():
            raise AdapterError("threads_human_verification_required")

    async def _navigate(self, url, kind):
        self.generation += 1
        self.kind, self.events = kind, []
        self.root_hint = None
        self.page_root_hint = None
        self.reply_roots = {}
        from .threads_data import browser_post_id
        self.post_target = browser_post_id(url) if kind == "post" else None
        await self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await self._settle()
        await self._page_guard()
        bootstrap = await self._bootstrap()
        if kind != "identity":
            posts = collect_posts(bootstrap)
            if self.post_target:
                self.root_hint = next((p for p in posts if p["id"] == self.post_target), None)
                self.page_root_hint = next((c["root"] for c in post_page_connections(bootstrap)
                                            if project_post(c["root"])["id"] == self.post_target), None)
                bound = {p["id"]: p for p in conversation_posts(bootstrap, self.post_target)}
                posts = [bound.get(p["id"], p) for p in posts]
                self._remember_reply_roots(bootstrap, self.post_target, None, bound)
            if posts:
                states = post_reply_state(bootstrap, self.post_target) if self.post_target else None
                event = {"posts": posts, "connections": post_connections(states) if states is not None else connection_info(bootstrap),
                         "template": None, **reply_visibility(bootstrap)}
                if states is not None:
                    event["reply_state"] = states
                self.events.insert(0, event)
        return bootstrap

    async def _identity(self, expected=None):
        payload = await self._navigate("https://www.threads.com/", "identity")
        actor = find_viewer(payload)
        if expected is not None and actor["username"] != username(expected):
            raise AdapterError("account_mismatch")
        # Require the site's own profile navigation to agree with its viewer payload.
        link = self.page.locator(f'a[href="/@{actor["username"]}"]')
        if not await link.count():
            raise AdapterError("threads_viewer_navigation_mismatch")
        self.actor = actor
        return actor

    def identity(self, expected=None):
        return self._run(self._identity, expected)

    def _posts(self):
        result = {}
        for event in self.events:
            if event.get("error"):
                raise AdapterError(event["error"])
            for post in event.get("posts", []):
                old = result.get(post["id"], {})
                result[post["id"]] = {**post, **{k: old[k] for k in ("parent_id", "root_id", "parent_binding") if old.get(k) and not post.get(k)}}
        return list(result.values())

    async def _replay(self, event, cursor, *, reply_root=None):
        target = self.post_target
        if reply_root is not None:
            child = project_post(reply_root)
            known = next((p for p in self._posts() if p["id"] == child["id"]
                          and p.get("root_id") == self.post_target and p.get("parent_id")), None)
            if (not known or child["id"] == self.post_target
                    or any(child[k] != known[k] for k in ("id", "author", "url"))
                    or child["id"] not in getattr(self, "reply_roots", {})
                    or self.reply_roots[child["id"]] != reply_root):
                raise AdapterError("unverified_nested_reply_root")
            target = child["id"]
        def usable(template):
            return (template and "after" in template.get("variables", {})
                    and (not target or query_targets_post(template, target)))

        template = event.get("template")
        if not usable(template):
            template = next((e.get("template") for e in reversed(self.events)
                             if usable(e.get("template"))), None)
        if not usable(template):
            return False
        parts = urlsplit(self.page.url)
        if urlsplit(template["url"]).netloc != parts.netloc:
            return False
        variables = {**template["variables"], "after": cursor}
        form = {**template["form"], "variables": json.dumps(variables, separators=(",", ":"))}
        waiter = {"generation": self.generation, "url": template["url"], "body": urlencode(form),
                  "future": asyncio.get_running_loop().create_future(), "reply_root": reply_root}
        self.replay_waiter = waiter
        try:
            outcome = await self.page.evaluate("""async ({url,body,headers}) => {
                const controller = new AbortController();
                const timer = setTimeout(() => controller.abort(), 12000);
                let status = 0, bytes = 0;
                try {
                    const result = await fetch(url, {method:'POST', credentials:'include',
                        redirect:'error', headers, body, signal:controller.signal});
                    status = result.status;
                    if (status !== 200) { controller.abort(); return {status}; }
                    // Drain the body so the browser exposes a finished response
                    // to the parser. Discard chunks; never return raw data.
                    const reader = result.body.getReader();
                    while (true) {
                        const part = await reader.read();
                        if (part.done) break;
                        bytes += part.value.byteLength;
                        if (bytes > 8000000) {
                            controller.abort(); return {status, body_read:false};
                        }
                    }
                    return {status, body_read:true};
                } catch { return {status, body_read:false}; }
                finally { clearTimeout(timer); }
            }""", {"url": template["url"], "headers": template["headers"], "body": waiter["body"]})
            if outcome.get("status") in (401, 403, 429):
                raise AdapterError("threads_access_denied_or_rate_limited")
            if outcome.get("status") != 200:
                raise AdapterError("threads_pagination_unavailable")
            consumed = False
            if outcome.get("body_read") is not False:
                try:
                    consumed = await asyncio.wait_for(waiter["future"], timeout=12)
                except asyncio.TimeoutError:
                    pass
            await self._page_guard()
            self._posts()  # Surface an observed denial/query error before another request.
            if self.generation != waiter["generation"]:
                raise AdapterError("threads_pagination_context_changed")
            return consumed
        finally:
            self.replay_waiter = None
            if not waiter["future"].done():
                waiter["future"].cancel()
            parser = waiter.get("task")
            if parser and parser is not asyncio.current_task() and not parser.done():
                parser.cancel()
                await asyncio.gather(parser, return_exceptions=True)

    async def _paginate(self, enough):
        seen = set()
        idle_rounds = 0
        def relevant(event):
            target = getattr(self, "post_target", None)
            return [i for i in event.get("connections", []) if not target or target in i["ids"]]
        for _ in range(40):
            await self._page_guard()
            if enough(self._posts()):
                return
            latest = next((e for e in reversed(self.events) if relevant(e)), None)
            if latest:
                infos = relevant(latest)
                if all(not info["has_next_page"] for info in infos):
                    return
                cursor = next((info["cursor"] for info in infos if info["has_next_page"] and info["cursor"]), None)
                if cursor and cursor not in seen:
                    if await self._replay(latest, cursor):
                        seen.add(cursor)
                        continue
                # A bootstrap cursor without an observed replay template, or
                # an unchanged cursor, still permits bounded native scrolling.
                # It is not evidence that the search connection is exhausted.
            before = len(self._posts())
            await self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await self.page.mouse.wheel(0, 1200)
            await self._settle()
            await self._page_guard()
            if len(self._posts()) <= before:
                await self.page.mouse.wheel(0, 800)
                await self._settle()
                await self._page_guard()
                if len(self._posts()) <= before:
                    idle_rounds += 1
                    if idle_rounds >= 3:
                        return
                    continue
            idle_rounds = 0

    async def _collect_nested_replies(self, limit):
        """Read only verified descendants through the observed direct-reply query."""
        if not getattr(self, "post_target", None):
            return
        def comments():
            return [p for p in self._posts() if p["id"] != self.post_target
                    and (p.get("root_id") == self.post_target or p.get("parent_id") == self.post_target)]

        if len(comments()) >= limit:
            return
        template = next((e["template"] for e in reversed(self.events) if e.get("template")
                         and e["template"]["operation"] == "BarcelonaPostPageDirectRepliesRefetchQuery"
                         and str(e["template"]["variables"].get("postID")) == self.post_target
                         and "after" in e["template"]["variables"]), None)
        if not template:
            return
        attempted, requests = set(), 0
        while len(comments()) < limit and requests < 40:
            observed = comments()
            members = {self.post_target, *(p["id"] for p in observed)}
            pending = {p["id"] for e in self.events for s in [*e.get("reply_state", []), *e.get("nested_reply_state", [])]
                       if s["root_id"] in members for p in s["nested_pages"] if p["has_next_page"] is True}
            child = next((p for p in observed if p["id"] not in attempted and p["id"] in self.reply_roots
                          and (p["id"] in pending or (type(p.get("direct_reply_count")) is int
                               and p["direct_reply_count"] > sum(c.get("parent_id") == p["id"] for c in observed)))), None)
            if child is None:
                return
            attempted.add(child["id"])
            raw = self.reply_roots[child["id"]]
            event = {"template": {**template, "variables": {**template["variables"], "postID": child["id"]}}}
            cursor, seen = None, set()
            while len(comments()) < limit and requests < 40:
                await self._page_guard()
                requests += 1
                if not await self._replay(event, cursor, reply_root=raw):
                    return
                event = next((e for e in reversed(self.events) if any(s["root_id"] == child["id"]
                              for s in e.get("nested_reply_state", []))), None)
                if event is None:
                    return
                state = next(s for s in reversed(event["nested_reply_state"]) if s["root_id"] == child["id"])
                if state["has_next_page"] is not True or not state["cursor"] or state["cursor"] in seen:
                    break
                cursor = state["cursor"]
                seen.add(cursor)

    async def _discover(self, scope):
        if scope["source"] == "post":
            return [numeric(scope["target"])]
        if scope["source"] in {"own", "creator"}:
            owner = scope["account"] if scope["source"] == "own" else scope["target"]
            url = "https://www.threads.com/@" + username(owner)
        elif scope["source"] == "topic":
            owner = None
            url = "https://www.threads.com/search?q=" + quote(scope["target"], safe="") + "&serp_type=default"
        else:
            raise AdapterError("unsupported_source")
        bootstrap = await self._navigate(url, "discovery")
        if find_viewer(bootstrap) != self.actor or self.actor["username"] != username(scope["account"]):
            raise AdapterError("account_mismatch")
        def selected(posts):
            return [p for p in posts if (owner is None or p["author"] == username(owner)) and eligible_time(p, scope)]
        await self._paginate(lambda posts: len(selected(posts)) >= scope["candidate_limit"])
        return [p["id"] for p in selected(self._posts())[:scope["candidate_limit"]]]

    def discover(self, scope):
        return self._run(self._discover, scope)

    async def _collect(self, post_id, scope, actor):
        owner = scope.get("target_owner") or (scope["account"] if scope["source"] == "own"
                                              else scope["target"] if scope["source"] == "creator" else None)
        code = id_to_shortcode(numeric(post_id))
        post_url = ("https://www.threads.com/@" + username(owner) + "/post/" + code
                    if owner else "https://www.threads.com/t/" + code)
        bootstrap = await self._navigate(post_url, "post")
        if find_viewer(bootstrap) != actor:
            raise AdapterError("account_mismatch")
        roots = [p for p in self._posts() if p["id"] == post_id]
        if len(roots) != 1:
            raise AdapterError("exact_threads_post_not_available")
        root = roots[0]
        observed = urlsplit(self.page.url)
        code = id_to_shortcode(numeric(post_id))
        expected_paths = {urlsplit(root["url"]).path.rstrip("/"), f"/t/{code}", ""}
        if observed.path.rstrip("/") not in expected_paths:
            raise AdapterError("exact_post_page_identity_mismatch")
        owner = actor["username"] if scope["source"] == "own" else username(scope["target"]) if scope["source"] == "creator" else None
        if owner is not None and root["author"] != owner:
            raise AdapterError("creator_mismatch")
        if scope.get("target_owner") and root["author"] != scope["target_owner"]:
            raise AdapterError("exact_post_owner_mismatch")
        if not eligible_time(root, scope):
            raise AdapterError("publication_time_outside_scope")
        await self._paginate(lambda posts: len([p for p in posts if p["id"] != post_id
                                               and (p.get("parent_id") == post_id or p.get("root_id") == post_id)]) >= scope["comments"])
        await self._collect_nested_replies(scope["comments"])
        comments = [p for p in self._posts() if p["id"] != post_id and (p.get("parent_id") == post_id or p.get("root_id") == post_id)]
        coverage = comment_coverage(root, comments, self.events, scope["comments"])
        complete = coverage["complete"]
        return {**root, "platform": "threads", "authority": "threads_authenticated_browser_api", "observed_at": now_iso(),
                "comments": comments[:scope["comments"]], "comments_complete": complete,
                "comments_coverage": coverage,
                "comments_status": "available" if complete else "bounded_or_parent_link_unavailable",
                "metrics_status": "browser_declared" if root["metrics"] else "not_provided",
                "collection_method": "observed_website_api_and_embedded_json"}

    def collect(self, post_id, scope, actor):
        return self._run(self._collect, post_id, scope, actor)

    def preflight(self, evidence, scope, actor):
        if self.identity(scope["account"]) != actor:
            raise AdapterError("account_mismatch")
        current = self.collect(evidence["id"], {**scope, "comments": 1000}, actor)
        if any(current[k] != evidence[k] for k in ("id", "author", "text", "url", "media_type", "published_at")):
            raise AdapterError("target_changed_recollect_required")
        if current.get("has_viewer_replied") is True or any(p["author"] == actor["username"] for p in current["comments"]):
            raise AdapterError("account_already_replied")
        if not current["comments_complete"]:
            raise AdapterError("duplicate_check_incomplete")
        if current.get("can_reply") is not True:
            raise AdapterError("threads_reply_access_unavailable")
        self.prepared = (evidence, scope, actor)

    async def _publish(self, evidence, actor, text, guard):
        if not self.prepared or self.prepared[0]["id"] != evidence["id"] or self.prepared[2] != actor:
            raise AdapterError("browser_publication_preflight_required")
        await self._page_guard()
        viewer = find_viewer(await self._bootstrap())
        if viewer != actor:
            raise AdapterError("account_mismatch")
        path = urlsplit(evidence["url"]).path
        # Mark only the one post card with the exact timestamp permalink.
        located = await self.page.evaluate("""path => {
            document.querySelectorAll('[data-engage-target]').forEach(e=>e.removeAttribute('data-engage-target'));
            const anchors=[...document.querySelectorAll('a[href]')].filter(a=>new URL(a.href).pathname===path && a.querySelector('time'));
            const roots=[...new Set(anchors.map(a=>a.closest('[data-pressable-container]')).filter(Boolean))];
            if(roots.length!==1)return false; roots[0].setAttribute('data-engage-target','true'); return true;
        }""", path)
        if not located:
            raise AdapterError("exact_reply_control_not_identified")
        target = self.page.locator('[data-engage-target="true"]')
        reply = target.get_by_role("button", name=re.compile(r"^Reply(?:\s.*)?$", re.I))
        if await reply.count() != 1:
            raise AdapterError("exact_reply_control_not_identified")
        if await self.page.get_by_role("dialog").count():
            raise AdapterError("unexpected_existing_composer")
        await reply.click()
        dialog = self.page.get_by_role("dialog")
        if await dialog.count() != 1:
            raise AdapterError("exact_reply_composer_not_identified")
        # The observed native dialog omits its parent's permalink. Bind the
        # newly opened dialog to the exact clicked card, its author and caption.
        parent_text = " ".join(evidence["text"].split())
        dialog_text = " ".join((await dialog.inner_text()).split())
        if (urlsplit(self.page.url).path.rstrip("/") != path or not parent_text or parent_text not in dialog_text
                or not await dialog.locator(f'a[href="/@{evidence["author"]}"]').count()
                or not await dialog.get_by_text("Reply", exact=True).count()
                or not await dialog.get_by_text(actor["username"], exact=True).count()):
            raise AdapterError("reply_composer_target_unverified")
        editor = dialog.locator('[contenteditable="true"][role="textbox"]')
        if await editor.count() != 1:
            raise AdapterError("exact_reply_editor_not_identified")
        await editor.fill(text)
        if await editor.inner_text() != text:
            raise AdapterError("reply_composer_text_mismatch")
        submit = dialog.get_by_role("button", name=re.compile(r"^(Post|Reply)$"))
        if await submit.count() != 1 or not await submit.is_enabled():
            raise AdapterError("reply_submit_control_not_identified")
        receipts = []
        async def receipt_response(response):
            try:
                parts = urlsplit(response.url)
                if parts.hostname not in HOSTS or parts.path != "/api/v1/media/configure_text_only_post/" or response.status != 200:
                    return
                payload = await response.json()
                receipts.append(project_post(payload.get("media") or {}))
            except Exception:
                return
        pending = []
        callback = lambda response: pending.append(self.loop.create_task(receipt_response(response)))
        self.page.on("response", callback)
        try:
            # One click only; durable reservation and this last check precede submission.
            guard()
            await submit.click(timeout=10000)
            await self.page.wait_for_timeout(3000)
            await asyncio.gather(*pending, return_exceptions=True)
        finally:
            self.page.remove_listener("response", callback)
        if len(receipts) != 1:
            raise AdapterError("publication_receipt_missing")
        current = await self._collect(evidence["id"], {**self.prepared[1], "comments": 1000}, actor)
        return verified_reply(receipts[0], current["comments"], evidence, actor, text)

    def publish(self, evidence, actor, text, guard):
        return self._run(self._publish, evidence, actor, text, guard)

    async def _close(self):
        try:
            if self.page is not None:
                await self.page.close()
        finally:
            try:
                pending = list(self.tasks)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
            finally:
                if self.pw is not None:
                    await self.pw.stop()  # Disconnect only; do not close browser/context.

    def close(self):
        if not self.closed:
            try:
                self.loop.run_until_complete(self._close())
            finally:
                self.closed = True
                self.loop.close()
                self.events.clear()
                self.prepared = None
                self.root_hint = self.page_root_hint = self.replay_waiter = None
                self.reply_roots = {}
