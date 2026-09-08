"""Resolve exact creator handles through visible native mention controls.

Used by guarded publication and the nonpublishing composer rehearsal.
Unsupported/ambiguous markup blocks composition; plain text is never a tag.
"""
from __future__ import annotations

import asyncio
import re
import time
from urllib.parse import urlsplit

from engage_browser_guard import BrowserOperationTimeout, bounded_operation, ensure_no_challenge

NATIVE_TAG_SELECTOR = '[class*="MentionUsernameText"]'
SUGGESTION_IDENTITY_JS = """el => ({
    connected: el.isConnected,
    visible: !!el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden',
    text: el.innerText,
    hrefs: [el.getAttribute('href'), ...Array.from(el.querySelectorAll('a[href]')).map(a => a.getAttribute('href'))].filter(Boolean)
})"""
SUGGESTION_LAYOUT_JS = """el => {
    const shape = node => node ? {tag: node.tagName, role: node.getAttribute('role') || '',
        e2e: node.getAttribute('data-e2e') || '', classes: String(node.className || '').slice(0, 240)} : null;
    const rect = el.getBoundingClientRect();
    const x = Math.max(0, Math.min(innerWidth - 1, rect.x + rect.width / 2));
    const y = Math.max(0, Math.min(innerHeight - 1, rect.y + rect.height / 2));
    return {selected: shape(el), parent: shape(el.parentElement),
        children: Array.from(el.children).slice(0, 6).map(shape),
        center_hit: shape(document.elementFromPoint(x, y)),
        rect: {x: rect.x, y: rect.y, width: rect.width, height: rect.height}};
}"""
EDITOR_ENTITIES_SNAPSHOT_JS = """el => ({
    editor: el,
    native: Array.from(el.querySelectorAll('[class*="MentionUsernameText"]')).map(node => ({node, text: node.textContent})),
    links: Array.from(el.querySelectorAll('a[href]')).map(node => ({node, text: node.textContent, href: node.getAttribute('href')}))
})"""
EDITOR_INSERTION_STATE_JS = """before => {
    const native = Array.from(before.editor.querySelectorAll('[class*="MentionUsernameText"]'));
    const links = Array.from(before.editor.querySelectorAll('a[href]'));
    return {
        connected: before.editor.isConnected,
        existing_native_preserved: before.native.every((item, i) => native[i] === item.node && item.node.isConnected && item.node.textContent === item.text),
        existing_links_preserved: before.links.every((item, i) => links[i] === item.node && item.node.isConnected && item.node.textContent === item.text && item.node.getAttribute('href') === item.href),
        native_added_count: native.length - before.native.length,
        link_added_count: links.length - before.links.length,
        before_native_count: before.native.length,
        after_native_count: native.length,
        before_link_hrefs: before.links.map(item => item.href),
        after_link_hrefs: links.map(node => node.getAttribute('href')),
        added_link_hrefs: links.slice(before.links.length).map(node => node.getAttribute('href'))
    };
}"""


async def traced_operation(operation, awaitable, *, trace=None, handle="", **details):
    """Emit only bounded operation metadata; never transport exception text."""
    timeout = float(details.get("timeout_ms", 10000)) / 1000
    if trace is None:
        return await bounded_operation(awaitable, timeout=timeout, phase=operation)
    record = {"operation": operation, **details}
    if handle:
        record["creator_handle"] = handle
    started = time.monotonic()
    trace("started", record)
    try:
        result = await bounded_operation(awaitable, timeout=timeout, phase=operation)
    except Exception as exc:
        trace("failed", {**record, "seconds": round(time.monotonic() - started, 3),
                         "error_type": type(exc).__name__})
        raise
    trace("passed", {**record, "seconds": round(time.monotonic() - started, 3)})
    return result


def profile_handle(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"", "https"}:
        return ""
    if parsed.netloc and parsed.hostname not in {"www.tiktok.com", "tiktok.com"}:
        return ""
    match = re.fullmatch(r"/@([A-Za-z0-9_][A-Za-z0-9_.]{0,23})/?", parsed.path)
    return match.group(1).casefold() if match else ""


