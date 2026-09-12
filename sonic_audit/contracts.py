"""Pure contracts and deterministic selection for the standalone SONIC AUDIT.

SONIC AUDIT deliberately reads the TikTok master registry without migrating or
mutating it.  Raw media is outside this module; only immutable registry
bindings, derived features, and evaluation labels are represented here.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlsplit


RUN_SCHEMA = "tiktok-sonic-audit-run-v1"
STATE_SCHEMA = "tiktok-sonic-audit-state-v1"
FEATURE_RECORD_SCHEMA = "tiktok-sonic-audit-feature-record-v1"
REPORT_SCHEMA = "tiktok-sonic-audit-report-v1"
DEFAULT_OUTPUT_ROOT = Path("comments_data") / "sonic_audit_runs"
MAX_PILOT_POSTS = 60
MIRELO_SYMBOLIC_CONFIG_SCHEMA = "tiktok-sonic-mirelo-config-v1"


class SonicAuditContractError(RuntimeError):
    """Raised when a SONIC AUDIT artifact or registry binding is invalid."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def bind_hash(value: Mapping[str, Any], field: str) -> dict[str, Any]:
    result = dict(value)
    result.pop(field, None)
    result[field] = canonical_sha256(result)
    return result


def verify_hash(value: Mapping[str, Any], field: str) -> bool:
    supplied = str(value.get(field) or "")
    if re.fullmatch(r"[0-9a-f]{64}", supplied) is None:
        return False
    body = dict(value)
    body.pop(field, None)
    return canonical_sha256(body) == supplied


def normalize_creator(value: Any) -> str:
    raw = str(value or "").strip()
    if raw.startswith("http://") or raw.startswith("https://"):
        parsed = urlsplit(raw)
        if (parsed.hostname or "").rstrip(".").casefold() not in {
            "tiktok.com",
            "www.tiktok.com",
            "m.tiktok.com",
        }:
            raise SonicAuditContractError("creator URL must use tiktok.com")
        match = re.fullmatch(r"/@([^/]+)/?", parsed.path)
        if match is None:
            raise SonicAuditContractError("creator URL must be an exact profile URL")
        raw = match.group(1)
    creator = raw.lstrip("@").strip().casefold()
    if not creator or re.fullmatch(r"[a-z0-9._]+", creator) is None:
        raise SonicAuditContractError("invalid TikTok creator")
    return creator


def normalize_post_url(value: Any) -> dict[str, str]:
    parsed = urlsplit(str(value or "").strip())
    if parsed.scheme.casefold() != "https" or (
        parsed.hostname or ""
    ).rstrip(".").casefold() not in {"tiktok.com", "www.tiktok.com"}:
        raise SonicAuditContractError("post URL must use canonical TikTok HTTPS")
    match = re.fullmatch(
        r"/@([A-Za-z0-9._-]+)/(video|photo)/(\d+)/?",
        parsed.path,
        flags=re.IGNORECASE,
    )
    if match is None:
        raise SonicAuditContractError("invalid canonical TikTok post URL")
    creator, media_type, post_id = match.groups()
    creator = creator.casefold()
    media_type = media_type.casefold()
    return {
        "post_id": post_id,
        "creator": creator,
        "content_type": media_type,
        "canonical_url": (
            f"https://www.tiktok.com/@{creator}/{media_type}/{post_id}"
        ),
    }


def open_master_readonly(path: str | Path) -> sqlite3.Connection:
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


def _load_json(value: Any, *, name: str) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or ""))
    except json.JSONDecodeError as exc:
        raise SonicAuditContractError(f"invalid {name} JSON") from exc
    if not isinstance(parsed, dict):
        raise SonicAuditContractError(f"{name} must be a JSON object")
    return parsed


def _verify_embedded_hash(
    document: Mapping[str, Any],
    *,
    field: str,
    expected: Any,
    name: str,
    embedded_required: bool = True,
) -> None:
    supplied = str(document.get(field) or "")
    if not supplied and not embedded_required:
        valid = canonical_sha256(document) == str(expected or "")
    else:
        valid = supplied == str(expected or "") and verify_hash(document, field)
    if not valid:
        raise SonicAuditContractError(f"{name} hash mismatch")


