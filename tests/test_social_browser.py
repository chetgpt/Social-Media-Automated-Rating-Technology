import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

import social_browser

from social_browser import (
    DEFAULT_PROFILE_DIRECTORY,
    ENGAGE_PROFILE_DIRECTORY,
    PROFILE_MODE_EXISTING,
    browser_profile_identity,
    configure_designated_profile,
    designated_profile_path,
    display_cdp_url,
    ensure_engage_profile7_browser,
    load_engage_profile7_designation,
    load_designated_profile,
    profile_identity_from_arguments,
    read_live_debugging_endpoint,
)


def test_designated_profile_configuration_is_stable(tmp_path: Path):
    runtime = tmp_path / "runtime"
    user_data = tmp_path / "Edge User Data"
    (user_data / "Profile 7").mkdir(parents=True)

    first = configure_designated_profile(runtime, user_data, "Profile 7")
    second = configure_designated_profile(runtime, user_data, "Profile 7")
    loaded = load_designated_profile(runtime)

    assert first["designation_id"] == second["designation_id"]
    assert loaded["designation_id"] == first["designation_id"]
    assert loaded["profile_directory"] == "Profile 7"
    assert designated_profile_path(runtime).is_file()


def test_missing_designation_auto_selects_existing_edge_profile7(
    tmp_path: Path,
    monkeypatch,
):
    runtime = tmp_path / "runtime"
    user_data = tmp_path / "Microsoft" / "Edge" / "User Data"
    (user_data / ENGAGE_PROFILE_DIRECTORY).mkdir(parents=True)
    monkeypatch.setattr(
        social_browser,
        "edge_default_user_data_dir",
        lambda: user_data,
    )

    designation = load_designated_profile(runtime)

    assert designation["mode"] == PROFILE_MODE_EXISTING
    assert designation["user_data_dir"] == str(user_data)
    assert designation["profile_directory"] == ENGAGE_PROFILE_DIRECTORY
    assert not (runtime / "edge_profile").exists()


def test_missing_profile7_fails_without_creating_managed_profile(
    tmp_path: Path,
    monkeypatch,
):
    runtime = tmp_path / "runtime"
    user_data = tmp_path / "Microsoft" / "Edge" / "User Data"
    user_data.mkdir(parents=True)
    monkeypatch.setattr(
        social_browser,
        "edge_default_user_data_dir",
        lambda: user_data,
    )

    with pytest.raises(RuntimeError, match="will not create or substitute"):
        load_designated_profile(runtime)

    assert not (runtime / "edge_profile").exists()
    assert not designated_profile_path(runtime).exists()


@pytest.mark.parametrize(
    ("mode", "profile_directory", "different_root"),
    [
        ("managed_cdp", "Profile 7", False),
        (PROFILE_MODE_EXISTING, "Default", False),
        (PROFILE_MODE_EXISTING, "Profile 7", True),
    ],
)
def test_engage_designation_rejects_any_profile_other_than_existing_profile7(
    tmp_path: Path,
    monkeypatch,
    mode,
    profile_directory,
    different_root,
):
    runtime = tmp_path / "runtime"
    expected_root = tmp_path / "Edge User Data"
    actual_root = tmp_path / "Other User Data" if different_root else expected_root
    (actual_root / profile_directory).mkdir(parents=True)
    monkeypatch.setattr(
        social_browser,
        "edge_default_user_data_dir",
        lambda: expected_root,
    )
    configure_designated_profile(
        runtime,
        actual_root,
        profile_directory,
        mode=mode,
    )

    with pytest.raises(RuntimeError, match="requires Microsoft Edge"):
        load_engage_profile7_designation(runtime)


def test_ensure_engage_browser_starts_and_returns_only_verified_profile7_state(
    tmp_path: Path,
    monkeypatch,
):
    runtime = tmp_path / "runtime"
    user_data = tmp_path / "Edge User Data"
    (user_data / ENGAGE_PROFILE_DIRECTORY).mkdir(parents=True)
    monkeypatch.setattr(
        social_browser,
        "edge_default_user_data_dir",
        lambda: user_data,
    )
    calls = []

    def fake_start(args, received_runtime, designation):
        calls.append((args, received_runtime, designation))
        social_browser.write_json(
            social_browser.state_path(received_runtime),
            {
                "mode": PROFILE_MODE_EXISTING,
                "cdp_url": "ws://127.0.0.1:9222/devtools/browser/test",
                "watcher_pid": 4321,
                "profile_path": str(user_data),
                "profile_directory": ENGAGE_PROFILE_DIRECTORY,
                "profile_designation_id": designation["designation_id"],
                "profile_verified": True,
            },
        )
        return 0

    monkeypatch.setattr(social_browser, "start_existing_profile", fake_start)
    monkeypatch.setattr(
        social_browser,
        "edge_bridge_watcher_is_running",
        lambda pid: pid == 4321,
    )

    result = ensure_engage_profile7_browser(runtime, startup_timeout=45)

    assert len(calls) == 1
    assert calls[0][2]["profile_directory"] == ENGAGE_PROFILE_DIRECTORY
    assert result["state"]["profile_directory"] == ENGAGE_PROFILE_DIRECTORY
    assert result["designation"]["mode"] == PROFILE_MODE_EXISTING


