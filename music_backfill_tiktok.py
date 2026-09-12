#!/usr/bin/env python
"""Append-only music-evidence upgrades for previously collected TikTok posts.

``MUSIC AUDIT BACKFILL`` is deliberately narrower than a canonical LISTEN
refresh.  It freezes eligible posts from the workspace master registry,
revisits TikTok only for structured music declarations, runs the configured
deterministic catalog enrichment, and appends a new music observation linked
to the original evidence snapshot.  It never collects comments, transcripts,
or subtitles and has no analysis, drafting, authorization, or publication
stage.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import re
import sqlite3
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Sequence
from urllib.parse import urlsplit

import tiktok_master_database as master_state


TARGET_MUSIC_SCHEMA = "tiktok-music-evidence-v3"
BACKFILL_EXPORT_SCHEMA = "tiktok-music-backfill-export-v1"
BACKFILL_OBSERVATION_SCHEMA = "tiktok-music-backfill-observation-v1"
DEFAULT_RETRY_STATUSES = ("unavailable", "rate_limited", "provider_error")
VALID_RETRY_STATUSES = frozenset(DEFAULT_RETRY_STATUSES)
TERMINAL_OBSERVATION_STATUSES = frozenset({"completed", "unavailable"})
DEFAULT_BROWSER_STATE = Path("comments_data") / "social_browser" / "state.json"
# New backfills retain TikTok declarations and the Apple/iTunes mapping path.
# MusicBrainz remains recognizable only in already-frozen historical runs.
DEFAULT_MUSIC_CATALOGS: tuple[str, ...] = ()
MUSIC_EVIDENCE_FIELDS = frozenset(
    {
        "schema_version",
        "configured_catalogs",
        "platform_music",
        "platform_contained_recording",
        "tt2dsp_resolution",
        "catalogs",
        "identity_status",
        "acoustic_verification",
        "lyrics",
        "limitations",
        "music_evidence_hash",
    }
)


class MusicBackfillError(RuntimeError):
    """Base error for a fail-closed music-only backfill."""


class CreatorInventoryIncompleteError(MusicBackfillError):
    """Raised when absent creator posts cannot be proved by a terminal feed."""


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, sqlite3.Row):
        return {key: value[key] for key in value.keys()}
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError("expected a mapping or SQLite row")


def _commit(conn: Any) -> None:
    commit = getattr(conn, "commit", None)
    if callable(commit):
        commit()


def _rollback(conn: Any) -> None:
    rollback = getattr(conn, "rollback", None)
    if callable(rollback):
        rollback()


def _parse_json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if not isinstance(value, str) or not value.strip():
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _post_id(value: str) -> str:
    normalized = _text(value)
    if re.fullmatch(r"\d+", normalized) is None:
        raise argparse.ArgumentTypeError("post ID must contain digits only")
    return normalized


def _retry_status(value: str) -> str:
    normalized = _text(value).casefold()
    if normalized not in VALID_RETRY_STATUSES:
        choices = ", ".join(sorted(VALID_RETRY_STATUSES))
        raise argparse.ArgumentTypeError(f"retry status must be one of: {choices}")
    return normalized


def _normalize_creator(value: Any) -> str:
    raw = _text(value)
    if not raw:
        raise ValueError("creator handle is required")
    if re.match(r"^https?://", raw, flags=re.IGNORECASE):
        parsed = urlsplit(raw)
        if (parsed.hostname or "").rstrip(".").casefold() not in {
            "tiktok.com",
            "www.tiktok.com",
            "m.tiktok.com",
        }:
            raise ValueError("creator profile URL must use tiktok.com")
        match = re.fullmatch(r"/@([^/]+)/?", parsed.path)
        if match is None:
            raise ValueError("expected an exact TikTok creator profile URL")
        raw = match.group(1)
    handle = raw.lstrip("@").strip()
    if not handle or len(handle) > 64 or re.fullmatch(r"[A-Za-z0-9._]+", handle) is None:
        raise ValueError("invalid TikTok creator handle")
    return handle.casefold()


def _normalize_direct_url(value: Any) -> dict[str, str]:
    raw = _text(value)
    parsed = urlsplit(raw)
    if parsed.scheme.casefold() != "https" or (parsed.hostname or "").casefold() not in {
        "tiktok.com",
        "www.tiktok.com",
    }:
        raise ValueError("direct URL must use canonical TikTok HTTPS")
    match = re.fullmatch(
        r"/@([A-Za-z0-9._-]+)/(video|photo)/(\d+)/?",
        parsed.path,
        flags=re.IGNORECASE,
    )
    if match is None:
        raise ValueError(
            "direct URL must be /@handle/video/<id> or /@handle/photo/<id>"
        )
    creator, content_type, post_id = (
        match.group(1).casefold(),
        match.group(2).casefold(),
        match.group(3),
    )
    return {
        "post_id": post_id,
        "creator": creator,
        "content_type": content_type,
        "url": f"https://www.tiktok.com/@{creator}/{content_type}/{post_id}",
    }


def _candidate_record(value: Any) -> dict[str, Any]:
    source = _mapping(value)
    post_id = _text(source.get("post_id") or source.get("id"))
    canonical_url = _text(
        source.get("canonical_url") or source.get("url") or source.get("video_url")
    )
    creator = _text(
        source.get("creator_handle") or source.get("creator") or source.get("username")
    ).lstrip("@").casefold()
    content_type = _text(source.get("content_type")).casefold()
    if not content_type:
        content_type = "photo" if "/photo/" in urlsplit(canonical_url).path else "video"
    if not post_id or not canonical_url:
        raise MusicBackfillError("frozen candidate is missing post_id or canonical_url")
    return {
        **source,
        "post_id": post_id,
        "canonical_url": canonical_url,
        "creator_handle": creator,
        "content_type": content_type,
        "base_snapshot_id": _text(source.get("base_snapshot_id")),
        "base_evidence_hash": _text(source.get("base_evidence_hash")),
        "base_observed_at": _text(source.get("base_observed_at")),
    }


def _run_candidates(run: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = run.get("candidates")
    if raw is None:
        raw = run.get("candidate_set_json")
    if raw is None:
        raw = run.get("candidates_json")
    return [_candidate_record(item) for item in _parse_json_list(raw)]


def _run_catalogs(run: Mapping[str, Any]) -> tuple[str, ...]:
    """Use the frozen catalog set, including an explicitly empty set."""

    raw = run.get("configured_catalogs")
    if raw is None:
        raw = run.get("configured_catalogs_json")
    if raw is None:
        return DEFAULT_MUSIC_CATALOGS
    parsed = _parse_json_list(raw)
    catalogs = tuple(
        dict.fromkeys(_text(value).casefold() for value in parsed if _text(value))
    )
    return catalogs


def _connect_master_readonly(path: str | Path) -> sqlite3.Connection:
    """Open the registry without creating, migrating, or journaling it."""

    database = Path(path).resolve()
    if not database.is_file():
        raise FileNotFoundError(database)
    connection = sqlite3.connect(
        database.as_uri() + "?mode=ro",
        uri=True,
        timeout=30,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


def _assert_master_binding(
    conn: sqlite3.Connection,
    run: Mapping[str, Any],
) -> None:
    saved = _text(run.get("master_database_path"))
    if not saved:
        return
    row = next(
        (
            item
            for item in conn.execute("PRAGMA database_list").fetchall()
            if _text(item[1]) == "main"
        ),
        None,
    )
    actual = _text(row[2]) if row is not None else ""
    if not actual or Path(actual).resolve() != Path(saved).resolve():
        raise MusicBackfillError(
            "music backfill run is bound to a different master database"
        )


def _observation_rows(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    validate_base_snapshots: bool = True,
) -> list[dict[str, Any]]:
    function = getattr(master_state, "music_backfill_observations")
    rows = function(
        conn,
        "main",
        run_id=run_id,
        validate_base_snapshots=validate_base_snapshots,
    )
    return [_mapping(row) for row in rows]


def _completed_post_ids(observations: Sequence[Mapping[str, Any]]) -> set[str]:
    return {
        _text(row.get("post_id"))
        for row in observations
        if _text(row.get("status")).casefold() in TERMINAL_OBSERVATION_STATUSES
        and _text(row.get("post_id"))
    }


def _sanitize_music_evidence(
    supplied: Mapping[str, Any],
    *,
    expected_catalogs: Sequence[str],
) -> dict[str, Any]:
    """Rebuild through the canonical closed schema before persistence/export."""

    from engage_tiktok import sanitized_terminal_music_evidence

    rebuilt, issues = sanitized_terminal_music_evidence(
        supplied,
        expected_catalogs=expected_catalogs,
    )
    if issues:
        raise MusicBackfillError(
            "music evidence failed closed-schema validation: " + ", ".join(issues)
        )
    if rebuilt != dict(supplied):
        mismatches = []
        for k in supplied:
            if k not in rebuilt: mismatches.append(f"{k} missing in rebuilt")
            elif supplied[k] != rebuilt[k]: mismatches.append(f"{k} mismatch: supplied={supplied[k]} != rebuilt={rebuilt[k]}")
        for k in rebuilt:
            if k not in supplied: mismatches.append(f"{k} extra in rebuilt")
        raise MusicBackfillError(f"music evidence contains fields outside the canonical safe projection. Mismatches: {mismatches}")
    return rebuilt


def _scope_from_args(args: argparse.Namespace) -> tuple[str, str, dict[str, Any]]:
    if getattr(args, "creator", None):
        handle = _normalize_creator(args.creator)
        return "creator", handle, {"creator_handle": handle}
    if getattr(args, "topic", None):
        topic = re.sub(r"\s+", " ", _text(args.topic))
        if not topic:
            raise ValueError("topic cannot be empty")
        return "topic", topic, {"topic": topic}
    if getattr(args, "url", None):
        target = _normalize_direct_url(args.url)
        return "url", target["url"], {"direct_url": target["url"]}
    post_ids = list(dict.fromkeys(getattr(args, "post_id", None) or ()))
    if post_ids:
        return "post_ids", json.dumps(post_ids), {"post_ids": post_ids}
    raise ValueError("exactly one backfill scope is required")


def _exact_scope_post_ids(
    scope_mode: str,
    selector: Mapping[str, Any],
) -> tuple[str, ...]:
    """Return every exact target that must already exist in the master registry."""

    if scope_mode == "post_ids":
        return tuple(
            dict.fromkeys(
                _text(value)
                for value in selector.get("post_ids", ())
                if _text(value)
            )
        )
    if scope_mode == "url":
        target = _normalize_direct_url(selector.get("direct_url"))
        return (target["post_id"],)
    return ()


def _validate_exact_scope_targets(
    *,
    scope_mode: str,
    scope_value: str,
    selector: Mapping[str, Any],
    registry_rows: Sequence[Any],
) -> None:
    """Reject unknown exact targets before eligibility filtering or run creation."""

    requested_ids = _exact_scope_post_ids(scope_mode, selector)
    if not requested_ids:
        return
    registry_candidates = [_candidate_record(row) for row in registry_rows]
    by_id = {candidate["post_id"]: candidate for candidate in registry_candidates}
    missing = [post_id for post_id in requested_ids if post_id not in by_id]
    if missing:
        label = "post ID" if len(missing) == 1 else "post IDs"
        raise MusicBackfillError(
            f"unknown music backfill {label}: {', '.join(missing)}"
        )

    if scope_mode == "url":
        requested = _normalize_direct_url(scope_value)
        frozen = _normalize_direct_url(by_id[requested["post_id"]]["canonical_url"])
        if any(
            requested[field] != frozen[field]
            for field in ("post_id", "creator", "content_type")
        ):
            raise MusicBackfillError(
                "unknown music backfill URL: the exact URL is not bound to the "
                "known master-registry post"
            )


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


@dataclass
class MusicObservation:
    candidate: dict[str, Any]
    music_observed_at: str
    music_evidence: dict[str, Any]
    status: str = "completed"
    error: str = ""


ObservationCallback = Callable[[MusicObservation], Any | Awaitable[Any]]


class MusicBackfillBrowserCollector:
    """Use Profile 7 to collect only current TikTok music declarations."""

    def __init__(
        self,
        *,
        state_path: Path = DEFAULT_BROWSER_STATE,
        preflight: Any = None,
        integration_factory: Callable[[], Any] | None = None,
        music_collector: Any = None,
    ) -> None:
        self.state_path = Path(state_path)
        self.preflight = preflight
        self.integration_factory = integration_factory
        self.music_collector = music_collector

    @staticmethod
    def _music_input(candidate: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "id": _text(candidate.get("post_id")),
            "url": _text(candidate.get("canonical_url")),
            "username": _text(candidate.get("creator_handle")),
            "content_type": _text(candidate.get("content_type")) or "video",
        }

    @staticmethod
    def _unavailable_input(
        candidate: Mapping[str, Any],
        *,
        reason: str,
    ) -> dict[str, Any]:
        record = MusicBackfillBrowserCollector._music_input(candidate)
        record.update(
            {
                "music_metadata_status": "unavailable",
                "music_metadata_source": "tiktok_item_music",
                "music_contained_recording_status": "unavailable",
                "music_contained_recording_source": "",
                "music_contained_recording_reason": reason,
            }
        )
        return record

    @staticmethod
    def _music_projection(
        candidate: Mapping[str, Any],
        source: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Discard captions, metrics, subtitle manifests, and all other fields."""

        projected = MusicBackfillBrowserCollector._music_input(candidate)
        for key, value in source.items():
            if key.startswith("music_") or key in {
                "post_duration_ms",
                "post_duration_seconds",
                "duration_seconds",
            }:
                projected[key] = value
        return projected

    @staticmethod
    def _validated_frozen_exact_target(
        candidate: Mapping[str, Any],
    ) -> dict[str, str]:
        target = _normalize_direct_url(_text(candidate.get("canonical_url")))
        expected = {
            "post_id": _text(candidate.get("post_id")),
            "creator": _text(candidate.get("creator_handle")).lstrip("@").casefold(),
            "content_type": _text(candidate.get("content_type")).casefold(),
        }
        for key, value in expected.items():
            if not value or target[key] != value:
                raise MusicBackfillError(
                    f"frozen exact target identity mismatch for {expected['post_id'] or '<unknown>'}"
                )
        return target

    @staticmethod
    def _validate_profile_fallback_match(
        candidate: Mapping[str, Any],
        row: Mapping[str, Any],
    ) -> None:
        target = MusicBackfillBrowserCollector._validated_frozen_exact_target(candidate)
        row_id = _text(row.get("id") or row.get("post_id"))
        row_owner = _text(
            row.get("username") or row.get("creator") or row.get("creator_handle")
        ).lstrip("@").casefold()
        if row_id != target["post_id"] or row_owner != target["creator"]:
            raise MusicBackfillError(
                f"profile fallback identity mismatch for post {target['post_id']}"
            )

        row_type = _text(row.get("content_type")).casefold()
        row_url = _text(row.get("url") or row.get("canonical_url") or row.get("video_url"))
        if row_url:
            parsed_row = _normalize_direct_url(row_url)
            if any(
                parsed_row[key] != target[key]
                for key in ("post_id", "creator", "content_type")
            ):
                raise MusicBackfillError(
                    f"profile fallback URL mismatch for post {target['post_id']}"
                )
        elif row_type and row_type != target["content_type"]:
            raise MusicBackfillError(
                f"profile fallback media type mismatch for post {target['post_id']}"
            )

    @staticmethod
    async def _terminal_creator_inventory(
        *,
        page: Any,
        integration: Any,
        creator_handle: str,
        max_pages: int | None,
    ) -> tuple[dict[str, dict[str, Any]], bool]:
        """Return one exact-owner profile inventory and its terminal status."""

        target_handle = _normalize_creator(creator_handle)
        inventory = await integration.discover_creator_profile_posts(
            page,
            target_handle,
            collect_all=True,
            max_pages=max_pages,
        )
        diagnostics = getattr(integration, "last_creator_profile_diagnostics", {})
        diagnostics = diagnostics if isinstance(diagnostics, Mapping) else {}
        diagnostic_target = _text(diagnostics.get("target_handle")).lstrip("@").casefold()
        if diagnostic_target and diagnostic_target != target_handle:
            raise MusicBackfillError("creator inventory diagnostic target mismatch")
        terminal = bool(
            diagnostics.get("terminal_verified")
            and diagnostics.get("inventory_complete")
        )
        inventory_by_id: dict[str, dict[str, Any]] = {}
        for raw in inventory:
            row = dict(raw)
            post_id = _text(row.get("id") or row.get("post_id"))
            owner = _text(
                row.get("username") or row.get("creator") or row.get("creator_handle")
            ).lstrip("@").casefold()
            if owner != target_handle:
                raise MusicBackfillError(
                    f"creator inventory owner mismatch for post {post_id or '<unknown>'}"
                )
            if post_id:
                inventory_by_id[post_id] = row
        return inventory_by_id, terminal

    async def _enrich(
        self,
        record: Mapping[str, Any],
        *,
        configured_catalogs: Sequence[str],
        request_slot_reserver: Callable[[str, float], float] | None,
        provider_cooldown: Callable[[str, float], None] | None,
    ) -> dict[str, Any]:
        if self.music_collector is None:
            from engage_tiktok import TikTokBrowserCollector

            self.music_collector = TikTokBrowserCollector(state_path=self.state_path)
        method = getattr(self.music_collector, "enrich_music_record", None)
        if method is None:
            # Kept only for compatibility with an in-progress checkout.  The
            # public wrapper is the production interface.
            method = getattr(self.music_collector, "_enrich_music")
            return await method(
                record,
                configured_catalogs,
                request_slot_reserver=request_slot_reserver,
                provider_cooldown=provider_cooldown,
            )
        return await method(
            record,
            configured_catalogs=configured_catalogs,
            request_slot_reserver=request_slot_reserver,
            provider_cooldown=provider_cooldown,
        )

    async def _emit(
        self,
        candidate: Mapping[str, Any],
        record: Mapping[str, Any],
        *,
        observed_at: str,
        error: str,
        observation_callback: ObservationCallback,
        configured_catalogs: Sequence[str],
        request_slot_reserver: Callable[[str, float], float] | None,
        provider_cooldown: Callable[[str, float], None] | None,
    ) -> MusicObservation:
        music_evidence = await self._enrich(
            record,
            configured_catalogs=configured_catalogs,
            request_slot_reserver=request_slot_reserver,
            provider_cooldown=provider_cooldown,
        )
        observation = MusicObservation(
            candidate=dict(candidate),
            music_observed_at=observed_at,
            music_evidence=music_evidence,
            status="unavailable" if error else "completed",
            error=error,
        )
        await _maybe_await(observation_callback(observation))
        return observation

    async def collect_with_page(
        self,
        *,
        page: Any,
        integration: Any,
        candidates: Sequence[Mapping[str, Any]],
        scope_mode: str,
        scope_value: str,
        max_pages: int | None,
        observation_callback: ObservationCallback,
        configured_catalogs: Sequence[str] = DEFAULT_MUSIC_CATALOGS,
        request_slot_reserver: Callable[[str, float], float] | None = None,
        provider_cooldown: Callable[[str, float], None] | None = None,
    ) -> list[MusicObservation]:
        """Collect and checkpoint only music fields using an initialized page."""

        normalized = [_candidate_record(candidate) for candidate in candidates]
        observations: list[MusicObservation] = []
        if scope_mode == "creator":
            target_handle = _normalize_creator(scope_value)
            inventory_by_id, terminal = await self._terminal_creator_inventory(
                page=page,
                integration=integration,
                creator_handle=target_handle,
                max_pages=max_pages,
            )

            missing = [
                candidate["post_id"]
                for candidate in normalized
                if candidate["post_id"] not in inventory_by_id
            ]
            if missing and not terminal:
                raise CreatorInventoryIncompleteError(
                    "creator inventory was not terminal; missing frozen posts cannot "
                    "be classified as unavailable"
                )

            for candidate in normalized:
                post_id = candidate["post_id"]
                observed_at = master_state.now_iso()
                record = inventory_by_id.get(post_id)
                error = ""
                if record is None:
                    error = "post_absent_from_terminal_creator_inventory"
                    record = self._unavailable_input(candidate, reason=error)
                else:
                    record = self._music_projection(candidate, record)
                observation = await self._emit(
                    candidate,
                    record,
                    observed_at=observed_at,
                    error=error,
                    observation_callback=observation_callback,
                    configured_catalogs=configured_catalogs,
                    request_slot_reserver=request_slot_reserver,
                    provider_cooldown=provider_cooldown,
                )
                observations.append(observation)
            return observations

        records = [self._music_input(candidate) for candidate in normalized]
        await integration.refresh_video_candidates_from_html(page, records)

        # Exact URL/ID backfills must never substitute another post, but a
        # direct HTML response may omit TikTok's item.music object even when
        # the authenticated creator feed exposes it.  Revisit only the exact
        # frozen owners and use profile rows only for failed exact targets.
        profile_fallback: dict[str, dict[str, Any]] = {}
        terminal_profile_absences: set[str] = set()
        if scope_mode in {"url", "post_ids"}:
            failed_by_creator: dict[str, list[dict[str, Any]]] = {}
            for candidate, record in zip(normalized, records):
                if record.get("metadata_refresh_ok") is True:
                    continue
                target = self._validated_frozen_exact_target(candidate)
                creator = target["creator"]
                failed_by_creator.setdefault(creator, []).append(candidate)

            for creator, failed_candidates in failed_by_creator.items():
                inventory_by_id, terminal = await self._terminal_creator_inventory(
                    page=page,
                    integration=integration,
                    creator_handle=creator,
                    max_pages=max_pages,
                )
                missing = [
                    candidate["post_id"]
                    for candidate in failed_candidates
                    if candidate["post_id"] not in inventory_by_id
                ]
                if missing and not terminal:
                    raise CreatorInventoryIncompleteError(
                        "creator inventory was not terminal; failed exact targets "
                        "cannot be classified as unavailable"
                    )
                for candidate in failed_candidates:
                    post_id = candidate["post_id"]
                    row = inventory_by_id.get(post_id)
                    if row is None:
                        terminal_profile_absences.add(post_id)
                    else:
                        self._validate_profile_fallback_match(candidate, row)
                        profile_fallback[post_id] = row

        for candidate, record in zip(normalized, records):
            observed_at = master_state.now_iso()
            error = ""
            post_id = candidate["post_id"]
            fallback_record = profile_fallback.get(post_id)
            if fallback_record is not None:
                record = self._music_projection(candidate, fallback_record)
            elif post_id in terminal_profile_absences:
                error = "post_absent_from_terminal_creator_inventory"
                record = self._unavailable_input(candidate, reason=error)
            elif record.get("metadata_refresh_ok") is not True:
                error = "tiktok_music_metadata_unavailable"
                record = self._unavailable_input(candidate, reason=error)
            else:
                record = self._music_projection(candidate, record)
            observation = await self._emit(
                candidate,
                record,
                observed_at=observed_at,
                error=error,
                observation_callback=observation_callback,
                configured_catalogs=configured_catalogs,
                request_slot_reserver=request_slot_reserver,
                provider_cooldown=provider_cooldown,
            )
            observations.append(observation)
        return observations

    async def collect(
        self,
        *,
        candidates: Sequence[Mapping[str, Any]],
        scope_mode: str,
        scope_value: str,
        expected_account: str,
        max_pages: int | None,
        observation_callback: ObservationCallback,
        configured_catalogs: Sequence[str] = DEFAULT_MUSIC_CATALOGS,
        account_callback: Callable[[str], Any | Awaitable[Any]] | None = None,
        request_slot_reserver: Callable[[str, float], float] | None = None,
        provider_cooldown: Callable[[str, float], None] | None = None,
    ) -> list[MusicObservation]:
        """Run Profile 7 preflight, collect, then close only the temporary page."""

        from engage_tiktok import SocialBrowserPreflight, TikTokBrowserCollector
        from playwright.async_api import async_playwright
        from social_browser import (
            load_engage_profile7_designation,
            platform_authentication,
            verified_profile_context,
        )
        from tiktok_scraper.api_integration import TikTokAPIIntegration

        preflight = self.preflight or SocialBrowserPreflight(
            state_path=self.state_path,
            expected_account=expected_account,
        )
        preflight_result = await preflight.ensure_ready()
        observed_account = _text(
            (preflight_result or {}).get("observed_account")
            if isinstance(preflight_result, Mapping)
            else ""
        ).lstrip("@").casefold()
        if account_callback is not None and observed_account:
            await _maybe_await(account_callback(observed_account))
        if self.music_collector is None:
            self.music_collector = TikTokBrowserCollector(state_path=self.state_path)
        cdp_url = self.music_collector._cdp_url()
        designation = load_engage_profile7_designation(self.state_path.resolve().parent)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.connect_over_cdp(cdp_url)
            if not browser.contexts:
                raise MusicBackfillError("verified Profile 7 has no active context")
            context, _ = await verified_profile_context(browser, designation)
            authentication = await platform_authentication(context, "tiktok")
            if not authentication.get("authenticated"):
                raise MusicBackfillError("TikTok is logged out in verified Profile 7")
            page = await context.new_page()
            integration = None
            try:
                integration = (
                    self.integration_factory()
                    if self.integration_factory is not None
                    else TikTokAPIIntegration(
                        enable_api=True,
                        persist_session_secrets=False,
                    )
                )
                if not await integration.initialize_api(page):
                    raise MusicBackfillError(
                        "TikTok music backfill could not initialize from Profile 7"
                    )
                return await self.collect_with_page(
                    page=page,
                    integration=integration,
                    candidates=candidates,
                    scope_mode=scope_mode,
                    scope_value=scope_value,
                    max_pages=max_pages,
                    observation_callback=observation_callback,
                    configured_catalogs=configured_catalogs,
                    request_slot_reserver=request_slot_reserver,
                    provider_cooldown=provider_cooldown,
                )
            finally:
                api = getattr(integration, "api", None)
                session = getattr(api, "session", None)
                close_session = getattr(session, "close", None)
                if callable(close_session):
                    try:
                        await _maybe_await(close_session())
                    except Exception:
                        pass
                if not page.is_closed():
                    await page.close()


