"""Offline argument, outbound-fence, and report tests for the native probe."""
import asyncio
import json
import sys
from types import SimpleNamespace

import pytest

import engage_mentions_probe as probe


@pytest.fixture(autouse=True)
def synthetic_page_has_no_challenge(monkeypatch):
    import engage_creator_mentions_ui as ui
    async def clear(*_args, **_kwargs):
        return None
    monkeypatch.setattr(probe, "ensure_no_challenge", clear)
    monkeypatch.setattr(ui, "ensure_no_challenge", clear)


URL = "https://www.tiktok.com/@piano_creator/video/123456789"


def arguments(tmp_path, *extra):
    return ["--url", URL, "--handle", "@Piano_A", "--expected-account", "@Operator",
            "--report", str(tmp_path / "probe.json"), *extra]


def test_explicit_scope_normalizes_handles_without_changing_post_url(tmp_path):
    args = probe.parse_args(arguments(tmp_path, "--handle", "piano.b"))
    assert args.url == URL
    assert args.handle == ["piano_a", "piano.b"]
    assert args.expected_account == "operator"


@pytest.mark.parametrize("url", [
    "http://www.tiktok.com/@piano_creator/video/123",
    "https://www.tiktok.com/@piano_creator/video/123?token=secret",
    "https://www.tiktok.com/@piano_creator/video/123#fragment",
    "https://evil.test/@piano_creator/video/123",
    "https://secret@www.tiktok.com/@piano_creator/video/123",
    "https://www.tiktok.com:443/@piano_creator/video/123",
    "https://www.tiktok.com/@piano_creator/video/not-numeric",
    "https://www.tiktok.com/@piano_creator",
    "https://vm.tiktok.com/short-link",
])
def test_noncanonical_or_unbounded_target_is_rejected_before_browser(tmp_path, monkeypatch, url):
    async def forbidden(*_args):
        raise AssertionError("browser work must not run")
    monkeypatch.setattr(probe, "run_probe", forbidden)
    argv = arguments(tmp_path)
    argv[1] = url
    with pytest.raises(SystemExit) as failure:
        probe.main(argv)
    assert failure.value.code == 2


@pytest.mark.parametrize("extras", [
    ["--handle", "piano_a"],
    ["--handle", "piano_b", "--handle", "piano_c"],
    ["--handle", "@Name With Space"],
    ["--handle", "name\nother"],
])
def test_invalid_or_excess_creator_scope_is_rejected(tmp_path, extras):
    with pytest.raises(SystemExit) as failure:
        probe.parse_args(arguments(tmp_path, *extras))
    assert failure.value.code == 2


def test_help_is_offline(monkeypatch, capsys):
    async def forbidden(*_args):
        raise AssertionError("--help must not start a browser")
    monkeypatch.setattr(probe, "run_probe", forbidden)
    with pytest.raises(SystemExit) as result:
        probe.main(["--help"])
    assert result.value.code == 0
    assert "--expected-account" in capsys.readouterr().out


class Route:
    def __init__(self, method, resource_type="fetch", url="https://www.tiktok.com/api/test/?token=private"):
        self.request = SimpleNamespace(method=method, resource_type=resource_type, url=url)
        self.actions = []
    async def abort(self):
        self.actions.append("abort")
    async def continue_(self):
        self.actions.append("continue")


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "CONNECT", "TRACE", "unknown"])
def test_fence_aborts_every_non_read_method_and_retains_no_request_details(method):
    report = {"blocked_non_read_requests": 0, "blocked_media_requests": 0, "blocked_publish_requests": 0}
    route = Route(method)
    asyncio.run(probe.request_fence(report, lambda _: False)(route))
    assert route.actions == ["abort"]
    assert report == {"blocked_non_read_requests": 1, "blocked_media_requests": 0, "blocked_publish_requests": 0}
    assert "private" not in json.dumps(report)


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_fence_allows_read_methods_but_blocks_even_get_to_publish_endpoint(method):
    report = {"blocked_non_read_requests": 0, "blocked_media_requests": 0, "blocked_publish_requests": 0}
    route = Route(method)
    asyncio.run(probe.request_fence(report, lambda _: False)(route))
    assert route.actions == ["continue"]
    forbidden = Route(method)
    asyncio.run(probe.request_fence(report, lambda _: True)(forbidden))
    assert forbidden.actions == ["abort"]
    assert report["blocked_publish_requests"] == 1