def test_profile_identity_requires_exact_user_data_and_profile_directory(tmp_path: Path):
    profile = tmp_path / "edge_profile"
    arguments = [
        f"--user-data-dir={profile}",
        "--profile-directory=Default",
        "--enable-automation",
    ]

    identity = profile_identity_from_arguments(arguments, profile)
    wrong_profile = profile_identity_from_arguments(
        [
            f"--user-data-dir={tmp_path / 'other_profile'}",
            "--profile-directory=Default",
        ],
        profile,
    )
    wrong_directory = profile_identity_from_arguments(
        [f"--user-data-dir={profile}", "--profile-directory=Profile 7"],
        profile,
    )

    assert identity["verified"] is True
    assert wrong_profile["verified"] is False
    assert wrong_directory["verified"] is False


def test_browser_identity_uses_observed_edge_version_profile_path(tmp_path: Path):
    user_data = tmp_path / "Edge User Data"
    expected_profile = user_data / ENGAGE_PROFILE_DIRECTORY
    expected_profile.mkdir(parents=True)
    other_profile = user_data / "Default"
    other_profile.mkdir()

    class FakePage:
        def __init__(self, observed_path):
            self.observed_path = observed_path
            self.closed = False

        async def goto(self, url, **kwargs):
            assert url == "edge://version/"

        async def evaluate(self, script):
            return str(self.observed_path)

        def is_closed(self):
            return self.closed

        async def close(self):
            self.closed = True

    class FakeContext:
        def __init__(self, observed_path):
            self.page = FakePage(observed_path)

        async def new_page(self):
            return self.page

    class FakeBrowser:
        contexts = [
            FakeContext(other_profile),
            FakeContext(expected_profile),
        ]

    identity = asyncio.run(
        browser_profile_identity(
            FakeBrowser(),
            user_data,
            ENGAGE_PROFILE_DIRECTORY,
        )
    )

    assert identity["verified"] is True
    assert identity["context_index"] == 1
    assert identity["verification_method"] == "edge_version_profile_path"


def test_existing_profile_designation_preserves_display_name(tmp_path: Path):
    runtime = tmp_path / "runtime"
    user_data = tmp_path / "Edge User Data"
    profile = user_data / "Profile 7"
    profile.mkdir(parents=True)
    (profile / "Preferences").write_text(
        '{"profile":{"name":"Profile 1"}}',
        encoding="utf-8",
    )

    configured = configure_designated_profile(
        runtime,
        user_data,
        "Profile 7",
        mode=PROFILE_MODE_EXISTING,
    )

    assert configured["mode"] == PROFILE_MODE_EXISTING
    assert configured["profile_display_name"] == "Profile 1"


def test_live_debugging_endpoint_comes_from_edge_active_port_file(tmp_path: Path):
    (tmp_path / "DevToolsActivePort").write_text(
        "9222\n/devtools/browser/test-browser-id\n",
        encoding="ascii",
    )

    endpoint = read_live_debugging_endpoint(tmp_path)

    assert endpoint["port"] == 9222
    assert endpoint["cdp_url"] == (
        "ws://127.0.0.1:9222/devtools/browser/test-browser-id"
    )


def test_display_cdp_url_redacts_browser_session_identifier():
    assert display_cdp_url(
        "ws://127.0.0.1:9222/devtools/browser/secret-browser-id"
    ) == "ws://127.0.0.1:9222"


