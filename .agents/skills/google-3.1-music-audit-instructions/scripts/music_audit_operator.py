#!/usr/bin/env python
"""Deterministic operator for canonical TikTok MUSIC AUDIT runs.

This script deliberately exposes only the canonical MUSIC AUDIT collection,
same-run resume, restart handoff, offline export, and verification surfaces.
It never launches Edge directly and accepts no passthrough browser flags.
"""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import hashlib
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import time
import unicodedata
import uuid
from contextlib import contextmanager
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence
from urllib.parse import urlsplit


HANDOFF_SCHEMA = "google-music-audit-operator-handoff-v2"
LEDGER_SCHEMA = "google-music-audit-operator-ledger-v1"
REVIEW_SCHEMA = "google-music-audit-review-v1"
EXPORT_SCHEMA = "tiktok-listen-evidence-export-v1"
LEGACY_PROJECT_LAYOUT_SCHEMA = "tiktok-music-audit-project-layout-v1"
PROJECT_LAYOUT_SCHEMA = "tiktok-music-audit-project-layout-v2"
EXPORT_KEYS = {
    "schema_version",
    "run_id",
    "project",
    "source_mode",
    "collection_policy",
    "post_id",
    "evidence_hash",
    "evidence_projection_hash",
    "evidence_packet",
}
REQUIRED_PYTHON = Path(
    r"C:\Users\DELL\AppData\Local\Programs\Python\Python311\python.exe"
)
RUN_ID_PATTERN = re.compile(r"^engage_[0-9a-f]{16}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
PROJECT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
ARTIFACT_STEM_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_]{0,39}$")
NEW_PROJECT_SLUG_MAX = 68
ARTIFACT_STEM_MAX = 40
RUN_LABEL_INPUT_MAX = 128
WINDOWS_LEGACY_PATH_MAX = 259
DIRECT_URL_PATTERN = re.compile(r"^/@[^/]+/(?:video|photo)/(?P<post_id>[0-9]+)/*$")
MUSIC_PLATFORM_TERMINAL = {
    "available",
    "partial",
    "not_provided",
    "unavailable",
}
MUSIC_CATALOG_TERMINAL = {
    "matched",
    "ambiguous",
    "not_found",
    "unsupported",
    "unavailable",
    "rate_limited",
    "provider_error",
}
TT2DSP_TERMINAL = {
    "resolved",
    "not_found",
    "unsupported",
    "unavailable",
    "rate_limited",
    "provider_error",
}
RESUME_ATTEMPTS_PER_EPOCH = 1
ZERO_STAGE_COUNTERS = (
    "analyzed",
    "drafted",
    "reviewed",
    "stored",
    "authorized",
    "published",
)
FORBIDDEN_ARG_FRAGMENTS = (
    "social_browser.py",
    "--headless",
    "--remote-debugging-port",
    "--remote-debugging-address",
    "--remote-allow-origins",
    "--user-data-dir",
    "devtoolsactiveport",
    "taskkill",
    "stop-process",
    "schtasks",
)
FORBIDDEN_EXPORT_KEYS = {
    "access_token",
    "approval_token",
    "authorization_header",
    "authorization_headers",
    "cookie",
    "cookies",
    "download_addr",
    "download_url",
    "embed_html",
    "headers",
    "media_authorization",
    "mstoken",
    "play_addr",
    "preview_url",
    "refresh_token",
    "session_token",
    "sessionid",
    "signed_audio_url",
    "signed_media_url",
    "websocket_url",
}
SAFE_EXPORT_URL_PATHS = {
    "evidence_packet.direct_source_validation.expected_url",
    "evidence_packet.direct_source_validation.observed_url",
}
AI_POST_FIELDS = (
    "analysis_hash",
    "analysis_actor",
    "analyzed_at",
    "draft_text",
    "draft_hash",
    "draft_actor",
    "drafted_at",
    "review_hash",
    "review_actor",
    "reviewed_at",
    "presentation_hash",
    "presentation_token_hash",
    "presented_to",
    "presented_at",
    "authorization_by",
    "authorized_at",
    "authorization_presentation_hash",
    "authorization_text_hash",
    "authorization_review_hash",
    "authorization_target_url",
    "authorization_content_key",
    "authorization_decision_hash",
    "authorization_analysis_hash",
    "authorization_expected_account",
    "handoff_hash",
    "handed_off_at",
)
HEARTBEAT_INTERVAL_SECONDS = 15.0
ACTIVE_EXECUTION_STATES = {"starting", "running", "finishing"}
RECOVERABLE_HANDOFF_TRANSITION_KEYS = {
    "confirmed_restart_count",
    "recovery_epoch",
    "restart_cancellations",
    "restart_pending",
    "restart_phase",
    "restart_reason",
    "state",
}


class OperatorError(RuntimeError):
    """A fail-closed operator or artifact validation error."""


@dataclass(frozen=True)
class OperatorPaths:
    workspace: Path
    python: Path
    engage_script: Path
    master_database: Path


@dataclass(frozen=True)
class MusicAuditProjectLayout:
    schema_version: str
    artifact_stem: str
    project_root: Path
    state_directory: Path
    database: Path
    handoff_file: Path
    ledger_file: Path
    export_file: Path
    review_json: Path
    review_markdown: Path


@dataclass(frozen=True)
class MusicAuditRunNaming:
    project: str
    artifact_stem: str
    run_label: str
    run_label_input: str
    run_descriptor: str


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    exit_code: int
    duration_ms: float
    payload: Mapping[str, Any]
    sanitized_error: str
    child_started: bool = True


def canonical_paths() -> OperatorPaths:
    workspace = Path(__file__).resolve().parents[4]
    return OperatorPaths(
        workspace=workspace,
        python=REQUIRED_PYTHON.resolve(),
        engage_script=(workspace / "engage_tiktok.py").resolve(),
        master_database=(
            workspace
            / "comments_data"
            / "tiktok_master"
            / "state"
            / "tiktok_master.sqlite"
        ).resolve(),
    )


def now_iso() -> str:
    return dt.datetime.now().astimezone().replace(microsecond=0).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def json_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def canonical_project_layout(
    paths: OperatorPaths,
    project: str,
    *,
    schema_version: str = LEGACY_PROJECT_LAYOUT_SCHEMA,
    artifact_stem: str = "",
) -> MusicAuditProjectLayout:
    """Return a versioned guarded MUSIC AUDIT project layout."""

    if not PROJECT_PATTERN.fullmatch(project):
        raise OperatorError("MUSIC AUDIT project slug is invalid")
    comments_root = (paths.workspace / "comments_data").resolve()
    if not is_within(comments_root, paths.workspace):
        raise OperatorError("comments_data escaped the MUSIC AUDIT workspace")
    project_root = (comments_root / f"project_{project}").resolve()
    if not is_within(project_root, comments_root):
        raise OperatorError("project root escaped comments_data")
    state_directory = project_root / "state"
    if schema_version == LEGACY_PROJECT_LAYOUT_SCHEMA:
        if artifact_stem:
            raise OperatorError("legacy MUSIC AUDIT layout cannot use an artifact stem")
        layout = MusicAuditProjectLayout(
            schema_version=schema_version,
            artifact_stem="",
            project_root=project_root,
            state_directory=state_directory,
            database=state_directory / "engage_state.sqlite",
            handoff_file=project_root / "music_audit_operator_handoff.json",
            ledger_file=project_root / "music_audit_operator_ledger.jsonl",
            export_file=project_root / "music_audit_evidence.jsonl",
            review_json=project_root / "music_audit_review.json",
            review_markdown=project_root / "log_file_music_audit_review.md",
        )
    elif schema_version == PROJECT_LAYOUT_SCHEMA:
        if not ARTIFACT_STEM_PATTERN.fullmatch(artifact_stem):
            raise OperatorError("MUSIC AUDIT artifact stem is invalid")
        layout = MusicAuditProjectLayout(
            schema_version=schema_version,
            artifact_stem=artifact_stem,
            project_root=project_root,
            state_directory=state_directory,
            database=state_directory / f"{artifact_stem}_state.sqlite",
            handoff_file=project_root / f"{artifact_stem}_handoff.json",
            ledger_file=project_root / f"{artifact_stem}_ledger.jsonl",
            export_file=project_root / f"{artifact_stem}_evidence.jsonl",
            review_json=project_root / f"{artifact_stem}_review.json",
            review_markdown=project_root / f"{artifact_stem}_review.md",
        )
    else:
        raise OperatorError("unsupported MUSIC AUDIT project layout schema")
    _validate_layout_path_budget(layout)
    return layout


def _validate_layout_path_budget(layout: MusicAuditProjectLayout) -> None:
    """Fail before creation when a Windows-safe canonical path would be too long."""

    export_temp = layout.export_file.with_name(
        f".{layout.export_file.name}.{'0' * 32}.tmp"
    )
    candidates = (
        layout.database,
        Path(str(layout.database) + "-journal"),
        Path(str(layout.database) + "-wal"),
        layout.handoff_file,
        layout.ledger_file,
        layout.export_file,
        export_temp,
        layout.review_json,
        layout.review_markdown,
    )
    longest = max(candidates, key=lambda value: len(str(value)))
    if len(str(longest)) > WINDOWS_LEGACY_PATH_MAX:
        raise OperatorError(
            "MUSIC AUDIT output path exceeds the Windows-safe path budget: "
            f"{longest}"
        )


def _legacy_project_layout(paths: OperatorPaths, project: str) -> MusicAuditProjectLayout:
    return canonical_project_layout(
        paths,
        project,
        schema_version=LEGACY_PROJECT_LAYOUT_SCHEMA,
    )


def _semantic_project_layout(
    paths: OperatorPaths,
    project: str,
    artifact_stem: str,
) -> MusicAuditProjectLayout:
    return canonical_project_layout(
        paths,
        project,
        schema_version=PROJECT_LAYOUT_SCHEMA,
        artifact_stem=artifact_stem,
    )


def output_layout_summary(
    handoff: Mapping[str, Any],
    *,
    evidence_export_status: str,
) -> dict[str, Any]:
    """Expose canonical paths without inventing a second result manifest."""

    return {
        "schema_version": str(
            handoff.get("project_layout_schema") or LEGACY_PROJECT_LAYOUT_SCHEMA
        ),
        "artifact_stem": str(handoff.get("artifact_stem") or ""),
        "project_directory": str(handoff["project_root"]),
        "workflow_database": str(handoff["database"]),
        "handoff": str(handoff["handoff_file"]),
        "operator_ledger": str(handoff["ledger_file"]),
        "evidence_export": str(handoff["export_file"]),
        "evidence_export_status": evidence_export_status,
        "machine_result": str(handoff["review_json"]),
        "review_log": str(handoff["review_markdown"]),
    }


def atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".ma_{os.getpid()}_{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(value, encoding="utf-8", newline="\n")
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def operator_lock_path(handoff: Mapping[str, Any]) -> Path:
    return Path(str(handoff["project_root"])).resolve() / ".music_audit_operator.lock"


@contextmanager
def acquire_start_registry_lock(paths: OperatorPaths) -> Iterator[None]:
    """Serialize unfinished-run discovery and new project creation."""

    if os.name != "nt":
        raise OperatorError("the MUSIC AUDIT start lock requires Windows")
    import msvcrt

    lock_path = (
        paths.workspace / "comments_data" / ".music_audit_start.lock"
    ).resolve()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    locked = False
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise OperatorError(
                "music_audit_start_active: wait for the existing start decision"
            ) from exc
        locked = True
        yield
    finally:
        if locked:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        handle.close()


@contextmanager
def acquire_operator_lock(handoff: Mapping[str, Any]) -> Iterator[None]:
    """Hold one nonblocking project-scoped writer lock for a full operation."""

    if os.name != "nt":
        raise OperatorError("the MUSIC AUDIT operator lock requires Windows")
    import msvcrt

    lock_path = operator_lock_path(handoff)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    locked = False
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise OperatorError(
                "operator_worker_active: wait for the running MUSIC AUDIT"
            ) from exc
        locked = True
        yield
    finally:
        if locked:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        handle.close()


def operator_lock_is_held(handoff: Mapping[str, Any]) -> bool:
    """Probe the writer lock without modifying workflow or handoff state."""

    lock_path = operator_lock_path(handoff)
    if not lock_path.exists():
        return False
    if lock_path.stat().st_size == 0:
        # A read-only probe must not initialize the file. Treat an existing
        # empty lock conservatively as unavailable.
        return True
    try:
        with acquire_operator_lock(handoff):
            return False
    except OperatorError as exc:
        if "operator_worker_active" in str(exc):
            return True
        raise


def process_identity(pid: int, *, role: str) -> dict[str, Any]:
    """Return the safe process identity needed to detect PID reuse."""

    try:
        import psutil

        process = psutil.Process(int(pid))
        return {
            "role": role,
            "pid": int(pid),
            "create_time": round(float(process.create_time()), 6),
            "executable": str(Path(process.exe()).resolve()),
        }
    except Exception as exc:
        raise OperatorError(f"cannot bind {role} process identity") from exc


def recorded_process_state(
    record: Mapping[str, Any] | None,
    *,
    paths: OperatorPaths,
) -> str:
    if not isinstance(record, Mapping) or int(record.get("pid") or 0) <= 0:
        return "dead"
    try:
        import psutil

        process = psutil.Process(int(record["pid"]))
        if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
            return "dead"
        if abs(float(process.create_time()) - float(record["create_time"])) > 0.01:
            return "dead"
        if Path(process.exe()).resolve() != paths.python.resolve():
            return "dead"
        if (
            Path(str(record.get("executable") or "")).resolve()
            != paths.python.resolve()
        ):
            return "dead"
        return "alive"
    except Exception as exc:
        try:
            import psutil

            if isinstance(exc, (psutil.NoSuchProcess, psutil.ZombieProcess)):
                return "dead"
        except Exception:
            pass
        return "unknown"


def recorded_process_alive(
    record: Mapping[str, Any] | None,
    *,
    paths: OperatorPaths,
) -> bool:
    """Compatibility boolean; uncertain identity fails closed as live."""

    return recorded_process_state(record, paths=paths) != "dead"


def execution_liveness(
    handoff: Mapping[str, Any],
    *,
    paths: OperatorPaths,
) -> dict[str, bool]:
    execution = handoff.get("execution")
    if not isinstance(execution, Mapping):
        execution = {}
    operator_state = recorded_process_state(
        execution.get("operator_process"), paths=paths
    )
    collector_state = recorded_process_state(
        execution.get("collector_process"), paths=paths
    )
    return {
        "operator_alive": operator_state == "alive",
        "collector_alive": collector_state == "alive",
        "inspection_uncertain": "unknown" in {operator_state, collector_state},
        "lock_held": operator_lock_is_held(handoff),
    }


def assert_no_live_execution(
    handoff: Mapping[str, Any],
    *,
    paths: OperatorPaths,
    include_lock: bool = True,
) -> None:
    live = execution_liveness(handoff, paths=paths)
    if not include_lock:
        live["lock_held"] = False
    if any(live.values()):
        raise OperatorError(
            "operator_worker_active: the existing MUSIC AUDIT must be allowed to finish"
        )


class _Guid(ctypes.Structure):
    _fields_ = (
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    )


class _BootEnvironment(ctypes.Structure):
    _fields_ = (
        ("BootIdentifier", _Guid),
        ("FirmwareType", ctypes.c_int),
        ("BootFlags", ctypes.c_ulonglong),
    )


def current_boot_marker() -> dict[str, Any]:
    """Return an OS-derived, hash-only Windows boot-session marker."""

    if os.name != "nt":
        raise OperatorError("Windows restart verification is unavailable")
    info = _BootEnvironment()
    returned = wintypes.ULONG()
    query = ctypes.windll.ntdll.NtQuerySystemInformation
    query.argtypes = (
        wintypes.ULONG,
        ctypes.c_void_p,
        wintypes.ULONG,
        ctypes.POINTER(wintypes.ULONG),
    )
    query.restype = ctypes.c_long
    status = int(
        query(
            90,
            ctypes.byref(info),
            ctypes.sizeof(info),
            ctypes.byref(returned),
        )
    )
    if status != 0:
        raise OperatorError("cannot obtain the Windows boot-session identifier")
    raw_guid = bytes(
        ctypes.string_at(
            ctypes.byref(info.BootIdentifier), ctypes.sizeof(info.BootIdentifier)
        )
    )
    boot_id = str(uuid.UUID(bytes_le=raw_guid))
    get_uptime = ctypes.windll.kernel32.GetTickCount64
    get_uptime.restype = ctypes.c_ulonglong
    uptime_seconds = float(get_uptime()) / 1000.0
    api_boot_time = time.time() - uptime_seconds
    try:
        import psutil

        corroborating_boot_time = float(psutil.boot_time())
    except Exception as exc:
        raise OperatorError("cannot corroborate the Windows boot time") from exc
    if abs(api_boot_time - corroborating_boot_time) > 120.0:
        raise OperatorError("Windows boot-time sources disagree")
    boot_time = (api_boot_time + corroborating_boot_time) / 2.0
    return {
        "source": "windows_boot_environment",
        "boot_id_hash": hashlib.sha256(boot_id.encode("ascii")).hexdigest(),
        "boot_started_at": dt.datetime.fromtimestamp(
            boot_time, dt.timezone.utc
        ).isoformat(),
        "observed_at": now_iso(),
    }


