"""Workspace-wide TikTok LISTEN/AUDIT/ENGAGE registry.

Project databases remain authoritative for their local workflow state.  This
module attaches one workspace-level SQLite database to those connections and
maintains the cross-project facts that must not be reset by starting a new
project:

* every LISTEN/AUDIT/ENGAGE run and its source database;
* immutable, hash-bound aggregate reports produced by AUDIT runs;
* one global identity row per TikTok post;
* append-only evidence observations and change summaries;
* topic/run membership and first-seen comment identities;
* short collection leases used to avoid concurrent duplicate scraping; and
* account-plus-post publication attempts and terminal comment history.

The module is deliberately stdlib-only so the registry can be inspected and
legacy databases can be imported even when browser/collector dependencies are
not installed in the current Python runtime.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Sequence


DEFAULT_MASTER_DATABASE = (
    Path("comments_data")
    / "tiktok_master"
    / "state"
    / "tiktok_master.sqlite"
)
MASTER_SCHEMA_VERSION = "7"
MASTER_SCHEMA_NAMES = frozenset({"main", "master"})
MUSIC_BACKFILL_RUN_STATUSES = frozenset(
    {"planned", "running", "backfill_complete", "backfill_incomplete", "failed"}
)
MUSIC_BACKFILL_OBSERVATION_STATUSES = frozenset(
    {"completed", "unavailable", "failed"}
)
DEFAULT_MUSIC_BACKFILL_RETRYABLE_STATUSES = (
    "unavailable",
    "rate_limited",
    "provider_error",
)
MUSIC_BACKFILL_SUPPORTED_CATALOGS = frozenset({"musicbrainz"})
MUSIC_BACKFILL_OBSERVATION_SCHEMA_VERSION = (
    "tiktok-music-backfill-observation-v1"
)
BLOCKING_COMMENT_STATES = frozenset(
    {"reserved", "submit_intent", "uncertain", "confirmed"}
)
BLOCKING_SHOWCASE_STATES = frozenset(
    {"reserved", "submit_intent", "uncertain", "confirmed"}
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


def _required_sha256(value: Any, *, field: str) -> str:
    normalized = str(value or "").strip().casefold()
    if re.fullmatch(r"[0-9a-f]{64}", normalized) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256 hex digest")
    return normalized


def stable_id(*parts: Any, length: int = 32) -> str:
    payload = "\x1f".join(str(part or "") for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]


def _schema(value: str) -> str:
    schema = str(value or "main").strip().casefold()
    if schema not in MASTER_SCHEMA_NAMES:
        raise ValueError("master schema must be main or master")
    return schema


def _table(schema: str, name: str) -> str:
    return f'"{_schema(schema)}"."{name}"'


def _index(schema: str, name: str) -> str:
    return f'"{_schema(schema)}"."{name}"'


def _normalize_path(value: str | Path) -> str:
    return str(Path(value).resolve())


def _main_database_path(conn: sqlite3.Connection) -> str:
    for row in conn.execute("PRAGMA database_list").fetchall():
        if str(row[1]) == "main":
            raw_path = str(row[2] or "").strip()
            return _normalize_path(raw_path) if raw_path else ""
    return ""


def _table_exists(
    conn: sqlite3.Connection,
    schema: str,
    table_name: str,
) -> bool:
    row = conn.execute(
        f"""
        SELECT 1
        FROM "{_schema(schema)}".sqlite_master
        WHERE type='table' AND name=?
        """,
        (table_name,),
    ).fetchone()
    return row is not None


def _columns(
    conn: sqlite3.Connection,
    schema: str,
    table_name: str,
) -> set[str]:
    return {
        str(row[1])
        for row in conn.execute(
            f'PRAGMA "{_schema(schema)}".table_info("{table_name}")'
        ).fetchall()
    }


def _row_dict(row: sqlite3.Row | Sequence[Any], columns: Sequence[str]) -> dict[str, Any]:
    if isinstance(row, sqlite3.Row):
        return {key: row[key] for key in row.keys()}
    return dict(zip(columns, row))


def _parse_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _parse_json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if not isinstance(value, str) or not value.strip():
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def normalize_topic(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def normalize_creator_handle(value: Any) -> str:
    """Return the stable lookup key for a TikTok creator handle.

    Creator selectors commonly arrive as ``maker``, ``@Maker``, or a full
    profile/post URL.  TikTok handles are case-insensitive, so the master
    registry keeps the original handle for display and a separate case-folded
    key for selection.  A URL without an ``/@handle`` path is not a creator
    identity and intentionally normalizes to the empty string.
    """

    raw = str(value or "").strip()
    if not raw:
        return ""
    match = re.search(
        r"(?:^|(?:https?://)?(?:www\.)?tiktok\.com/)@([^/?#\s]+)",
        raw,
        flags=re.IGNORECASE,
    )
    if match:
        raw = match.group(1)
    elif "://" in raw:
        return ""
    else:
        raw = raw.lstrip("@").strip()
        if "/" in raw or "?" in raw or "#" in raw:
            return ""
    return raw.casefold()


def _creator_key(creator_handle: Any, canonical_url: Any = "") -> str:
    return normalize_creator_handle(creator_handle) or normalize_creator_handle(
        canonical_url
    )


class CreatorIdentityConflictError(RuntimeError):
    """Raised when evidence would move a post to an unproven creator."""


def _consistent_creator_identifier(
    values: Iterable[Any],
    *,
    field: str,
) -> str:
    normalized = {
        str(value or "").strip()
        for value in values
        if str(value or "").strip()
    }
    if len(normalized) > 1:
        raise CreatorIdentityConflictError(
            f"conflicting TikTok creator {field} values in one evidence packet"
        )
    return next(iter(normalized), "")


def _creator_identity_from_evidence(evidence: Any) -> dict[str, str]:
    evidence = evidence if isinstance(evidence, dict) else {}
    nested = (
        evidence.get("creator_identity")
        if isinstance(evidence.get("creator_identity"), dict)
        else {}
    )
    handle_values = [
        evidence.get("creator"),
        evidence.get("username"),
        evidence.get("content_creator"),
        nested.get("handle"),
    ]
    creator = str(next((value for value in handle_values if value), "")).strip()
    creator = creator.lstrip("@")
    canonical_url = str(evidence.get("url") or "").strip()
    handle_keys = {
        key
        for key in (
            *(normalize_creator_handle(value) for value in handle_values),
            normalize_creator_handle(canonical_url),
        )
        if key
    }
    if len(handle_keys) > 1:
        raise CreatorIdentityConflictError(
            "conflicting TikTok creator handles in one evidence packet"
        )
    creator_key = next(iter(handle_keys), "")
    if not creator and creator_key:
        creator = creator_key
    return {
        "handle": creator,
        "key": creator_key,
        "user_id": _consistent_creator_identifier(
            (
                evidence.get("creator_user_id"),
                evidence.get("creator_id"),
                nested.get("user_id"),
                nested.get("id"),
                nested.get("creator_id"),
            ),
            field="user ID",
        ),
        "sec_uid": _consistent_creator_identifier(
            (
                evidence.get("creator_sec_uid"),
                evidence.get("sec_uid"),
                nested.get("sec_uid"),
                nested.get("secUid"),
            ),
            field="secUid",
        ),
    }


def _merge_creator_identity(
    current: dict[str, str],
    incoming: dict[str, str],
    *,
    post_id: str,
) -> dict[str, str]:
    """Merge one chronological owner observation under fail-closed rules."""

    current = {
        key: str(current.get(key) or "").strip()
        for key in ("handle", "key", "user_id", "sec_uid")
    }
    incoming = {
        key: str(incoming.get(key) or "").strip()
        for key in ("handle", "key", "user_id", "sec_uid")
    }
    matching_stable_id = False
    for field, label in (("user_id", "user ID"), ("sec_uid", "secUid")):
        before = current[field]
        after = incoming[field]
        if before and after:
            if before != after:
                raise CreatorIdentityConflictError(
                    f"TikTok post {post_id} creator {label} drift"
                )
            matching_stable_id = True
    handle_changed = bool(
        current["key"]
        and incoming["key"]
        and current["key"] != incoming["key"]
    )
    if handle_changed and not matching_stable_id:
        raise CreatorIdentityConflictError(
            f"TikTok post {post_id} creator handle drift lacks stable-ID continuity"
        )
    merged = dict(current)
    if incoming["key"] and (not current["key"] or handle_changed):
        merged["handle"] = incoming["handle"] or incoming["key"]
        merged["key"] = incoming["key"]
    elif incoming["key"] and incoming["key"] == current["key"]:
        # Case-only changes do not create a new creator scope, but retain the
        # latest platform spelling for display.
        merged["handle"] = incoming["handle"] or current["handle"]
    for field in ("user_id", "sec_uid"):
        if incoming[field] and not merged[field]:
            merged[field] = incoming[field]
    return merged


def normalize_account(value: Any) -> str:
    normalized = str(value or "").strip().lstrip("@").casefold()
    if normalized in {
        "",
        "*",
        "legacy_unknown",
        "unknown",
        "unverified",
        "not_available",
    }:
        return "*"
    return normalized or "*"


def canonical_timestamp(value: Any, *, fallback_now: bool = False) -> str:
    """Normalize an ISO-8601 timestamp to a comparable UTC representation."""

    raw = str(value or "").strip()
    if not raw and fallback_now:
        raw = now_iso()
    if not raw:
        raise ValueError("timestamp is required")
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid ISO-8601 timestamp: {raw}") from exc
    if parsed.tzinfo is None:
        # Legacy timestamps without an offset must not change meaning when
        # imported on a workstation in a different local timezone.
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    parsed = parsed.astimezone(dt.timezone.utc)
    timespec = "microseconds" if parsed.microsecond else "seconds"
    return parsed.isoformat(timespec=timespec)


def ensure_master_schema(
    conn: sqlite3.Connection,
    schema: str = "main",
) -> None:
    """Create or migrate the workspace registry in ``schema``.

    The function does not commit.  Callers can therefore attach the master and
    include registry writes in the same transaction as local workflow writes.
    """

    schema = _schema(schema)
    q = lambda name: _table(schema, name)
    statements = [
        f"""
        CREATE TABLE IF NOT EXISTS {q("tiktok_master_meta")} (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {q("tiktok_master_sources")} (
            source_id TEXT PRIMARY KEY,
            database_path TEXT NOT NULL UNIQUE,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            imported_at TEXT NOT NULL DEFAULT ''
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {q("tiktok_master_runs")} (
            master_run_id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL,
            local_run_id TEXT NOT NULL,
            project TEXT NOT NULL DEFAULT '',
            workflow TEXT NOT NULL DEFAULT 'legacy_unknown',
            topic TEXT NOT NULL DEFAULT '',
            topic_key TEXT NOT NULL DEFAULT '',
            source_mode TEXT NOT NULL DEFAULT 'topic',
            direct_post_url TEXT NOT NULL DEFAULT '',
            creator_handle TEXT NOT NULL DEFAULT '',
            cardinality_mode TEXT NOT NULL DEFAULT 'exact_count',
            profile_inventory_count INTEGER NOT NULL DEFAULT 0,
            profile_inventory_terminal INTEGER NOT NULL DEFAULT 0,
            profile_inventory_hash TEXT NOT NULL DEFAULT '',
            requested_count INTEGER NOT NULL DEFAULT 0,
            collection_policy TEXT NOT NULL DEFAULT 'new_only',
            mode TEXT NOT NULL DEFAULT 'shadow',
            expected_account TEXT NOT NULL DEFAULT '',
            observed_account TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT '',
            requested INTEGER NOT NULL DEFAULT 0,
            unique_collected INTEGER NOT NULL DEFAULT 0,
            evidence_ready INTEGER NOT NULL DEFAULT 0,
            analyzed INTEGER NOT NULL DEFAULT 0,
            drafted INTEGER NOT NULL DEFAULT 0,
            reviewed INTEGER NOT NULL DEFAULT 0,
            stored INTEGER NOT NULL DEFAULT 0,
            authorized INTEGER NOT NULL DEFAULT 0,
            published INTEGER NOT NULL DEFAULT 0,
            skipped INTEGER NOT NULL DEFAULT 0,
            failed INTEGER NOT NULL DEFAULT 0,
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT '',
            synced_at TEXT NOT NULL,
            UNIQUE (source_id, local_run_id)
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {q("tiktok_master_audit_reports")} (
            master_run_id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL,
            local_run_id TEXT NOT NULL,
            schema_version TEXT NOT NULL,
            rubric_version TEXT NOT NULL,
            analysis_set_hash TEXT NOT NULL,
            report_json TEXT NOT NULL,
            report_hash TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            local_updated_at TEXT NOT NULL,
            synced_at TEXT NOT NULL,
            UNIQUE (source_id, local_run_id)
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {q("tiktok_master_posts")} (
            post_id TEXT PRIMARY KEY,
            canonical_url TEXT NOT NULL DEFAULT '',
            creator_handle TEXT NOT NULL DEFAULT '',
            creator_key TEXT NOT NULL DEFAULT '',
            creator_user_id TEXT NOT NULL DEFAULT '',
            creator_sec_uid TEXT NOT NULL DEFAULT '',
            creator_display_name TEXT NOT NULL DEFAULT '',
            first_source_id TEXT NOT NULL DEFAULT '',
            first_run_id TEXT NOT NULL DEFAULT '',
            first_seen_at TEXT NOT NULL,
            last_source_id TEXT NOT NULL DEFAULT '',
            last_run_id TEXT NOT NULL DEFAULT '',
            last_seen_at TEXT NOT NULL,
            latest_snapshot_id TEXT NOT NULL DEFAULT '',
            latest_evidence_hash TEXT NOT NULL DEFAULT '',
            latest_content_hash TEXT NOT NULL DEFAULT '',
            latest_evidence_json TEXT NOT NULL DEFAULT '{{}}',
            snapshot_count INTEGER NOT NULL DEFAULT 0,
            run_count INTEGER NOT NULL DEFAULT 0,
            known_comment_count INTEGER NOT NULL DEFAULT 0,
            last_change_at TEXT NOT NULL DEFAULT '',
            unavailable_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {q("tiktok_master_snapshots")} (
            snapshot_id TEXT PRIMARY KEY,
            post_id TEXT NOT NULL,
            source_id TEXT NOT NULL,
            master_run_id TEXT NOT NULL,
            local_run_id TEXT NOT NULL,
            account_key TEXT NOT NULL DEFAULT '*',
            observed_at TEXT NOT NULL,
            evidence_hash TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            creator_user_id TEXT NOT NULL DEFAULT '',
            creator_sec_uid TEXT NOT NULL DEFAULT '',
            previous_snapshot_id TEXT NOT NULL DEFAULT '',
            is_changed INTEGER NOT NULL DEFAULT 0,
            new_comment_count INTEGER NOT NULL DEFAULT 0,
            changed_comment_count INTEGER NOT NULL DEFAULT 0,
            total_comment_count INTEGER NOT NULL DEFAULT 0,
            change_json TEXT NOT NULL DEFAULT '{{}}',
            evidence_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (source_id, local_run_id, post_id)
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {q("tiktok_master_run_posts")} (
            master_run_id TEXT NOT NULL,
            post_id TEXT NOT NULL,
            snapshot_id TEXT NOT NULL,
            collection_policy TEXT NOT NULL DEFAULT 'new_only',
            position INTEGER NOT NULL DEFAULT 0,
            first_linked_at TEXT NOT NULL,
            PRIMARY KEY (master_run_id, post_id)
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {q("tiktok_master_post_topics")} (
            post_id TEXT NOT NULL,
            topic_key TEXT NOT NULL,
            topic TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            run_count INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (post_id, topic_key)
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {q("tiktok_master_comments")} (
            post_id TEXT NOT NULL,
            comment_key TEXT NOT NULL,
            platform_comment_id TEXT NOT NULL DEFAULT '',
            parent_comment_key TEXT NOT NULL DEFAULT '',
            is_reply INTEGER NOT NULL DEFAULT 0,
            author_handle TEXT NOT NULL DEFAULT '',
            author_display_name TEXT NOT NULL DEFAULT '',
            comment_text TEXT NOT NULL DEFAULT '',
            text_hash TEXT NOT NULL DEFAULT '',
            platform_created_at TEXT NOT NULL DEFAULT '',
            first_snapshot_id TEXT NOT NULL,
            last_snapshot_id TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            seen_count INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (post_id, comment_key)
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {q("tiktok_master_collection_leases")} (
            post_id TEXT PRIMARY KEY,
            master_run_id TEXT NOT NULL,
            local_run_id TEXT NOT NULL,
            attempt_id TEXT NOT NULL,
            collection_policy TEXT NOT NULL,
            acquired_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {q("tiktok_master_provider_rate_limits")} (
            provider TEXT PRIMARY KEY,
            next_allowed_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {q("tiktok_master_music_backfill_runs")} (
            run_id TEXT PRIMARY KEY,
            project TEXT NOT NULL DEFAULT '',
            master_database_path TEXT NOT NULL DEFAULT '',
            scope_mode TEXT NOT NULL,
            scope_value TEXT NOT NULL DEFAULT '',
            target_schema_version TEXT NOT NULL,
            configured_catalogs_json TEXT NOT NULL,
            retryable_statuses_json TEXT NOT NULL,
            force INTEGER NOT NULL DEFAULT 0,
            expected_account TEXT NOT NULL DEFAULT '',
            observed_account TEXT NOT NULL DEFAULT '',
            candidate_set_json TEXT NOT NULL,
            candidate_set_hash TEXT NOT NULL,
            run_hash TEXT NOT NULL,
            selected_count INTEGER NOT NULL,
            completed_count INTEGER NOT NULL DEFAULT 0,
            unavailable_count INTEGER NOT NULL DEFAULT 0,
            failed_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'planned',
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT NOT NULL DEFAULT ''
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {q("tiktok_master_music_backfill_observations")} (
            observation_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            post_id TEXT NOT NULL,
            base_snapshot_id TEXT NOT NULL,
            base_evidence_hash TEXT NOT NULL,
            base_observed_at TEXT NOT NULL,
            music_observed_at TEXT NOT NULL,
            music_evidence_json TEXT NOT NULL,
            music_evidence_hash TEXT NOT NULL,
            status TEXT NOT NULL,
            error TEXT NOT NULL DEFAULT '',
            observation_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (run_id, post_id)
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {q("tiktok_master_comment_targets")} (
            account_key TEXT NOT NULL,
            post_id TEXT NOT NULL,
            state TEXT NOT NULL,
            active_attempt_id TEXT NOT NULL DEFAULT '',
            confirmed_attempt_id TEXT NOT NULL DEFAULT '',
            first_publication_id TEXT NOT NULL DEFAULT '',
            last_publication_id TEXT NOT NULL DEFAULT '',
            canonical_url TEXT NOT NULL DEFAULT '',
            confirmed_text_hash TEXT NOT NULL DEFAULT '',
            remote_comment_id TEXT NOT NULL DEFAULT '',
            remote_comment_url TEXT NOT NULL DEFAULT '',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            first_attempt_at TEXT NOT NULL DEFAULT '',
            last_attempt_at TEXT NOT NULL DEFAULT '',
            submit_intent_at TEXT NOT NULL DEFAULT '',
            confirmed_at TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            PRIMARY KEY (account_key, post_id)
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {q("tiktok_master_comment_attempts")} (
            attempt_id TEXT PRIMARY KEY,
            account_key TEXT NOT NULL,
            post_id TEXT NOT NULL,
            publication_id TEXT NOT NULL,
            source_id TEXT NOT NULL DEFAULT '',
            local_run_id TEXT NOT NULL DEFAULT '',
            target_url TEXT NOT NULL DEFAULT '',
            text_hash TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL,
            local_attempt_number INTEGER NOT NULL DEFAULT 0,
            reserved_at TEXT NOT NULL,
            submit_intent_at TEXT NOT NULL DEFAULT '',
            resolved_at TEXT NOT NULL DEFAULT '',
            local_receipt_id TEXT NOT NULL DEFAULT '',
            remote_comment_id TEXT NOT NULL DEFAULT '',
            remote_comment_url TEXT NOT NULL DEFAULT '',
            visible INTEGER NOT NULL DEFAULT 0,
            persisted INTEGER NOT NULL DEFAULT 0,
            error TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{{}}',
            UNIQUE (source_id, publication_id, local_attempt_number)
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {q("tiktok_master_showcase_targets")} (
            source_comment_attempt_id TEXT PRIMARY KEY,
            posting_account_key TEXT NOT NULL,
            source_post_id TEXT NOT NULL DEFAULT '',
            source_comment_id TEXT NOT NULL DEFAULT '',
            source_publication_id TEXT NOT NULL DEFAULT '',
            media_hash TEXT NOT NULL,
            caption_hash TEXT NOT NULL,
            state TEXT NOT NULL,
            active_attempt_id TEXT NOT NULL DEFAULT '',
            confirmed_attempt_id TEXT NOT NULL DEFAULT '',
            first_source_id TEXT NOT NULL DEFAULT '',
            last_source_id TEXT NOT NULL DEFAULT '',
            first_local_job_id TEXT NOT NULL DEFAULT '',
            last_local_job_id TEXT NOT NULL DEFAULT '',
            remote_post_id TEXT NOT NULL DEFAULT '',
            remote_post_url TEXT NOT NULL DEFAULT '',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            first_attempt_at TEXT NOT NULL DEFAULT '',
            last_attempt_at TEXT NOT NULL DEFAULT '',
            submit_intent_at TEXT NOT NULL DEFAULT '',
            confirmed_at TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS {q("tiktok_master_showcase_attempts")} (
            attempt_id TEXT PRIMARY KEY,
            source_comment_attempt_id TEXT NOT NULL,
            posting_account_key TEXT NOT NULL,
            source_post_id TEXT NOT NULL DEFAULT '',
            source_comment_id TEXT NOT NULL DEFAULT '',
            source_publication_id TEXT NOT NULL DEFAULT '',
            media_hash TEXT NOT NULL,
            caption_hash TEXT NOT NULL,
            source_id TEXT NOT NULL,
            local_job_id TEXT NOT NULL,
            local_attempt_number INTEGER NOT NULL DEFAULT 0,
            state TEXT NOT NULL,
            reserved_at TEXT NOT NULL,
            submit_intent_at TEXT NOT NULL DEFAULT '',
            publish_id TEXT NOT NULL DEFAULT '',
            publish_id_checkpoint_at TEXT NOT NULL DEFAULT '',
            resolved_at TEXT NOT NULL DEFAULT '',
            local_receipt_id TEXT NOT NULL DEFAULT '',
            remote_post_id TEXT NOT NULL DEFAULT '',
            remote_post_url TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{{}}',
            UNIQUE (source_id, local_job_id, local_attempt_number)
        )
        """,
    ]
    for statement in statements:
        conn.execute(statement)

    run_columns = _columns(conn, schema, "tiktok_master_runs")
    run_additions = {
        "source_mode": "TEXT NOT NULL DEFAULT 'topic'",
        "direct_post_url": "TEXT NOT NULL DEFAULT ''",
        "creator_handle": "TEXT NOT NULL DEFAULT ''",
        "cardinality_mode": "TEXT NOT NULL DEFAULT 'exact_count'",
        "profile_inventory_count": "INTEGER NOT NULL DEFAULT 0",
        "profile_inventory_terminal": "INTEGER NOT NULL DEFAULT 0",
        "profile_inventory_hash": "TEXT NOT NULL DEFAULT ''",
    }
    for name, definition in run_additions.items():
        if name not in run_columns:
            conn.execute(
                f"ALTER TABLE "
                f'{_table(schema, "tiktok_master_runs")} '
                f'ADD COLUMN "{name}" {definition}'
            )

    post_columns = _columns(conn, schema, "tiktok_master_posts")
    post_identity_migration = any(
        name not in post_columns
        for name in ("creator_user_id", "creator_sec_uid")
    )
    post_additions = {
        "creator_key": "TEXT NOT NULL DEFAULT ''",
        "creator_user_id": "TEXT NOT NULL DEFAULT ''",
        "creator_sec_uid": "TEXT NOT NULL DEFAULT ''",
    }
    for name, definition in post_additions.items():
        if name not in post_columns:
            conn.execute(
                f"ALTER TABLE "
                f'{_table(schema, "tiktok_master_posts")} '
                f'ADD COLUMN "{name}" {definition}'
            )

    snapshot_columns = _columns(conn, schema, "tiktok_master_snapshots")
    snapshot_identity_migration = any(
        name not in snapshot_columns
        for name in ("creator_user_id", "creator_sec_uid")
    )
    snapshot_additions = {
        "creator_user_id": "TEXT NOT NULL DEFAULT ''",
        "creator_sec_uid": "TEXT NOT NULL DEFAULT ''",
    }
    for name, definition in snapshot_additions.items():
        if name not in snapshot_columns:
            conn.execute(
                f"ALTER TABLE "
                f'{_table(schema, "tiktok_master_snapshots")} '
                f'ADD COLUMN "{name}" {definition}'
            )

    identity_rows = (
        conn.execute(
            f"""
            SELECT snapshot_id, post_id, evidence_json,
                   creator_user_id, creator_sec_uid
            FROM {q("tiktok_master_snapshots")}
            """
        ).fetchall()
        if post_identity_migration or snapshot_identity_migration
        else []
    )
    post_identity_values: dict[str, dict[str, set[str]]] = {}
    for (
        snapshot_id,
        post_id,
        evidence_json,
        stored_user_id,
        stored_sec_uid,
    ) in identity_rows:
        identity = _creator_identity_from_evidence(
            _parse_json_object(evidence_json)
        )
        user_id = _consistent_creator_identifier(
            (stored_user_id, identity["user_id"]),
            field="user ID",
        )
        sec_uid = _consistent_creator_identifier(
            (stored_sec_uid, identity["sec_uid"]),
            field="secUid",
        )
        if user_id != str(stored_user_id or "") or sec_uid != str(
            stored_sec_uid or ""
        ):
            conn.execute(
                f"""
                UPDATE {q("tiktok_master_snapshots")}
                SET creator_user_id=?, creator_sec_uid=?
                WHERE snapshot_id=?
                """,
                (user_id, sec_uid, str(snapshot_id)),
            )
        values = post_identity_values.setdefault(
            str(post_id),
            {"user_id": set(), "sec_uid": set()},
        )
        if user_id:
            values["user_id"].add(user_id)
        if sec_uid:
            values["sec_uid"].add(sec_uid)

    for post_id, values in post_identity_values.items():
        if len(values["user_id"]) > 1 or len(values["sec_uid"]) > 1:
            raise CreatorIdentityConflictError(
                f"TikTok post {post_id} has conflicting stable creator identities"
            )
        observed_user_id = next(iter(values["user_id"]), "")
        observed_sec_uid = next(iter(values["sec_uid"]), "")
        stored = conn.execute(
            f"""
            SELECT creator_user_id, creator_sec_uid
            FROM {q("tiktok_master_posts")}
            WHERE post_id=?
            """,
            (post_id,),
        ).fetchone()
        if stored is None:
            continue
        user_id = _consistent_creator_identifier(
            (stored[0], observed_user_id),
            field="user ID",
        )
        sec_uid = _consistent_creator_identifier(
            (stored[1], observed_sec_uid),
            field="secUid",
        )
        if user_id != str(stored[0] or "") or sec_uid != str(stored[1] or ""):
            conn.execute(
                f"""
                UPDATE {q("tiktok_master_posts")}
                SET creator_user_id=?, creator_sec_uid=?
                WHERE post_id=?
                """,
                (user_id, sec_uid, post_id),
            )

    creator_key_rows = conn.execute(
        f"""
        SELECT post_id, creator_handle, canonical_url
        FROM {q("tiktok_master_posts")}
        WHERE creator_key=''
        """
    ).fetchall()
    for post_id, creator_handle, canonical_url in creator_key_rows:
        creator_key = _creator_key(creator_handle, canonical_url)
        if creator_key:
            conn.execute(
                f"""
                UPDATE {q("tiktok_master_posts")}
                SET creator_key=?
                WHERE post_id=? AND creator_key=''
                """,
                (creator_key, str(post_id)),
            )

    showcase_attempt_columns = _columns(
        conn,
        schema,
        "tiktok_master_showcase_attempts",
    )
    showcase_attempt_additions = {
        "publish_id": "TEXT NOT NULL DEFAULT ''",
        "publish_id_checkpoint_at": "TEXT NOT NULL DEFAULT ''",
    }
    for name, definition in showcase_attempt_additions.items():
        if name not in showcase_attempt_columns:
            conn.execute(
                f"ALTER TABLE "
                f'{_table(schema, "tiktok_master_showcase_attempts")} '
                f'ADD COLUMN "{name}" {definition}'
            )

    indexes = [
        (
            "idx_tiktok_master_runs_status",
            "tiktok_master_runs",
            "workflow, status, created_at",
        ),
        (
            "idx_tiktok_master_runs_creator",
            "tiktok_master_runs",
            "source_mode, creator_handle COLLATE NOCASE, created_at",
        ),
        (
            "idx_tiktok_master_audit_reports_hash",
            "tiktok_master_audit_reports",
            "analysis_set_hash, report_hash",
        ),
        (
            "idx_tiktok_master_snapshots_post",
            "tiktok_master_snapshots",
            "post_id, observed_at",
        ),
        (
            "idx_tiktok_master_posts_refresh",
            "tiktok_master_posts",
            "last_seen_at, post_id",
        ),
        (
            "idx_tiktok_master_posts_creator",
            "tiktok_master_posts",
            "creator_key, last_seen_at, post_id",
        ),
        (
            "idx_tiktok_master_posts_creator_identity",
            "tiktok_master_posts",
            "creator_user_id, creator_sec_uid, post_id",
        ),
        (
            "idx_tiktok_master_run_posts_post",
            "tiktok_master_run_posts",
            "post_id, master_run_id",
        ),
        (
            "idx_tiktok_master_topics_topic",
            "tiktok_master_post_topics",
            "topic_key, last_seen_at",
        ),
        (
            "idx_tiktok_master_comments_post",
            "tiktok_master_comments",
            "post_id, first_seen_at",
        ),
        (
            "idx_tiktok_master_attempts_target",
            "tiktok_master_comment_attempts",
            "account_key, post_id, reserved_at",
        ),
        (
            "idx_tiktok_master_targets_post",
            "tiktok_master_comment_targets",
            "post_id, account_key, state",
        ),
        (
            "idx_tiktok_master_showcase_attempts_target",
            "tiktok_master_showcase_attempts",
            "source_comment_attempt_id, reserved_at",
        ),
        (
            "idx_tiktok_master_showcase_attempts_job",
            "tiktok_master_showcase_attempts",
            "source_id, local_job_id, local_attempt_number",
        ),
        (
            "idx_tiktok_master_showcase_targets_state",
            "tiktok_master_showcase_targets",
            "state, posting_account_key, updated_at",
        ),
        (
            "idx_tiktok_master_showcase_targets_remote",
            "tiktok_master_showcase_targets",
            "remote_post_id, state",
        ),
        (
            "idx_tiktok_master_leases_expiry",
            "tiktok_master_collection_leases",
            "expires_at, post_id",
        ),
        (
            "idx_tiktok_master_music_backfill_runs_status",
            "tiktok_master_music_backfill_runs",
            "status, created_at, run_id",
        ),
        (
            "idx_tiktok_master_music_backfill_observations_post",
            "tiktok_master_music_backfill_observations",
            "post_id, music_observed_at, observation_id",
        ),
    ]
    for name, table_name, columns in indexes:
        conn.execute(
            f"""
            CREATE INDEX IF NOT EXISTS {_index(schema, name)}
            ON "{table_name}" ({columns})
            """
        )
    timestamp = now_iso()
    conn.execute(
        f"""
        INSERT INTO {q("tiktok_master_meta")} (key, value, updated_at)
        VALUES ('schema_version', ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value=excluded.value,
            updated_at=excluded.updated_at
        """,
        (MASTER_SCHEMA_VERSION, timestamp),
    )


def attach_master_database(
    conn: sqlite3.Connection,
    path: str | Path | None = None,
) -> str:
    """Attach the canonical registry and return ``main`` or ``master``."""

    target = Path(path or DEFAULT_MASTER_DATABASE).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    main_database = _main_database_path(conn)
    main_path = Path(main_database).resolve() if main_database else None
    if main_path == target:
        conn.execute("PRAGMA main.synchronous=FULL")
        ensure_master_schema(conn, "main")
        return "main"

    # SQLite guarantees crash-atomic commits across attached databases only
    # when every participating file uses a rollback journal.  ENGAGE writes
    # the project checkpoint and master snapshot in one transaction, so a
    # file-backed project connection must leave WAL before the attach.
    if main_path is not None:
        if conn.in_transaction:
            raise RuntimeError(
                "master database must be attached outside an active transaction"
            )
        main_mode = str(
            conn.execute("PRAGMA main.journal_mode=DELETE").fetchone()[0]
        ).casefold()
        if main_mode == "wal":
            raise RuntimeError(
                "project database could not enter crash-atomic rollback-journal mode"
            )
        conn.execute("PRAGMA main.synchronous=FULL")

    for row in conn.execute("PRAGMA database_list").fetchall():
        if str(row[1]) != "master":
            continue
        attached = Path(str(row[2] or "")).resolve()
        if attached != target:
            raise RuntimeError(
                f"a different master database is already attached: {attached}"
            )
        ensure_master_schema(conn, "master")
        return "master"

    conn.execute("ATTACH DATABASE ? AS master", (str(target),))
    if main_path is not None:
        master_mode = str(
            conn.execute("PRAGMA master.journal_mode=DELETE").fetchone()[0]
        ).casefold()
        if master_mode == "wal":
            conn.execute("DETACH DATABASE master")
            raise RuntimeError(
                "master database could not enter crash-atomic rollback-journal mode"
            )
    else:
        # Read-only/status helpers sometimes attach the registry to :memory:.
        # There is no cross-file transaction in that case.
        conn.execute("PRAGMA master.journal_mode=WAL")
    conn.execute("PRAGMA master.synchronous=FULL")
    conn.execute("PRAGMA busy_timeout=30000")
    ensure_master_schema(conn, "master")
    return "master"


def register_source(
    conn: sqlite3.Connection,
    schema: str,
    source_path: str | Path,
    *,
    imported: bool = False,
) -> str:
    path = _normalize_path(source_path)
    source_id = stable_id("tiktok-engage-source", path)
    timestamp = now_iso()
    conn.execute(
        f"""
        INSERT INTO {_table(schema, "tiktok_master_sources")} (
            source_id, database_path, first_seen_at, last_seen_at, imported_at
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(source_id) DO UPDATE SET
            database_path=excluded.database_path,
            last_seen_at=excluded.last_seen_at,
            imported_at=CASE
                WHEN excluded.imported_at <> '' THEN excluded.imported_at
                ELSE imported_at
            END
        """,
        (
            source_id,
            path,
            timestamp,
            timestamp,
            timestamp if imported else "",
        ),
    )
    return source_id


def _local_run_dict(
    conn: sqlite3.Connection,
    run_id: str,
) -> dict[str, Any]:
    if not _table_exists(conn, "main", "engage_tiktok_runs"):
        raise RuntimeError("source database has no engage_tiktok_runs table")
    columns = sorted(_columns(conn, "main", "engage_tiktok_runs"))
    selected = ", ".join(f'"{column}"' for column in columns)
    row = conn.execute(
        f"""
        SELECT {selected}
        FROM "main"."engage_tiktok_runs"
        WHERE run_id=?
        """,
        (run_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"source run not found: {run_id}")
    return _row_dict(row, columns)


def _local_run_creator_identity(
    conn: sqlite3.Connection,
    run_id: str,
) -> dict[str, str]:
    """Read the frozen creator-profile identity without mutating either DB."""

    run = _local_run_dict(conn, run_id)
    if str(run.get("source_mode") or "").strip().casefold() not in {
        "creator",
        "creator_profile",
    }:
        return {"handle": "", "key": "", "user_id": "", "sec_uid": ""}
    nested = _parse_json_object(run.get("creator_identity_json"))
    evidence_shape = {
        "creator": run.get("creator_handle") or nested.get("handle") or "",
        "creator_user_id": run.get("creator_user_id") or "",
        "creator_sec_uid": run.get("creator_sec_uid") or "",
        "creator_identity": nested,
    }
    return _creator_identity_from_evidence(evidence_shape)


def register_run_from_local(
    conn: sqlite3.Connection,
    schema: str,
    run_id: str,
    source_path: str | Path | None = None,
    *,
    legacy: bool = False,
) -> str:
    run = _local_run_dict(conn, run_id)
    source_path = source_path or _main_database_path(conn)
    source_id = register_source(
        conn,
        schema,
        source_path,
        imported=legacy,
    )
    master_run_id = stable_id(source_id, run_id)
    topic = str(run.get("topic") or "")
    workflow = str(run.get("workflow") or "").casefold()
    if workflow not in {"listen", "audit", "engage"}:
        workflow = "legacy_unknown" if legacy else "engage"
    values = {
        "master_run_id": master_run_id,
        "source_id": source_id,
        "local_run_id": str(run_id),
        "project": str(run.get("project") or ""),
        "workflow": workflow,
        "topic": topic,
        "topic_key": normalize_topic(topic),
        "source_mode": str(run.get("source_mode") or "topic"),
        "direct_post_url": str(run.get("direct_post_url") or ""),
        "creator_handle": str(run.get("creator_handle") or "")
        .strip()
        .lstrip("@"),
        "cardinality_mode": str(
            run.get("cardinality_mode") or "exact_count"
        ),
        "profile_inventory_count": int(
            run.get("profile_inventory_count") or 0
        ),
        "profile_inventory_terminal": int(
            run.get("profile_inventory_terminal") or 0
        ),
        "profile_inventory_hash": str(
            run.get("profile_inventory_hash") or ""
        ),
        "requested_count": int(run.get("requested_count") or 0),
        "collection_policy": str(
            run.get("collection_policy") or "new_only"
        ),
        "mode": str(run.get("mode") or "shadow"),
        "expected_account": str(run.get("expected_account") or ""),
        "observed_account": str(run.get("observed_account") or ""),
        "status": str(run.get("status") or ""),
        "requested": int(run.get("requested") or 0),
        "unique_collected": int(run.get("unique_collected") or 0),
        "evidence_ready": int(run.get("evidence_ready") or 0),
        "analyzed": int(run.get("analyzed") or 0),
        "drafted": int(run.get("drafted") or 0),
        "reviewed": int(run.get("reviewed") or 0),
        "stored": int(run.get("stored") or 0),
        "authorized": int(run.get("authorized") or 0),
        "published": int(run.get("published") or 0),
        "skipped": int(run.get("skipped") or 0),
        "failed": int(run.get("failed") or 0),
        "error": str(run.get("error") or ""),
        "created_at": str(run.get("created_at") or ""),
        "updated_at": str(run.get("updated_at") or ""),
        "synced_at": now_iso(),
    }
    columns = tuple(values)
    placeholders = ", ".join("?" for _ in columns)
    updates = ", ".join(
        f'"{column}"=excluded."{column}"'
        for column in columns
        if column not in {"master_run_id", "source_id", "local_run_id"}
    )
    conn.execute(
        f"""
        INSERT INTO {_table(schema, "tiktok_master_runs")} (
            {", ".join(f'"{column}"' for column in columns)}
        ) VALUES ({placeholders})
        ON CONFLICT(source_id, local_run_id) DO UPDATE SET {updates}
        """,
        tuple(values[column] for column in columns),
    )
    return master_run_id


def _audit_report_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        report = value
    elif isinstance(value, str) and value.strip():
        try:
            report = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("audit report JSON is invalid") from exc
    else:
        raise ValueError("audit report must be a JSON object")
    if not isinstance(report, dict):
        raise ValueError("audit report must be a JSON object")
    return report


def _matching_report_field(
    report: dict[str, Any],
    field: str,
    expected: Any,
) -> None:
    if str(report.get(field) or "") != str(expected or ""):
        raise ValueError(f"audit report {field} does not match its binding")


def store_audit_report(
    conn: sqlite3.Connection,
    schema: str,
    *,
    source_path: str | Path | None,
    run_id: str,
    schema_version: str,
    rubric_version: str,
    analysis_set_hash: str,
    report: dict[str, Any] | str,
    report_hash: str,
    generated_at: str,
    updated_at: str,
) -> dict[str, Any]:
    """Store one immutable aggregate report for a completed AUDIT run.

    The canonical report bytes are bound to the local run, rubric, and exact
    per-post analysis set.  Repeating the same write is idempotent; attempting
    to replace any of those bindings for an existing run fails closed.
    """

    run_id = str(run_id or "").strip()
    if not run_id:
        raise ValueError("audit run_id is required")
    local_run = _local_run_dict(conn, run_id)
    if str(local_run.get("workflow") or "").strip().casefold() != "audit":
        raise ValueError("aggregate audit reports require workflow=audit")

    schema_version = str(schema_version or "").strip()
    if schema_version != "tiktok-audit-report-v1":
        raise ValueError("unsupported aggregate audit report schema")
    source_mode = str(local_run.get("source_mode") or "topic").casefold()
    is_creator = source_mode in {"creator", "creator_profile"}
    expected_rubric = (
        "creator-portfolio-v1" if is_creator else "topic-portfolio-v1"
    )
    rubric_version = str(rubric_version or "").strip()
    if rubric_version != expected_rubric:
        raise ValueError(
            f"{source_mode or 'topic'} AUDIT requires rubric "
            f"{expected_rubric}"
        )

    analysis_set_hash = _required_sha256(
        analysis_set_hash,
        field="analysis_set_hash",
    )
    report_hash = _required_sha256(report_hash, field="report_hash")
    report_object = _audit_report_object(report)
    canonical_report = canonical_json(report_object)
    if json_hash(report_object) != report_hash:
        raise ValueError("audit report hash does not match canonical report JSON")

    _matching_report_field(report_object, "run_id", run_id)
    _matching_report_field(
        report_object,
        "schema_version",
        schema_version,
    )
    _matching_report_field(
        report_object,
        "rubric_version",
        rubric_version,
    )
    embedded_analysis_hash = _required_sha256(
        report_object.get("analysis_set_hash"),
        field="report analysis_set_hash",
    )
    if embedded_analysis_hash != analysis_set_hash:
        raise ValueError(
            "audit report analysis_set_hash does not match its binding"
        )

    normalized_source_mode = "creator" if is_creator else "topic"
    if str(report_object.get("source_mode") or "").casefold() != (
        normalized_source_mode
    ):
        raise ValueError("audit report source_mode does not match its run")
    if str(local_run.get("mode") or "").strip().casefold() != "shadow":
        raise ValueError("aggregate audit reports require mode=shadow")
    for scope_field in (
        "project",
        "topic",
        "collection_policy",
        "cardinality_mode",
        "mode",
    ):
        _matching_report_field(
            report_object,
            scope_field,
            local_run.get(scope_field),
        )

    requested = int(local_run.get("requested") or 0)
    evidence_ready = int(local_run.get("evidence_ready") or 0)
    analyzed = int(local_run.get("analyzed") or 0)
    if not requested == evidence_ready == analyzed:
        raise ValueError(
            "aggregate audit report requires exact completed collection and "
            "analysis counters"
        )
    try:
        report_requested = int(report_object.get("requested_count"))
        report_analyzed = int(report_object.get("analyzed_count"))
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "audit report requested_count and analyzed_count are required"
        ) from exc
    if report_requested != requested or report_analyzed != analyzed:
        raise ValueError("audit report counters do not match its run")

    if is_creator:
        if int(local_run.get("profile_inventory_terminal") or 0) != 1:
            raise ValueError(
                "creator AUDIT report requires a terminal profile inventory"
            )
        creator_handle = normalize_creator_handle(
            local_run.get("creator_handle")
        )
        if normalize_creator_handle(report_object.get("creator_handle")) != (
            creator_handle
        ):
            raise ValueError(
                "audit report creator_handle does not match its run"
            )
        _matching_report_field(
            report_object,
            "profile_inventory_hash",
            local_run.get("profile_inventory_hash"),
        )

    generated_at = canonical_timestamp(generated_at)
    updated_at = canonical_timestamp(updated_at or generated_at)
    source_path = source_path or _main_database_path(conn)
    if not str(source_path or "").strip():
        raise ValueError("source_path is required for an in-memory audit run")

    master_run_id = register_run_from_local(
        conn,
        schema,
        run_id,
        source_path,
    )
    source_id = stable_id(
        "tiktok-engage-source",
        _normalize_path(source_path),
    )
    existing = conn.execute(
        f"""
        SELECT source_id, local_run_id, schema_version, rubric_version,
               analysis_set_hash, report_hash, report_json, generated_at
        FROM {_table(schema, "tiktok_master_audit_reports")}
        WHERE master_run_id=?
        """,
        (master_run_id,),
    ).fetchone()
    expected = (
        source_id,
        run_id,
        schema_version,
        rubric_version,
        analysis_set_hash,
        report_hash,
        canonical_report,
        generated_at,
    )
    if existing is not None:
        if tuple(str(value) for value in existing) != expected:
            raise RuntimeError(
                "immutable aggregate audit report binding drift detected"
            )
        conn.execute(
            f"""
            UPDATE {_table(schema, "tiktok_master_audit_reports")}
            SET local_updated_at=?, synced_at=?
            WHERE master_run_id=?
            """,
            (updated_at, now_iso(), master_run_id),
        )
        return {
            "master_run_id": master_run_id,
            "report_hash": report_hash,
            "created": False,
        }

    conn.execute(
        f"""
        INSERT INTO {_table(schema, "tiktok_master_audit_reports")} (
            master_run_id, source_id, local_run_id, schema_version,
            rubric_version, analysis_set_hash, report_json, report_hash,
            generated_at, local_updated_at, synced_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            master_run_id,
            source_id,
            run_id,
            schema_version,
            rubric_version,
            analysis_set_hash,
            canonical_report,
            report_hash,
            generated_at,
            updated_at,
            now_iso(),
        ),
    )
    return {
        "master_run_id": master_run_id,
        "report_hash": report_hash,
        "created": True,
    }


def register_audit_report_from_local(
    conn: sqlite3.Connection,
    schema: str,
    run_id: str,
    source_path: str | Path | None = None,
) -> dict[str, Any] | None:
    """Validate and synchronize a local ``engage_tiktok_audit_reports`` row."""

    table = "engage_tiktok_audit_reports"
    if not _table_exists(conn, "main", table):
        return None
    required = {
        "run_id",
        "schema_version",
        "rubric_version",
        "analysis_set_hash",
        "report_json",
        "report_hash",
        "generated_at",
        "updated_at",
    }
    columns = _columns(conn, "main", table)
    missing = sorted(required - columns)
    if missing:
        raise RuntimeError(
            "local aggregate audit report table is missing columns: "
            + ", ".join(missing)
        )
    row = conn.execute(
        f"""
        SELECT run_id, schema_version, rubric_version, analysis_set_hash,
               report_json, report_hash, generated_at, updated_at
        FROM "main"."{table}"
        WHERE run_id=?
        """,
        (str(run_id),),
    ).fetchone()
    if row is None:
        return None
    return store_audit_report(
        conn,
        schema,
        source_path=source_path,
        run_id=str(row[0]),
        schema_version=str(row[1]),
        rubric_version=str(row[2]),
        analysis_set_hash=str(row[3]),
        report=str(row[4]),
        report_hash=str(row[5]),
        generated_at=str(row[6]),
        updated_at=str(row[7]),
    )


def audit_report_for_run(
    conn: sqlite3.Connection,
    schema: str = "main",
    *,
    master_run_id: str = "",
    run_id: str = "",
    source_path: str | Path | None = None,
) -> dict[str, Any] | None:
    """Return a verified aggregate report by master or source-local run ID."""

    master_run_id = str(master_run_id or "").strip()
    if not master_run_id:
        run_id = str(run_id or "").strip()
        if not run_id:
            raise ValueError("master_run_id or run_id is required")
        source_path = source_path or _main_database_path(conn)
        if not str(source_path or "").strip():
            raise ValueError("source_path is required with a local run_id")
        source_id = stable_id(
            "tiktok-engage-source",
            _normalize_path(source_path),
        )
        master_run_id = stable_id(source_id, run_id)
    row = conn.execute(
        f"""
        SELECT master_run_id, source_id, local_run_id, schema_version,
               rubric_version, analysis_set_hash, report_json, report_hash,
               generated_at, local_updated_at, synced_at
        FROM {_table(schema, "tiktok_master_audit_reports")}
        WHERE master_run_id=?
        """,
        (master_run_id,),
    ).fetchone()
    if row is None:
        return None
    report = _audit_report_object(row[6])
    if json_hash(report) != _required_sha256(row[7], field="report_hash"):
        raise RuntimeError("stored aggregate audit report hash mismatch")
    stored_source_id = str(row[1])
    stored_run_id = str(row[2])
    if stable_id(stored_source_id, stored_run_id) != str(row[0]):
        raise RuntimeError("stored aggregate audit report run binding mismatch")
    run_binding = conn.execute(
        f"""
        SELECT source_id, local_run_id, workflow, project, topic,
               source_mode, collection_policy, cardinality_mode, mode,
               creator_handle, profile_inventory_hash
        FROM {_table(schema, "tiktok_master_runs")}
        WHERE master_run_id=?
        """,
        (str(row[0]),),
    ).fetchone()
    if run_binding is None or tuple(str(value) for value in run_binding[:3]) != (
        stored_source_id,
        stored_run_id,
        "audit",
    ):
        raise RuntimeError("stored aggregate audit report has invalid run binding")
    try:
        _matching_report_field(report, "run_id", stored_run_id)
        _matching_report_field(report, "schema_version", str(row[3]))
        _matching_report_field(report, "rubric_version", str(row[4]))
        embedded_analysis_hash = _required_sha256(
            report.get("analysis_set_hash"),
            field="report analysis_set_hash",
        )
        for field, expected in {
            "project": run_binding[3],
            "topic": run_binding[4],
            "collection_policy": run_binding[6],
            "cardinality_mode": run_binding[7],
            "mode": run_binding[8],
        }.items():
            _matching_report_field(report, field, expected)
        bound_source_mode = str(run_binding[5]).casefold()
        _matching_report_field(
            report,
            "source_mode",
            (
                "creator"
                if bound_source_mode in {"creator", "creator_profile"}
                else "topic"
            ),
        )
        if bound_source_mode in {"creator", "creator_profile"}:
            if normalize_creator_handle(report.get("creator_handle")) != (
                normalize_creator_handle(run_binding[9])
            ):
                raise ValueError("audit report creator_handle binding mismatch")
            _matching_report_field(
                report,
                "profile_inventory_hash",
                run_binding[10],
            )
    except ValueError as exc:
        raise RuntimeError(
            "stored aggregate audit report binding mismatch"
        ) from exc
    if embedded_analysis_hash != str(row[5]):
        raise RuntimeError("stored aggregate audit report binding mismatch")
    return {
        "master_run_id": str(row[0]),
        "source_id": stored_source_id,
        "run_id": stored_run_id,
        "schema_version": str(row[3]),
        "rubric_version": str(row[4]),
        "analysis_set_hash": str(row[5]),
        "report": report,
        "report_hash": str(row[7]),
        "generated_at": str(row[8]),
        "updated_at": str(row[9]),
        "synced_at": str(row[10]),
    }


def known_post_ids(
    conn: sqlite3.Connection,
    schema: str = "main",
) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            f"""
            SELECT post_id
            FROM {_table(schema, "tiktok_master_posts")}
            UNION
            SELECT remote_post_id
            FROM {_table(schema, "tiktok_master_showcase_targets")}
            WHERE state='confirmed' AND remote_post_id <> ''
            """
        ).fetchall()
        if str(row[0] or "")
    }


def known_creator_post_ids(
    conn: sqlite3.Connection,
    schema: str = "main",
    *,
    creator_handle: str,
) -> set[str]:
    """Return master-registry posts owned by exactly one creator.

    This is creator inventory, not publication history.  Use
    :func:`confirmed_comment_post_ids` separately when deciding which posts a
    particular logged-in account has already commented on.
    """

    creator_key = normalize_creator_handle(creator_handle)
    if not creator_key:
        raise ValueError("a valid TikTok creator handle is required")
    return {
        str(row[0])
        for row in conn.execute(
            f"""
            SELECT post_id
            FROM {_table(schema, "tiktok_master_posts")}
            WHERE creator_key=?
            """,
            (creator_key,),
        ).fetchall()
    }


def confirmed_comment_post_ids(
    conn: sqlite3.Connection,
    schema: str = "main",
    *,
    account: str,
    post_ids: Sequence[str] = (),
) -> set[str]:
    """Return posts with confirmed comments for a verified posting account.

    Legacy ``*`` account records are included because they intentionally fence
    every account.  Active, submit-intent, and uncertain attempts are not
    reported as confirmed here; :func:`comment_target_guard` remains the
    authoritative fail-closed publication guard for those states.
    """

    account_key = normalize_account(account)
    if account_key == "*":
        raise ValueError("a verified TikTok posting account is required")
    normalized_ids = list(
        dict.fromkeys(str(value or "").strip() for value in post_ids)
    )
    normalized_ids = [value for value in normalized_ids if value]
    conditions = ["state='confirmed'", "account_key IN (?, '*')"]
    parameters: list[Any] = [account_key]
    if normalized_ids:
        placeholders = ", ".join("?" for _ in normalized_ids)
        conditions.append(f"post_id IN ({placeholders})")
        parameters.extend(normalized_ids)
    return {
        str(row[0])
        for row in conn.execute(
            f"""
            SELECT post_id
            FROM {_table(schema, "tiktok_master_comment_targets")}
            WHERE {" AND ".join(conditions)}
            """,
            tuple(parameters),
        ).fetchall()
    }


def blocked_comment_post_ids(
    conn: sqlite3.Connection,
    schema: str = "main",
    *,
    account: str,
    creator_handle: str = "",
) -> set[str]:
    """Return account/post targets blocked by the global publication fence.

    This is the bulk, read-only equivalent of :func:`comment_target_guard`.
    Supplying a creator restricts the result to known posts whose normalized
    owner is that exact creator.  Legacy wildcard-account targets remain
    blocking for every verified account.
    """

    account_key = normalize_account(account)
    if account_key == "*":
        raise ValueError("a verified TikTok posting account is required")
    raw_creator_handle = str(creator_handle or "").strip()
    creator_key = normalize_creator_handle(raw_creator_handle)
    if raw_creator_handle and not creator_key:
        raise ValueError("a valid TikTok creator handle is required")
    states = sorted(BLOCKING_COMMENT_STATES)
    state_placeholders = ", ".join("?" for _ in states)
    parameters: list[Any] = [*states, account_key]
    query = f"""
        SELECT t.post_id
        FROM {_table(schema, "tiktok_master_comment_targets")} t
    """
    conditions = [
        f"t.state IN ({state_placeholders})",
        "t.account_key IN (?, '*')",
    ]
    if creator_key:
        query += (
            f" JOIN {_table(schema, 'tiktok_master_posts')} p"
            " ON p.post_id=t.post_id"
        )
        conditions.append("p.creator_key=?")
        parameters.append(creator_key)
    query += " WHERE " + " AND ".join(conditions)
    return {
        str(row[0])
        for row in conn.execute(query, tuple(parameters)).fetchall()
    }


def _compact_comment(
    value: Any,
    *,
    parent_key: str = "",
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    if not isinstance(value, dict):
        return None, []
    user = value.get("user") if isinstance(value.get("user"), dict) else {}
    platform_id = str(
        value.get("cid")
        or value.get("comment_id")
        or value.get("id")
        or ""
    ).strip()
    text = str(value.get("text") or value.get("comment_text") or "").strip()
    author = str(
        user.get("unique_id")
        or value.get("username")
        or value.get("author_handle")
        or ""
    ).strip().lstrip("@")
    display = str(
        user.get("nickname")
        or value.get("nickname")
        or value.get("author_display_name")
        or ""
    ).strip()
    created = str(value.get("create_time") or value.get("created_at") or "")
    comment_key = (
        f"id:{platform_id}"
        if platform_id
        else "synthetic:"
        + stable_id(parent_key, author.casefold(), created, text, length=40)
    )
    current = {
        "comment_key": comment_key,
        "platform_comment_id": platform_id,
        "parent_comment_key": parent_key,
        "is_reply": bool(parent_key),
        "author_handle": author,
        "author_display_name": display,
        "comment_text": text,
        "text_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "platform_created_at": created,
        "likes": (
            value.get("digg_count")
            if value.get("digg_count") is not None
            else value.get("like_count")
        ),
        "creator_liked": value.get("is_author_digged") is True,
        "creator_pinned": value.get("author_pin") is True,
        "reported_reply_count": (
            value.get("reply_comment_total")
            if value.get("reply_comment_total") is not None
            else value.get("reply_count")
        ),
    }
    replies_value = (
        value.get("reply_comment")
        if isinstance(value.get("reply_comment"), list)
        else value.get("replies")
    )
    replies_value = replies_value if isinstance(replies_value, list) else []
    descendants: list[dict[str, Any]] = []
    for reply in replies_value:
        child, nested = _compact_comment(reply, parent_key=comment_key)
        if child:
            descendants.append(child)
        descendants.extend(nested)
    return current, descendants


def flatten_comments(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    comments = (
        evidence.get("comments")
        if isinstance(evidence.get("comments"), list)
        else []
    )
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in comments:
        current, descendants = _compact_comment(value)
        for comment in ([current] if current else []) + descendants:
            key = str(comment["comment_key"])
            if key not in seen:
                seen.add(key)
                output.append(comment)
    return sorted(output, key=lambda item: str(item["comment_key"]))


def evidence_content_state(evidence: dict[str, Any]) -> dict[str, Any]:
    """Return the topic/time-neutral public state used for change detection."""

    comments = flatten_comments(evidence)
    creator_identity = _creator_identity_from_evidence(evidence)
    return {
        "post_id": str(evidence.get("post_id") or ""),
        "url": str(evidence.get("url") or ""),
        "creator": str(evidence.get("creator") or ""),
        "creator_user_id": creator_identity["user_id"],
        "creator_sec_uid": creator_identity["sec_uid"],
        "creator_display_name": str(
            evidence.get("creator_display_name") or ""
        ),
        "caption": str(evidence.get("caption") or ""),
        "published_at": evidence.get("published_at"),
        "metrics": (
            evidence.get("metrics")
            if isinstance(evidence.get("metrics"), dict)
            else {}
        ),
        "metric_availability": (
            evidence.get("metric_availability")
            if isinstance(evidence.get("metric_availability"), dict)
            else {}
        ),
        "post_duration_ms": evidence.get("post_duration_ms"),
        "music_evidence": (
            evidence.get("music_evidence")
            if isinstance(evidence.get("music_evidence"), dict)
            else {}
        ),
        "transcript": str(evidence.get("transcript") or ""),
        "transcript_status": str(evidence.get("transcript_status") or ""),
        "transcript_language": str(
            evidence.get("transcript_language") or ""
        ),
        "transcript_segments": (
            evidence.get("transcript_segments")
            if isinstance(evidence.get("transcript_segments"), list)
            else []
        ),
        "subtitle_tracks": (
            evidence.get("subtitle_tracks")
            if isinstance(evidence.get("subtitle_tracks"), list)
            else []
        ),
        "comments_status": (
            evidence.get("comments_status")
            if isinstance(evidence.get("comments_status"), dict)
            else {}
        ),
        "comments": comments,
        "data_availability": (
            evidence.get("data_availability")
            if isinstance(evidence.get("data_availability"), dict)
            else {}
        ),
    }


def _numeric_metric_delta(
    previous: dict[str, Any],
    current: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    old_metrics = (
        previous.get("metrics")
        if isinstance(previous.get("metrics"), dict)
        else {}
    )
    new_metrics = (
        current.get("metrics")
        if isinstance(current.get("metrics"), dict)
        else {}
    )
    output: dict[str, dict[str, Any]] = {}
    for key in sorted(set(old_metrics) | set(new_metrics)):
        old = old_metrics.get(key)
        new = new_metrics.get(key)
        if old == new:
            continue
        delta: Any = None
        if (
            isinstance(old, (int, float))
            and not isinstance(old, bool)
            and isinstance(new, (int, float))
            and not isinstance(new, bool)
        ):
            delta = new - old
        output[key] = {"before": old, "after": new, "delta": delta}
    return output


def _snapshot_change_summary(
    previous_evidence: dict[str, Any],
    current_evidence: dict[str, Any],
    *,
    previous_snapshot_id: str,
) -> dict[str, Any]:
    """Explain every component that can change the snapshot content hash."""

    previous_state = (
        evidence_content_state(previous_evidence)
        if previous_evidence
        else {}
    )
    current_state = evidence_content_state(current_evidence)
    previous_comments = {
        str(item["comment_key"]): item
        for item in flatten_comments(previous_evidence)
    }
    current_comments = {
        str(item["comment_key"]): item
        for item in flatten_comments(current_evidence)
    }
    new_comment_keys = sorted(current_comments.keys() - previous_comments.keys())
    removed_comment_keys = sorted(
        previous_comments.keys() - current_comments.keys()
    )
    previous_comments_status = (
        previous_evidence.get("comments_status")
        if isinstance(previous_evidence.get("comments_status"), dict)
        else {}
    )
    current_comments_status = (
        current_evidence.get("comments_status")
        if isinstance(current_evidence.get("comments_status"), dict)
        else {}
    )
    removed_comment_interpretation = (
        "removed_from_complete_snapshot"
        if (
            removed_comment_keys
            and previous_comments_status.get("complete") is True
            and current_comments_status.get("complete") is True
        )
        else (
            "not_observed_in_current_snapshot"
            if removed_comment_keys
            else ""
        )
    )
    changed_comment_keys: list[str] = []
    changed_comment_fields: dict[str, list[str]] = {}
    for key in sorted(current_comments.keys() & previous_comments.keys()):
        before = previous_comments[key]
        after = current_comments[key]
        changed_fields = sorted(
            field
            for field in set(before) | set(after)
            if before.get(field) != after.get(field)
        )
        if changed_fields:
            changed_comment_keys.append(key)
            changed_comment_fields[key] = changed_fields

    metric_changes = _numeric_metric_delta(
        previous_evidence,
        current_evidence,
    )
    component_keys = sorted(
        (set(previous_state) | set(current_state))
        - {"post_id", "metrics", "comments"}
    )
    changed_fields = (
        [
            key
            for key in component_keys
            if previous_state.get(key) != current_state.get(key)
        ]
        if previous_evidence
        else []
    )
    is_first = not previous_evidence
    is_changed = (
        is_first
        or json_hash(previous_state) != json_hash(current_state)
    )
    changed_components: list[str] = []
    if changed_fields:
        changed_components.extend(changed_fields)
    if metric_changes:
        changed_components.append("metrics")
    if new_comment_keys:
        changed_components.append("comments_added")
    if changed_comment_keys:
        changed_components.append("comments_changed")
    if removed_comment_keys:
        changed_components.append(
            "comments_removed"
            if removed_comment_interpretation
            == "removed_from_complete_snapshot"
            else "comments_not_observed"
        )
    if is_changed and not is_first and not changed_components:
        # This should only be reachable for a newly introduced state field.
        # Preserve an explicit explanation instead of reporting an opaque
        # content-hash change.
        changed_components.append("other_content_state")

    return {
        "schema_version": "tiktok-master-change-v2",
        "previous_snapshot_id": previous_snapshot_id,
        "is_first_observation": is_first,
        "is_changed": is_changed,
        "changed_components": changed_components,
        "changed_fields": changed_fields,
        "metric_changes": metric_changes,
        "new_comment_count": len(new_comment_keys),
        "new_comment_keys": new_comment_keys,
        "changed_comment_count": len(changed_comment_keys),
        "changed_comment_keys": changed_comment_keys,
        "changed_comment_fields": changed_comment_fields,
        "removed_comment_count": len(removed_comment_keys),
        "removed_comment_keys": removed_comment_keys,
        "removed_comment_interpretation": removed_comment_interpretation,
        "total_comment_count": len(current_comments),
    }


def _ordered_post_snapshots(
    conn: sqlite3.Connection,
    schema: str,
    post_id: str,
) -> list[dict[str, Any]]:
    columns = (
        "snapshot_id",
        "post_id",
        "source_id",
        "master_run_id",
        "local_run_id",
        "observed_at",
        "evidence_hash",
        "content_hash",
        "creator_user_id",
        "creator_sec_uid",
        "evidence_json",
        "created_at",
    )
    rows = conn.execute(
        f"""
        SELECT {", ".join(columns)}
        FROM {_table(schema, "tiktok_master_snapshots")}
        WHERE post_id=?
        ORDER BY julianday(observed_at), observed_at, snapshot_id
        """,
        (post_id,),
    ).fetchall()
    return [_row_dict(row, columns) for row in rows]


def _snapshot_creator_identity(snapshot: dict[str, Any]) -> dict[str, str]:
    evidence = _parse_json_object(snapshot.get("evidence_json"))
    identity = _creator_identity_from_evidence(evidence)
    column_identity = {
        "handle": "",
        "key": "",
        "user_id": str(snapshot.get("creator_user_id") or ""),
        "sec_uid": str(snapshot.get("creator_sec_uid") or ""),
    }
    return _merge_creator_identity(
        identity,
        column_identity,
        post_id=str(snapshot.get("post_id") or evidence.get("post_id") or ""),
    )


def _resolve_snapshot_creator_history(
    snapshots: Sequence[dict[str, Any]],
    *,
    post_id: str,
) -> dict[str, str]:
    ordered = sorted(
        snapshots,
        key=lambda snapshot: (
            canonical_timestamp(snapshot.get("observed_at")),
            str(snapshot.get("created_at") or ""),
            str(snapshot.get("snapshot_id") or ""),
        ),
    )
    identity = {"handle": "", "key": "", "user_id": "", "sec_uid": ""}
    for snapshot in ordered:
        identity = _merge_creator_identity(
            identity,
            _snapshot_creator_identity(snapshot),
            post_id=post_id,
        )
    return identity


def _rebuild_post_rollups(
    conn: sqlite3.Connection,
    schema: str,
    post_id: str,
) -> dict[str, dict[str, Any]]:
    """Rebuild chronological deltas and post/comment rollups after a backfill."""

    snapshots = _ordered_post_snapshots(conn, schema, post_id)
    if not snapshots:
        return {}

    changes: dict[str, dict[str, Any]] = {}
    previous_evidence: dict[str, Any] = {}
    previous_snapshot_id = ""
    for snapshot in snapshots:
        evidence = _parse_json_object(snapshot["evidence_json"])
        state = evidence_content_state(evidence)
        content_hash = json_hash(state)
        change = _snapshot_change_summary(
            previous_evidence,
            evidence,
            previous_snapshot_id=previous_snapshot_id,
        )
        conn.execute(
            f"""
            UPDATE {_table(schema, "tiktok_master_snapshots")} SET
                content_hash=?, previous_snapshot_id=?, is_changed=?,
                new_comment_count=?, changed_comment_count=?,
                total_comment_count=?, change_json=?
            WHERE snapshot_id=?
            """,
            (
                content_hash,
                previous_snapshot_id,
                1 if change["is_changed"] else 0,
                int(change["new_comment_count"]),
                int(change["changed_comment_count"]),
                int(change["total_comment_count"]),
                canonical_json(change),
                snapshot["snapshot_id"],
            ),
        )
        snapshot["content_hash"] = content_hash
        changes[str(snapshot["snapshot_id"])] = change
        previous_evidence = evidence
        previous_snapshot_id = str(snapshot["snapshot_id"])

    conn.execute(
        f"""
        DELETE FROM {_table(schema, "tiktok_master_comments")}
        WHERE post_id=?
        """,
        (post_id,),
    )
    for snapshot in snapshots:
        evidence = _parse_json_object(snapshot["evidence_json"])
        observed_at = str(snapshot["observed_at"])
        snapshot_id = str(snapshot["snapshot_id"])
        for comment in flatten_comments(evidence):
            conn.execute(
                f"""
                INSERT INTO {_table(schema, "tiktok_master_comments")} (
                    post_id, comment_key, platform_comment_id,
                    parent_comment_key, is_reply, author_handle,
                    author_display_name, comment_text, text_hash,
                    platform_created_at, first_snapshot_id, last_snapshot_id,
                    first_seen_at, last_seen_at, seen_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(post_id, comment_key) DO UPDATE SET
                    platform_comment_id=CASE
                        WHEN excluded.platform_comment_id <> ''
                        THEN excluded.platform_comment_id
                        ELSE platform_comment_id END,
                    parent_comment_key=excluded.parent_comment_key,
                    is_reply=excluded.is_reply,
                    author_handle=excluded.author_handle,
                    author_display_name=excluded.author_display_name,
                    comment_text=excluded.comment_text,
                    text_hash=excluded.text_hash,
                    platform_created_at=CASE
                        WHEN excluded.platform_created_at <> ''
                        THEN excluded.platform_created_at
                        ELSE platform_created_at END,
                    last_snapshot_id=excluded.last_snapshot_id,
                    last_seen_at=excluded.last_seen_at,
                    seen_count=seen_count+1
                """,
                (
                    post_id,
                    comment["comment_key"],
                    comment["platform_comment_id"],
                    comment["parent_comment_key"],
                    1 if comment["is_reply"] else 0,
                    comment["author_handle"],
                    comment["author_display_name"],
                    comment["comment_text"],
                    comment["text_hash"],
                    comment["platform_created_at"],
                    snapshot_id,
                    snapshot_id,
                    observed_at,
                    observed_at,
                ),
            )

    first = snapshots[0]
    latest = snapshots[-1]
    evidence_history = [
        _parse_json_object(snapshot["evidence_json"])
        for snapshot in snapshots
    ]

    def latest_nonempty(field: str) -> str:
        for evidence in reversed(evidence_history):
            value = str(evidence.get(field) or "").strip()
            if value:
                return value
        return ""

    run_count = int(
        conn.execute(
            f"""
            SELECT COUNT(*)
            FROM {_table(schema, "tiktok_master_run_posts")}
            WHERE post_id=?
            """,
            (post_id,),
        ).fetchone()[0]
    )
    known_comment_count = int(
        conn.execute(
            f"""
            SELECT COUNT(*)
            FROM {_table(schema, "tiktok_master_comments")}
            WHERE post_id=?
            """,
            (post_id,),
        ).fetchone()[0]
    )
    last_change_at = ""
    for snapshot in snapshots:
        if changes[str(snapshot["snapshot_id"])]["is_changed"]:
            last_change_at = str(snapshot["observed_at"])
    timestamp = now_iso()
    canonical_url = latest_nonempty("url")
    creator_identity = _resolve_snapshot_creator_history(
        snapshots,
        post_id=post_id,
    )
    creator_handle = creator_identity["handle"]
    creator_key = creator_identity["key"]
    conn.execute(
        f"""
        UPDATE {_table(schema, "tiktok_master_posts")} SET
            canonical_url=?,
            creator_handle=?,
            creator_key=?,
            creator_user_id=?,
            creator_sec_uid=?,
            creator_display_name=?,
            first_source_id=?,
            first_run_id=?,
            first_seen_at=?,
            last_source_id=?,
            last_run_id=?,
            last_seen_at=?,
            latest_snapshot_id=?,
            latest_evidence_hash=?,
            latest_content_hash=?,
            latest_evidence_json=?,
            snapshot_count=?,
            run_count=?,
            known_comment_count=?,
            last_change_at=?,
            updated_at=?
        WHERE post_id=?
        """,
        (
            canonical_url,
            creator_handle,
            creator_key,
            creator_identity["user_id"],
            creator_identity["sec_uid"],
            latest_nonempty("creator_display_name"),
            first["source_id"],
            first["local_run_id"],
            first["observed_at"],
            latest["source_id"],
            latest["local_run_id"],
            latest["observed_at"],
            latest["snapshot_id"],
            latest["evidence_hash"],
            latest["content_hash"],
            "{}",
            len(snapshots),
            run_count,
            known_comment_count,
            last_change_at,
            timestamp,
            post_id,
        ),
    )
    return changes


def record_evidence_snapshot(
    conn: sqlite3.Connection,
    schema: str,
    *,
    source_path: str | Path,
    run_id: str,
    post_id: str,
    evidence: dict[str, Any],
    evidence_hash: str,
    account: str = "",
) -> dict[str, Any]:
    """Idempotently append one evidence-ready observation to the master."""

    if not isinstance(evidence, dict):
        raise ValueError("evidence must be a JSON object")
    post_id = str(post_id or evidence.get("post_id") or "").strip()
    if not post_id:
        raise ValueError("post_id is required")
    if str(evidence.get("post_id") or post_id) != post_id:
        raise ValueError("evidence post_id does not match")
    if evidence.get("evidence_ready") is not True:
        raise ValueError("only evidence-ready snapshots enter the master")

    observed_at = canonical_timestamp(
        evidence.get("observed_at"),
        fallback_now=True,
    )
    incoming_identity = _creator_identity_from_evidence(evidence)
    if not incoming_identity["key"]:
        raise ValueError("evidence requires a canonical TikTok creator identity")
    run_identity = _local_run_creator_identity(conn, run_id)
    incoming_identity = _merge_creator_identity(
        incoming_identity,
        run_identity,
        post_id=post_id,
    )
    existing_identity_snapshots = _ordered_post_snapshots(
        conn,
        schema,
        post_id,
    )
    pending_identity_snapshot = {
        "snapshot_id": "~pending",
        "post_id": post_id,
        "observed_at": observed_at,
        "created_at": now_iso(),
        "creator_user_id": incoming_identity["user_id"],
        "creator_sec_uid": incoming_identity["sec_uid"],
        "evidence_json": canonical_json(evidence),
    }
    resolved_creator_identity = _resolve_snapshot_creator_history(
        [*existing_identity_snapshots, pending_identity_snapshot],
        post_id=post_id,
    )
    stored_owner = conn.execute(
        f"""
        SELECT creator_handle, creator_key,
               creator_user_id, creator_sec_uid
        FROM {_table(schema, "tiktok_master_posts")}
        WHERE post_id=?
        """,
        (post_id,),
    ).fetchone()
    if stored_owner is not None:
        resolved_creator_identity = _merge_creator_identity(
            {
                "handle": str(stored_owner[0] or ""),
                "key": str(stored_owner[1] or ""),
                "user_id": str(stored_owner[2] or ""),
                "sec_uid": str(stored_owner[3] or ""),
            },
            resolved_creator_identity,
            post_id=post_id,
        )

    master_run_id = register_run_from_local(
        conn,
        schema,
        run_id,
        source_path,
    )
    source_id = register_source(conn, schema, source_path)
    existing_snapshot = conn.execute(
        f"""
        SELECT *
        FROM {_table(schema, "tiktok_master_snapshots")}
        WHERE source_id=? AND local_run_id=? AND post_id=?
        """,
        (source_id, run_id, post_id),
    ).fetchone()
    if existing_snapshot is not None:
        keys = (
            existing_snapshot.keys()
            if isinstance(existing_snapshot, sqlite3.Row)
            else [
                item[1]
                for item in conn.execute(
                    f'PRAGMA "{_schema(schema)}".table_info('
                    '"tiktok_master_snapshots")'
                ).fetchall()
            ]
        )
        return {
            **_row_dict(existing_snapshot, list(keys)),
            "created": False,
        }

    state = evidence_content_state(evidence)
    content_hash = json_hash(state)
    previous = conn.execute(
        f"""
        SELECT snapshot_id, content_hash, evidence_json
        FROM {_table(schema, "tiktok_master_snapshots")}
        WHERE post_id=?
        ORDER BY observed_at DESC, created_at DESC
        LIMIT 1
        """,
        (post_id,),
    ).fetchone()
    previous_snapshot_id = str(previous[0] or "") if previous else ""
    previous_content_hash = str(previous[1] or "") if previous else ""
    previous_evidence = (
        _parse_json_object(previous[2]) if previous else {}
    )
    comments = flatten_comments(evidence)
    comment_keys = [str(item["comment_key"]) for item in comments]
    existing_comments: dict[str, str] = {}
    if comment_keys:
        placeholders = ", ".join("?" for _ in comment_keys)
        existing_comments = {
            str(row[0]): str(row[1] or "")
            for row in conn.execute(
                f"""
                SELECT comment_key, text_hash
                FROM {_table(schema, "tiktok_master_comments")}
                WHERE post_id=? AND comment_key IN ({placeholders})
                """,
                (post_id, *comment_keys),
            ).fetchall()
        }
    new_comments = [
        item for item in comments if item["comment_key"] not in existing_comments
    ]
    changed_comments = [
        item
        for item in comments
        if item["comment_key"] in existing_comments
        and item["text_hash"] != existing_comments[item["comment_key"]]
    ]
    changed_fields = [
        key
        for key in (
            "url",
            "creator",
            "creator_display_name",
            "caption",
            "published_at",
            "transcript",
            "transcript_status",
            "transcript_language",
        )
        if previous_evidence
        and evidence_content_state(previous_evidence).get(key) != state.get(key)
    ]
    is_changed = not previous or previous_content_hash != content_hash
    change = _snapshot_change_summary(
        previous_evidence,
        evidence,
        previous_snapshot_id=previous_snapshot_id,
    )
    snapshot_id = stable_id(
        source_id,
        run_id,
        post_id,
        evidence_hash,
        observed_at,
    )
    timestamp = now_iso()
    conn.execute(
        f"""
        INSERT INTO {_table(schema, "tiktok_master_snapshots")} (
            snapshot_id, post_id, source_id, master_run_id, local_run_id,
            account_key, observed_at, evidence_hash, content_hash,
            creator_user_id, creator_sec_uid,
            previous_snapshot_id, is_changed, new_comment_count,
            changed_comment_count, total_comment_count, change_json,
            evidence_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            snapshot_id,
            post_id,
            source_id,
            master_run_id,
            run_id,
            normalize_account(account),
            observed_at,
            evidence_hash,
            content_hash,
            incoming_identity["user_id"],
            incoming_identity["sec_uid"],
            previous_snapshot_id,
            1 if is_changed else 0,
            int(change["new_comment_count"]),
            int(change["changed_comment_count"]),
            int(change["total_comment_count"]),
            canonical_json(change),
            canonical_json(evidence),
            timestamp,
        ),
    )

    run_policy_row = conn.execute(
        f"""
        SELECT collection_policy, topic, topic_key
        FROM {_table(schema, "tiktok_master_runs")}
        WHERE master_run_id=?
        """,
        (master_run_id,),
    ).fetchone()
    collection_policy = (
        str(run_policy_row[0] or "new_only")
        if run_policy_row
        else "new_only"
    )
    position = int(
        conn.execute(
            f"""
            SELECT COUNT(*)
            FROM {_table(schema, "tiktok_master_run_posts")}
            WHERE master_run_id=?
            """,
            (master_run_id,),
        ).fetchone()[0]
    ) + 1
    relation_inserted = conn.execute(
        f"""
        INSERT OR IGNORE INTO {_table(schema, "tiktok_master_run_posts")} (
            master_run_id, post_id, snapshot_id, collection_policy,
            position, first_linked_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            master_run_id,
            post_id,
            snapshot_id,
            collection_policy,
            position,
            timestamp,
        ),
    ).rowcount

    canonical_url = str(evidence.get("url") or "")
    creator = resolved_creator_identity["handle"]
    creator_key = resolved_creator_identity["key"]
    creator_user_id = resolved_creator_identity["user_id"]
    creator_sec_uid = resolved_creator_identity["sec_uid"]
    creator_display = str(evidence.get("creator_display_name") or "")
    post_row = conn.execute(
        f"""
        SELECT last_seen_at
        FROM {_table(schema, "tiktok_master_posts")}
        WHERE post_id=?
        """,
        (post_id,),
    ).fetchone()
    if post_row:
        is_latest = observed_at >= str(post_row[0] or "")
        conn.execute(
            f"""
            UPDATE {_table(schema, "tiktok_master_posts")} SET
                snapshot_count=snapshot_count+1,
                run_count=run_count+?,
                creator_user_id=CASE
                    WHEN ? <> '' THEN ? ELSE creator_user_id END,
                creator_sec_uid=CASE
                    WHEN ? <> '' THEN ? ELSE creator_sec_uid END,
                known_comment_count=(
                    SELECT COUNT(*)
                    FROM {_table(schema, "tiktok_master_comments")}
                    WHERE post_id=?
                ),
                last_change_at=CASE
                    WHEN ? AND ? >= last_change_at THEN ?
                    ELSE last_change_at
                END,
                updated_at=?
            WHERE post_id=?
            """,
            (
                1 if relation_inserted else 0,
                creator_user_id,
                creator_user_id,
                creator_sec_uid,
                creator_sec_uid,
                post_id,
                1 if is_changed else 0,
                observed_at,
                observed_at,
                timestamp,
                post_id,
            ),
        )
        if is_latest:
            conn.execute(
                f"""
                UPDATE {_table(schema, "tiktok_master_posts")} SET
                    canonical_url=CASE
                        WHEN ? <> '' THEN ? ELSE canonical_url END,
                    creator_handle=CASE
                        WHEN ? <> '' THEN ? ELSE creator_handle END,
                    creator_key=CASE
                        WHEN ? <> '' THEN ? ELSE creator_key END,
                    creator_display_name=CASE
                        WHEN ? <> '' THEN ? ELSE creator_display_name END,
                    last_source_id=?,
                    last_run_id=?,
                    last_seen_at=?,
                    latest_snapshot_id=?,
                    latest_evidence_hash=?,
                    latest_content_hash=?,
                    latest_evidence_json=?,
                    updated_at=?
                WHERE post_id=?
                """,
                (
                    canonical_url,
                    canonical_url,
                    creator,
                    creator,
                    creator_key,
                    creator_key,
                    creator_display,
                    creator_display,
                    source_id,
                    run_id,
                    observed_at,
                    snapshot_id,
                    evidence_hash,
                    content_hash,
                    "{}",
                    timestamp,
                    post_id,
                ),
            )
    else:
        conn.execute(
            f"""
            INSERT INTO {_table(schema, "tiktok_master_posts")} (
                post_id, canonical_url, creator_handle,
                creator_key, creator_user_id, creator_sec_uid,
                creator_display_name, first_source_id, first_run_id,
                first_seen_at, last_source_id, last_run_id, last_seen_at,
                latest_snapshot_id, latest_evidence_hash,
                latest_content_hash, latest_evidence_json,
                snapshot_count, run_count, known_comment_count,
                last_change_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1, 0, ?, ?)
            """,
            (
                post_id,
                canonical_url,
                creator,
                creator_key,
                creator_user_id,
                creator_sec_uid,
                creator_display,
                source_id,
                run_id,
                observed_at,
                source_id,
                run_id,
                observed_at,
                snapshot_id,
                evidence_hash,
                content_hash,
                "{}",
                observed_at,
                timestamp,
            ),
        )

    topic = str(run_policy_row[1] or "") if run_policy_row else ""
    topic_key = str(run_policy_row[2] or "") if run_policy_row else ""
    if topic_key and relation_inserted:
        conn.execute(
            f"""
            INSERT INTO {_table(schema, "tiktok_master_post_topics")} (
                post_id, topic_key, topic, first_seen_at, last_seen_at,
                run_count
            ) VALUES (?, ?, ?, ?, ?, 1)
            ON CONFLICT(post_id, topic_key) DO UPDATE SET
                topic=excluded.topic,
                first_seen_at=CASE
                    WHEN julianday(excluded.first_seen_at)
                         < julianday(first_seen_at)
                    THEN excluded.first_seen_at ELSE first_seen_at END,
                last_seen_at=CASE
                    WHEN julianday(excluded.last_seen_at)
                         > julianday(last_seen_at)
                    THEN excluded.last_seen_at ELSE last_seen_at END,
                run_count=run_count+1
            """,
            (
                post_id,
                topic_key,
                topic,
                observed_at,
                observed_at,
            ),
        )

    for comment in comments:
        conn.execute(
            f"""
            INSERT INTO {_table(schema, "tiktok_master_comments")} (
                post_id, comment_key, platform_comment_id,
                parent_comment_key, is_reply, author_handle,
                author_display_name, comment_text, text_hash,
                platform_created_at, first_snapshot_id, last_snapshot_id,
                first_seen_at, last_seen_at, seen_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(post_id, comment_key) DO UPDATE SET
                platform_comment_id=CASE
                    WHEN excluded.platform_comment_id <> ''
                    THEN excluded.platform_comment_id
                    ELSE platform_comment_id END,
                parent_comment_key=excluded.parent_comment_key,
                is_reply=excluded.is_reply,
                author_handle=excluded.author_handle,
                author_display_name=excluded.author_display_name,
                comment_text=excluded.comment_text,
                text_hash=excluded.text_hash,
                platform_created_at=CASE
                    WHEN excluded.platform_created_at <> ''
                    THEN excluded.platform_created_at
                    ELSE platform_created_at END,
                last_snapshot_id=excluded.last_snapshot_id,
                last_seen_at=excluded.last_seen_at,
                seen_count=seen_count+1
            """,
            (
                post_id,
                comment["comment_key"],
                comment["platform_comment_id"],
                comment["parent_comment_key"],
                1 if comment["is_reply"] else 0,
                comment["author_handle"],
                comment["author_display_name"],
                comment["comment_text"],
                comment["text_hash"],
                comment["platform_created_at"],
                snapshot_id,
                snapshot_id,
                observed_at,
                observed_at,
            ),
        )
    conn.execute(
        f"""
        UPDATE {_table(schema, "tiktok_master_posts")}
        SET known_comment_count=(
            SELECT COUNT(*)
            FROM {_table(schema, "tiktok_master_comments")}
            WHERE post_id=?
        ), updated_at=?
        WHERE post_id=?
        """,
        (post_id, timestamp, post_id),
    )
    rebuilt_changes = _rebuild_post_rollups(
        conn,
        schema,
        post_id,
    )
    change = rebuilt_changes[snapshot_id]
    snapshot_row = conn.execute(
        f"""
        SELECT content_hash, previous_snapshot_id, is_changed,
               new_comment_count, changed_comment_count,
               total_comment_count
        FROM {_table(schema, "tiktok_master_snapshots")}
        WHERE snapshot_id=?
        """,
        (snapshot_id,),
    ).fetchone()
    return {
        "created": True,
        "snapshot_id": snapshot_id,
        "post_id": post_id,
        "content_hash": str(snapshot_row[0]),
        "evidence_hash": evidence_hash,
        "previous_snapshot_id": str(snapshot_row[1] or ""),
        "is_changed": bool(snapshot_row[2]),
        "new_comment_count": int(snapshot_row[3]),
        "changed_comment_count": int(snapshot_row[4]),
        "total_comment_count": int(snapshot_row[5]),
        "change": change,
    }


def select_refresh_candidates(
    conn: sqlite3.Connection,
    schema: str = "main",
    *,
    topic: str = "",
    creator_handle: str = "",
    post_ids: Sequence[str] = (),
    stale_before: str = "",
) -> list[dict[str, Any]]:
    """Return every eligible known record, oldest observation first.

    No SQL LIMIT is applied.  The collector stops after the exact requested
    count and can therefore use later rows as replacements for unavailable
    posts.  Selector precedence is explicit IDs, then creator, then topic:
    creator constrains explicit IDs when both are supplied, while the legacy
    explicit-ID behavior continues to ignore topic membership.
    """

    normalized_ids = list(
        dict.fromkeys(str(value or "").strip() for value in post_ids)
    )
    normalized_ids = [value for value in normalized_ids if value]
    raw_creator_handle = str(creator_handle or "").strip()
    creator_key = normalize_creator_handle(raw_creator_handle)
    if raw_creator_handle and not creator_key:
        raise ValueError("a valid TikTok creator handle is required")
    parameters: list[Any] = []
    query = f"""
        SELECT p.post_id, p.canonical_url, p.creator_handle,
               p.creator_key, p.creator_user_id, p.creator_sec_uid,
               p.creator_display_name, p.last_seen_at,
               p.latest_snapshot_id, p.latest_evidence_hash,
               p.latest_content_hash, s.evidence_json AS latest_evidence_json
        FROM {_table(schema, "tiktok_master_posts")} p
        JOIN {_table(schema, "tiktok_master_snapshots")} s
          ON s.snapshot_id=p.latest_snapshot_id
    """
    conditions: list[str] = []
    if normalized_ids:
        placeholders = ", ".join("?" for _ in normalized_ids)
        conditions.append(f"p.post_id IN ({placeholders})")
        parameters.extend(normalized_ids)
    if creator_key:
        conditions.append("p.creator_key=?")
        parameters.append(creator_key)
    elif not normalized_ids:
        topic_key = normalize_topic(topic)
        if topic_key:
            query += (
                f" JOIN {_table(schema, 'tiktok_master_post_topics')} t"
                " ON t.post_id=p.post_id"
            )
            conditions.append("t.topic_key=?")
            parameters.append(topic_key)
    if str(stale_before or "").strip():
        conditions.append(
            "julianday(p.last_seen_at) <= julianday(?)"
        )
        parameters.append(canonical_timestamp(stale_before))
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY julianday(p.last_seen_at), p.post_id"
    rows = conn.execute(query, tuple(parameters)).fetchall()
    columns = [
        "post_id",
        "canonical_url",
        "creator_handle",
        "creator_key",
        "creator_user_id",
        "creator_sec_uid",
        "creator_display_name",
        "last_seen_at",
        "latest_snapshot_id",
        "latest_evidence_hash",
        "latest_content_hash",
        "latest_evidence_json",
    ]
    by_id = {
        str(item["post_id"]): item
        for item in (_row_dict(row, columns) for row in rows)
    }
    if normalized_ids:
        return [by_id[post_id] for post_id in normalized_ids if post_id in by_id]
    return list(by_id.values())


def _music_backfill_schema_version(value: Any) -> str:
    normalized = str(value or "").strip().casefold()
    if re.fullmatch(r"tiktok-music-evidence-v[1-9][0-9]*", normalized) is None:
        raise ValueError(
            "target_schema_version must be a TikTok music evidence schema"
        )
    return normalized


def _music_backfill_retryable_statuses(
    values: Iterable[Any],
) -> tuple[str, ...]:
    normalized = sorted(
        {
            str(value or "").strip().casefold()
            for value in values
            if str(value or "").strip()
        }
    )
    if any(
        re.fullmatch(r"[a-z][a-z0-9_]*", value) is None
        for value in normalized
    ):
        raise ValueError("retryable music statuses must be normalized identifiers")
    return tuple(normalized)


def _music_backfill_catalogs(values: Iterable[Any]) -> tuple[str, ...]:
    normalized = tuple(
        dict.fromkeys(
            str(value or "").strip().casefold()
            for value in values
            if str(value or "").strip()
        )
    )
    if not normalized:
        raise ValueError("at least one music backfill catalog is required")
    unsupported = sorted(set(normalized) - MUSIC_BACKFILL_SUPPORTED_CATALOGS)
    if unsupported:
        raise ValueError(
            "unsupported music backfill catalog: " + ", ".join(unsupported)
        )
    return normalized


def _schema_database_path(conn: sqlite3.Connection, schema: str) -> str:
    normalized_schema = _schema(schema)
    for row in conn.execute("PRAGMA database_list").fetchall():
        if str(row[1]) == normalized_schema:
            raw_path = str(row[2] or "").strip()
            return _normalize_path(raw_path) if raw_path else ""
    raise RuntimeError("master database schema is not attached")


def _music_status_values(value: Any) -> set[str]:
    statuses: set[str] = set()
    if isinstance(value, dict):
        for key, nested in value.items():
            if str(key).casefold() == "status":
                status = str(nested or "").strip().casefold()
                if status:
                    statuses.add(status)
            else:
                statuses.update(_music_status_values(nested))
    elif isinstance(value, list):
        for nested in value:
            statuses.update(_music_status_values(nested))
    return statuses


def _music_evidence_state(
    value: Any,
    *,
    target_schema_version: str,
    retryable_statuses: Sequence[str],
    observation_status: str = "",
) -> dict[str, Any]:
    document = value if isinstance(value, dict) else {}
    schema_version = str(document.get("schema_version") or "").strip().casefold()
    stored_hash = str(document.get("music_evidence_hash") or "").strip().casefold()
    unhashed = dict(document)
    unhashed.pop("music_evidence_hash", None)
    hash_valid = bool(document) and (
        re.fullmatch(r"[0-9a-f]{64}", stored_hash) is not None
        and json_hash(unhashed) == stored_hash
    )
    retryable = sorted(
        _music_status_values(document).intersection(retryable_statuses)
    )
    normalized_observation_status = str(observation_status or "").casefold()
    if (
        normalized_observation_status in retryable_statuses
        and normalized_observation_status not in retryable
    ):
        retryable.append(normalized_observation_status)
        retryable.sort()
    if normalized_observation_status == "failed":
        base_status = "failed"
    elif not document:
        base_status = "missing"
    elif not hash_valid:
        base_status = "invalid_hash"
    elif retryable:
        base_status = "retryable:" + ",".join(retryable)
    elif schema_version != target_schema_version:
        base_status = "schema_upgrade_required"
    else:
        base_status = "terminal"
    return {
        "document": document,
        "schema_version": schema_version,
        "hash_valid": hash_valid,
        "stored_hash": stored_hash,
        "retryable_statuses": retryable,
        "base_status": base_status,
        "terminal_current": bool(
            schema_version == target_schema_version
            and hash_valid
            and not retryable
            and normalized_observation_status != "failed"
        ),
    }


def _direct_tiktok_post_id(value: Any) -> str:
    url = str(value or "").strip()
    match = re.fullmatch(
        r"https://(?:www\.|m\.)?tiktok\.com/@[^/?#\s]+/"
        r"(?:video|photo)/(\d+)(?:[/?#].*)?",
        url,
        flags=re.IGNORECASE,
    )
    if match is None:
        raise ValueError("direct_url must be a canonical TikTok video or photo URL")
    return match.group(1)


def _validated_base_snapshot(
    conn: sqlite3.Connection,
    schema: str,
    *,
    post_id: str,
    snapshot_id: str,
    evidence_hash: str,
    observed_at: str,
    require_latest: bool,
) -> dict[str, Any]:
    required_hash = _required_sha256(evidence_hash, field="base_evidence_hash")
    row = conn.execute(
        f"""
        SELECT s.post_id, s.observed_at, s.evidence_hash, s.evidence_json,
               p.latest_snapshot_id, p.latest_evidence_hash,
               p.canonical_url, p.creator_handle, p.creator_key
        FROM {_table(schema, "tiktok_master_snapshots")} s
        JOIN {_table(schema, "tiktok_master_posts")} p
          ON p.post_id=s.post_id
        WHERE s.snapshot_id=? AND s.post_id=?
        """,
        (str(snapshot_id), str(post_id)),
    ).fetchone()
    if row is None:
        raise RuntimeError("music backfill base snapshot does not exist")
    stored_observed_at = canonical_timestamp(row[1])
    if stored_observed_at != canonical_timestamp(observed_at):
        raise RuntimeError("music backfill base observation timestamp mismatch")
    stored_hash = _required_sha256(row[2], field="stored base evidence_hash")
    if stored_hash != required_hash:
        raise RuntimeError("music backfill base evidence hash mismatch")
    evidence = _parse_json_object(row[3])
    if not evidence or json_hash(evidence) != stored_hash:
        raise RuntimeError("music backfill base evidence JSON/hash mismatch")
    if str(evidence.get("post_id") or "") != str(post_id):
        raise RuntimeError("music backfill base evidence post binding mismatch")
    if require_latest:
        if str(row[4] or "") != str(snapshot_id):
            raise RuntimeError("music backfill candidate is no longer the latest snapshot")
        if _required_sha256(row[5], field="latest_evidence_hash") != stored_hash:
            raise RuntimeError("latest master post evidence binding mismatch")
    return {
        "post_id": str(row[0]),
        "base_snapshot_id": str(snapshot_id),
        "base_evidence_hash": stored_hash,
        "base_observed_at": stored_observed_at,
        "evidence": evidence,
        "canonical_url": str(evidence.get("url") or row[6] or ""),
        "creator_handle": str(evidence.get("creator") or row[7] or ""),
        "creator_key": str(row[8] or ""),
    }


def select_music_backfill_candidates(
    conn: sqlite3.Connection,
    schema: str = "main",
    *,
    creator_handle: str = "",
    topic: str = "",
    post_ids: Sequence[str] = (),
    direct_url: str = "",
    target_schema_version: str = "tiktok-music-evidence-v3",
    retryable_statuses: Sequence[str] = DEFAULT_MUSIC_BACKFILL_RETRYABLE_STATUSES,
    force: bool = False,
) -> list[dict[str, Any]]:
    """Select old/retryable music records without changing master evidence.

    Exactly one optional scope may be supplied; no scope means every known
    post.  Every result is bound to the evidence JSON in the row referenced by
    ``tiktok_master_posts.latest_snapshot_id``.  The denormalized
    ``latest_evidence_json`` column is intentionally never used here.
    """

    schema = _schema(schema)
    target_schema = _music_backfill_schema_version(target_schema_version)
    retryable = _music_backfill_retryable_statuses(retryable_statuses)
    normalized_ids = list(
        dict.fromkeys(str(value or "").strip() for value in post_ids)
    )
    normalized_ids = [value for value in normalized_ids if value]
    if any(re.fullmatch(r"\d+", value) is None for value in normalized_ids):
        raise ValueError("music backfill post IDs must be numeric TikTok IDs")
    creator_key = normalize_creator_handle(creator_handle)
    if str(creator_handle or "").strip() and not creator_key:
        raise ValueError("a valid TikTok creator handle is required")
    topic_key = normalize_topic(topic)
    direct_post_id = _direct_tiktok_post_id(direct_url) if direct_url else ""
    scope_count = sum(
        bool(value)
        for value in (creator_key, topic_key, normalized_ids, direct_post_id)
    )
    if scope_count > 1:
        raise ValueError("music backfill accepts exactly one selection scope")

    query = f"""
        SELECT p.post_id, p.canonical_url, p.creator_handle, p.creator_key,
               p.latest_snapshot_id, p.latest_evidence_hash,
               s.observed_at AS base_observed_at,
               s.evidence_hash AS snapshot_evidence_hash, s.evidence_json,
               o.observation_id,
               o.music_observed_at AS prior_music_observed_at,
               o.music_evidence_json AS prior_music_evidence_json,
               o.music_evidence_hash AS prior_music_evidence_hash,
               o.status AS prior_observation_status,
               o.error AS prior_observation_error,
               o.observation_hash AS prior_observation_hash
        FROM {_table(schema, "tiktok_master_posts")} p
        JOIN {_table(schema, "tiktok_master_snapshots")} s
          ON s.snapshot_id=p.latest_snapshot_id AND s.post_id=p.post_id
        LEFT JOIN {_table(schema, "tiktok_master_music_backfill_observations")} o
          ON o.observation_id=(
              SELECT o2.observation_id
              FROM {_table(schema, "tiktok_master_music_backfill_observations")} o2
              WHERE o2.post_id=p.post_id
              ORDER BY julianday(o2.music_observed_at) DESC,
                       o2.music_observed_at DESC, o2.observation_id DESC
              LIMIT 1
          )
    """
    parameters: list[Any] = []
    conditions: list[str] = []
    if normalized_ids:
        placeholders = ", ".join("?" for _ in normalized_ids)
        conditions.append(f"p.post_id IN ({placeholders})")
        parameters.extend(normalized_ids)
    elif direct_post_id:
        conditions.append("p.post_id=?")
        parameters.append(direct_post_id)
    elif creator_key:
        conditions.append("p.creator_key=?")
        parameters.append(creator_key)
    elif topic_key:
        query += (
            f" JOIN {_table(schema, 'tiktok_master_post_topics')} t"
            " ON t.post_id=p.post_id"
        )
        conditions.append("t.topic_key=?")
        parameters.append(topic_key)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY julianday(s.observed_at), s.observed_at, p.post_id"
    rows = conn.execute(query, tuple(parameters)).fetchall()
    columns = (
        "post_id", "canonical_url", "creator_handle", "creator_key",
        "latest_snapshot_id", "latest_evidence_hash", "base_observed_at",
        "snapshot_evidence_hash", "evidence_json", "observation_id",
        "prior_music_observed_at", "prior_music_evidence_json",
        "prior_music_evidence_hash", "prior_observation_status",
        "prior_observation_error", "prior_observation_hash",
    )
    candidates: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for raw_row in rows:
        row = _row_dict(raw_row, columns)
        post_id = str(row["post_id"])
        latest_hash = _required_sha256(
            row["latest_evidence_hash"], field="latest_evidence_hash"
        )
        snapshot_hash = _required_sha256(
            row["snapshot_evidence_hash"], field="snapshot evidence_hash"
        )
        if latest_hash != snapshot_hash:
            raise RuntimeError("latest master post evidence binding mismatch")
        evidence = _parse_json_object(row["evidence_json"])
        if not evidence or json_hash(evidence) != snapshot_hash:
            raise RuntimeError("latest master snapshot evidence JSON/hash mismatch")
        if str(evidence.get("post_id") or "") != post_id:
            raise RuntimeError("latest master snapshot post binding mismatch")

        prior_observation_status = str(row["prior_observation_status"] or "")
        if row["observation_id"]:
            if prior_observation_status not in MUSIC_BACKFILL_OBSERVATION_STATUSES:
                raise RuntimeError("stored music backfill observation status is invalid")
            prior_music = _parse_json_object(row["prior_music_evidence_json"])
            prior_hash = _required_sha256(
                row["prior_music_evidence_hash"],
                field="prior music_evidence_hash",
            )
            if prior_observation_status == "failed":
                if prior_music or prior_hash != json_hash({}):
                    raise RuntimeError("stored failed music observation is invalid")
            else:
                if not prior_music:
                    raise RuntimeError("stored music observation JSON is missing")
                internal_hash = _required_sha256(
                    prior_music.get("music_evidence_hash"),
                    field="stored music_evidence music_evidence_hash",
                )
                unhashed = dict(prior_music)
                unhashed.pop("music_evidence_hash", None)
                if internal_hash != prior_hash or json_hash(unhashed) != prior_hash:
                    raise RuntimeError("stored music observation JSON/hash mismatch")
            prior_binding = {
                "run_id": "",
                "post_id": post_id,
                "base_snapshot_id": "",
                "base_evidence_hash": "",
                "base_observed_at": "",
                "music_observed_at": canonical_timestamp(
                    row["prior_music_observed_at"]
                ),
                "music_evidence_hash": prior_hash,
                "status": prior_observation_status,
                "error": str(row["prior_observation_error"] or ""),
            }
            prior_full = conn.execute(
                f"""
                SELECT run_id, base_snapshot_id, base_evidence_hash,
                       base_observed_at
                FROM {_table(schema, "tiktok_master_music_backfill_observations")}
                WHERE observation_id=?
                """,
                (str(row["observation_id"]),),
            ).fetchone()
            prior_binding.update(
                {
                    "run_id": str(prior_full[0]),
                    "base_snapshot_id": str(prior_full[1]),
                    "base_evidence_hash": str(prior_full[2]),
                    "base_observed_at": canonical_timestamp(prior_full[3]),
                }
            )
            if _required_sha256(
                row["prior_observation_hash"],
                field="prior music observation_hash",
            ) != json_hash(_music_backfill_observation_hash_document(prior_binding)):
                raise RuntimeError("stored music observation hash mismatch")
            effective_music = prior_music
        else:
            effective_music = (
                evidence.get("music_evidence")
                if isinstance(evidence.get("music_evidence"), dict)
                else {}
            )
        state = _music_evidence_state(
            effective_music,
            target_schema_version=target_schema,
            retryable_statuses=retryable,
            observation_status=prior_observation_status,
        )
        if force:
            eligibility_reason = "forced"
        elif state["terminal_current"]:
            continue
        elif prior_observation_status == "failed":
            eligibility_reason = "prior_backfill_failed"
        elif not state["document"]:
            eligibility_reason = "music_evidence_missing"
        elif not state["hash_valid"]:
            eligibility_reason = "music_evidence_hash_invalid"
        elif state["retryable_statuses"]:
            eligibility_reason = "retryable_status:" + ",".join(
                state["retryable_statuses"]
            )
        else:
            eligibility_reason = "music_schema_upgrade_required"
        candidate = {
            "post_id": post_id,
            "canonical_url": str(row["canonical_url"] or evidence.get("url") or ""),
            "creator_handle": str(
                row["creator_handle"] or evidence.get("creator") or ""
            ),
            "creator_key": str(row["creator_key"] or ""),
            "base_snapshot_id": str(row["latest_snapshot_id"]),
            "base_evidence_hash": snapshot_hash,
            "base_observed_at": canonical_timestamp(row["base_observed_at"]),
            "base_music_schema": str(state["schema_version"]),
            "base_music_status": str(state["base_status"]),
            "eligibility_reason": eligibility_reason,
        }
        by_id[post_id] = candidate
        candidates.append(candidate)
    if normalized_ids:
        return [by_id[post_id] for post_id in normalized_ids if post_id in by_id]
    return candidates


def _music_backfill_candidate_projection(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("music backfill candidate must be an object")
    post_id = str(value.get("post_id") or "").strip()
    snapshot_id = str(value.get("base_snapshot_id") or "").strip()
    if re.fullmatch(r"\d+", post_id) is None or not snapshot_id:
        raise ValueError("music backfill candidate binding is incomplete")
    return {
        "post_id": post_id,
        "canonical_url": str(value.get("canonical_url") or "").strip(),
        "creator_handle": str(value.get("creator_handle") or "").strip(),
        "creator_key": str(value.get("creator_key") or "").strip().casefold(),
        "base_snapshot_id": snapshot_id,
        "base_evidence_hash": _required_sha256(
            value.get("base_evidence_hash"), field="base_evidence_hash"
        ),
        "base_observed_at": canonical_timestamp(value.get("base_observed_at")),
        "base_music_schema": str(value.get("base_music_schema") or "")
        .strip()
        .casefold(),
        "base_music_status": str(value.get("base_music_status") or "")
        .strip()
        .casefold(),
        "eligibility_reason": str(value.get("eligibility_reason") or "").strip(),
    }


def _music_backfill_run_hash_document(values: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": values["run_id"],
        "project": values["project"],
        "master_database_path": values["master_database_path"],
        "scope_mode": values["scope_mode"],
        "scope_value": values["scope_value"],
        "target_schema_version": values["target_schema_version"],
        "configured_catalogs": values["configured_catalogs"],
        "retryable_statuses": values["retryable_statuses"],
        "force": bool(values["force"]),
        "candidate_set_hash": values["candidate_set_hash"],
        "selected_count": int(values["selected_count"]),
        "created_at": values["created_at"],
    }


def _music_backfill_account(value: Any) -> str:
    return str(value or "").strip().lstrip("@").casefold()


def register_music_backfill_run(
    conn: sqlite3.Connection,
    schema: str = "main",
    *,
    run_id: str,
    project: str,
    scope_mode: str,
    scope_value: str,
    target_schema_version: str,
    retryable_statuses: Sequence[str],
    candidates: Sequence[dict[str, Any]],
    configured_catalogs: Sequence[str] = ("musicbrainz",),
    force: bool = False,
    expected_account: str = "",
    observed_account: str = "",
    created_at: str = "",
    master_database_path: str | Path | None = None,
) -> dict[str, Any]:
    """Freeze one immutable backfill candidate set; repeated calls are exact."""

    schema = _schema(schema)
    normalized_run_id = str(run_id or "").strip()
    if not normalized_run_id or len(normalized_run_id) > 200:
        raise ValueError("music backfill run_id is required")
    existing = get_music_backfill_run(
        conn, schema, run_id=normalized_run_id
    )
    normalized_scope = str(scope_mode or "").strip().casefold()
    if normalized_scope not in {"creator", "topic", "post_ids", "url", "workspace"}:
        raise ValueError("music backfill scope_mode is invalid")
    target_schema = _music_backfill_schema_version(target_schema_version)
    catalogs = _music_backfill_catalogs(configured_catalogs)
    retryable = _music_backfill_retryable_statuses(retryable_statuses)
    frozen_candidates = [
        _music_backfill_candidate_projection(value) for value in candidates
    ]
    post_ids = [value["post_id"] for value in frozen_candidates]
    if len(post_ids) != len(set(post_ids)):
        raise ValueError("music backfill candidates contain duplicate post IDs")
    for candidate in frozen_candidates:
        base = _validated_base_snapshot(
            conn,
            schema,
            post_id=candidate["post_id"],
            snapshot_id=candidate["base_snapshot_id"],
            evidence_hash=candidate["base_evidence_hash"],
            observed_at=candidate["base_observed_at"],
            require_latest=existing is None,
        )
        if candidate["canonical_url"] != base["canonical_url"]:
            raise RuntimeError("music backfill candidate URL binding mismatch")
        if candidate["creator_key"] != base["creator_key"]:
            raise RuntimeError("music backfill candidate creator binding mismatch")
    timestamp = (
        canonical_timestamp(created_at)
        if str(created_at or "").strip()
        else (
            existing["created_at"]
            if existing is not None
            else canonical_timestamp(now_iso())
        )
    )
    attached_database_path = _schema_database_path(conn, schema)
    supplied_database_path = (
        _normalize_path(master_database_path)
        if master_database_path is not None
        and str(master_database_path).strip()
        else ""
    )
    if (
        supplied_database_path
        and attached_database_path
        and supplied_database_path != attached_database_path
    ):
        raise ValueError("master_database_path does not match attached registry")
    frozen_database_path = supplied_database_path or attached_database_path
    candidate_hash = json_hash(frozen_candidates)
    values = {
        "run_id": normalized_run_id,
        "project": str(project or "").strip(),
        "master_database_path": frozen_database_path,
        "scope_mode": normalized_scope,
        "scope_value": str(scope_value or "").strip(),
        "target_schema_version": target_schema,
        "configured_catalogs": list(catalogs),
        "retryable_statuses": list(retryable),
        "force": bool(force),
        "candidate_set_hash": candidate_hash,
        "selected_count": len(frozen_candidates),
        "created_at": timestamp,
    }
    run_hash = json_hash(_music_backfill_run_hash_document(values))
    expected_key = _music_backfill_account(expected_account)
    observed_key = _music_backfill_account(observed_account)
    if expected_key and observed_key and expected_key != observed_key:
        raise ValueError("observed TikTok account does not match expected account")
    if existing is not None:
        expected = {
            **values,
            "candidates": frozen_candidates,
            "run_hash": run_hash,
        }
        for field in (
            "project", "master_database_path", "scope_mode", "scope_value",
            "target_schema_version", "configured_catalogs",
            "retryable_statuses", "force", "candidate_set_hash",
            "selected_count", "created_at", "candidates", "run_hash",
        ):
            if existing[field] != expected[field]:
                raise RuntimeError("music backfill run immutable binding mismatch")
        if expected_key and existing["expected_account"] != expected_key:
            raise RuntimeError("music backfill expected account binding mismatch")
        if observed_key and existing["observed_account"] != observed_key:
            raise RuntimeError("music backfill observed account binding mismatch")
        result = dict(existing)
        result["created"] = False
        return result
    conn.execute(
        f"""
        INSERT INTO {_table(schema, "tiktok_master_music_backfill_runs")} (
            run_id, project, master_database_path, scope_mode, scope_value,
            target_schema_version, configured_catalogs_json,
            retryable_statuses_json, force, expected_account,
            observed_account, candidate_set_json, candidate_set_hash,
            run_hash, selected_count, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'planned', ?, ?)
        """,
        (
            normalized_run_id,
            values["project"],
            frozen_database_path,
            normalized_scope,
            values["scope_value"],
            target_schema,
            canonical_json(list(catalogs)),
            canonical_json(list(retryable)),
            int(bool(force)),
            expected_key,
            observed_key,
            canonical_json(frozen_candidates),
            candidate_hash,
            run_hash,
            len(frozen_candidates),
            timestamp,
            timestamp,
        ),
    )
    result = get_music_backfill_run(conn, schema, run_id=normalized_run_id)
    assert result is not None
    result["created"] = True
    return result


def _music_backfill_run_row(
    conn: sqlite3.Connection,
    schema: str,
    run_id: str,
) -> dict[str, Any] | None:
    columns = [
        str(row[1])
        for row in conn.execute(
            f'PRAGMA "{_schema(schema)}".table_info('
            '"tiktok_master_music_backfill_runs")'
        ).fetchall()
    ]
    row = conn.execute(
        f"""
        SELECT * FROM {_table(schema, "tiktok_master_music_backfill_runs")}
        WHERE run_id=?
        """,
        (str(run_id),),
    ).fetchone()
    return _row_dict(row, columns) if row is not None else None


def get_music_backfill_run(
    conn: sqlite3.Connection,
    schema: str = "main",
    *,
    run_id: str,
    validate_base_snapshots: bool = True,
) -> dict[str, Any] | None:
    """Read and verify an immutable backfill run and its derived counters.

    ``validate_base_snapshots=False`` is reserved for bounded internal
    mutations that separately validate the one base snapshot they touch.  It
    still verifies the frozen candidate-set hash, run hash, identities, and
    counters.  Public reads keep the full snapshot validation by default.
    """

    schema = _schema(schema)
    row = _music_backfill_run_row(conn, schema, str(run_id))
    if row is None:
        return None
    status = str(row["status"] or "").casefold()
    if status not in MUSIC_BACKFILL_RUN_STATUSES:
        raise RuntimeError("stored music backfill run status is invalid")
    candidates_raw = _parse_json_list(row["candidate_set_json"])
    candidates = [
        _music_backfill_candidate_projection(value) for value in candidates_raw
    ]
    candidate_hash = _required_sha256(
        row["candidate_set_hash"], field="candidate_set_hash"
    )
    if json_hash(candidates) != candidate_hash:
        raise RuntimeError("stored music backfill candidate set hash mismatch")
    if int(row["selected_count"]) != len(candidates):
        raise RuntimeError("stored music backfill selected count mismatch")
    if len({value["post_id"] for value in candidates}) != len(candidates):
        raise RuntimeError("stored music backfill candidate set has duplicates")
    if validate_base_snapshots:
        for candidate in candidates:
            _validated_base_snapshot(
                conn,
                schema,
                post_id=candidate["post_id"],
                snapshot_id=candidate["base_snapshot_id"],
                evidence_hash=candidate["base_evidence_hash"],
                observed_at=candidate["base_observed_at"],
                require_latest=False,
            )
    retryable = _music_backfill_retryable_statuses(
        _parse_json_list(row["retryable_statuses_json"])
    )
    catalogs = _music_backfill_catalogs(
        _parse_json_list(row["configured_catalogs_json"])
    )
    values = {
        "run_id": str(row["run_id"]),
        "project": str(row["project"]),
        "master_database_path": str(row["master_database_path"] or ""),
        "scope_mode": str(row["scope_mode"]),
        "scope_value": str(row["scope_value"]),
        "target_schema_version": _music_backfill_schema_version(
            row["target_schema_version"]
        ),
        "configured_catalogs": list(catalogs),
        "retryable_statuses": list(retryable),
        "force": bool(row["force"]),
        "candidate_set_hash": candidate_hash,
        "selected_count": len(candidates),
        "created_at": canonical_timestamp(row["created_at"]),
    }
    run_hash = _required_sha256(row["run_hash"], field="music backfill run_hash")
    if json_hash(_music_backfill_run_hash_document(values)) != run_hash:
        raise RuntimeError("stored music backfill run hash mismatch")
    counts = {
        str(item[0]): int(item[1])
        for item in conn.execute(
            f"""
            SELECT status, COUNT(*)
            FROM {_table(schema, "tiktok_master_music_backfill_observations")}
            WHERE run_id=? GROUP BY status
            """,
            (str(run_id),),
        ).fetchall()
    }
    for observation_status in MUSIC_BACKFILL_OBSERVATION_STATUSES:
        stored = int(row[f"{observation_status}_count"])
        if stored != counts.get(observation_status, 0):
            raise RuntimeError("stored music backfill counters are inconsistent")
    return {
        **values,
        "expected_account": str(row["expected_account"] or ""),
        "observed_account": str(row["observed_account"] or ""),
        "candidates": candidates,
        "run_hash": run_hash,
        "completed_count": counts.get("completed", 0),
        "unavailable_count": counts.get("unavailable", 0),
        "failed_count": counts.get("failed", 0),
        "observed_count": sum(counts.values()),
        "status": status,
        "error": str(row["error"] or ""),
        "updated_at": canonical_timestamp(row["updated_at"]),
        "completed_at": (
            canonical_timestamp(row["completed_at"])
            if str(row["completed_at"] or "")
            else ""
        ),
    }


def _refresh_music_backfill_counters(
    conn: sqlite3.Connection,
    schema: str,
    run_id: str,
) -> dict[str, int]:
    counts = {
        str(row[0]): int(row[1])
        for row in conn.execute(
            f"""
            SELECT status, COUNT(*)
            FROM {_table(schema, "tiktok_master_music_backfill_observations")}
            WHERE run_id=? GROUP BY status
            """,
            (str(run_id),),
        ).fetchall()
    }
    result = {
        "completed": counts.get("completed", 0),
        "unavailable": counts.get("unavailable", 0),
        "failed": counts.get("failed", 0),
    }
    conn.execute(
        f"""
        UPDATE {_table(schema, "tiktok_master_music_backfill_runs")}
        SET completed_count=?, unavailable_count=?, failed_count=?
        WHERE run_id=?
        """,
        (
            result["completed"], result["unavailable"], result["failed"],
            str(run_id),
        ),
    )
    return result


def update_music_backfill_run(
    conn: sqlite3.Connection,
    schema: str = "main",
    *,
    run_id: str,
    status: str = "",
    error: str | None = None,
    expected_account: str | None = None,
    observed_account: str | None = None,
) -> dict[str, Any]:
    """Update only operational state; immutable selection fields never move."""

    schema = _schema(schema)
    current = get_music_backfill_run(
        conn,
        schema,
        run_id=run_id,
        validate_base_snapshots=False,
    )
    if current is None:
        raise RuntimeError("music backfill run not found")
    next_status = str(status or current["status"]).strip().casefold()
    if next_status not in MUSIC_BACKFILL_RUN_STATUSES:
        raise ValueError("music backfill run status is invalid")
    if current["status"] in {
        "backfill_complete", "failed"
    } and next_status != current["status"]:
        raise RuntimeError("terminal music backfill status is immutable")
    expected = current["expected_account"]
    observed = current["observed_account"]
    if expected_account is not None:
        new_expected = _music_backfill_account(expected_account)
        if expected and new_expected != expected:
            raise RuntimeError("music backfill expected account is immutable once bound")
        expected = new_expected
    if observed_account is not None:
        new_observed = _music_backfill_account(observed_account)
        if observed and new_observed != observed:
            raise RuntimeError("music backfill observed account is immutable once bound")
        observed = new_observed
    if expected and observed and expected != observed:
        raise ValueError("observed TikTok account does not match expected account")
    counts = _refresh_music_backfill_counters(conn, schema, run_id)
    observed_count = sum(counts.values())
    if next_status == "backfill_complete" and (
        observed_count != current["selected_count"] or counts["failed"]
    ):
        raise RuntimeError("music backfill cannot complete with missing/failed rows")
    timestamp = canonical_timestamp(now_iso())
    completed_at = (
        timestamp
        if next_status in {"backfill_complete", "failed"}
        else ""
    )
    conn.execute(
        f"""
        UPDATE {_table(schema, "tiktok_master_music_backfill_runs")}
        SET expected_account=?, observed_account=?, status=?, error=?,
            updated_at=?, completed_at=?
        WHERE run_id=?
        """,
        (
            expected,
            observed,
            next_status,
            current["error"] if error is None else str(error or ""),
            timestamp,
            completed_at,
            str(run_id),
        ),
    )
    result = get_music_backfill_run(
        conn,
        schema,
        run_id=run_id,
        validate_base_snapshots=False,
    )
    assert result is not None
    return result


def finalize_music_backfill_run(
    conn: sqlite3.Connection,
    schema: str = "main",
    *,
    run_id: str,
    error: str = "",
) -> dict[str, Any]:
    current = get_music_backfill_run(conn, schema, run_id=run_id)
    if current is None:
        raise RuntimeError("music backfill run not found")
    terminal_count = current["observed_count"]
    complete = (
        terminal_count == current["selected_count"]
        and current["failed_count"] == 0
    )
    return update_music_backfill_run(
        conn,
        schema,
        run_id=run_id,
        status="backfill_complete" if complete else "backfill_incomplete",
        error=error,
    )


def _validated_music_observation_document(
    music_evidence: Any,
    music_evidence_hash: Any,
    *,
    target_schema_version: str,
    status: str,
    error: str,
) -> tuple[dict[str, Any], str]:
    document = music_evidence if isinstance(music_evidence, dict) else {}
    required_hash = _required_sha256(
        music_evidence_hash, field="music_evidence_hash"
    )
    if status == "failed":
        if document or required_hash != json_hash({}) or not str(error or "").strip():
            raise ValueError(
                "failed music observation requires empty hash-bound evidence and error"
            )
        return {}, required_hash
    if not document:
        raise ValueError("terminal music observation requires music evidence")
    if str(document.get("schema_version") or "").casefold() != target_schema_version:
        raise ValueError("music observation schema does not match backfill target")
    embedded_hash = _required_sha256(
        document.get("music_evidence_hash"),
        field="embedded music_evidence_hash",
    )
    unhashed = dict(document)
    unhashed.pop("music_evidence_hash", None)
    if embedded_hash != required_hash or json_hash(unhashed) != required_hash:
        raise ValueError("music observation evidence hash mismatch")
    return document, required_hash


def _music_backfill_observation_hash_document(
    values: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": MUSIC_BACKFILL_OBSERVATION_SCHEMA_VERSION,
        "run_id": values["run_id"],
        "post_id": values["post_id"],
        "base_snapshot_id": values["base_snapshot_id"],
        "base_evidence_hash": values["base_evidence_hash"],
        "base_observed_at": values["base_observed_at"],
        "music_observed_at": values["music_observed_at"],
        "music_evidence_hash": values["music_evidence_hash"],
        "status": values["status"],
        "error": values["error"],
    }


def record_music_backfill_observation(
    conn: sqlite3.Connection,
    schema: str = "main",
    *,
    run_id: str,
    post_id: str,
    base_snapshot_id: str,
    base_evidence_hash: str,
    base_observed_at: str,
    music_observed_at: str,
    music_evidence: dict[str, Any],
    music_evidence_hash: str,
    status: str = "completed",
    error: str = "",
) -> dict[str, Any]:
    """Append one hash-bound result; conflicting retries can never overwrite."""

    schema = _schema(schema)
    run = get_music_backfill_run(
        conn,
        schema,
        run_id=run_id,
        validate_base_snapshots=False,
    )
    if run is None:
        raise RuntimeError("music backfill run not found")
    normalized_status = str(status or "").strip().casefold()
    if normalized_status not in MUSIC_BACKFILL_OBSERVATION_STATUSES:
        raise ValueError("music backfill observation status is invalid")
    candidate = next(
        (value for value in run["candidates"] if value["post_id"] == str(post_id)),
        None,
    )
    if candidate is None:
        raise RuntimeError("post is not in the immutable backfill candidate set")
    supplied_binding = {
        "base_snapshot_id": str(base_snapshot_id),
        "base_evidence_hash": _required_sha256(
            base_evidence_hash, field="base_evidence_hash"
        ),
        "base_observed_at": canonical_timestamp(base_observed_at),
    }
    for field, supplied in supplied_binding.items():
        if supplied != candidate[field]:
            raise RuntimeError(f"music backfill {field} binding mismatch")
    _validated_base_snapshot(
        conn,
        schema,
        post_id=str(post_id),
        snapshot_id=supplied_binding["base_snapshot_id"],
        evidence_hash=supplied_binding["base_evidence_hash"],
        observed_at=supplied_binding["base_observed_at"],
        require_latest=False,
    )
    observed_at = canonical_timestamp(music_observed_at)
    if _parse_iso(observed_at) < _parse_iso(supplied_binding["base_observed_at"]):
        raise ValueError("music observation cannot precede its base evidence")
    document, evidence_hash = _validated_music_observation_document(
        music_evidence,
        music_evidence_hash,
        target_schema_version=run["target_schema_version"],
        status=normalized_status,
        error=error,
    )
    observation_id = stable_id(
        "tiktok-music-backfill-observation", run_id, post_id, length=40
    )
    stored_values = {
        "observation_id": observation_id,
        "run_id": str(run_id),
        "post_id": str(post_id),
        **supplied_binding,
        "music_observed_at": observed_at,
        "music_evidence": document,
        "music_evidence_hash": evidence_hash,
        "status": normalized_status,
        "error": str(error or ""),
    }
    stored_values["observation_hash"] = json_hash(
        _music_backfill_observation_hash_document(stored_values)
    )
    existing = conn.execute(
        f"""
        SELECT observation_id, base_snapshot_id, base_evidence_hash,
               base_observed_at, music_observed_at, music_evidence_json,
               music_evidence_hash, status, error, observation_hash
        FROM {_table(schema, "tiktok_master_music_backfill_observations")}
        WHERE run_id=? AND post_id=?
        """,
        (str(run_id), str(post_id)),
    ).fetchone()
    if existing is not None:
        existing_values = {
            "observation_id": str(existing[0]),
            "run_id": str(run_id),
            "post_id": str(post_id),
            "base_snapshot_id": str(existing[1]),
            "base_evidence_hash": str(existing[2]),
            "base_observed_at": canonical_timestamp(existing[3]),
            "music_observed_at": canonical_timestamp(existing[4]),
            "music_evidence": _parse_json_object(existing[5]),
            "music_evidence_hash": str(existing[6]),
            "status": str(existing[7]),
            "error": str(existing[8] or ""),
            "observation_hash": str(existing[9] or ""),
        }
        if existing_values != stored_values:
            raise RuntimeError("music backfill observation is append-only")
        return {**existing_values, "created": False}
    if run["status"] in {"backfill_complete", "failed"}:
        raise RuntimeError("cannot append to a terminal music backfill run")
    timestamp = canonical_timestamp(now_iso())
    conn.execute(
        f"""
        INSERT INTO {_table(schema, "tiktok_master_music_backfill_observations")} (
            observation_id, run_id, post_id, base_snapshot_id,
            base_evidence_hash, base_observed_at, music_observed_at,
            music_evidence_json, music_evidence_hash, status, error,
            created_at, observation_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            observation_id, str(run_id), str(post_id),
            supplied_binding["base_snapshot_id"],
            supplied_binding["base_evidence_hash"],
            supplied_binding["base_observed_at"], observed_at,
            canonical_json(document), evidence_hash, normalized_status,
            str(error or ""), timestamp, stored_values["observation_hash"],
        ),
    )
    _refresh_music_backfill_counters(conn, schema, str(run_id))
    conn.execute(
        f"""
        UPDATE {_table(schema, "tiktok_master_music_backfill_runs")}
        SET status=CASE WHEN status='planned' THEN 'running' ELSE status END,
            updated_at=?
        WHERE run_id=?
        """,
        (timestamp, str(run_id)),
    )
    return {**stored_values, "created": True}


def music_backfill_observations(
    conn: sqlite3.Connection,
    schema: str = "main",
    *,
    run_id: str,
    validate_base_snapshots: bool = True,
) -> list[dict[str, Any]]:
    run = get_music_backfill_run(
        conn,
        schema,
        run_id=run_id,
        validate_base_snapshots=validate_base_snapshots,
    )
    if run is None:
        raise RuntimeError("music backfill run not found")
    rows = conn.execute(
        f"""
        SELECT observation_id, run_id, post_id, base_snapshot_id,
               base_evidence_hash, base_observed_at, music_observed_at,
               music_evidence_json, music_evidence_hash, status, error,
               observation_hash, created_at
        FROM {_table(schema, "tiktok_master_music_backfill_observations")}
        WHERE run_id=?
        ORDER BY julianday(music_observed_at), music_observed_at, post_id
        """,
        (str(run_id),),
    ).fetchall()
    candidates = {value["post_id"]: value for value in run["candidates"]}
    results: list[dict[str, Any]] = []
    for row in rows:
        status = str(row[9] or "").casefold()
        if status not in MUSIC_BACKFILL_OBSERVATION_STATUSES:
            raise RuntimeError("stored music backfill observation status is invalid")
        post_id = str(row[2])
        candidate = candidates.get(post_id)
        if candidate is None:
            raise RuntimeError("stored music observation is outside frozen candidates")
        document, evidence_hash = _validated_music_observation_document(
            _parse_json_object(row[7]),
            row[8],
            target_schema_version=run["target_schema_version"],
            status=status,
            error=str(row[10] or ""),
        )
        result = {
            "observation_id": str(row[0]),
            "run_id": str(row[1]),
            "post_id": post_id,
            "base_snapshot_id": str(row[3]),
            "base_evidence_hash": _required_sha256(
                row[4], field="stored observation base_evidence_hash"
            ),
            "base_observed_at": canonical_timestamp(row[5]),
            "music_observed_at": canonical_timestamp(row[6]),
            "music_evidence": document,
            "music_evidence_hash": evidence_hash,
            "status": status,
            "error": str(row[10] or ""),
            "observation_hash": _required_sha256(
                row[11], field="stored music observation_hash"
            ),
            "created_at": canonical_timestamp(row[12]),
        }
        if result["run_id"] != str(run_id) or result["observation_id"] != stable_id(
            "tiktok-music-backfill-observation", run_id, post_id, length=40
        ):
            raise RuntimeError("stored music observation identity binding mismatch")
        for field in ("base_snapshot_id", "base_evidence_hash", "base_observed_at"):
            if result[field] != candidate[field]:
                raise RuntimeError("stored music observation base binding mismatch")
        if result["observation_hash"] != json_hash(
            _music_backfill_observation_hash_document(result)
        ):
            raise RuntimeError("stored music observation hash mismatch")
        _validated_base_snapshot(
            conn,
            schema,
            post_id=post_id,
            snapshot_id=result["base_snapshot_id"],
            evidence_hash=result["base_evidence_hash"],
            observed_at=result["base_observed_at"],
            require_latest=False,
        )
        results.append(result)
    return results


def _parse_iso(value: Any) -> dt.datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def reserve_provider_request_slot(
    conn: sqlite3.Connection,
    schema: str,
    *,
    provider: str,
    minimum_interval_seconds: float,
) -> float:
    """Atomically reserve a workspace-wide provider request start time."""

    provider = str(provider or "").strip().casefold()
    interval = float(minimum_interval_seconds)
    if not re.fullmatch(r"[a-z0-9._-]+", provider):
        raise ValueError("provider must be a normalized identifier")
    if not 0 < interval <= 300:
        raise ValueError("minimum provider interval must be in (0, 300]")
    if conn.in_transaction:
        raise RuntimeError("provider request slot requires no active transaction")
    conn.execute("BEGIN IMMEDIATE")
    try:
        now = dt.datetime.now(dt.timezone.utc)
        row = conn.execute(
            f"""
            SELECT next_allowed_at
            FROM {_table(schema, "tiktok_master_provider_rate_limits")}
            WHERE provider=?
            """,
            (provider,),
        ).fetchone()
        saved = _parse_iso(row[0]) if row else None
        reserved = max(now, saved) if saved is not None else now
        next_allowed = reserved + dt.timedelta(seconds=interval)
        conn.execute(
            f"""
            INSERT INTO {_table(schema, "tiktok_master_provider_rate_limits")} (
                provider, next_allowed_at, updated_at
            ) VALUES (?, ?, ?)
            ON CONFLICT(provider) DO UPDATE SET
                next_allowed_at=excluded.next_allowed_at,
                updated_at=excluded.updated_at
            """,
            (
                provider,
                next_allowed.isoformat(timespec="microseconds"),
                now.isoformat(timespec="microseconds"),
            ),
        )
        conn.commit()
        return max(0.0, (reserved - now).total_seconds())
    except Exception:
        conn.rollback()
        raise


def defer_provider_requests(
    conn: sqlite3.Connection,
    schema: str,
    *,
    provider: str,
    delay_seconds: float,
) -> None:
    """Extend a provider's workspace-global cooldown after rate refusal."""

    provider = str(provider or "").strip().casefold()
    delay = float(delay_seconds)
    if not re.fullmatch(r"[a-z0-9._-]+", provider):
        raise ValueError("provider must be a normalized identifier")
    if not 0 < delay <= 86_400:
        raise ValueError("provider cooldown must be in (0, 86400]")
    if conn.in_transaction:
        raise RuntimeError("provider cooldown requires no active transaction")
    conn.execute("BEGIN IMMEDIATE")
    try:
        now = dt.datetime.now(dt.timezone.utc)
        requested = now + dt.timedelta(seconds=delay)
        row = conn.execute(
            f"""
            SELECT next_allowed_at
            FROM {_table(schema, "tiktok_master_provider_rate_limits")}
            WHERE provider=?
            """,
            (provider,),
        ).fetchone()
        saved = _parse_iso(row[0]) if row else None
        next_allowed = max(requested, saved) if saved is not None else requested
        conn.execute(
            f"""
            INSERT INTO {_table(schema, "tiktok_master_provider_rate_limits")} (
                provider, next_allowed_at, updated_at
            ) VALUES (?, ?, ?)
            ON CONFLICT(provider) DO UPDATE SET
                next_allowed_at=excluded.next_allowed_at,
                updated_at=excluded.updated_at
            """,
            (
                provider,
                next_allowed.isoformat(timespec="microseconds"),
                now.isoformat(timespec="microseconds"),
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def reserve_collection_candidate(
    conn: sqlite3.Connection,
    schema: str,
    *,
    post_id: str,
    run_id: str,
    attempt_id: str,
    policy: str,
    lease_minutes: int = 30,
    source_path: str | Path | None = None,
) -> bool:
    post_id = str(post_id or "").strip()
    policy = str(policy or "new_only").casefold()
    if not post_id or policy not in {"new_only", "refresh_known"}:
        return False
    known = conn.execute(
        f"""
        SELECT 1 FROM {_table(schema, "tiktok_master_posts")}
        WHERE post_id=?
        """,
        (post_id,),
    ).fetchone() is not None
    if policy == "new_only" and known:
        return False
    if policy == "refresh_known" and not known:
        return False
    source_path = source_path or _main_database_path(conn)
    source_id = register_source(conn, schema, source_path)
    master_run_id = stable_id(source_id, run_id)
    lease = conn.execute(
        f"""
        SELECT master_run_id, local_run_id, expires_at
        FROM {_table(schema, "tiktok_master_collection_leases")}
        WHERE post_id=?
        """,
        (post_id,),
    ).fetchone()
    now = dt.datetime.now(dt.timezone.utc)
    if lease:
        expires = _parse_iso(lease[2])
        same_run = str(lease[1] or "") == str(run_id)
        if not same_run and expires is not None and expires > now:
            return False
    expires_at = (
        now + dt.timedelta(minutes=max(1, int(lease_minutes)))
    ).replace(microsecond=0).isoformat()
    acquired_at = now.replace(microsecond=0).isoformat()
    changed = conn.execute(
        f"""
        INSERT INTO {_table(schema, "tiktok_master_collection_leases")} (
            post_id, master_run_id, local_run_id, attempt_id,
            collection_policy, acquired_at, expires_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(post_id) DO UPDATE SET
            master_run_id=excluded.master_run_id,
            local_run_id=excluded.local_run_id,
            attempt_id=excluded.attempt_id,
            collection_policy=excluded.collection_policy,
            acquired_at=excluded.acquired_at,
            expires_at=excluded.expires_at,
            updated_at=excluded.updated_at
        WHERE local_run_id=excluded.local_run_id
           OR expires_at <= excluded.acquired_at
        """,
        (
            post_id,
            master_run_id,
            run_id,
            attempt_id,
            policy,
            acquired_at,
            expires_at,
            acquired_at,
        ),
    ).rowcount
    return changed == 1


def collection_candidate_guard(
    conn: sqlite3.Connection,
    schema: str,
    *,
    post_id: str,
    run_id: str,
    attempt_id: str,
    policy: str,
) -> dict[str, Any]:
    """Verify exact, unexpired lease ownership immediately before checkpoint."""

    post_id = str(post_id or "").strip()
    run_id = str(run_id or "").strip()
    attempt_id = str(attempt_id or "").strip()
    policy = str(policy or "").strip().casefold()
    result: dict[str, Any] = {
        "allowed": False,
        "reason": "",
        "post_id": post_id,
        "run_id": run_id,
        "attempt_id": attempt_id,
        "policy": policy,
    }
    if not post_id or not run_id or not attempt_id:
        result["reason"] = "candidate_identity_incomplete"
        return result
    if policy not in {"new_only", "refresh_known"}:
        result["reason"] = "collection_policy_invalid"
        return result
    lease = conn.execute(
        f"""
        SELECT local_run_id, attempt_id, collection_policy,
               acquired_at, expires_at
        FROM {_table(schema, "tiktok_master_collection_leases")}
        WHERE post_id=?
        """,
        (post_id,),
    ).fetchone()
    if not lease:
        result["reason"] = "candidate_lease_missing"
        return result
    result.update(
        {
            "lease_run_id": str(lease[0] or ""),
            "lease_attempt_id": str(lease[1] or ""),
            "lease_policy": str(lease[2] or ""),
            "lease_acquired_at": str(lease[3] or ""),
            "lease_expires_at": str(lease[4] or ""),
        }
    )
    if str(lease[0] or "") != run_id or str(lease[1] or "") != attempt_id:
        result["reason"] = "candidate_lease_not_owned"
        return result
    if str(lease[2] or "").casefold() != policy:
        result["reason"] = "candidate_lease_policy_changed"
        return result
    expires = _parse_iso(lease[4])
    if expires is None or expires <= dt.datetime.now(dt.timezone.utc):
        result["reason"] = "candidate_lease_expired"
        return result
    known = conn.execute(
        f"""
        SELECT 1
        FROM {_table(schema, "tiktok_master_posts")}
        WHERE post_id=?
        """,
        (post_id,),
    ).fetchone() is not None
    result["known"] = known
    if policy == "new_only" and known:
        result["reason"] = "candidate_became_known"
        return result
    if policy == "refresh_known" and not known:
        result["reason"] = "refresh_candidate_no_longer_known"
        return result
    result["allowed"] = True
    result["reason"] = "lease_owned"
    return result


def release_collection_candidate(
    conn: sqlite3.Connection,
    schema: str,
    *,
    post_id: str,
    run_id: str = "",
    attempt_id: str = "",
) -> bool:
    conditions = ["post_id=?"]
    parameters: list[Any] = [str(post_id)]
    if run_id:
        conditions.append("local_run_id=?")
        parameters.append(str(run_id))
    if attempt_id:
        conditions.append("attempt_id=?")
        parameters.append(str(attempt_id))
    changed = conn.execute(
        f"""
        DELETE FROM {_table(schema, "tiktok_master_collection_leases")}
        WHERE {" AND ".join(conditions)}
        """,
        tuple(parameters),
    ).rowcount
    return changed == 1


def comment_target_guard(
    conn: sqlite3.Connection,
    schema: str,
    *,
    account: str,
    post_id: str,
) -> dict[str, Any]:
    account_key = normalize_account(account)
    rows = conn.execute(
        f"""
        SELECT *
        FROM {_table(schema, "tiktok_master_comment_targets")}
        WHERE post_id=? AND account_key IN (?, '*')
        ORDER BY CASE WHEN account_key='*' THEN 0 ELSE 1 END
        """,
        (str(post_id), account_key),
    ).fetchall()
    columns = [
        item[1]
        for item in conn.execute(
            f'PRAGMA "{_schema(schema)}".table_info('
            '"tiktok_master_comment_targets")'
        ).fetchall()
    ]
    for row in rows:
        item = _row_dict(row, columns)
        if str(item.get("state") or "") in BLOCKING_COMMENT_STATES:
            return {"blocked": True, **item}
    return {
        "blocked": False,
        "account_key": account_key,
        "post_id": str(post_id),
    }


def register_publication_claim(
    conn: sqlite3.Connection,
    schema: str,
    *,
    account: str,
    post_id: str,
    publication_id: str,
    source_path: str | Path | None = None,
    run_id: str = "",
    target_url: str = "",
    text_hash: str = "",
    local_attempt_number: int = 0,
    metadata: dict[str, Any] | None = None,
) -> str:
    guard = comment_target_guard(
        conn,
        schema,
        account=account,
        post_id=post_id,
    )
    if guard["blocked"]:
        raise RuntimeError(
            "This TikTok account/post already has a confirmed, uncertain, "
            "submit-intent, or active comment attempt"
        )
    account_key = normalize_account(account)
    source_path = source_path or _main_database_path(conn)
    source_id = register_source(conn, schema, source_path)
    existing = conn.execute(
        f"""
        SELECT attempt_id
        FROM {_table(schema, "tiktok_master_comment_attempts")}
        WHERE source_id=? AND publication_id=? AND local_attempt_number=?
        """,
        (source_id, publication_id, int(local_attempt_number)),
    ).fetchone()
    if existing:
        return str(existing[0])
    timestamp = now_iso()
    attempt_id = stable_id(
        source_id,
        publication_id,
        int(local_attempt_number),
        timestamp,
    )
    conn.execute(
        f"""
        INSERT INTO {_table(schema, "tiktok_master_comment_attempts")} (
            attempt_id, account_key, post_id, publication_id, source_id,
            local_run_id, target_url, text_hash, state,
            local_attempt_number, reserved_at, metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?, ?)
        """,
        (
            attempt_id,
            account_key,
            str(post_id),
            str(publication_id),
            source_id,
            str(run_id),
            str(target_url),
            str(text_hash),
            int(local_attempt_number),
            timestamp,
            canonical_json(metadata or {}),
        ),
    )
    conn.execute(
        f"""
        INSERT INTO {_table(schema, "tiktok_master_comment_targets")} (
            account_key, post_id, state, active_attempt_id,
            first_publication_id, last_publication_id, canonical_url,
            attempt_count, first_attempt_at, last_attempt_at, updated_at
        ) VALUES (?, ?, 'reserved', ?, ?, ?, ?, 1, ?, ?, ?)
        ON CONFLICT(account_key, post_id) DO UPDATE SET
            state='reserved',
            active_attempt_id=excluded.active_attempt_id,
            last_publication_id=excluded.last_publication_id,
            canonical_url=excluded.canonical_url,
            attempt_count=attempt_count+1,
            last_attempt_at=excluded.last_attempt_at,
            error='',
            updated_at=excluded.updated_at
        """,
        (
            account_key,
            str(post_id),
            attempt_id,
            str(publication_id),
            str(publication_id),
            str(target_url),
            timestamp,
            timestamp,
            timestamp,
        ),
    )
    return attempt_id


def mark_submit_intent(
    conn: sqlite3.Connection,
    schema: str,
    attempt_id: str,
    *,
    submitted_at: str = "",
) -> None:
    timestamp = str(submitted_at or now_iso())
    row = conn.execute(
        f"""
        SELECT account_key, post_id, state
        FROM {_table(schema, "tiktok_master_comment_attempts")}
        WHERE attempt_id=?
        """,
        (attempt_id,),
    ).fetchone()
    if not row:
        raise RuntimeError("master publication attempt is missing")
    if str(row[2]) not in {"reserved", "submit_intent"}:
        raise RuntimeError("master publication attempt cannot enter submit intent")
    conn.execute(
        f"""
        UPDATE {_table(schema, "tiktok_master_comment_attempts")}
        SET state='submit_intent', submit_intent_at=?
        WHERE attempt_id=?
        """,
        (timestamp, attempt_id),
    )
    conn.execute(
        f"""
        UPDATE {_table(schema, "tiktok_master_comment_targets")}
        SET state='submit_intent', submit_intent_at=?, updated_at=?
        WHERE account_key=? AND post_id=? AND active_attempt_id=?
        """,
        (timestamp, timestamp, row[0], row[1], attempt_id),
    )


def register_publication_outcome(
    conn: sqlite3.Connection,
    schema: str,
    *,
    attempt_id: str,
    outcome: str,
    receipt_id: str = "",
    remote_comment_id: str = "",
    remote_comment_url: str = "",
    visible: bool = False,
    persisted: bool = False,
    text_hash: str = "",
    error: str = "",
    resolved_at: str = "",
) -> str:
    row = conn.execute(
        f"""
        SELECT account_key, post_id, publication_id, state, text_hash
        FROM {_table(schema, "tiktok_master_comment_attempts")}
        WHERE attempt_id=?
        """,
        (attempt_id,),
    ).fetchone()
    if not row:
        raise RuntimeError("master publication attempt is missing")
    requested = str(outcome or "").casefold()
    prior_state = str(row[3] or "")
    if requested in {"published", "confirmed", "deleted"}:
        state = "confirmed"
    elif requested in {"uncertain", "submit_intent"}:
        state = "uncertain"
    elif requested in {
        "failed",
        "pre_submit_failed",
        "retryable",
        "definitive_rejection",
    }:
        state = "uncertain" if prior_state == "submit_intent" else "retryable"
    else:
        raise ValueError(f"unsupported master publication outcome: {outcome}")
    timestamp = str(resolved_at or now_iso())
    conn.execute(
        f"""
        UPDATE {_table(schema, "tiktok_master_comment_attempts")}
        SET state=?, resolved_at=?, local_receipt_id=?,
            remote_comment_id=?, remote_comment_url=?, visible=?,
            persisted=?, error=?
        WHERE attempt_id=?
        """,
        (
            state,
            timestamp,
            str(receipt_id),
            str(remote_comment_id),
            str(remote_comment_url),
            1 if visible else 0,
            1 if persisted else 0,
            str(error),
            attempt_id,
        ),
    )
    conn.execute(
        f"""
        UPDATE {_table(schema, "tiktok_master_comment_targets")} SET
            state=?,
            active_attempt_id=CASE
                WHEN ?='retryable' THEN '' ELSE active_attempt_id END,
            confirmed_attempt_id=CASE
                WHEN ?='confirmed' THEN ? ELSE confirmed_attempt_id END,
            confirmed_text_hash=CASE
                WHEN ?='confirmed' THEN ? ELSE confirmed_text_hash END,
            remote_comment_id=CASE
                WHEN ?='confirmed' THEN ? ELSE remote_comment_id END,
            remote_comment_url=CASE
                WHEN ?='confirmed' THEN ? ELSE remote_comment_url END,
            confirmed_at=CASE
                WHEN ?='confirmed' THEN ? ELSE confirmed_at END,
            error=?,
            updated_at=?
        WHERE account_key=? AND post_id=? AND active_attempt_id=?
        """,
        (
            state,
            state,
            state,
            attempt_id,
            state,
            str(text_hash or row[4] or ""),
            state,
            str(remote_comment_id),
            state,
            str(remote_comment_url),
            state,
            timestamp,
            str(error),
            timestamp,
            row[0],
            row[1],
            attempt_id,
        ),
    )
    return state


def bind_confirmed_comment_remote_id(
    conn: sqlite3.Connection,
    schema: str,
    *,
    attempt_id: str,
    account: str,
    post_id: str,
    publication_id: str,
    receipt_id: str,
    text_hash: str,
    remote_comment_id: str,
    remote_comment_url: str = "",
) -> str:
    """Bind a recovered comment ID to one already-confirmed attempt.

    This transition exists for the narrow case where browser publication was
    confirmed through exact persisted text before TikTok exposed a comment ID.
    The caller must execute it in the same transaction that updates the local
    publication queue and receipt.  Every immutable source field is checked
    before either the attempt or its account/post target can be changed.
    """

    attempt_key = str(attempt_id or "").strip()
    account_key = normalize_account(account)
    post_key = str(post_id or "").strip()
    publication_key = str(publication_id or "").strip()
    receipt_key = str(receipt_id or "").strip()
    expected_text_hash = str(text_hash or "").strip().casefold()
    comment_id = str(remote_comment_id or "").strip()
    comment_url = str(remote_comment_url or "").strip()
    if not all(
        (
            attempt_key,
            post_key,
            publication_key,
            receipt_key,
            expected_text_hash,
            comment_id,
        )
    ):
        raise ValueError(
            "confirmed comment ID binding requires attempt, post, publication, "
            "receipt, text hash, and remote comment ID"
        )
    if account_key == "*":
        raise ValueError(
            "confirmed comment ID binding requires a verified posting account"
        )

    attempt_columns = (
        "attempt_id",
        "account_key",
        "post_id",
        "publication_id",
        "text_hash",
        "state",
        "local_receipt_id",
        "remote_comment_id",
        "remote_comment_url",
    )
    attempt_row = conn.execute(
        f"""
        SELECT {", ".join(attempt_columns)}
        FROM {_table(schema, "tiktok_master_comment_attempts")}
        WHERE attempt_id=?
        """,
        (attempt_key,),
    ).fetchone()
    if attempt_row is None:
        raise RuntimeError("confirmed source comment attempt is missing")
    attempt = _row_dict(attempt_row, attempt_columns)
    expected_attempt = {
        "attempt_id": attempt_key,
        "account_key": account_key,
        "post_id": post_key,
        "publication_id": publication_key,
        "text_hash": expected_text_hash,
        "state": "confirmed",
    }
    for field, expected in expected_attempt.items():
        actual = str(attempt.get(field) or "")
        if field in {"account_key", "text_hash", "state"}:
            actual = actual.casefold()
        if actual != expected:
            raise RuntimeError(
                f"confirmed source comment attempt drift: {field}"
            )
    stored_receipt_id = str(attempt.get("local_receipt_id") or "").strip()
    if stored_receipt_id not in {"", receipt_key}:
        raise RuntimeError("confirmed source comment attempt receipt drift")
    stored_attempt_comment_id = str(
        attempt.get("remote_comment_id") or ""
    ).strip()
    if stored_attempt_comment_id not in {"", comment_id}:
        raise RuntimeError("confirmed source comment attempt ID conflict")
    stored_attempt_comment_url = str(
        attempt.get("remote_comment_url") or ""
    ).strip()
    if (
        comment_url
        and stored_attempt_comment_url
        and stored_attempt_comment_url != comment_url
    ):
        raise RuntimeError("confirmed source comment attempt URL conflict")

    target_columns = (
        "account_key",
        "post_id",
        "state",
        "confirmed_attempt_id",
        "confirmed_text_hash",
        "remote_comment_id",
        "remote_comment_url",
    )
    target_row = conn.execute(
        f"""
        SELECT {", ".join(target_columns)}
        FROM {_table(schema, "tiktok_master_comment_targets")}
        WHERE account_key=? AND post_id=?
        """,
        (account_key, post_key),
    ).fetchone()
    if target_row is None:
        raise RuntimeError("confirmed source comment target is missing")
    target = _row_dict(target_row, target_columns)
    target_expected = {
        "account_key": account_key,
        "post_id": post_key,
        "state": "confirmed",
        "confirmed_attempt_id": attempt_key,
        "confirmed_text_hash": expected_text_hash,
    }
    for field, expected in target_expected.items():
        actual = str(target.get(field) or "")
        if field in {"account_key", "state", "confirmed_text_hash"}:
            actual = actual.casefold()
        if actual != expected:
            raise RuntimeError(f"confirmed source comment target drift: {field}")
    stored_target_comment_id = str(target.get("remote_comment_id") or "").strip()
    if stored_target_comment_id not in {"", comment_id}:
        raise RuntimeError("confirmed source comment target ID conflict")
    stored_target_comment_url = str(
        target.get("remote_comment_url") or ""
    ).strip()
    if (
        comment_url
        and stored_target_comment_url
        and stored_target_comment_url != comment_url
    ):
        raise RuntimeError("confirmed source comment target URL conflict")

    duplicate_attempt = conn.execute(
        f"""
        SELECT attempt_id
        FROM {_table(schema, "tiktok_master_comment_attempts")}
        WHERE remote_comment_id=? AND attempt_id<>?
        LIMIT 1
        """,
        (comment_id, attempt_key),
    ).fetchone()
    if duplicate_attempt is not None:
        raise RuntimeError("remote comment ID is already bound to another attempt")
    duplicate_target = conn.execute(
        f"""
        SELECT account_key, post_id
        FROM {_table(schema, "tiktok_master_comment_targets")}
        WHERE remote_comment_id=?
          AND NOT (account_key=? AND post_id=?)
        LIMIT 1
        """,
        (comment_id, account_key, post_key),
    ).fetchone()
    if duplicate_target is not None:
        raise RuntimeError("remote comment ID is already bound to another target")

    existing_showcase = conn.execute(
        f"""
        SELECT source_comment_id
        FROM {_table(schema, "tiktok_master_showcase_targets")}
        WHERE source_comment_attempt_id=?
        """,
        (attempt_key,),
    ).fetchone()
    if existing_showcase is not None and str(
        existing_showcase[0] or ""
    ).strip() not in {comment_id}:
        raise RuntimeError(
            "a showcase source was already bound before remote comment ID recovery"
        )

    attempt_changed = conn.execute(
        f"""
        UPDATE {_table(schema, "tiktok_master_comment_attempts")}
        SET local_receipt_id=?, remote_comment_id=?,
            remote_comment_url=CASE
                WHEN ?<>'' THEN ? ELSE remote_comment_url END
        WHERE attempt_id=? AND state='confirmed'
          AND account_key=? AND post_id=? AND publication_id=?
          AND lower(text_hash)=?
          AND local_receipt_id IN ('', ?)
          AND remote_comment_id IN ('', ?)
        """,
        (
            receipt_key,
            comment_id,
            comment_url,
            comment_url,
            attempt_key,
            account_key,
            post_key,
            publication_key,
            expected_text_hash,
            receipt_key,
            comment_id,
        ),
    ).rowcount
    target_changed = conn.execute(
        f"""
        UPDATE {_table(schema, "tiktok_master_comment_targets")}
        SET remote_comment_id=?,
            remote_comment_url=CASE
                WHEN ?<>'' THEN ? ELSE remote_comment_url END,
            updated_at=?
        WHERE account_key=? AND post_id=? AND state='confirmed'
          AND confirmed_attempt_id=? AND lower(confirmed_text_hash)=?
          AND remote_comment_id IN ('', ?)
        """,
        (
            comment_id,
            comment_url,
            comment_url,
            now_iso(),
            account_key,
            post_key,
            attempt_key,
            expected_text_hash,
            comment_id,
        ),
    ).rowcount
    if attempt_changed != 1 or target_changed != 1:
        raise RuntimeError("confirmed remote comment ID binding lost its source")
    return comment_id


_SHOWCASE_BINDING_FIELDS = (
    "source_comment_attempt_id",
    "posting_account_key",
    "source_post_id",
    "source_comment_id",
    "source_publication_id",
    "media_hash",
    "caption_hash",
)


def _showcase_target_record(
    conn: sqlite3.Connection,
    schema: str,
    source_comment_attempt_id: str,
) -> dict[str, Any] | None:
    columns = (
        *_SHOWCASE_BINDING_FIELDS,
        "state",
        "active_attempt_id",
        "confirmed_attempt_id",
        "first_source_id",
        "last_source_id",
        "first_local_job_id",
        "last_local_job_id",
        "remote_post_id",
        "remote_post_url",
        "attempt_count",
        "first_attempt_at",
        "last_attempt_at",
        "submit_intent_at",
        "confirmed_at",
        "error",
        "updated_at",
    )
    row = conn.execute(
        f"""
        SELECT {", ".join(columns)}
        FROM {_table(schema, "tiktok_master_showcase_targets")}
        WHERE source_comment_attempt_id=?
        """,
        (str(source_comment_attempt_id),),
    ).fetchone()
    return _row_dict(row, columns) if row is not None else None


def _assert_showcase_binding(
    record: dict[str, Any],
    expected: dict[str, str],
    *,
    label: str,
) -> None:
    for field in _SHOWCASE_BINDING_FIELDS:
        if str(record.get(field) or "") == str(expected.get(field) or ""):
            continue
        if field in {"media_hash", "caption_hash"}:
            drift = "hash"
        elif field == "posting_account_key":
            drift = "posting account"
        else:
            drift = "source"
        raise RuntimeError(f"{label} {drift} drift: {field}")


def _confirmed_comment_binding(
    conn: sqlite3.Connection,
    schema: str,
    source_comment_attempt_id: str,
) -> dict[str, str]:
    columns = (
        "attempt_id",
        "post_id",
        "publication_id",
        "remote_comment_id",
        "state",
    )
    row = conn.execute(
        f"""
        SELECT {", ".join(columns)}
        FROM {_table(schema, "tiktok_master_comment_attempts")}
        WHERE attempt_id=?
        """,
        (str(source_comment_attempt_id),),
    ).fetchone()
    if row is None:
        raise RuntimeError("source comment attempt is missing")
    record = _row_dict(row, columns)
    if str(record.get("state") or "") != "confirmed":
        raise RuntimeError("source comment attempt is not confirmed")
    return {
        "source_comment_attempt_id": str(record["attempt_id"]),
        "source_post_id": str(record.get("post_id") or ""),
        "source_comment_id": str(record.get("remote_comment_id") or ""),
        "source_publication_id": str(record.get("publication_id") or ""),
    }


def showcase_target_guard(
    conn: sqlite3.Connection,
    schema: str,
    *,
    source_comment_attempt_id: str,
    posting_account: str | None = None,
    source_post_id: str | None = None,
    source_comment_id: str | None = None,
    source_publication_id: str | None = None,
    media_hash: str | None = None,
    caption_hash: str | None = None,
) -> dict[str, Any]:
    """Return the workspace-wide showcase fence for one published comment.

    Optional binding values make this more than a state lookup: a caller that
    presents a different posting account, source identity, media, or caption
    is rejected rather than being allowed to reinterpret an existing target.
    """

    source_attempt = str(source_comment_attempt_id or "").strip()
    if not source_attempt:
        raise ValueError("source_comment_attempt_id is required")
    target = _showcase_target_record(conn, schema, source_attempt)
    if target is None:
        return {
            "blocked": False,
            "source_comment_attempt_id": source_attempt,
            "posting_account_key": (
                normalize_account(posting_account)
                if posting_account is not None
                else ""
            ),
        }
    optional = {
        "source_comment_attempt_id": source_attempt,
        "posting_account_key": (
            normalize_account(posting_account)
            if posting_account is not None
            else target["posting_account_key"]
        ),
        "source_post_id": (
            str(source_post_id)
            if source_post_id is not None
            else target["source_post_id"]
        ),
        "source_comment_id": (
            str(source_comment_id)
            if source_comment_id is not None
            else target["source_comment_id"]
        ),
        "source_publication_id": (
            str(source_publication_id)
            if source_publication_id is not None
            else target["source_publication_id"]
        ),
        "media_hash": (
            str(media_hash)
            if media_hash is not None
            else target["media_hash"]
        ),
        "caption_hash": (
            str(caption_hash)
            if caption_hash is not None
            else target["caption_hash"]
        ),
    }
    _assert_showcase_binding(target, optional, label="showcase target")
    return {
        "blocked": str(target.get("state") or "")
        in BLOCKING_SHOWCASE_STATES,
        **target,
    }


def register_showcase_claim(
    conn: sqlite3.Connection,
    schema: str,
    *,
    source_comment_attempt_id: str,
    posting_account: str,
    source_post_id: str,
    source_comment_id: str,
    source_publication_id: str,
    media_hash: str,
    caption_hash: str,
    local_job_id: str,
    source_path: str | Path | None = None,
    local_attempt_number: int = 0,
    metadata: dict[str, Any] | None = None,
) -> str:
    """Reserve one COMMENT SHOWCASE publication attempt.

    The published comment is the global target identity. The source path,
    local job, and attempt number identify an idempotent local call, while all
    account/source/content hashes are immutable across projects and retries.
    """

    source_attempt = str(source_comment_attempt_id or "").strip()
    account_key = normalize_account(posting_account)
    local_job = str(local_job_id or "").strip()
    media = str(media_hash or "").strip()
    caption = str(caption_hash or "").strip()
    if not source_attempt:
        raise ValueError("source_comment_attempt_id is required")
    if account_key == "*":
        raise ValueError("a verified posting_account is required")
    if not local_job:
        raise ValueError("local_job_id is required")
    if not media or not caption:
        raise ValueError("media_hash and caption_hash are required")
    attempt_number = int(local_attempt_number)
    if attempt_number < 0:
        raise ValueError("local_attempt_number cannot be negative")

    confirmed_source = _confirmed_comment_binding(
        conn,
        schema,
        source_attempt,
    )
    supplied_source = {
        "source_comment_attempt_id": source_attempt,
        "source_post_id": str(source_post_id or ""),
        "source_comment_id": str(source_comment_id or ""),
        "source_publication_id": str(source_publication_id or ""),
    }
    for field, expected in confirmed_source.items():
        if supplied_source[field] != expected:
            raise RuntimeError(f"showcase source drift: {field}")

    binding = {
        **supplied_source,
        "posting_account_key": account_key,
        "media_hash": media,
        "caption_hash": caption,
    }
    source_path = source_path or _main_database_path(conn)
    source_id = register_source(conn, schema, source_path)
    attempt_columns = (
        "attempt_id",
        *_SHOWCASE_BINDING_FIELDS,
        "source_id",
        "local_job_id",
        "local_attempt_number",
        "state",
    )
    existing = conn.execute(
        f"""
        SELECT {", ".join(attempt_columns)}
        FROM {_table(schema, "tiktok_master_showcase_attempts")}
        WHERE source_id=? AND local_job_id=? AND local_attempt_number=?
        """,
        (source_id, local_job, attempt_number),
    ).fetchone()
    if existing is not None:
        record = _row_dict(existing, attempt_columns)
        _assert_showcase_binding(
            record,
            binding,
            label="local showcase attempt",
        )
        return str(record["attempt_id"])

    target = _showcase_target_record(conn, schema, source_attempt)
    if target is not None:
        _assert_showcase_binding(target, binding, label="showcase target")
        if str(target.get("state") or "") in BLOCKING_SHOWCASE_STATES:
            raise RuntimeError(
                "This published comment already has a confirmed, uncertain, "
                "submit-intent, or active COMMENT SHOWCASE attempt"
            )

    timestamp = now_iso()
    attempt_id = stable_id(
        "tiktok-comment-showcase",
        source_id,
        local_job,
        attempt_number,
        timestamp,
    )
    conn.execute(
        f"""
        INSERT INTO {_table(schema, "tiktok_master_showcase_attempts")} (
            attempt_id, source_comment_attempt_id, posting_account_key,
            source_post_id, source_comment_id, source_publication_id,
            media_hash, caption_hash, source_id, local_job_id,
            local_attempt_number, state, reserved_at, metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?)
        """,
        (
            attempt_id,
            source_attempt,
            account_key,
            supplied_source["source_post_id"],
            supplied_source["source_comment_id"],
            supplied_source["source_publication_id"],
            media,
            caption,
            source_id,
            local_job,
            attempt_number,
            timestamp,
            canonical_json(metadata or {}),
        ),
    )
    if target is None:
        conn.execute(
            f"""
            INSERT INTO {_table(schema, "tiktok_master_showcase_targets")} (
                source_comment_attempt_id, posting_account_key,
                source_post_id, source_comment_id, source_publication_id,
                media_hash, caption_hash, state, active_attempt_id,
                first_source_id, last_source_id, first_local_job_id,
                last_local_job_id, attempt_count, first_attempt_at,
                last_attempt_at, updated_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?, ?, ?, ?, 1, ?, ?, ?
            )
            """,
            (
                source_attempt,
                account_key,
                supplied_source["source_post_id"],
                supplied_source["source_comment_id"],
                supplied_source["source_publication_id"],
                media,
                caption,
                attempt_id,
                source_id,
                source_id,
                local_job,
                local_job,
                timestamp,
                timestamp,
                timestamp,
            ),
        )
    else:
        changed = conn.execute(
            f"""
            UPDATE {_table(schema, "tiktok_master_showcase_targets")}
            SET state='reserved', active_attempt_id=?, last_source_id=?,
                last_local_job_id=?, attempt_count=attempt_count+1,
                last_attempt_at=?, submit_intent_at='', error='',
                updated_at=?
            WHERE source_comment_attempt_id=? AND state='retryable'
            """,
            (
                attempt_id,
                source_id,
                local_job,
                timestamp,
                timestamp,
                source_attempt,
            ),
        ).rowcount
        if changed != 1:
            raise RuntimeError("showcase target changed while being claimed")
    return attempt_id


def mark_showcase_submit_intent(
    conn: sqlite3.Connection,
    schema: str,
    attempt_id: str,
    *,
    submitted_at: str = "",
) -> None:
    timestamp = str(submitted_at or now_iso())
    row = conn.execute(
        f"""
        SELECT source_comment_attempt_id, state
        FROM {_table(schema, "tiktok_master_showcase_attempts")}
        WHERE attempt_id=?
        """,
        (str(attempt_id),),
    ).fetchone()
    if row is None:
        raise RuntimeError("master showcase attempt is missing")
    source_attempt = str(row[0])
    attempt_state = str(row[1] or "")
    target = _showcase_target_record(conn, schema, source_attempt)
    if (
        target is None
        or str(target.get("active_attempt_id") or "") != str(attempt_id)
    ):
        raise RuntimeError("master showcase attempt lost its active target")
    target_state = str(target.get("state") or "")
    if attempt_state == "submit_intent" and target_state == "submit_intent":
        return
    if attempt_state != "reserved" or target_state != "reserved":
        raise RuntimeError("master showcase attempt cannot enter submit intent")
    changed = conn.execute(
        f"""
        UPDATE {_table(schema, "tiktok_master_showcase_targets")}
        SET state='submit_intent', submit_intent_at=?, updated_at=?
        WHERE source_comment_attempt_id=? AND active_attempt_id=?
          AND state='reserved'
        """,
        (timestamp, timestamp, source_attempt, str(attempt_id)),
    ).rowcount
    if changed != 1:
        raise RuntimeError("master showcase target changed before submit intent")
    attempt_changed = conn.execute(
        f"""
        UPDATE {_table(schema, "tiktok_master_showcase_attempts")}
        SET state='submit_intent', submit_intent_at=?
        WHERE attempt_id=? AND state='reserved'
        """,
        (timestamp, str(attempt_id)),
    ).rowcount
    if attempt_changed != 1:
        raise RuntimeError("master showcase attempt changed before submit intent")


def checkpoint_showcase_publish_id(
    conn: sqlite3.Connection,
    schema: str,
    attempt_id: str,
    publish_id: str,
    *,
    checkpointed_at: str = "",
) -> dict[str, str]:
    """Bind one TikTok API publish ID to the active master attempt."""

    normalized_publish_id = str(publish_id or "").strip()
    if (
        not normalized_publish_id
        or len(normalized_publish_id) > 512
        or any(
            ord(character) < 0x21 or ord(character) > 0x7E
            for character in normalized_publish_id
        )
    ):
        raise ValueError(
            "TikTok publish ID must be a non-empty printable identifier"
        )
    row = conn.execute(
        f"""
        SELECT source_comment_attempt_id, state, publish_id,
               publish_id_checkpoint_at
        FROM {_table(schema, "tiktok_master_showcase_attempts")}
        WHERE attempt_id=?
        """,
        (str(attempt_id),),
    ).fetchone()
    if row is None:
        raise RuntimeError("master showcase attempt is missing")
    source_attempt = str(row[0])
    if str(row[1] or "") != "submit_intent":
        raise RuntimeError(
            "master showcase attempt cannot checkpoint a publish ID"
        )
    target = _showcase_target_record(conn, schema, source_attempt)
    if (
        target is None
        or str(target.get("active_attempt_id") or "") != str(attempt_id)
        or str(target.get("state") or "") != "submit_intent"
    ):
        raise RuntimeError(
            "master showcase attempt lost its submit-intent target"
        )
    existing = str(row[2] or "")
    if existing and existing != normalized_publish_id:
        raise RuntimeError("master showcase attempt publish ID changed")
    timestamp = str(row[3] or checkpointed_at or now_iso())
    if not existing:
        changed = conn.execute(
            f"""
            UPDATE {_table(schema, "tiktok_master_showcase_attempts")}
            SET publish_id=?, publish_id_checkpoint_at=?
            WHERE attempt_id=? AND state='submit_intent' AND publish_id=''
            """,
            (normalized_publish_id, timestamp, str(attempt_id)),
        ).rowcount
        if changed != 1:
            raise RuntimeError(
                "master showcase attempt changed before publish-ID checkpoint"
            )
    return {
        "attempt_id": str(attempt_id),
        "state": "submit_intent",
        "publish_id": normalized_publish_id,
        "publish_id_checkpoint_at": timestamp,
    }


def register_showcase_outcome(
    conn: sqlite3.Connection,
    schema: str,
    *,
    attempt_id: str,
    outcome: str,
    receipt_id: str = "",
    remote_post_id: str = "",
    remote_post_url: str = "",
    media_hash: str = "",
    caption_hash: str = "",
    publish_id: str = "",
    error: str = "",
    resolved_at: str = "",
) -> str:
    columns = (
        *_SHOWCASE_BINDING_FIELDS,
        "state",
        "local_receipt_id",
        "remote_post_id",
        "remote_post_url",
        "publish_id",
    )
    row = conn.execute(
        f"""
        SELECT {", ".join(columns)}
        FROM {_table(schema, "tiktok_master_showcase_attempts")}
        WHERE attempt_id=?
        """,
        (str(attempt_id),),
    ).fetchone()
    if row is None:
        raise RuntimeError("master showcase attempt is missing")
    record = _row_dict(row, columns)
    for field, supplied in (
        ("media_hash", media_hash),
        ("caption_hash", caption_hash),
        ("publish_id", publish_id),
    ):
        existing = str(record.get(field) or "")
        if supplied and existing and str(supplied) != existing:
            drift_kind = "publish-ID" if field == "publish_id" else "hash"
            raise RuntimeError(
                f"showcase outcome {drift_kind} drift: {field}"
            )

    requested = str(outcome or "").casefold()
    prior_state = str(record.get("state") or "")
    if requested in {"published", "confirmed"}:
        state = "confirmed"
        if not str(remote_post_id or "").strip():
            raise ValueError("remote_post_id is required for a confirmed showcase")
    elif requested in {"uncertain", "submit_intent"}:
        state = "uncertain"
    elif requested in {
        "failed",
        "pre_submit_failed",
        "retryable",
        "definitive_rejection",
    }:
        state = (
            "uncertain"
            if prior_state in {"submit_intent", "uncertain"}
            else "retryable"
        )
    else:
        raise ValueError(f"unsupported master showcase outcome: {outcome}")

    if prior_state in {"retryable", "uncertain", "confirmed"}:
        if prior_state == "uncertain" and state == "confirmed":
            pass
        elif prior_state != state:
            raise RuntimeError("master showcase attempt outcome is already fenced")
        else:
            for field, supplied in (
                ("local_receipt_id", receipt_id),
                ("remote_post_id", remote_post_id),
                ("remote_post_url", remote_post_url),
            ):
                existing = str(record.get(field) or "")
                if supplied and existing and str(supplied) != existing:
                    raise RuntimeError(
                        f"master showcase attempt outcome drift: {field}"
                    )
            return state

    source_attempt = str(record["source_comment_attempt_id"])
    target = _showcase_target_record(conn, schema, source_attempt)
    if (
        target is None
        or str(target.get("active_attempt_id") or "") != str(attempt_id)
    ):
        raise RuntimeError("master showcase attempt lost its active target")
    _assert_showcase_binding(
        target,
        {
            field: str(record.get(field) or "")
            for field in _SHOWCASE_BINDING_FIELDS
        },
        label="showcase target",
    )
    timestamp = str(resolved_at or now_iso())
    changed = conn.execute(
        f"""
        UPDATE {_table(schema, "tiktok_master_showcase_targets")} SET
            state=?,
            active_attempt_id=CASE
                WHEN ?='retryable' THEN '' ELSE active_attempt_id END,
            confirmed_attempt_id=CASE
                WHEN ?='confirmed' THEN ? ELSE confirmed_attempt_id END,
            remote_post_id=CASE
                WHEN ?='confirmed' THEN ? ELSE remote_post_id END,
            remote_post_url=CASE
                WHEN ?='confirmed' THEN ? ELSE remote_post_url END,
            confirmed_at=CASE
                WHEN ?='confirmed' THEN ? ELSE confirmed_at END,
            error=?,
            updated_at=?
        WHERE source_comment_attempt_id=? AND active_attempt_id=?
        """,
        (
            state,
            state,
            state,
            str(attempt_id),
            state,
            str(remote_post_id),
            state,
            str(remote_post_url),
            state,
            timestamp,
            str(error),
            timestamp,
            source_attempt,
            str(attempt_id),
        ),
    ).rowcount
    if changed != 1:
        raise RuntimeError("master showcase target changed before outcome")
    conn.execute(
        f"""
        UPDATE {_table(schema, "tiktok_master_showcase_attempts")}
        SET state=?, resolved_at=?, local_receipt_id=?,
            remote_post_id=?, remote_post_url=?, error=?
        WHERE attempt_id=?
        """,
        (
            state,
            timestamp,
            str(receipt_id),
            str(remote_post_id),
            str(remote_post_url),
            str(error),
            str(attempt_id),
        ),
    )
    return state


def _import_legacy_publications(
    conn: sqlite3.Connection,
    schema: str,
    source_path: str | Path,
) -> dict[str, int]:
    if not _table_exists(conn, "main", "publication_queue"):
        return {"confirmed": 0, "uncertain": 0}
    queue_columns = _columns(conn, "main", "publication_queue")
    wanted = [
        column
        for column in (
            "publication_id",
            "content_key",
            "engage_post_id",
            "target_url",
            "expected_account",
            "observed_account",
            "status",
            "draft_hash",
            "remote_comment_id",
            "remote_comment_url",
            "published_at",
            "engage_run_id",
            "attempts",
            "error",
        )
        if column in queue_columns
    ]
    if not {"publication_id", "status"}.issubset(wanted):
        return {"confirmed": 0, "uncertain": 0}
    rows = conn.execute(
        f"""
        SELECT {", ".join(f'"{column}"' for column in wanted)}
        FROM "main"."publication_queue"
        WHERE status IN (
            'published', 'deleted', 'uncertain', 'publishing', 'failed'
        )
        """
    ).fetchall()
    receipts: dict[str, dict[str, Any]] = {}
    if _table_exists(conn, "main", "publication_receipts"):
        receipt_columns = _columns(conn, "main", "publication_receipts")
        if {"publication_id", "response_json"}.issubset(receipt_columns):
            receipt_order = (
                "attempted_at"
                if "attempted_at" in receipt_columns
                else "publication_id"
            )
            for publication_id, response_json in conn.execute(
                f"""
                SELECT publication_id, response_json
                FROM "main"."publication_receipts"
                ORDER BY "{receipt_order}"
                """
            ).fetchall():
                receipts[str(publication_id)] = _parse_json_object(response_json)

    source_id = register_source(
        conn,
        schema,
        source_path,
        imported=True,
    )
    counts = {"confirmed": 0, "uncertain": 0}
    timestamp = now_iso()
    for row in rows:
        item = _row_dict(row, wanted)
        status = str(item.get("status") or "").casefold()
        if status == "failed" and "attempts" in queue_columns:
            try:
                attempted = int(item.get("attempts") or 0) > 0
            except (TypeError, ValueError):
                attempted = True
            if not attempted:
                continue
        publication_id = str(item.get("publication_id") or "")
        target_url = str(item.get("target_url") or "")
        post_id = str(
            item.get("content_key")
            or item.get("engage_post_id")
            or ""
        ).strip()
        if not post_id.isdigit():
            match = re.search(r"/(?:video|photo)/(\d+)", target_url)
            if match:
                post_id = match.group(1)
            else:
                match = re.search(r"(?<!\d)(\d{8,})(?!\d)", post_id)
                post_id = match.group(1) if match else ""
        if not publication_id or not post_id:
            continue
        response = receipts.get(publication_id, {})
        account = normalize_account(
            response.get("observed_account")
            or item.get("observed_account")
            or item.get("expected_account")
        )
        state = (
            "confirmed"
            if status in {"published", "deleted"}
            else "uncertain"
        )
        attempt_number = int(item.get("attempts") or 0)
        attempt_id = "legacy_" + stable_id(source_id, publication_id)
        existing_attempt = conn.execute(
            f"""
            SELECT state, attempt_id
            FROM {_table(schema, "tiktok_master_comment_attempts")}
            WHERE attempt_id=? OR (source_id=? AND publication_id=? AND local_attempt_number=?)
            """,
            (attempt_id, source_id, publication_id, attempt_number),
        ).fetchone()
        prior_state = str(existing_attempt[0] or "") if existing_attempt else ""
        if existing_attempt:
            attempt_id = str(existing_attempt[1] or attempt_id)
        inserted = existing_attempt is None
        upgraded = (
            existing_attempt is not None
            and prior_state != "confirmed"
            and state == "confirmed"
        )
        if not inserted and not upgraded:
            continue
        conn.execute(
            f"""
            INSERT INTO {_table(schema, "tiktok_master_comment_attempts")} (
                attempt_id, account_key, post_id, publication_id, source_id,
                local_run_id, target_url, text_hash, state,
                local_attempt_number, reserved_at, resolved_at,
                remote_comment_id, remote_comment_url, error, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(attempt_id) DO UPDATE SET
                state=CASE
                    WHEN state='confirmed' OR excluded.state='confirmed'
                    THEN 'confirmed' ELSE excluded.state END,
                resolved_at=excluded.resolved_at,
                remote_comment_id=CASE
                    WHEN excluded.remote_comment_id <> ''
                    THEN excluded.remote_comment_id
                    ELSE remote_comment_id END,
                remote_comment_url=CASE
                    WHEN excluded.remote_comment_url <> ''
                    THEN excluded.remote_comment_url
                    ELSE remote_comment_url END,
                error=excluded.error,
                metadata_json=excluded.metadata_json
            """,
            (
                attempt_id,
                account,
                post_id,
                publication_id,
                source_id,
                str(item.get("engage_run_id") or ""),
                target_url,
                str(item.get("draft_hash") or ""),
                state,
                int(item.get("attempts") or 0),
                str(item.get("published_at") or timestamp),
                str(item.get("published_at") or timestamp),
                str(
                    response.get("remote_comment_id")
                    or item.get("remote_comment_id")
                    or ""
                ),
                str(item.get("remote_comment_url") or ""),
                str(item.get("error") or ""),
                canonical_json({"legacy_import": True}),
            ),
        )
        conn.execute(
            f"""
            INSERT INTO {_table(schema, "tiktok_master_comment_targets")} (
                account_key, post_id, state, active_attempt_id,
                confirmed_attempt_id, first_publication_id,
                last_publication_id, canonical_url, confirmed_text_hash,
                remote_comment_id, remote_comment_url, attempt_count,
                first_attempt_at, last_attempt_at, confirmed_at, error,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
            ON CONFLICT(account_key, post_id) DO UPDATE SET
                state=CASE
                    WHEN state='confirmed' OR excluded.state='confirmed'
                    THEN 'confirmed'
                    ELSE excluded.state END,
                active_attempt_id=CASE
                    WHEN state='confirmed' OR excluded.state='confirmed'
                    THEN '' ELSE excluded.active_attempt_id END,
                confirmed_attempt_id=CASE
                    WHEN excluded.state='confirmed'
                    THEN excluded.confirmed_attempt_id
                    ELSE confirmed_attempt_id END,
                last_publication_id=excluded.last_publication_id,
                confirmed_text_hash=CASE
                    WHEN excluded.state='confirmed'
                    THEN excluded.confirmed_text_hash
                    ELSE confirmed_text_hash END,
                remote_comment_id=CASE
                    WHEN excluded.remote_comment_id <> ''
                    THEN excluded.remote_comment_id
                    ELSE remote_comment_id END,
                remote_comment_url=CASE
                    WHEN excluded.remote_comment_url <> ''
                    THEN excluded.remote_comment_url
                    ELSE remote_comment_url END,
                last_attempt_at=excluded.last_attempt_at,
                confirmed_at=CASE
                    WHEN excluded.state='confirmed' THEN excluded.confirmed_at
                    ELSE confirmed_at END,
                error=excluded.error,
                updated_at=excluded.updated_at
            """,
            (
                account,
                post_id,
                state,
                "" if state == "confirmed" else attempt_id,
                attempt_id if state == "confirmed" else "",
                publication_id,
                publication_id,
                target_url,
                str(item.get("draft_hash") or ""),
                str(
                    response.get("remote_comment_id")
                    or item.get("remote_comment_id")
                    or ""
                ),
                str(item.get("remote_comment_url") or ""),
                str(item.get("published_at") or timestamp),
                str(item.get("published_at") or timestamp),
                str(item.get("published_at") or "")
                if state == "confirmed"
                else "",
                str(item.get("error") or ""),
                timestamp,
            ),
        )
        conn.execute(
            f"""
            UPDATE {_table(schema, "tiktok_master_comment_targets")}
            SET attempt_count=(
                SELECT COUNT(*)
                FROM {_table(schema, "tiktok_master_comment_attempts")} a
                WHERE a.account_key=?
                  AND a.post_id=?
            )
            WHERE account_key=? AND post_id=?
            """,
            (account, post_id, account, post_id),
        )
        counts[state] += 1
    return counts


def sync_local_database(
    conn: sqlite3.Connection,
    schema: str,
    source_path: str | Path | None = None,
    *,
    legacy: bool = False,
) -> dict[str, int]:
    """Idempotently synchronize one local TikTok workflow DB into master."""

    source_path = source_path or _main_database_path(conn)
    has_runs = _table_exists(conn, "main", "engage_tiktok_runs")
    run_ids = (
        [
            str(row[0])
            for row in conn.execute(
                'SELECT run_id FROM "main"."engage_tiktok_runs" '
                "ORDER BY created_at"
            ).fetchall()
        ]
        if has_runs
        else []
    )
    for run_id in run_ids:
        register_run_from_local(
            conn,
            schema,
            run_id,
            source_path,
            legacy=legacy,
        )
    snapshots = 0
    if has_runs and _table_exists(conn, "main", "engage_tiktok_posts"):
        post_columns = _columns(conn, "main", "engage_tiktok_posts")
        if {
            "run_id",
            "post_id",
            "evidence_ready",
            "evidence_json",
            "evidence_hash",
        }.issubset(post_columns):
            rows = conn.execute(
                """
                SELECT run_id, post_id, evidence_json, evidence_hash
                FROM "main"."engage_tiktok_posts"
                WHERE evidence_ready=1
                ORDER BY created_at, run_id, post_id
                """
            ).fetchall()
            run_columns = _columns(
                conn,
                "main",
                "engage_tiktok_runs",
            )
            account_expressions = [
                "run_id",
                (
                    "observed_account"
                    if "observed_account" in run_columns
                    else "''"
                ),
                (
                    "expected_account"
                    if "expected_account" in run_columns
                    else "''"
                ),
            ]
            run_accounts = {
                str(row[0]): str(row[1] or row[2] or "")
                for row in conn.execute(
                    "SELECT "
                    + ", ".join(account_expressions)
                    + ' FROM "main"."engage_tiktok_runs"'
                ).fetchall()
            }
            for run_id, post_id, evidence_json, evidence_hash in rows:
                evidence = _parse_json_object(evidence_json)
                if evidence.get("evidence_ready") is not True:
                    continue
                snapshot_result = record_evidence_snapshot(
                    conn,
                    schema,
                    source_path=source_path,
                    run_id=str(run_id),
                    post_id=str(post_id),
                    evidence=evidence,
                    evidence_hash=str(evidence_hash or json_hash(evidence)),
                    account=run_accounts.get(str(run_id), ""),
                )
                if snapshot_result.get("created") is True:
                    snapshots += 1
    audit_reports = 0
    if _table_exists(conn, "main", "engage_tiktok_audit_reports"):
        report_run_ids = {
            str(row[0])
            for row in conn.execute(
                'SELECT run_id FROM "main"."engage_tiktok_audit_reports" '
                "ORDER BY run_id"
            ).fetchall()
        }
        unknown_run_ids = sorted(report_run_ids - set(run_ids))
        if unknown_run_ids:
            raise RuntimeError(
                "aggregate audit report references unknown local run: "
                + ", ".join(unknown_run_ids)
            )
        for run_id in sorted(report_run_ids):
            result = register_audit_report_from_local(
                conn,
                schema,
                run_id,
                source_path,
            )
            if result and result.get("created") is True:
                audit_reports += 1
    publication_counts = _import_legacy_publications(
        conn,
        schema,
        source_path,
    )
    return {
        "runs": len(run_ids),
        "audit_reports": audit_reports,
        "snapshots": snapshots,
        "confirmed_comments": publication_counts["confirmed"],
        "uncertain_comments": publication_counts["uncertain"],
    }


def master_summary(
    conn: sqlite3.Connection,
    schema: str = "main",
) -> dict[str, Any]:
    def count(table: str) -> int:
        return int(
            conn.execute(
                f"SELECT COUNT(*) FROM {_table(schema, table)}"
            ).fetchone()[0]
        )

    workflow_rows = conn.execute(
        f"""
        SELECT workflow, COUNT(*)
        FROM {_table(schema, "tiktok_master_runs")}
        GROUP BY workflow ORDER BY workflow
        """
    ).fetchall()
    comment_rows = conn.execute(
        f"""
        SELECT state, COUNT(*)
        FROM {_table(schema, "tiktok_master_comment_targets")}
        GROUP BY state ORDER BY state
        """
    ).fetchall()
    showcase_rows = conn.execute(
        f"""
        SELECT state, COUNT(*)
        FROM {_table(schema, "tiktok_master_showcase_targets")}
        GROUP BY state ORDER BY state
        """
    ).fetchall()
    active_collection_leases = int(
        conn.execute(
            f"""
            SELECT COUNT(*)
            FROM {_table(schema, "tiktok_master_collection_leases")}
            WHERE julianday(expires_at) > julianday(?)
            """,
            (canonical_timestamp(now_iso()),),
        ).fetchone()[0]
    )
    return {
        "schema_version": MASTER_SCHEMA_VERSION,
        "sources": count("tiktok_master_sources"),
        "runs": count("tiktok_master_runs"),
        "runs_by_workflow": {
            str(row[0]): int(row[1]) for row in workflow_rows
        },
        "audit_reports": count("tiktok_master_audit_reports"),
        "music_backfill_runs": count("tiktok_master_music_backfill_runs"),
        "music_backfill_observations": count(
            "tiktok_master_music_backfill_observations"
        ),
        "unique_posts": count("tiktok_master_posts"),
        "snapshots": count("tiktok_master_snapshots"),
        "known_comments": count("tiktok_master_comments"),
        "active_collection_leases": active_collection_leases,
        "comment_targets_by_state": {
            str(row[0]): int(row[1]) for row in comment_rows
        },
        "showcase_attempts": count("tiktok_master_showcase_attempts"),
        "showcase_targets_by_state": {
            str(row[0]): int(row[1]) for row in showcase_rows
        },
        "generated_at": now_iso(),
    }


def connect_master(path: str | Path = DEFAULT_MASTER_DATABASE) -> sqlite3.Connection:
    database = Path(path).resolve()
    database.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(database, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    ensure_master_schema(conn, "main")
    conn.commit()
    return conn


def import_legacy_database(
    master_database: str | Path,
    source_database: str | Path,
) -> dict[str, Any]:
    source = Path(source_database).resolve()
    master = Path(master_database).resolve()
    if source == master:
        raise ValueError("source database cannot be the master database")
    if not source.is_file():
        raise FileNotFoundError(source)
    conn = sqlite3.connect(source, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        schema = attach_master_database(conn, master)
        result = sync_local_database(
            conn,
            schema,
            source,
            legacy=True,
        )
        conn.commit()
        return {"source": str(source), **result}
    finally:
        conn.close()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Workspace-wide TikTok LISTEN/AUDIT/ENGAGE master database"
        )
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_MASTER_DATABASE,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status")
    importer = subparsers.add_parser("import")
    importer.add_argument(
        "--source-database",
        type=Path,
        action="append",
        required=True,
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "status":
        conn = connect_master(args.database)
        try:
            result: Any = master_summary(conn)
        finally:
            conn.close()
    else:
        result = {
            "master_database": str(Path(args.database).resolve()),
            "imports": [
                import_legacy_database(args.database, source)
                for source in args.source_database
            ],
        }
        conn = connect_master(args.database)
        try:
            result["summary"] = master_summary(conn)
        finally:
            conn.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