def test_stop_without_active_state_does_not_touch_existing_profile(
    tmp_path: Path,
    monkeypatch,
):
    user_data = tmp_path / "User Data"
    (user_data / "Profile 7").mkdir(parents=True)
    designation = {
        "mode": PROFILE_MODE_EXISTING,
        "user_data_dir": str(user_data),
        "profile_directory": "Profile 7",
        "profile_display_name": "Profile 1",
        "designation_id": "test-designation",
    }
    monkeypatch.setattr(
        social_browser,
        "load_engage_profile7_designation",
        lambda runtime_dir: designation,
    )

    def unexpected_bridge_call(*args, **kwargs):
        raise AssertionError("stop should not touch Edge without active state")

    monkeypatch.setattr(social_browser, "run_edge_bridge", unexpected_bridge_call)
    args = SimpleNamespace(
        runtime_dir=str(tmp_path / "runtime"),
        cdp_url="",
        port=9223,
    )

    assert social_browser.stop(args) == 0


def existing_profile_test_setup(tmp_path: Path, monkeypatch):
    runtime = tmp_path / "runtime"
    user_data = tmp_path / "Microsoft" / "Edge" / "User Data"
    (user_data / ENGAGE_PROFILE_DIRECTORY).mkdir(parents=True)
    monkeypatch.setattr(
        social_browser,
        "edge_default_user_data_dir",
        lambda: user_data,
    )
    designation = configure_designated_profile(
        runtime,
        user_data,
        ENGAGE_PROFILE_DIRECTORY,
        mode=PROFILE_MODE_EXISTING,
    )
    args = SimpleNamespace(
        startup_timeout=30.0,
        open_tabs=False,
        json=True,
    )
    return runtime, user_data, designation, args