@pytest.mark.parametrize("resource_type,url", [
    ("media", "https://video.test/play"),
    ("fetch", "https://video.test/video.mp4?signature=private"),
    ("fetch", "https://video.test/audio.M4A"),
])
def test_fence_blocks_media_even_when_fetched_as_xhr(resource_type, url):
    report = {"blocked_non_read_requests": 0, "blocked_media_requests": 0, "blocked_publish_requests": 0}
    route = Route("GET", resource_type, url)
    asyncio.run(probe.request_fence(report, lambda _: False)(route))
    assert route.actions == ["abort"]
    assert report["blocked_media_requests"] == 1


@pytest.mark.parametrize("labels", [["@Piano A 🎹"], ["@Piano A 🎹", "@Piano B"]])
def test_rehearsals_are_single_paragraph_and_contain_each_exact_label(labels):
    cases = probe.composition_cases(labels)
    assert len(cases) == 2
    for case in cases:
        assert "\n" not in case and "\r" not in case
        assert all(case.count(label) == 1 for label in labels)


def test_error_report_and_console_do_not_expose_transport_exception(tmp_path, monkeypatch, capsys):
    async def failed(_args, report):
        report["phase"] = "target_account_verification"
        raise RuntimeError("https://private.test/?token=secret Authorization: private")
    monkeypatch.setattr(probe, "run_probe", failed)
    assert probe.main(arguments(tmp_path)) == 1
    saved = (tmp_path / "probe.json").read_text(encoding="utf-8")
    console = capsys.readouterr().out
    assert "private" not in saved + console and "secret" not in saved + console
    report = json.loads(saved)
    assert report["status"] == "blocked" and report["error_type"] == "RuntimeError"
    assert report["phase"] == "target_account_verification"
    assert report["publication_enabled"] is False


def test_cleanup_uses_only_select_all_and_backspace():
    keys = []
    async def noop():
        pass
    async def key(value):
        keys.append(value)
    async def state(_editor):
        return {"text": "", "native_labels": [], "links": []}
    async def focused(_script):
        return True
    page = SimpleNamespace(bring_to_front=noop, keyboard=SimpleNamespace(press=key))
    editor = SimpleNamespace(focus=noop, evaluate=focused)
    asyncio.run(probe.clear_editor(page, editor, SimpleNamespace(editor_state=state)))
    assert keys == ["ControlOrMeta+A", "Backspace"]


def test_cleanup_does_not_claim_success_with_a_native_entity_remaining():
    async def noop(*_args):
        pass
    async def state(_editor):
        return {"text": "", "native_labels": ["@Piano A"], "links": []}
    async def focused(_script):
        return True
    page = SimpleNamespace(bring_to_front=noop, keyboard=SimpleNamespace(press=noop))
    with pytest.raises(RuntimeError, match="composer_cleanup_failed"):
        asyncio.run(probe.clear_editor(page, SimpleNamespace(focus=noop, evaluate=focused), SimpleNamespace(editor_state=state)))


def test_diagnostics_bound_events_drop_unapproved_fields_and_keep_first_failure():
    report = {"phase": "exact_creator_label_observation"}
    trace = probe.ProbeDiagnostics(report)
    trace("failed", {"operation": "editor_focus", "error_type": "TimeoutError",
                     "creator_handle": "guitar.les", "seconds": 5.0,
                     "exception": "Authorization: secret", "url": "https://private.test/?token=secret"})
    trace.phase = "cleanup"
    for _ in range(trace.MAX_EVENTS + 1):
        trace("passed", {"operation": "editor_backspace", "seconds": 0.1})
    trace("failed", {"operation": "temporary_tab_close", "error_type": "RuntimeError"})
    trace.fail(RuntimeError("private token=secret"))
    data = report["diagnostics"]
    assert len(data["events"]) == trace.MAX_EVENTS
    assert data["dropped_events"] == 4
    assert data["failure"]["code"] == "editor_focus_timeout"
    assert data["failure"]["phase"] == "exact_creator_label_observation"
    assert data["current_operation"]["phase"] == "cleanup"
    assert "private" not in json.dumps(report) and "secret" not in json.dumps(report)


