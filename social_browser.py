"""Manage the authenticated Edge session used by social scrapers.

The controller supports a managed automation profile and Edge's permission-based
attachment to an existing profile. Scraper workers connect over local CDP,
observe the same API responses as the signed-in web applications, and disconnect
without closing the user's other Edge windows.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent
DEFAULT_RUNTIME_DIR = ROOT / "comments_data" / "social_browser"
DEFAULT_PORT = 9223
DEFAULT_PROFILE_DIRECTORY = "Default"
ENGAGE_PROFILE_DIRECTORY = "Profile 7"
DESIGNATED_PROFILE_NAME = "designated_profile.json"
LIVE_DEBUG_HELPER = ROOT / "edge_live_debugging.ps1"
PROFILE_MODE_MANAGED = "managed_cdp"
PROFILE_MODE_EXISTING = "existing_profile_attach"
PLATFORM_URLS = {
    "youtube": "https://www.youtube.com/",
    "instagram": "https://www.instagram.com/",
    "facebook": "https://www.facebook.com/",
    "tiktok": "https://www.tiktok.com/",
    "x": "https://x.com/home",
    "threads": "https://www.threads.com/",
    "linkedin": "https://www.linkedin.com/feed/",
}
AUTH_COOKIE_RULES = {
    "youtube": {
        "urls": ["https://www.youtube.com/", "https://accounts.google.com/"],
        "required_any": ["SAPISID", "__Secure-1PAPISID", "__Secure-3PAPISID", "LOGIN_INFO"],
    },
    "instagram": {
        "urls": ["https://www.instagram.com/"],
        "required_all": ["sessionid"],
    },
    "facebook": {
        "urls": ["https://www.facebook.com/"],
        "required_all": ["c_user", "xs"],
    },
    "tiktok": {
        "urls": ["https://www.tiktok.com/"],
        "required_any": ["sessionid", "sessionid_ss", "sid_tt"],
    },
    "x": {
        "urls": ["https://x.com/", "https://twitter.com/"],
        "required_all": ["auth_token", "ct0"],
    },
    "threads": {
        "urls": ["https://www.threads.com/", "https://www.threads.net/"],
        "required_all": ["sessionid"],
    },
    "linkedin": {
        "urls": ["https://www.linkedin.com/"],
        "required_all": ["li_at"],
    },
}

# Keep watcher process handles alive for the lifetime of the Python workflow.
# Some Windows hosts tear down the consent watcher when its final Popen handle
# is collected even though the child was started in a new process group.
_OWNED_EDGE_BRIDGE_WATCHERS: dict[int, subprocess.Popen[bytes]] = {}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def edge_executable() -> Path:
    candidates = [
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        Path(os.environ.get("PROGRAMFILES", "")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("Microsoft Edge was not found")


def edge_default_user_data_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Edge" / "User Data"


def canonical_path(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


def profile_display_name(user_data_dir: Path, profile_directory: str) -> str:
    preferences = read_json(user_data_dir / profile_directory / "Preferences")
    profile = preferences.get("profile") if isinstance(preferences.get("profile"), dict) else {}
    return str(profile.get("name") or profile_directory)


def state_path(runtime_dir: Path) -> Path:
    return runtime_dir / "state.json"


def profile_path(runtime_dir: Path) -> Path:
    return runtime_dir / "edge_profile"


def designated_profile_path(runtime_dir: Path) -> Path:
    return runtime_dir / DESIGNATED_PROFILE_NAME


def cdp_url(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def display_cdp_url(value: str) -> str:
    """Render a local endpoint without exposing its browser-session UUID."""
    parsed = urlparse(str(value or ""))
    if not parsed.scheme or not parsed.hostname:
        return "<unavailable>"
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}"


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")
    temporary.replace(path)


def configure_designated_profile(
    runtime_dir: Path,
    user_data_dir: Path,
    profile_directory: str,
    mode: str = "",
) -> dict[str, Any]:
    # Keep the configured spelling for launch arguments while using canonical
    # paths only for identity comparisons.
    user_data_dir = Path(os.path.abspath(os.path.expandvars(str(user_data_dir))))
    profile_directory = profile_directory.strip()
    if not profile_directory:
        raise RuntimeError("The designated Edge profile directory cannot be empty")
    edge_profile = user_data_dir / profile_directory
    if not edge_profile.is_dir():
        raise RuntimeError(f"The designated Edge profile does not exist: {edge_profile}")

    mode = mode.strip()
    if not mode:
        mode = (
            PROFILE_MODE_EXISTING
            if canonical_path(user_data_dir) == canonical_path(edge_default_user_data_dir())
            else PROFILE_MODE_MANAGED
        )
    if mode not in {PROFILE_MODE_MANAGED, PROFILE_MODE_EXISTING}:
        raise RuntimeError(f"Unsupported social-browser profile mode: {mode}")

    config_file = designated_profile_path(runtime_dir)
    previous = read_json(config_file)
    previous_path = str(previous.get("user_data_dir") or "")
    previous_directory = str(previous.get("profile_directory") or "")
    same_designation = bool(
        previous_path
        and canonical_path(Path(previous_path)) == canonical_path(user_data_dir)
        and previous_directory.casefold() == profile_directory.casefold()
        and str(previous.get("mode") or PROFILE_MODE_MANAGED) == mode
    )
    designation = {
        "schema_version": 2,
        "designation_id": (
            str(previous.get("designation_id") or uuid.uuid4().hex)
            if same_designation
            else uuid.uuid4().hex
        ),
        "user_data_dir": str(user_data_dir),
        "profile_directory": profile_directory,
        "profile_display_name": profile_display_name(user_data_dir, profile_directory),
        "mode": mode,
        "configured_at": utc_now(),
    }
    write_json(config_file, designation)
    return designation


def load_designated_profile(runtime_dir: Path) -> dict[str, Any]:
    config_file = designated_profile_path(runtime_dir)
    designation = read_json(config_file)
    if not designation:
        fallback_user_data = edge_default_user_data_dir()
        fallback_profile = fallback_user_data / ENGAGE_PROFILE_DIRECTORY
        if not fallback_profile.is_dir():
            raise RuntimeError(
                "The required authenticated Edge Profile 7 does not exist: "
                f"{fallback_profile}. ENGAGE will not create or substitute a "
                "managed browser profile."
            )
        designation = configure_designated_profile(
            runtime_dir,
            fallback_user_data,
            ENGAGE_PROFILE_DIRECTORY,
            mode=PROFILE_MODE_EXISTING,
        )

    user_data_dir = str(designation.get("user_data_dir") or "")
    profile_directory = str(designation.get("profile_directory") or "")
    designation_id = str(designation.get("designation_id") or "")
    if not user_data_dir or not profile_directory or not designation_id:
        raise RuntimeError(f"Invalid designated social-browser profile: {config_file}")
    edge_profile = Path(user_data_dir) / profile_directory
    if not edge_profile.is_dir():
        raise RuntimeError(f"The designated Edge profile does not exist: {edge_profile}")
    mode = str(designation.get("mode") or "")
    if not mode:
        mode = (
            PROFILE_MODE_EXISTING
            if canonical_path(Path(user_data_dir)) == canonical_path(edge_default_user_data_dir())
            else PROFILE_MODE_MANAGED
        )
        designation["mode"] = mode
    if mode not in {PROFILE_MODE_MANAGED, PROFILE_MODE_EXISTING}:
        raise RuntimeError(f"Unsupported designated social-browser mode: {mode}")
    designation.setdefault(
        "profile_display_name",
        profile_display_name(Path(user_data_dir), profile_directory),
    )
    return designation


def load_engage_profile7_designation(runtime_dir: Path) -> dict[str, Any]:
    """Load the one browser identity ENGAGE is allowed to use."""
    designation = load_designated_profile(runtime_dir)
    expected_user_data = edge_default_user_data_dir()
    actual_user_data = Path(str(designation.get("user_data_dir") or ""))
    actual_directory = str(designation.get("profile_directory") or "")
    mode = str(designation.get("mode") or "")
    if (
        mode != PROFILE_MODE_EXISTING
        or canonical_path(actual_user_data) != canonical_path(expected_user_data)
        or actual_directory.casefold() != ENGAGE_PROFILE_DIRECTORY.casefold()
    ):
        raise RuntimeError(
            "ENGAGE requires Microsoft Edge's existing Profile 7 at "
            f"{expected_user_data / ENGAGE_PROFILE_DIRECTORY}; the saved "
            "designation points somewhere else. No fallback profile will be used."
        )
    if not (actual_user_data / actual_directory).is_dir():
        raise RuntimeError(
            "The designated Edge Profile 7 directory is missing: "
            f"{actual_user_data / actual_directory}"
        )
    if not str(designation.get("designation_id") or ""):
        raise RuntimeError("The Edge Profile 7 designation has no identity")
    return designation


def state_matches_designation(
    state: dict[str, Any],
    designation: dict[str, Any],
) -> bool:
    """Return whether runtime state belongs to the exact saved profile."""
    state_profile = str(state.get("profile_path") or "")
    designated_profile = str(designation.get("user_data_dir") or "")
    return bool(
        state.get("mode") == PROFILE_MODE_EXISTING
        and state.get("profile_verified") is True
        and state_profile
        and designated_profile
        and canonical_path(Path(state_profile))
        == canonical_path(Path(designated_profile))
        and str(state.get("profile_directory") or "").casefold()
        == str(designation.get("profile_directory") or "").casefold()
        and str(state.get("profile_designation_id") or "")
        == str(designation.get("designation_id") or "")
    )


def launch_argument_value(arguments: list[str], name: str) -> str:
    option = f"--{name}"
    for index, argument in enumerate(arguments):
        if argument.startswith(option + "="):
            return argument.split("=", 1)[1].strip().strip('"')
        if argument == option and index + 1 < len(arguments):
            return arguments[index + 1].strip().strip('"')
    return ""


def profile_identity_from_arguments(
    arguments: list[str],
    expected_profile: Path,
    expected_profile_directory: str = DEFAULT_PROFILE_DIRECTORY,
) -> dict[str, Any]:
    observed_profile_text = launch_argument_value(arguments, "user-data-dir")
    observed_directory = launch_argument_value(arguments, "profile-directory")
    observed_profile = ""
    if observed_profile_text:
        observed_profile = str(Path(observed_profile_text).resolve())
    expected_profile_text = str(expected_profile.resolve())
    expected_directory_path = str(
        (expected_profile / expected_profile_directory).resolve()
    )
    observed_directory_path = (
        str((Path(observed_profile) / observed_directory).resolve())
        if observed_profile and observed_directory
        else ""
    )
    verified = bool(
        observed_profile
        and os.path.normcase(observed_profile) == os.path.normcase(expected_profile_text)
        and observed_directory.casefold() == expected_profile_directory.casefold()
    )
    return {
        "verified": verified,
        "expected_user_data_path": expected_profile_text,
        "observed_user_data_path": observed_profile,
        "expected_profile_path": expected_directory_path,
        "observed_profile_path": observed_directory_path,
        "expected_profile_directory": expected_profile_directory,
        "observed_profile_directory": observed_directory,
        "verification_error": "" if verified else "browser_launch_profile_mismatch",
        "verification_method": "browser_command_line",
    }


async def context_profile_path(context: Any) -> str:
    """Observe the concrete profile directory through Edge's version page."""
    page = None
    try:
        page = await context.new_page()
        await page.goto(
            "edge://version/",
            wait_until="domcontentloaded",
            timeout=15000,
        )
        return str(
            await page.evaluate(
                """
                () => {
                  const direct = document.querySelector('#profile_path');
                  if (direct && direct.textContent) {
                    return direct.textContent.trim();
                  }
                  for (const row of document.querySelectorAll('tr')) {
                    const cells = Array.from(row.querySelectorAll('td, th'));
                    if (
                      cells.length >= 2 &&
                      /profile\\s*path/i.test(cells[0].textContent || '')
                    ) {
                      return (cells[cells.length - 1].textContent || '').trim();
                    }
                  }
                  return '';
                }
                """
            )
            or ""
        ).strip()
    except Exception:
        return ""
    finally:
        if page is not None:
            try:
                if not page.is_closed():
                    await page.close()
            except Exception:
                pass