def _register_run(
    conn: sqlite3.Connection,
    *,
    args: argparse.Namespace,
    scope_mode: str,
    scope_value: str,
    candidates: Sequence[Mapping[str, Any]],
    retry_statuses: Sequence[str],
) -> str:
    run_id = f"music_backfill_{uuid.uuid4().hex[:16]}"
    try:
        master_state.register_music_backfill_run(
            conn,
            "main",
            run_id=run_id,
            project=args.project,
            scope_mode=scope_mode,
            scope_value=scope_value,
            target_schema_version=args.target_schema,
            retryable_statuses=tuple(retry_statuses),
            candidates=[dict(candidate) for candidate in candidates],
            configured_catalogs=DEFAULT_MUSIC_CATALOGS,
            force=bool(args.force),
            expected_account=_text(args.expected_account).lstrip("@").casefold(),
        )
        _commit(conn)
    except Exception:
        _rollback(conn)
        raise
    return run_id


def _update_run(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    status: str,
    error: str = "",
) -> None:
    updater = getattr(master_state, "update_music_backfill_run")
    try:
        updater(conn, "main", run_id=run_id, status=status, error=error)
        _commit(conn)
    except Exception:
        _rollback(conn)
        raise


def _finalize_run(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    error: str = "",
) -> None:
    try:
        master_state.finalize_music_backfill_run(
            conn,
            "main",
            run_id=run_id,
            error=error,
        )
        _commit(conn)
    except Exception:
        _rollback(conn)
        raise


