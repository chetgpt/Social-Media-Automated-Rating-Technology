"""Durable platform-neutral LISTEN state for social music evidence.

This module deliberately owns collection state only.  It has no analysis,
drafting, approval, publication, browser, or platform-client behavior.  A
workspace can place the database wherever it keeps durable collection state;
the tables use a distinct ``social_music_`` prefix so existing databases are
left intact.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence


SCHEMA_VERSION = "social-music-state-v1"
WORKFLOW = "listen"
SOURCE_MODES = frozenset({"topic", "creator", "url"})
RUN_STATUSES = frozenset(
    {"collecting", "collection_complete", "collection_incomplete"}
)

_HASH_RE = re.compile(r"[0-9a-f]{64}")
_PLATFORM_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


class SocialMusicStateError(RuntimeError):
    """Base class for fail-closed social music state errors."""


class InvalidStateInput(SocialMusicStateError, ValueError):
    """A run scope, checkpoint, or finalization input is invalid."""


class ImmutableScopeConflict(SocialMusicStateError):
    """A caller attempted to reuse an identity with different immutable data."""


class CheckpointConflict(SocialMusicStateError):
    """A checkpoint conflicts with an existing ordinal or content identity."""


class StateIntegrityError(SocialMusicStateError):
    """Stored state does not satisfy its hash or relational bindings."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise InvalidStateInput("value is not canonical JSON") from exc


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _text(value: Any, *, name: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise InvalidStateInput(f"{name} is required")
    return result


def _platform(value: Any) -> str:
    result = _text(value, name="platform").casefold()
    if _PLATFORM_RE.fullmatch(result) is None:
        raise InvalidStateInput("platform is invalid")
    return result


def _content_id(value: Any) -> str:
    result = _text(value, name="content_id")
    if len(result) > 512 or any(character.isspace() for character in result):
        raise InvalidStateInput("content_id is invalid")
    return result


def _positive_integer(value: Any, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidStateInput(f"{name} must be a positive integer")
    result = int(value)
    if result < 1:
        raise InvalidStateInput(f"{name} must be a positive integer")
    return result


def _sha256(value: Any, *, name: str) -> str:
    result = str(value or "").strip().casefold()
    if _HASH_RE.fullmatch(result) is None:
        raise InvalidStateInput(f"{name} must be a lowercase SHA-256 hex digest")
    return result


def _verify_evidence_identity(
    evidence: Mapping[str, Any], *, platform: str, content_id: str
) -> None:
    """Require the closed CLI envelope's nested identity binding.

    Top-level identity hints are intentionally insufficient: the durable
    record consumed by export carries its binding under ``identity``.
    """

    identity = evidence.get("identity")
    if not isinstance(identity, Mapping):
        raise StateIntegrityError("checkpoint evidence identity is required")
    supplied_platform = identity.get("platform")
    supplied_content_id = identity.get("content_id")
    if supplied_platform in (None, "") or supplied_content_id in (None, ""):
        raise StateIntegrityError(
            "checkpoint evidence identity platform/content_id are required"
        )
    if _platform(supplied_platform) != platform:
        raise StateIntegrityError("evidence nested platform binding mismatch")
    if _content_id(supplied_content_id) != content_id:
        raise StateIntegrityError("evidence nested content_id binding mismatch")


def _scope_body(
    *,
    run_id: str,
    platform: str,
    source_mode: str,
    target: str,
    requested_count: int,
    capability_manifest_hash: str,
    adapter_version: str,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "workflow": WORKFLOW,
        "platform": platform,
        "source_mode": source_mode,
        "target": target,
        "requested_count": requested_count,
        "capability_manifest_hash": capability_manifest_hash,
        "adapter_version": adapter_version,
    }


def _normalize_reasons(reasons: str | Iterable[str] | None) -> list[str]:
    if reasons is None:
        return []
    values: Iterable[str] = [reasons] if isinstance(reasons, str) else reasons
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        reason = str(value or "").strip()
        if not reason:
            raise InvalidStateInput("finalization reasons cannot be empty")
        if len(reason) > 500:
            raise InvalidStateInput("finalization reason is too long")
        if reason not in seen:
            seen.add(reason)
            result.append(reason)
    return result


class SocialMusicState:
    """SQLite-backed, collection-only state for platform-neutral LISTEN runs."""

    def __init__(self, database: str | Path) -> None:
        database_text = str(database)
        if database_text != ":memory:":
            path = Path(database).expanduser().resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            database_text = str(path)
        self.database = database_text
        self._connection = sqlite3.connect(
            database_text,
            timeout=30.0,
            isolation_level=None,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute("PRAGMA busy_timeout=30000")
        self._initialize_schema()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "SocialMusicState":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        del exc_type, exc, traceback
        self.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()

    def _initialize_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS social_music_runs (
                run_id TEXT PRIMARY KEY,
                schema_version TEXT NOT NULL CHECK(schema_version='social-music-state-v1'),
                workflow TEXT NOT NULL CHECK(workflow='listen'),
                platform TEXT NOT NULL,
                source_mode TEXT NOT NULL CHECK(source_mode IN ('topic','creator','url')),
                target TEXT NOT NULL,
                requested_count INTEGER NOT NULL CHECK(requested_count > 0),
                capability_manifest_hash TEXT NOT NULL,
                adapter_version TEXT NOT NULL,
                scope_hash TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN (
                    'collecting','collection_complete','collection_incomplete'
                )),
                incomplete_reasons_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS social_music_records (
                run_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                content_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL CHECK(ordinal > 0),
                evidence_json TEXT NOT NULL,
                evidence_hash TEXT NOT NULL,
                evidence_ready INTEGER NOT NULL CHECK(evidence_ready IN (0,1)),
                collection_error TEXT NOT NULL DEFAULT '',
                checkpointed_at TEXT NOT NULL,
                PRIMARY KEY (run_id, platform, content_id),
                UNIQUE (run_id, ordinal),
                FOREIGN KEY (run_id) REFERENCES social_music_runs(run_id)
                    ON DELETE RESTRICT
            );

            CREATE TABLE IF NOT EXISTS social_music_known_content (
                platform TEXT NOT NULL,
                content_id TEXT NOT NULL,
                first_run_id TEXT NOT NULL,
                first_evidence_hash TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                PRIMARY KEY (platform, content_id),
                FOREIGN KEY (first_run_id) REFERENCES social_music_runs(run_id)
                    ON DELETE RESTRICT
            );

            CREATE INDEX IF NOT EXISTS social_music_records_run_ready_idx
                ON social_music_records(run_id, evidence_ready, ordinal);

            CREATE TRIGGER IF NOT EXISTS social_music_runs_scope_immutable
            BEFORE UPDATE OF
                run_id, schema_version, workflow, platform, source_mode, target,
                requested_count, capability_manifest_hash, adapter_version,
                scope_hash
            ON social_music_runs
            BEGIN
                SELECT RAISE(ABORT, 'social music run scope is immutable');
            END;

            CREATE TRIGGER IF NOT EXISTS social_music_records_immutable
            BEFORE UPDATE ON social_music_records
            BEGIN
                SELECT RAISE(ABORT, 'social music checkpoints are immutable');
            END;

            CREATE TRIGGER IF NOT EXISTS social_music_known_content_immutable
            BEFORE UPDATE ON social_music_known_content
            BEGIN
                SELECT RAISE(ABORT, 'social music known content is immutable');
            END;

            CREATE TRIGGER IF NOT EXISTS social_music_runs_delete_guard
            BEFORE DELETE ON social_music_runs
            BEGIN
                SELECT RAISE(ABORT, 'social music run is immutable');
            END;

            CREATE TRIGGER IF NOT EXISTS social_music_records_delete_guard
            BEFORE DELETE ON social_music_records
            BEGIN
                SELECT RAISE(ABORT, 'social music checkpoint is immutable');
            END;

            CREATE TRIGGER IF NOT EXISTS social_music_known_content_delete_guard
            BEFORE DELETE ON social_music_known_content
            BEGIN
                SELECT RAISE(ABORT, 'social music known content is immutable');
            END;
            """
        )

    def create_run(
        self,
        run_id: str,
        *,
        platform: str,
        source_mode: str,
        target: str,
        requested_count: int,
        capability_manifest_hash: str,
        adapter_version: str,
    ) -> dict[str, Any]:
        normalized_run_id = _text(run_id, name="run_id")
        if len(normalized_run_id) > 200 or any(
            character.isspace() for character in normalized_run_id
        ):
            raise InvalidStateInput("run_id is invalid")
        normalized_platform = _platform(platform)
        normalized_source = _text(source_mode, name="source_mode").casefold()
        if normalized_source not in SOURCE_MODES:
            raise InvalidStateInput("source_mode must be topic, creator, or url")
        normalized_target = _text(target, name="target")
        normalized_count = _positive_integer(
            requested_count, name="requested_count"
        )
        if normalized_source == "url" and normalized_count != 1:
            raise InvalidStateInput("url source_mode requires requested_count=1")
        normalized_manifest_hash = _sha256(
            capability_manifest_hash, name="capability_manifest_hash"
        )
        normalized_adapter = _text(adapter_version, name="adapter_version")
        body = _scope_body(
            run_id=normalized_run_id,
            platform=normalized_platform,
            source_mode=normalized_source,
            target=normalized_target,
            requested_count=normalized_count,
            capability_manifest_hash=normalized_manifest_hash,
            adapter_version=normalized_adapter,
        )
        scope_hash = canonical_sha256(body)
        timestamp = utc_now()
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM social_music_runs WHERE run_id=?",
                (normalized_run_id,),
            ).fetchone()
            if existing is not None:
                self._verify_run_row(existing)
                if existing["scope_hash"] != scope_hash:
                    raise ImmutableScopeConflict(
                        "run_id already exists with a different immutable scope"
                    )
                return self._status_from_connection(connection, normalized_run_id)
            connection.execute(
                """
                INSERT INTO social_music_runs (
                    run_id, schema_version, workflow, platform, source_mode,
                    target, requested_count, capability_manifest_hash,
                    adapter_version, scope_hash, status,
                    incomplete_reasons_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'collecting', '[]', ?, ?)
                """,
                (
                    normalized_run_id,
                    SCHEMA_VERSION,
                    WORKFLOW,
                    normalized_platform,
                    normalized_source,
                    normalized_target,
                    normalized_count,
                    normalized_manifest_hash,
                    normalized_adapter,
                    scope_hash,
                    timestamp,
                    timestamp,
                ),
            )
            return self._status_from_connection(connection, normalized_run_id)

    def _verify_run_row(self, row: sqlite3.Row) -> None:
        if row["schema_version"] != SCHEMA_VERSION or row["workflow"] != WORKFLOW:
            raise StateIntegrityError("run schema/workflow binding is invalid")
        if row["status"] not in RUN_STATUSES:
            raise StateIntegrityError("run status is invalid")
        body = _scope_body(
            run_id=str(row["run_id"]),
            platform=str(row["platform"]),
            source_mode=str(row["source_mode"]),
            target=str(row["target"]),
            requested_count=int(row["requested_count"]),
            capability_manifest_hash=str(row["capability_manifest_hash"]),
            adapter_version=str(row["adapter_version"]),
        )
        if canonical_sha256(body) != row["scope_hash"]:
            raise StateIntegrityError("run scope hash mismatch")

    def _run_row(self, connection: sqlite3.Connection, run_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM social_music_runs WHERE run_id=?",
            (_text(run_id, name="run_id"),),
        ).fetchone()
        if row is None:
            raise InvalidStateInput("run_id was not found")
        self._verify_run_row(row)
        return row

    def get_run(self, run_id: str) -> dict[str, Any]:
        return self._status_from_connection(self._connection, run_id)

    status = get_run

    def _record_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        try:
            evidence = json.loads(row["evidence_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise StateIntegrityError("checkpoint evidence JSON is invalid") from exc
        if not isinstance(evidence, dict):
            raise StateIntegrityError("checkpoint evidence must be a JSON object")
        if canonical_sha256(evidence) != row["evidence_hash"]:
            raise StateIntegrityError("checkpoint evidence hash mismatch")
        _verify_evidence_identity(
            evidence,
            platform=str(row["platform"]),
            content_id=str(row["content_id"]),
        )
        return {
            "run_id": str(row["run_id"]),
            "platform": str(row["platform"]),
            "content_id": str(row["content_id"]),
            "ordinal": int(row["ordinal"]),
            "evidence": evidence,
            "evidence_json": str(row["evidence_json"]),
            "evidence_hash": str(row["evidence_hash"]),
            "evidence_ready": bool(row["evidence_ready"]),
            "collection_error": str(row["collection_error"]),
            "checkpointed_at": str(row["checkpointed_at"]),
        }

    def records(self, run_id: str) -> list[dict[str, Any]]:
        row = self._run_row(self._connection, run_id)
        stored = self._connection.execute(
            """
            SELECT * FROM social_music_records
            WHERE run_id=? ORDER BY ordinal, content_id
            """,
            (row["run_id"],),
        ).fetchall()
        result = [self._record_from_row(item) for item in stored]
        if any(item["platform"] != row["platform"] for item in result):
            raise StateIntegrityError("checkpoint platform/run binding mismatch")
        return result

    def checkpoint_record(
        self,
        run_id: str,
        *,
        ordinal: int,
        content_id: str,
        evidence: Mapping[str, Any],
        evidence_hash: str | None = None,
        evidence_ready: bool = True,
        collection_error: str = "",
    ) -> dict[str, Any]:
        normalized_ordinal = _positive_integer(ordinal, name="ordinal")
        normalized_content_id = _content_id(content_id)
        if not isinstance(evidence, Mapping):
            raise InvalidStateInput("evidence must be a JSON object")
        evidence_value = json.loads(canonical_json(dict(evidence)))
        calculated_hash = canonical_sha256(evidence_value)
        if evidence_hash is not None and _sha256(
            evidence_hash, name="evidence_hash"
        ) != calculated_hash:
            raise StateIntegrityError("supplied evidence hash mismatch")
        if not isinstance(evidence_ready, bool):
            raise InvalidStateInput("evidence_ready must be boolean")
        normalized_error = str(collection_error or "").strip()
        if len(normalized_error) > 1000:
            raise InvalidStateInput("collection_error is too long")

        with self._transaction() as connection:
            run = self._run_row(connection, run_id)
            _verify_evidence_identity(
                evidence_value,
                platform=str(run["platform"]),
                content_id=normalized_content_id,
            )

            existing_content = connection.execute(
                """
                SELECT * FROM social_music_records
                WHERE run_id=? AND platform=? AND content_id=?
                """,
                (run["run_id"], run["platform"], normalized_content_id),
            ).fetchone()
            existing_ordinal = connection.execute(
                """
                SELECT * FROM social_music_records
                WHERE run_id=? AND ordinal=?
                """,
                (run["run_id"], normalized_ordinal),
            ).fetchone()
            existing = existing_content or existing_ordinal
            if existing is not None:
                same = (
                    existing_content is not None
                    and existing_ordinal is not None
                    and existing_content["content_id"]
                    == existing_ordinal["content_id"]
                    and int(existing["ordinal"]) == normalized_ordinal
                    and existing["evidence_hash"] == calculated_hash
                    and bool(existing["evidence_ready"]) is evidence_ready
                    and existing["collection_error"] == normalized_error
                )
                if not same:
                    raise CheckpointConflict(
                        "content_id or ordinal already has a different checkpoint"
                    )
                result = self._record_from_row(existing)
                result["inserted"] = False
                return result

            if run["status"] != "collecting":
                raise CheckpointConflict("terminal runs cannot accept checkpoints")

            ready_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM social_music_records
                    WHERE run_id=? AND evidence_ready=1
                    """,
                    (run["run_id"],),
                ).fetchone()[0]
            )
            if ready_count >= int(run["requested_count"]):
                raise CheckpointConflict(
                    "requested evidence-ready cardinality is already satisfied"
                )
            known = connection.execute(
                """
                SELECT first_run_id FROM social_music_known_content
                WHERE platform=? AND content_id=?
                """,
                (run["platform"], normalized_content_id),
            ).fetchone()
            if known is not None and known["first_run_id"] != run["run_id"]:
                raise CheckpointConflict(
                    "content is already known in the workspace-global registry"
                )

            timestamp = utc_now()
            connection.execute(
                """
                INSERT INTO social_music_records (
                    run_id, platform, content_id, ordinal, evidence_json,
                    evidence_hash, evidence_ready, collection_error,
                    checkpointed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run["run_id"],
                    run["platform"],
                    normalized_content_id,
                    normalized_ordinal,
                    canonical_json(evidence_value),
                    calculated_hash,
                    int(evidence_ready),
                    normalized_error,
                    timestamp,
                ),
            )
            if evidence_ready:
                connection.execute(
                    """
                    INSERT INTO social_music_known_content (
                        platform, content_id, first_run_id,
                        first_evidence_hash, first_seen_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        run["platform"],
                        normalized_content_id,
                        run["run_id"],
                        calculated_hash,
                        timestamp,
                    ),
                )
            inserted = connection.execute(
                """
                SELECT * FROM social_music_records
                WHERE run_id=? AND platform=? AND content_id=?
                """,
                (run["run_id"], run["platform"], normalized_content_id),
            ).fetchone()
            if inserted is None:
                raise StateIntegrityError("checkpoint insert was not durable")
            result = self._record_from_row(inserted)
            result["inserted"] = True
            return result

    append_checkpoint = checkpoint_record
    checkpoint = checkpoint_record

    def known_content_ids(
        self, platform: str, content_ids: Iterable[str] | None = None
    ) -> set[str]:
        normalized_platform = _platform(platform)
        if content_ids is None:
            rows = self._connection.execute(
                """
                SELECT content_id FROM social_music_known_content
                WHERE platform=? ORDER BY content_id
                """,
                (normalized_platform,),
            ).fetchall()
            return {str(row["content_id"]) for row in rows}
        normalized = [_content_id(value) for value in content_ids]
        if not normalized:
            return set()
        known: set[str] = set()
        # Keep the variable count comfortably below SQLite's common limit.
        for offset in range(0, len(normalized), 500):
            batch = normalized[offset : offset + 500]
            placeholders = ",".join("?" for _ in batch)
            rows = self._connection.execute(
                f"""
                SELECT content_id FROM social_music_known_content
                WHERE platform=? AND content_id IN ({placeholders})
                """,
                (normalized_platform, *batch),
            ).fetchall()
            known.update(str(row["content_id"]) for row in rows)
        return known

    def is_known_content(self, platform: str, content_id: str) -> bool:
        return _content_id(content_id) in self.known_content_ids(
            platform, [_content_id(content_id)]
        )

    def filter_unknown_content(
        self, platform: str, content_ids: Iterable[str]
    ) -> list[str]:
        ordered: list[str] = []
        seen: set[str] = set()
        for value in content_ids:
            content_id = _content_id(value)
            if content_id not in seen:
                seen.add(content_id)
                ordered.append(content_id)
        known = self.known_content_ids(platform, ordered)
        return [content_id for content_id in ordered if content_id not in known]

    exclude_known_content = filter_unknown_content

    def _status_from_connection(
        self, connection: sqlite3.Connection, run_id: str
    ) -> dict[str, Any]:
        run = self._run_row(connection, run_id)
        counts = connection.execute(
            """
            SELECT
                COUNT(*) AS checkpointed,
                COALESCE(SUM(evidence_ready), 0) AS evidence_ready,
                COUNT(DISTINCT CASE WHEN evidence_ready=1 THEN content_id END)
                    AS unique_evidence_ready
            FROM social_music_records WHERE run_id=?
            """,
            (run["run_id"],),
        ).fetchone()
        ready = int(counts["evidence_ready"])
        unique_ready = int(counts["unique_evidence_ready"])
        if ready != unique_ready:
            raise StateIntegrityError("evidence-ready checkpoint identities are not unique")
        try:
            reasons = json.loads(run["incomplete_reasons_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise StateIntegrityError("run finalization reasons are invalid") from exc
        if not isinstance(reasons, list) or not all(
            isinstance(value, str) and value for value in reasons
        ):
            raise StateIntegrityError("run finalization reasons are invalid")
        requested = int(run["requested_count"])
        status = str(run["status"])
        if status == "collection_complete" and ready != requested:
            raise StateIntegrityError("complete run does not have exact cardinality")
        if status == "collection_incomplete" and ready == requested:
            raise StateIntegrityError("incomplete run has complete cardinality")
        return {
            "schema_version": str(run["schema_version"]),
            "run_id": str(run["run_id"]),
            "workflow": str(run["workflow"]),
            "platform": str(run["platform"]),
            "source_mode": str(run["source_mode"]),
            "target": str(run["target"]),
            "requested_count": requested,
            "capability_manifest_hash": str(run["capability_manifest_hash"]),
            "adapter_version": str(run["adapter_version"]),
            "scope_hash": str(run["scope_hash"]),
            "status": status,
            "checkpointed": int(counts["checkpointed"]),
            "evidence_ready": ready,
            "not_ready": int(counts["checkpointed"]) - ready,
            "pending": max(0, requested - ready),
            "reasons": reasons,
            "created_at": str(run["created_at"]),
            "updated_at": str(run["updated_at"]),
            "status_summary": (
                status
                if status == "collecting"
                else f"{status}: {ready}/{requested}"
            ),
        }

    def finalize(
        self,
        run_id: str,
        *,
        reasons: str | Sequence[str] | None = None,
    ) -> dict[str, Any]:
        normalized_reasons = _normalize_reasons(reasons)
        with self._transaction() as connection:
            run = self._run_row(connection, run_id)
            before = self._status_from_connection(connection, run["run_id"])
            if run["status"] != "collecting":
                if (
                    run["status"] == "collection_incomplete"
                    and normalized_reasons
                    and before["reasons"] != normalized_reasons
                ):
                    raise ImmutableScopeConflict(
                        "terminal finalization reasons are immutable"
                    )
                return before

            # Verify every stored JSON/hash before changing the terminal state.
            records = connection.execute(
                "SELECT * FROM social_music_records WHERE run_id=?",
                (run["run_id"],),
            ).fetchall()
            for record in records:
                parsed = self._record_from_row(record)
                if parsed["platform"] != run["platform"]:
                    raise StateIntegrityError(
                        "checkpoint platform/run binding mismatch"
                    )

            ready = int(before["evidence_ready"])
            requested = int(before["requested_count"])
            if ready > requested:
                raise StateIntegrityError(
                    "evidence-ready cardinality exceeds immutable request"
                )
            if ready == requested:
                terminal_status = "collection_complete"
                terminal_reasons: list[str] = []
            else:
                terminal_status = "collection_incomplete"
                terminal_reasons = normalized_reasons or [
                    "insufficient_evidence_ready_records"
                ]
            connection.execute(
                """
                UPDATE social_music_runs
                SET status=?, incomplete_reasons_json=?, updated_at=?
                WHERE run_id=? AND status='collecting'
                """,
                (
                    terminal_status,
                    canonical_json(terminal_reasons),
                    utc_now(),
                    run["run_id"],
                ),
            )
            result = self._status_from_connection(connection, run["run_id"])
            if result["status"] != terminal_status:
                raise StateIntegrityError("run finalization did not commit")
            return result


__all__ = [
    "CheckpointConflict",
    "ImmutableScopeConflict",
    "InvalidStateInput",
    "SCHEMA_VERSION",
    "SOURCE_MODES",
    "SocialMusicState",
    "SocialMusicStateError",
    "StateIntegrityError",
    "WORKFLOW",
    "canonical_json",
    "canonical_sha256",
    "utc_now",
]