async def browser_profile_identity(
    browser: Any,
    expected_profile: Path,
    expected_profile_directory: str = DEFAULT_PROFILE_DIRECTORY,
) -> dict[str, Any]:
    expected_user_data = expected_profile.resolve()
    expected_directory_path = (
        expected_user_data / expected_profile_directory
    ).resolve()
    observed_context_paths: list[str] = []
    for index, context in enumerate(list(browser.contexts)):
        observed_path = await context_profile_path(context)
        if not observed_path:
            continue
        observed_context_paths.append(observed_path)
        try:
            matches = (
                canonical_path(Path(observed_path))
                == canonical_path(expected_directory_path)
            )
        except (OSError, RuntimeError, ValueError):
            matches = False
        if matches:
            return {
                "verified": True,
                "expected_user_data_path": str(expected_user_data),
                "observed_user_data_path": str(expected_user_data),
                "expected_profile_path": str(expected_directory_path),
                "observed_profile_path": str(Path(observed_path).resolve()),
                "expected_profile_directory": expected_profile_directory,
                "observed_profile_directory": Path(observed_path).name,
                "context_index": index,
                "verification_error": "",
                "verification_method": "edge_version_profile_path",
            }

    if observed_context_paths:
        return {
            "verified": False,
            "expected_user_data_path": str(expected_user_data),
            "observed_user_data_path": "",
            "expected_profile_path": str(expected_directory_path),
            "observed_profile_path": "",
            "expected_profile_directory": expected_profile_directory,
            "observed_profile_directory": "",
            "observed_context_profile_paths": observed_context_paths,
            "verification_error": "edge_version_profile_path_mismatch",
            "verification_method": "edge_version_profile_path",
        }

    # Managed automation profiles expose their launch command line. Existing
    # profiles usually do not, so this is a secondary observed signal rather
    # than a fabricated assertion.
    session = None
    try:
        session = await browser.new_browser_cdp_session()
        payload = await session.send("Browser.getBrowserCommandLine")
        arguments = payload.get("arguments") if isinstance(payload, dict) else []
        if not isinstance(arguments, list):
            arguments = []
        identity = profile_identity_from_arguments(
            [str(argument) for argument in arguments],
            expected_profile,
            expected_profile_directory,
        )
        if identity.get("verified"):
            identity["context_index"] = 0
            return identity
        return identity
    except Exception as exc:
        return {
            "verified": False,
            "expected_user_data_path": str(expected_user_data),
            "observed_user_data_path": "",
            "expected_profile_path": str(expected_directory_path),
            "observed_profile_path": "",
            "expected_profile_directory": expected_profile_directory,
            "observed_profile_directory": "",
            "observed_context_profile_paths": observed_context_paths,
            "verification_error": (
                "edge_version_profile_path_mismatch"
                if observed_context_paths
                else f"profile_identity_unavailable: {exc}"
            ),
        }
    finally:
        if session is not None:
            try:
                await session.detach()
            except Exception:
                pass