def _status_packet(conn: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    row = master_state.get_music_backfill_run(conn, "main", run_id=run_id)
    if row is None:
        raise MusicBackfillError(f"unknown music backfill run: {run_id}")
    run = _mapping(row)
    _assert_master_binding(conn, run)
    candidates = _run_candidates(run)
    observations = _observation_rows(
        conn,
        run_id,
        validate_base_snapshots=False,
    )
    by_status: dict[str, int] = {}
    for observation in observations:
        status = _text(observation.get("status")).casefold() or "unknown"
        by_status[status] = by_status.get(status, 0) + 1
    complete_ids = _completed_post_ids(observations)
    return {
        "run_id": run_id,
        "workflow": "music_audit_backfill",
        "collection_only": True,
        "status": _text(run.get("status")),
        "project": _text(run.get("project")),
        "scope_mode": _text(run.get("scope_mode")),
        "scope_value": _text(run.get("scope_value")),
        "target_schema_version": _text(run.get("target_schema_version")),
        "configured_catalogs": list(_run_catalogs(run)),
        "selected": len(candidates),
        "completed": len(complete_ids),
        "pending": max(0, len(candidates) - len(complete_ids)),
        "observations_by_status": by_status,
        "error": _text(run.get("error") or run.get("last_error")),
        "created_at": _text(run.get("created_at")),
        "updated_at": _text(run.get("updated_at")),
    }


async def _execute_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    expected_account_override: str = "",
    max_pages: int | None = None,
    collector: MusicBackfillBrowserCollector | None = None,
) -> dict[str, Any]:
    row = master_state.get_music_backfill_run(conn, "main", run_id=run_id)
    if row is None:
        raise MusicBackfillError(f"unknown music backfill run: {run_id}")
    run = _mapping(row)
    _assert_master_binding(conn, run)
    candidates = _run_candidates(run)
    completed = _completed_post_ids(
        _observation_rows(
            conn,
            run_id,
            validate_base_snapshots=False,
        )
    )
    pending = [item for item in candidates if item["post_id"] not in completed]
    if not pending:
        _finalize_run(conn, run_id)
        return _status_packet(conn, run_id)

    saved_account = _text(run.get("expected_account")).lstrip("@").casefold()
    override = _text(expected_account_override).lstrip("@").casefold()
    if saved_account and override and saved_account != override:
        raise MusicBackfillError(
            f"resume account mismatch: saved @{saved_account}, requested @{override}"
        )
    expected_account = saved_account or override
    configured_catalogs = _run_catalogs(run)

    def reserve_slot(provider: str, interval: float) -> float:
        return master_state.reserve_provider_request_slot(
            conn,
            "main",
            provider=provider,
            minimum_interval_seconds=interval,
        )

    def defer_provider(provider: str, delay: float) -> None:
        master_state.defer_provider_requests(
            conn,
            "main",
            provider=provider,
            delay_seconds=delay,
        )

    def checkpoint(observation: MusicObservation) -> None:
        candidate = observation.candidate
        safe_music_evidence = _sanitize_music_evidence(
            observation.music_evidence,
            expected_catalogs=configured_catalogs,
        )
        try:
            master_state.record_music_backfill_observation(
                conn,
                "main",
                run_id=run_id,
                post_id=candidate["post_id"],
                base_snapshot_id=candidate["base_snapshot_id"],
                base_evidence_hash=candidate["base_evidence_hash"],
                base_observed_at=candidate["base_observed_at"],
                music_observed_at=observation.music_observed_at,
                music_evidence=safe_music_evidence,
                music_evidence_hash=_text(
                    safe_music_evidence.get("music_evidence_hash")
                ),
                status=observation.status,
                error=observation.error,
            )
            _commit(conn)
        except Exception:
            _rollback(conn)
            raise

    def bind_observed_account(observed_account: str) -> None:
        try:
            master_state.update_music_backfill_run(
                conn,
                "main",
                run_id=run_id,
                observed_account=observed_account,
            )
            _commit(conn)
        except Exception:
            _rollback(conn)
            raise

    _update_run(conn, run_id, status="running")
    collector = collector or MusicBackfillBrowserCollector()
    try:
        await collector.collect(
            candidates=pending,
            scope_mode=_text(run.get("scope_mode")),
            scope_value=_text(run.get("scope_value")),
            expected_account=expected_account,
            max_pages=max_pages,
            observation_callback=checkpoint,
            configured_catalogs=configured_catalogs,
            account_callback=bind_observed_account,
            request_slot_reserver=reserve_slot,
            provider_cooldown=defer_provider,
        )
    except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"
        _finalize_run(
            conn,
            run_id,
            error=failure,
        )
        raise MusicBackfillError(failure) from exc

    refreshed = _status_packet(conn, run_id)
    _finalize_run(
        conn,
        run_id,
        error=(
            "collector returned before every frozen candidate became terminal"
            if refreshed["pending"]
            else ""
        ),
    )
    return _status_packet(conn, run_id)