def test_existing_profile_cold_start_uses_permission_bridge_then_verifies(
    tmp_path: Path,
    monkeypatch,
):
    runtime, user_data, designation, args = existing_profile_test_setup(
        tmp_path,
        monkeypatch,
    )
    calls = []
    launched = []

    class FakeProcess:
        pid = 2468

    class FakeWatcher:
        pid = 9753

        @staticmethod
        def poll():
            return None

    def fake_popen(command, **kwargs):
        launched.append(command)
        calls.append("launch")
        return FakeProcess()

    def fake_bridge(command, profile_name, timeout, **kwargs):
        calls.append(f"bridge:{command}")
        assert profile_name == "Profile 7"
        return {"success": True, "window_handle": 1357, "process_id": 2468}

    def fake_endpoint(profile, timeout):
        calls.append("endpoint")
        assert profile == user_data
        return {
            "port": 9222,
            "cdp_url": "ws://127.0.0.1:9222/devtools/browser/test",
            "active_port_file": str(user_data / "DevToolsActivePort"),
        }

    def fake_watcher(profile_name, window_handle):
        calls.append("watcher")
        assert profile_name == "Profile 7"
        assert window_handle == 1357
        return FakeWatcher()

    async def fake_status(url, **kwargs):
        calls.append("verify")
        assert kwargs["expected_profile"] == user_data
        assert kwargs["expected_profile_directory"] == ENGAGE_PROFILE_DIRECTORY
        return {"reachable": True, "profile": {"verified": True}, "platforms": {}}

    monkeypatch.setattr(
        social_browser,
        "edge_bridge_watcher_is_running",
        lambda pid: pid == 9753,
    )
    monkeypatch.setattr(social_browser, "read_live_debugging_endpoint", lambda profile: {})
    monkeypatch.setattr(social_browser, "edge_executable", lambda: Path("msedge.exe"))
    monkeypatch.setattr(social_browser.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(social_browser, "run_edge_bridge", fake_bridge)
    monkeypatch.setattr(social_browser, "wait_for_live_debugging_endpoint", fake_endpoint)
    monkeypatch.setattr(social_browser, "start_edge_bridge_watcher", fake_watcher)
    monkeypatch.setattr(social_browser, "browser_status", fake_status)

    assert social_browser.start_existing_profile(args, runtime, designation) == 0

    assert calls == ["launch", "bridge:enable", "endpoint", "watcher", "verify"]
    command = launched[0]
    assert f"--profile-directory={ENGAGE_PROFILE_DIRECTORY}" in command
    assert "about:blank" in command
    assert not any(item.startswith("--remote-debugging-port") for item in command)
    assert not any(item.startswith("--remote-debugging-address") for item in command)
    assert not any(item.startswith("--user-data-dir") for item in command)
    state = social_browser.read_json(social_browser.state_path(runtime))
    assert state["bridge_origin"] == "enabled_here"
    assert state["watcher_pid"] == 9753


def test_existing_profile_bridge_error_is_not_hidden(
    tmp_path: Path,
    monkeypatch,
):
    runtime, _, designation, args = existing_profile_test_setup(tmp_path, monkeypatch)
    endpoint_waited = False
    watcher_started = False

    class FakeProcess:
        pid = 2468

    def fail_bridge(*args, **kwargs):
        raise RuntimeError("approval prompt not found")

    def unexpected_endpoint(*args, **kwargs):
        nonlocal endpoint_waited
        endpoint_waited = True
        return {}

    def unexpected_watcher(*args, **kwargs):
        nonlocal watcher_started
        watcher_started = True
        raise AssertionError("watcher must not start after bridge failure")

    monkeypatch.setattr(social_browser, "edge_bridge_watcher_is_running", lambda pid: False)
    monkeypatch.setattr(social_browser, "read_live_debugging_endpoint", lambda profile: {})
    monkeypatch.setattr(social_browser, "edge_executable", lambda: Path("msedge.exe"))
    monkeypatch.setattr(
        social_browser.subprocess,
        "Popen",
        lambda *args, **kwargs: FakeProcess(),
    )
    monkeypatch.setattr(social_browser, "run_edge_bridge", fail_bridge)
    monkeypatch.setattr(
        social_browser,
        "wait_for_live_debugging_endpoint",
        unexpected_endpoint,
    )
    monkeypatch.setattr(social_browser, "start_edge_bridge_watcher", unexpected_watcher)

    with pytest.raises(RuntimeError, match="approval prompt not found"):
        social_browser.start_existing_profile(args, runtime, designation)

    assert endpoint_waited is False
    assert watcher_started is False
    assert not social_browser.state_path(runtime).exists()


def test_existing_profile_adopts_verified_shared_endpoint_without_relaunch(
    tmp_path: Path,
    monkeypatch,
):
    runtime, user_data, designation, args = existing_profile_test_setup(
        tmp_path,
        monkeypatch,
    )
    endpoint = {
        "port": 9222,
        "cdp_url": "ws://127.0.0.1:9222/devtools/browser/shared",
        "active_port_file": str(user_data / "DevToolsActivePort"),
    }

    class FakeWatcher:
        pid = 8642

        @staticmethod
        def poll():
            return None

    async def fake_status(url, **kwargs):
        assert url == endpoint["cdp_url"]
        assert kwargs["expected_profile"] == user_data
        return {"reachable": True, "profile": {"verified": True}, "platforms": {}}

    def unexpected(*args, **kwargs):
        raise AssertionError("a verified shared endpoint must not relaunch or toggle Edge")

    monkeypatch.setattr(
        social_browser,
        "edge_bridge_watcher_is_running",
        lambda pid: pid == 8642,
    )
    monkeypatch.setattr(social_browser, "read_live_debugging_endpoint", lambda profile: endpoint)
    monkeypatch.setattr(social_browser, "tcp_port_reachable", lambda *args, **kwargs: True)
    monkeypatch.setattr(social_browser, "start_edge_bridge_watcher", lambda *args: FakeWatcher())
    monkeypatch.setattr(social_browser, "browser_status", fake_status)
    monkeypatch.setattr(social_browser, "edge_executable", lambda: Path("msedge.exe"))
    monkeypatch.setattr(social_browser.subprocess, "Popen", unexpected)
    monkeypatch.setattr(social_browser, "run_edge_bridge", unexpected)

    assert social_browser.start_existing_profile(args, runtime, designation) == 0

    state = social_browser.read_json(social_browser.state_path(runtime))
    assert state["bridge_origin"] == "adopted_existing"
    assert state["watcher_pid"] == 8642


def test_existing_profile_stop_detaches_without_disabling_shared_edge(
    tmp_path: Path,
    monkeypatch,
):
    runtime, user_data, designation, _ = existing_profile_test_setup(
        tmp_path,
        monkeypatch,
    )
    social_browser.write_json(
        social_browser.state_path(runtime),
        {
            "mode": PROFILE_MODE_EXISTING,
            "cdp_url": "ws://127.0.0.1:9222/devtools/browser/shared",
            "watcher_pid": 8642,
            "profile_path": str(user_data),
            "profile_directory": ENGAGE_PROFILE_DIRECTORY,
            "profile_designation_id": designation["designation_id"],
            "profile_verified": True,
        },
    )
    stopped = []

    monkeypatch.setattr(social_browser, "edge_bridge_watcher_is_running", lambda pid: True)
    monkeypatch.setattr(social_browser, "stop_edge_bridge_watcher", stopped.append)
    monkeypatch.setattr(
        social_browser,
        "run_edge_bridge",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("detach must not disable or close shared Edge")
        ),
    )
    args = SimpleNamespace(runtime_dir=str(runtime), cdp_url="", port=9223)

    assert social_browser.stop(args) == 0
    assert stopped == [8642]
    assert not social_browser.state_path(runtime).exists()