async def verified_profile_context(
    browser: Any,
    designation: dict[str, Any],
) -> tuple[Any, dict[str, Any]]:
    """Return the context whose observed profile path is the designation."""
    identity = await browser_profile_identity(
        browser,
        Path(str(designation["user_data_dir"])),
        str(designation["profile_directory"]),
    )
    if not identity.get("verified"):
        raise RuntimeError(
            "The live-debugging endpoint did not expose the designated "
            f"{designation['profile_directory']} context: "
            f"{identity.get('verification_error') or 'profile mismatch'}"
        )
    contexts = list(browser.contexts)
    context_index = int(identity.get("context_index") or 0)
    if context_index < 0 or context_index >= len(contexts):
        raise RuntimeError("The verified Edge profile context is no longer available")
    return contexts[context_index], identity


async def platform_authentication(
    context: Any,
    platform: str,
) -> dict[str, Any]:
    """Check login cookies in one specific browser context only."""
    if platform not in AUTH_COOKIE_RULES:
        raise RuntimeError(f"Unsupported social platform: {platform}")
    rule = AUTH_COOKIE_RULES[platform]
    try:
        cookies = await context.cookies(rule["urls"])
    except Exception:
        cookies = []
    cookie_names = {str(item.get("name") or "") for item in cookies}
    required_all = set(rule.get("required_all") or [])
    required_any = set(rule.get("required_any") or [])
    authenticated = required_all.issubset(cookie_names)
    if required_any:
        authenticated = authenticated and bool(required_any & cookie_names)
    return {
        "authenticated": authenticated,
        "auth_cookie_names_present": sorted(
            (required_all | required_any) & cookie_names
        ),
    }


def cdp_version(url: str, timeout: float = 1.5) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/json/version", timeout=timeout) as response:
            value = json.loads(response.read().decode("utf-8", errors="replace"))
            return value if isinstance(value, dict) else None
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return None


def wait_for_cdp(url: str, timeout: float) -> bool:
    deadline = time.monotonic() + max(0.1, timeout)
    while time.monotonic() < deadline:
        if cdp_version(url):
            return True
        time.sleep(0.25)
    return False


def devtools_active_port_path(user_data_dir: Path) -> Path:
    return user_data_dir / "DevToolsActivePort"