async def editor_state(editor) -> dict:
    # Only visible editor text and native profile links are read. No React
    # internals, credentials, API requests, or hidden account data are used.
    return await bounded_operation(editor.evaluate("""el => ({
        text: ('value' in el ? el.value : (el.querySelector('[data-block="true"]') ? Array.from(el.querySelectorAll('[data-block="true"]')).map(n => n.textContent).join('\\n') : el.innerText)).replace(/\\r\\n/g, '\\n'),
        native_labels: Array.from(el.querySelectorAll('[class*="MentionUsernameText"]')).map(n => n.textContent),
        links: Array.from(el.querySelectorAll('a[href]')).map(a => ({
            href: a.getAttribute('href'), text: a.innerText
        }))
    })"""), timeout=3, phase="editor_state")


async def focus_editor(editor, *, page=None) -> None:
    """Do not send page keyboard input while an overlay holds focus."""
    if page is not None:
        await ensure_no_challenge(page, "editor_focus")
    await bounded_operation(editor.focus(), timeout=5, phase="editor_focus")
    if not await bounded_operation(editor.evaluate(
        "el => el === document.activeElement || el.contains(document.activeElement)"
    ), timeout=3, phase="editor_focus_read"):
        try:
            await bounded_operation(editor.click(), timeout=5, phase="editor_focus_click")
        except BrowserOperationTimeout:
            raise
        except Exception:
            if page is not None:
                await ensure_no_challenge(page, "editor_focus_click")
    if await bounded_operation(editor.evaluate(
        "el => el === document.activeElement || el.contains(document.activeElement)"
    ), timeout=3, phase="editor_focus_read") is not True:
        if page is not None:
            await ensure_no_challenge(page, "editor_focus")
        raise RuntimeError("TikTok comment editor did not receive focus")


async def verify_editor(editor, final_text: str, handles: list[str], *, labels: list[str] | None = None, bindings: list | None = None) -> None:
    current = await editor_state(editor)
    if current.get("text") != final_text:
        raise RuntimeError("TikTok comment editor differs from the exact approved response")
    native = current.get("native_labels") or []
    if native:
        expected_labels = labels or ["@" + handle for handle in handles]
        if native != expected_labels or not bindings or len(bindings) != len(handles):
            raise RuntimeError("Native mention identities require exact selected-account bindings")
        for index, (handle, label, node) in enumerate(bindings):
            if handle != handles[index] or label != expected_labels[index]:
                raise RuntimeError("Native mention identity differs from the approved creator")
            same = await bounded_operation(editor.evaluate("""(el, args) => {
                const nodes = Array.from(el.querySelectorAll('[class*="MentionUsernameText"]'));
                return nodes[args.index] === args.node && args.node.isConnected;
            }""", {"index": index, "node": node}), timeout=3, phase="mention_identity_verification")
            if not same:
                raise RuntimeError("Selected native mention was replaced or removed")
        return
    resolved = [profile_handle(link.get("href") or "") for link in current.get("links", [])]
    resolved = [handle for handle in resolved if handle]
    if resolved != handles:
        raise RuntimeError("TikTok did not resolve the exact approved creator mentions")


def _suggestion_matches(identity: dict, handle: str) -> bool:
    if not identity.get("connected") or not identity.get("visible"):
        return False
    hrefs = {profile_handle(url) for url in identity.get("hrefs", [])} - {""}
    lines = [line.strip().lstrip("@").casefold()
             for line in (identity.get("text") or "").splitlines() if line.strip()]
    # Username is the last visible line; the first may be a lookalike display
    # name. Native display labels alone never select an account.
    return hrefs == {handle} or (not hrefs and bool(lines) and lines[-1] == handle)


async def _wait_for_mention_insertion(snapshot, handle: str, *, trace=None, timeout_ms=5000) -> None:
    """Require a new editor entity after the exact visible account was clicked."""
    async def observe():
        while True:
            state = await snapshot.evaluate(EDITOR_INSERTION_STATE_JS)
            native_added = state["native_added_count"]
            links_added = state["link_added_count"]
            if trace is not None:
                trace("observed", {"operation": "mention_insertion_state", "creator_handle": handle,
                                   "native_added_count": native_added, "link_added_count": links_added,
                                   "before_native_count": state["before_native_count"],
                                   "after_native_count": state["after_native_count"],
                                   "before_exact_link_count": sum(profile_handle(href or "") == handle for href in state["before_link_hrefs"]),
                                   "after_exact_link_count": sum(profile_handle(href or "") == handle for href in state["after_link_hrefs"])})
            if not state["connected"] or not state["existing_native_preserved"] or not state["existing_links_preserved"]:
                raise RuntimeError("Existing editor mention identities changed during selection")
            if native_added not in (0, 1) or links_added not in (0, 1):
                raise RuntimeError("Mention selection inserted an unexpected number of entities")
            if links_added and [profile_handle(href or "") for href in state["added_link_hrefs"]] != [handle]:
                raise RuntimeError("Mention selection inserted a link to a different creator")
            if native_added == 1 or links_added == 1:
                return
            await asyncio.sleep(0.1)
    # Keep the existing settlement deadline; list visibility is not evidence of
    # editor insertion, and increasing the deadline would not repair that gate.
    await bounded_operation(observe(), timeout=timeout_ms / 1000, phase="mention_insertion_settlement")


