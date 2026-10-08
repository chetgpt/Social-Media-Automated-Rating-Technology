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
from .threads_data import collect_posts, find_viewer, id_to_shortcode, project_post, _walk


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


def connection_info(payload):
    """Project cursors only from connections containing Threads post items."""
    result = []
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


def conversation_posts(payload, target_id, root_hint=None):
    """Bind replies only inside the site's observed post-page connection.

    The observed connection includes the root thread and reply-thread edges,
    plus an explicit unavailable-replies flag. Other feeds/quoted attachments
    cannot assign a parent. A pagination caller must separately bind its query.
    """
    output = {}
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
                item = items[index] if index < len(items) else {}
                if (post.get("is_reply") is not True or post.get("reply_to_author") != previous["author"]
                        or item.get("parent_post_unavailable_reason") not in (None, "")
                        or post.get("parent_id") not in ("", previous["id"])
                        or post.get("root_id") not in ("", target_id)):
                    break
                post = {**post, "parent_id": previous["id"], "root_id": target_id,
                        "parent_binding": "observed_post_connection_thread_order"}
                output[post["id"]] = post
                previous = post
    return list(output.values())


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
        if len(self.tasks) >= 40:
            return
        task = self.loop.create_task(self._response(response, self.generation))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _response(self, response, generation):
        try:
            request = response.request
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
            posts = collect_posts(payload)
            if self.post_target:
                hint = self.root_hint if query_targets_post(template, self.post_target) else None
                bound = {p["id"]: p for p in conversation_posts(payload, self.post_target, hint)}
                posts = [bound.get(p["id"], p) for p in posts]
                self.root_hint = next((p for p in posts if p["id"] == self.post_target), self.root_hint)
            if posts and len(self.events) < 100:
                self.events.append({"posts": posts, "connections": connection_info(payload), "template": template,
                                    "unavailable_replies": any(isinstance(n, dict) and n.get("show_unavailable_replies_disclaimer") is True for n in _walk(payload))})
        except Exception:
            # No provider exception, raw request, response or header may escape.
            return

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
                bound = {p["id"]: p for p in conversation_posts(bootstrap, self.post_target)}
                posts = [bound.get(p["id"], p) for p in posts]
            if posts:
                self.events.insert(0, {"posts": posts, "connections": connection_info(bootstrap), "template": None,
                                      "unavailable_replies": any(isinstance(n, dict) and n.get("show_unavailable_replies_disclaimer") is True for n in _walk(bootstrap))})
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

    async def _replay(self, event, cursor):
        template = event.get("template")
        if not template or "after" not in template.get("variables", {}):
            template = next((e.get("template") for e in reversed(self.events)
                             if e.get("template") and "after" in e["template"].get("variables", {})), None)
        if not template or "after" not in template["variables"]:
            return False
        parts = urlsplit(self.page.url)
        if urlsplit(template["url"]).netloc != parts.netloc:
            return False
        variables = {**template["variables"], "after": cursor}
        form = {**template["form"], "variables": json.dumps(variables, separators=(",", ":"))}
        outcome = await self.page.evaluate("""async ({url,body,headers}) => {
            const result = await fetch(url, {method:'POST', credentials:'include',
                redirect:'error', headers, body});
            return {status:result.status};
        }""", {"url": template["url"], "headers": template["headers"], "body": urlencode(form)})
        if outcome.get("status") != 200:
            raise AdapterError("threads_pagination_unavailable")
        await self._settle()
        return True

    async def _paginate(self, enough):
        seen = set()
        idle_rounds = 0
        for _ in range(40):
            await self._page_guard()
            if enough(self._posts()):
                return
            latest = next((e for e in reversed(self.events) if e.get("connections")), None)
            if latest:
                infos = latest["connections"]
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
        bootstrap = await self._navigate("https://www.threads.com/t/" + id_to_shortcode(numeric(post_id)), "post")
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
        await self._paginate(lambda posts: len([p for p in posts if p.get("parent_id") == post_id]) >= scope["comments"])
        comments = [p for p in self._posts() if p["id"] != post_id and (p.get("parent_id") == post_id or p.get("root_id") == post_id)]
        count = root.get("direct_reply_count")
        direct = [p for p in comments if p.get("parent_id") == post_id]
        terminal = any(not i["has_next_page"] for e in self.events for i in e.get("connections", [])
                       if post_id in i["ids"] or any(c["id"] in i["ids"] for c in comments))
        nested_complete = all(type(p.get("direct_reply_count")) is int and p["direct_reply_count"] == sum(c.get("parent_id") == p["id"] for c in comments) for p in comments)
        hidden = any(e.get("unavailable_replies") for e in self.events)
        complete = (not hidden and terminal and type(count) is int and count == len(direct)
                    and nested_complete and len(comments) <= scope["comments"])
        return {**root, "platform": "threads", "authority": "threads_authenticated_browser_api", "observed_at": now_iso(),
                "comments": comments[:scope["comments"]], "comments_complete": complete,
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