def test_diagnostics_reject_unsafe_operation_values_and_invalid_counts():
    trace = probe.ProbeDiagnostics({"phase": "cleanup"})
    trace("passed", {"operation": "https://private.test/?token=secret", "error_type": "private error",
                     "creator_handle": "@invalid handle", "seconds": float("inf"), "attempt": -1,
                     "candidate_count": True, "timeout_ms": 1_000_000, "exact_candidate_count": float("nan")})
    assert trace.data["current_operation"] == {"event": "passed", "phase": "cleanup"}


def test_untraced_validation_failure_does_not_blame_preceding_successful_operation():
    trace = probe.ProbeDiagnostics({"phase": "exact_creator_label_observation"})
    trace("passed", {"operation": "suggestion_snapshot_dispose"})
    trace.fail(RuntimeError("private validation token=secret"))
    assert trace.data["failure"]["code"] == "unclassified_operation_failed"
    assert trace.data["failure"]["phase"] == "exact_creator_label_observation"
    assert "private" not in json.dumps(trace.data) and "secret" not in json.dumps(trace.data)


def test_probe_preserves_operation_timeout_when_composer_and_tab_cleanup_fail(tmp_path, monkeypatch, capsys):
    actions = []
    async def noop(*_args, **_kwargs):
        pass
    async def account(_page):
        return "operator"
    async def focus():
        actions.append("focus")
        if actions.count("focus") == 1:
            raise TimeoutError("https://private.test/?token=secret Authorization: private")
        raise RuntimeError("private cleanup token=secret")
    async def close():
        actions.append("close")
        raise ValueError("private tab cleanup token=secret")
    editor = SimpleNamespace(focus=focus, scroll_into_view_if_needed=noop)
    page = SimpleNamespace(route=noop, goto=noop, bring_to_front=noop, close=close)
    async def first_visible(*_args, **_kwargs):
        return editor, "synthetic_editor"
    async def new_page():
        return page
    async def cdp_session(_page):
        return SimpleNamespace(send=noop, detach=noop)
    context = SimpleNamespace(new_page=new_page, new_cdp_session=cdp_session)
    async def verified_context(*_args):
        return context, None
    async def connect(*_args, **_kwargs):
        return object()
    class PlaywrightContext:
        async def __aenter__(self):
            return SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=connect))
        async def __aexit__(self, *_args):
            return False
    class Preflight:
        def __init__(self, **_kwargs):
            pass
        async def ensure_ready(self):
            return {"observed_account": "operator"}
    modules = {
        "engage_tiktok": SimpleNamespace(SocialBrowserPreflight=Preflight, DEFAULT_BROWSER_STATE=tmp_path / "state.json", active_tiktok_account=account),
        "social_browser": SimpleNamespace(load_engage_profile7_designation=lambda *_: {},
                                          load_verified_profile7_state=lambda *_: {"cdp_url": "http://localhost/synthetic"},
                                          verified_profile_context=verified_context),
        "playwright.async_api": SimpleNamespace(async_playwright=PlaywrightContext),
        "tiktok_publication_adapter": SimpleNamespace(INPUT_SELECTORS=["synthetic"], open_comments_panel=noop,
                                                       wait_for_first_visible=first_visible, is_tiktok_comment_publish_url=lambda _: False),
        "engage_creator_matching": SimpleNamespace(_mention_label=lambda value: value),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    assert probe.main(arguments(tmp_path)) == 1
    saved = (tmp_path / "probe.json").read_text(encoding="utf-8")
    output = capsys.readouterr().out
    report = json.loads(saved)
    assert actions == ["focus", "focus", "close"]
    assert report["error_type"] == "TimeoutError"
    assert report["diagnostics"]["failure"]["code"] == "editor_focus_timeout"
    assert report["diagnostics"]["failure"]["phase"] == "exact_creator_label_observation"
    assert report["cleanup_error"] == "composer_cleanup_failed"
    assert report["tab_close_error"] == "temporary_tab_close_failed"
    assert report["editor_cleared"] is False and report["temporary_tab_closed"] is False
    assert "private" not in saved + output and "secret" not in saved + output


def test_challenge_probe_preserves_target_and_removes_own_fences(tmp_path, monkeypatch, capsys):
    from engage_browser_guard import HumanVerificationRequired
    actions = []
    async def noop(*_args, **_kwargs):
        pass
    async def account(_page):
        return "operator"
    async def route(pattern, handler):
        actions.append(("route", pattern, handler))
    async def unroute(pattern, handler):
        actions.append(("unroute", pattern, handler))
    async def close():
        actions.append(("close",))
    async def screenshot(**_kwargs):
        actions.append(("screenshot",))
    async def send(method, params):
        actions.append((method, params["bypass"]))
    page = SimpleNamespace(route=route, unroute=unroute, goto=noop, close=close, screenshot=screenshot)
    async def new_page():
        return page
    async def cdp_session(_page):
        return SimpleNamespace(send=send, detach=noop)
    context = SimpleNamespace(new_page=new_page, new_cdp_session=cdp_session)
    async def verified_context(*_args):
        return context, None
    async def connect(*_args, **_kwargs):
        return object()
    class PlaywrightContext:
        async def __aenter__(self):
            return SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=connect))
        async def __aexit__(self, *_args):
            actions.append(("disconnect",))
            return False
    class Preflight:
        def __init__(self, **_kwargs):
            pass
        async def ensure_ready(self):
            return {"observed_account": "operator"}
    modules = {
        "engage_tiktok": SimpleNamespace(SocialBrowserPreflight=Preflight, DEFAULT_BROWSER_STATE=tmp_path / "state.json", active_tiktok_account=account),
        "social_browser": SimpleNamespace(load_engage_profile7_designation=lambda *_: {},
                                          load_verified_profile7_state=lambda *_: {"cdp_url": "http://localhost/synthetic"},
                                          verified_profile_context=verified_context),
        "playwright.async_api": SimpleNamespace(async_playwright=PlaywrightContext),
        "tiktok_publication_adapter": SimpleNamespace(INPUT_SELECTORS=[], open_comments_panel=noop,
                                                       wait_for_first_visible=noop, is_tiktok_comment_publish_url=lambda _: False),
        "engage_creator_matching": SimpleNamespace(_mention_label=lambda value: value),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    async def challenge(_page, phase):
        raise HumanVerificationRequired(phase, "captcha_container")
    monkeypatch.setattr(probe, "ensure_no_challenge", challenge)
    assert probe.main(arguments(tmp_path)) == 1
    report = json.loads((tmp_path / "probe.json").read_text(encoding="utf-8"))
    assert report["status"] == "human_verification_required"
    assert report["blocker"]["submit_intent_recorded"] is False
    assert report["temporary_tab_preserved"] is True
    assert report["temporary_tab_closed"] is False
    assert report["human_verification_tab_ready"] is True
    assert report["failure_screenshot_saved"] is True
    assert report["editor_cleared"] is False
    assert not any(action[0] == "close" for action in actions)
    assert ("Network.setBypassServiceWorker", True) in actions
    assert ("Network.setBypassServiceWorker", False) in actions
    assert ("disconnect",) == actions[-1]
    installed = next(action for action in actions if action[0] == "route")
    removed = next(action for action in actions if action[0] == "unroute")
    assert installed[1:] == removed[1:]  # Remove this exact handler only.


def test_preserved_tab_cleanup_attempts_service_worker_restore_after_route_error():
    actions = []
    async def unroute(*_args):
        raise RuntimeError("private transport")
    async def send(*_args):
        actions.append("restore")
    async def detach():
        actions.append("detach")
    async def cdp(_page):
        return SimpleNamespace(send=send, detach=detach)
    report = {}
    with pytest.raises(RuntimeError, match="preserved_tab_cleanup_failed"):
        asyncio.run(probe.release_probe_fences(SimpleNamespace(unroute=unroute),
                                               SimpleNamespace(new_cdp_session=cdp), object(), report,
                                               route_installed=True, bypass_set=True))
    assert actions == ["restore", "detach"]
    assert report == {"probe_route_removed": False, "probe_service_worker_restored": True,
                      "human_verification_tab_ready": False}