async def _choose_exact_suggestion(page, handle: str, *, editor=None, trace=None) -> None:
    try:
        return await bounded_operation(_choose_exact_suggestion_impl(page, handle, editor=editor, trace=trace),
                                       timeout=20, phase="mention_suggestion_selection")
    except Exception:
        # A challenge may have appeared while the click or insertion was in
        # flight. Detect it before reporting a generic suggestion failure.
        await ensure_no_challenge(page, "mention_suggestion_selection")
        raise


async def _choose_exact_suggestion_impl(page, handle: str, *, editor=None, trace=None) -> None:
    # Require a visible mention suggestion, never an ordinary profile link
    # elsewhere on the post. Unsupported TikTok layouts fail closed.
    for attempt in range(1, 31):
        await ensure_no_challenge(page, "mention_suggestion_lookup")
        # Retain the actual observed elements, not live nth() locators that
        # could resolve to another account after asynchronous result reordering.
        snapshot = await traced_operation("suggestion_snapshot", page.evaluate_handle(
            "selector => Array.from(document.querySelectorAll(selector)).slice(0, 50)",
            '[role="listbox"] [role="option"], [data-e2e="comment-at-list"]',
        ), trace=trace, handle=handle, attempt=attempt)
        snapshot_error = None
        try:
            identities = await traced_operation("suggestion_identity_read", snapshot.evaluate(
                "elements => elements.map((el, index) => ({index, ...(" + SUGGESTION_IDENTITY_JS + ")(el)}))"
            ), trace=trace, handle=handle, attempt=attempt)
            eligible = [item["index"] for item in identities if _suggestion_matches(item, handle)]
            if trace is not None:
                trace("observed", {"operation": "suggestion_candidates", "creator_handle": handle,
                                   "attempt": attempt, "candidate_count": len(identities),
                                   "visible_candidate_count": sum(bool(item.get("connected") and item.get("visible")) for item in identities),
                                   "exact_candidate_count": len(eligible)})
            if len(eligible) > 1:
                raise RuntimeError("TikTok creator mention suggestions are ambiguous")
            if eligible:
                selected = await traced_operation("suggestion_node_binding", snapshot.evaluate_handle(
                    "(elements, index) => elements[index]", eligible[0]), trace=trace, handle=handle)
                selection_error = None
                editor_snapshot = None
                try:
                    option = selected.as_element()
                    if editor is not None:
                        editor_snapshot = await traced_operation("editor_entity_snapshot", editor.evaluate_handle(
                            EDITOR_ENTITIES_SNAPSHOT_JS), trace=trace, handle=handle)
                    if option is None or not _suggestion_matches(await traced_operation(
                        "suggestion_identity_recheck", option.evaluate(SUGGESTION_IDENTITY_JS),
                        trace=trace, handle=handle), handle):
                        raise RuntimeError("TikTok creator mention suggestion changed before selection")
                    if trace is not None:
                        trace("observed", {"operation": "suggestion_layout", "creator_handle": handle,
                                           "layout": await bounded_operation(option.evaluate(SUGGESTION_LAYOUT_JS), timeout=3, phase="suggestion_layout")})
                    await ensure_no_challenge(page, "mention_suggestion_click")
                    await traced_operation("suggestion_click", option.click(timeout=5000),
                                           trace=trace, handle=handle, timeout_ms=5000)
                    if editor_snapshot is not None:
                        await traced_operation("mention_insertion_settlement",
                                               _wait_for_mention_insertion(editor_snapshot, handle, trace=trace),
                                               trace=trace, handle=handle, timeout_ms=5000)
                    else:
                        # Compatibility for direct suggestion-only helpers.
                        # Every production composer/probe supplies its editor.
                        await traced_operation("suggestion_selection_settlement",
                                               option.wait_for_element_state("hidden", timeout=5000),
                                               trace=trace, handle=handle, timeout_ms=5000)
                    return
                except Exception as exc:
                    selection_error = exc
                    raise
                finally:
                    cleanup_error = None
                    for resource, operation in ((editor_snapshot, "editor_entity_snapshot_dispose"), (selected, "suggestion_node_dispose")):
                        if resource is not None:
                            try:
                                await traced_operation(operation, resource.dispose(), trace=trace, handle=handle, timeout_ms=500)
                            except Exception as exc:
                                if cleanup_error is None:
                                    cleanup_error = exc
                    if selection_error is None and cleanup_error is not None:
                        raise cleanup_error
        except Exception as exc:
            snapshot_error = exc
            raise
        finally:
            try:
                await traced_operation("suggestion_snapshot_dispose", snapshot.dispose(), trace=trace, handle=handle, timeout_ms=500)
            except Exception:
                if snapshot_error is None:
                    raise
        await asyncio.sleep(0.1)
    raise RuntimeError(f"TikTok could not resolve the approved mention @{handle}; nothing was submitted")