def tcp_port_reachable(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def read_live_debugging_endpoint(user_data_dir: Path) -> dict[str, Any]:
    active_file = devtools_active_port_path(user_data_dir)
    try:
        lines = active_file.read_text(encoding="ascii").splitlines()
        port = int(lines[0])
        browser_path = lines[1].strip()
    except (FileNotFoundError, OSError, ValueError, IndexError):
        return {}
    if port <= 0 or not browser_path.startswith("/devtools/browser/"):
        return {}
    return {
        "port": port,
        "cdp_url": f"ws://127.0.0.1:{port}{browser_path}",
        "active_port_file": str(active_file),
    }


def wait_for_live_debugging_endpoint(user_data_dir: Path, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + max(1.0, timeout)
    while time.monotonic() < deadline:
        endpoint = read_live_debugging_endpoint(user_data_dir)
        if endpoint and tcp_port_reachable("127.0.0.1", int(endpoint["port"])):
            return endpoint
        time.sleep(0.25)
    return {}


def edge_bridge_arguments(
    command: str,
    profile_name: str,
    timeout: float,
    window_handle: int = 0,
    close_window: bool = False,
) -> list[str]:
    if not LIVE_DEBUG_HELPER.is_file():
        raise RuntimeError(f"Edge live-debugging helper is missing: {LIVE_DEBUG_HELPER}")
    arguments = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(LIVE_DEBUG_HELPER),
        "-Command",
        command,
        "-ProfileDisplayName",
        profile_name,
        "-TimeoutSeconds",
        str(max(1, int(timeout))),
    ]
    if window_handle:
        arguments.extend(["-WindowHandle", str(window_handle)])
    if close_window:
        arguments.append("-CloseWindow")
    return arguments


def run_edge_bridge(
    command: str,
    profile_name: str,
    timeout: float,
    window_handle: int = 0,
    close_window: bool = False,
) -> dict[str, Any]:
    completed = subprocess.run(
        edge_bridge_arguments(
            command,
            profile_name,
            timeout,
            window_handle=window_handle,
            close_window=close_window,
        ),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=max(5.0, timeout + 5.0),
        check=False,
    )
    output_lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    payload: dict[str, Any] = {}
    if output_lines:
        try:
            value = json.loads(output_lines[-1])
            payload = value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            payload = {}
    if completed.returncode != 0 or not payload.get("success"):
        detail = str(payload.get("action") or completed.stderr.strip() or "unknown error")
        raise RuntimeError(f"Edge live-debugging {command} failed: {detail}")
    return payload


def start_edge_bridge_watcher(
    profile_name: str,
    window_handle: int,
) -> subprocess.Popen[bytes]:
    watcher = subprocess.Popen(
        edge_bridge_arguments(
            "watch",
            profile_name,
            86400,
            window_handle=window_handle,
        ),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=(
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        ),
    )
    _OWNED_EDGE_BRIDGE_WATCHERS[watcher.pid] = watcher
    return watcher


def process_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        process_query_limited_information = 0x1000
        still_active = 259
        handle = ctypes.windll.kernel32.OpenProcess(
            process_query_limited_information,
            False,
            pid,
        )
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not ctypes.windll.kernel32.GetExitCodeProcess(
                handle,
                ctypes.byref(exit_code),
            ):
                return False
            return exit_code.value == still_active
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def process_command_line(pid: int) -> str:
    if pid <= 0:
        return ""
    script = (
        f"$p=Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\" "
        "-ErrorAction SilentlyContinue; if($p){[Console]::Write($p.CommandLine)}"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else ""


def stop_verified_process_tree(pid: int, markers: list[str], label: str) -> None:
    if not process_is_running(pid):
        return
    command_line = process_command_line(pid)
    if not all(marker.casefold() in command_line.casefold() for marker in markers):
        raise RuntimeError(f"Refusing to stop PID {pid}: it is not the {label}")
    completed = subprocess.run(
        ["taskkill.exe", "/PID", str(pid), "/T", "/F"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=15,
        check=False,
    )
    for _ in range(20):
        if not process_is_running(pid):
            return
        time.sleep(0.1)
    raise RuntimeError(
        f"Failed to stop the {label} process tree for PID {pid} "
        f"(taskkill exit {completed.returncode})"
    )


def stop_edge_bridge_watcher(pid: int) -> None:
    try:
        stop_verified_process_tree(
            pid,
            [str(LIVE_DEBUG_HELPER), "-Command watch"],
            "Profile 7 consent watcher",
        )
    except Exception:
        # Preserve ownership if process identity could not be verified or the
        # stop failed. A later status check can still safely reconcile it.
        raise
    else:
        _OWNED_EDGE_BRIDGE_WATCHERS.pop(pid, None)


def edge_bridge_watcher_is_running(pid: int) -> bool:
    """Return true only for a watcher launched by this workspace copy."""
    owned = _OWNED_EDGE_BRIDGE_WATCHERS.get(pid)
    if owned is not None and owned.poll() is not None:
        _OWNED_EDGE_BRIDGE_WATCHERS.pop(pid, None)
        return False
    if not process_is_running(pid):
        _OWNED_EDGE_BRIDGE_WATCHERS.pop(pid, None)
        return False
    command_line = process_command_line(pid).casefold()
    return (
        str(LIVE_DEBUG_HELPER).casefold() in command_line
        and "-command watch" in command_line
    )


def wait_for_edge_bridge_watcher(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + max(0.1, timeout)
    while time.monotonic() < deadline:
        if edge_bridge_watcher_is_running(pid):
            return True
        if not process_is_running(pid):
            return False
        time.sleep(0.1)
    return False


async def browser_status(
    url: str,
    open_missing_tabs: bool = False,
    expected_profile: Path | None = None,
    expected_profile_directory: str = DEFAULT_PROFILE_DIRECTORY,
    verified_profile_identity: dict[str, Any] | None = None,
    connect_timeout_ms: float = 30000,
) -> dict[str, Any]:
    from playwright.async_api import async_playwright

    result: dict[str, Any] = {
        "cdp_url": url,
        "reachable": False,
        "platforms": {},
    }
    async with async_playwright() as playwright:
        browser = await playwright.chromium.connect_over_cdp(
            url,
            timeout=connect_timeout_ms,
        )
        result["reachable"] = True
        if verified_profile_identity is not None:
            result["profile"] = verified_profile_identity
        elif expected_profile is not None:
            result["profile"] = await browser_profile_identity(
                browser,
                expected_profile,
                expected_profile_directory,
            )
        contexts = list(browser.contexts)
        if not contexts:
            contexts = [await browser.new_context()]
        profile_result = result.get("profile") or {}
        if expected_profile is not None and not profile_result.get("verified"):
            checked_contexts: list[Any] = []
        elif profile_result.get("verified") and "context_index" in profile_result:
            context_index = int(profile_result.get("context_index") or 0)
            checked_contexts = (
                [contexts[context_index]]
                if 0 <= context_index < len(contexts)
                else []
            )
        else:
            checked_contexts = contexts
        context = checked_contexts[0] if checked_contexts else None

        if open_missing_tabs and context is not None:
            existing_hosts = {
                (urlparse(page.url).hostname or "").lower()
                for page in context.pages
                if page.url
            }
            pending_navigations = []
            for target in PLATFORM_URLS.values():
                host = (urlparse(target).hostname or "").lower()
                if host not in existing_hosts:
                    page = await context.new_page()
                    pending_navigations.append(
                        page.goto(target, wait_until="domcontentloaded", timeout=30000)
                    )
                    existing_hosts.add(host)
            if pending_navigations:
                await asyncio.gather(*pending_navigations, return_exceptions=True)

        for platform in AUTH_COOKIE_RULES:
            platform_results = [
                await platform_authentication(current_context, platform)
                for current_context in checked_contexts
            ]
            authenticated = next(
                (
                    item
                    for item in platform_results
                    if item.get("authenticated")
                ),
                {"authenticated": False, "auth_cookie_names_present": []},
            )
            result["platforms"][platform] = authenticated

        result["page_count"] = sum(
            len(current_context.pages) for current_context in checked_contexts
        )
    return result


async def close_browser(url: str) -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.connect_over_cdp(url)
        await browser.close()


def unverified_profile_identity(
    designation: dict[str, Any],
    error: str,
) -> dict[str, Any]:
    user_data = Path(str(designation["user_data_dir"])).resolve()
    profile_directory = str(designation["profile_directory"])
    return {
        "verified": False,
        "expected_user_data_path": str(user_data),
        "observed_user_data_path": "",
        "expected_profile_path": str(
            (user_data / profile_directory).resolve()
        ),
        "observed_profile_path": "",
        "expected_profile_directory": profile_directory,
        "observed_profile_directory": "",
        "verification_error": error,
    }


def start_existing_profile(
    args: argparse.Namespace,
    runtime_dir: Path,
    designation: dict[str, Any],
) -> int:
    from social_browser_startup_guard import startup_guard
    with startup_guard(runtime_dir, timeout=max(30, args.startup_timeout)):
        return _start_existing_profile_locked(args, runtime_dir, designation)


def _start_existing_profile_locked(
    args: argparse.Namespace, runtime_dir: Path, designation: dict[str, Any]
) -> int:
    profile = Path(str(designation["user_data_dir"])).absolute()
    profile_directory = str(designation["profile_directory"])
    display_name = str(
        designation.get("profile_display_name")
        or profile_display_name(profile, profile_directory)
    )
    if canonical_path(profile) != canonical_path(edge_default_user_data_dir()):
        raise RuntimeError(
            "Existing-profile attachment is reserved for Edge's real user-data root; "
            f"received {profile}"
        )

    state_file = state_path(runtime_dir)
    state = read_json(state_file)
    bridge_timeout = max(30.0, args.startup_timeout)
    connect_timeout_ms = int(min(120.0, bridge_timeout) * 1000)
    current_watcher = int(state.get("watcher_pid") or 0)
    current_url = str(state.get("cdp_url") or "")
    if state and (current_watcher or current_url):
        if not state_matches_designation(state, designation):
            raise RuntimeError(
                "Recorded social-browser state does not belong to the designated "
                "Edge Profile 7. It will not be reused or stopped automatically."
            )
    if (
        state.get("mode") == PROFILE_MODE_EXISTING
        and current_url.startswith("ws://")
        and edge_bridge_watcher_is_running(current_watcher)
    ):
        try:
            payload = asyncio.run(
                browser_status(
                    current_url,
                    open_missing_tabs=args.open_tabs,
                    expected_profile=profile,
                    expected_profile_directory=profile_directory,
                    connect_timeout_ms=connect_timeout_ms,
                )
            )
            if not (payload.get("profile") or {}).get("verified"):
                raise RuntimeError(
                    "The existing live-debugging endpoint is not Edge Profile 7"
                )
            print(
                "[INFO] Existing Profile 7 social browser is already running at "
                f"{display_cdp_url(current_url)}"
            )
            print_status(payload, args.json)
            return 0
        except Exception:
            stop_edge_bridge_watcher(current_watcher)

    if edge_bridge_watcher_is_running(current_watcher):
        stop_edge_bridge_watcher(current_watcher)

    executable = edge_executable()

    # Profile 7 and DevToolsActivePort are machine-global even though this
    # workspace keeps its own fail-closed state. If another trusted launcher
    # already enabled the real profile, verify and adopt that endpoint instead
    # of opening or toggling another Edge window.
    shared_endpoint = read_live_debugging_endpoint(profile)
    if (
        shared_endpoint
        and tcp_port_reachable("127.0.0.1", int(shared_endpoint["port"]))
    ):
        watcher = start_edge_bridge_watcher(display_name, 0)
        try:
            payload = asyncio.run(
                browser_status(
                    str(shared_endpoint["cdp_url"]),
                    open_missing_tabs=args.open_tabs,
                    expected_profile=profile,
                    expected_profile_directory=profile_directory,
                    connect_timeout_ms=connect_timeout_ms,
                )
            )
            if not (payload.get("profile") or {}).get("verified"):
                raise RuntimeError(
                    "The existing live-debugging endpoint is not Edge Profile 7"
                )
            if not wait_for_edge_bridge_watcher(watcher.pid):
                raise RuntimeError(
                    "The Profile 7 consent watcher exited during endpoint adoption"
                )
            write_json(state_file, {
                "schema_version": 5,
                "mode": PROFILE_MODE_EXISTING,
                "cdp_url": shared_endpoint["cdp_url"],
                "port": shared_endpoint["port"],
                "pid": 0,
                "watcher_pid": watcher.pid,
                "bridge_origin": "adopted_existing",
                "profile_path": str(profile),
                "profile_directory": profile_directory,
                "profile_display_name": display_name,
                "profile_designation_id": designation["designation_id"],
                "profile_verified": True,
                "profile_window_handle": 0,
                "active_port_file": shared_endpoint["active_port_file"],
                "edge_executable": str(executable),
                "started_at": utc_now(),
            })
        except Exception:
            if watcher.poll() is None:
                stop_edge_bridge_watcher(watcher.pid)
            raise
        print(
            "[INFO] Existing Edge Profile 7 endpoint adopted at "
            f"{display_cdp_url(str(shared_endpoint['cdp_url']))}"
        )
        print(f"[INFO] Profile directory: {profile / profile_directory}")
        print_status(payload, args.json)
        return 0

    # Never reuse a saved HWND after its endpoint/watcher failed. Windows can
    # recycle handles, so recovery launches Profile 7 and lets the helper find
    # the newly visible exact-profile window.
    launch_command = [
        str(executable),
        f"--profile-directory={profile_directory}",
        "--new-window",
        "--no-first-run",
        "--no-default-browser-check",
        "about:blank",
    ]
    subprocess.Popen(
        launch_command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    bridge = run_edge_bridge("enable", display_name, bridge_timeout)
    window_handle = int(bridge.get("window_handle") or 0)
    edge_pid = int(bridge.get("process_id") or 0)
    endpoint = wait_for_live_debugging_endpoint(profile, bridge_timeout)
    if not endpoint:
        raise RuntimeError(
            "Edge's remote-debugging control reported enabled, but "
            f"{devtools_active_port_path(profile)} did not expose a reachable "
            "endpoint. No shared Edge setting was changed during cleanup."
        )

    watcher = start_edge_bridge_watcher(display_name, window_handle)
    try:
        payload = asyncio.run(
            browser_status(
                str(endpoint["cdp_url"]),
                open_missing_tabs=args.open_tabs,
                expected_profile=profile,
                expected_profile_directory=profile_directory,
                connect_timeout_ms=connect_timeout_ms,
            )
        )
        if not (payload.get("profile") or {}).get("verified"):
            raise RuntimeError(
                "The enabled live-debugging endpoint is not Edge Profile 7"
            )
        if not wait_for_edge_bridge_watcher(watcher.pid):
            raise RuntimeError(
                "The Profile 7 consent watcher exited during browser verification"
            )
        write_json(state_file, {
            "schema_version": 5,
            "mode": PROFILE_MODE_EXISTING,
            "cdp_url": endpoint["cdp_url"],
            "port": endpoint["port"],
            "pid": edge_pid,
            "watcher_pid": watcher.pid,
            "bridge_origin": "enabled_here",
            "profile_path": str(profile),
            "profile_directory": profile_directory,
            "profile_display_name": display_name,
            "profile_designation_id": designation["designation_id"],
            "profile_verified": True,
            "profile_window_handle": window_handle,
            "active_port_file": endpoint["active_port_file"],
            "edge_executable": str(executable),
            "started_at": utc_now(),
        })
    except Exception:
        if watcher.poll() is None:
            stop_edge_bridge_watcher(watcher.pid)
        raise
    print(
        "[INFO] Existing Edge Profile 7 attached at "
        f"{display_cdp_url(str(endpoint['cdp_url']))}"
    )
    print(f"[INFO] Profile directory: {profile / profile_directory}")
    print_status(payload, args.json)
    return 0


def load_verified_profile7_state(
    runtime_dir: Path,
    designation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Read fail-closed runtime state for the attached Edge Profile 7."""
    designation = designation or load_engage_profile7_designation(runtime_dir)
    state = read_json(state_path(runtime_dir))
    if not state:
        raise RuntimeError("Edge Profile 7 social-browser state is missing")
    if not state_matches_designation(state, designation):
        raise RuntimeError(
            "The active social-browser state is not verified as Edge Profile 7"
        )
    cdp_endpoint = str(state.get("cdp_url") or "")
    parsed = urlparse(cdp_endpoint)
    if parsed.scheme != "ws" or parsed.hostname != "127.0.0.1":
        raise RuntimeError(
            "Edge Profile 7 has no verified local live-debugging endpoint"
        )
    watcher_pid = int(state.get("watcher_pid") or 0)
    if not edge_bridge_watcher_is_running(watcher_pid):
        raise RuntimeError("The Edge Profile 7 live-debugging watcher is not running")
    return state


def ensure_engage_profile7_browser(
    runtime_dir: Path,
    *,
    startup_timeout: float = 120.0,
    open_tabs: bool = False,
) -> dict[str, Any]:
    """Start or reuse the real Edge Profile 7 and return verified state."""
    runtime_dir = Path(runtime_dir).resolve()
    runtime_dir.mkdir(parents=True, exist_ok=True)
    designation = load_engage_profile7_designation(runtime_dir)
    existing_state = read_json(state_path(runtime_dir))
    if existing_state and not state_matches_designation(
        existing_state,
        designation,
    ):
        raise RuntimeError(
            "Existing social-browser state belongs to another profile. "
            "ENGAGE refuses to replace or attach to it."
        )
    args = argparse.Namespace(
        runtime_dir=str(runtime_dir),
        startup_timeout=max(30.0, float(startup_timeout)),
        open_tabs=bool(open_tabs),
        json=False,
    )
    result = start_existing_profile(args, runtime_dir, designation)
    if result != 0:
        raise RuntimeError("Edge Profile 7 could not be started or reused")
    state = load_verified_profile7_state(runtime_dir, designation)
    return {
        "designation": designation,
        "state": state,
    }


def start_managed_profile(args: argparse.Namespace) -> int:
    runtime_dir = Path(args.runtime_dir).resolve()
    runtime_dir.mkdir(parents=True, exist_ok=True)
    designation = load_designated_profile(runtime_dir)
    profile = Path(designation["user_data_dir"]).absolute()
    profile_directory = str(designation["profile_directory"])
    state_file = state_path(runtime_dir)
    url = cdp_url(args.port)

    current = cdp_version(url)
    if current:
        payload = asyncio.run(
            browser_status(
                url,
                open_missing_tabs=args.open_tabs,
                expected_profile=profile,
                expected_profile_directory=profile_directory,
            )
        )
        if (payload.get("profile") or {}).get("verified"):
            print(
                "[INFO] Social browser is already running at "
                f"{display_cdp_url(url)}"
            )
            print_status(payload, args.json)
            return 0
        raise RuntimeError(
            f"Port {args.port} hosts a DevTools browser that is not the designated "
            f"social profile at {profile}. Stop that browser or choose another port."
        )

    executable = edge_executable()
    command = [
        str(executable),
        f"--remote-debugging-port={args.port}",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={profile}",
        f"--profile-directory={profile_directory}",
        "--enable-automation",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if args.open_tabs:
        command.extend(PLATFORM_URLS.values())

    creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creation_flags,
    )
    if not wait_for_cdp(url, args.startup_timeout):
        if process.poll() is None:
            process.terminate()
        raise RuntimeError(f"Edge started as PID {process.pid}, but CDP did not become ready at {url}")

    payload = asyncio.run(
        browser_status(
            url,
            open_missing_tabs=args.open_tabs,
            expected_profile=profile,
            expected_profile_directory=profile_directory,
        )
    )
    if not (payload.get("profile") or {}).get("verified"):
        try:
            asyncio.run(close_browser(url))
        except Exception:
            if process.poll() is None:
                process.terminate()
        raise RuntimeError(
            "Edge started, but CDP could not verify the designated social-browser "
            f"profile at {profile}"
        )

    write_json(state_file, {
        "schema_version": 2,
        "cdp_url": url,
        "port": args.port,
        "pid": process.pid,
        "profile_path": str(profile),
        "profile_directory": profile_directory,
        "profile_designation_id": designation["designation_id"],
        "profile_verified": True,
        "edge_executable": str(executable),
        "started_at": utc_now(),
    })
    print(
        "[INFO] Dedicated social browser started at "
        f"{display_cdp_url(url)}"
    )
    print(f"[INFO] Isolated profile: {profile}")
    print_status(payload, args.json)
    return 0


def start(args: argparse.Namespace) -> int:
    runtime_dir = Path(args.runtime_dir).resolve()
    runtime_dir.mkdir(parents=True, exist_ok=True)
    designation = load_engage_profile7_designation(runtime_dir)
    return start_existing_profile(args, runtime_dir, designation)


def status(args: argparse.Namespace) -> int:
    runtime_dir = Path(args.runtime_dir).resolve()
    state = read_json(state_path(runtime_dir))
    designation = load_engage_profile7_designation(runtime_dir)
    if args.cdp_url:
        raise RuntimeError(
            "ENGAGE status does not accept an arbitrary CDP URL; it verifies "
            "the recorded Edge Profile 7 endpoint only"
        )
    if state and not state_matches_designation(state, designation):
        raise RuntimeError(
            "Recorded social-browser state does not belong to Edge Profile 7"
        )
    profile = Path(designation["user_data_dir"]).absolute()
    profile_directory = str(designation["profile_directory"])
    url = str(state.get("cdp_url") or cdp_url(args.port))
    if designation.get("mode") == PROFILE_MODE_EXISTING:
        watcher_pid = int(state.get("watcher_pid") or 0)
        if (
            state.get("mode") != PROFILE_MODE_EXISTING
            or not url.startswith("ws://")
            or not edge_bridge_watcher_is_running(watcher_pid)
        ):
            payload = {
                "cdp_url": url,
                "reachable": False,
                "platforms": {},
                "profile": unverified_profile_identity(
                    designation,
                    "profile7_bridge_unreachable",
                ),
            }
            print_status(payload, args.json)
            return 1
        try:
            payload = asyncio.run(
                browser_status(
                    url,
                    open_missing_tabs=args.open_tabs,
                    expected_profile=profile,
                    expected_profile_directory=profile_directory,
                    connect_timeout_ms=120000,
                )
            )
        except Exception as exc:
            payload = {
                "cdp_url": url,
                "reachable": False,
                "platforms": {},
                "profile": unverified_profile_identity(
                    designation,
                    f"profile7_cdp_unreachable: {exc}",
                ),
            }
            print_status(payload, args.json)
            return 1
        print_status(payload, args.json)
        return 0 if (payload.get("profile") or {}).get("verified") else 2

    if not cdp_version(url):
        payload = {
            "cdp_url": url,
            "reachable": False,
            "platforms": {},
            "profile": {
                "verified": False,
                "expected_profile_path": str(profile.resolve()),
                "expected_profile_directory": profile_directory,
                "verification_error": "cdp_unreachable",
            },
        }
        print_status(payload, args.json)
        return 1
    payload = asyncio.run(
        browser_status(
            url,
            open_missing_tabs=args.open_tabs,
            expected_profile=profile,
            expected_profile_directory=profile_directory,
        )
    )
    print_status(payload, args.json)
    return 0 if (payload.get("profile") or {}).get("verified") else 2


def stop(args: argparse.Namespace) -> int:
    runtime_dir = Path(args.runtime_dir).resolve()
    state_file = state_path(runtime_dir)
    state = read_json(state_file)
    designation = load_engage_profile7_designation(runtime_dir)
    if args.cdp_url:
        raise RuntimeError(
            "ENGAGE stop does not accept an arbitrary CDP URL; it can stop "
            "only the recorded Edge Profile 7 connection"
        )
    if state and not state_matches_designation(state, designation):
        raise RuntimeError(
            "Refusing to stop a social-browser state that is not Edge Profile 7"
        )
    profile = Path(designation["user_data_dir"]).absolute()
    profile_directory = str(designation["profile_directory"])
    url = str(state.get("cdp_url") or cdp_url(args.port))
    if designation.get("mode") == PROFILE_MODE_EXISTING:
        if state.get("mode") != PROFILE_MODE_EXISTING:
            print("[INFO] No active Profile 7 social-browser connection was recorded")
            return 0
        watcher_pid = int(state.get("watcher_pid") or 0)
        if edge_bridge_watcher_is_running(watcher_pid):
            stop_edge_bridge_watcher(watcher_pid)
        state_file.unlink(missing_ok=True)
        print(
            "[INFO] Profile 7 social-browser connection detached: "
            f"{display_cdp_url(url)}"
        )
        print("[INFO] Edge Profile 7 and its other controllers were left intact")
        return 0

    if not cdp_version(url):
        print(
            "[INFO] No social browser is reachable at "
            f"{display_cdp_url(url)}"
        )
        state_file.unlink(missing_ok=True)
        return 0
    payload = asyncio.run(
        browser_status(
            url,
            expected_profile=profile,
            expected_profile_directory=profile_directory,
        )
    )
    if not (payload.get("profile") or {}).get("verified"):
        raise RuntimeError(
            f"Refusing to stop the browser at {url}: it is not using the designated "
            f"social profile at {profile}"
        )
    asyncio.run(close_browser(url))
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and cdp_version(url, timeout=0.5):
        time.sleep(0.25)
    state_file.unlink(missing_ok=True)
    print(
        "[INFO] Dedicated social browser stopped: "
        f"{display_cdp_url(url)}"
    )
    return 0


def configure(args: argparse.Namespace) -> int:
    runtime_dir = Path(args.runtime_dir).resolve()
    runtime_dir.mkdir(parents=True, exist_ok=True)
    state = read_json(state_path(runtime_dir))
    watcher_pid = int(state.get("watcher_pid") or 0)
    if state.get("mode") == PROFILE_MODE_EXISTING and process_is_running(watcher_pid):
        raise RuntimeError(
            "Stop the active Profile 7 social browser before changing its designation"
        )
    url = cdp_url(args.port)
    if cdp_version(url):
        raise RuntimeError(
            f"Stop the social browser at {url} before changing its designated profile"
        )
    if not args.user_data_dir or not args.profile_directory:
        raise RuntimeError(
            "configure requires --user-data-dir and --profile-directory"
        )
    designation = configure_designated_profile(
        runtime_dir,
        Path(args.user_data_dir),
        args.profile_directory,
        mode=args.profile_mode,
    )
    if args.json:
        print(json.dumps(designation, indent=2, ensure_ascii=True))
    else:
        print("[INFO] Designated social-browser Edge profile configured")
        print(f"[INFO] User data: {designation['user_data_dir']}")
        print(f"[INFO] Profile directory: {designation['profile_directory']}")
        print(f"[INFO] Profile mode: {designation['mode']}")
    return 0


def print_status(payload: dict[str, Any], as_json: bool) -> None:
    safe_payload = dict(payload)
    safe_payload["cdp_url"] = display_cdp_url(str(payload.get("cdp_url") or ""))
    if as_json:
        print(json.dumps(safe_payload, indent=2, ensure_ascii=True))
        return
    reachable = bool(payload.get("reachable"))
    print(
        f"[STATUS] CDP reachable: {'yes' if reachable else 'no'} "
        f"({safe_payload['cdp_url']})"
    )
    if not reachable:
        return
    profile = payload.get("profile") or {}
    profile_label = "verified" if profile.get("verified") else "NOT VERIFIED"
    print(
        f"[STATUS] Designated profile: {profile_label} "
        f"({profile.get('expected_profile_path', '')})"
    )
    for platform in AUTH_COOKIE_RULES:
        item = (payload.get("platforms") or {}).get(platform) or {}
        label = "logged in" if item.get("authenticated") else "login needed"
        names = ", ".join(item.get("auth_cookie_names_present") or []) or "none"
        print(f"[STATUS] {platform}: {label} (auth cookies present: {names})")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Manage the authenticated Edge session used for social API capture."
    )
    parser.add_argument(
        "command",
        choices=["configure", "start", "status", "stop"],
        help="Start, inspect, or stop the dedicated social browser.",
    )
    parser.add_argument("--runtime-dir", default=str(DEFAULT_RUNTIME_DIR))
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--cdp-url", default="")
    parser.add_argument("--startup-timeout", type=float, default=30.0)
    parser.add_argument("--open-tabs", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--user-data-dir", default="")
    parser.add_argument("--profile-directory", default="")
    parser.add_argument(
        "--profile-mode",
        choices=["", PROFILE_MODE_MANAGED, PROFILE_MODE_EXISTING],
        default="",
    )
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "configure":
        return configure(args)
    if args.command == "start":
        return start(args)
    if args.command == "status":
        return status(args)
    return stop(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        raise SystemExit(1)
