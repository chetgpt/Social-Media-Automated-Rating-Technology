"""Offline positive-pair corpus planning for SONIC AUDIT.

This module is deliberately transport-free.  It opens the workspace master
registry query-only, accepts only hash-verified completed v3 music-backfill
observations with exact Apple ``tt2dsp`` resolution, and returns a deterministic
hash-bound plan.  It never creates a SONIC run or acquires media/audio.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from sonic_audit.contracts import (
    bind_hash,
    canonical_sha256,
    normalize_creator,
    normalize_post_url,
    open_master_readonly,
    utc_now,
    verify_hash,
)
from tiktok_scraper.tt2dsp_resolution import (
    apple_song_ids,
    validate_tt2dsp_resolution_document,
)


CORPUS_PLAN_SCHEMA = "tiktok-sonic-audit-corpus-plan-v1"
MUSIC_SCHEMA = "tiktok-music-evidence-v3"
OBSERVATION_SCHEMA = "tiktok-music-backfill-observation-v1"
APPLE_PROVIDER = "apple_itunes_lookup"
SELECTION_ALGORITHM = "max-marginal-positive-pairs-v1"
MAX_BATCH_POSTS = 60
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_POST_ID_RE = re.compile(r"[1-9]\d*")


class CorpusPlanningError(RuntimeError):
    """Raised when an exact, verified corpus plan cannot be produced."""


def _json_object(value: Any, *, name: str) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or ""))
    except json.JSONDecodeError as exc:
        raise CorpusPlanningError(f"invalid {name} JSON") from exc
    if not isinstance(parsed, dict):
        raise CorpusPlanningError(f"{name} must be a JSON object")
    return parsed


def _json_list(value: Any, *, name: str) -> list[Any]:
    try:
        parsed = json.loads(str(value or ""))
    except json.JSONDecodeError as exc:
        raise CorpusPlanningError(f"invalid {name} JSON") from exc
    if not isinstance(parsed, list):
        raise CorpusPlanningError(f"{name} must be a JSON array")
    return parsed


def _sha256(value: Any, *, name: str) -> str:
    result = str(value or "").strip().casefold()
    if _SHA256_RE.fullmatch(result) is None:
        raise CorpusPlanningError(f"invalid {name}")
    return result


def _stable_id(*parts: Any, length: int = 40) -> str:
    payload = "\x1f".join(str(part or "") for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]


def _positive_int(value: Any, *, name: str, minimum: int = 1) -> int:
    if isinstance(value, bool):
        raise CorpusPlanningError(f"{name} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise CorpusPlanningError(f"{name} must be an integer") from exc
    if result < minimum:
        raise CorpusPlanningError(f"{name} must be at least {minimum}")
    return result


def _normalized_post_ids(values: Iterable[Any], *, name: str) -> list[str]:
    result: set[str] = set()
    for value in values:
        post_id = str(value or "").strip()
        if _POST_ID_RE.fullmatch(post_id) is None:
            raise CorpusPlanningError(f"invalid {name}: {post_id!r}")
        if post_id in result:
            raise CorpusPlanningError(f"duplicate {name}: {post_id}")
        result.add(post_id)
    return sorted(result, key=lambda item: (len(item), item))


def _verify_embedded_document(
    document: Mapping[str, Any],
    *,
    field: str,
    expected: Any,
    name: str,
    allow_missing_field: bool = False,
) -> str:
    digest = _sha256(expected, name=f"{name} hash")
    supplied = str(document.get(field) or "").strip().casefold()
    if supplied:
        if supplied != digest or not verify_hash(document, field):
            raise CorpusPlanningError(f"{name} hash mismatch")
    elif not allow_missing_field or canonical_sha256(document) != digest:
        raise CorpusPlanningError(f"{name} hash mismatch")
    return digest


def _observation_hash_document(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": OBSERVATION_SCHEMA,
        "run_id": str(row["run_id"]),
        "post_id": str(row["post_id"]),
        "base_snapshot_id": str(row["base_snapshot_id"]),
        "base_evidence_hash": str(row["base_evidence_hash"]),
        "base_observed_at": str(row["base_observed_at"]),
        "music_observed_at": str(row["music_observed_at"]),
        "music_evidence_hash": str(row["music_evidence_hash"]),
        "status": str(row["observation_status"]),
        "error": str(row["observation_error"] or ""),
    }


def _run_hash_document(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "run_id": str(row["run_id"]),
        "project": str(row["run_project"]),
        "master_database_path": str(row["run_master_database_path"] or ""),
        "scope_mode": str(row["run_scope_mode"]),
        "scope_value": str(row["run_scope_value"]),
        "target_schema_version": str(row["run_target_schema_version"]),
        "configured_catalogs": _json_list(
            row["run_configured_catalogs_json"], name="configured catalogs"
        ),
        "retryable_statuses": _json_list(
            row["run_retryable_statuses_json"], name="retryable statuses"
        ),
        "force": bool(row["run_force"]),
        "candidate_set_hash": str(row["run_candidate_set_hash"]),
        "selected_count": int(row["run_selected_count"]),
        "created_at": str(row["run_created_at"]),
    }


def _required_tables(connection: sqlite3.Connection) -> None:
    required = {
        "tiktok_master_posts",
        "tiktok_master_snapshots",
        "tiktok_master_music_backfill_runs",
        "tiktok_master_music_backfill_observations",
    }
    present = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?,?,?,?)",
            tuple(sorted(required)),
        )
    }
    missing = sorted(required - present)
    if missing:
        raise CorpusPlanningError(
            "master registry lacks required SONIC corpus tables: " + ", ".join(missing)
        )


def _scope_query(field: str, values: Sequence[str]) -> tuple[str, list[str]]:
    placeholders = ",".join("?" for _ in values)
    return f"p.{field} IN ({placeholders})", list(values)


def _query_rows(
    connection: sqlite3.Connection,
    *,
    field: str,
    values: Sequence[str],
) -> list[sqlite3.Row]:
    rows: list[sqlite3.Row] = []
    # Keep well below SQLite's variable limit while retaining a narrow indexed
    # predicate for every registry query.
    for offset in range(0, len(values), 400):
        chunk = values[offset : offset + 400]
        predicate, parameters = _scope_query(field, chunk)
        rows.extend(
            connection.execute(
                f"""
                WITH scoped_posts AS (
                  SELECT p.post_id, p.canonical_url, p.creator_handle,
                         p.creator_key, p.unavailable_at,
                         p.latest_snapshot_id, p.latest_evidence_hash
                  FROM tiktok_master_posts p
                  WHERE {predicate}
                ),
                ranked AS (
                  SELECT o.*,
                         ROW_NUMBER() OVER (
                           PARTITION BY o.post_id
                           ORDER BY julianday(o.music_observed_at) DESC,
                                    o.music_observed_at DESC,
                                    julianday(o.created_at) DESC,
                                    o.created_at DESC,
                                    o.observation_id DESC
                         ) AS rn
                  FROM tiktok_master_music_backfill_observations o
                  JOIN tiktok_master_music_backfill_runs r ON r.run_id=o.run_id
                  JOIN scoped_posts sp ON sp.post_id=o.post_id
                  WHERE o.status='completed'
                    AND r.status='backfill_complete'
                    AND lower(r.target_schema_version)=?
                )
                SELECT sp.post_id, sp.canonical_url, sp.creator_handle,
                       sp.creator_key, sp.unavailable_at,
                       sp.latest_snapshot_id, sp.latest_evidence_hash,
                       latest.post_id AS latest_snapshot_post_id,
                       latest.observed_at AS latest_snapshot_observed_at,
                       latest.evidence_hash AS latest_snapshot_evidence_hash,
                       latest.evidence_json AS latest_snapshot_evidence_json,
                       base.snapshot_id AS base_snapshot_row_id,
                       base.post_id AS base_snapshot_post_id,
                       base.observed_at AS base_snapshot_observed_at,
                       base.evidence_hash AS base_snapshot_evidence_hash,
                       base.evidence_json AS base_snapshot_evidence_json,
                       o.observation_id, o.run_id, o.base_snapshot_id,
                       o.base_evidence_hash, o.base_observed_at,
                       o.music_observed_at, o.music_evidence_json,
                       o.music_evidence_hash,
                       o.status AS observation_status,
                       o.error AS observation_error,
                       o.observation_hash, o.created_at AS observation_created_at,
                       r.project AS run_project,
                       r.master_database_path AS run_master_database_path,
                       r.scope_mode AS run_scope_mode,
                       r.scope_value AS run_scope_value,
                       r.target_schema_version AS run_target_schema_version,
                       r.configured_catalogs_json AS run_configured_catalogs_json,
                       r.retryable_statuses_json AS run_retryable_statuses_json,
                       r.force AS run_force,
                       r.candidate_set_json AS run_candidate_set_json,
                       r.candidate_set_hash AS run_candidate_set_hash,
                       r.run_hash AS run_hash,
                       r.selected_count AS run_selected_count,
                       r.completed_count AS run_completed_count,
                       r.unavailable_count AS run_unavailable_count,
                       r.failed_count AS run_failed_count,
                       r.created_at AS run_created_at,
                       r.status AS run_status
                FROM scoped_posts sp
                JOIN ranked o ON o.post_id=sp.post_id AND o.rn=1
                LEFT JOIN tiktok_master_snapshots latest
                  ON latest.snapshot_id=sp.latest_snapshot_id
                LEFT JOIN tiktok_master_snapshots base
                  ON base.snapshot_id=o.base_snapshot_id
                JOIN tiktok_master_music_backfill_runs r ON r.run_id=o.run_id
                WHERE COALESCE(sp.unavailable_at, '')=''
                  AND json_valid(o.music_evidence_json)
                  AND json_extract(
                        o.music_evidence_json,
                        '$.tt2dsp_resolution.status'
                      )='resolved'
                  AND json_extract(
                        o.music_evidence_json,
                        '$.tt2dsp_resolution.provider'
                      )=?
                ORDER BY sp.post_id
                """,
                (*parameters, MUSIC_SCHEMA, APPLE_PROVIDER),
            ).fetchall()
        )
    return rows


def _validate_run(
    connection: sqlite3.Connection,
    row: Mapping[str, Any],
    *,
    master_database: Path,
    cache: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    run_id = str(row["run_id"])
    cached = cache.get(run_id)
    if cached is not None:
        return cached
    if (
        str(row["run_status"]).casefold() != "backfill_complete"
        or str(row["run_target_schema_version"]).casefold() != MUSIC_SCHEMA
    ):
        raise CorpusPlanningError("music observation is not from a completed v3 run")
    frozen_path = str(row["run_master_database_path"] or "").strip()
    if frozen_path and Path(frozen_path).expanduser().resolve() != master_database:
        raise CorpusPlanningError("music backfill run master-database binding mismatch")

    candidates = _json_list(row["run_candidate_set_json"], name="candidate set")
    if not all(isinstance(item, Mapping) for item in candidates):
        raise CorpusPlanningError("candidate set contains a non-object")
    candidate_hash = _sha256(
        row["run_candidate_set_hash"], name="candidate-set hash"
    )
    if canonical_sha256(candidates) != candidate_hash:
        raise CorpusPlanningError("music backfill candidate-set hash mismatch")
    if int(row["run_selected_count"]) != len(candidates):
        raise CorpusPlanningError("music backfill selected-count mismatch")
    post_ids = [str(item.get("post_id") or "") for item in candidates]
    if not all(_POST_ID_RE.fullmatch(item) for item in post_ids) or len(
        set(post_ids)
    ) != len(post_ids):
        raise CorpusPlanningError("music backfill candidate identities are invalid")

    run_hash = _sha256(row["run_hash"], name="music backfill run hash")
    if canonical_sha256(_run_hash_document(row)) != run_hash:
        raise CorpusPlanningError("music backfill run hash mismatch")
    counts = {
        str(status): int(count)
        for status, count in connection.execute(
            """
            SELECT status, COUNT(*)
            FROM tiktok_master_music_backfill_observations
            WHERE run_id=? GROUP BY status
            """,
            (run_id,),
        ).fetchall()
    }
    expected_counts = {
        "completed": int(row["run_completed_count"]),
        "unavailable": int(row["run_unavailable_count"]),
        "failed": int(row["run_failed_count"]),
    }
    if any(counts.get(key, 0) != value for key, value in expected_counts.items()):
        raise CorpusPlanningError("music backfill run counters are inconsistent")
    if sum(expected_counts.values()) != len(candidates) or expected_counts["failed"]:
        raise CorpusPlanningError("music backfill run is not terminally complete")
    result = {
        "run_hash": run_hash,
        "candidate_set_hash": candidate_hash,
        "candidates": {str(item["post_id"]): dict(item) for item in candidates},
    }
    cache[run_id] = result
    return result


def _validated_candidate(
    connection: sqlite3.Connection,
    row: Mapping[str, Any],
    *,
    master_database: Path,
    run_cache: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    post_id = str(row["post_id"])
    identity = normalize_post_url(row["canonical_url"])
    creator = normalize_creator(row["creator_key"])
    if (
        identity["post_id"] != post_id
        or identity["creator"] != creator
        or identity["content_type"] != "video"
        or str(row["unavailable_at"] or "")
    ):
        raise CorpusPlanningError("candidate is not a known public video")

    latest_hash = _sha256(row["latest_evidence_hash"], name="latest evidence hash")
    latest_snapshot_hash = _sha256(
        row["latest_snapshot_evidence_hash"], name="latest snapshot evidence hash"
    )
    if (
        str(row["latest_snapshot_id"] or "") == ""
        or str(row["latest_snapshot_post_id"] or "") != post_id
        or latest_hash != latest_snapshot_hash
    ):
        raise CorpusPlanningError("current latest-snapshot binding mismatch")
    latest_evidence = _json_object(
        row["latest_snapshot_evidence_json"], name="latest evidence"
    )
    _verify_embedded_document(
        latest_evidence,
        field="evidence_hash",
        expected=latest_hash,
        name="latest evidence",
        allow_missing_field=True,
    )
    latest_evidence_post_id = str(latest_evidence.get("post_id") or "")
    if latest_evidence_post_id and latest_evidence_post_id != post_id:
        raise CorpusPlanningError("latest evidence post binding mismatch")

    snapshot_hash = _sha256(
        row["base_snapshot_evidence_hash"], name="base snapshot evidence hash"
    )
    base_hash = _sha256(row["base_evidence_hash"], name="base evidence hash")
    if (
        str(row["base_snapshot_row_id"] or "") != str(row["base_snapshot_id"])
        or str(row["base_snapshot_post_id"] or "") != post_id
        or snapshot_hash != base_hash
        or str(row["base_snapshot_observed_at"] or "")
        != str(row["base_observed_at"])
    ):
        raise CorpusPlanningError("music observation base-snapshot binding mismatch")
    evidence = _json_object(row["base_snapshot_evidence_json"], name="base evidence")
    _verify_embedded_document(
        evidence,
        field="evidence_hash",
        expected=base_hash,
        name="base evidence",
        allow_missing_field=True,
    )
    evidence_post_id = str(evidence.get("post_id") or "")
    if evidence_post_id and evidence_post_id != post_id:
        raise CorpusPlanningError("base evidence post binding mismatch")

    run = _validate_run(
        connection,
        row,
        master_database=master_database,
        cache=run_cache,
    )
    frozen = run["candidates"].get(post_id)
    if frozen is None:
        raise CorpusPlanningError("music observation is outside its frozen run scope")
    expected_frozen = {
        "canonical_url": identity["canonical_url"],
        "creator_key": creator,
        "base_snapshot_id": str(row["base_snapshot_id"]),
        "base_evidence_hash": base_hash,
        "base_observed_at": str(row["base_observed_at"]),
    }
    if any(str(frozen.get(key) or "") != value for key, value in expected_frozen.items()):
        raise CorpusPlanningError("music backfill candidate binding mismatch")

    music = _json_object(row["music_evidence_json"], name="music evidence")
    music_hash = _verify_embedded_document(
        music,
        field="music_evidence_hash",
        expected=row["music_evidence_hash"],
        name="music evidence",
    )
    if str(music.get("schema_version") or "").casefold() != MUSIC_SCHEMA:
        raise CorpusPlanningError("music evidence is not schema v3")

    observation_hash = _sha256(
        row["observation_hash"], name="music observation hash"
    )
    if canonical_sha256(_observation_hash_document(row)) != observation_hash:
        raise CorpusPlanningError("music observation hash mismatch")
    if str(row["observation_id"]) != _stable_id(
        "tiktok-music-backfill-observation", row["run_id"], post_id
    ):
        raise CorpusPlanningError("music observation identity binding mismatch")

    resolution = music.get("tt2dsp_resolution")
    if not isinstance(resolution, Mapping):
        return {}
    resolution_valid = validate_tt2dsp_resolution_document(resolution)
    if not resolution_valid:
        if str(resolution.get("status") or "").casefold() == "resolved":
            raise CorpusPlanningError("invalid resolved tt2dsp resolution hash or contract")
        return {}
    selected = resolution.get("selected")
    if (
        resolution.get("provider") != APPLE_PROVIDER
        or resolution.get("status") != "resolved"
        or not isinstance(selected, Mapping)
    ):
        return {}
    track_id = str(selected.get("provider_track_id") or "")
    storefront = str(resolution.get("storefront") or "").strip().upper()
    if apple_song_ids(resolution.get("input_links")) != [track_id]:
        raise CorpusPlanningError("Apple resolution is not bound to its input track ID")
    reference_label = f"{APPLE_PROVIDER}:{storefront}:{track_id}"
    return {
        "post_id": post_id,
        "canonical_url": identity["canonical_url"],
        "creator_handle": creator,
        "content_type": "video",
        "latest_snapshot_id": str(row["latest_snapshot_id"]),
        "latest_evidence_hash": latest_hash,
        "latest_observed_at": str(row["latest_snapshot_observed_at"]),
        "base_snapshot_id": str(row["base_snapshot_id"]),
        "base_evidence_hash": base_hash,
        "base_observed_at": str(row["base_observed_at"]),
        "backfill_run_id": str(row["run_id"]),
        "backfill_run_hash": run["run_hash"],
        "backfill_candidate_set_hash": run["candidate_set_hash"],
        "music_observation_id": str(row["observation_id"]),
        "music_observation_hash": observation_hash,
        "music_observed_at": str(row["music_observed_at"]),
        "music_evidence_schema": MUSIC_SCHEMA,
        "music_evidence_hash": music_hash,
        "tt2dsp_resolution_hash": str(resolution["resolution_hash"]),
        "reference_provider": APPLE_PROVIDER,
        "reference_storefront": storefront,
        "reference_track_id": track_id,
        "reference_label": reference_label,
        "reference_title": str(selected.get("title") or "")[:500],
        "reference_artist": str(selected.get("artist") or "")[:500],
    }


def _load_candidates(
    master_database: str | Path,
    *,
    creators: Sequence[str],
    post_ids: Sequence[str],
) -> list[dict[str, Any]]:
    database = Path(master_database).expanduser().resolve()
    connection = open_master_readonly(database)
    try:
        _required_tables(connection)
        rows = _query_rows(
            connection,
            field="creator_key" if creators else "post_id",
            values=creators or post_ids,
        )
        cache: dict[str, dict[str, Any]] = {}
        validated = [
            _validated_candidate(
                connection,
                row,
                master_database=database,
                run_cache=cache,
            )
            for row in rows
        ]
        candidates = [row for row in validated if row]
    except (sqlite3.Error, ValueError, KeyError) as exc:
        if isinstance(exc, CorpusPlanningError):
            raise
        raise CorpusPlanningError(f"invalid SONIC corpus registry: {exc}") from exc
    finally:
        connection.close()
    if len({row["post_id"] for row in candidates}) != len(candidates):
        raise CorpusPlanningError("registry returned duplicate corpus candidates")
    return candidates


def _candidate_order(row: Mapping[str, Any]) -> tuple[str, str]:
    digest = canonical_sha256(
        [row.get("creator_handle"), row.get("reference_label"), row.get("post_id")]
    )
    return digest, str(row.get("post_id") or "")


def _group_candidates(
    candidates: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for candidate in candidates:
        row = dict(candidate)
        grouped[row["reference_label"]].append(row)
    result: list[dict[str, Any]] = []
    for label, rows in grouped.items():
        if len(rows) < 2:
            continue
        by_creator: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            by_creator[row["creator_handle"]].append(row)
        for creator_rows in by_creator.values():
            creator_rows.sort(key=_candidate_order)
        # A creator round-robin avoids throwing away cross-creator positives
        # when a single account has many uses of the same exact recording.
        ordered_rows: list[dict[str, Any]] = []
        depth = 0
        creators = sorted(by_creator)
        while len(ordered_rows) < len(rows):
            for creator in creators:
                creator_rows = by_creator[creator]
                if depth < len(creator_rows):
                    ordered_rows.append(creator_rows[depth])
            depth += 1
        result.append(
            {
                "group_id": "sonic_group_"
                + canonical_sha256(label)[:16],
                "creator_handles": creators,
                "reference_label": label,
                "available_candidates": ordered_rows,
            }
        )
    return result


def _positive_pairs(count: int) -> int:
    return math.comb(count, 2) if count >= 2 else 0


def _select_creator_scope(
    groups: Sequence[Mapping[str, Any]],
    *,
    min_repeated_groups: int,
    min_positive_pairs: int,
    max_posts: int,
    max_posts_per_reference: int,
) -> list[dict[str, Any]]:
    ordered = sorted(
        (dict(group) for group in groups),
        key=lambda group: (
            -min(len(group["available_candidates"]), max_posts_per_reference),
            group["reference_label"],
        ),
    )
    if len(ordered) < min_repeated_groups:
        raise CorpusPlanningError(
            f"only {len(ordered)} repeated Apple groups exist; "
            f"{min_repeated_groups} required"
        )
    selected: list[dict[str, Any]] = []
    total_posts = 0

    def seed(group: Mapping[str, Any]) -> None:
        nonlocal total_posts
        if total_posts + 2 > max_posts:
            raise CorpusPlanningError("max_posts cannot seed the required repeated groups")
        item = dict(group)
        item["selected_candidates"] = [
            dict(row) for row in item["available_candidates"][:2]
        ]
        selected.append(item)
        total_posts += 2

    for group in ordered[:min_repeated_groups]:
        seed(group)
    next_group = min_repeated_groups

    def pair_count() -> int:
        return sum(_positive_pairs(len(group["selected_candidates"])) for group in selected)

    while pair_count() < min_positive_pairs:
        expandable = [
            group
            for group in selected
            if len(group["selected_candidates"])
            < min(len(group["available_candidates"]), max_posts_per_reference)
        ]
        if expandable:
            # Adding one member to a group of size n contributes n new
            # positive pairs.  Choose the greatest marginal gain so a feasible
            # target cannot be rejected merely because a balanced allocation
            # used the post budget inefficiently.
            group = min(
                expandable,
                key=lambda item: (
                    -len(item["selected_candidates"]),
                    -min(
                        len(item["available_candidates"]),
                        max_posts_per_reference,
                    ),
                    item["reference_label"],
                ),
            )
            if total_posts >= max_posts:
                raise CorpusPlanningError(
                    "positive-pair target is infeasible within max_posts"
                )
            depth = len(group["selected_candidates"])
            group["selected_candidates"].append(
                dict(group["available_candidates"][depth])
            )
            total_posts += 1
        else:
            if next_group >= len(ordered):
                raise CorpusPlanningError(
                    "positive-pair target exceeds the eligible repeated-group capacity"
                )
            seed(ordered[next_group])
            next_group += 1
    return selected


def _select_exact_scope(
    groups: Sequence[Mapping[str, Any]],
    *,
    expected_candidate_count: int,
    min_repeated_groups: int,
    min_positive_pairs: int,
    max_posts: int,
    max_posts_per_reference: int,
) -> list[dict[str, Any]]:
    repeated_count = sum(len(group["available_candidates"]) for group in groups)
    if repeated_count != expected_candidate_count:
        raise CorpusPlanningError(
            "explicit post-ID scope contains a singleton Apple reference"
        )
    if expected_candidate_count > max_posts:
        raise CorpusPlanningError("explicit post-ID scope exceeds max_posts")
    ordered = sorted(
        (dict(group) for group in groups),
        key=lambda group: group["reference_label"],
    )
    if len(ordered) < min_repeated_groups:
        raise CorpusPlanningError("explicit post-ID scope misses the repeated-group target")
    for group in ordered:
        if len(group["available_candidates"]) > max_posts_per_reference:
            raise CorpusPlanningError(
                "explicit post-ID scope exceeds max_posts_per_reference"
            )
        group["selected_candidates"] = [
            dict(row) for row in group["available_candidates"]
        ]
    pairs = sum(_positive_pairs(len(group["selected_candidates"])) for group in ordered)
    if pairs < min_positive_pairs:
        raise CorpusPlanningError("explicit post-ID scope misses the positive-pair target")
    return ordered


def _freeze_plan(
    *,
    master_database: Path,
    scope: Mapping[str, Any],
    targets: Mapping[str, int],
    pool: Sequence[Mapping[str, Any]],
    selected_groups: Sequence[Mapping[str, Any]],
    excluded_post_ids: Sequence[str],
) -> dict[str, Any]:
    group_rows = [dict(group) for group in selected_groups]
    batches: list[dict[str, Any]] = []
    post_batch: dict[str, str] = {}
    group_batch_ids: dict[str, list[str]] = defaultdict(list)
    # A reference label may span creators.  Build creator-local fragments so
    # every executable batch remains one creator while the corpus group keeps
    # the cross-creator positive identity.
    by_creator: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for group in group_rows:
        fragment_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for candidate in group["selected_candidates"]:
            fragment_rows[candidate["creator_handle"]].append(candidate)
        for creator, rows in fragment_rows.items():
            by_creator[creator].append(
                {
                    "group_id": group["group_id"],
                    "selected_candidates": rows,
                }
            )
    for creator in sorted(by_creator):
        current: list[dict[str, Any]] = []
        current_count = 0
        creator_batches: list[list[dict[str, Any]]] = []
        for fragment in by_creator[creator]:
            count = len(fragment["selected_candidates"])
            if count > MAX_BATCH_POSTS:
                raise CorpusPlanningError("one reference group exceeds the 60-post batch cap")
            if current and current_count + count > MAX_BATCH_POSTS:
                creator_batches.append(current)
                current, current_count = [], 0
            current.append(fragment)
            current_count += count
        if current:
            creator_batches.append(current)
        for creator_position, batch_groups in enumerate(creator_batches, start=1):
            group_ids = [fragment["group_id"] for fragment in batch_groups]
            batch_id = "sonic_batch_" + canonical_sha256(
                [creator, creator_position, group_ids]
            )[:16]
            post_ids = [
                row["post_id"]
                for fragment in batch_groups
                for row in fragment["selected_candidates"]
            ]
            for post_id in post_ids:
                post_batch[post_id] = batch_id
            for group_id in group_ids:
                if batch_id not in group_batch_ids[group_id]:
                    group_batch_ids[group_id].append(batch_id)
            batches.append(
                {
                    "batch_id": batch_id,
                    "creator_handle": creator,
                    "creator_batch_position": creator_position,
                    "group_ids": list(dict.fromkeys(group_ids)),
                }
            )

    candidates: list[dict[str, Any]] = []
    batch_positions: dict[str, int] = defaultdict(int)
    for group_position, group in enumerate(group_rows, start=1):
        for row in group["selected_candidates"]:
            batch_id = post_batch[row["post_id"]]
            batch_positions[batch_id] += 1
            candidates.append(
                bind_hash(
                    {
                        **dict(row),
                        "selection_position": len(candidates) + 1,
                        "group_id": group["group_id"],
                        "group_position": group_position,
                        "batch_id": batch_id,
                        "batch_position": batch_positions[batch_id],
                    },
                    "candidate_hash",
                )
            )
    candidates_by_group: dict[str, list[dict[str, Any]]] = defaultdict(list)
    candidates_by_batch: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for candidate in candidates:
        candidates_by_group[candidate["group_id"]].append(candidate)
        candidates_by_batch[candidate["batch_id"]].append(candidate)

    groups: list[dict[str, Any]] = []
    for group in group_rows:
        selected = candidates_by_group[group["group_id"]]
        groups.append(
            bind_hash(
                {
                    "group_id": group["group_id"],
                    "creator_handles": sorted(
                        {row["creator_handle"] for row in selected}
                    ),
                    "reference_label": group["reference_label"],
                    "available_count": len(group["available_candidates"]),
                    "selected_count": len(selected),
                    "positive_pair_count": _positive_pairs(len(selected)),
                    "batch_ids": group_batch_ids[group["group_id"]],
                    "post_ids": [row["post_id"] for row in selected],
                    "candidate_set_hash": canonical_sha256(selected),
                },
                "group_hash",
            )
        )
    group_map = {group["group_id"]: group for group in groups}
    frozen_batches: list[dict[str, Any]] = []
    for batch in batches:
        selected = candidates_by_batch[batch["batch_id"]]
        frozen_batches.append(
            bind_hash(
                {
                    **batch,
                    "selected_count": len(selected),
                    "post_ids": [row["post_id"] for row in selected],
                    "candidate_set_hash": canonical_sha256(selected),
                    "group_hashes": [
                        group_map[group_id]["group_hash"]
                        for group_id in batch["group_ids"]
                    ],
                },
                "batch_hash",
            )
        )

    positive_pairs = sum(group["positive_pair_count"] for group in groups)
    source_scope = bind_hash(
        {
            **dict(scope),
            "excluded_post_ids": list(excluded_post_ids),
            "excluded_post_id_count": len(excluded_post_ids),
            "excluded_post_id_set_hash": canonical_sha256(list(excluded_post_ids)),
        },
        "scope_hash",
    )
    body = {
        "schema_version": CORPUS_PLAN_SCHEMA,
        "workflow": "sonic_audit_corpus_planning",
        "planned_at": utc_now(),
        "master_database": str(master_database),
        "source_scope": source_scope,
        "selection_algorithm": SELECTION_ALGORITHM,
        "targets": dict(targets),
        "eligibility_contract": {
            "content_type": "public_video",
            "backfill_run_status": "backfill_complete",
            "music_observation_status": "completed",
            "music_schema_version": MUSIC_SCHEMA,
            "reference_provider": APPLE_PROVIDER,
            "reference_status": "resolved",
            "reference_label_format": "apple_itunes_lookup:<STOREFRONT>:<TRACK_ID>",
        },
        "eligible_candidate_count": len(pool),
        "eligible_candidate_pool_hash": canonical_sha256(list(pool)),
        "selected_candidate_count": len(candidates),
        "selected_repeated_group_count": len(groups),
        "selected_positive_pair_count": positive_pairs,
        "candidate_set_hash": canonical_sha256(candidates),
        "group_set_hash": canonical_sha256(groups),
        "batch_set_hash": canonical_sha256(frozen_batches),
        "candidates": candidates,
        "groups": groups,
        "batches": frozen_batches,
        "boundaries": {
            "registry_access": "query_only",
            "browser_access_performed": False,
            "catalog_request_performed": False,
            "media_access_performed": False,
            "audio_acquisition_performed": False,
            "ai_analysis_performed": False,
            "sonic_run_created": False,
            "master_registry_mutated": False,
            "publication_eligible": False,
        },
    }
    return bind_hash(body, "plan_hash")


def plan_corpus(
    master_database: str | Path,
    creators: Sequence[str] = (),
    post_ids: Sequence[str] = (),
    min_repeated_groups: int = 1,
    min_positive_pairs: int = 1,
    max_posts: int = MAX_BATCH_POSTS,
    max_posts_per_reference: int = 10,
    excluded_post_ids: Sequence[str] = (),
) -> dict[str, Any]:
    """Return a verified deterministic positive-pair corpus plan.

    Exactly one source scope is accepted.  Creator scope selects a bounded
    feasible subset.  Explicit post-ID scope is exact: every supplied ID must
    be eligible and selected, so it only tests feasibility and never replaces
    or silently drops a requested post.
    """

    normalized_creators = sorted({normalize_creator(value) for value in creators})
    normalized_post_ids = _normalized_post_ids(post_ids, name="post ID")
    excluded = _normalized_post_ids(excluded_post_ids, name="excluded post ID")
    if bool(normalized_creators) == bool(normalized_post_ids):
        raise CorpusPlanningError("provide exactly one of creators or post_ids")
    if normalized_post_ids and set(normalized_post_ids) & set(excluded):
        raise CorpusPlanningError("explicit post-ID scope intersects exclusions")

    min_groups = _positive_int(
        min_repeated_groups, name="min_repeated_groups"
    )
    min_pairs = _positive_int(min_positive_pairs, name="min_positive_pairs")
    posts_cap = _positive_int(max_posts, name="max_posts", minimum=2)
    reference_cap = _positive_int(
        max_posts_per_reference, name="max_posts_per_reference", minimum=2
    )
    if reference_cap > MAX_BATCH_POSTS:
        raise CorpusPlanningError(
            f"max_posts_per_reference cannot exceed {MAX_BATCH_POSTS}"
        )
    if min_groups * 2 > posts_cap:
        raise CorpusPlanningError("max_posts cannot seed min_repeated_groups")

    database = Path(master_database).expanduser().resolve()
    pool = _load_candidates(
        database,
        creators=normalized_creators,
        post_ids=normalized_post_ids,
    )
    pool_by_id = {row["post_id"]: row for row in pool}
    if normalized_post_ids:
        missing = [post_id for post_id in normalized_post_ids if post_id not in pool_by_id]
        if missing:
            raise CorpusPlanningError(
                "explicit post IDs are missing or ineligible: " + ", ".join(missing)
            )
    excluded_set = set(excluded)
    filtered_pool = [row for row in pool if row["post_id"] not in excluded_set]
    groups = _group_candidates(filtered_pool)
    if normalized_post_ids:
        selected_groups = _select_exact_scope(
            groups,
            expected_candidate_count=len(normalized_post_ids),
            min_repeated_groups=min_groups,
            min_positive_pairs=min_pairs,
            max_posts=posts_cap,
            max_posts_per_reference=reference_cap,
        )
        scope = {"mode": "post_ids", "post_ids": normalized_post_ids}
    else:
        selected_groups = _select_creator_scope(
            groups,
            min_repeated_groups=min_groups,
            min_positive_pairs=min_pairs,
            max_posts=posts_cap,
            max_posts_per_reference=reference_cap,
        )
        scope = {"mode": "creators", "creators": normalized_creators}

    return _freeze_plan(
        master_database=database,
        scope=scope,
        targets={
            "min_repeated_groups": min_groups,
            "min_positive_pairs": min_pairs,
            "max_posts": posts_cap,
            "max_posts_per_reference": reference_cap,
            "max_posts_per_batch": MAX_BATCH_POSTS,
        },
        pool=filtered_pool,
        selected_groups=selected_groups,
        excluded_post_ids=excluded,
    )


def validate_corpus_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Validate every closed hash and relationship in a v1 corpus plan."""

    if (
        plan.get("schema_version") != CORPUS_PLAN_SCHEMA
        or plan.get("workflow") != "sonic_audit_corpus_planning"
        or not verify_hash(plan, "plan_hash")
    ):
        raise CorpusPlanningError("invalid corpus plan hash or schema")
    if plan.get("selection_algorithm") != SELECTION_ALGORITHM:
        raise CorpusPlanningError("unsupported corpus plan selection algorithm")
    master_database = str(plan.get("master_database") or "").strip()
    if not master_database:
        raise CorpusPlanningError("corpus plan has no master-database binding")

    source_scope = plan.get("source_scope")
    if not isinstance(source_scope, Mapping) or not verify_hash(
        source_scope, "scope_hash"
    ):
        raise CorpusPlanningError("invalid corpus plan source-scope hash")
    targets = plan.get("targets")
    if not isinstance(targets, Mapping) or int(
        targets.get("max_posts_per_batch") or 0
    ) != MAX_BATCH_POSTS:
        raise CorpusPlanningError("invalid corpus plan batch contract")
    eligibility = plan.get("eligibility_contract")
    if not isinstance(eligibility, Mapping) or any(
        eligibility.get(field) != expected
        for field, expected in {
            "content_type": "public_video",
            "backfill_run_status": "backfill_complete",
            "music_observation_status": "completed",
            "music_schema_version": MUSIC_SCHEMA,
            "reference_provider": APPLE_PROVIDER,
            "reference_status": "resolved",
            "reference_label_format": (
                "apple_itunes_lookup:<STOREFRONT>:<TRACK_ID>"
            ),
        }.items()
    ):
        raise CorpusPlanningError("invalid corpus plan eligibility contract")
    boundaries = plan.get("boundaries")
    if not isinstance(boundaries, Mapping) or any(
        boundaries.get(field) is not False
        for field in (
            "browser_access_performed",
            "media_access_performed",
            "audio_acquisition_performed",
            "sonic_run_created",
            "master_registry_mutated",
        )
    ):
        raise CorpusPlanningError("corpus plan cannot carry execution authority")

    candidates = plan.get("candidates")
    groups = plan.get("groups")
    batches = plan.get("batches")
    if not isinstance(candidates, list) or not isinstance(groups, list) or not isinstance(
        batches, list
    ):
        raise CorpusPlanningError("corpus plan collections are invalid")
    if int(plan.get("selected_candidate_count") or -1) != len(candidates):
        raise CorpusPlanningError("corpus plan candidate count mismatch")
    if int(plan.get("selected_repeated_group_count") or -1) != len(groups):
        raise CorpusPlanningError("corpus plan group count mismatch")
    if canonical_sha256(candidates) != str(plan.get("candidate_set_hash") or ""):
        raise CorpusPlanningError("corpus plan candidate-set hash mismatch")
    if canonical_sha256(groups) != str(plan.get("group_set_hash") or ""):
        raise CorpusPlanningError("corpus plan group-set hash mismatch")
    if canonical_sha256(batches) != str(plan.get("batch_set_hash") or ""):
        raise CorpusPlanningError("corpus plan batch-set hash mismatch")

    candidate_ids: set[str] = set()
    candidate_rows: list[dict[str, Any]] = []
    for position, value in enumerate(candidates, start=1):
        if not isinstance(value, Mapping) or not verify_hash(value, "candidate_hash"):
            raise CorpusPlanningError("invalid corpus candidate hash")
        row = dict(value)
        post_id = str(row.get("post_id") or "")
        if (
            _POST_ID_RE.fullmatch(post_id) is None
            or post_id in candidate_ids
            or int(row.get("selection_position") or 0) != position
            or str(row.get("content_type") or "").casefold() != "video"
        ):
            raise CorpusPlanningError("invalid corpus candidate identity")
        identity = normalize_post_url(row.get("canonical_url"))
        provider = str(row.get("reference_provider") or "")
        storefront = str(row.get("reference_storefront") or "")
        track_id = str(row.get("reference_track_id") or "")
        expected_label = f"{provider}:{storefront}:{track_id}"
        if (
            identity["post_id"] != post_id
            or identity["creator"] != normalize_creator(row.get("creator_handle"))
            or identity["content_type"] != "video"
            or provider != APPLE_PROVIDER
            or re.fullmatch(r"[A-Z]{2}", storefront) is None
            or re.fullmatch(r"[1-9]\d{1,24}", track_id) is None
            or str(row.get("reference_label") or "") != expected_label
        ):
            raise CorpusPlanningError("invalid corpus candidate binding")
        candidate_ids.add(post_id)
        candidate_rows.append(row)

    groups_by_id: dict[str, dict[str, Any]] = {}
    positive_pairs = 0
    for value in groups:
        if not isinstance(value, Mapping) or not verify_hash(value, "group_hash"):
            raise CorpusPlanningError("invalid corpus group hash")
        group = dict(value)
        group_id = str(group.get("group_id") or "")
        if not group_id or group_id in groups_by_id:
            raise CorpusPlanningError("duplicate corpus group identity")
        selected = [
            row for row in candidate_rows if str(row.get("group_id") or "") == group_id
        ]
        post_ids = [row["post_id"] for row in selected]
        if (
            int(group.get("selected_count") or 0) != len(selected)
            or list(group.get("post_ids") or []) != post_ids
            or canonical_sha256(selected)
            != str(group.get("candidate_set_hash") or "")
            or len({str(row.get("reference_label") or "") for row in selected}) != 1
            or (selected and selected[0].get("reference_label"))
            != group.get("reference_label")
        ):
            raise CorpusPlanningError("corpus group/candidate binding mismatch")
        expected_pairs = _positive_pairs(len(selected))
        if int(group.get("positive_pair_count") or -1) != expected_pairs:
            raise CorpusPlanningError("corpus group positive-pair mismatch")
        positive_pairs += expected_pairs
        groups_by_id[group_id] = group
    if int(plan.get("selected_positive_pair_count") or -1) != positive_pairs:
        raise CorpusPlanningError("corpus plan positive-pair count mismatch")

    batches_by_id: dict[str, dict[str, Any]] = {}
    seen_batch_posts: set[str] = set()
    for value in batches:
        if not isinstance(value, Mapping) or not verify_hash(value, "batch_hash"):
            raise CorpusPlanningError("invalid corpus batch hash")
        batch = dict(value)
        batch_id = str(batch.get("batch_id") or "")
        if not batch_id or batch_id in batches_by_id:
            raise CorpusPlanningError("duplicate corpus batch identity")
        selected = sorted(
            (
                row
                for row in candidate_rows
                if str(row.get("batch_id") or "") == batch_id
            ),
            key=lambda row: int(row.get("batch_position") or 0),
        )
        post_ids = [row["post_id"] for row in selected]
        creator = normalize_creator(batch.get("creator_handle"))
        if (
            not 1 <= len(selected) <= MAX_BATCH_POSTS
            or int(batch.get("selected_count") or 0) != len(selected)
            or list(batch.get("post_ids") or []) != post_ids
            or len(post_ids) != len(set(post_ids))
            or seen_batch_posts.intersection(post_ids)
            or canonical_sha256(selected)
            != str(batch.get("candidate_set_hash") or "")
            or any(normalize_creator(row.get("creator_handle")) != creator for row in selected)
            or [int(row.get("batch_position") or 0) for row in selected]
            != list(range(1, len(selected) + 1))
        ):
            raise CorpusPlanningError("corpus batch/candidate binding mismatch")
        group_ids = list(batch.get("group_ids") or [])
        if (
            not group_ids
            or any(group_id not in groups_by_id for group_id in group_ids)
            or list(batch.get("group_hashes") or [])
            != [groups_by_id[group_id]["group_hash"] for group_id in group_ids]
            or any(str(row.get("group_id") or "") not in group_ids for row in selected)
        ):
            raise CorpusPlanningError("corpus batch/group binding mismatch")
        seen_batch_posts.update(post_ids)
        batches_by_id[batch_id] = batch
    if seen_batch_posts != candidate_ids:
        raise CorpusPlanningError("corpus batches do not partition the candidate set")

    return {
        "plan": dict(plan),
        "master_database": str(Path(master_database).expanduser().resolve()),
        "candidates": candidate_rows,
        "groups_by_id": groups_by_id,
        "batches_by_id": batches_by_id,
    }