async def type_editor_text(page, value: str) -> None:
    """Enter/Shift+Enter may submit on TikTok; never use either to compose."""
    if "\n" in value or "\r" in value:
        raise RuntimeError("Native mention comments must use one paragraph; Enter-based composition can submit prematurely")
    if value:
        await ensure_no_challenge(page, "comment_text_typing")
        await bounded_operation(page.keyboard.type(value, delay=10), timeout=10, phase="comment_text_typing")


async def compose_mentions(page, editor, final_text: str, handles: list[str], *, labels: list[str] | None = None, trace=None) -> list:
    """Resolve exact native tags and retain DOM identity bindings until submit.

    Display labels must already be in the reviewed text. A site-rendered label
    change blocks submission; it is never silently substituted after approval.
    """
    labels = labels or ["@" + handle for handle in handles]
    if "\n" in final_text or "\r" in final_text:
        raise RuntimeError("Native mention comments must use one paragraph; re-draft and review this legacy multiline response")
    if len(labels) != len(handles) or len(set(handles)) != len(handles):
        raise RuntimeError("Invalid approved mention identities")
    await traced_operation("composer_bring_to_front", page.bring_to_front(), trace=trace)
    await traced_operation("composer_scroll_into_view", editor.scroll_into_view_if_needed(), trace=trace)
    await traced_operation("composer_focus", focus_editor(editor, page=page), trace=trace)
    await traced_operation("composer_select_all", page.keyboard.press("ControlOrMeta+A"), trace=trace)
    await traced_operation("composer_backspace", page.keyboard.press("Backspace"), trace=trace)
    if (await traced_operation("composer_read_after_clear", editor_state(editor), trace=trace)).get("text", "").strip():
        raise RuntimeError("TikTok comment editor could not be cleared safely")
    offset, bindings = 0, []
    for index, (handle, label) in enumerate(zip(handles, labels)):
        start = final_text.find(label, offset)
        if start < 0:
            raise RuntimeError("Approved creator mention is missing from the comment")
        await traced_operation("comment_prefix_typing", type_editor_text(page, final_text[offset:start]), trace=trace, handle=handle)
        await traced_operation("mention_handle_typing", page.keyboard.type("@" + handle, delay=30), trace=trace, handle=handle)
        await _choose_exact_suggestion(page, handle, editor=editor, trace=trace)
        expected_prefix = final_text[:start + len(label)]
        current = await traced_operation("native_entity_read", editor_state(editor), trace=trace, handle=handle)
        if current.get("native_labels"):
            native_labels = current["native_labels"]
            if native_labels != labels[:index+1]:
                raise RuntimeError("TikTok native display label differs from reviewed text; present the actual label before approval")
            node = await traced_operation("native_entity_binding", editor.locator(NATIVE_TAG_SELECTOR).nth(index).element_handle(), trace=trace, handle=handle)
            bindings.append((handle, label, node))
        if current.get("text") == expected_prefix + " ":
            await traced_operation("native_trailing_space_removal", page.keyboard.press("Backspace"), trace=trace, handle=handle)
        elif current.get("text") != expected_prefix:
            raise RuntimeError("TikTok mention selection changed the approved text")
        offset = start + len(label)
    await traced_operation("comment_suffix_typing", type_editor_text(page, final_text[offset:]), trace=trace)
    await traced_operation("composed_comment_verification", verify_editor(editor, final_text, handles, labels=labels, bindings=bindings), trace=trace)
    return bindings
