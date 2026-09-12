"""Observe native creator labels and rehearse composition without publication.

Uses one temporary tab in the required Profile 7 session. All non-read HTTP
methods and media requests from that tab are blocked; nothing is submitted.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import time
import sys
from urllib.parse import urlsplit

from engage_creator_mentions_ui import traced_operation, focus_editor
from engage_browser_guard import HumanVerificationRequired, BrowserOperationTimeout, bounded_operation, ensure_no_challenge


HANDLE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.]{0,23}\Z", re.ASCII)
POST_PATH = re.compile(r"/@([A-Za-z0-9_][A-Za-z0-9_.]{0,23})/(video|photo)/[0-9]+\Z", re.ASCII)
READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
MEDIA_SUFFIXES = (".mp4", ".m4a", ".mp3", ".webm", ".m3u8", ".mpd", ".aac", ".wav")


class ProbeDiagnostics:
    """Bounded safe operation timings, retaining the first failure over cleanup."""
    MAX_EVENTS = 256

    def __init__(self, report):
        self.report = report
        self.phase = None
        self.data = report.setdefault("diagnostics", {"events": [], "dropped_events": 0})

    def __call__(self, event, record):
        # The callback accepts operation metadata only. No page text, URLs,
        # request data, or exception messages enter the diagnostic report.
        safe = {"event": event, "phase": self.phase or self.report.get("phase")}
        for key in ("operation", "creator_handle", "error_type"):
            value = record.get(key)
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.]{1,64}", value):
                safe[key] = value
        for key in ("seconds", "attempt", "timeout_ms", "candidate_count", "visible_candidate_count", "exact_candidate_count", "native_added_count", "link_added_count", "before_native_count", "after_native_count", "before_exact_link_count", "after_exact_link_count"):
            value = record.get(key)
            if type(value) in (int, float) and 0 <= value < 1_000_000:
                safe[key] = value
        layout = record.get("layout")
        if isinstance(layout, dict):
            def shape(value):
                if not isinstance(value, dict):
                    return {}
                return {key: item for key in ("tag", "role", "e2e", "classes")
                        if isinstance(item := value.get(key), str)
                        and re.fullmatch(r"[A-Za-z0-9_ .-]{0,240}", item)}
            safe["layout"] = {key: shape(layout.get(key)) for key in ("selected", "parent", "center_hit")}
            children = layout.get("children")
            safe["layout"]["children"] = [shape(item) for item in children[:6]] if isinstance(children, list) else []
            rect = layout.get("rect")
            safe["layout"]["rect"] = {key: value for key in ("x", "y", "width", "height")
                                      if isinstance(rect, dict) and type(value := rect.get(key)) in (int, float)
                                      and -100_000 < value < 100_000}
        self.data["current_operation"] = safe
        if event == "failed":
            self._keep_failure(safe)
        if event != "started":
            if len(self.data["events"]) >= self.MAX_EVENTS:
                self.data["events"].pop(0)
                self.data["dropped_events"] += 1
            self.data["events"].append(safe)

    def _keep_failure(self, failure):
        if "failure" not in self.data:
            operation = failure.get("operation", "unclassified_operation")
            suffix = "timeout" if failure.get("error_type") in {"TimeoutError", "BrowserOperationTimeout"} else "failed"
            self.data["failure"] = {**failure, "code": f"{operation}_{suffix}"}

    def fail(self, exc):
        current = self.data.get("current_operation", {})
        phase = self.phase or self.report.get("phase")
        # A validation error between operations did not make the preceding
        # successful browser call fail. Avoid falsely blaming that call (or a
        # successful disposal) for an untraced error.
        if current.get("event") not in {"started", "failed"} or current.get("phase") != phase:
            current = {"operation": "unclassified_operation"}
        self("failed", {**current, "error_type": type(exc).__name__})


def canonical_post_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme == "https"
            and parsed.netloc == "www.tiktok.com"
            and POST_PATH.fullmatch(parsed.path)
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        raise argparse.ArgumentTypeError("requires an exact query-free https://www.tiktok.com/@handle/video-or-photo/numeric-id URL")
    return value


def creator_handle(value: str) -> str:
    handle = value.removeprefix("@").casefold()
    if not HANDLE.fullmatch(handle):
        raise argparse.ArgumentTypeError("requires a TikTok creator handle, with optional leading @")
    return handle


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, type=canonical_post_url)
    parser.add_argument("--handle", required=True, action="append", type=creator_handle,
                        help="Exact account to resolve; repeat for a second creator.")
    parser.add_argument("--expected-account", required=True, type=creator_handle)
    parser.add_argument("--report", required=True, type=Path,
                        help="Local JSON report path; includes observed public labels and timings.")
    parser.add_argument("--failure-screenshot", type=Path,
                        help="Optional screenshot of this temporary target tab before failure cleanup.")
    args = parser.parse_args(argv)
    if not 1 <= len(args.handle) <= 2 or len(set(args.handle)) != len(args.handle):
        parser.error("provide one or two distinct --handle values")
    return args


def request_fence(report: dict, is_publish_url):
    """Return a page-local deny-write handler; retain counts, never requests."""
    async def fence(route) -> None:
        request = route.request
        publish_request = is_publish_url(request.url)
        if request.method.upper() not in READ_METHODS or publish_request:
            report["blocked_non_read_requests"] += 1
            if publish_request:
                report["blocked_publish_requests"] += 1
            await bounded_operation(route.abort(), timeout=3, phase="probe_request_abort")
        elif request.resource_type == "media" or urlsplit(request.url).path.casefold().endswith(MEDIA_SUFFIXES):
            report["blocked_media_requests"] += 1
            await bounded_operation(route.abort(), timeout=3, phase="probe_media_abort")
        else:
            await bounded_operation(route.continue_(), timeout=3, phase="probe_request_continue")
    return fence


def composition_cases(labels: list[str]) -> list[str]:
    short = " ".join(f"{label}: Unpublished creator mention verification." for label in labels)
    full = "AI-assisted perspective: This is an unpublished composer test. " + " ".join(
        f"{label}: Testing the exact creator connection wording." for label in labels
    )
    return [short, full]


async def release_probe_fences(page, context, fence, report, *, route_installed, bypass_set):
    """Remove only this probe's controls; attempt both bounded cleanup steps."""
    failed = False
    try:
        if route_installed:
            await bounded_operation(page.unroute("**/*", fence), timeout=3, phase="probe_route_remove")
        report["probe_route_removed"] = True
    except Exception:
        failed = True
        report["probe_route_removed"] = False
    try:
        if bypass_set:
            cdp = await bounded_operation(context.new_cdp_session(page), timeout=3, phase="probe_cdp_cleanup_create")
            try:
                await bounded_operation(cdp.send("Network.setBypassServiceWorker", {"bypass": False}), timeout=3, phase="probe_service_worker_restore")
            finally:
                await bounded_operation(cdp.detach(), timeout=1, phase="probe_cdp_cleanup_detach")
        report["probe_service_worker_restored"] = True
    except Exception:
        failed = True
        report["probe_service_worker_restored"] = False
    report["human_verification_tab_ready"] = not failed
    if failed:
        raise RuntimeError("preserved_tab_cleanup_failed")