def _run_command(args: argparse.Namespace) -> dict[str, Any]:
    scope_mode, scope_value, selector = _scope_from_args(args)
    retry_statuses = tuple(
        dict.fromkeys(args.retry_status or DEFAULT_RETRY_STATUSES)
    )
    conn = master_state.connect_master(args.master_database)
    try:
        selection_options = {
            "creator_handle": selector.get("creator_handle", ""),
            "topic": selector.get("topic", ""),
            "post_ids": tuple(selector.get("post_ids", ())),
            "direct_url": selector.get("direct_url", ""),
            "target_schema_version": args.target_schema,
            "retryable_statuses": retry_statuses,
        }
        exact_target_ids = _exact_scope_post_ids(scope_mode, selector)
        forced_registry_rows: Sequence[Any] | None = None
        if exact_target_ids:
            forced_registry_rows = master_state.select_music_backfill_candidates(
                conn,
                "main",
                **selection_options,
                force=True,
            )
            _validate_exact_scope_targets(
                scope_mode=scope_mode,
                scope_value=scope_value,
                selector=selector,
                registry_rows=forced_registry_rows,
            )
        if args.force and forced_registry_rows is not None:
            raw_candidates = forced_registry_rows
        else:
            raw_candidates = master_state.select_music_backfill_candidates(
                conn,
                "main",
                **selection_options,
                force=bool(args.force),
            )
        candidates = [_candidate_record(row) for row in raw_candidates]
        if not args.all_eligible:
            candidates = candidates[: args.limit]
        run_id = _register_run(
            conn,
            args=args,
            scope_mode=scope_mode,
            scope_value=scope_value,
            candidates=candidates,
            retry_statuses=retry_statuses,
        )
        if not candidates:
            _finalize_run(conn, run_id)
            return _status_packet(conn, run_id)
        return asyncio.run(
            _execute_run(
                conn,
                run_id=run_id,
                max_pages=args.max_pages,
            )
        )
    finally:
        conn.close()