def _safe_reference(music: Mapping[str, Any]) -> dict[str, Any]:
    platform = music.get("platform_music")
    platform = platform if isinstance(platform, Mapping) else {}
    resolution = music.get("tt2dsp_resolution")
    resolution = resolution if isinstance(resolution, Mapping) else {}
    selected = resolution.get("selected")
    selected = selected if isinstance(selected, Mapping) else {}
    provider_track_id = str(selected.get("provider_track_id") or "").strip()
    resolved = (
        str(resolution.get("status") or "").casefold() == "resolved"
        and bool(provider_track_id)
    )
    return {
        "reference_kind": "apple_track_id" if resolved else "unresolved",
        "reference_id": provider_track_id if resolved else "",
        "reference_title": str(selected.get("title") or "")[:500],
        "reference_artist": str(selected.get("artist") or "")[:500],
        "reference_basis": (
            "tt2dsp_exact_apple_id_resolution" if resolved else "none"
        ),
        "platform_music_id": str(platform.get("music_id") or "")[:200],
        "platform_music_title": str(platform.get("title") or "")[:500],
        "platform_music_author": str(platform.get("author") or "")[:500],
        "platform_music_original": (
            platform.get("is_original")
            if isinstance(platform.get("is_original"), bool)
            else None
        ),
    }