async def clear_editor(page, editor, ui, *, trace=None, handle="") -> None:
    await ensure_no_challenge(page, "editor_cleanup")
    await traced_operation("editor_bring_to_front", page.bring_to_front(), trace=trace, handle=handle)
    await traced_operation("editor_focus", focus_editor(editor, page=page), trace=trace, handle=handle)
    await traced_operation("editor_select_all", page.keyboard.press("ControlOrMeta+A"), trace=trace, handle=handle)
    await traced_operation("editor_backspace", page.keyboard.press("Backspace"), trace=trace, handle=handle)
    state = await traced_operation("editor_read_after_clear", ui.editor_state(editor), trace=trace, handle=handle)
    if state.get("text", "").strip() or state.get("native_labels") or state.get("links"):
        raise RuntimeError("composer_cleanup_failed")


async def run_probe(args: argparse.Namespace, report: dict) -> None:
    # Keep all browser and workflow imports after argument parsing: --help and
    # invalid scopes are strictly offline and need no browser dependencies.
    from engage_tiktok import SocialBrowserPreflight, DEFAULT_BROWSER_STATE, active_tiktok_account
    from social_browser import load_engage_profile7_designation, load_verified_profile7_state, verified_profile_context
    from playwright.async_api import async_playwright
    from tiktok_publication_adapter import INPUT_SELECTORS, open_comments_panel, wait_for_first_visible, is_tiktok_comment_publish_url
    from engage_creator_matching import _mention_label
    import engage_creator_mentions_ui as ui

    trace = ProbeDiagnostics(report)
    report["phase"] = "profile7_preflight"
    preflight_started = time.monotonic()
    preflight = await bounded_operation(SocialBrowserPreflight(expected_account=args.expected_account, challenge_detection=True).ensure_ready(),
                                       timeout=300, phase="profile7_preflight")
    report["preflight_seconds"] = round(time.monotonic() - preflight_started, 3)
    if preflight.get("observed_account") != args.expected_account:
        raise RuntimeError("preflight_account_mismatch")
    report["observed_account"] = args.expected_account
    report["phase"] = "verified_profile_connection"
    designation = load_engage_profile7_designation(DEFAULT_BROWSER_STATE.parent)
    state = load_verified_profile7_state(DEFAULT_BROWSER_STATE.parent, designation)
    manager = async_playwright()
    playwright = await bounded_operation(manager.__aenter__(), timeout=10, phase="playwright_start")
    try:
        browser = await bounded_operation(playwright.chromium.connect_over_cdp(state["cdp_url"], timeout=30000), timeout=31, phase="profile_connection")
        context, _ = await bounded_operation(verified_profile_context(browser, designation), timeout=15, phase="profile_context")
        page = await bounded_operation(context.new_page(), timeout=10, phase="probe_tab_create")
        editor = None
        primary_error = None
        fence = request_fence(report, is_tiktok_comment_publish_url)
        route_installed = False
        bypass_set = False
        try:
            await bounded_operation(page.route("**/*", fence), timeout=5, phase="probe_route_install")
            route_installed = True
            # Keep this target's requests within the page route even when the
            # shared profile has an existing service worker for TikTok.
            cdp = await bounded_operation(context.new_cdp_session(page), timeout=5, phase="probe_cdp_create")
            try:
                await bounded_operation(cdp.send("Network.setBypassServiceWorker", {"bypass": True}), timeout=5, phase="probe_service_worker_fence")
                bypass_set = True
            finally:
                await bounded_operation(cdp.detach(), timeout=1, phase="probe_cdp_detach")
            report["phase"] = "target_account_verification"
            await bounded_operation(page.goto(args.url, wait_until="domcontentloaded", timeout=60000), timeout=61, phase="target_navigation")
            await ensure_no_challenge(page, "target_navigation")
            await bounded_operation(page.bring_to_front(), timeout=5, phase="target_bring_to_front")
            if await bounded_operation(active_tiktok_account(page), timeout=15, phase="target_account_verification") != args.expected_account:
                raise RuntimeError("target_account_mismatch_or_unresolved")
            await bounded_operation(open_comments_panel(page), timeout=15, phase="comments_panel_open")
            await ensure_no_challenge(page, "comments_panel_open")
            editor, _ = await bounded_operation(wait_for_first_visible(page, INPUT_SELECTORS, timeout_ms=20000), timeout=21, phase="editor_lookup")
            if editor is None:
                await ensure_no_challenge(page, "editor_lookup")
                raise RuntimeError("composer_unavailable")
            await bounded_operation(editor.scroll_into_view_if_needed(), timeout=5, phase="editor_scroll")
            report["phase"] = "exact_creator_label_observation"
            labels = []
            for handle in args.handle:
                await clear_editor(page, editor, ui, trace=trace, handle=handle)
                await traced_operation("mention_handle_typing", page.keyboard.type("@" + handle, delay=30), trace=trace, handle=handle)
                await ui._choose_exact_suggestion(page, handle, editor=editor, trace=trace)
                current = await traced_operation("native_entity_read", ui.editor_state(editor), trace=trace, handle=handle)
                native = current.get("native_labels") or []
                if len(native) != 1:
                    raise RuntimeError("expected_one_native_creator_entity")
                label = _mention_label(native[0])
                if current.get("text") not in {label, label + " "}:
                    raise RuntimeError("native_label_observation_text_mismatch")
                labels.append(label)
                report["observed_mentions"].append({"creator_handle": handle, "mention_label": label})
            report["phase"] = "production_composition_rehearsal"
            for index, final_text in enumerate(composition_cases(labels), start=1):
                started = time.monotonic()
                await bounded_operation(page.bring_to_front(), timeout=5, phase="rehearsal_bring_to_front")
                bindings = await ui.compose_mentions(page, editor, final_text, args.handle, labels=labels, trace=trace)
                await traced_operation("rehearsal_final_verification", ui.verify_editor(editor, final_text, args.handle, labels=labels, bindings=bindings), trace=trace)
                report["cases"].append({"case": index, "status": "passed", "text": final_text,
                                        "seconds": round(time.monotonic() - started, 3)})
        except Exception as exc:
            if not isinstance(exc, HumanVerificationRequired):
                try:
                    await ensure_no_challenge(page, report["phase"])
                except HumanVerificationRequired as challenge:
                    exc = challenge
                except Exception:
                    pass  # An unreadable page is not positive challenge evidence.
            primary_error = exc
            if isinstance(exc, HumanVerificationRequired):
                report["blocker"] = {**exc.as_dict(), "submit_intent_recorded": False}
            trace.fail(exc)
            # Read only the visible composer, never page internals or transport.
            # Counts/booleans help distinguish missing input from missing suggestions.
            if editor is not None:
                try:
                    current = await bounded_operation(ui.editor_state(editor), timeout=3, phase="failed_editor_read")
                    focused = await bounded_operation(editor.evaluate(
                        "el => el === document.activeElement || el.contains(document.activeElement)"), timeout=3, phase="failed_editor_focus_read")
                    report["failed_editor"] = {
                        "focused": focused is True,
                        "text_length": len(current.get("text") or ""),
                        "contains_requested_handle": any("@" + handle in (current.get("text") or "") for handle in args.handle),
                        "native_entity_count": len(current.get("native_labels") or []),
                        "link_count": len(current.get("links") or []),
                    }
                except Exception:
                    report["failed_editor"] = {"unavailable": True}
            screenshot_path = getattr(args, "failure_screenshot", None)
            if screenshot_path is None and isinstance(exc, HumanVerificationRequired):
                screenshot_path = args.report.with_suffix(".challenge.png")
            if screenshot_path is not None:
                try:
                    screenshot_path.parent.mkdir(parents=True, exist_ok=True)
                    await bounded_operation(page.screenshot(path=str(screenshot_path), timeout=5000), timeout=5.5, phase="failure_screenshot")
                    report["failure_screenshot_saved"] = True
                except Exception:
                    report["failure_screenshot_saved"] = False
            raise exc
        finally:
            trace.phase = "cleanup"
            try:
                if editor is not None and not isinstance(primary_error, (HumanVerificationRequired, BrowserOperationTimeout)):
                    await clear_editor(page, editor, ui, trace=trace)
                    report["editor_cleared"] = True
            except Exception as exc:
                trace.fail(exc)
                if isinstance(exc, HumanVerificationRequired):
                    primary_error = exc
                    report["blocker"] = {**exc.as_dict(), "submit_intent_recorded": False}
                else:
                    report["cleanup_error"] = "composer_cleanup_failed"
            finally:
                try:
                    if isinstance(primary_error, HumanVerificationRequired):
                        report["temporary_tab_preserved"] = True
                        # Let the operator complete verification in this same
                        # verified profile, without a lingering probe write fence.
                        await release_probe_fences(page, context, fence, report,
                                                   route_installed=route_installed, bypass_set=bypass_set)
                    else:
                        await traced_operation("temporary_tab_close", page.close(), trace=trace, timeout_ms=3000)
                        report["temporary_tab_closed"] = True
                except Exception as exc:
                    trace.fail(exc)
                    report["tab_close_error"] = "preserved_tab_cleanup_failed" if isinstance(primary_error, HumanVerificationRequired) else "temporary_tab_close_failed"
                    if primary_error is None:
                        raise
            trace.phase = None
        if isinstance(primary_error, HumanVerificationRequired):
            raise primary_error
        if report.get("cleanup_error"):
            raise RuntimeError("composer_cleanup_failed")
        if report["blocked_publish_requests"]:
            raise RuntimeError("unexpected_publish_request_blocked")
    finally:
        try:
            await bounded_operation(manager.__aexit__(None, None, None), timeout=3, phase="playwright_disconnect")
        except Exception:
            report["disconnect_error"] = "playwright_disconnect_failed"
            if not report.get("blocker") and not report.get("diagnostics", {}).get("failure"):
                raise
    report["phase"] = "complete"
    report["status"] = "passed"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = {"schema_version": "engage-mentions-probe-v1", "status": "running",
              "publication_enabled": False, "url": args.url, "expected_account": args.expected_account,
              "handles": args.handle, "observed_mentions": [], "cases": [],
              "blocked_non_read_requests": 0, "blocked_media_requests": 0, "blocked_publish_requests": 0,
              "editor_cleared": False, "temporary_tab_closed": False}
    started = time.monotonic()
    try:
        asyncio.run(run_probe(args, report))
    except Exception as exc:
        report["status"] = "human_verification_required" if isinstance(exc, HumanVerificationRequired) else "blocked"
        if isinstance(exc, (HumanVerificationRequired, BrowserOperationTimeout)):
            report.setdefault("blocker", {**exc.as_dict(), "submit_intent_recorded": False})
        if isinstance(exc, HumanVerificationRequired):
            report.update(getattr(exc, "context", {}))
        # Exception strings from transport libraries can include signed URLs or
        # headers. Report a bounded phase and exception class instead.
        report["error_type"] = type(exc).__name__
    report["total_seconds"] = round(time.monotonic() - started, 3)
    try:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError:
        print(json.dumps({"status": "blocked", "publication_enabled": False, "error": "report_write_failed"}))
        return 1
    print(json.dumps({"status": report["status"], "publication_enabled": False,
                      "report": str(args.report.resolve()), "phase": report.get("phase"),
                      "total_seconds": report["total_seconds"]}, ensure_ascii=True))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    # Unit callers keep main() local. The executable always has an owned worker
    # backstop in case a transport ignores async cancellation during teardown.
    from engage_publication_worker import STATE_ENV, WorkerProgress, supervise_adapter
    parsed = parse_args()
    from engage_tiktok import DEFAULT_BROWSER_STATE
    if not os.environ.get(STATE_ENV):
        completed = supervise_adapter(
            [sys.executable, "-u", str(Path(__file__).resolve()), *sys.argv[1:]],
            database=parsed.report, publication_id="native_mention_probe",
            social_browser_state=DEFAULT_BROWSER_STATE,
            total_timeout=600, stall_timeout=600,
        )
        payload = json.loads(completed.stdout) if completed.stdout.strip() else {}
        if payload.get("status") in {"reconcile_required", "worker_start_failed", "worker_busy"}:
            can_save = payload.get("worker_stopped") is True or payload.get("status") == "worker_start_failed"
            payload = {"schema_version": "engage-mentions-probe-v1", "status": "blocked",
                       "publication_enabled": False,
                       "reason": payload.get("reason", "probe_worker_interrupted"),
                       "worker_progress_path": payload.get("worker_progress_path", ""),
                       "editor_cleared": False, "temporary_tab_closed": False}
            if can_save:
                parsed.report.parent.mkdir(parents=True, exist_ok=True)
                parsed.report.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(payload), flush=True)
        raise SystemExit(completed.returncode)
    progress = WorkerProgress.from_environment(publication_id="native_mention_probe", database=parsed.report,
                                               social_browser_state=DEFAULT_BROWSER_STATE)
    code = 1
    try:
        progress.phase("native_probe")
        code = main()
    finally:
        progress.close("complete" if code == 0 else "failed")
    raise SystemExit(code)
