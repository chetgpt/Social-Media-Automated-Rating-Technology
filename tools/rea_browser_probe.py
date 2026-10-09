"""Run an isolated REA probe against an existing verified Profile 7 connection."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import social_browser as sb


REQUIRED_PYTHON = Path(r"C:\Users\DELL\AppData\Local\Programs\Python\Python311\python.exe")
MAX_RUNTIME_BYTES = 64 * 1024
MAX_SUMMARY_BYTES = 8 * 1024
COLLECTOR_SCRIPTS = {
    "engage_tiktok.py",
    "music_audit_operator.py",
    "posts_discovery.py",
    "music_audit_topics.py",
    "linkedin_engage.py",
    "threads_workflow.py",
    "engage_social.py",
    "music_backfill_tiktok.py",
    "sonic_audit_tiktok.py",
    "audio_archive_tiktok.py",
    "run_audio_archive_batch.py",
    "tiktok_one_top_content.py",
    "music_discovery.py",
    "quick_audit_tiktok.py",
    "social_browser.py",
}
SAFE_NODE_ERRORS = {
    "unsupported_rea_version", "invalid_profile_endpoint", "invalid_platform",
    "capture_budget_exceeded", "cdp_connection_failed", "invalid_cdp_message",
    "probe_deadline_exceeded", "cdp_discovery_failed", "no_existing_platform_tab",
    "platform_login_or_challenge", "target_scope_changed", "local_bridge_failed",
    "cdp_command_denied", "bridge_command_failed", "rea_inspection_failed",
    "unexpected_rea_result", "probe_failed",
}
NODE_ENV_KEYS = {"path", "systemroot", "temp", "tmp", "comspec", "pathext", "localappdata", "appdata"}


class ProbeError(RuntimeError):
    """A canned error suitable for the public probe summary."""


def runtime_snapshot(runtime_dir: Path) -> tuple[bytes | None, bytes | None]:
    snapshots = []
    for path in (sb.designated_profile_path(runtime_dir), sb.state_path(runtime_dir)):
        try:
            with path.open("rb") as source:
                content = source.read(MAX_RUNTIME_BYTES + 1)
        except FileNotFoundError:
            content = None
        except OSError:
            raise ProbeError("runtime_snapshot_unavailable") from None
        if content is not None and len(content) > MAX_RUNTIME_BYTES:
            raise ProbeError("runtime_snapshot_too_large")
        snapshots.append(content)
    return snapshots[0], snapshots[1]


def require_idle_collectors() -> None:
    try:
        import psutil
    except ImportError:
        raise ProbeError("collector_check_unavailable") from None
    try:
        for process in psutil.process_iter(["pid", "name"]):
            if process.info["pid"] == os.getpid():
                continue
            name = str(process.info.get("name") or "").casefold()
            if not (name.startswith("python") or name in {"py", "py.exe"}):
                continue
            try:
                arguments = process.cmdline()
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
            except psutil.AccessDenied:
                raise ProbeError("collector_check_unavailable") from None
            if not arguments:
                raise ProbeError("collector_check_unavailable")
            scripts = {Path(arg.replace("\\", "/")).name.casefold() for arg in arguments}
            if scripts & COLLECTOR_SCRIPTS:
                raise ProbeError("collector_busy")
    except ProbeError:
        raise
    except Exception:
        raise ProbeError("collector_check_unavailable") from None


def existing_state(runtime_dir: Path, snapshot: tuple[bytes | None, bytes | None]) -> dict:
    try:
        if snapshot[0] is None or snapshot[1] is None:
            raise ProbeError("existing_connection_required")
        designation = json.loads(snapshot[0].decode("utf-8"))
        if not isinstance(designation, dict) or not designation:
            raise ProbeError("profile_designation_invalid")
        user_data_dir = Path(str(designation.get("user_data_dir") or ""))
        profile_directory = str(designation.get("profile_directory") or "")
        if (
            designation.get("mode") != sb.PROFILE_MODE_EXISTING
            or sb.canonical_path(user_data_dir) != sb.canonical_path(sb.edge_default_user_data_dir())
            or profile_directory.casefold() != sb.ENGAGE_PROFILE_DIRECTORY.casefold()
            or not str(designation.get("designation_id") or "")
            or not (user_data_dir / profile_directory).is_dir()
        ):
            raise ProbeError("profile_designation_invalid")
        state = sb.load_verified_profile7_state(runtime_dir, designation)
        return {"designation": designation, "state": state}
    except ProbeError:
        raise
    except Exception:
        raise ProbeError("existing_connection_unverified") from None


async def verify_live_profile(connection: dict) -> None:
    try:
        from playwright.async_api import async_playwright

        require_idle_collectors()
        async with async_playwright() as driver:
            browser = await driver.chromium.connect_over_cdp(
                connection["state"]["cdp_url"], timeout=15000
            )
            require_idle_collectors()
            await sb.verified_profile_context(browser, connection["designation"])
            # Exiting Playwright stops this driver and disconnects; never close Edge.
    except ProbeError:
        raise
    except Exception:
        raise ProbeError("live_profile_verification_failed") from None


def run_node_probe(node: str, args: argparse.Namespace, endpoint: str) -> dict:
    helper = Path(__file__).with_suffix(".mjs")
    rea_package = Path(args.rea_package).resolve()
    if not helper.is_file() or not (rea_package / "package.json").is_file():
        raise ProbeError("probe_package_unavailable")
    require_idle_collectors()
    payload = {"endpoint": endpoint, "platform": args.platform, "reaPackage": str(rea_package)}
    try:
        result = subprocess.run(
            [node, str(helper)],
            input=json.dumps(payload),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=45,
            cwd=ROOT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            env={key: value for key, value in os.environ.items() if key.casefold() in NODE_ENV_KEYS},
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise ProbeError("probe_timed_out") from None
    except Exception:
        raise ProbeError("probe_execution_failed") from None
    output = result.stdout.strip()
    session_path = urlparse(endpoint).path
    if (
        len(output.encode("utf-8")) > MAX_SUMMARY_BYTES
        or endpoint in output
        or (session_path and session_path in output)
    ):
        raise ProbeError("probe_summary_invalid")
    try:
        summary = json.loads(output)
    except (ValueError, TypeError):
        raise ProbeError("probe_summary_invalid") from None
    if not isinstance(summary, dict) or summary.get("status") not in {"ok", "error", "blocked"}:
        raise ProbeError("probe_summary_invalid")
    if result.returncode != 0 or summary["status"] != "ok":
        reason = summary.get("reason")
        raise ProbeError(reason if isinstance(reason, str) and reason in SAFE_NODE_ERRORS else "probe_failed")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=("threads", "linkedin", "tiktok"), default="threads")
    temp_dir = Path(os.environ.get("TEMP") or os.environ.get("TMP") or str(Path.home() / "AppData/Local/Temp"))
    parser.add_argument(
        "--rea-package", type=Path,
        default=temp_dir / "codex-rea-trial-6.0.0/node_modules/rea-agents",
        help="Existing isolated rea-agents package directory; no package is installed.",
    )
    parser.add_argument("--runtime-dir", type=Path, default=sb.DEFAULT_RUNTIME_DIR,
                        help="Existing Profile 7 social-browser runtime directory.")
    args = parser.parse_args(argv)
    runtime_dir = args.runtime_dir.resolve()
    before = None
    report = {"status": "blocked", "platform": args.platform, "runtime_unchanged": False}
    try:
        if sb.canonical_path(Path(sys.executable)) != sb.canonical_path(REQUIRED_PYTHON):
            raise ProbeError("required_python_interpreter")
        before = runtime_snapshot(runtime_dir)
        require_idle_collectors()
        connection = existing_state(runtime_dir, before)
        node = shutil.which("node")
        if not node:
            raise ProbeError("node_runtime_unavailable")
        asyncio.run(asyncio.wait_for(verify_live_profile(connection), timeout=20))
        if runtime_snapshot(runtime_dir) != before:
            raise ProbeError("runtime_state_changed")
        summary = run_node_probe(node, args, connection["state"]["cdp_url"])
        report.update(status="ok", profile_verified=True, probe=summary)
    except ProbeError as exc:
        report["error"] = str(exc)
    except Exception:
        report["error"] = "probe_verification_failed"
    finally:
        try:
            report["runtime_unchanged"] = before is not None and runtime_snapshot(runtime_dir) == before
        except Exception:
            report["runtime_unchanged"] = False
        if before is not None and not report["runtime_unchanged"]:
            report.pop("probe", None)
            report.update(status="blocked", error="runtime_state_changed")
    print(json.dumps(report, ensure_ascii=True))
    return 0 if report["status"] == "ok" and report["runtime_unchanged"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