def load_creator_registry_candidates(
    master_database: str | Path,
    creator: str,
) -> list[dict[str, Any]]:
    """Return hash-verified known posts with their latest music observation.

    The query uses ``latest_snapshot_id -> tiktok_master_snapshots`` rather
    than the historically unreliable denormalized ``latest_evidence_json``.
    It is intentionally query-only and performs no schema migration.
    """

    creator_key = normalize_creator(creator)
    connection = open_master_readonly(master_database)
    try:
        has_backfill_observations = connection.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table'
              AND name = 'tiktok_master_music_backfill_observations'
            """
        ).fetchone() is not None
        if has_backfill_observations:
            query = """
            WITH ranked_music AS (
              SELECT o.*,
                     ROW_NUMBER() OVER (
                       PARTITION BY o.post_id
                       ORDER BY o.music_observed_at DESC,
                                o.created_at DESC,
                                o.observation_id DESC
                     ) AS rn
              FROM tiktok_master_music_backfill_observations o
            )
            SELECT p.post_id, p.canonical_url, p.creator_handle,
                   p.latest_snapshot_id, p.latest_evidence_hash,
                   s.observed_at AS base_observed_at,
                   s.evidence_hash AS snapshot_evidence_hash,
                   s.evidence_json,
                   m.music_observed_at, m.music_evidence_hash,
                   m.music_evidence_json, m.status AS music_observation_status
            FROM tiktok_master_posts p
            JOIN tiktok_master_snapshots s
              ON s.snapshot_id = p.latest_snapshot_id
             AND s.post_id = p.post_id
            LEFT JOIN ranked_music m
              ON m.post_id = p.post_id AND m.rn = 1
            WHERE p.creator_key = ?
            ORDER BY p.post_id
            """
        else:
            query = """
            SELECT p.post_id, p.canonical_url, p.creator_handle,
                   p.latest_snapshot_id, p.latest_evidence_hash,
                   s.observed_at AS base_observed_at,
                   s.evidence_hash AS snapshot_evidence_hash,
                   s.evidence_json,
                   NULL AS music_observed_at,
                   NULL AS music_evidence_hash,
                   NULL AS music_evidence_json,
                   NULL AS music_observation_status
            FROM tiktok_master_posts p
            JOIN tiktok_master_snapshots s
              ON s.snapshot_id = p.latest_snapshot_id
             AND s.post_id = p.post_id
            WHERE p.creator_key = ?
            ORDER BY p.post_id
            """
        rows = connection.execute(query, (creator_key,)).fetchall()
    finally:
        connection.close()

    candidates: list[dict[str, Any]] = []
    for row in rows:
        identity = normalize_post_url(row["canonical_url"])
        if identity["post_id"] != str(row["post_id"]):
            raise SonicAuditContractError("master post ID/URL mismatch")
        if identity["creator"] != creator_key:
            raise SonicAuditContractError("master creator/URL mismatch")
        evidence = _load_json(row["evidence_json"], name="base evidence")
        evidence_post_id = str(evidence.get("post_id") or "")
        if evidence_post_id and evidence_post_id != identity["post_id"]:
            raise SonicAuditContractError("base evidence post binding mismatch")
        _verify_embedded_hash(
            evidence,
            field="evidence_hash",
            expected=row["snapshot_evidence_hash"],
            name="base evidence",
            embedded_required=False,
        )
        if str(row["latest_evidence_hash"] or "") != str(
            row["snapshot_evidence_hash"] or ""
        ):
            raise SonicAuditContractError("latest snapshot binding mismatch")

        embedded_music = evidence.get("music_evidence")
        music: dict[str, Any] = (
            dict(embedded_music) if isinstance(embedded_music, Mapping) else {}
        )
        if music and music.get("music_evidence_hash"):
            _verify_embedded_hash(
                music,
                field="music_evidence_hash",
                expected=music.get("music_evidence_hash"),
                name="embedded music evidence",
            )
        if row["music_evidence_json"]:
            music = _load_json(row["music_evidence_json"], name="music evidence")
            _verify_embedded_hash(
                music,
                field="music_evidence_hash",
                expected=row["music_evidence_hash"],
                name="music evidence",
            )
        reference = _safe_reference(music)
        candidates.append(
            {
                "post_id": identity["post_id"],
                "canonical_url": identity["canonical_url"],
                "creator_handle": creator_key,
                "content_type": identity["content_type"],
                "base_snapshot_id": str(row["latest_snapshot_id"] or ""),
                "base_evidence_hash": str(row["snapshot_evidence_hash"] or ""),
                "base_observed_at": str(row["base_observed_at"] or ""),
                "music_observed_at": str(
                    row["music_observed_at"] or row["base_observed_at"] or ""
                ),
                "music_observation_status": str(
                    row["music_observation_status"]
                    or ("base_evidence" if music else "")
                ),
                **reference,
            }
        )
    return candidates


def _stable_order(rows: Iterable[Mapping[str, Any]], salt: str) -> list[dict[str, Any]]:
    return sorted(
        (dict(row) for row in rows),
        key=lambda row: hashlib.sha256(
            f"{salt}:{row.get('post_id', '')}".encode("utf-8")
        ).hexdigest(),
    )


def select_pilot_candidates(
    candidates: Sequence[Mapping[str, Any]],
    requested_count: int,
    *,
    creator: str,
) -> list[dict[str, Any]]:
    """Build a deterministic validation-rich pilot sample.

    For a 60-post pilot this targets 21 usages from repeated exact Apple track
    IDs, 30 distinct singleton Apple tracks, and 9 unresolved/original posts.
    Other requested sizes retain the same 35%/50%/remainder allocation and
    fill shortfalls from the remaining pools without duplicating a post.
    """

    if isinstance(requested_count, bool) or not 1 <= int(requested_count) <= MAX_PILOT_POSTS:
        raise SonicAuditContractError(
            f"posts must be between 1 and {MAX_PILOT_POSTS} for the pilot"
        )
    normalized_creator = normalize_creator(creator)
    rows = [
        dict(row)
        for row in candidates
        if str(row.get("content_type") or "").casefold() == "video"
    ]
    if len({str(row.get("post_id") or "") for row in rows}) != len(rows):
        raise SonicAuditContractError("candidate post IDs must be unique")
    for row in rows:
        if normalize_creator(row.get("creator_handle")) != normalized_creator:
            raise SonicAuditContractError("candidate owner mismatch")

    by_reference: dict[str, list[dict[str, Any]]] = defaultdict(list)
    unresolved: list[dict[str, Any]] = []
    for row in rows:
        reference = str(row.get("reference_id") or "")
        if reference:
            by_reference[reference].append(row)
        else:
            unresolved.append(row)

    repeated_groups = [
        _stable_order(group, f"repeat:{reference}")
        for reference, group in sorted(by_reference.items())
        if len(group) > 1
    ]
    singleton_rows = [
        group[0]
        for reference, group in sorted(by_reference.items())
        if len(group) == 1
    ]
    repeated_budget = min(requested_count, int(math.ceil(requested_count * 0.35)))
    singleton_budget = min(
        requested_count - repeated_budget,
        int(math.floor(requested_count * 0.50)),
    )

    repeated: list[dict[str, Any]] = []
    # Round-robin prevents one large recording group from swallowing the test.
    depth = 0
    while len(repeated) < repeated_budget:
        added = False
        for group in repeated_groups:
            if depth < len(group) and len(repeated) < repeated_budget:
                repeated.append(dict(group[depth]))
                added = True
        if not added:
            break
        depth += 1

    chosen: list[dict[str, Any]] = []
    chosen_ids: set[str] = set()

    def add(row: Mapping[str, Any], bucket: str) -> None:
        post_id = str(row.get("post_id") or "")
        if post_id in chosen_ids or len(chosen) >= requested_count:
            return
        item = dict(row)
        item["selection_bucket"] = bucket
        reference = str(item.get("reference_id") or "")
        item["reference_group_size"] = len(by_reference.get(reference, ())) if reference else 0
        chosen.append(item)
        chosen_ids.add(post_id)

    for row in repeated:
        add(row, "repeated_catalog_reference")
    for row in _stable_order(singleton_rows, f"{normalized_creator}:singleton")[:singleton_budget]:
        add(row, "singleton_catalog_reference")
    unresolved_order = _stable_order(unresolved, f"{normalized_creator}:unresolved")
    unresolved_target = requested_count - len(chosen)
    for row in unresolved_order[:unresolved_target]:
        add(row, "unresolved_platform_sound")

    # Fill any shortage without weakening identity or exact-owner gates.
    for row in _stable_order(singleton_rows, f"{normalized_creator}:singleton-fill"):
        add(row, "singleton_catalog_reference")
    for group in repeated_groups:
        for row in group:
            add(row, "repeated_catalog_reference")
    for row in unresolved_order:
        add(row, "unresolved_platform_sound")
    if len(chosen) != requested_count:
        raise SonicAuditContractError(
            f"only {len(chosen)} eligible known posts exist for requested {requested_count}"
        )
    return chosen


def select_non_overlapping_candidates(
    candidates: Sequence[Mapping[str, Any]],
    requested_count: int,
    *,
    creator: str,
    excluded_post_ids: Iterable[str],
) -> list[dict[str, Any]]:
    """Select a deterministic, externally-labelled-first extension sample.

    Exclusions are applied before selection.  Every remaining external
    catalogue-labelled video is ordered ahead of unresolved videos; when the
    labelled pool is larger than the requested sample, the same stable hash
    ordering makes the bounded subset reproducible.
    """

    if (
        isinstance(requested_count, bool)
        or not 1 <= int(requested_count) <= MAX_PILOT_POSTS
    ):
        raise SonicAuditContractError(
            f"posts must be between 1 and {MAX_PILOT_POSTS} for the pilot"
        )
    normalized_creator = normalize_creator(creator)
    excluded = {str(post_id or "").strip() for post_id in excluded_post_ids}
    if "" in excluded:
        raise SonicAuditContractError("excluded post IDs must be non-empty")

    videos: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for candidate in candidates:
        row = dict(candidate)
        if str(row.get("content_type") or "").casefold() != "video":
            continue
        if normalize_creator(row.get("creator_handle")) != normalized_creator:
            raise SonicAuditContractError("candidate owner mismatch")
        post_id = str(row.get("post_id") or "")
        if not post_id or post_id in seen_ids:
            raise SonicAuditContractError("candidate post IDs must be unique")
        seen_ids.add(post_id)
        if post_id not in excluded:
            videos.append(row)

    by_reference: dict[str, list[dict[str, Any]]] = defaultdict(list)
    unresolved: list[dict[str, Any]] = []
    for row in videos:
        reference_id = str(row.get("reference_id") or "").strip()
        if reference_id:
            by_reference[reference_id].append(row)
        else:
            unresolved.append(row)

    labelled = [row for group in by_reference.values() for row in group]
    labelled = _stable_order(
        labelled,
        f"{normalized_creator}:non-overlap:externally-labelled",
    )
    unresolved = _stable_order(
        unresolved,
        f"{normalized_creator}:non-overlap:unresolved",
    )

    chosen: list[dict[str, Any]] = []
    for row in (*labelled, *unresolved):
        if len(chosen) >= requested_count:
            break
        item = dict(row)
        reference_id = str(item.get("reference_id") or "").strip()
        if reference_id and reference_id in by_reference:
            group_size = len(by_reference[reference_id])
            item["reference_group_size"] = group_size
            item["selection_bucket"] = (
                "repeated_catalog_reference"
                if group_size > 1
                else "singleton_catalog_reference"
            )
        else:
            item["reference_group_size"] = 0
            item["selection_bucket"] = "unresolved_platform_sound"
        chosen.append(item)

    if len(chosen) != requested_count:
        raise SonicAuditContractError(
            f"only {len(chosen)} eligible non-overlapping known posts exist "
            f"for requested {requested_count}"
        )
    return chosen


def build_run_manifest(
    *,
    run_id: str,
    project: str,
    creator: str,
    requested_count: int,
    master_database: str | Path,
    candidates: Sequence[Mapping[str, Any]],
    transient_audio_authorized: bool,
    selection_method: str | None = None,
    selection_context: Mapping[str, Any] | None = None,
    third_party_audio_upload_authorized: bool = False,
    symbolic_analysis: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not transient_audio_authorized:
        raise SonicAuditContractError("transient audio authorization is required")
    creator_key = normalize_creator(creator)
    selected = [dict(row) for row in candidates]
    if len(selected) != requested_count:
        raise SonicAuditContractError("manifest candidate count mismatch")
    method = (
        "stratified-known-post-sonic-pilot-v1"
        if selection_method is None
        else str(selection_method).strip()
    )
    if not method:
        raise SonicAuditContractError("selection method is required")
    body = {
        "schema_version": RUN_SCHEMA,
        "run_id": str(run_id),
        "project": str(project).strip(),
        "workflow": "sonic_audit",
        "creator": creator_key,
        "requested_count": int(requested_count),
        "selected_count": len(selected),
        "selection_method": method,
        "master_database": str(Path(master_database).resolve()),
        "created_at": utc_now(),
        "authorization": {
            "transient_audio": True,
            "authorized_by": "operator",
        },
        "retention": {
            "raw_media_persisted": False,
            "decoded_audio_persisted": False,
            "derived_features_persisted": True,
            "cleanup_required_on_success_and_error": True,
        },
        "boundaries": {
            "master_registry_mutated": False,
            "ai_analysis_performed": False,
            "publication_eligible": False,
            "engagement_causality_claimed": False,
        },
        "candidates": selected,
    }
    if symbolic_analysis is not None:
        if not third_party_audio_upload_authorized:
            raise SonicAuditContractError(
                "third-party audio-upload authorization is required"
            )
        if not isinstance(symbolic_analysis, Mapping):
            raise SonicAuditContractError("symbolic analysis config must be an object")
        try:
            frozen_symbolic = json.loads(canonical_json(symbolic_analysis))
        except (TypeError, ValueError) as exc:
            raise SonicAuditContractError(
                "symbolic analysis config must be canonically serializable"
            ) from exc
        if not frozen_symbolic:
            raise SonicAuditContractError("symbolic analysis config is empty")
        validate_symbolic_analysis_config(frozen_symbolic)
        body["symbolic_analysis"] = frozen_symbolic
        body["authorization"]["third_party_audio_upload"] = True
        body["authorization"]["third_party_provider"] = str(
            frozen_symbolic.get("provider") or ""
        )
        body["retention"].update(
            {
                "provider_midi_persisted": False,
                "provider_musicxml_persisted": False,
                "provider_raw_notes_persisted": False,
            }
        )
        body["boundaries"].update(
            {
                "semantic_ai_analysis_performed": False,
                "external_symbolic_transcription_configured": True,
                "symbolic_used_in_primary_recording_score": False,
            }
        )
    elif third_party_audio_upload_authorized:
        raise SonicAuditContractError(
            "third-party upload authorization has no configured provider"
        )
    if selection_context is not None:
        if not isinstance(selection_context, Mapping):
            raise SonicAuditContractError("selection context must be an object")
        # Canonical round-tripping both detaches caller-owned nested values and
        # rejects values that cannot participate in a hash-bound manifest.
        try:
            body["selection_context"] = json.loads(canonical_json(selection_context))
        except (TypeError, ValueError) as exc:
            raise SonicAuditContractError(
                "selection context must be canonically serializable"
            ) from exc
    if not body["run_id"] or not body["project"]:
        raise SonicAuditContractError("run ID and project are required")
    return bind_hash(body, "manifest_hash")


def validate_symbolic_analysis_config(config: Mapping[str, Any]) -> None:
    """Validate the closed, secret-free Mirelo contract frozen in a run."""

    allowed_keys = {
        "schema_version",
        "provider",
        "model",
        "endpoint_version",
        "timing",
        "input_basis",
        "max_run_credits",
        "preflight_required",
        "server_asset_retention",
        "credential_environment_variable",
        "persist_raw_notes",
        "persist_midi",
        "persist_musicxml",
        "changes_primary_recording_score",
        "config_hash",
    }
    if set(config) != allowed_keys:
        raise SonicAuditContractError("symbolic analysis config schema is not closed")
    if config.get("schema_version") != MIRELO_SYMBOLIC_CONFIG_SCHEMA:
        raise SonicAuditContractError("symbolic analysis config schema is unsupported")
    if config.get("provider") != "mirelo" or config.get("model") != "audio-to-midi-v1.0":
        raise SonicAuditContractError("symbolic analysis provider/model is unsupported")
    if config.get("endpoint_version") != "v2" or config.get("timing") != "performance":
        raise SonicAuditContractError("symbolic analysis endpoint/timing is unsupported")
    if config.get("input_basis") != "transient_decoded_wav":
        raise SonicAuditContractError("symbolic analysis input basis is unsupported")
    budget = config.get("max_run_credits")
    if isinstance(budget, bool) or not isinstance(budget, int) or budget < 1:
        raise SonicAuditContractError("symbolic analysis credit budget is invalid")
    if config.get("preflight_required") is not True:
        raise SonicAuditContractError("symbolic analysis preflight must be required")
    if config.get("server_asset_retention") != "up_to_24_hours":
        raise SonicAuditContractError("symbolic analysis retention contract is invalid")
    if config.get("credential_environment_variable") != "MIRELO_API_KEY":
        raise SonicAuditContractError("symbolic analysis credential contract is invalid")
    for key in (
        "persist_raw_notes",
        "persist_midi",
        "persist_musicxml",
        "changes_primary_recording_score",
    ):
        if config.get(key) is not False:
            raise SonicAuditContractError(f"symbolic analysis {key} must be false")
    if not verify_hash(config, "config_hash"):
        raise SonicAuditContractError("symbolic analysis config hash is invalid")


def build_initial_state(manifest: Mapping[str, Any]) -> dict[str, Any]:
    if not verify_hash(manifest, "manifest_hash"):
        raise SonicAuditContractError("invalid manifest hash")
    return bind_hash(
        {
            "schema_version": STATE_SCHEMA,
            "run_id": str(manifest.get("run_id") or ""),
            "manifest_hash": str(manifest.get("manifest_hash") or ""),
            "status": "planned",
            "selected": int(manifest.get("selected_count") or 0),
            "completed": 0,
            "unavailable": 0,
            "pending": int(manifest.get("selected_count") or 0),
            "observed_account": "",
            "last_error": "",
            "updated_at": utc_now(),
        },
        "state_hash",
    )


def bind_feature_record(value: Mapping[str, Any]) -> dict[str, Any]:
    record = dict(value)
    record["schema_version"] = FEATURE_RECORD_SCHEMA
    return bind_hash(record, "feature_record_hash")


def validate_feature_record(
    record: Mapping[str, Any],
    *,
    manifest: Mapping[str, Any],
) -> None:
    if not verify_hash(manifest, "manifest_hash"):
        raise SonicAuditContractError("invalid manifest hash")
    if record.get("schema_version") != FEATURE_RECORD_SCHEMA or not verify_hash(
        record, "feature_record_hash"
    ):
        raise SonicAuditContractError("invalid feature record")
    if record.get("run_id") != manifest.get("run_id") or record.get(
        "manifest_hash"
    ) != manifest.get("manifest_hash"):
        raise SonicAuditContractError("feature record run binding mismatch")
    candidates = {
        str(row.get("post_id") or ""): row
        for row in manifest.get("candidates", [])
        if isinstance(row, Mapping)
    }
    post_id = str(record.get("post_id") or "")
    candidate = candidates.get(post_id)
    if candidate is None:
        raise SonicAuditContractError("feature record post is outside frozen scope")
    for key in ("base_snapshot_id", "base_evidence_hash"):
        if record.get(key) != candidate.get(key):
            raise SonicAuditContractError(f"feature record {key} mismatch")
    if candidate.get("corpus_plan_hash"):
        provenance = record.get("selection_provenance")
        if not isinstance(provenance, Mapping):
            raise SonicAuditContractError(
                "plan-bound feature record has no selection provenance"
            )
        for key in (
            "corpus_plan_hash",
            "corpus_batch_id",
            "corpus_batch_hash",
            "plan_candidate_hash",
            "group_id",
            "corpus_group_hash",
            "batch_position",
            "selection_position",
        ):
            if provenance.get(key) != candidate.get(key):
                raise SonicAuditContractError(
                    f"feature record selection provenance {key} mismatch"
                )
        reference = record.get("reference")
        if not isinstance(reference, Mapping):
            raise SonicAuditContractError("plan-bound feature record has no reference")
        expected_reference = {
            "reference_kind": "apple_track_id",
            "reference_id": candidate.get("reference_track_id"),
            "reference_label": candidate.get("reference_label"),
            "reference_title": candidate.get("reference_title"),
            "reference_artist": candidate.get("reference_artist"),
            "reference_provider": candidate.get("reference_provider"),
            "reference_storefront": candidate.get("reference_storefront"),
            "reference_track_id": candidate.get("reference_track_id"),
        }
        if any(reference.get(key) != value for key, value in expected_reference.items()):
            raise SonicAuditContractError("plan-bound feature record reference mismatch")
    if record.get("status") not in {"completed", "unavailable"}:
        raise SonicAuditContractError("feature record status is not terminal")


__all__ = [
    "DEFAULT_OUTPUT_ROOT",
    "FEATURE_RECORD_SCHEMA",
    "MAX_PILOT_POSTS",
    "REPORT_SCHEMA",
    "RUN_SCHEMA",
    "STATE_SCHEMA",
    "SonicAuditContractError",
    "bind_feature_record",
    "bind_hash",
    "build_initial_state",
    "build_run_manifest",
    "canonical_json",
    "canonical_sha256",
    "load_creator_registry_candidates",
    "normalize_creator",
    "normalize_post_url",
    "open_master_readonly",
    "select_pilot_candidates",
    "select_non_overlapping_candidates",
    "utc_now",
    "validate_feature_record",
    "verify_hash",
]