def _latest_music_observation_ids(
    master_database: Path,
    post_ids: Sequence[str],
) -> dict[str, str]:
    connection = open_master_readonly(master_database)
    try:
        result: dict[str, str] = {}
        for offset in range(0, len(post_ids), 400):
            chunk = list(post_ids[offset : offset + 400])
            placeholders = ",".join("?" for _ in chunk)
            rows = connection.execute(
                f"""
                SELECT o.post_id, o.observation_id
                FROM tiktok_master_music_backfill_observations o
                WHERE o.post_id IN ({placeholders})
                  AND o.observation_id=(
                    SELECT o2.observation_id
                    FROM tiktok_master_music_backfill_observations o2
                    WHERE o2.post_id=o.post_id
                    ORDER BY julianday(o2.music_observed_at) DESC,
                             o2.music_observed_at DESC,
                             julianday(o2.created_at) DESC,
                             o2.created_at DESC,
                             o2.observation_id DESC
                    LIMIT 1
                  )
                """,
                tuple(chunk),
            ).fetchall()
            result.update({str(row[0]): str(row[1]) for row in rows})
        return result
    finally:
        connection.close()


def validate_corpus_plan_batch(
    plan: Mapping[str, Any],
    *,
    batch_id: str,
    master_database: str | Path | None = None,
) -> dict[str, Any]:
    """Revalidate one exact frozen plan batch against current registry state."""

    validated = validate_corpus_plan(plan)
    batch = validated["batches_by_id"].get(str(batch_id or "").strip())
    if batch is None:
        raise CorpusPlanningError("unknown corpus plan batch")
    frozen_master = Path(validated["master_database"]).resolve()
    supplied_master = (
        frozen_master
        if master_database is None
        else Path(master_database).expanduser().resolve()
    )
    if supplied_master != frozen_master:
        raise CorpusPlanningError("corpus plan master-database binding mismatch")

    frozen = sorted(
        (
            row
            for row in validated["candidates"]
            if row.get("batch_id") == batch["batch_id"]
        ),
        key=lambda row: int(row["batch_position"]),
    )
    post_ids = [row["post_id"] for row in frozen]
    current = _load_candidates(
        frozen_master,
        creators=(),
        post_ids=post_ids,
    )
    current_by_id = {row["post_id"]: row for row in current}
    latest_observations = _latest_music_observation_ids(frozen_master, post_ids)
    binding_fields = (
        "post_id",
        "canonical_url",
        "creator_handle",
        "content_type",
        "latest_snapshot_id",
        "latest_evidence_hash",
        "latest_observed_at",
        "base_snapshot_id",
        "base_evidence_hash",
        "base_observed_at",
        "backfill_run_id",
        "backfill_run_hash",
        "backfill_candidate_set_hash",
        "music_observation_id",
        "music_observation_hash",
        "music_observed_at",
        "music_evidence_schema",
        "music_evidence_hash",
        "tt2dsp_resolution_hash",
        "reference_provider",
        "reference_storefront",
        "reference_track_id",
        "reference_label",
        "reference_title",
        "reference_artist",
    )
    for candidate in frozen:
        post_id = candidate["post_id"]
        refreshed = current_by_id.get(post_id)
        if refreshed is None:
            raise CorpusPlanningError(
                f"corpus plan candidate is no longer eligible: {post_id}"
            )
        if any(
            str(candidate.get(field) or "") != str(refreshed.get(field) or "")
            for field in binding_fields
        ):
            raise CorpusPlanningError(
                f"corpus plan candidate binding changed: {post_id}"
            )
        if latest_observations.get(post_id) != candidate.get("music_observation_id"):
            raise CorpusPlanningError(
                f"corpus plan music observation is no longer latest: {post_id}"
            )
    if len(current_by_id) != len(frozen):
        raise CorpusPlanningError("corpus plan current candidate set mismatch")
    return {
        "plan_hash": str(plan["plan_hash"]),
        "master_database": str(frozen_master),
        "batch": dict(batch),
        "candidates": [dict(row) for row in frozen],
        "groups_by_id": dict(validated["groups_by_id"]),
    }


__all__ = [
    "APPLE_PROVIDER",
    "CORPUS_PLAN_SCHEMA",
    "CorpusPlanningError",
    "MAX_BATCH_POSTS",
    "MUSIC_SCHEMA",
    "SELECTION_ALGORITHM",
    "plan_corpus",
    "validate_corpus_plan",
    "validate_corpus_plan_batch",
]