def _resume_command(args: argparse.Namespace) -> dict[str, Any]:
    conn = master_state.connect_master(args.master_database)
    try:
        return asyncio.run(
            _execute_run(
                conn,
                run_id=args.run_id,
                expected_account_override=args.expected_account,
                max_pages=args.max_pages,
            )
        )
    finally:
        conn.close()


def _status_command(args: argparse.Namespace) -> dict[str, Any]:
    conn = _connect_master_readonly(args.master_database)
    try:
        return _status_packet(conn, args.run_id)
    finally:
        conn.close()


def _validated_export_observations(
    run: Mapping[str, Any],
    observations: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    candidates = {item["post_id"]: item for item in _run_candidates(run)}
    expected_schema = _text(run.get("target_schema_version"))
    expected_catalogs = list(_run_catalogs(run))
    validated: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in observations:
        observation = dict(raw)
        post_id = _text(observation.get("post_id"))
        if post_id in seen or post_id not in candidates:
            raise MusicBackfillError(
                f"observation is not uniquely bound to the frozen set: {post_id}"
            )
        seen.add(post_id)
        candidate = candidates[post_id]
        for field in ("base_snapshot_id", "base_evidence_hash", "base_observed_at"):
            if _text(observation.get(field)) != _text(candidate.get(field)):
                raise MusicBackfillError(
                    f"observation {post_id} has a mismatched {field} binding"
                )
        if re.fullmatch(r"[0-9a-f]{64}", candidate["base_evidence_hash"]) is None:
            raise MusicBackfillError(
                f"observation {post_id} has an invalid base evidence hash"
            )
        status = _text(observation.get("status")).casefold()
        if status not in {"completed", "unavailable", "failed"}:
            raise MusicBackfillError(
                f"observation {post_id} has an invalid terminal status"
            )
        supplied_music = observation.get("music_evidence")
        if supplied_music is None:
            supplied_music = observation.get("music_evidence_json")
        if isinstance(supplied_music, str):
            try:
                supplied_music = json.loads(supplied_music)
            except json.JSONDecodeError as exc:
                raise MusicBackfillError(
                    f"observation {post_id} has invalid music JSON"
                ) from exc
        if not isinstance(supplied_music, Mapping):
            raise MusicBackfillError(
                f"observation {post_id} has no closed music evidence object"
            )
        music_evidence = dict(supplied_music)
        unexpected = set(music_evidence).difference(MUSIC_EVIDENCE_FIELDS)
        if unexpected:
            raise MusicBackfillError(
                f"observation {post_id} has non-whitelisted music fields"
            )
        music_hash = _text(observation.get("music_evidence_hash")).casefold()
        if status == "failed":
            if music_evidence or music_hash != master_state.json_hash({}):
                raise MusicBackfillError(
                    f"failed observation {post_id} has invalid empty evidence"
                )
        else:
            if _text(music_evidence.get("schema_version")) != expected_schema:
                raise MusicBackfillError(
                    f"observation {post_id} does not match the frozen target schema"
                )
            if music_evidence.get("configured_catalogs") != expected_catalogs:
                raise MusicBackfillError(
                    f"observation {post_id} does not match the frozen catalog set"
                )
            if music_hash != _text(
                music_evidence.get("music_evidence_hash")
            ).casefold():
                raise MusicBackfillError(
                    f"observation {post_id} music hash binding does not match"
                )
            unhashed = dict(music_evidence)
            unhashed.pop("music_evidence_hash", None)
            if (
                re.fullmatch(r"[0-9a-f]{64}", music_hash) is None
                or master_state.json_hash(unhashed) != music_hash
            ):
                raise MusicBackfillError(
                    f"observation {post_id} music evidence hash is invalid"
                )
            music_evidence = _sanitize_music_evidence(
                music_evidence,
                expected_catalogs=expected_catalogs,
            )
        observation_hash = _text(observation.get("observation_hash")).casefold()
        observation_hash_document = {
            "schema_version": BACKFILL_OBSERVATION_SCHEMA,
            "run_id": _text(run.get("run_id")),
            "post_id": post_id,
            "base_snapshot_id": _text(observation.get("base_snapshot_id")),
            "base_evidence_hash": _text(observation.get("base_evidence_hash")),
            "base_observed_at": _text(observation.get("base_observed_at")),
            "music_observed_at": _text(observation.get("music_observed_at")),
            "music_evidence_hash": music_hash,
            "status": status,
            "error": _text(observation.get("error")),
        }
        if (
            re.fullmatch(r"[0-9a-f]{64}", observation_hash) is None
            or master_state.json_hash(observation_hash_document) != observation_hash
        ):
            raise MusicBackfillError(
                f"observation {post_id} append-only hash is invalid"
            )
        validated.append({**observation, "music_evidence": music_evidence})
    return validated


def _export_command(args: argparse.Namespace) -> dict[str, Any]:
    master_database = Path(args.master_database).resolve()
    destination = Path(args.file).resolve()
    blocked_paths = {
        str(master_database).casefold(),
        str(Path(str(master_database) + "-wal")).casefold(),
        str(Path(str(master_database) + "-shm")).casefold(),
    }
    if str(destination).casefold() in blocked_paths:
        raise MusicBackfillError(
            "export destination cannot overwrite the master database or its sidecars"
        )
    conn = _connect_master_readonly(master_database)
    temporary = destination.with_name(
        f".{destination.name}.{uuid.uuid4().hex}.tmp"
    )
    try:
        run_row = master_state.get_music_backfill_run(
            conn,
            "main",
            run_id=args.run_id,
        )
        if run_row is None:
            raise MusicBackfillError(f"unknown music backfill run: {args.run_id}")
        run = _mapping(run_row)
        _assert_master_binding(conn, run)
        observations = _validated_export_observations(
            run,
            _observation_rows(conn, args.run_id),
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            for observation in observations:
                packet = {
                    "schema_version": BACKFILL_EXPORT_SCHEMA,
                    "run_id": args.run_id,
                    "project": _text(run.get("project")),
                    "scope_mode": _text(run.get("scope_mode")),
                    "scope_value": _text(run.get("scope_value")),
                    "post_id": _text(observation.get("post_id")),
                    "base_snapshot_id": _text(observation.get("base_snapshot_id")),
                    "base_evidence_hash": _text(observation.get("base_evidence_hash")),
                    "base_observed_at": _text(observation.get("base_observed_at")),
                    "music_observed_at": _text(observation.get("music_observed_at")),
                    "status": _text(observation.get("status")),
                    "error": _text(observation.get("error")),
                    "music_evidence_hash": _text(
                        observation.get("music_evidence_hash")
                    ),
                    "observation_hash": _text(observation.get("observation_hash")),
                    "music_evidence": observation["music_evidence"],
                }
                handle.write(
                    json.dumps(
                        packet,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                )
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(destination)
        return {
            "run_id": args.run_id,
            "file": str(destination),
            "exported": len(observations),
            "schema_version": BACKFILL_EXPORT_SCHEMA,
            "collection_only": True,
        }
    finally:
        conn.close()
        if temporary.exists():
            temporary.unlink()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Append current TikTok music declarations and catalog support to "
            "eligible historical evidence without recollecting comments or transcripts."
        )
    )
    parser.add_argument(
        "--master-database",
        default=str(master_state.DEFAULT_MASTER_DATABASE),
        help="workspace TikTok master database",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="freeze and process an eligible scope")
    run.add_argument("--project", required=True)
    scope = run.add_mutually_exclusive_group(required=True)
    scope.add_argument("--creator")
    scope.add_argument("--topic")
    scope.add_argument("--url")
    scope.add_argument("--post-id", action="append", type=_post_id)
    amount = run.add_mutually_exclusive_group(required=True)
    amount.add_argument("--all-eligible", action="store_true")
    amount.add_argument("--limit", type=_positive_int)
    run.add_argument(
        "--target-schema",
        default=TARGET_MUSIC_SCHEMA,
        choices=[TARGET_MUSIC_SCHEMA],
    )
    run.add_argument(
        "--retry-status",
        action="append",
        type=_retry_status,
        help="include a retryable terminal provider result; repeat as needed",
    )
    run.add_argument("--force", action="store_true")
    run.add_argument("--expected-account", default="")
    run.add_argument("--max-pages", type=_positive_int)

    resume = subparsers.add_parser("resume", help="continue a frozen run")
    resume.add_argument("--run-id", required=True)
    resume.add_argument("--expected-account", default="")
    resume.add_argument("--max-pages", type=_positive_int)

    status = subparsers.add_parser("status", help="show stored progress")
    status.add_argument("--run-id", required=True)

    export = subparsers.add_parser("export", help="export stored observations")
    export.add_argument("--run-id", required=True)
    export.add_argument("--file", required=True)
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "run":
            result = _run_command(args)
        elif args.command == "resume":
            result = _resume_command(args)
        elif args.command == "status":
            result = _status_command(args)
        elif args.command == "export":
            result = _export_command(args)
        else:  # pragma: no cover - argparse makes this unreachable
            raise MusicBackfillError(f"unsupported command: {args.command}")
    except (MusicBackfillError, ValueError, FileNotFoundError, RuntimeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
