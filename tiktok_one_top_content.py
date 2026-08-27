"""TikTok One Top Content link discovery and canonical LISTEN bridge.

The discovery half of this runner stores only query-free TikTok video links
and bounded page provenance.  The LISTEN half delegates every eligible link to
the existing one-URL ``music-audit`` command; it does not implement another
TikTok evidence collector or any publication surface.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import uuid
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import engage_tiktok
import tiktok_master_database
from tiktok_scraper.top_content_browser import (
    DEFAULT_RANKINGS,
    TopContentBrowserError,
    discover_top_content_links,
    normalize_top_content_source_url,
)
from tiktok_scraper.top_content_state import (
    TopContentLinkState,
    TopContentStateError,
    canonical_sha256,
)


ROOT = Path(__file__).resolve().parent
DEFAULT_SOURCE_URL = (
    "https://ads.tiktok.com/creative/forpartners/creator/top-content?region=row"
)
DEFAULT_LINK_DATABASE = (
    ROOT / "comments_data" / "tiktok_one" / "top_content_links.sqlite3"
)
DEFAULT_CHILD_ROOT = ROOT / "comments_data" / "tiktok_one" / "listen"
CAPABILITY_SCHEMA = "tiktok-one-top-content-capabilities-v1"
RESULT_SCHEMA = "tiktok-one-top-content-result-v1"
LISTEN_SETTINGS_SCHEMA = "tiktok-one-top-content-listen-settings-v1"
KNOWN_SET_SCHEMA = "tiktok-master-known-post-ids-snapshot-v1"

_SAFE_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,119}")
_SAFE_PROJECT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}")
_SAFE_ACCOUNT = re.compile(r"[A-Za-z0-9._]{1,64}")
_HASH = re.compile(r"[0-9a-f]{64}")
_TERMINAL_CHILD_COMPLETE = "collection_complete"
_RETRYABLE_CHILD = frozenset(engage_tiktok.RESUMABLE_COLLECTION_STATUSES)


class TopContentRunnerError(RuntimeError):
    """A Top Content command failed closed."""


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _resolved(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _validate_run_id(value: Any) -> str:
    run_id = str(value or "").strip()
    if _SAFE_RUN_ID.fullmatch(run_id) is None:
        raise TopContentRunnerError(
            "run_id must contain only letters, numbers, underscores, or hyphens"
        )
    return run_id


def _new_run_id() -> str:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"top-content-{stamp}-{uuid.uuid4().hex[:10]}"


def _normalize_project(value: Any, *, run_id: str) -> str:
    project = str(value or "").strip()
    if not project:
        project = f"tiktok_one_top_content_{run_id}"
    if _SAFE_PROJECT.fullmatch(project) is None:
        raise TopContentRunnerError(
            "project must contain only letters, numbers, dots, underscores, or hyphens"
        )
    return project


def _normalize_account(value: Any) -> str:
    account = str(value or "").strip().lstrip("@").casefold()
    if account and _SAFE_ACCOUNT.fullmatch(account) is None:
        raise TopContentRunnerError("expected_account is invalid")
    return account


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise TopContentRunnerError(f"{name} must be a positive integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise TopContentRunnerError(f"{name} must be a positive integer") from exc
    if number < 1:
        raise TopContentRunnerError(f"{name} must be a positive integer")
    return number


def _timeout(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise TopContentRunnerError(
            "browser_startup_timeout must be a positive number"
        ) from exc
    if not (number > 0 and number < 86_400):
        raise TopContentRunnerError("browser_startup_timeout must be a positive number")
    return number


def _json_result(value: Mapping[str, Any]) -> None:
    print(json.dumps(dict(value), ensure_ascii=True, indent=2, sort_keys=True))


def _bounded_reason(value: Any, fallback: str) -> str:
    """Return a bounded status reason without retaining URLs or child logs."""

    text = " ".join(str(value or fallback).split())
    text = re.sub(r"https?://\S+", "[url omitted]", text, flags=re.IGNORECASE)
    text = re.sub(
        r"(?i)\b(cookie|authorization|access[_ -]?token|session)\s*[:=]\s*\S+",
        r"\1=[omitted]",
        text,
    )
    return (text or fallback)[:300]


def _known_snapshot(master_database: str | Path) -> tuple[set[str], dict[str, Any]]:
    path = _resolved(master_database)
    connection = tiktok_master_database.connect_master(path)
    try:
        known = tiktok_master_database.known_post_ids(connection)
    finally:
        connection.close()
    digest = canonical_sha256(sorted(known))
    return known, {
        "schema_version": KNOWN_SET_SCHEMA,
        "database": str(path),
        "known_post_count": len(known),
        "known_post_ids_hash": digest,
        "observed_at": _utc_now(),
    }


def _account_binding(discovery: Mapping[str, Any]) -> dict[str, Any]:
    digest = str(discovery.get("account_binding_hash") or "").strip().casefold()
    if _HASH.fullmatch(digest) is None:
        raise TopContentRunnerError("discovery did not return a valid account binding")
    return {
        "schema_version": "tiktok-profile7-account-binding-v1",
        "sha256": digest,
    }


def _frozen_list_scope(
    *, requested_count: int, rankings: Sequence[str]
) -> dict[str, Any]:
    """Return stable list-selection rules, excluding per-attempt counters."""

    return {
        "schema_version": "tiktok-one-top-content-list-scope-v1",
        "requested_count": requested_count,
        "rankings_requested": list(rankings),
        "positions_per_ranking": 100,
        "deduplication_key": "numeric_video_id",
        "selection_order": "ranking_order_then_ranking_position",
    }


def _persist_discovery(
    state: TopContentLinkState,
    *,
    run_id: str,
    requested_count: int,
    source_url: str,
    rankings: Sequence[str],
    discovery: Mapping[str, Any],
    collection_policy: str,
    master_database: Path | None,
    known_snapshot: Mapping[str, Any] | None,
) -> dict[str, Any]:
    links = discovery.get("links")
    if not isinstance(links, list):
        raise TopContentRunnerError("discovery returned an invalid link inventory")
    filter_snapshot = discovery.get("filter_snapshot")
    discovery_observation = discovery.get("list_snapshot")
    if not isinstance(filter_snapshot, dict) or not isinstance(
        discovery_observation, dict
    ):
        raise TopContentRunnerError("discovery returned invalid bounded provenance")
    list_snapshot = _frozen_list_scope(
        requested_count=requested_count,
        rankings=rankings,
    )
    account = _account_binding(discovery)

    excluded_count = int(discovery_observation.get("excluded_video_ids") or 0)
    provenance = dict(known_snapshot or {})
    master_hash = str(provenance.get("known_post_ids_hash") or "")
    state.create_run(
        run_id,
        source_url=source_url,
        requested_count=requested_count,
        scope={
            "source_mode": "tiktok_one_top_content_links",
            "region": str(discovery.get("region") or ""),
            "data_boundary": "canonical_video_links_only",
        },
        filter_snapshot=filter_snapshot,
        list_snapshot=list_snapshot,
        account_binding=account,
        ranking_order=list(rankings),
        collection_policy=collection_policy,
        master_database_identity=(KNOWN_SET_SCHEMA if master_database else ""),
        master_database_hash=(master_hash if master_database else ""),
        master_database_path=(str(master_database) if master_database else ""),
        known_excluded_count=(excluded_count if master_database else 0),
        known_excluded_provenance=(provenance if master_database else {}),
    )

    for raw in links[:requested_count]:
        if not isinstance(raw, dict):
            raise TopContentRunnerError("discovery returned an invalid link row")
        state.checkpoint_link(
            run_id,
            ordinal=_positive_int(raw.get("ordinal"), "link ordinal"),
            video_id=str(raw.get("video_id") or ""),
            canonical_url=str(raw.get("url") or ""),
            creator=str(raw.get("creator") or ""),
            ranking_key=str(raw.get("ranking") or ""),
            ranking_position=_positive_int(
                raw.get("ranking_position"), "ranking position"
            ),
        )

    reasons = [
        _bounded_reason(reason, "link_discovery_incomplete")
        for reason in discovery.get("reasons", [])
        if str(reason or "").strip()
    ]
    if len(links) != requested_count:
        if not reasons:
            reasons = ["link_discovery_incomplete"]
        for reason in reasons:
            state.record_discovery_failure(run_id, reason, stage="top_content_page")
        status = state.finalize(run_id, reasons)
    else:
        status = state.finalize(run_id)
    return {
        "schema_version": RESULT_SCHEMA,
        "operation": "collect_links",
        **status,
        "canonical_urls": [row["canonical_url"] for row in state.links(run_id)],
    }


async def collect_links(
    *,
    database: str | Path,
    source_url: str,
    requested_count: int,
    rankings: Sequence[str] = DEFAULT_RANKINGS,
    run_id: str = "",
    for_listen: bool = False,
    master_database: str | Path = tiktok_master_database.DEFAULT_MASTER_DATABASE,
    social_browser_state: str | Path = engage_tiktok.DEFAULT_BROWSER_STATE,
    browser_startup_timeout: float = engage_tiktok.PROFILE7_STARTUP_TIMEOUT_SECONDS,
    expected_account: str = "",
) -> dict[str, Any]:
    normalized_source, _region = normalize_top_content_source_url(source_url)
    count = _positive_int(requested_count, "links")
    normalized_run_id = _validate_run_id(run_id or _new_run_id())
    master_path: Path | None = None
    known: set[str] = set()
    snapshot: dict[str, Any] | None = None
    policy = "all_links"
    if for_listen:
        master_path = _resolved(master_database)
        known, snapshot = _known_snapshot(master_path)
        policy = "new_only_against_tiktok_master"

    discovery = await discover_top_content_links(
        normalized_source,
        count,
        rankings=rankings,
        social_browser_runtime_dir=_resolved(social_browser_state).parent,
        startup_timeout=_timeout(browser_startup_timeout),
        expected_account=_normalize_account(expected_account),
        exclude_video_ids=known,
    )
    with TopContentLinkState(_resolved(database)) as state:
        return _persist_discovery(
            state,
            run_id=normalized_run_id,
            requested_count=count,
            source_url=normalized_source,
            rankings=rankings,
            discovery=discovery,
            collection_policy=policy,
            master_database=master_path,
            known_snapshot=snapshot,
        )


async def resume_links(
    *,
    database: str | Path,
    run_id: str,
    social_browser_state: str | Path = engage_tiktok.DEFAULT_BROWSER_STATE,
    browser_startup_timeout: float = engage_tiktok.PROFILE7_STARTUP_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    normalized_run_id = _validate_run_id(run_id)
    database_path = _resolved(database)
    with TopContentLinkState(database_path) as state:
        frozen = state.status(normalized_run_id)
        if frozen["status"] == "links_complete":
            raise TopContentRunnerError("a complete link run does not need resume")
        rankings = tuple(str(value) for value in frozen["ranking_order"])
        excluded: set[str] = set()
        if frozen["collection_policy"] == "new_only_against_tiktok_master":
            master_path = _resolved(frozen["master_database"]["path"])
            excluded, current_snapshot = _known_snapshot(master_path)
            if (
                current_snapshot["known_post_ids_hash"]
                != frozen["master_database"]["sha256"]
            ):
                raise TopContentRunnerError(
                    "the frozen TikTok master known-ID fence changed; link resume refused"
                )
        discovery = await discover_top_content_links(
            frozen["source_url"],
            frozen["requested_count"],
            rankings=rankings,
            social_browser_runtime_dir=_resolved(social_browser_state).parent,
            startup_timeout=_timeout(browser_startup_timeout),
            exclude_video_ids=excluded,
        )
        if _account_binding(discovery) != frozen["account_binding"]:
            raise TopContentRunnerError("Profile 7 account binding changed on resume")
        if discovery.get("filter_snapshot") != frozen["filter_snapshot"]:
            raise TopContentRunnerError("Top Content filters changed on resume")
        if (
            _frozen_list_scope(
                requested_count=frozen["requested_count"], rankings=rankings
            )
            != frozen["list_snapshot"]
        ):
            raise TopContentRunnerError("Top Content list scope changed on resume")
        existing = state.links(normalized_run_id)
        live_links = discovery.get("links")
        if not isinstance(live_links, list):
            raise TopContentRunnerError("discovery returned an invalid link inventory")
        for old in existing:
            ordinal = int(old["ordinal"])
            if ordinal > len(live_links):
                raise TopContentRunnerError(
                    "frozen link inventory is no longer available"
                )
            current = live_links[ordinal - 1]
            current_url = engage_tiktok.normalize_direct_post_target(
                current.get("url")
            )["url"]
            if (
                old["video_id"] != str(current.get("video_id") or "")
                or old["canonical_url"] != current_url
                or old["ranking_key"] != str(current.get("ranking") or "")
                or old["ranking_position"] != int(current.get("ranking_position") or 0)
            ):
                raise TopContentRunnerError("frozen link inventory changed on resume")
        state.resume(
            normalized_run_id,
            expected_scope_hash=frozen["immutable_scope_hash"],
            source_url=frozen["source_url"],
            requested_count=frozen["requested_count"],
            collection_policy=frozen["collection_policy"],
            scope=frozen["scope"],
            filter_snapshot=frozen["filter_snapshot"],
            list_snapshot=frozen["list_snapshot"],
            account_binding=frozen["account_binding"],
            ranking_order=frozen["ranking_order"],
        )
        for raw in live_links[len(existing) : frozen["requested_count"]]:
            state.checkpoint_link(
                normalized_run_id,
                ordinal=raw.get("ordinal"),
                video_id=raw.get("video_id"),
                canonical_url=raw.get("url"),
                creator=raw.get("creator"),
                ranking_key=raw.get("ranking"),
                ranking_position=raw.get("ranking_position"),
            )
        current_links = state.links(normalized_run_id)
        reasons = [
            _bounded_reason(value, "link_discovery_incomplete")
            for value in discovery.get("reasons", [])
            if str(value or "").strip()
        ]
        status = (
            state.finalize(normalized_run_id)
            if len(current_links) == frozen["requested_count"]
            else state.finalize(
                normalized_run_id, reasons or ["link_discovery_incomplete"]
            )
        )
        return {
            "schema_version": RESULT_SCHEMA,
            "operation": "resume_links",
            **status,
            "canonical_urls": [row["canonical_url"] for row in current_links],
        }


def _child_database_for(run_id: str) -> Path:
    safe = _validate_run_id(run_id)
    root = DEFAULT_CHILD_ROOT.resolve()
    child = (root / safe / "engage_state.sqlite").resolve()
    if root not in child.parents:
        raise TopContentRunnerError("child database escaped its intended directory")
    return child


def _fresh_child_command(
    *,
    project: str,
    canonical_url: str,
    child_database: Path,
    master_database: Path,
    max_comments: int,
    social_browser_state: Path,
    browser_startup_timeout: float,
    expected_account: str,
) -> list[str]:
    normalized = engage_tiktok.normalize_direct_post_target(canonical_url)
    if normalized["content_type"] != "video":
        raise TopContentRunnerError("Top Content bridge accepts video URLs only")
    command = [
        str(engage_tiktok.REQUIRED_PYTHON_INTERPRETER),
        str((ROOT / "engage_tiktok.py").resolve()),
        "--database",
        str(child_database),
        "--master-database",
        str(master_database),
        "music-audit",
        "--project",
        project,
        "--url",
        normalized["url"],
        "--posts",
        "1",
        "--max-comments",
        str(max_comments),
        "--max-pages",
        "1",
        "--collection-policy",
        "new_only",
        "--music-catalog",
        "musicbrainz",
        "--social-browser-state",
        str(social_browser_state),
        "--browser-startup-timeout",
        str(browser_startup_timeout),
    ]
    if expected_account:
        command.extend(["--expected-account", expected_account])
    return command


def _resume_child_command(
    *,
    child_run_id: str,
    child_database: Path,
    master_database: Path,
    social_browser_state: Path,
    browser_startup_timeout: float,
) -> list[str]:
    return [
        str(engage_tiktok.REQUIRED_PYTHON_INTERPRETER),
        str((ROOT / "engage_tiktok.py").resolve()),
        "--database",
        str(child_database),
        "--master-database",
        str(master_database),
        "resume-collect",
        "--run-id",
        str(child_run_id),
        "--social-browser-state",
        str(social_browser_state),
        "--browser-startup-timeout",
        str(browser_startup_timeout),
    ]


def _status_child_command(
    *, child_run_id: str, child_database: Path, master_database: Path
) -> list[str]:
    return [
        str(engage_tiktok.REQUIRED_PYTHON_INTERPRETER),
        str((ROOT / "engage_tiktok.py").resolve()),
        "--database",
        str(child_database),
        "--master-database",
        str(master_database),
        "status",
        "--run-id",
        str(child_run_id),
    ]


def _execute_child(command: Sequence[str]) -> tuple[int, dict[str, Any] | None]:
    if isinstance(command, (str, bytes)):
        raise TopContentRunnerError("child command must be an argument list")
    completed = subprocess.run(
        list(command),
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        shell=False,
    )
    payload: dict[str, Any] | None = None
    try:
        decoded = json.loads(completed.stdout)
        if isinstance(decoded, dict):
            payload = decoded
    except (TypeError, json.JSONDecodeError):
        payload = None
    return int(completed.returncode), payload


def _lookup_child(
    *, child_database: Path, project: str, canonical_url: str
) -> dict[str, Any] | None:
    if not child_database.is_file():
        return None
    normalized = engage_tiktok.normalize_direct_post_target(canonical_url)["url"]
    connection = sqlite3.connect(child_database, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='engage_tiktok_runs'"
        ).fetchone()
        if table is None:
            return None
        rows = connection.execute(
            """
            SELECT * FROM engage_tiktok_runs
            WHERE project=? AND direct_post_url=?
              AND workflow='listen' AND source_mode='url'
            """,
            (project, normalized),
        ).fetchall()
    finally:
        connection.close()
    if len(rows) > 1:
        raise TopContentRunnerError(
            "multiple child runs match one frozen project and URL"
        )
    if not rows:
        return None
    return {key: rows[0][key] for key in rows[0].keys()}


def _validate_child(
    child: Mapping[str, Any],
    *,
    project: str,
    canonical_url: str,
    master_database: Path,
    settings: Mapping[str, Any],
) -> dict[str, Any]:
    normalized = engage_tiktok.normalize_direct_post_target(canonical_url)
    if str(child.get("project") or "") != project:
        raise TopContentRunnerError("child project binding mismatch")
    if str(child.get("workflow") or "") != "listen":
        raise TopContentRunnerError("child workflow is not LISTEN")
    if str(child.get("source_mode") or "") != "url":
        raise TopContentRunnerError("child source mode is not direct URL")
    if str(child.get("collection_policy") or "") != "new_only":
        raise TopContentRunnerError("child collection policy is not new_only")
    if str(child.get("mode") or "") != "shadow":
        raise TopContentRunnerError("child mode is not shadow")
    if str(child.get("cardinality_mode") or "") != "fixed":
        raise TopContentRunnerError("child cardinality is not fixed")
    child_target = engage_tiktok.normalize_direct_post_target(
        child.get("direct_post_url")
    )
    if child_target["url"] != normalized["url"]:
        raise TopContentRunnerError("child direct URL binding mismatch")
    if int(child.get("requested_count") or 0) != 1:
        raise TopContentRunnerError("child requested count is not one")
    if int(child.get("max_pages") or 0) != 1:
        raise TopContentRunnerError("child page bound does not match the bridge")
    if int(child.get("max_comments") or -1) != int(settings["max_comments"]):
        raise TopContentRunnerError("child comment bound does not match the bridge")
    try:
        music_catalogs = json.loads(str(child.get("music_catalogs_json") or "[]"))
    except json.JSONDecodeError as exc:
        raise TopContentRunnerError("child music catalog binding is invalid") from exc
    if music_catalogs != settings["music_catalogs"]:
        raise TopContentRunnerError("child music catalog binding mismatch")
    saved_master = str(child.get("master_database") or "")
    if not saved_master or _resolved(saved_master) != master_database:
        raise TopContentRunnerError("child master database binding mismatch")
    run_id = str(child.get("run_id") or "").strip()
    if not run_id:
        raise TopContentRunnerError("child run identity is missing")
    observed_account = (
        str(child.get("observed_account") or "").strip().lstrip("@").casefold()
    )
    saved_expected = (
        str(child.get("expected_account") or "").strip().lstrip("@").casefold()
    )
    frozen_expected = _normalize_account(settings.get("expected_account"))
    if frozen_expected and (
        saved_expected != frozen_expected
        or (observed_account and observed_account != frozen_expected)
    ):
        raise TopContentRunnerError("child TikTok account binding mismatch")
    return {
        "run_id": run_id,
        "status": str(child.get("status") or ""),
        "evidence_ready": int(child.get("evidence_ready") or 0),
        "observed_account": observed_account,
        "expected_account": saved_expected,
    }


def _listen_summary(state: TopContentLinkState, run_id: str) -> dict[str, Any]:
    parent = state.status(run_id)
    jobs = state.listen_jobs(run_id)
    counts = {
        name: 0
        for name in (
            "pending",
            "running",
            "complete",
            "incomplete",
            "blocked",
            "skipped_known",
        )
    }
    for job in jobs:
        counts[job["status"]] += 1
    unresolved = (
        counts["pending"] + counts["running"] + counts["incomplete"] + counts["blocked"]
    )
    terminal = bool(jobs) and unresolved == 0
    if parent["requested_count"] == 0:
        terminal = True
    return {
        "schema_version": RESULT_SCHEMA,
        "operation": "listen_status",
        "run_id": run_id,
        "link_status": parent["status"],
        "links_discovered": parent["link_count"],
        "requested_links": parent["requested_count"],
        "listen_status": "listen_complete" if terminal else "listen_incomplete",
        "new_evidence_complete": counts["complete"],
        "skipped_known": counts["skipped_known"],
        "unresolved": unresolved,
        "job_counts": counts,
        "project": parent["listen_jobs"]["project"],
        "settings_hash": parent["listen_jobs"]["settings_hash"],
        "jobs": jobs,
    }


def _listen_settings(
    *,
    state_status: Mapping[str, Any],
    run_id: str,
    project: str | None,
    child_database: str | Path | None,
    master_database: str | Path | None,
    max_comments: int | None,
    social_browser_state: str | Path | None,
    browser_startup_timeout: float | None,
    expected_account: str | None,
) -> tuple[str, dict[str, Any]]:
    initialized = bool(state_status["listen_jobs"]["initialized"])
    frozen = dict(state_status["listen_jobs"].get("settings") or {})
    if initialized:
        frozen_project = str(state_status["listen_jobs"]["project"])
        chosen_project = _normalize_project(project or frozen_project, run_id=run_id)
        supplied = {
            "child_database": child_database,
            "master_database": master_database,
            "max_comments": max_comments,
            "social_browser_state": social_browser_state,
            "browser_startup_timeout": browser_startup_timeout,
            "expected_account": expected_account,
        }
        for key, value in supplied.items():
            if value is None:
                continue
            normalized_value: Any = value
            if key in {"child_database", "master_database", "social_browser_state"}:
                normalized_value = str(_resolved(str(value)))
            elif key == "max_comments":
                normalized_value = _positive_int(value, key)
            elif key == "browser_startup_timeout":
                normalized_value = _timeout(value)
            elif key == "expected_account":
                normalized_value = _normalize_account(value)
            if frozen.get(key) != normalized_value:
                raise TopContentRunnerError(f"frozen LISTEN setting changed: {key}")
        if chosen_project != frozen_project:
            raise TopContentRunnerError("frozen LISTEN project changed")
        return chosen_project, frozen

    chosen_project = _normalize_project(project, run_id=run_id)
    child = _resolved(child_database or _child_database_for(run_id))
    master = _resolved(
        master_database or tiktok_master_database.DEFAULT_MASTER_DATABASE
    )
    browser_state = _resolved(
        social_browser_state or engage_tiktok.DEFAULT_BROWSER_STATE
    )
    settings = {
        "schema_version": LISTEN_SETTINGS_SCHEMA,
        "child_database": str(child),
        "master_database": str(master),
        "max_comments": _positive_int(
            100 if max_comments is None else max_comments, "max_comments"
        ),
        "music_catalogs": ["musicbrainz"],
        "social_browser_state": str(browser_state),
        "browser_startup_timeout": _timeout(
            browser_startup_timeout
            if browser_startup_timeout is not None
            else engage_tiktok.PROFILE7_STARTUP_TIMEOUT_SECONDS
        ),
        "expected_account": _normalize_account(expected_account or ""),
    }
    return chosen_project, settings


@contextlib.contextmanager
def _exclusive_listen_lock(database: Path, run_id: str) -> Iterator[None]:
    """Use an OS-released byte lock to serialize one parent bridge run."""

    lock_path = database.with_name(f"{database.name}.{run_id}.listen.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover - workstation is Windows
                fcntl_module = __import__("fcntl")
                fcntl_module.flock(
                    handle.fileno(),
                    fcntl_module.LOCK_EX | fcntl_module.LOCK_NB,
                )
        except (OSError, BlockingIOError) as exc:
            raise TopContentRunnerError(
                "another LISTEN bridge process is already running for this link run"
            ) from exc
        yield
    finally:
        with contextlib.suppress(Exception):
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover
                fcntl_module = __import__("fcntl")
                fcntl_module.flock(handle.fileno(), fcntl_module.LOCK_UN)
        handle.close()


def run_listen(
    *,
    database: str | Path,
    run_id: str,
    project: str | None = None,
    child_database: str | Path | None = None,
    master_database: str | Path | None = None,
    max_comments: int | None = None,
    social_browser_state: str | Path | None = None,
    browser_startup_timeout: float | None = None,
    expected_account: str | None = None,
) -> dict[str, Any]:
    normalized_run_id = _validate_run_id(run_id)
    database_path = _resolved(database)
    with _exclusive_listen_lock(database_path, normalized_run_id):
        with TopContentLinkState(database_path) as state:
            parent = state.status(normalized_run_id)
            if parent["status"] != "links_complete":
                raise TopContentRunnerError(
                    "LISTEN requires an exact complete frozen link run"
                )
            chosen_project, settings = _listen_settings(
                state_status=parent,
                run_id=normalized_run_id,
                project=project,
                child_database=child_database,
                master_database=master_database,
                max_comments=max_comments,
                social_browser_state=social_browser_state,
                browser_startup_timeout=browser_startup_timeout,
                expected_account=expected_account,
            )
            state.initialize_listen_jobs(
                normalized_run_id, chosen_project, settings=settings
            )
            child_db = _resolved(settings["child_database"])
            child_db.parent.mkdir(parents=True, exist_ok=True)
            master_db = _resolved(settings["master_database"])
            browser_state = _resolved(settings["social_browser_state"])
            expected = _normalize_account(settings.get("expected_account"))
            timeout = _timeout(settings["browser_startup_timeout"])
            max_comment_count = _positive_int(settings["max_comments"], "max_comments")

            known, _snapshot = _known_snapshot(master_db)
            bound_accounts: set[str] = set()
            stop_batch = False
            for job in state.listen_jobs(normalized_run_id):
                if job["status"] in {"complete", "skipped_known"}:
                    if job["status"] == "complete" and job["child_run_id"]:
                        child = _lookup_child(
                            child_database=child_db,
                            project=chosen_project,
                            canonical_url=job["canonical_url"],
                        )
                        if child is None:
                            raise TopContentRunnerError(
                                "completed parent job lost its child run"
                            )
                        completed_child = _validate_child(
                            child,
                            project=chosen_project,
                            canonical_url=job["canonical_url"],
                            master_database=master_db,
                            settings=settings,
                        )
                        if completed_child["run_id"] != job["child_run_id"]:
                            raise TopContentRunnerError(
                                "completed parent/child run binding mismatch"
                            )
                        if completed_child["status"] != _TERMINAL_CHILD_COMPLETE:
                            raise TopContentRunnerError(
                                "completed parent job disagrees with child state"
                            )
                        if completed_child["evidence_ready"] != 1:
                            raise TopContentRunnerError(
                                "completed child does not contain one evidence-ready post"
                            )
                        if completed_child["observed_account"]:
                            bound_accounts.add(completed_child["observed_account"])
                        if len(bound_accounts) > 1:
                            raise TopContentRunnerError(
                                "child runs are bound to different TikTok accounts"
                            )
                    continue

                post_id = engage_tiktok.normalize_direct_post_target(
                    job["canonical_url"]
                )["post_id"]
                child = _lookup_child(
                    child_database=child_db,
                    project=chosen_project,
                    canonical_url=job["canonical_url"],
                )
                if child is None and post_id in known and job["status"] != "running":
                    state.update_listen_job(
                        normalized_run_id,
                        post_id,
                        status="skipped_known",
                        reason="tiktok_master_known_before_dispatch",
                    )
                    continue
                if stop_batch:
                    continue

                verified: dict[str, Any] | None = None
                if child is not None:
                    verified = _validate_child(
                        child,
                        project=chosen_project,
                        canonical_url=job["canonical_url"],
                        master_database=master_db,
                        settings=settings,
                    )
                    if (
                        job["child_run_id"]
                        and job["child_run_id"] != verified["run_id"]
                    ):
                        raise TopContentRunnerError("parent/child run binding mismatch")
                    state.update_listen_job(
                        normalized_run_id,
                        post_id,
                        status="running",
                        child_run_id=verified["run_id"],
                        child_database=str(child_db),
                    )
                    if verified["observed_account"]:
                        bound_accounts.add(verified["observed_account"])
                    if len(bound_accounts) > 1:
                        raise TopContentRunnerError(
                            "child runs are bound to different TikTok accounts"
                        )
                    if verified["status"] == _TERMINAL_CHILD_COMPLETE:
                        if verified["evidence_ready"] != 1:
                            raise TopContentRunnerError(
                                "complete child does not contain one evidence-ready post"
                            )
                        state.update_listen_job(
                            normalized_run_id,
                            post_id,
                            status="complete",
                            child_run_id=verified["run_id"],
                            child_database=str(child_db),
                        )
                        known.add(post_id)
                        if verified["observed_account"]:
                            bound_accounts.add(verified["observed_account"])
                        continue
                    if verified["status"] not in _RETRYABLE_CHILD:
                        state.update_listen_job(
                            normalized_run_id,
                            post_id,
                            status="blocked",
                            child_run_id=verified["run_id"],
                            child_database=str(child_db),
                            reason=f"child_status_not_resumable:{_bounded_reason(verified['status'], 'unknown')}",
                        )
                        stop_batch = True
                        continue

                child_run_id = verified["run_id"] if verified else ""
                state.update_listen_job(
                    normalized_run_id,
                    post_id,
                    status="running",
                    child_run_id=child_run_id,
                    child_database=str(child_db),
                )
                active_expected = expected
                if not active_expected and len(bound_accounts) == 1:
                    active_expected = next(iter(bound_accounts))
                if len(bound_accounts) > 1:
                    raise TopContentRunnerError(
                        "completed children are bound to different TikTok accounts"
                    )
                if child_run_id:
                    command = _resume_child_command(
                        child_run_id=child_run_id,
                        child_database=child_db,
                        master_database=master_db,
                        social_browser_state=browser_state,
                        browser_startup_timeout=timeout,
                    )
                else:
                    command = _fresh_child_command(
                        project=chosen_project,
                        canonical_url=job["canonical_url"],
                        child_database=child_db,
                        master_database=master_db,
                        max_comments=max_comment_count,
                        social_browser_state=browser_state,
                        browser_startup_timeout=timeout,
                        expected_account=active_expected,
                    )
                exit_code, payload = _execute_child(command)
                recovered = _lookup_child(
                    child_database=child_db,
                    project=chosen_project,
                    canonical_url=job["canonical_url"],
                )
                if recovered is None:
                    status_name = str((payload or {}).get("status") or "")
                    state.update_listen_job(
                        normalized_run_id,
                        post_id,
                        status="blocked",
                        child_database=str(child_db),
                        reason=(
                            "child_run_not_recoverable_after_dispatch"
                            f":exit_{exit_code}:{_bounded_reason(status_name, 'invalid_output')}"
                        ),
                    )
                    stop_batch = True
                    continue
                verified = _validate_child(
                    recovered,
                    project=chosen_project,
                    canonical_url=job["canonical_url"],
                    master_database=master_db,
                    settings=settings,
                )
                state.update_listen_job(
                    normalized_run_id,
                    post_id,
                    status="running",
                    child_run_id=verified["run_id"],
                    child_database=str(child_db),
                )
                if verified["status"] == _TERMINAL_CHILD_COMPLETE:
                    if exit_code != 0 or verified["evidence_ready"] != 1:
                        raise TopContentRunnerError(
                            "child completion payload or evidence count is invalid"
                        )
                    state.update_listen_job(
                        normalized_run_id,
                        post_id,
                        status="complete",
                        child_run_id=verified["run_id"],
                        child_database=str(child_db),
                    )
                    known.add(post_id)
                    if verified["observed_account"]:
                        bound_accounts.add(verified["observed_account"])
                    if len(bound_accounts) > 1:
                        raise TopContentRunnerError(
                            "child runs are bound to different TikTok accounts"
                        )
                    continue
                reason = f"child_collection_incomplete:{_bounded_reason(verified['status'], 'unknown')}"
                if verified["status"] in _RETRYABLE_CHILD and exit_code in {0, 2}:
                    state.update_listen_job(
                        normalized_run_id,
                        post_id,
                        status="incomplete",
                        child_run_id=verified["run_id"],
                        child_database=str(child_db),
                        reason=reason,
                    )
                    continue
                state.update_listen_job(
                    normalized_run_id,
                    post_id,
                    status="blocked",
                    child_run_id=verified["run_id"],
                    child_database=str(child_db),
                    reason=f"child_collection_blocked:exit_{exit_code}",
                )
                stop_batch = True

            return _listen_summary(state, normalized_run_id)


def capabilities() -> dict[str, Any]:
    return {
        "schema_version": CAPABILITY_SCHEMA,
        "source": "tiktok_one_top_content",
        "source_url": DEFAULT_SOURCE_URL,
        "profile": "Edge Profile 7 existing_profile_attach",
        "discovery_persists": [
            "canonical_video_url",
            "video_id",
            "creator_from_url",
            "ordinal",
            "ranking",
            "ranking_position",
            "bounded_filter_provenance",
            "hashes_and_terminal_status",
        ],
        "discovery_never_persists": [
            "captions",
            "comments_or_replies",
            "media",
            "html_or_raw_responses",
            "cookies_headers_or_tokens",
        ],
        "outbound_actions": False,
        "rankings": list(DEFAULT_RANKINGS),
        "observed_positions_per_ranking": 100,
        "listen_bridge": {
            "command": "engage_tiktok.py music-audit --url <url> --posts 1",
            "sequential": True,
            "workflow": "listen",
            "ai": False,
            "publication": False,
        },
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only TikTok One Top Content link discovery"
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_LINK_DATABASE)
    parser.add_argument("--master-database", type=Path, default=None)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("capabilities")

    collect = subparsers.add_parser("collect-links")
    collect.add_argument("--source-url", default=DEFAULT_SOURCE_URL)
    collect.add_argument("--links", type=int, required=True)
    collect.add_argument("--run-id", default="")
    collect.add_argument(
        "--ranking",
        action="append",
        choices=tuple(DEFAULT_RANKINGS),
        default=None,
    )
    collect.add_argument("--for-listen", action="store_true")
    collect.add_argument("--expected-account", default="")
    collect.add_argument("--social-browser-state", type=Path, default=None)
    collect.add_argument("--browser-startup-timeout", type=float, default=None)

    resume = subparsers.add_parser("resume-links")
    resume.add_argument("--run-id", required=True)
    resume.add_argument("--social-browser-state", type=Path, default=None)
    resume.add_argument("--browser-startup-timeout", type=float, default=None)

    status = subparsers.add_parser("status")
    status.add_argument("--run-id", required=True)

    export = subparsers.add_parser("export-links")
    export.add_argument("--run-id", required=True)
    export.add_argument("--output", type=Path, required=True)

    listen = subparsers.add_parser("listen")
    listen.add_argument("--run-id", required=True)
    listen.add_argument("--project")
    listen.add_argument("--child-database", type=Path)
    listen.add_argument("--max-comments", type=int)
    listen.add_argument("--expected-account", default=None)
    listen.add_argument("--social-browser-state", type=Path, default=None)
    listen.add_argument("--browser-startup-timeout", type=float, default=None)

    listen_status = subparsers.add_parser("listen-status")
    listen_status.add_argument("--run-id", required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        database = _resolved(args.database)
        if args.command == "capabilities":
            result = capabilities()
        elif args.command == "collect-links":
            result = asyncio.run(
                collect_links(
                    database=database,
                    source_url=args.source_url,
                    requested_count=args.links,
                    rankings=tuple(args.ranking or DEFAULT_RANKINGS),
                    run_id=args.run_id,
                    for_listen=bool(args.for_listen),
                    master_database=_resolved(
                        args.master_database
                        or tiktok_master_database.DEFAULT_MASTER_DATABASE
                    ),
                    social_browser_state=_resolved(
                        args.social_browser_state or engage_tiktok.DEFAULT_BROWSER_STATE
                    ),
                    browser_startup_timeout=(
                        args.browser_startup_timeout
                        if args.browser_startup_timeout is not None
                        else engage_tiktok.PROFILE7_STARTUP_TIMEOUT_SECONDS
                    ),
                    expected_account=args.expected_account,
                )
            )
        elif args.command == "resume-links":
            result = asyncio.run(
                resume_links(
                    database=database,
                    run_id=args.run_id,
                    social_browser_state=_resolved(
                        args.social_browser_state or engage_tiktok.DEFAULT_BROWSER_STATE
                    ),
                    browser_startup_timeout=(
                        args.browser_startup_timeout
                        if args.browser_startup_timeout is not None
                        else engage_tiktok.PROFILE7_STARTUP_TIMEOUT_SECONDS
                    ),
                )
            )
        elif args.command == "status":
            with TopContentLinkState(database) as state:
                result = {
                    "schema_version": RESULT_SCHEMA,
                    "operation": "status",
                    **state.status(_validate_run_id(args.run_id)),
                }
        elif args.command == "export-links":
            with TopContentLinkState(database) as state:
                result = state.export_links(_validate_run_id(args.run_id))
            output = _resolved(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp")
            temporary.write_text(
                json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            temporary.replace(output)
            result = {
                "schema_version": RESULT_SCHEMA,
                "operation": "export_links",
                "run_id": _validate_run_id(args.run_id),
                "exported_count": len(result["canonical_urls"]),
                "output": str(output),
            }
        elif args.command == "listen":
            result = run_listen(
                database=database,
                run_id=args.run_id,
                project=args.project,
                child_database=args.child_database,
                master_database=args.master_database,
                max_comments=args.max_comments,
                social_browser_state=args.social_browser_state,
                browser_startup_timeout=args.browser_startup_timeout,
                expected_account=args.expected_account,
            )
        elif args.command == "listen-status":
            with TopContentLinkState(database) as state:
                result = _listen_summary(state, _validate_run_id(args.run_id))
        else:  # pragma: no cover - argparse owns this boundary
            raise TopContentRunnerError("unsupported command")
        _json_result(result)
        status = str(result.get("status") or result.get("listen_status") or "")
        return 2 if status in {"links_incomplete", "listen_incomplete"} else 0
    except (
        TopContentBrowserError,
        TopContentRunnerError,
        TopContentStateError,
        ValueError,
        sqlite3.Error,
        OSError,
    ) as exc:
        _json_result(
            {
                "schema_version": RESULT_SCHEMA,
                "status": "blocked",
                "error": _bounded_reason(str(exc), type(exc).__name__),
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