def _parse_iso(value: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OperatorError("operator timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise OperatorError("operator timestamp must include a timezone")
    return parsed.astimezone(dt.timezone.utc)


def _without_hash(value: Mapping[str, Any], field: str) -> dict[str, Any]:
    result = dict(value)
    result.pop(field, None)
    return result


def _handoff_hash(value: Mapping[str, Any]) -> str:
    return json_hash(_without_hash(value, "handoff_hash"))


def write_handoff(path: Path, handoff: dict[str, Any]) -> None:
    handoff["updated_at"] = now_iso()
    handoff["handoff_hash"] = _handoff_hash(handoff)
    atomic_write_json(path, handoff)


def _require_equal_path(actual: Any, expected: Path, field: str) -> None:
    if Path(str(actual or "")).resolve() != expected.resolve():
        raise OperatorError(f"handoff {field} does not match the canonical path")


def validate_handoff(
    handoff: Mapping[str, Any],
    *,
    path: Path,
    paths: OperatorPaths,
) -> None:
    if handoff.get("schema_version") != HANDOFF_SCHEMA:
        raise OperatorError("unsupported MUSIC AUDIT handoff schema")
    supplied_hash = str(handoff.get("handoff_hash") or "")
    if not SHA256_PATTERN.fullmatch(supplied_hash) or supplied_hash != _handoff_hash(
        handoff
    ):
        raise OperatorError("MUSIC AUDIT handoff hash validation failed")
    intent = handoff.get("intent")
    if not isinstance(intent, Mapping):
        raise OperatorError("MUSIC AUDIT handoff is missing immutable intent")
    intent_hash = str(handoff.get("intent_hash") or "")
    if not SHA256_PATTERN.fullmatch(intent_hash) or intent_hash != json_hash(intent):
        raise OperatorError("MUSIC AUDIT immutable intent hash failed")
    _require_equal_path(handoff.get("workspace"), paths.workspace, "workspace")
    _require_equal_path(handoff.get("python"), paths.python, "python")
    _require_equal_path(
        handoff.get("engage_script"), paths.engage_script, "engage_script"
    )
    _require_equal_path(
        handoff.get("master_database"),
        paths.master_database,
        "master_database",
    )
    _require_equal_path(handoff.get("handoff_file"), path, "handoff_file")
    project = str(handoff.get("project") or "")
    if not PROJECT_PATTERN.fullmatch(project):
        raise OperatorError("handoff project slug is invalid")
    layout_schema = str(
        handoff.get("project_layout_schema") or LEGACY_PROJECT_LAYOUT_SCHEMA
    )
    artifact_stem = str(handoff.get("artifact_stem") or "")
    layout = canonical_project_layout(
        paths,
        project,
        schema_version=layout_schema,
        artifact_stem=artifact_stem,
    )
    _require_equal_path(
        handoff.get("project_root"), layout.project_root, "project_root"
    )
    _require_equal_path(
        handoff.get("database"),
        layout.database,
        "database",
    )
    artifact_expectations = {
        "handoff_file": layout.handoff_file,
        "export_file": layout.export_file,
        "review_json": layout.review_json,
        "review_markdown": layout.review_markdown,
        "ledger_file": layout.ledger_file,
    }
    for field, expected in artifact_expectations.items():
        _require_equal_path(handoff.get(field), expected, field)
    run_id = str(handoff.get("run_id") or "")
    if run_id and not RUN_ID_PATTERN.fullmatch(run_id):
        raise OperatorError("handoff run_id is not the emitted engage_* identifier")
    if run_id and not layout.database.is_file():
        raise OperatorError(
            "canonical MUSIC AUDIT database is missing; expected "
            f"{layout.database}. Do not substitute a renamed or copied database."
        )
    frozen_selection = handoff.get("frozen_selection")
    frozen_selection_hash = str(handoff.get("frozen_selection_hash") or "")
    if run_id:
        if not isinstance(frozen_selection, Mapping):
            raise OperatorError("handoff is missing its frozen run selection")
        if not SHA256_PATTERN.fullmatch(
            frozen_selection_hash
        ) or frozen_selection_hash != json_hash(frozen_selection):
            raise OperatorError("handoff frozen selection hash failed")
    elif frozen_selection != {} or frozen_selection_hash:
        raise OperatorError("unbound handoff contains a frozen run selection")
    if handoff.get("source_mode") not in {"topic", "creator", "url"}:
        raise OperatorError("handoff source_mode is invalid")
    if handoff.get("collection_policy") not in {"new_only", "refresh_known"}:
        raise OperatorError("handoff collection_policy is invalid")
    intent_bindings = {
        "project": handoff.get("project"),
        "project_layout_schema": handoff.get("project_layout_schema"),
        "artifact_stem": handoff.get("artifact_stem"),
        "run_label": handoff.get("run_label"),
        "run_label_input": handoff.get("run_label_input"),
        "run_descriptor": handoff.get("run_descriptor"),
        "source_mode": handoff.get("source_mode"),
        "source_target": handoff.get("source_target"),
        "requested_count": handoff.get("requested_count"),
        "all_posts": handoff.get("all_posts"),
        "collection_policy": handoff.get("collection_policy"),
        "max_comments": handoff.get("max_comments"),
        "browser_startup_timeout": handoff.get("browser_startup_timeout"),
        "expected_account": handoff.get("expected_account"),
        "database": handoff.get("database"),
        "master_database": handoff.get("master_database"),
    }
    for field, actual in intent_bindings.items():
        if intent.get(field) != actual:
            raise OperatorError(f"handoff drifted from immutable intent: {field}")
    if int(handoff.get("restart_count") or 0) not in {0, 1}:
        raise OperatorError("handoff exceeds the single bounded restart allowance")
    if int(handoff.get("recovery_epoch") or 0) not in {0, 1}:
        raise OperatorError("handoff recovery_epoch is invalid")
    attempts = handoff.get("resume_attempts_by_epoch")
    if not isinstance(attempts, Mapping) or any(
        str(key) not in {"0", "1"} or int(value) not in {0, 1}
        for key, value in attempts.items()
    ):
        raise OperatorError("handoff resume attempt ledger is invalid")
    execution = handoff.get("execution", {})
    if not isinstance(execution, Mapping):
        raise OperatorError("handoff execution state is invalid")
    if not isinstance(handoff.get("execution_history", []), list):
        raise OperatorError("handoff execution history is invalid")
    if execution:
        if execution.get("operation") not in {"start", "resume"}:
            raise OperatorError("handoff execution operation is invalid")
        if execution.get("state") not in {
            "starting",
            "running",
            "finishing",
            "complete",
            "blocked",
            "interrupted",
        }:
            raise OperatorError("handoff execution status is invalid")
        execution_id = str(execution.get("execution_id") or "")
        if not re.fullmatch(r"[0-9a-f]{32}", execution_id):
            raise OperatorError("handoff execution ID is invalid")
    marker = handoff.get("restart_boot_marker", {})
    if not isinstance(marker, Mapping):
        raise OperatorError("handoff restart boot marker is invalid")
    if marker and not SHA256_PATTERN.fullmatch(str(marker.get("boot_id_hash") or "")):
        raise OperatorError("handoff restart boot marker hash is invalid")


def load_handoff(path: Path, paths: OperatorPaths) -> dict[str, Any]:
    path = path.resolve()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OperatorError(f"cannot read MUSIC AUDIT handoff: {exc}") from exc
    if not isinstance(value, dict):
        raise OperatorError("MUSIC AUDIT handoff must be a JSON object")
    validate_handoff(value, path=path, paths=paths)
    return value


def sanitize_error(value: Any) -> str:
    message = str(value or "").replace("\r", " ").replace("\n", " ")
    # Credential-bearing header contexts are truncated aggressively. Diagnostic
    # precision is less important than ensuring a second token or cookie pair
    # cannot survive a partial key/value replacement.
    message = re.sub(
        r"(?i)\b(?:authorization|proxy-authorization|cookie|set-cookie)[\"']?\s*[:=]\s*.*$",
        "<redacted-credential-context>",
        message,
    )
    message = re.sub(
        r"(?i)\b(?:bearer|basic)\s+[^\s,;]+",
        "<redacted-credential>",
        message,
    )
    message = re.sub(
        r"(?i)\b(sessionid|mstoken|access_token|refresh_token|approval_token)[\"']?\s*[:=]\s*[\"']?[^\s,;\"']+",
        r"\1=<redacted>",
        message,
    )
    message = re.sub(
        r"(?i)wss?://[^\s\"']+",
        "<redacted-cdp-endpoint>",
        message,
    )
    message = re.sub(
        r"(?i)https?://127\.0\.0\.1:\d+[^\s\"']*",
        "<redacted-local-debug-endpoint>",
        message,
    )
    message = re.sub(
        r"(?i)https?://[^\s\"']+",
        "<redacted-url>",
        message,
    )
    return message[:1000]


def append_ledger(
    handoff: dict[str, Any],
    *,
    action: str,
    stage: str,
    paths: OperatorPaths,
    command: Sequence[str] = (),
    exit_code: int | None = None,
    duration_ms: float | None = None,
    status: Mapping[str, Any] | None = None,
    error: str = "",
    artifacts: Sequence[Mapping[str, Any]] = (),
    next_action: str = "",
    handoff_transition: Mapping[str, Any] | None = None,
) -> None:
    transition = dict(handoff_transition or {})
    if not set(transition).issubset(RECOVERABLE_HANDOFF_TRANSITION_KEYS):
        raise OperatorError("operator ledger contains an unsafe handoff transition")
    sequence = int(handoff.get("ledger_sequence") or 0) + 1
    safe_status = status_summary(status or {})
    entry: dict[str, Any] = {
        "schema_version": LEDGER_SCHEMA,
        "sequence": sequence,
        "timestamp": now_iso(),
        "stage": stage,
        "action": action,
        "policy_basis": "canonical_music_audit_collection_only",
        "interpreter": str(paths.python),
        "working_directory": str(paths.workspace),
        "argv": list(command),
        "exit_code": exit_code,
        "duration_ms": duration_ms,
        "run_id": str(handoff.get("run_id") or ""),
        "frozen_selection_hash": str(handoff.get("frozen_selection_hash") or ""),
        "database": str(handoff["database"]),
        "master_database": str(handoff["master_database"]),
        "status": safe_status,
        "recovery_epoch": int(handoff.get("recovery_epoch") or 0),
        "restart_pending": handoff.get("restart_pending") is True,
        "error": sanitize_error(error),
        "artifacts": list(artifacts),
        "next_action": next_action,
        "handoff_transition": transition,
        "ai_actions": [],
        "outbound_actions": [],
        "previous_record_hash": str(handoff.get("ledger_head_hash") or ""),
    }
    entry["record_hash"] = json_hash(entry)
    ledger = Path(str(handoff["ledger_file"]))
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
    handoff["ledger_sequence"] = sequence
    handoff["ledger_head_hash"] = entry["record_hash"]
    write_handoff(Path(str(handoff["handoff_file"])), handoff)


def validate_ledger(path: Path) -> tuple[int, str]:
    previous = ""
    count = 0
    with path.open("r", encoding="utf-8-sig") as handle:
        for count, line in enumerate(handle, start=1):
            if not line.strip():
                raise OperatorError("operator ledger contains a blank record")
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                raise OperatorError("operator ledger contains invalid JSON") from exc
            if entry.get("schema_version") != LEDGER_SCHEMA:
                raise OperatorError("operator ledger schema mismatch")
            if int(entry.get("sequence") or 0) != count:
                raise OperatorError("operator ledger sequence is not contiguous")
            if str(entry.get("previous_record_hash") or "") != previous:
                raise OperatorError("operator ledger hash chain is broken")
            supplied = str(entry.get("record_hash") or "")
            if supplied != json_hash(_without_hash(entry, "record_hash")):
                raise OperatorError("operator ledger record hash is invalid")
            if entry.get("ai_actions") != [] or entry.get("outbound_actions") != []:
                raise OperatorError("operator ledger reports a prohibited action")
            previous = supplied
    return count, previous


def reconcile_ledger_head(handoff: dict[str, Any]) -> None:
    """Adopt one fully written ledger record after an interrupted handoff write.

    ``append_ledger`` deliberately persists the append-only record before it
    advances the handoff head. A process interruption between those writes can
    therefore leave the validated ledger exactly one record ahead. Under the
    per-project operator lock, it is safe to adopt only that one chained record;
    every other mismatch remains a fail-closed integrity error.
    """

    ledger = Path(str(handoff["ledger_file"])).resolve()
    stored_count = int(handoff.get("ledger_sequence") or 0)
    stored_head = str(handoff.get("ledger_head_hash") or "")
    if not ledger.exists():
        if stored_count == 0 and not stored_head:
            return
        raise OperatorError("operator ledger is missing")
    records, head = validate_ledger(ledger)
    if records == stored_count and head == stored_head:
        return
    if records != stored_count + 1:
        raise OperatorError("operator ledger count does not match the handoff")
    last_entry: Mapping[str, Any] | None = None
    with ledger.open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            if line.strip():
                parsed = json.loads(line)
                if not isinstance(parsed, Mapping):
                    raise OperatorError("operator ledger record is not an object")
                last_entry = parsed
    if last_entry is None:
        raise OperatorError("operator ledger recovery record is missing")
    if int(last_entry.get("sequence") or 0) != records:
        raise OperatorError("operator ledger recovery sequence is invalid")
    if str(last_entry.get("previous_record_hash") or "") != stored_head:
        raise OperatorError("operator ledger recovery record is not chained")
    if str(last_entry.get("record_hash") or "") != head:
        raise OperatorError("operator ledger recovery head is invalid")
    transition = last_entry.get("handoff_transition") or {}
    if not isinstance(transition, Mapping) or not set(transition).issubset(
        RECOVERABLE_HANDOFF_TRANSITION_KEYS
    ):
        raise OperatorError("operator ledger recovery transition is invalid")
    for key, value in transition.items():
        handoff[key] = value
    handoff["ledger_sequence"] = records
    handoff["ledger_head_hash"] = head
    write_handoff(Path(str(handoff["handoff_file"])), handoff)


def read_validated_ledger(
    handoff: Mapping[str, Any],
) -> list[dict[str, Any]]:
    path = Path(str(handoff["ledger_file"])).resolve()
    records, head = validate_ledger(path)
    if records != int(handoff.get("ledger_sequence") or 0):
        raise OperatorError("operator ledger count does not match the handoff")
    if head != str(handoff.get("ledger_head_hash") or ""):
        raise OperatorError("operator ledger head does not match the handoff")
    entries: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            if line.strip():
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise OperatorError("operator ledger record is not an object")
                entries.append(value)
    return entries


def require_paired_terminal_collection_result(
    handoff: Mapping[str, Any],
) -> Mapping[str, Any]:
    entries = read_validated_ledger(handoff)
    starts = {
        "music-audit-start": "music-audit-result",
        "resume-collect-start": "resume-collect-result",
    }
    start_index = -1
    expected_result = ""
    for index, entry in enumerate(entries):
        action = str(entry.get("action") or "")
        if action in starts:
            start_index = index
            expected_result = starts[action]
    if start_index < 0:
        raise OperatorError("restart is not bound to a canonical collection attempt")
    later = entries[start_index + 1 :]
    results = [entry for entry in later if entry.get("action") == expected_result]
    if len(results) != 1 or results[0].get("exit_code") is None:
        raise OperatorError(
            "restart rejected: the latest collection attempt has no terminal result"
        )
    if int(results[0].get("exit_code") or 0) == 0:
        raise OperatorError("restart rejected: the latest collection attempt succeeded")
    return results[0]


def successful_browser_preflight_exists(handoff: Mapping[str, Any]) -> bool:
    database = Path(str(handoff["database"])).resolve()
    run_id = str(handoff.get("run_id") or "")
    conn = readonly_connect(database)
    try:
        count = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM engage_tiktok_events
                WHERE run_id=? AND stage='browser_preflight' AND event='passed'
                """,
                (run_id,),
            ).fetchone()[0]
        )
    finally:
        conn.close()
    return count > 0


def latest_collection_attempt_is_unpaired(
    entries: Sequence[Mapping[str, Any]],
) -> bool:
    starts = {
        "music-audit-start": "music-audit-result",
        "resume-collect-start": "resume-collect-result",
    }
    start_index = -1
    expected_result = ""
    for index, entry in enumerate(entries):
        action = str(entry.get("action") or "")
        if action in starts:
            start_index = index
            expected_result = starts[action]
    if start_index < 0:
        return False
    return not any(
        entry.get("action") == expected_result for entry in entries[start_index + 1 :]
    )


def executor_compliance_summary(
    handoff: Mapping[str, Any],
    entries: Sequence[Mapping[str, Any]],
) -> tuple[str, list[str]]:
    deviations: list[str] = []
    intent = handoff.get("intent")
    if (
        isinstance(intent, Mapping)
        and intent.get("all_posts") is True
        and int(intent.get("max_pages") or 0) > 0
    ):
        deviations.append(
            "Creator ALL used an executor-selected finite page bound; ALL "
            "requires an uncapped verified terminal creator inventory."
        )
    if any(entry.get("action") == "cancel-restart-handoff" for entry in entries):
        deviations.append(
            "A premature restart claim was cancelled in the same Windows boot session."
        )
        deviations.append(
            "The original collection command was interrupted before its terminal result."
        )
    histories = handoff.get("execution_history", [])
    if isinstance(histories, list) and any(
        isinstance(value, Mapping) and value.get("state") == "interrupted"
        for value in histories
    ):
        deviations.append("A recorded guarded-operator execution was interrupted.")
    return ("FAIL" if deviations else "PASS"), list(dict.fromkeys(deviations))


def assert_runtime(paths: OperatorPaths) -> None:
    if Path(sys.executable).resolve() != paths.python.resolve():
        raise OperatorError(
            "run this operator with "
            r"C:\Users\DELL\AppData\Local\Programs\Python\Python311\python.exe"
        )
    if Path.cwd().resolve() != paths.workspace.resolve():
        raise OperatorError(f"working directory must be {paths.workspace}")
    if not paths.engage_script.is_file():
        raise OperatorError("canonical engage_tiktok.py is missing")


def assert_safe_command(command: Sequence[str], paths: OperatorPaths) -> None:
    if len(command) < 3:
        raise OperatorError("operator command is incomplete")
    if Path(command[0]).resolve() != paths.python.resolve():
        raise OperatorError("operator rejected a noncanonical Python interpreter")
    if Path(command[1]).resolve() != paths.engage_script.resolve():
        raise OperatorError("operator rejected a noncanonical workflow script")
    lowered = [str(value).casefold() for value in command]
    joined = " ".join(lowered)
    for fragment in FORBIDDEN_ARG_FRAGMENTS:
        if fragment in joined:
            raise OperatorError(f"operator rejected forbidden argument: {fragment}")
    allowed_commands = {"music-audit", "resume-collect", "status", "export-evidence"}
    observed = [value for value in lowered[2:] if value in allowed_commands]
    if len(observed) != 1:
        raise OperatorError("operator command must contain one canonical subcommand")


def _parse_cli_json(stdout: str) -> Mapping[str, Any]:
    try:
        payload = json.loads(stdout.strip())
    except json.JSONDecodeError as exc:
        raise OperatorError("canonical CLI did not return one JSON document") from exc
    if not isinstance(payload, Mapping):
        raise OperatorError("canonical CLI result must be a JSON object")
    return payload


def begin_execution(handoff: dict[str, Any], *, operation: str) -> None:
    previous = handoff.get("execution")
    if isinstance(previous, Mapping) and previous:
        history = handoff.setdefault("execution_history", [])
        if not isinstance(history, list):
            raise OperatorError("handoff execution history is invalid")
        history.append(dict(previous))
    handoff["execution"] = {
        "execution_id": uuid.uuid4().hex,
        "operation": operation,
        "state": "starting",
        "operator_process": process_identity(os.getpid(), role="operator"),
        "collector_process": {},
        "started_at": now_iso(),
        "heartbeat_at": "",
        "heartbeat_sequence": 0,
        "phase": "initializing" if operation == "start" else "resuming",
        "finished_at": "",
        "exit_code": None,
        "error": "",
    }
    write_handoff(Path(str(handoff["handoff_file"])), handoff)


def finish_execution(
    handoff: dict[str, Any],
    *,
    state: str,
    exit_code: int,
    error: str = "",
) -> None:
    execution = handoff.get("execution")
    if not isinstance(execution, dict):
        return
    execution["state"] = state
    execution["finished_at"] = now_iso()
    execution["exit_code"] = int(exit_code)
    execution["error"] = sanitize_error(error)
    collector = execution.get("collector_process")
    if isinstance(collector, dict):
        collector["alive"] = False
    write_handoff(Path(str(handoff["handoff_file"])), handoff)


def _record_collector_start(
    handoff: dict[str, Any],
    *,
    pid: int,
) -> None:
    execution = handoff.get("execution")
    if not isinstance(execution, dict):
        return
    identity = process_identity(pid, role="canonical_collector")
    identity["alive"] = True
    execution["collector_process"] = identity
    execution["state"] = "running"
    write_handoff(Path(str(handoff["handoff_file"])), handoff)


def _emit_collection_heartbeat(
    handoff: dict[str, Any],
    *,
    paths: OperatorPaths,
    elapsed_seconds: float,
) -> None:
    try:
        progress_error = ""
        try:
            _try_bind_run_while_active(handoff, paths=paths)
            progress = durable_progress_snapshot(handoff)
            if handoff.get("run_id"):
                handoff["last_status"] = status_summary(
                    durable_local_run_state(handoff)
                )
        except Exception as exc:
            progress_error = sanitize_error(exc)
            progress = {
                "phase": "progress_check_deferred",
                "durable_status": "unknown",
                "requested": int(handoff.get("requested_count") or 0),
                "evidence_ready": 0,
                "unique_collected": 0,
                "failed": 0,
                "latest_event": {},
            }
        execution = handoff.get("execution")
        if isinstance(execution, dict):
            execution["heartbeat_sequence"] = (
                int(execution.get("heartbeat_sequence") or 0) + 1
            )
            execution["heartbeat_at"] = now_iso()
            execution["phase"] = progress["phase"]
            execution["state"] = "running"
            collector = execution.get("collector_process")
            if isinstance(collector, dict):
                collector["alive"] = recorded_process_alive(collector, paths=paths)
        try:
            write_handoff(Path(str(handoff["handoff_file"])), handoff)
        except Exception as exc:
            progress_error = sanitize_error(exc)
    except Exception as exc:
        # Heartbeat inspection is advisory. It must never unwind supervision of
        # a healthy collector or orphan the exact child process.
        progress_error = sanitize_error(exc)
        progress = {
            "phase": "heartbeat_deferred",
            "durable_status": "unknown",
            "requested": int(handoff.get("requested_count") or 0),
            "evidence_ready": 0,
            "unique_collected": 0,
            "failed": 0,
        }
    message = {
        "status": "RUNNING",
        "project": handoff["project"],
        "run_id": handoff.get("run_id") or "pending",
        "handoff": str(Path(str(handoff["handoff_file"])).resolve()),
        "phase": progress["phase"],
        "durable_status": progress["durable_status"],
        "evidence_ready": progress["evidence_ready"],
        "requested": progress["requested"],
        "unique_collected": progress["unique_collected"],
        "failed": progress["failed"],
        "elapsed_seconds": int(elapsed_seconds),
        "collector_running": True,
        "instruction": "keep_waiting_do_not_restart",
    }
    if progress_error:
        message["progress_note"] = progress_error
    try:
        print(
            "MUSIC_AUDIT_HEARTBEAT "
            + json.dumps(message, ensure_ascii=True, separators=(",", ":")),
            file=sys.stderr,
            flush=True,
        )
    except OSError:
        pass


def emit_execution_started(handoff: Mapping[str, Any], *, operation: str) -> None:
    message = {
        "status": "RUNNING",
        "operation": operation,
        "project": handoff["project"],
        "project_directory": str(Path(str(handoff["project_root"])).resolve()),
        "run_id": handoff.get("run_id") or "pending",
        "handoff": str(Path(str(handoff["handoff_file"])).resolve()),
        "instruction": "keep_task_alive_wait_for_heartbeat",
    }
    try:
        print(
            "MUSIC_AUDIT_STARTED "
            + json.dumps(message, ensure_ascii=True, separators=(",", ":")),
            file=sys.stderr,
            flush=True,
        )
    except OSError:
        pass


def _stop_and_reap_exact_child(
    process: subprocess.Popen[str],
) -> tuple[str, str]:
    """Stop only the child created by this operator and always reap it."""

    stdout = ""
    stderr = ""
    try:
        if process.poll() is None:
            process.terminate()
        try:
            stdout, stderr = process.communicate(timeout=5.0)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate(timeout=5.0)
    except Exception:
        try:
            if process.poll() is None:
                process.kill()
            stdout, stderr = process.communicate(timeout=5.0)
        except Exception:
            pass
    return stdout or "", stderr or ""


def run_engage(
    arguments: Sequence[str],
    *,
    paths: OperatorPaths,
    handoff: dict[str, Any] | None = None,
    heartbeat_interval: float = HEARTBEAT_INTERVAL_SECONDS,
    on_process_started: Callable[[], None] | None = None,
) -> CommandResult:
    command = (str(paths.python), str(paths.engage_script), *map(str, arguments))
    assert_safe_command(command, paths)
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            command,
            cwd=paths.workspace,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        duration_ms = round((time.monotonic() - started) * 1000.0, 2)
        error = sanitize_error(f"collector_launch_failed: {exc}")
        return CommandResult(
            command,
            -1,
            duration_ms,
            {"status": "blocked", "error": "collector_launch_failed"},
            error,
            child_started=False,
        )
    try:
        if handoff is not None:
            _record_collector_start(handoff, pid=process.pid)
        if on_process_started is not None:
            on_process_started()
    except Exception as exc:
        _, child_stderr = _stop_and_reap_exact_child(process)
        duration_ms = round((time.monotonic() - started) * 1000.0, 2)
        error = sanitize_error(
            f"collector_launch_registration_failed: {exc}; {child_stderr}"
        )
        return CommandResult(
            command,
            int(process.returncode if process.returncode is not None else -1),
            duration_ms,
            {
                "status": "blocked",
                "error": "collector_launch_registration_failed",
            },
            error,
            child_started=False,
        )
    stdout = ""
    stderr = ""
    while True:
        try:
            stdout, stderr = process.communicate(timeout=float(heartbeat_interval))
            break
        except subprocess.TimeoutExpired:
            if handoff is not None:
                _emit_collection_heartbeat(
                    handoff,
                    paths=paths,
                    elapsed_seconds=time.monotonic() - started,
                )
        except Exception as exc:
            child_stdout, child_stderr = _stop_and_reap_exact_child(process)
            stdout = child_stdout
            stderr = child_stderr
            duration_ms = round((time.monotonic() - started) * 1000.0, 2)
            error = sanitize_error(f"collector_supervision_failed: {exc}; {stderr}")
            return CommandResult(
                command,
                int(process.returncode if process.returncode is not None else -1),
                duration_ms,
                {"status": "blocked", "error": "collector_supervision_failed"},
                error,
                child_started=True,
            )
    duration_ms = round((time.monotonic() - started) * 1000.0, 2)
    try:
        payload = _parse_cli_json(stdout)
    except OperatorError:
        payload = {"status": "blocked", "error": "non_json_cli_output"}
    error = sanitize_error(payload.get("error") or stderr)
    if handoff is not None:
        execution = handoff.get("execution")
        if isinstance(execution, dict):
            execution["state"] = "finishing"
            collector = execution.get("collector_process")
            if isinstance(collector, dict):
                collector["alive"] = False
                collector["exit_code"] = int(process.returncode)
                collector["finished_at"] = now_iso()
            write_handoff(Path(str(handoff["handoff_file"])), handoff)
    return CommandResult(command, process.returncode, duration_ms, payload, error)


def readonly_connect(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise OperatorError(f"database does not exist: {path}")
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def discover_run_id(database: Path, project: str) -> str:
    conn = readonly_connect(database)
    try:
        rows = conn.execute(
            "SELECT run_id FROM engage_tiktok_runs WHERE project=? ORDER BY created_at",
            (project,),
        ).fetchall()
    finally:
        conn.close()
    if len(rows) != 1:
        raise OperatorError(
            "cannot identify exactly one durable run for the immutable project"
        )
    run_id = str(rows[0]["run_id"] or "")
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise OperatorError("database run ID is not the canonical engage_* value")
    return run_id


def _refresh_candidate_post_id(value: Any) -> str:
    if not isinstance(value, Mapping):
        raise OperatorError("durable refresh selection contains a non-object row")
    post_id = str(value.get("id") or value.get("post_id") or "")
    if not post_id.isdigit():
        raise OperatorError("durable refresh candidate has an invalid post ID")
    return post_id


def frozen_selection_from_run(run: Mapping[str, Any]) -> dict[str, Any]:
    policy = str(run.get("collection_policy") or "")
    refresh_ids = _json_array(
        run.get("refresh_post_ids_json"), "durable refresh post selection"
    )
    candidates = _json_array(
        run.get("refresh_candidates_json"), "durable refresh candidate selection"
    )
    if any(not isinstance(value, str) or not value.isdigit() for value in refresh_ids):
        raise OperatorError("durable refresh post selection contains an invalid ID")
    if len(refresh_ids) != len(set(refresh_ids)):
        raise OperatorError("durable refresh post selection contains duplicate IDs")
    candidate_ids = [_refresh_candidate_post_id(value) for value in candidates]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise OperatorError("durable refresh candidate selection contains duplicates")
    if not set(candidate_ids).issubset(refresh_ids):
        raise OperatorError("durable refresh candidates are outside their ID selection")
    cutoff = str(run.get("refresh_stale_before") or "")
    if policy == "new_only":
        if refresh_ids or candidates or cutoff:
            raise OperatorError("new_only run contains a refresh selection")
    elif policy != "refresh_known":
        raise OperatorError("durable run has an invalid collection policy")
    return {
        "collection_policy": policy,
        "refresh_post_ids": refresh_ids,
        "refresh_candidate_post_ids": candidate_ids,
        "refresh_candidate_count": len(candidates),
        "refresh_candidates_hash": json_hash(candidates),
        "refresh_stale_before": cutoff,
    }


def capture_frozen_selection(handoff: dict[str, Any]) -> dict[str, Any]:
    run_id = str(handoff.get("run_id") or "")
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise OperatorError("cannot freeze selection without the real engage_* run ID")
    conn = readonly_connect(Path(str(handoff["database"])))
    try:
        row = conn.execute(
            "SELECT * FROM engage_tiktok_runs WHERE run_id=?", (run_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise OperatorError("cannot freeze selection for a missing durable run")
    actual = frozen_selection_from_run(_row_dict(row))
    stored = handoff.get("frozen_selection")
    stored_hash = str(handoff.get("frozen_selection_hash") or "")
    if stored not in ({}, None):
        if not isinstance(stored, Mapping):
            raise OperatorError("handoff frozen selection is invalid")
        if stored_hash != json_hash(stored):
            raise OperatorError("handoff frozen selection hash failed")
        if canonical_json(stored) != canonical_json(actual):
            raise OperatorError("durable frozen refresh selection drifted")
    handoff["frozen_selection"] = actual
    handoff["frozen_selection_hash"] = json_hash(actual)
    return actual


def durable_local_run_state(handoff: Mapping[str, Any]) -> dict[str, Any]:
    database = Path(str(handoff["database"])).resolve()
    run_id = str(handoff.get("run_id") or "")
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise OperatorError("handoff does not contain a valid durable run_id")
    conn = readonly_connect(database)
    try:
        row = conn.execute(
            """
            SELECT run_id, project, workflow, source_mode, collection_policy,
                   status, requested_count, requested, unique_collected,
                   evidence_ready, analyzed, drafted, reviewed, stored,
                   authorized, published, skipped, failed, expected_account,
                   observed_account, cardinality_mode, max_pages,
                   profile_inventory_terminal, profile_inventory_count,
                   browser_preflight_json, error
            FROM engage_tiktok_runs WHERE run_id=?
            """,
            (run_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise OperatorError("durable local workflow run is missing")
    return _row_dict(row)


def durable_local_run_status(handoff: Mapping[str, Any]) -> str:
    return str(durable_local_run_state(handoff)["status"] or "")


def _latest_durable_event(handoff: Mapping[str, Any]) -> dict[str, Any]:
    database = Path(str(handoff["database"])).resolve()
    run_id = str(handoff.get("run_id") or "")
    if not database.is_file() or not RUN_ID_PATTERN.fullmatch(run_id):
        return {}
    conn = readonly_connect(database)
    try:
        row = conn.execute(
            """
            SELECT event_id, stage, event, created_at
            FROM engage_tiktok_events
            WHERE run_id=? ORDER BY event_id DESC LIMIT 1
            """,
            (run_id,),
        ).fetchone()
    finally:
        conn.close()
    return _row_dict(row) if row is not None else {}


def _try_bind_run_while_active(
    handoff: dict[str, Any],
    *,
    paths: OperatorPaths,
) -> None:
    if handoff.get("run_id"):
        return
    database = Path(str(handoff["database"])).resolve()
    if not database.is_file():
        return
    conn = readonly_connect(database)
    try:
        rows = conn.execute(
            "SELECT run_id FROM engage_tiktok_runs WHERE project=? ORDER BY created_at",
            (str(handoff["project"]),),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return
    if len(rows) != 1:
        raise OperatorError(
            "cannot identify exactly one durable run for the immutable project"
        )
    run_id = str(rows[0]["run_id"] or "")
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise OperatorError("database run ID is not the canonical engage_* value")
    handoff["run_id"] = run_id
    capture_frozen_selection(handoff)
    validate_run_intent(handoff, paths=paths)
    write_handoff(Path(str(handoff["handoff_file"])), handoff)


def durable_progress_snapshot(
    handoff: Mapping[str, Any],
) -> dict[str, Any]:
    if not handoff.get("run_id"):
        return {
            "phase": "initializing",
            "durable_status": "not_created",
            "requested": int(handoff.get("requested_count") or 0),
            "evidence_ready": 0,
            "unique_collected": 0,
            "failed": 0,
            "latest_event": {},
        }
    run = durable_local_run_state(handoff)
    latest = _latest_durable_event(handoff)
    durable_status = str(run.get("status") or "unknown")
    if durable_status == "collection_complete":
        phase = "collection_complete"
    elif durable_status in {"browser_blocked", "collection_failed"}:
        phase = durable_status
    elif latest.get("stage") == "browser_preflight" and latest.get("event") == "passed":
        phase = "collecting_evidence"
    elif latest.get("stage") == "browser_preflight":
        phase = "browser_preflight"
    elif latest.get("event") == "attempt_claimed":
        phase = "browser_preflight"
    else:
        phase = (
            "collecting_evidence" if durable_status == "collecting" else durable_status
        )
    return {
        "phase": phase,
        "durable_status": durable_status,
        "requested": int(run.get("requested") or run.get("requested_count") or 0),
        "evidence_ready": int(run.get("evidence_ready") or 0),
        "unique_collected": int(run.get("unique_collected") or 0),
        "failed": int(run.get("failed") or 0),
        "latest_event": latest,
    }


def _creator_handle_from_target(value: str) -> str:
    target = value.strip()
    if target.casefold().startswith("http"):
        parsed = urlsplit(target)
        match = re.match(r"^/@([^/]+)/*$", parsed.path)
        if not match:
            raise OperatorError("creator target is not a TikTok profile URL")
        target = match.group(1)
    return target.lstrip("@").casefold()


def validate_run_intent(
    handoff: Mapping[str, Any],
    *,
    paths: OperatorPaths,
) -> dict[str, Any]:
    run_id = str(handoff.get("run_id") or "")
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise OperatorError("cannot bind intent without the real engage_* run ID")
    intent = handoff["intent"]
    conn = readonly_connect(Path(str(handoff["database"])))
    try:
        row = conn.execute(
            "SELECT * FROM engage_tiktok_runs WHERE run_id=?", (run_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise OperatorError("immutable MUSIC AUDIT run is missing")
    run = _row_dict(row)
    exact = {
        "run_id": run_id,
        "project": intent["project"],
        "workflow": "listen",
        "mode": "shadow",
        "source_mode": intent["source_mode"],
        "collection_policy": intent["collection_policy"],
        "max_comments": int(intent["max_comments"]),
        "max_pages": int(intent["resolved_max_pages"]),
    }
    for field, expected in exact.items():
        actual = run.get(field)
        if field in {"max_comments", "max_pages"}:
            actual = int(actual or 0)
        if actual != expected:
            raise OperatorError(f"durable run drifted from immutable intent: {field}")
    if Path(str(run["master_database"])).resolve() != paths.master_database:
        raise OperatorError("durable run drifted from immutable master database")
    cardinality = "all" if intent["all_posts"] else "fixed"
    if run["cardinality_mode"] != cardinality:
        raise OperatorError("durable run cardinality drifted from intent")
    if not intent["all_posts"] and int(run["requested_count"] or 0) != int(
        intent["requested_count"]
    ):
        raise OperatorError("durable requested count drifted from intent")
    source_mode = intent["source_mode"]
    target = str(intent["source_target"])
    if source_mode == "topic" and str(run["topic"]) != target:
        raise OperatorError("durable topic drifted from intent")
    if source_mode == "url" and str(run["direct_post_url"]) != target:
        raise OperatorError("durable direct URL drifted from intent")
    if source_mode == "creator" and str(
        run["creator_handle"]
    ).casefold() != _creator_handle_from_target(target):
        raise OperatorError("durable creator drifted from intent")
    try:
        catalogs = json.loads(str(run["music_catalogs_json"] or "[]"))
    except json.JSONDecodeError as exc:
        raise OperatorError("durable run contains invalid immutable JSON") from exc
    if catalogs != intent["music_catalogs"]:
        raise OperatorError("durable catalog scope drifted from intent")
    actual_selection = frozen_selection_from_run(run)
    stored_selection = handoff.get("frozen_selection")
    stored_selection_hash = str(handoff.get("frozen_selection_hash") or "")
    if not isinstance(stored_selection, Mapping):
        raise OperatorError("handoff is missing its frozen run selection")
    if not SHA256_PATTERN.fullmatch(
        stored_selection_hash
    ) or stored_selection_hash != json_hash(stored_selection):
        raise OperatorError("handoff frozen selection hash failed")
    if canonical_json(actual_selection) != canonical_json(stored_selection):
        raise OperatorError("durable frozen refresh selection drifted")
    selector_ids = list(intent["refresh_post_ids"])
    actual_ids = list(actual_selection["refresh_post_ids"])
    candidate_ids = list(actual_selection["refresh_candidate_post_ids"])
    if intent["collection_policy"] == "refresh_known":
        if selector_ids:
            if actual_ids != selector_ids:
                raise OperatorError("durable refresh ID selector drifted from intent")
        elif actual_ids != candidate_ids:
            raise OperatorError("automatic refresh selection is not candidate-bound")
        if source_mode == "url":
            _, direct_id = _canonical_direct_url(target)
            if actual_ids != [direct_id] or candidate_ids not in ([], [direct_id]):
                raise OperatorError("direct URL refresh selection drifted from intent")
    if str(run["refresh_stale_before"] or "") != str(
        intent["refresh_stale_before"] or ""
    ):
        raise OperatorError("durable refresh cutoff drifted from intent")
    expected_account = str(intent.get("expected_account") or "").casefold()
    if (
        expected_account
        and str(run["expected_account"] or "").casefold() != expected_account
    ):
        raise OperatorError("durable expected account drifted from intent")
    if (
        not expected_account
        and run["expected_account"]
        and str(run["expected_account"]).casefold()
        != str(run["observed_account"] or "").casefold()
    ):
        raise OperatorError("durable bound account drifted from observed account")
    return run


def status_summary(status: Mapping[str, Any]) -> dict[str, Any]:
    if not status:
        return {}
    keys = (
        "run_id",
        "project",
        "workflow",
        "source_mode",
        "collection_policy",
        "cardinality_mode",
        "max_pages",
        "profile_inventory_terminal",
        "profile_inventory_count",
        "status",
        "requested_count",
        "requested",
        "unique_collected",
        "evidence_ready",
        "analyzed",
        "drafted",
        "reviewed",
        "stored",
        "authorized",
        "published",
        "skipped",
        "failed",
        "expected_account",
        "observed_account",
    )
    return {key: status.get(key) for key in keys if key in status}


def _status_arguments(handoff: Mapping[str, Any]) -> list[str]:
    return [
        "--database",
        str(handoff["database"]),
        "--master-database",
        str(handoff["master_database"]),
        "status",
        "--run-id",
        str(handoff["run_id"]),
    ]


def canonical_status(
    handoff: dict[str, Any],
    *,
    paths: OperatorPaths,
) -> tuple[Mapping[str, Any], CommandResult]:
    result = run_engage(_status_arguments(handoff), paths=paths)
    if result.exit_code != 0:
        raise OperatorError(result.sanitized_error or "canonical status command failed")
    status = result.payload
    if str(status.get("run_id") or "") != str(handoff["run_id"]):
        raise OperatorError("status returned a different run ID")
    handoff["last_status"] = status_summary(status)
    handoff["state"] = str(status.get("status") or "unknown")
    write_handoff(Path(str(handoff["handoff_file"])), handoff)
    return status, result


def _safe_project_slug(value: str) -> str:
    project = value.strip()
    if not PROJECT_PATTERN.fullmatch(project):
        raise OperatorError(
            "project must contain only letters, digits, dot, underscore, or hyphen"
        )
    if project.casefold().startswith("project_"):
        raise OperatorError(
            "pass the project slug without the project_ folder prefix"
        )
    if project != project.casefold():
        raise OperatorError("guarded MUSIC AUDIT project slugs must be lowercase")
    if not project.casefold().startswith("music_audit_") or len(project) <= len(
        "music_audit_"
    ):
        raise OperatorError(
            "guarded MUSIC AUDIT project slugs must start with music_audit_"
        )
    if project.endswith("."):
        raise OperatorError("project must not end with a dot")
    if len(project) > NEW_PROJECT_SLUG_MAX:
        raise OperatorError(
            "new guarded MUSIC AUDIT project slugs must be at most "
            f"{NEW_PROJECT_SLUG_MAX} characters"
        )
    return project


def _canonical_direct_url(value: str) -> tuple[str, str]:
    raw = value.strip()
    parsed = urlsplit(raw)
    if (
        parsed.scheme.casefold() != "https"
        or parsed.hostname not in {"tiktok.com", "www.tiktok.com"}
        or parsed.query
        or parsed.fragment
    ):
        raise OperatorError("direct URL must be a query-free canonical TikTok URL")
    match = DIRECT_URL_PATTERN.fullmatch(parsed.path)
    if not match:
        raise OperatorError("direct URL must identify one TikTok video or photo")
    return f"https://www.tiktok.com{parsed.path.rstrip('/')}", match.group("post_id")


def _name_token(value: str, *, fallback: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode(
        "ascii", "ignore"
    ).decode("ascii")
    token = re.sub(r"[^a-z0-9]+", "_", normalized.casefold()).strip("_")
    if token:
        return token
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    return f"{fallback}_{digest}"


def _safe_run_label(value: str) -> tuple[str, str]:
    raw = value.strip()
    if not raw:
        return "", ""
    if len(raw) > RUN_LABEL_INPUT_MAX:
        raise OperatorError(
            f"--run-label must be at most {RUN_LABEL_INPUT_MAX} characters"
        )
    if raw in {".", ".."} or any(char in raw for char in ("/", "\\")):
        raise OperatorError("--run-label must be a descriptive name, not a path")
    if any(ord(char) < 32 or ord(char) == 127 for char in raw):
        raise OperatorError("--run-label cannot contain control characters")
    return _name_token(raw, fallback="label"), raw


def _source_target_name(source_mode: str, source_target: str) -> str:
    if source_mode == "url":
        _, post_id = _canonical_direct_url(source_target)
        return post_id
    if source_mode == "creator":
        raw = source_target.strip()
        parsed = urlsplit(raw)
        if parsed.scheme and parsed.netloc:
            segments = [segment for segment in parsed.path.split("/") if segment]
            handle = next(
                (segment[1:] for segment in segments if segment.startswith("@")),
                raw,
            )
        else:
            handle = raw.lstrip("@")
        return _name_token(handle, fallback="creator")
    return _name_token(source_target, fallback="topic")


def _fit_project_name(run_descriptor: str, timestamp: str) -> str:
    prefix = "music_audit_"
    candidate = f"{prefix}{run_descriptor}_{timestamp}"
    if len(candidate) <= NEW_PROJECT_SLUG_MAX:
        return candidate
    descriptor_hash = hashlib.sha256(run_descriptor.encode("utf-8")).hexdigest()[:8]
    fixed = len(prefix) + len(timestamp) + len(descriptor_hash) + 2
    available = NEW_PROJECT_SLUG_MAX - fixed
    shortened = run_descriptor[:available].rstrip("_") or "run"
    return f"{prefix}{shortened}_{descriptor_hash}_{timestamp}"


def _artifact_stem(
    artifact_descriptor: str,
    *,
    project: str,
    timestamp: str,
) -> str:
    date_token = timestamp[2:8]
    project_hash = hashlib.sha256(project.encode("utf-8")).hexdigest()[:8]
    suffix = f"_{date_token}_{project_hash}"
    available = ARTIFACT_STEM_MAX - len(suffix)
    shortened = artifact_descriptor[:available].rstrip("_") or "run"
    stem = f"{shortened}{suffix}"
    if not ARTIFACT_STEM_PATTERN.fullmatch(stem):
        raise OperatorError("generated MUSIC AUDIT artifact stem is invalid")
    return stem


def automatic_run_naming(
    *,
    source_mode: str,
    source_target: str,
    requested_count: int,
    all_posts: bool,
    collection_policy: str,
    run_label_input: str = "",
    timestamp: str | None = None,
) -> MusicAuditRunNaming:
    label, raw_label = _safe_run_label(run_label_input)
    target = _source_target_name(source_mode, source_target)
    cardinality = "all" if all_posts else f"{requested_count}p"
    policy = "refresh" if collection_policy == "refresh_known" else "new"
    descriptor_parts = [
        value
        for value in (label, policy, source_mode, target, cardinality)
        if value
    ]
    run_descriptor = "_".join(descriptor_parts)
    stamp = timestamp or dt.datetime.now().astimezone().strftime(
        "%Y%m%d_%H%M%S_%f"
    )
    if not re.fullmatch(r"[0-9]{8}_[0-9]{6}_[0-9]{6}", stamp):
        raise OperatorError("MUSIC AUDIT naming timestamp is invalid")
    project = _fit_project_name(run_descriptor, stamp)
    artifact_parts = [
        value
        for value in (
            "refresh" if collection_policy == "refresh_known" else "",
            label or source_mode,
            cardinality,
            target,
        )
        if value
    ]
    artifact_descriptor = "_".join(artifact_parts)
    return MusicAuditRunNaming(
        project=project,
        artifact_stem=_artifact_stem(
            artifact_descriptor,
            project=project,
            timestamp=stamp,
        ),
        run_label=label,
        run_label_input=raw_label,
        run_descriptor=run_descriptor,
    )


def explicit_project_naming(
    project: str,
    *,
    timestamp: str | None = None,
) -> MusicAuditRunNaming:
    safe_project = _safe_project_slug(project)
    stamp = timestamp or dt.datetime.now().astimezone().strftime(
        "%Y%m%d_%H%M%S_%f"
    )
    descriptor = _name_token(
        safe_project.removeprefix("music_audit_"),
        fallback="run",
    )
    return MusicAuditRunNaming(
        project=safe_project,
        artifact_stem=_artifact_stem(
            descriptor,
            project=safe_project,
            timestamp=stamp,
        ),
        run_label="",
        run_label_input="",
        run_descriptor=descriptor,
    )


def validate_start_scope(args: argparse.Namespace) -> tuple[str, str, int, bool]:
    if str(getattr(args, "project", "") or "").strip() and str(
        getattr(args, "run_label", "") or ""
    ).strip():
        raise OperatorError("use either --project or --run-label, not both")
    if int(args.max_comments) <= 0:
        raise OperatorError("--max-comments must be positive")
    if int(args.max_pages or 0) < 0:
        raise OperatorError("--max-pages cannot be negative")
    if float(args.browser_startup_timeout) <= 0:
        raise OperatorError("--browser-startup-timeout must be positive")
    sources = [
        ("topic", str(args.topic or "").strip()),
        ("creator", str(args.creator or "").strip()),
        ("url", str(args.url or "").strip()),
    ]
    selected = [(mode, value) for mode, value in sources if value]
    if len(selected) != 1:
        raise OperatorError("select exactly one of --topic, --creator, or --url")
    source_mode, source_target = selected[0]
    all_posts = bool(args.all_posts)
    posts = int(args.posts or 0)
    if all_posts:
        if source_mode != "creator" or args.collection_policy != "new_only":
            raise OperatorError("--all-posts requires a new_only creator source")
        if posts:
            raise OperatorError("do not combine --all-posts and --posts")
        if int(args.max_pages or 0) > 0:
            raise OperatorError(
                "--all-posts cannot be combined with --max-pages; ALL requires "
                "an uncapped verified terminal creator frontier"
            )
    elif posts <= 0:
        raise OperatorError("--posts must be a positive integer")
    if source_mode == "url":
        source_target, _ = _canonical_direct_url(source_target)
        if posts != 1 or all_posts:
            raise OperatorError("direct URL MUSIC AUDIT requires --posts 1")
    if args.collection_policy == "new_only" and (
        args.refresh_post_id or args.refresh_stale_before
    ):
        raise OperatorError("refresh selectors are invalid under new_only")
    if args.collection_policy == "refresh_known" and all_posts:
        raise OperatorError("refresh_known does not accept --all-posts")
    return source_mode, source_target, posts, all_posts


def freeze_start_options(
    args: argparse.Namespace,
    *,
    source_mode: str,
    source_target: str,
    requested_count: int,
) -> None:
    refresh_ids = list(
        dict.fromkeys(
            str(value).strip() for value in args.refresh_post_id if str(value).strip()
        )
    )
    if any(not value.isdigit() for value in refresh_ids):
        raise OperatorError("--refresh-post-id must be a numeric TikTok ID")
    if source_mode == "url" and args.collection_policy == "refresh_known":
        _, direct_id = _canonical_direct_url(source_target)
        if refresh_ids and refresh_ids != [direct_id]:
            raise OperatorError(
                "direct URL refresh cannot select a different --refresh-post-id"
            )
        refresh_ids = [direct_id]
    refresh_stale_before = str(args.refresh_stale_before or "").strip()
    if (
        args.collection_policy == "refresh_known"
        and source_mode != "url"
        and not refresh_ids
        and not refresh_stale_before
    ):
        refresh_stale_before = (
            (dt.datetime.now().astimezone() - dt.timedelta(hours=24))
            .replace(microsecond=0)
            .isoformat()
        )
    if refresh_stale_before:
        try:
            dt.datetime.fromisoformat(refresh_stale_before.replace("Z", "+00:00"))
        except ValueError as exc:
            raise OperatorError(
                "--refresh-stale-before must be an ISO-8601 timestamp"
            ) from exc
    args.refresh_post_id = refresh_ids
    args.refresh_stale_before = refresh_stale_before
    supplied_max_pages = int(args.max_pages or 0)
    if supplied_max_pages:
        resolved_max_pages = supplied_max_pages
    elif source_mode == "topic":
        resolved_max_pages = max(3, math.ceil(requested_count / 12) * 4)
    elif source_mode == "url":
        resolved_max_pages = 1
    else:
        resolved_max_pages = 0
    args.resolved_max_pages = resolved_max_pages


def refuse_automatic_duplicate_start(
    args: argparse.Namespace,
    *,
    source_mode: str,
    source_target: str,
    requested_count: int,
    all_posts: bool,
    paths: OperatorPaths,
) -> None:
    """Keep an automatic start from replacing an unfinished identical run."""

    comments_root = (paths.workspace / "comments_data").resolve()
    if not comments_root.is_dir():
        return
    expected = {
        "source_mode": source_mode,
        "source_target": source_target,
        "requested_count": requested_count,
        "all_posts": all_posts,
        "collection_policy": args.collection_policy,
        "max_comments": int(args.max_comments),
        "resolved_max_pages": int(args.resolved_max_pages),
        "refresh_stale_before": str(args.refresh_stale_before or ""),
        "refresh_post_ids": [str(value) for value in args.refresh_post_id or ()],
        "expected_account": str(args.expected_account or "").strip().lstrip("@"),
    }
    handoff_paths = {
        path.resolve()
        for pattern in (
            "project_*/music_audit_operator_handoff.json",
            "project_*/*_handoff.json",
        )
        for path in comments_root.glob(pattern)
    }
    for handoff_path in sorted(handoff_paths):
        try:
            existing = load_handoff(handoff_path, paths)
        except OperatorError:
            continue
        intent = existing.get("intent")
        if not isinstance(intent, Mapping):
            continue
        if any(intent.get(key) != value for key, value in expected.items()):
            continue
        complete = existing.get("state") == "complete"
        if not complete:
            raise OperatorError(
                "unfinished matching MUSIC AUDIT exists; resume its handoff: "
                + str(handoff_path.resolve())
            )


def build_start_arguments(
    handoff: Mapping[str, Any],
) -> list[str]:
    intent = handoff.get("intent")
    if not isinstance(intent, Mapping):
        raise OperatorError("MUSIC AUDIT handoff is missing immutable intent")
    if handoff.get("all_posts") is True and int(intent.get("max_pages") or 0) > 0:
        raise OperatorError(
            "guarded creator ALL handoff contains a prohibited finite page bound"
        )
    result = [
        "--database",
        str(handoff["database"]),
        "--master-database",
        str(handoff["master_database"]),
        "music-audit",
        "--project",
        str(handoff["project"]),
        f"--{handoff['source_mode']}",
        str(handoff["source_target"]),
    ]
    if handoff["all_posts"]:
        result.append("--all-posts")
    else:
        result.extend(("--posts", str(handoff["requested_count"])))
    result.extend(
        (
            "--max-comments",
            str(handoff["max_comments"]),
            "--collection-policy",
            str(handoff["collection_policy"]),
            "--browser-startup-timeout",
            str(handoff["browser_startup_timeout"]),
        )
    )
    if int(intent.get("max_pages") or 0) > 0:
        result.extend(("--max-pages", str(int(intent["max_pages"]))))
    if handoff.get("expected_account"):
        result.extend(("--expected-account", str(handoff["expected_account"])))
    refresh_stale_before = str(intent.get("refresh_stale_before") or "")
    if refresh_stale_before:
        result.extend(("--refresh-stale-before", refresh_stale_before))
    refresh_post_ids = intent.get("refresh_post_ids") or ()
    if not isinstance(refresh_post_ids, Sequence) or isinstance(
        refresh_post_ids, (str, bytes)
    ):
        raise OperatorError("immutable refresh post selection is invalid")
    for post_id in refresh_post_ids:
        if not str(post_id).isdigit():
            raise OperatorError("--refresh-post-id must be a numeric TikTok ID")
        result.extend(("--refresh-post-id", str(post_id)))
    return result


def build_resume_arguments(handoff: Mapping[str, Any]) -> list[str]:
    return [
        "--database",
        str(handoff["database"]),
        "--master-database",
        str(handoff["master_database"]),
        "resume-collect",
        "--run-id",
        str(handoff["run_id"]),
        "--browser-startup-timeout",
        str(handoff["browser_startup_timeout"]),
    ]


def new_handoff(
    *,
    project: str,
    source_mode: str,
    source_target: str,
    requested_count: int,
    all_posts: bool,
    args: argparse.Namespace,
    paths: OperatorPaths,
    naming: MusicAuditRunNaming | None = None,
    project_layout_schema: str = PROJECT_LAYOUT_SCHEMA,
) -> tuple[Path, dict[str, Any]]:
    if project_layout_schema == PROJECT_LAYOUT_SCHEMA:
        if naming is None:
            stamp = dt.datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")
            descriptor = _name_token(project, fallback="run")
            naming = MusicAuditRunNaming(
                project=project,
                artifact_stem=_artifact_stem(
                    descriptor,
                    project=project,
                    timestamp=stamp,
                ),
                run_label="",
                run_label_input="",
                run_descriptor=descriptor,
            )
        if naming.project != project:
            raise OperatorError("MUSIC AUDIT naming project does not match the handoff")
        layout = _semantic_project_layout(paths, project, naming.artifact_stem)
    elif project_layout_schema == LEGACY_PROJECT_LAYOUT_SCHEMA:
        if naming is not None:
            raise OperatorError("legacy MUSIC AUDIT layout cannot use semantic naming")
        layout = _legacy_project_layout(paths, project)
        naming = MusicAuditRunNaming(
            project=project,
            artifact_stem="",
            run_label="",
            run_label_input="",
            run_descriptor="",
        )
    else:
        raise OperatorError("unsupported MUSIC AUDIT project layout schema")
    project_root = layout.project_root
    database = layout.database
    handoff_file = layout.handoff_file
    if project_root.exists() or database.exists() or handoff_file.exists():
        raise OperatorError(
            "new operator start refuses an existing project; resume its handoff instead"
        )
    created = now_iso()
    intent = {
        "project": project,
        "project_layout_schema": project_layout_schema,
        "artifact_stem": naming.artifact_stem,
        "run_label": naming.run_label,
        "run_label_input": naming.run_label_input,
        "run_descriptor": naming.run_descriptor,
        "source_mode": source_mode,
        "source_target": source_target,
        "requested_count": requested_count,
        "all_posts": all_posts,
        "collection_policy": args.collection_policy,
        "max_comments": int(args.max_comments),
        "max_pages": int(args.max_pages or 0),
        "resolved_max_pages": int(args.resolved_max_pages),
        "music_catalogs": ["musicbrainz"],
        "refresh_stale_before": str(args.refresh_stale_before or ""),
        "refresh_post_ids": [str(value) for value in args.refresh_post_id or ()],
        "expected_account": str(args.expected_account or "").strip().lstrip("@"),
        "browser_startup_timeout": float(args.browser_startup_timeout),
        "database": str(database),
        "master_database": str(paths.master_database),
    }
    handoff: dict[str, Any] = {
        "schema_version": HANDOFF_SCHEMA,
        "workspace": str(paths.workspace),
        "python": str(paths.python),
        "engage_script": str(paths.engage_script),
        "project": project,
        "project_layout_schema": project_layout_schema,
        "artifact_stem": naming.artifact_stem,
        "run_label": naming.run_label,
        "run_label_input": naming.run_label_input,
        "run_descriptor": naming.run_descriptor,
        "project_root": str(project_root),
        "database": str(database),
        "master_database": str(paths.master_database),
        "handoff_file": str(handoff_file),
        "ledger_file": str(layout.ledger_file),
        "export_file": str(layout.export_file),
        "review_json": str(layout.review_json),
        "review_markdown": str(layout.review_markdown),
        "intent": intent,
        "intent_hash": json_hash(intent),
        "run_id": "",
        "frozen_selection": {},
        "frozen_selection_hash": "",
        "source_mode": source_mode,
        "source_target": source_target,
        "requested_count": requested_count,
        "all_posts": all_posts,
        "collection_policy": args.collection_policy,
        "max_comments": int(args.max_comments),
        "browser_startup_timeout": float(args.browser_startup_timeout),
        "expected_account": str(args.expected_account or "").strip().lstrip("@"),
        "state": "created",
        "last_status": {},
        "initial_attempts": 0,
        "resume_attempts_by_epoch": {"0": 0},
        "recovery_epoch": 0,
        "restart_count": 0,
        "restart_pending": False,
        "restart_reason": "",
        "restart_phase": "none",
        "restart_boot_marker": {},
        "confirmed_restart_count": 0,
        "restart_cancellations": [],
        "execution": {},
        "execution_history": [],
        "ledger_sequence": 0,
        "ledger_head_hash": "",
        "artifacts": {},
        "created_at": created,
        "updated_at": created,
        "handoff_hash": "",
    }
    write_handoff(handoff_file, handoff)
    return handoff_file, handoff


def _resolve_run_id_after_attempt(
    handoff: dict[str, Any],
    result: CommandResult,
    *,
    paths: OperatorPaths,
) -> None:
    emitted = str(result.payload.get("run_id") or "")
    if emitted and not RUN_ID_PATTERN.fullmatch(emitted):
        raise OperatorError("collector emitted an invalid run ID")
    discovered = discover_run_id(
        Path(str(handoff["database"])), str(handoff["project"])
    )
    if emitted and emitted != discovered:
        raise OperatorError("emitted run ID does not match durable project state")
    handoff["run_id"] = discovered
    capture_frozen_selection(handoff)
    validate_run_intent(handoff, paths=paths)
    write_handoff(Path(str(handoff["handoff_file"])), handoff)


def ensure_handoff_run_binding(
    handoff: dict[str, Any],
    *,
    paths: OperatorPaths,
) -> None:
    if not handoff.get("run_id"):
        handoff["run_id"] = discover_run_id(
            Path(str(handoff["database"])), str(handoff["project"])
        )
    capture_frozen_selection(handoff)
    validate_run_intent(handoff, paths=paths)
    write_handoff(Path(str(handoff["handoff_file"])), handoff)


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _required_sha(value: Any, field: str) -> str:
    supplied = str(value or "").casefold()
    if not SHA256_PATTERN.fullmatch(supplied):
        raise OperatorError(f"{field} is not a SHA-256 hash")
    return supplied


def _json_object(value: Any, field: str) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError as exc:
        raise OperatorError(f"{field} is invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise OperatorError(f"{field} must be a JSON object")
    return parsed


def _read_export(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    raise OperatorError(
                        f"evidence export contains a blank row at {line_number}"
                    )
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise OperatorError("evidence export rows must be JSON objects")
                rows.append(value)
    except json.JSONDecodeError as exc:
        raise OperatorError("evidence export contains invalid JSON") from exc
    return rows


def _walk_forbidden(value: Any, path: str = "") -> None:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key)
            key_folded = key.casefold()
            child_path = f"{path}.{key}" if path else key
            if key_folded in FORBIDDEN_EXPORT_KEYS:
                raise OperatorError(f"forbidden transport/session field: {child_path}")
            if (
                (key_folded.endswith("_url") or key_folded.endswith("_uri"))
                and key_folded != "url"
                and child_path not in SAFE_EXPORT_URL_PATHS
            ):
                raise OperatorError(f"unexpected transport URL field: {child_path}")
            _walk_forbidden(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _walk_forbidden(child, f"{path}[{index}]")
    elif isinstance(value, str):
        folded = value.casefold()
        if "ws://" in folded or "wss://" in folded:
            raise OperatorError(f"CDP/WebSocket endpoint present at {path}")
        if re.search(r"(?i)(sessionid|mstoken)\s*=", value):
            raise OperatorError(f"session credential present at {path}")


def _validate_music(packet: Mapping[str, Any], engage_module: Any) -> dict[str, Any]:
    music = packet.get("music_evidence")
    if not isinstance(music, Mapping):
        raise OperatorError("evidence packet is missing music_evidence")
    supplied_hash = _required_sha(music.get("music_evidence_hash"), "music hash")
    if supplied_hash != json_hash(_without_hash(music, "music_evidence_hash")):
        raise OperatorError("music_evidence hash validation failed")
    platform = music.get("platform_music")
    if (
        not isinstance(platform, Mapping)
        or str(platform.get("status") or "") not in MUSIC_PLATFORM_TERMINAL
    ):
        raise OperatorError("TikTok platform music outcome is not terminal")
    contained = music.get("platform_contained_recording")
    if (
        not isinstance(contained, Mapping)
        or str(contained.get("status") or "") not in MUSIC_PLATFORM_TERMINAL
    ):
        raise OperatorError("contained-recording outcome is not terminal")
    tt2dsp = music.get("tt2dsp_resolution")
    if (
        not isinstance(tt2dsp, Mapping)
        or str(tt2dsp.get("status") or "") not in TT2DSP_TERMINAL
    ):
        raise OperatorError("tt2dsp resolution outcome is not terminal")
    configured = music.get("configured_catalogs")
    catalogs = music.get("catalogs")
    if not isinstance(configured, list) or not configured:
        raise OperatorError("music catalog scope is missing")
    if not isinstance(catalogs, Mapping):
        raise OperatorError("music catalog outcomes are missing")
    catalog_summary: dict[str, str] = {}
    for provider in configured:
        provider_name = str(provider or "").casefold()
        entry = catalogs.get(provider_name)
        if not isinstance(entry, Mapping):
            raise OperatorError(f"catalog outcome is missing: {provider_name}")
        status = str(entry.get("status") or "").casefold()
        if status not in MUSIC_CATALOG_TERMINAL:
            raise OperatorError(f"catalog outcome is not terminal: {provider_name}")
        result = entry.get("result")
        if (
            not isinstance(result, Mapping)
            or str(result.get("status") or "").casefold() != status
        ):
            raise OperatorError(f"catalog result binding failed: {provider_name}")
        result_hash = _required_sha(
            entry.get("result_hash"), f"{provider_name} result hash"
        )
        embedded_hash = str(
            result.get("enrichment_hash")
            or result.get("result_hash")
            or result.get("resolution_hash")
            or ""
        ).casefold()
        if embedded_hash != result_hash:
            raise OperatorError(f"catalog result hash binding failed: {provider_name}")
        catalog_summary[provider_name] = status
    acoustic = music.get("acoustic_verification")
    if (
        not isinstance(acoustic, Mapping)
        or acoustic.get("status") != "not_attempted"
        or acoustic.get("verified") is not False
    ):
        raise OperatorError(
            "MUSIC AUDIT must leave acoustic verification not_attempted"
        )
    lyrics = music.get("lyrics")
    if not isinstance(lyrics, Mapping) or lyrics.get("status") != "not_attempted":
        raise OperatorError("MUSIC AUDIT must leave lyrics not_attempted")
    issues = engage_module._music_terminality_issues(
        music,
        expected_catalogs=configured,
    )
    safe_music, safe_issues = engage_module.sanitized_terminal_music_evidence(
        music,
        expected_catalogs=configured,
    )
    issues.extend(safe_issues)
    if issues:
        raise OperatorError(
            "canonical music validation failed: "
            + ", ".join(dict.fromkeys(map(str, issues)))
        )
    if canonical_json(safe_music) != canonical_json(music):
        raise OperatorError(
            "exported music evidence is not the canonical sanitized form"
        )
    return {
        "platform_music": str(platform.get("status") or ""),
        "contained_recording": str(contained.get("status") or ""),
        "tt2dsp_resolution": str(tt2dsp.get("status") or ""),
        "catalogs": catalog_summary,
        "identity_status": str(music.get("identity_status") or ""),
        "music_evidence_hash": supplied_hash,
    }


def _validate_export_row(
    row: Mapping[str, Any],
    *,
    run: Mapping[str, Any],
    engage_module: Any,
) -> dict[str, Any]:
    if set(row) != EXPORT_KEYS:
        raise OperatorError("evidence export outer field set is invalid")
    if row.get("schema_version") != EXPORT_SCHEMA:
        raise OperatorError("evidence export schema is invalid")
    for field in ("run_id", "project", "source_mode", "collection_policy"):
        expected = str(run.get(field) or "")
        if str(row.get(field) or "") != expected:
            raise OperatorError(f"evidence export {field} binding failed")
    post_id = str(row.get("post_id") or "")
    if not post_id.isdigit():
        raise OperatorError("evidence export post ID is invalid")
    evidence_hash = _required_sha(row.get("evidence_hash"), "evidence hash")
    packet_hash = _required_sha(
        row.get("evidence_projection_hash"), "evidence projection hash"
    )
    packet = row.get("evidence_packet")
    if not isinstance(packet, Mapping):
        raise OperatorError("evidence export packet is missing")
    if packet.get("schema_version") != "tiktok-engage-ai-evidence-v1":
        raise OperatorError("evidence projection schema is invalid")
    if json_hash(packet) != packet_hash:
        raise OperatorError("evidence projection hash validation failed")
    if str(packet.get("source_evidence_hash") or "") != evidence_hash:
        raise OperatorError("projection-to-raw evidence binding failed")
    if str(packet.get("post_id") or "") != post_id:
        raise OperatorError("projection post ID binding failed")
    if packet.get("evidence_ready") is not True or packet.get(
        "readiness_issues"
    ) not in ([], None):
        raise OperatorError("exported evidence is not ready")
    creator = str(packet.get("creator") or "").strip().lstrip("@")
    if not creator:
        raise OperatorError("exported evidence is missing its creator")
    url, url_post_id = _canonical_direct_url(str(packet.get("url") or ""))
    if url_post_id != post_id or url != str(packet.get("url") or ""):
        raise OperatorError("canonical post URL binding failed")
    if not str(packet.get("caption_status") or ""):
        raise OperatorError("caption outcome is missing")
    if not isinstance(packet.get("metrics"), Mapping):
        raise OperatorError("current public metrics are missing")
    if not str(packet.get("transcript_status") or ""):
        raise OperatorError("transcript/subtitle outcome is missing")
    comments_status = packet.get("comments_status")
    if not isinstance(comments_status, Mapping) or not isinstance(
        comments_status.get("ok"), bool
    ):
        raise OperatorError("comments/replies outcome is missing")
    if not isinstance(packet.get("provenance"), Mapping):
        raise OperatorError("evidence provenance is missing")
    _walk_forbidden(row)
    music_summary = _validate_music(packet, engage_module)
    return {
        "post_id": post_id,
        "creator": creator,
        "url": url,
        "content_type": str(packet.get("content_type") or ""),
        "caption_status": str(packet.get("caption_status") or ""),
        "comments": {
            "ok": comments_status.get("ok"),
            "complete": comments_status.get("complete"),
            "limit_reached": comments_status.get("limit_reached"),
            "top_level_count": packet.get("top_level_comment_count"),
            "comment_and_reply_count": packet.get("comment_and_reply_count"),
        },
        "transcript": {
            "status": str(packet.get("transcript_status") or ""),
            "language": str(packet.get("transcript_language") or ""),
            "segment_count": len(packet.get("transcript_segments") or []),
            "subtitle_track_count": len(packet.get("subtitle_tracks") or []),
        },
        "music": music_summary,
        "evidence_hash": evidence_hash,
        "evidence_projection_hash": packet_hash,
    }


def _load_engage_module(paths: OperatorPaths) -> Any:
    workspace = str(paths.workspace)
    if workspace not in sys.path:
        sys.path.insert(0, workspace)
    try:
        import engage_tiktok  # type: ignore
    except Exception as exc:
        raise OperatorError("cannot load canonical evidence validators") from exc
    return engage_tiktok


def _json_array(value: Any, field: str) -> list[Any]:
    try:
        parsed = json.loads(str(value or "[]"))
    except json.JSONDecodeError as exc:
        raise OperatorError(f"{field} is invalid JSON") from exc
    if not isinstance(parsed, list):
        raise OperatorError(f"{field} must be a JSON array")
    return parsed


def _validate_local_source_scope(
    run: Mapping[str, Any],
    local_by_post: Mapping[str, Mapping[str, Any]],
    engage_module: Any,
) -> None:
    source_mode = str(run["source_mode"] or "")
    if source_mode == "url":
        expected_url, expected_id = _canonical_direct_url(
            str(run["direct_post_url"] or "")
        )
        if set(local_by_post) != {expected_id}:
            raise OperatorError("direct URL evidence post ID drifted from source")
        packet = local_by_post[expected_id]["evidence_json"]
        if str(packet.get("url") or "") != expected_url:
            raise OperatorError("direct URL evidence URL drifted from source")
        expected_handle = (
            urlsplit(expected_url).path.split("/")[1].lstrip("@").casefold()
        )
        expected_content_type = "photo" if "/photo/" in expected_url else "video"
        validation = packet.get("direct_source_validation")
        if not isinstance(validation, Mapping):
            raise OperatorError("direct URL evidence validation is missing")
        required = {
            "required": True,
            "matched": True,
            "expected_post_id": expected_id,
            "observed_post_id": expected_id,
            "expected_url": expected_url,
            "observed_url": expected_url,
            "expected_handle": expected_handle,
            "observed_handle": expected_handle,
            "expected_content_type": expected_content_type,
            "observed_content_type": expected_content_type,
        }
        for field, expected in required.items():
            if validation.get(field) != expected:
                raise OperatorError(f"direct URL evidence validation drifted: {field}")
        if str(packet.get("creator") or "").lstrip("@").casefold() != expected_handle:
            raise OperatorError("direct URL evidence creator drifted from source")
        return

    if source_mode != "creator":
        return
    expected_handle = str(run["creator_handle"] or "").lstrip("@").casefold()
    if not expected_handle:
        raise OperatorError("creator source is missing its bound handle")
    for post in local_by_post.values():
        packet = post["evidence_json"]
        if str(packet.get("creator") or "").lstrip("@").casefold() != expected_handle:
            raise OperatorError("creator evidence owner drifted from source")
        validation = packet.get("creator_source_validation")
        if (
            not isinstance(validation, Mapping)
            or validation.get("required") is not True
            or validation.get("matched") is not True
            or str(validation.get("expected_handle") or "").casefold()
            != expected_handle
            or str(validation.get("observed_handle") or "").casefold()
            != expected_handle
        ):
            raise OperatorError("creator evidence validation drifted from source")
    if str(run["collection_policy"] or "") != "new_only":
        return
    if int(run["profile_inventory_terminal"] or 0) != 1:
        raise OperatorError("creator profile inventory is not terminal")
    inventory = _json_array(run["creator_inventory_json"], "creator inventory")
    selected_ids = _json_array(
        run["creator_selected_post_ids_json"], "creator selected IDs"
    )
    identity = _json_object(run["creator_identity_json"], "creator identity")
    if str(identity.get("handle") or "").casefold() != expected_handle:
        raise OperatorError("creator inventory identity drifted from source")
    if int(run["profile_inventory_count"] or 0) != len(inventory):
        raise OperatorError("creator inventory count does not match its rows")
    inventory_ids: list[str] = []
    for candidate in inventory:
        if not isinstance(candidate, Mapping):
            raise OperatorError("creator inventory contains a non-object row")
        post_id = str(engage_module.extract_post_id(candidate) or "")
        owner = (
            str(
                candidate.get("username")
                or candidate.get("creator")
                or candidate.get("content_creator")
                or ""
            )
            .lstrip("@")
            .casefold()
        )
        if not post_id or owner != expected_handle:
            raise OperatorError("creator inventory candidate identity drifted")
        canonical_url = str(
            engage_module.canonical_tiktok_url(dict(candidate), post_id) or ""
        )
        content_type = str(candidate.get("content_type") or "").casefold()
        if content_type not in {"video", "photo"}:
            content_type = "photo" if "/photo/" in canonical_url else "video"
        expected_url = (
            f"https://www.tiktok.com/@{expected_handle}/{content_type}/{post_id}"
        )
        if canonical_url.casefold() != expected_url.casefold():
            raise OperatorError("creator inventory candidate URL drifted")
        inventory_ids.append(post_id)
    if len(inventory_ids) != len(set(inventory_ids)):
        raise OperatorError("creator inventory contains duplicate post IDs")
    if any(not isinstance(value, str) or not value for value in selected_ids):
        raise OperatorError("creator selected IDs are invalid")
    if len(selected_ids) != len(set(selected_ids)):
        raise OperatorError("creator selected IDs contain duplicates")
    if not set(selected_ids).issubset(inventory_ids):
        raise OperatorError("creator selected IDs are outside the inventory")
    if not set(local_by_post).issubset(selected_ids):
        raise OperatorError("creator evidence is outside the frozen selection")
    cardinality = str(run["cardinality_mode"] or "")
    requested = int(run["requested_count"] or 0)
    if cardinality == "all":
        if requested != len(selected_ids) or set(local_by_post) != set(selected_ids):
            raise OperatorError("creator ALL evidence drifted from frozen selection")
    elif cardinality != "fixed" or len(selected_ids) < requested:
        raise OperatorError("creator fixed-count selection is invalid")
    snapshot = {
        "creator_handle": expected_handle,
        "creator_identity": identity,
        "terminal": True,
        "candidates": inventory,
        "selected_post_ids": selected_ids,
    }
    if json_hash(snapshot) != str(run["profile_inventory_hash"] or ""):
        raise OperatorError("creator profile inventory hash is invalid")


def _creator_inventory_coverage(
    local: sqlite3.Connection,
    run: Mapping[str, Any],
) -> dict[str, Any]:
    """Return closed creator-inventory proof fields for the machine review."""

    if (
        str(run.get("source_mode") or "") != "creator"
        or str(run.get("collection_policy") or "") != "new_only"
    ):
        return {}
    inventory = _json_array(run.get("creator_inventory_json"), "creator inventory")
    selected_ids = _json_array(
        run.get("creator_selected_post_ids_json"), "creator selected IDs"
    )
    inventory_count = int(run.get("profile_inventory_count") or 0)
    selected_count = len(selected_ids)
    coverage: dict[str, Any] = {
        "cardinality_mode": str(run.get("cardinality_mode") or ""),
        "terminal_verified": bool(run.get("profile_inventory_terminal")),
        "inventory_complete": None,
        "has_more": None,
        "frontier_stop_reason": "",
        "source_exhausted": None,
        "limit_reached": None,
        "unique_owner_posts_observed": None,
        "inventory_count": inventory_count,
        "selected_new_count": selected_count,
        "new_only_excluded_count": max(0, len(inventory) - selected_count),
        "page_bound": int(run.get("max_pages") or 0),
        "uncapped": int(run.get("max_pages") or 0) == 0,
    }
    event_row = local.execute(
        """
        SELECT payload_json
        FROM engage_tiktok_events
        WHERE run_id=? AND stage='collection' AND event='complete'
        ORDER BY event_id DESC
        LIMIT 1
        """,
        (str(run.get("run_id") or ""),),
    ).fetchone()
    diagnostics: Mapping[str, Any] = {}
    if event_row is not None:
        payload = _json_object(event_row["payload_json"], "collection completion event")
        raw_diagnostics = payload.get("diagnostics")
        if isinstance(raw_diagnostics, Mapping):
            diagnostics = raw_diagnostics
    capture = diagnostics.get("capture")
    capture = capture if isinstance(capture, Mapping) else {}
    has_more = diagnostics.get("has_more")
    if has_more is None:
        has_more = capture.get("latest_has_more")
    frontier_stop_reason = str(
        diagnostics.get("stop_reason")
        or capture.get("cursor_chain_stop_reason")
        or capture.get("stop_reason")
        or ""
    )
    observed_count = diagnostics.get("unique_owner_posts_observed")
    try:
        observed_count = int(observed_count)
    except (TypeError, ValueError):
        observed_count = None
    coverage.update(
        {
            "terminal_verified": (
                diagnostics.get("terminal_verified") is True
                if "terminal_verified" in diagnostics
                else bool(run.get("profile_inventory_terminal"))
            ),
            "inventory_complete": (
                diagnostics.get("inventory_complete") is True
                if "inventory_complete" in diagnostics
                else bool(run.get("profile_inventory_terminal"))
            ),
            "has_more": has_more,
            "frontier_stop_reason": frontier_stop_reason,
            "source_exhausted": diagnostics.get("source_exhausted") is True,
            "limit_reached": diagnostics.get("limit_reached"),
            "unique_owner_posts_observed": observed_count,
        }
    )
    if coverage["cardinality_mode"] == "all":
        required = (
            coverage["terminal_verified"] is True
            and coverage["inventory_complete"] is True
            and coverage["has_more"] is False
            and coverage["frontier_stop_reason"] == "source_exhausted"
            and coverage["source_exhausted"] is True
            and coverage["limit_reached"] is False
            and coverage["unique_owner_posts_observed"] == inventory_count
        )
        if not required:
            raise OperatorError(
                "creator ALL completion lacks verified terminal inventory diagnostics"
            )
    return coverage


def validate_completed_artifacts(
    handoff: Mapping[str, Any],
    *,
    paths: OperatorPaths,
) -> dict[str, Any]:
    database = Path(str(handoff["database"])).resolve()
    master_database = Path(str(handoff["master_database"])).resolve()
    export_file = Path(str(handoff["export_file"])).resolve()
    run_id = str(handoff["run_id"])
    validate_run_intent(handoff, paths=paths)
    engage_module = _load_engage_module(paths)
    local = readonly_connect(database)
    try:
        run_row = local.execute(
            "SELECT * FROM engage_tiktok_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if run_row is None:
            raise OperatorError("local workflow run is missing")
        run = _row_dict(run_row)
        if (
            run["workflow"] != "listen"
            or run["mode"] != "shadow"
            or run["status"] != "collection_complete"
        ):
            raise OperatorError(
                "MUSIC AUDIT validation requires completed LISTEN state"
            )
        if run["collection_attempt_id"]:
            raise OperatorError("collection still has an active attempt")
        if str(run["error"] or ""):
            raise OperatorError("completed MUSIC AUDIT retains a run error")
        requested = int(run["requested_count"])
        if (
            int(run["requested"]) != requested
            or int(run["evidence_ready"]) != requested
        ):
            raise OperatorError("exact-count completion gate failed")
        for field in ZERO_STAGE_COUNTERS:
            if int(run[field] or 0) != 0:
                raise OperatorError(
                    f"prohibited downstream counter is nonzero: {field}"
                )
        if str(run["project"]) != str(handoff["project"]):
            raise OperatorError("handoff project binding failed")
        if str(run["source_mode"]) != str(handoff["source_mode"]):
            raise OperatorError("handoff source binding failed")
        if str(run["collection_policy"]) != str(handoff["collection_policy"]):
            raise OperatorError("handoff collection policy binding failed")
        if Path(str(run["master_database"])).resolve() != master_database:
            raise OperatorError("local run master-database binding failed")
        browser = _json_object(run["browser_preflight_json"], "browser preflight")
        profile = browser.get("profile")
        if (
            browser.get("reachable") is not True
            or browser.get("browser_endpoint_configured") is not True
            or browser.get("tiktok_authenticated") is not True
            or not isinstance(profile, Mapping)
            or profile.get("verified") is not True
            or profile.get("profile_directory") != "Profile 7"
            or profile.get("mode") != "existing_profile_attach"
        ):
            raise OperatorError("stored Profile 7 preflight is not complete")
        if not str(run["observed_account"] or ""):
            raise OperatorError("stored TikTok account was not resolved")
        if (
            run["expected_account"]
            and run["expected_account"] != run["observed_account"]
        ):
            raise OperatorError("stored expected/observed TikTok account mismatch")
        post_rows = local.execute(
            "SELECT * FROM engage_tiktok_posts WHERE run_id=? ORDER BY post_id",
            (run_id,),
        ).fetchall()
        ready_rows = [row for row in post_rows if int(row["evidence_ready"] or 0) == 1]
        computed_failed = len(post_rows) - len(ready_rows)
        if int(run["unique_collected"] or 0) != len(post_rows):
            raise OperatorError("unique_collected does not match durable post rows")
        if int(run["failed"] or 0) != computed_failed:
            raise OperatorError("failed does not match non-ready durable post rows")
        if len(ready_rows) != requested:
            raise OperatorError("local evidence-ready row count is not exact")
        local_by_post: dict[str, dict[str, Any]] = {}
        for row in ready_rows:
            post_id = str(row["post_id"] or "")
            if row["status"] != "collected" or str(row["collection_error"] or ""):
                raise OperatorError(f"ready post is not terminal collected: {post_id}")
            packet = _json_object(row["evidence_json"], f"local evidence {post_id}")
            evidence_hash = _required_sha(row["evidence_hash"], "local evidence hash")
            if json_hash(packet) != evidence_hash:
                raise OperatorError(f"local raw evidence hash failed: {post_id}")
            if (
                packet.get("evidence_ready") is not True
                or str(packet.get("post_id") or "") != post_id
            ):
                raise OperatorError(f"local raw evidence binding failed: {post_id}")
            local_by_post[post_id] = {
                "evidence_hash": evidence_hash,
                "evidence_json": packet,
            }
        frozen_selection = handoff["frozen_selection"]
        if run["collection_policy"] == "refresh_known":
            frozen_candidate_ids = set(frozen_selection["refresh_candidate_post_ids"])
            if not set(local_by_post).issubset(frozen_candidate_ids):
                raise OperatorError(
                    "refresh evidence is outside the frozen candidate selection"
                )
        _validate_local_source_scope(run, local_by_post, engage_module)
        creator_inventory = _creator_inventory_coverage(local, run)
        for row in post_rows:
            for field in AI_POST_FIELDS:
                if row[field] not in (None, "", "{}"):
                    raise OperatorError(
                        f"prohibited downstream artifact exists: {field}"
                    )
            for field in (
                "analysis_json",
                "review_json",
                "presentation_json",
                "handoff_json",
            ):
                if _json_object(row[field], field):
                    raise OperatorError(
                        f"prohibited downstream JSON artifact exists: {field}"
                    )
            if any(
                row[field] is not None
                for field in (
                    "post_quality_score",
                    "conversation_value_score",
                    "analysis_score",
                )
            ):
                raise OperatorError("prohibited semantic score exists")
        report_count = local.execute(
            "SELECT COUNT(*) FROM engage_tiktok_audit_reports WHERE run_id=?",
            (run_id,),
        ).fetchone()[0]
        if int(report_count) != 0:
            raise OperatorError("MUSIC AUDIT unexpectedly contains an AI audit report")
        bad_event_count = local.execute(
            """
            SELECT COUNT(*) FROM engage_tiktok_events
            WHERE run_id=? AND lower(stage) IN (
                'analysis','draft','review','presentation','authorization',
                'handoff','publication','publish'
            )
            """,
            (run_id,),
        ).fetchone()[0]
        if int(bad_event_count) != 0:
            raise OperatorError("MUSIC AUDIT contains a prohibited downstream event")
        local_tables = {
            str(row[0])
            for row in local.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        downstream_queries = {
            "content analysis state": (
                ("content_analysis_state",),
                "SELECT COUNT(*) FROM content_analysis_state WHERE project=?",
                (run["project"],),
            ),
            "analysis run": (
                ("analysis_runs",),
                "SELECT COUNT(*) FROM analysis_runs WHERE project=?",
                (run["project"],),
            ),
            "fact-check source": (
                ("fact_check_sources", "analysis_runs"),
                """
                SELECT COUNT(*) FROM fact_check_sources f
                JOIN analysis_runs a ON a.analysis_id=f.analysis_id
                WHERE a.project=?
                """,
                (run["project"],),
            ),
            "publication queue": (
                ("publication_queue",),
                """
                SELECT COUNT(*) FROM publication_queue
                WHERE project=? OR engage_run_id=?
                """,
                (run["project"], run_id),
            ),
            "publication receipt": (
                ("publication_receipts", "publication_queue"),
                """
                SELECT COUNT(*) FROM publication_receipts r
                JOIN publication_queue q ON q.publication_id=r.publication_id
                WHERE q.project=? OR q.engage_run_id=?
                """,
                (run["project"], run_id),
            ),
            "publication capture": (
                ("publication_captures",),
                "SELECT COUNT(*) FROM publication_captures WHERE project=?",
                (run["project"],),
            ),
        }
        for artifact, (
            required_tables,
            query,
            parameters,
        ) in downstream_queries.items():
            if not set(required_tables).issubset(local_tables):
                continue
            count = int(local.execute(query, parameters).fetchone()[0])
            if count:
                raise OperatorError(
                    f"MUSIC AUDIT contains a prohibited {artifact} artifact"
                )
    finally:
        local.close()

    exported_rows = _read_export(export_file)
    if len(exported_rows) != requested:
        raise OperatorError("evidence export row count is not exact")
    export_by_post: dict[str, Mapping[str, Any]] = {}
    per_posts: list[dict[str, Any]] = []
    for row in exported_rows:
        summary = _validate_export_row(
            row,
            run=run,
            engage_module=engage_module,
        )
        post_id = summary["post_id"]
        if post_id in export_by_post:
            raise OperatorError("evidence export contains duplicate post IDs")
        if post_id not in local_by_post:
            raise OperatorError("evidence export post is not locally evidence-ready")
        if row["evidence_hash"] != local_by_post[post_id]["evidence_hash"]:
            raise OperatorError("export-to-local evidence hash binding failed")
        expected_projection = engage_module.compact_ai_evidence_projection(
            local_by_post[post_id]["evidence_json"],
            evidence_hash=local_by_post[post_id]["evidence_hash"],
        )
        if canonical_json(row["evidence_packet"]) != canonical_json(
            expected_projection
        ):
            raise OperatorError(
                "export projection does not match canonical raw evidence"
            )
        export_by_post[post_id] = row
        per_posts.append(summary)

    master = readonly_connect(master_database)
    try:
        master_rows = master.execute(
            """
            SELECT r.*, s.database_path
            FROM tiktok_master_runs r
            JOIN tiktok_master_sources s ON s.source_id=r.source_id
            WHERE r.local_run_id=?
            """,
            (run_id,),
        ).fetchall()
        matching = [
            row
            for row in master_rows
            if Path(str(row["database_path"])).resolve() == database
        ]
        if len(matching) != 1:
            raise OperatorError("master registry run binding is missing or ambiguous")
        master_run = matching[0]
        for field in (
            "project",
            "workflow",
            "topic",
            "mode",
            "source_mode",
            "direct_post_url",
            "creator_handle",
            "cardinality_mode",
            "profile_inventory_count",
            "profile_inventory_terminal",
            "profile_inventory_hash",
            "collection_policy",
            "status",
            "requested_count",
            "requested",
            "unique_collected",
            "evidence_ready",
            "analyzed",
            "drafted",
            "reviewed",
            "stored",
            "authorized",
            "published",
            "skipped",
            "failed",
            "expected_account",
            "observed_account",
            "error",
        ):
            if master_run[field] != run[field]:
                raise OperatorError(f"local/master run mismatch: {field}")
        snapshots = master.execute(
            """
            SELECT snapshot_id, post_id, evidence_hash, evidence_json
            FROM tiktok_master_snapshots
            WHERE source_id=? AND local_run_id=?
            ORDER BY post_id
            """,
            (master_run["source_id"], run_id),
        ).fetchall()
        if len(snapshots) != requested:
            raise OperatorError("master snapshot count is not exact")
        snapshot_by_post: dict[str, sqlite3.Row] = {}
        for snapshot in snapshots:
            post_id = str(snapshot["post_id"] or "")
            if post_id not in local_by_post:
                raise OperatorError("master snapshot post is not locally ready")
            evidence_hash = _required_sha(
                snapshot["evidence_hash"], "master evidence hash"
            )
            evidence_json = _json_object(
                snapshot["evidence_json"], "master evidence JSON"
            )
            if json_hash(evidence_json) != evidence_hash:
                raise OperatorError("master snapshot evidence hash failed")
            if evidence_hash != local_by_post[post_id]["evidence_hash"]:
                raise OperatorError("master/local evidence hash binding failed")
            if canonical_json(evidence_json) != canonical_json(
                local_by_post[post_id]["evidence_json"]
            ):
                raise OperatorError("master/local evidence JSON binding failed")
            snapshot_by_post[post_id] = snapshot
        links = master.execute(
            """
            SELECT post_id, snapshot_id, collection_policy, position
            FROM tiktok_master_run_posts
            WHERE master_run_id=?
            ORDER BY post_id
            """,
            (master_run["master_run_id"],),
        ).fetchall()
        if len(links) != requested:
            raise OperatorError("master run-post link count is not exact")
        if sorted(int(link["position"] or 0) for link in links) != list(
            range(1, requested + 1)
        ):
            raise OperatorError("master run-post positions are not contiguous")
        for link in links:
            post_id = str(link["post_id"] or "")
            snapshot = snapshot_by_post.get(post_id)
            if snapshot is None or link["snapshot_id"] != snapshot["snapshot_id"]:
                raise OperatorError("master run-post snapshot binding failed")
            if link["collection_policy"] != run["collection_policy"]:
                raise OperatorError("master run-post policy binding failed")
        master_audit_reports = int(
            master.execute(
                """
                SELECT COUNT(*) FROM tiktok_master_audit_reports
                WHERE local_run_id=?
                """,
                (run_id,),
            ).fetchone()[0]
        )
        if master_audit_reports:
            raise OperatorError("master registry contains a prohibited audit report")
        master_comment_attempts = int(
            master.execute(
                """
                SELECT COUNT(*) FROM tiktok_master_comment_attempts
                WHERE local_run_id=?
                """,
                (run_id,),
            ).fetchone()[0]
        )
        if master_comment_attempts:
            raise OperatorError("master registry contains a prohibited comment attempt")
    finally:
        master.close()

    ledger_path = Path(str(handoff["ledger_file"])).resolve()
    ledger_entries = read_validated_ledger(handoff)
    ledger_records = len(ledger_entries)
    ledger_head = str(handoff.get("ledger_head_hash") or "")
    compliance, deviations = executor_compliance_summary(handoff, ledger_entries)
    return {
        "schema_version": REVIEW_SCHEMA,
        "executor_compliance": compliance,
        "task_outcome": "COMPLETE",
        "generated_at": now_iso(),
        "offline_validation": True,
        "intent": handoff["intent"],
        "intent_hash": handoff["intent_hash"],
        "frozen_selection": handoff["frozen_selection"],
        "frozen_selection_hash": handoff["frozen_selection_hash"],
        "project": run["project"],
        "run_id": run_id,
        "workflow": run["workflow"],
        "source_mode": run["source_mode"],
        "source_target": handoff["source_target"],
        "collection_policy": run["collection_policy"],
        "database": str(database),
        "master_database": str(master_database),
        "interpreter": str(paths.python),
        "runtime": {
            "operator": {
                "path": str(Path(__file__).resolve()),
                "sha256": file_hash(Path(__file__).resolve()),
            },
            "collector": {
                "path": str(paths.engage_script),
                "sha256": file_hash(paths.engage_script),
            },
        },
        "output_layout": output_layout_summary(
            handoff,
            evidence_export_status="validated",
        ),
        "status": status_summary(run),
        "counter_interpretation": {
            "success_gate": f"evidence_ready={requested}/{requested}",
            "unique_collected_may_exceed_requested": True,
            "failed_candidate_history_may_be_nonzero": True,
        },
        "creator_inventory": creator_inventory,
        "deviations": deviations,
        "browser_preflight": {
            "reachable": browser.get("reachable"),
            "tiktok_authenticated": browser.get("tiktok_authenticated"),
            "profile_directory": profile.get("profile_directory"),
            "profile_mode": profile.get("mode"),
            "profile_verified": profile.get("verified"),
            "expected_account": run["expected_account"],
            "observed_account": run["observed_account"],
        },
        "posts": per_posts,
        "artifacts": {
            "evidence_export": {
                "path": str(export_file),
                "schema_version": EXPORT_SCHEMA,
                "records": len(exported_rows),
                "sha256": file_hash(export_file),
            },
            "operator_ledger": {
                "path": str(ledger_path),
                "records": ledger_records,
                "sha256": file_hash(ledger_path),
                "head_hash": ledger_head,
            },
        },
        "restart_handoff": {
            "restart_count": int(handoff.get("restart_count") or 0),
            "recovery_epoch": int(handoff.get("recovery_epoch") or 0),
            "restart_pending": handoff.get("restart_pending") is True,
        },
        "ai_actions": [],
        "outbound_actions": [],
        "final_stopping_reason": "collection_complete_and_export_validated",
    }


def review_markdown(review: Mapping[str, Any]) -> str:
    output_layout = review.get("output_layout") or {}
    if review.get("task_outcome") != "COMPLETE":
        status = review.get("status") or {}
        lines = [
            "# Log File MUSIC AUDIT Review",
            "",
            f"- Executor compliance: **{review['executor_compliance']}**",
            f"- Task outcome: **{review['task_outcome']}**",
            f"- Project: `{review['project']}`",
            f"- Run ID: `{review.get('run_id') or 'not_yet_recovered'}`",
            f"- Source: `{review['source_mode']}` — `{review['source_target']}`",
            f"- Policy: `{review['collection_policy']}`",
            f"- Durable status: `{status.get('status') or 'unknown'}`",
            f"- Evidence-ready: `{status.get('evidence_ready', 0)}/{status.get('requested', review['intent'].get('requested_count', 0))}`",
            f"- Sanitized blocker: `{review['blocker']}`",
            f"- Next action: `{review['next_action']}`",
            "",
            "## Preserved state",
            "",
            f"- Local database: `{review['database']}`",
            f"- Master database: `{review['master_database']}`",
            f"- Restart pending: `{review['restart_handoff']['restart_pending']}`",
            f"- Recovery epoch: `{review['restart_handoff']['recovery_epoch']}`",
            "- AI actions: `[]`",
            "- Outbound actions: `[]`",
            "",
            "## Project output",
            "",
            f"- Layout schema: `{output_layout.get('schema_version', 'unknown')}`",
            f"- Artifact stem: `{output_layout.get('artifact_stem') or 'legacy_fixed_names'}`",
            f"- Dedicated project folder: `{output_layout.get('project_directory', 'unknown')}`",
            f"- Workflow database: `{output_layout.get('workflow_database', review['database'])}`",
            f"- Evidence export: `{output_layout.get('evidence_export', 'unknown')}`; status=`{output_layout.get('evidence_export_status', 'not_created')}`",
            f"- Machine result: `{output_layout.get('machine_result', 'unknown')}`",
            f"- Review log: `{output_layout.get('review_log', 'unknown')}`",
            "",
        ]
        if review["intent"].get("all_posts") is True:
            page_bound = int(
                status.get(
                    "max_pages",
                    review["intent"].get("resolved_max_pages", 0),
                )
                or 0
            )
            lines.extend(
                [
                    "## Creator inventory state",
                    "",
                    "- Cardinality: `all`",
                    f"- Terminal verified: `{bool(status.get('profile_inventory_terminal'))}`",
                    f"- Frozen inventory rows: `{status.get('profile_inventory_count', 'unknown')}`",
                    f"- Selected-new target so far: `{status.get('requested', 0)}`",
                    f"- Discovery page bound: `{'uncapped' if page_bound == 0 else page_bound}`",
                    "- Current profile total: `not_claimable_without_terminal_frontier`",
                    "",
                ]
            )
        deviations = list(review.get("deviations") or [])
        if deviations:
            lines.extend(["## Deviations", ""])
            lines.extend(f"- {value}" for value in deviations)
            lines.append("")
        return "\n".join(lines)
    status = review["status"]
    browser = review["browser_preflight"]
    export = review["artifacts"]["evidence_export"]
    lines = [
        "# Log File MUSIC AUDIT Review",
        "",
        f"- Executor compliance: **{review['executor_compliance']}**",
        f"- Task outcome: **{review['task_outcome']}**",
        f"- Project: `{review['project']}`",
        f"- Run ID: `{review['run_id']}`",
        f"- Workflow: `{review['workflow']}`",
        f"- Source: `{review['source_mode']}` — `{review['source_target']}`",
        f"- Policy: `{review['collection_policy']}`",
        f"- Status: `{status['status']}`",
        f"- Evidence-ready: `{status['evidence_ready']}/{status['requested']}`",
        f"- Unique candidates checkpointed: `{status['unique_collected']}`",
        f"- Failed candidate history: `{status['failed']}`",
        "",
    ]
    creator_inventory = review.get("creator_inventory")
    if isinstance(creator_inventory, Mapping) and creator_inventory:
        page_bound = int(creator_inventory.get("page_bound") or 0)
        lines.extend(
            [
                "## Creator inventory coverage",
                "",
                f"- Cardinality: `{creator_inventory['cardinality_mode']}`",
                f"- Terminal verified / inventory complete: `{creator_inventory['terminal_verified']}/{creator_inventory['inventory_complete']}`",
                f"- Frontier has_more / stop reason: `{creator_inventory['has_more']}` / `{creator_inventory['frontier_stop_reason']}`",
                f"- Unique owner posts observed: `{creator_inventory['unique_owner_posts_observed']}`",
                f"- Frozen inventory rows: `{creator_inventory['inventory_count']}`",
                f"- Selected new / excluded by new_only: `{creator_inventory['selected_new_count']}/{creator_inventory['new_only_excluded_count']}`",
                f"- Discovery page bound: `{'uncapped' if page_bound == 0 else page_bound}`",
                "",
            ]
        )
    lines.extend(
        [
            "## Browser preflight",
            "",
            f"- Profile: `{browser['profile_directory']}`",
            f"- Mode: `{browser['profile_mode']}`",
            f"- Reachable/authenticated/verified: `{browser['reachable']}/{browser['tiktok_authenticated']}/{browser['profile_verified']}`",
            f"- Observed account: `{browser['observed_account']}`",
            "",
            "## Evidence",
            "",
        ]
    )
    for post in review["posts"]:
        catalogs = ", ".join(
            f"{name}={value}" for name, value in post["music"]["catalogs"].items()
        )
        lines.extend(
            [
                f"### {post['post_id']} — @{post['creator']}",
                "",
                f"- URL: {post['url']}",
                f"- Caption outcome: `{post['caption_status']}`",
                f"- Comments/replies: `{post['comments']['comment_and_reply_count']}` represented; complete=`{post['comments']['complete']}`; limit_reached=`{post['comments']['limit_reached']}`",
                f"- Transcript: `{post['transcript']['status']}`; language=`{post['transcript']['language']}`; segments=`{post['transcript']['segment_count']}`",
                f"- TikTok music: `{post['music']['platform_music']}`",
                f"- Contained recording: `{post['music']['contained_recording']}`",
                f"- tt2dsp: `{post['music']['tt2dsp_resolution']}`",
                f"- Catalogs: `{catalogs}`",
                f"- Identity: `{post['music']['identity_status']}`",
                "",
            ]
        )
    deviations = list(review.get("deviations") or [])
    if deviations:
        lines.extend(["## Execution deviations", ""])
        lines.extend(f"- {value}" for value in deviations)
        lines.append("")
    lines.extend(
        [
            "## Project output",
            "",
            f"- Layout schema: `{output_layout.get('schema_version', 'unknown')}`",
            f"- Artifact stem: `{output_layout.get('artifact_stem') or 'legacy_fixed_names'}`",
            f"- Dedicated project folder: `{output_layout.get('project_directory', 'unknown')}`",
            f"- Workflow database: `{output_layout.get('workflow_database', review['database'])}`",
            f"- Evidence export: `{output_layout.get('evidence_export', export['path'])}`; status=`{output_layout.get('evidence_export_status', 'validated')}`",
            f"- Machine result: `{output_layout.get('machine_result', 'unknown')}`",
            f"- Review log: `{output_layout.get('review_log', 'unknown')}`",
            "",
            "## Validated artifacts",
            "",
            f"- Evidence export: `{export['path']}`",
            f"- Export schema/rows: `{export['schema_version']}` / `{export['records']}`",
            f"- Export SHA-256: `{export['sha256']}`",
            f"- Local database: `{review['database']}`",
            f"- Master database: `{review['master_database']}`",
            "- AI actions: `[]`",
            "- Outbound actions: `[]`",
            f"- Final stopping reason: `{review['final_stopping_reason']}`",
            "",
        ]
    )
    return "\n".join(lines)


def write_blocked_review(
    handoff: dict[str, Any],
    *,
    paths: OperatorPaths,
    blocker: str,
    next_action: str,
    executor_compliance: str = "PASS",
    deviations: Sequence[str] = (),
) -> dict[str, Any]:
    if executor_compliance not in {"PASS", "FAIL"}:
        raise OperatorError("blocked review compliance value is invalid")
    ledger = Path(str(handoff["ledger_file"])).resolve()
    records, head = validate_ledger(ledger)
    automatic_compliance, automatic_deviations = executor_compliance_summary(
        handoff,
        read_validated_ledger(handoff),
    )
    if automatic_compliance == "FAIL":
        executor_compliance = "FAIL"
    merged_deviations = list(dict.fromkeys((*automatic_deviations, *deviations)))
    review: dict[str, Any] = {
        "schema_version": REVIEW_SCHEMA,
        "executor_compliance": executor_compliance,
        "task_outcome": "BLOCKED",
        "generated_at": now_iso(),
        "offline_validation": False,
        "intent": handoff["intent"],
        "intent_hash": handoff["intent_hash"],
        "frozen_selection": handoff.get("frozen_selection") or {},
        "frozen_selection_hash": handoff.get("frozen_selection_hash") or "",
        "project": handoff["project"],
        "run_id": handoff.get("run_id") or "",
        "workflow": "listen",
        "source_mode": handoff["source_mode"],
        "source_target": handoff["source_target"],
        "collection_policy": handoff["collection_policy"],
        "database": handoff["database"],
        "master_database": handoff["master_database"],
        "interpreter": str(paths.python),
        "output_layout": output_layout_summary(
            handoff,
            evidence_export_status=(
                "present_unvalidated"
                if Path(str(handoff["export_file"])).is_file()
                else "not_created"
            ),
        ),
        "status": handoff.get("last_status") or {},
        "blocker": sanitize_error(blocker) or "durable_collection_blocker",
        "next_action": next_action,
        "deviations": [sanitize_error(value) for value in merged_deviations],
        "artifacts": {
            "operator_ledger": {
                "path": str(ledger),
                "records": records,
                "sha256": file_hash(ledger),
                "head_hash": head,
            }
        },
        "restart_handoff": {
            "restart_count": int(handoff.get("restart_count") or 0),
            "recovery_epoch": int(handoff.get("recovery_epoch") or 0),
            "restart_pending": handoff.get("restart_pending") is True,
            "reason": handoff.get("restart_reason") or "",
        },
        "ai_actions": [],
        "outbound_actions": [],
        "final_stopping_reason": "preserved_for_same_run_continuation",
    }
    review_json = Path(str(handoff["review_json"])).resolve()
    review_md = Path(str(handoff["review_markdown"])).resolve()
    atomic_write_json(review_json, review)
    atomic_write_text(review_md, review_markdown(review))
    handoff["artifacts"] = {
        "review_json": _artifact_entry(review_json, schema_version=REVIEW_SCHEMA),
        "review_markdown": _artifact_entry(review_md),
        "operator_ledger": _artifact_entry(ledger, records=records),
    }
    write_handoff(Path(str(handoff["handoff_file"])), handoff)
    return review


def _artifact_entry(path: Path, **extra: Any) -> dict[str, Any]:
    return {"path": str(path), "sha256": file_hash(path), **extra}


def finalize(
    handoff: dict[str, Any],
    *,
    paths: OperatorPaths,
) -> dict[str, Any]:
    status, status_result = canonical_status(handoff, paths=paths)
    append_ledger(
        handoff,
        action="status",
        stage="offline_verification",
        paths=paths,
        command=status_result.argv,
        exit_code=status_result.exit_code,
        duration_ms=status_result.duration_ms,
        status=status,
        error=status_result.sanitized_error,
        next_action=(
            "export_evidence"
            if status.get("status") == "collection_complete"
            else "stop"
        ),
    )
    if status.get("status") != "collection_complete":
        raise OperatorError("export is locked until collection_complete")
    requested = int(status.get("requested_count") or 0)
    if int(status.get("evidence_ready") or 0) != requested:
        raise OperatorError("export is locked until the exact-count gate passes")
    export_file = Path(str(handoff["export_file"])).resolve()
    if not export_file.exists():
        arguments = [
            "--database",
            str(handoff["database"]),
            "--master-database",
            str(handoff["master_database"]),
            "export-evidence",
            "--run-id",
            str(handoff["run_id"]),
            "--file",
            str(export_file),
        ]
        result = run_engage(arguments, paths=paths)
        append_ledger(
            handoff,
            action="export-evidence",
            stage="offline_export",
            paths=paths,
            command=result.argv,
            exit_code=result.exit_code,
            duration_ms=result.duration_ms,
            status=status,
            error=result.sanitized_error,
            artifacts=(
                ({"path": str(export_file), "records": result.payload.get("records")},)
                if result.exit_code == 0 and export_file.exists()
                else ()
            ),
            next_action="validate" if result.exit_code == 0 else "stop",
        )
        if result.exit_code != 0:
            raise OperatorError(result.sanitized_error or "evidence export failed")
    else:
        append_ledger(
            handoff,
            action="reuse-existing-export",
            stage="offline_export",
            paths=paths,
            status=status,
            artifacts=({"path": str(export_file), "sha256": file_hash(export_file)},),
            next_action="validate",
        )
    review = validate_completed_artifacts(handoff, paths=paths)
    append_ledger(
        handoff,
        action="validation-complete",
        stage="offline_verification",
        paths=paths,
        status=status,
        artifacts=(
            _artifact_entry(
                export_file,
                records=review["artifacts"]["evidence_export"]["records"],
                schema_version=EXPORT_SCHEMA,
            ),
        ),
        next_action="stop",
    )
    # The final ledger append changes its hash, so refresh the validated ledger
    # binding before writing the human and machine review artifacts.
    ledger = Path(str(handoff["ledger_file"])).resolve()
    ledger_records, ledger_head = validate_ledger(ledger)
    review["artifacts"]["operator_ledger"] = {
        "path": str(ledger),
        "records": ledger_records,
        "sha256": file_hash(ledger),
        "head_hash": ledger_head,
    }
    review_json = Path(str(handoff["review_json"])).resolve()
    review_md = Path(str(handoff["review_markdown"])).resolve()
    atomic_write_json(review_json, review)
    atomic_write_text(review_md, review_markdown(review))
    handoff["state"] = "complete"
    handoff["artifacts"] = {
        "evidence_export": _artifact_entry(export_file, records=requested),
        "review_json": _artifact_entry(review_json, schema_version=REVIEW_SCHEMA),
        "review_markdown": _artifact_entry(review_md),
        "operator_ledger": _artifact_entry(ledger, records=ledger_records),
    }
    write_handoff(Path(str(handoff["handoff_file"])), handoff)
    return review


def start_command(args: argparse.Namespace, paths: OperatorPaths) -> int:
    source_mode, source_target, posts, all_posts = validate_start_scope(args)
    freeze_start_options(
        args,
        source_mode=source_mode,
        source_target=source_target,
        requested_count=posts,
    )
    with acquire_start_registry_lock(paths):
        if not args.project:
            refuse_automatic_duplicate_start(
                args,
                source_mode=source_mode,
                source_target=source_target,
                requested_count=posts,
                all_posts=all_posts,
                paths=paths,
            )
        naming_timestamp = dt.datetime.now().astimezone().strftime(
            "%Y%m%d_%H%M%S_%f"
        )
        if args.project:
            naming = explicit_project_naming(
                args.project,
                timestamp=naming_timestamp,
            )
        else:
            naming = automatic_run_naming(
                source_mode=source_mode,
                source_target=source_target,
                requested_count=posts,
                all_posts=all_posts,
                collection_policy=args.collection_policy,
                run_label_input=str(args.run_label or ""),
                timestamp=naming_timestamp,
            )
        project = naming.project
        handoff_file, handoff = new_handoff(
            project=project,
            source_mode=source_mode,
            source_target=source_target,
            requested_count=posts,
            all_posts=all_posts,
            args=args,
            paths=paths,
            naming=naming,
        )
    with acquire_operator_lock(handoff):
        begin_execution(handoff, operation="start")
        try:
            exit_code = _execute_start_command(
                paths,
                handoff_file=handoff_file,
                handoff=handoff,
            )
        except Exception as exc:
            finish_execution(handoff, state="blocked", exit_code=1, error=str(exc))
            raise
        finish_execution(
            handoff,
            state="complete" if exit_code == 0 else "blocked",
            exit_code=exit_code,
        )
        return exit_code


def _execute_start_command(
    paths: OperatorPaths,
    *,
    handoff_file: Path,
    handoff: dict[str, Any],
    operation: str = "start",
) -> int:
    if int(handoff.get("initial_attempts") or 0) >= 1:
        raise OperatorError("initial collection attempt is already consumed")
    arguments = build_start_arguments(handoff)
    append_ledger(
        handoff,
        action="music-audit-start",
        stage="collection",
        paths=paths,
        command=(str(paths.python), str(paths.engage_script), *arguments),
        next_action="capture_run_id",
    )
    emit_execution_started(handoff, operation=operation)

    def claim_initial_attempt() -> None:
        previous = int(handoff.get("initial_attempts") or 0)
        handoff["initial_attempts"] = 1
        handoff["state"] = "collecting"
        try:
            write_handoff(handoff_file, handoff)
        except Exception:
            handoff["initial_attempts"] = previous
            handoff["state"] = "created"
            raise

    result = run_engage(
        arguments,
        paths=paths,
        handoff=handoff,
        on_process_started=claim_initial_attempt,
    )
    if not result.child_started:
        # If the child created durable state before launch registration failed,
        # bind it and consume the attempt conservatively. Otherwise this was a
        # pure prelaunch failure and the same immutable handoff may retry.
        database = Path(str(handoff["database"])).resolve()
        durable_state_created = database.is_file()
        if durable_state_created:
            try:
                _resolve_run_id_after_attempt(handoff, result, paths=paths)
            except OperatorError:
                pass
        if handoff.get("run_id"):
            handoff["initial_attempts"] = 1
            retry_action = "resume_same_run_without_after_restart"
        elif durable_state_created:
            handoff["initial_attempts"] = 1
            handoff["state"] = "blocked_unresolved_initial_launch"
            retry_action = "preserve_and_inspect_durable_initial_state"
        else:
            handoff["initial_attempts"] = 0
            handoff["state"] = "initial_launch_failed"
            retry_action = "retry_same_handoff_initial_launch"
        write_handoff(handoff_file, handoff)
        append_ledger(
            handoff,
            action="music-audit-result",
            stage="collection",
            paths=paths,
            command=result.argv,
            exit_code=result.exit_code,
            duration_ms=result.duration_ms,
            error=result.sanitized_error,
            next_action=retry_action,
        )
        write_blocked_review(
            handoff,
            paths=paths,
            blocker=result.sanitized_error or "collector_launch_failed",
            next_action=retry_action,
        )
        print(
            json.dumps(
                {
                    "status": "initial_launch_failed",
                    "run_id": handoff.get("run_id") or "pending",
                    "project_directory": str(handoff["project_root"]),
                    "handoff": str(handoff_file),
                    "next_action": retry_action,
                },
                ensure_ascii=True,
                indent=2,
            )
        )
        return 2
    try:
        _resolve_run_id_after_attempt(handoff, result, paths=paths)
        status, status_result = canonical_status(handoff, paths=paths)
    except OperatorError as exc:
        append_ledger(
            handoff,
            action="music-audit-result",
            stage="collection",
            paths=paths,
            command=result.argv,
            exit_code=result.exit_code,
            duration_ms=result.duration_ms,
            error=result.sanitized_error or str(exc),
            next_action="inspect_durable_blocker",
        )
        write_blocked_review(
            handoff,
            paths=paths,
            blocker=result.sanitized_error or str(exc),
            next_action="inspect_durable_blocker",
        )
        raise
    durable_status = str(status.get("status") or "")
    if durable_status == "collection_complete":
        next_action = "offline_finalize"
    elif durable_status == "collection_incomplete":
        next_action = "preserve_and_report_collection_incomplete"
    elif durable_status == "browser_blocked":
        next_action = "poll_for_guarded_same_handoff_resume_availability"
    else:
        next_action = "inspect_durable_blocker"
    append_ledger(
        handoff,
        action="music-audit-result",
        stage="collection",
        paths=paths,
        command=result.argv,
        exit_code=result.exit_code,
        duration_ms=result.duration_ms,
        status=status,
        error=result.sanitized_error,
        next_action=next_action,
    )
    append_ledger(
        handoff,
        action="status",
        stage="collection_checkpoint",
        paths=paths,
        command=status_result.argv,
        exit_code=status_result.exit_code,
        duration_ms=status_result.duration_ms,
        status=status,
        error=status_result.sanitized_error,
        next_action=next_action,
    )
    if status.get("status") == "collection_complete":
        review = finalize(handoff, paths=paths)
        print(json.dumps(review, ensure_ascii=True, indent=2))
        return 0
    write_blocked_review(
        handoff,
        paths=paths,
        blocker=result.sanitized_error or str(status.get("status") or "blocked"),
        next_action=next_action,
    )
    print(
        json.dumps(
            {
                "status": status.get("status"),
                "run_id": handoff["run_id"],
                "project_directory": str(handoff["project_root"]),
                "handoff": str(handoff_file),
                "counters": status_summary(status),
                "next_action": next_action,
            },
            ensure_ascii=True,
            indent=2,
        )
    )
    return 2


def prepare_restart_command(args: argparse.Namespace, paths: OperatorPaths) -> int:
    handoff_path = args.handoff.resolve()
    handoff = load_handoff(handoff_path, paths)
    with acquire_operator_lock(handoff):
        handoff = load_handoff(handoff_path, paths)
        reconcile_ledger_head(handoff)
        assert_no_live_execution(handoff, paths=paths, include_lock=False)
        return _prepare_restart_locked(
            args,
            paths,
            handoff=handoff,
        )


def _prepare_restart_locked(
    args: argparse.Namespace,
    paths: OperatorPaths,
    *,
    handoff: dict[str, Any],
) -> int:
    if handoff.get("state") == "complete":
        raise OperatorError("a completed MUSIC AUDIT does not need a restart")
    if handoff.get("restart_pending") is True:
        raise OperatorError("restart handoff is already pending")
    if int(handoff.get("restart_count") or 0) >= 1:
        raise OperatorError("the bounded single restart handoff is exhausted")
    ensure_handoff_run_binding(handoff, paths=paths)
    durable_state = durable_local_run_state(handoff)
    durable_status = str(durable_state["status"] or "")
    if durable_status == "collection_complete":
        raise OperatorError("a completed MUSIC AUDIT does not need a restart")
    requested = int(durable_state["requested_count"] or 0)
    ready = int(durable_state["evidence_ready"] or 0)
    cardinality = str(durable_state["cardinality_mode"] or "fixed")
    logically_complete = ready == requested and (
        (
            cardinality == "all"
            and int(durable_state["profile_inventory_terminal"] or 0) == 1
        )
        or (cardinality != "all" and requested > 0)
    )
    if logically_complete:
        raise OperatorError("a logically complete MUSIC AUDIT does not need a restart")
    if durable_status not in {"browser_blocked", "collection_failed"}:
        raise OperatorError(
            "restart handoff requires a terminal browser/crash collection state; "
            "collecting or command silence never qualifies"
        )
    terminal_result = require_paired_terminal_collection_result(handoff)
    if successful_browser_preflight_exists(handoff):
        raise OperatorError(
            "restart rejected: this run already recorded a successful Profile 7 preflight"
        )
    terminal_error = str(terminal_result.get("error") or "").casefold()
    if args.reason == "edge_native_crash" and not any(
        marker in terminal_error
        for marker in (
            "0xc0000005",
            "bex64",
            "native_edge_crash",
            "native edge crash",
        )
    ):
        raise OperatorError(
            "restart rejected: terminal result lacks a native Edge crash signature"
        )
    if args.reason == "edge_repair_requires_restart" and not all(
        marker in terminal_error
        for marker in ("registered_edge_repair", "restart_required")
    ):
        raise OperatorError(
            "restart rejected: terminal result lacks a registered repair receipt"
        )
    # A model-visible reason string and handoff hash cannot prove human
    # authorization or bind Windows fault evidence to the exact Edge PID. Fail
    # closed until an external trusted receipt channel exists.
    raise OperatorError(
        "human_action_required: automatic restart preparation is disabled until "
        "a trusted PID/time-bound Windows fault receipt is available"
    )


def _recovery_state_projection(handoff: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "restart_count": int(handoff.get("restart_count") or 0),
        "confirmed_restart_count": int(handoff.get("confirmed_restart_count") or 0),
        "recovery_epoch": int(handoff.get("recovery_epoch") or 0),
        "restart_pending": handoff.get("restart_pending") is True,
        "restart_reason": str(handoff.get("restart_reason") or ""),
        "restart_phase": str(handoff.get("restart_phase") or ""),
        "resume_attempts_by_epoch": dict(handoff.get("resume_attempts_by_epoch") or {}),
    }


def cancel_restart_command(args: argparse.Namespace, paths: OperatorPaths) -> int:
    handoff_path = args.handoff.resolve()
    handoff = load_handoff(handoff_path, paths)
    with acquire_operator_lock(handoff):
        handoff = load_handoff(handoff_path, paths)
        reconcile_ledger_head(handoff)
        assert_no_live_execution(handoff, paths=paths, include_lock=False)
        return _cancel_restart_locked(
            args,
            paths,
            handoff_path=handoff_path,
            handoff=handoff,
        )


def _cancel_restart_locked(
    args: argparse.Namespace,
    paths: OperatorPaths,
    *,
    handoff_path: Path,
    handoff: dict[str, Any],
) -> int:
    if handoff.get("restart_pending") is not True:
        raise OperatorError("no pending restart handoff can be cancelled")
    if int(handoff.get("restart_count") or 0) != 1:
        raise OperatorError(
            "restart cancellation requires exactly one prepared handoff"
        )
    attempts = handoff.get("resume_attempts_by_epoch")
    if not isinstance(attempts, Mapping) or int(attempts.get("1", 0)) != 0:
        raise OperatorError("restart cancellation is locked after an epoch-1 resume")
    ensure_handoff_run_binding(handoff, paths=paths)
    durable_state = durable_local_run_state(handoff)
    if str(durable_state.get("status") or "") == "collection_complete":
        raise OperatorError("a completed MUSIC AUDIT cannot cancel into resume")
    if str(durable_state.get("status") or "") != "collecting":
        raise OperatorError(
            "restart cancellation requires the preserved interrupted collecting state"
        )
    entries = read_validated_ledger(handoff)
    prepared = [
        (index, entry)
        for index, entry in enumerate(entries)
        if entry.get("action") == "prepare-restart-handoff"
    ]
    if len(prepared) != 1:
        raise OperatorError("restart cancellation requires one exact prepare record")
    if not latest_collection_attempt_is_unpaired(entries):
        raise OperatorError(
            "restart cancellation requires an interrupted unpaired collection start"
        )
    if not successful_browser_preflight_exists(handoff):
        raise OperatorError(
            "restart cancellation requires the successful preflight that contradicted the crash claim"
        )
    prepare_index, prepare_entry = prepared[0]
    allowed_after_prepare = {"status"}
    for entry in entries[prepare_index + 1 :]:
        if entry.get("action") not in allowed_after_prepare:
            raise OperatorError(
                "restart cancellation rejected after a later workflow action"
            )
    observed_boot = current_boot_marker()
    prepared_marker = handoff.get("restart_boot_marker")
    if isinstance(prepared_marker, Mapping) and prepared_marker:
        if observed_boot["boot_id_hash"] != prepared_marker.get("boot_id_hash"):
            raise OperatorError("Windows restarted after the handoff was prepared")
    else:
        # Compatibility path for v2 handoffs created before boot markers were
        # recorded. Two corroborated boot-time sources feed current_boot_marker.
        boot_started = _parse_iso(str(observed_boot["boot_started_at"]))
        prepared_at = _parse_iso(str(prepare_entry.get("timestamp") or ""))
        if boot_started >= prepared_at - dt.timedelta(seconds=60):
            raise OperatorError(
                "cannot prove that the legacy handoff is still in the same boot"
            )
    before = _recovery_state_projection(handoff)
    previous_reason = str(handoff.get("restart_reason") or "")
    handoff["restart_pending"] = False
    handoff["restart_reason"] = ""
    handoff["restart_phase"] = "cancelled_same_boot"
    handoff["recovery_epoch"] = 0
    handoff["confirmed_restart_count"] = 0
    handoff["state"] = "restart_cancelled_same_boot"
    handoff["last_status"] = status_summary(durable_state)
    after = _recovery_state_projection(handoff)
    cancellations = handoff.setdefault("restart_cancellations", [])
    if not isinstance(cancellations, list):
        raise OperatorError("restart cancellation history is invalid")
    cancellations.append(
        {
            "classification": args.classification,
            "cancelled_at": now_iso(),
            "prepared_sequence": int(prepare_entry.get("sequence") or 0),
            "prepared_at": str(prepare_entry.get("timestamp") or ""),
            "claimed_reason": previous_reason,
            "observed_boot_id_hash": observed_boot["boot_id_hash"],
            "before_state_hash": json_hash(before),
            "after_state_hash": json_hash(after),
        }
    )
    next_action = (
        "resume_same_run_without_after_restart"
        if int(attempts.get("0", 0)) == 0
        else "preserve_same_run_resume_epoch_exhausted"
    )
    append_ledger(
        handoff,
        action="cancel-restart-handoff",
        stage="offline_reclassification",
        paths=paths,
        status=durable_state,
        error="premature_unverified_restart",
        next_action=next_action,
        handoff_transition={
            "restart_pending": handoff["restart_pending"],
            "restart_reason": handoff["restart_reason"],
            "restart_phase": handoff["restart_phase"],
            "recovery_epoch": handoff["recovery_epoch"],
            "confirmed_restart_count": handoff["confirmed_restart_count"],
            "state": handoff["state"],
            "restart_cancellations": handoff["restart_cancellations"],
        },
    )
    write_blocked_review(
        handoff,
        paths=paths,
        blocker="operator_interrupted_after_successful_browser_preflight",
        next_action=next_action,
        executor_compliance="FAIL",
        deviations=(
            "The running operator and collector were interrupted before a terminal result.",
            "The restart claim was cancelled because Windows remained in the same boot session.",
        ),
    )
    print(
        json.dumps(
            {
                "status": "restart_cancelled_same_boot",
                "run_id": handoff["run_id"],
                "project_directory": str(handoff["project_root"]),
                "handoff": str(handoff_path),
                "counters": status_summary(durable_state),
                "next_action": next_action,
            },
            ensure_ascii=True,
            indent=2,
        )
    )
    return 0


def resume_command(args: argparse.Namespace, paths: OperatorPaths) -> int:
    handoff_path = args.handoff.resolve()
    handoff = load_handoff(handoff_path, paths)
    with acquire_operator_lock(handoff):
        handoff = load_handoff(handoff_path, paths)
        reconcile_ledger_head(handoff)
        assert_no_live_execution(handoff, paths=paths, include_lock=False)
        begin_execution(handoff, operation="resume")
        try:
            exit_code = _execute_resume_command(
                args,
                paths,
                handoff_path=handoff_path,
                handoff=handoff,
            )
        except Exception as exc:
            finish_execution(handoff, state="blocked", exit_code=1, error=str(exc))
            raise
        finish_execution(
            handoff,
            state="complete" if exit_code == 0 else "blocked",
            exit_code=exit_code,
        )
        return exit_code


def _execute_resume_command(
    args: argparse.Namespace,
    paths: OperatorPaths,
    *,
    handoff_path: Path,
    handoff: dict[str, Any],
) -> int:
    if not handoff.get("run_id"):
        database = Path(str(handoff["database"])).resolve()
        if database.is_file():
            ensure_handoff_run_binding(handoff, paths=paths)
        elif int(handoff.get("initial_attempts") or 0) == 0:
            if args.after_restart:
                raise OperatorError(
                    "an unlaunched MUSIC AUDIT cannot resume after restart"
                )
            return _execute_start_command(
                paths,
                handoff_file=handoff_path,
                handoff=handoff,
                operation="resume",
            )
        else:
            raise OperatorError(
                "initial collection launched but no durable run binding is available"
            )
    else:
        ensure_handoff_run_binding(handoff, paths=paths)
    if (
        handoff.get("state") == "complete"
        or durable_local_run_status(handoff) == "collection_complete"
    ):
        review = finalize(handoff, paths=paths)
        print(json.dumps(review, ensure_ascii=True, indent=2))
        return 0
    pending = handoff.get("restart_pending") is True
    observed_marker: Mapping[str, Any] | None = None
    if pending:
        if not args.after_restart:
            raise OperatorError("pending restart resume requires --after-restart")
        prepared_marker = handoff.get("restart_boot_marker")
        if not isinstance(prepared_marker, Mapping) or not prepared_marker:
            raise OperatorError(
                "legacy restart claim is unverified; cancel it before ordinary resume"
            )
        observed_marker = current_boot_marker()
        if observed_marker["boot_id_hash"] == prepared_marker.get("boot_id_hash"):
            raise OperatorError(
                "Windows has not restarted since the handoff was prepared"
            )
    elif args.after_restart:
        raise OperatorError("resume must be invoked without --after-restart")
    epoch = "1" if pending else str(int(handoff.get("recovery_epoch") or 0))
    attempts = handoff["resume_attempts_by_epoch"]
    if int(attempts.get(epoch, 0)) >= 1:
        raise OperatorError("same-run resume is exhausted for this recovery epoch")
    arguments = build_resume_arguments(handoff)
    append_ledger(
        handoff,
        action="resume-collect-start",
        stage="collection",
        paths=paths,
        command=(str(paths.python), str(paths.engage_script), *arguments),
        next_action="canonical_resume_collect",
    )
    emit_execution_started(handoff, operation="resume")

    def claim_resume_attempt() -> None:
        previous_attempt = int(attempts.get(epoch, 0))
        previous_fields = {
            key: handoff.get(key)
            for key in (
                "recovery_epoch",
                "restart_pending",
                "restart_phase",
                "confirmed_restart_count",
                "restart_observed_boot_marker",
                "state",
            )
        }
        attempts[epoch] = 1
        if pending:
            handoff["recovery_epoch"] = 1
            handoff["restart_pending"] = False
            handoff["restart_phase"] = "reboot_observed"
            handoff["confirmed_restart_count"] = 1
            handoff["restart_observed_boot_marker"] = dict(observed_marker or {})
            handoff["state"] = "resuming_after_restart"
        else:
            handoff["state"] = "resuming_after_bounded_recovery"
        try:
            write_handoff(handoff_path, handoff)
        except Exception:
            attempts[epoch] = previous_attempt
            for key, value in previous_fields.items():
                if value is None:
                    handoff.pop(key, None)
                else:
                    handoff[key] = value
            raise

    result = run_engage(
        arguments,
        paths=paths,
        handoff=handoff,
        on_process_started=claim_resume_attempt,
    )
    if not result.child_started:
        next_action = "retry_same_run_launch_without_after_restart"
        if pending:
            next_action = "retry_same_pending_restart_launch_after_restart"
        handoff["state"] = "resume_launch_failed"
        write_handoff(handoff_path, handoff)
        append_ledger(
            handoff,
            action="resume-collect-result",
            stage="collection",
            paths=paths,
            command=result.argv,
            exit_code=result.exit_code,
            duration_ms=result.duration_ms,
            error=result.sanitized_error,
            next_action=next_action,
        )
        write_blocked_review(
            handoff,
            paths=paths,
            blocker=result.sanitized_error or "collector_launch_failed",
            next_action=next_action,
        )
        print(
            json.dumps(
                {
                    "status": "resume_launch_failed",
                    "run_id": handoff["run_id"],
                    "project_directory": str(handoff["project_root"]),
                    "handoff": str(handoff_path),
                    "next_action": next_action,
                },
                ensure_ascii=True,
                indent=2,
            )
        )
        return 2
    try:
        status, status_result = canonical_status(handoff, paths=paths)
    except OperatorError as exc:
        append_ledger(
            handoff,
            action="resume-collect-result",
            stage="collection",
            paths=paths,
            command=result.argv,
            exit_code=result.exit_code,
            duration_ms=result.duration_ms,
            error=result.sanitized_error or str(exc),
            next_action="preserve_and_report_durable_blocker",
        )
        handoff["state"] = "blocked"
        write_handoff(handoff_path, handoff)
        write_blocked_review(
            handoff,
            paths=paths,
            blocker=result.sanitized_error or str(exc),
            next_action="preserve_and_report_durable_blocker",
        )
        raise
    append_ledger(
        handoff,
        action="resume-collect-result",
        stage="collection",
        paths=paths,
        command=result.argv,
        exit_code=result.exit_code,
        duration_ms=result.duration_ms,
        status=status,
        error=result.sanitized_error,
        next_action=(
            "offline_finalize"
            if status.get("status") == "collection_complete"
            else "preserve_and_report_durable_blocker"
        ),
    )
    append_ledger(
        handoff,
        action="status",
        stage="collection_checkpoint",
        paths=paths,
        command=status_result.argv,
        exit_code=status_result.exit_code,
        duration_ms=status_result.duration_ms,
        status=status,
        error=status_result.sanitized_error,
        next_action=(
            "offline_finalize"
            if status.get("status") == "collection_complete"
            else "preserve_and_report_durable_blocker"
        ),
    )
    if status.get("status") == "collection_complete":
        review = finalize(handoff, paths=paths)
        print(json.dumps(review, ensure_ascii=True, indent=2))
        return 0
    handoff["state"] = "blocked"
    write_handoff(handoff_path, handoff)
    write_blocked_review(
        handoff,
        paths=paths,
        blocker=result.sanitized_error or str(status.get("status") or "blocked"),
        next_action="preserve_and_report_durable_blocker",
    )
    print(
        json.dumps(
            {
                "status": status.get("status"),
                "run_id": handoff["run_id"],
                "project_directory": str(handoff["project_root"]),
                "handoff": str(handoff_path),
                "counters": status_summary(status),
                "next_action": "preserve_and_report_durable_blocker",
            },
            ensure_ascii=True,
            indent=2,
        )
    )
    return 2


def poll_command(args: argparse.Namespace, paths: OperatorPaths) -> int:
    """Report safe progress without writing handoff, ledger, or workflow state."""

    handoff = load_handoff(args.handoff.resolve(), paths)
    snapshot_handoff: dict[str, Any] = dict(handoff)
    if not snapshot_handoff.get("run_id"):
        database = Path(str(snapshot_handoff["database"])).resolve()
        if database.is_file():
            try:
                snapshot_handoff["run_id"] = discover_run_id(
                    database, str(snapshot_handoff["project"])
                )
            except OperatorError:
                pass
    try:
        progress = durable_progress_snapshot(snapshot_handoff)
    except OperatorError as exc:
        progress = {
            "phase": "progress_unavailable",
            "durable_status": "unknown",
            "requested": int(handoff.get("requested_count") or 0),
            "evidence_ready": 0,
            "unique_collected": 0,
            "failed": 0,
            "latest_event": {},
            "error": sanitize_error(exc),
        }
    live = execution_liveness(handoff, paths=paths)
    execution = handoff.get("execution")
    execution_state = (
        str(execution.get("state") or "") if isinstance(execution, Mapping) else ""
    )
    active = any(live.values())
    durable_status = str(progress.get("durable_status") or "unknown")
    attempts = handoff.get("resume_attempts_by_epoch")
    if not isinstance(attempts, Mapping):
        attempts = {}
    current_epoch = str(int(handoff.get("recovery_epoch") or 0))
    resume_attempts_used = int(attempts.get(current_epoch, 0))
    resume_attempts_remaining = max(
        0, RESUME_ATTEMPTS_PER_EPOCH - resume_attempts_used
    )
    resume_exhausted = resume_attempts_remaining == 0
    if active:
        operator_status = "RUNNING"
        next_action = "keep_waiting_do_not_restart"
    elif handoff.get("state") == "complete":
        operator_status = "COMPLETE"
        next_action = "validate_or_stop"
    elif durable_status == "collection_complete":
        operator_status = "COLLECTION_COMPLETE_NEEDS_FINALIZE"
        next_action = "offline_finalize"
    elif handoff.get("restart_pending") is True:
        operator_status = "RESTART_PENDING"
        next_action = "human_review_required"
    elif (
        not snapshot_handoff.get("run_id")
        and int(handoff.get("initial_attempts") or 0) == 0
    ):
        operator_status = "INTERRUPTED"
        next_action = "resume_same_handoff_initial_launch"
    elif durable_status == "browser_blocked":
        if resume_exhausted:
            operator_status = "BLOCKED"
            next_action = "preserve_same_run_resume_epoch_exhausted"
        else:
            operator_status = "RESUME_READY"
            next_action = (
                "run_guarded_resume_same_handoff_without_preflight_or_after_restart"
            )
    elif execution_state in ACTIVE_EXECUTION_STATES or durable_status == "collecting":
        if resume_exhausted:
            operator_status = "BLOCKED"
            next_action = "preserve_same_run_resume_epoch_exhausted"
        else:
            operator_status = "INTERRUPTED"
            next_action = "resume_same_run_without_after_restart"
    else:
        operator_status = "BLOCKED"
        next_action = "inspect_terminal_result"
    same_handoff_resume_available = operator_status in {
        "INTERRUPTED",
        "RESUME_READY",
    }
    safe_same_handoff_action = None
    if same_handoff_resume_available:
        safe_same_handoff_action = {
            "operation": "guarded_resume",
            "argv": [
                str(paths.python),
                str(Path(__file__).resolve()),
                "resume",
                "--handoff",
                str(args.handoff.resolve()),
            ],
            "first_browser_touching_operation": True,
            "after_restart": False,
            "replacement_project_allowed": False,
            "requires_explicit_user_direction": operator_status == "RESUME_READY",
        }
    latest = progress.get("latest_event") or {}
    response = {
        "operator_status": operator_status,
        "project": handoff["project"],
        "project_directory": str(handoff["project_root"]),
        "run_id": snapshot_handoff.get("run_id") or "pending",
        "phase": progress.get("phase"),
        "durable_status": durable_status,
        "requested": int(progress.get("requested") or 0),
        "evidence_ready": int(progress.get("evidence_ready") or 0),
        "unique_collected": int(progress.get("unique_collected") or 0),
        "failed": int(progress.get("failed") or 0),
        "latest_event": {
            key: latest.get(key)
            for key in ("event_id", "stage", "event", "created_at")
            if key in latest
        },
        "operator_process_alive": live["operator_alive"],
        "collector_process_alive": live["collector_alive"],
        "inspection_uncertain": live["inspection_uncertain"],
        "operator_lock_held": live["lock_held"],
        "restart_pending": handoff.get("restart_pending") is True,
        "resume_budget": {
            "epoch": int(current_epoch),
            "attempts_used": resume_attempts_used,
            "attempts_limit": RESUME_ATTEMPTS_PER_EPOCH,
            "attempts_remaining": resume_attempts_remaining,
            "would_consume_on_child_registration": True,
        },
        "same_handoff_resume_available": same_handoff_resume_available,
        "safe_same_handoff_action": safe_same_handoff_action,
        "replacement_project_allowed": False,
        "next_action": next_action,
    }
    if progress.get("error"):
        response["progress_error"] = progress["error"]
    print(json.dumps(response, ensure_ascii=True, indent=2))
    return 0


def status_command(args: argparse.Namespace, paths: OperatorPaths) -> int:
    handoff_path = args.handoff.resolve()
    handoff = load_handoff(handoff_path, paths)
    with acquire_operator_lock(handoff):
        handoff = load_handoff(handoff_path, paths)
        reconcile_ledger_head(handoff)
        assert_no_live_execution(handoff, paths=paths, include_lock=False)
        ensure_handoff_run_binding(handoff, paths=paths)
        status, result = canonical_status(handoff, paths=paths)
        append_ledger(
            handoff,
            action="status",
            stage="offline_status",
            paths=paths,
            command=result.argv,
            exit_code=result.exit_code,
            duration_ms=result.duration_ms,
            status=status,
            error=result.sanitized_error,
            next_action=(
                "offline_finalize"
                if status.get("status") == "collection_complete"
                else "preserve"
            ),
        )
        print(json.dumps(status_summary(status), ensure_ascii=True, indent=2))
        return 0


def finalize_command(args: argparse.Namespace, paths: OperatorPaths) -> int:
    handoff_path = args.handoff.resolve()
    handoff = load_handoff(handoff_path, paths)
    with acquire_operator_lock(handoff):
        handoff = load_handoff(handoff_path, paths)
        reconcile_ledger_head(handoff)
        assert_no_live_execution(handoff, paths=paths, include_lock=False)
        ensure_handoff_run_binding(handoff, paths=paths)
        review = finalize(handoff, paths=paths)
        print(json.dumps(review, ensure_ascii=True, indent=2))
        return 0


def validate_command(args: argparse.Namespace, paths: OperatorPaths) -> int:
    handoff = load_handoff(args.handoff.resolve(), paths)
    validate_run_intent(handoff, paths=paths)
    review = validate_completed_artifacts(handoff, paths=paths)
    print(json.dumps(review, ensure_ascii=True, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Guarded canonical TikTok MUSIC AUDIT operator"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    start = subparsers.add_parser("start", help="Create and run one new audit")
    source = start.add_mutually_exclusive_group(required=True)
    source.add_argument("--topic")
    source.add_argument("--creator")
    source.add_argument("--url")
    cardinality = start.add_mutually_exclusive_group(required=True)
    cardinality.add_argument("--posts", type=int)
    cardinality.add_argument("--all-posts", action="store_true")
    start.add_argument(
        "--project",
        default="",
        help=(
            "optional advanced unique slug beginning with music_audit_; "
            "mutually exclusive with --run-label"
        ),
    )
    start.add_argument(
        "--run-label",
        default="",
        help=(
            "optional descriptive naming label such as test or brand-mie-sedaap; "
            "it affects output names only, never collection scope"
        ),
    )
    start.add_argument("--max-comments", type=int, default=20)
    start.add_argument(
        "--max-pages",
        type=int,
        default=0,
        help="finite bound for non-ALL scopes; invalid with --all-posts",
    )
    start.add_argument(
        "--collection-policy",
        choices=("new_only", "refresh_known"),
        default="new_only",
    )
    start.add_argument("--refresh-stale-before", default="")
    start.add_argument("--refresh-post-id", action="append", default=[])
    start.add_argument("--expected-account", default="")
    start.add_argument("--browser-startup-timeout", type=float, default=120.0)
    start.set_defaults(handler=start_command)

    prepare = subparsers.add_parser(
        "prepare-restart",
        help="Persist one bounded native-Edge restart handoff; does not restart Windows",
    )
    prepare.add_argument("--handoff", type=Path, required=True)
    prepare.add_argument(
        "--reason",
        choices=("edge_native_crash", "edge_repair_requires_restart"),
        required=True,
    )
    prepare.set_defaults(handler=prepare_restart_command)

    cancel = subparsers.add_parser(
        "cancel-restart",
        help="Cancel an unverified same-boot restart claim without browser access",
    )
    cancel.add_argument("--handoff", type=Path, required=True)
    cancel.add_argument(
        "--classification",
        choices=("premature_unverified_restart",),
        default="premature_unverified_restart",
    )
    cancel.set_defaults(handler=cancel_restart_command)

    resume = subparsers.add_parser("resume", help="Resume the same immutable run")
    resume.add_argument("--handoff", type=Path, required=True)
    resume.add_argument("--after-restart", action="store_true")
    resume.set_defaults(handler=resume_command)

    poll = subparsers.add_parser(
        "poll", help="Read live progress without mutating workflow or operator state"
    )
    poll.add_argument("--handoff", type=Path, required=True)
    poll.set_defaults(handler=poll_command)

    status = subparsers.add_parser("status", help="Read canonical durable status")
    status.add_argument("--handoff", type=Path, required=True)
    status.set_defaults(handler=status_command)

    finish = subparsers.add_parser(
        "finalize", help="Export and validate a completed audit offline"
    )
    finish.add_argument("--handoff", type=Path, required=True)
    finish.set_defaults(handler=finalize_command)

    validate = subparsers.add_parser(
        "validate", help="Validate existing export/local/master artifacts offline"
    )
    validate.add_argument("--handoff", type=Path, required=True)
    validate.set_defaults(handler=validate_command)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    paths = canonical_paths()
    try:
        assert_runtime(paths)
        return int(args.handler(args, paths))
    except OperatorError as exc:
        print(
            json.dumps(
                {"status": "blocked", "error": sanitize_error(exc)},
                ensure_ascii=True,
                indent=2,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
