"""Durable link-discovery state for TikTok One's Top Content list.

This module stores link discovery only.  It deliberately does not collect post
evidence, read the TikTok master database, start a browser, or execute a LISTEN
run.  A caller may freeze provenance for a master-database comparison and may
track separately executed LISTEN URL child runs after discovery completes.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit


SCHEMA_VERSION = "tiktok-one-top-content-links-state-v1"
EXPORT_SCHEMA_VERSION = "tiktok-one-top-content-links-export-v1"
RUN_STATUSES = frozenset({"collecting", "links_complete", "links_incomplete"})
COLLECTION_POLICIES = frozenset({"all_links", "new_only_against_tiktok_master"})
LISTEN_JOB_STATUSES = frozenset(
    {
        "pending",
        "running",
        "complete",
        "incomplete",
        "blocked",
        "skipped_known",
    }
)

_TOP_CONTENT_PATH = "/creative/forpartners/creator/top-content"
_HASH_RE = re.compile(r"[0-9a-f]{64}")
_REGION_RE = re.compile(r"[a-z]{2,8}")
_VIDEO_ID_RE = re.compile(r"[0-9]+")
_CREATOR_RE = re.compile(r"[A-Za-z0-9._]{1,64}")
_VIDEO_PATH_RE = re.compile(
    r"/@(?P<creator>[A-Za-z0-9._]{1,64})/video/(?P<video_id>[0-9]+)/?"
)


class TopContentStateError(RuntimeError):
    """Base class for fail-closed Top Content state errors."""


class InvalidTopContentInput(TopContentStateError, ValueError):
    """A caller supplied an invalid state value."""


class TopContentRunNotFound(InvalidTopContentInput):
    """The requested run does not exist in this database."""


class TopContentStateConflict(TopContentStateError):
    """A requested mutation conflicts with frozen or terminal state."""


class TopContentStateIntegrityError(TopContentStateError):
    """Stored state no longer satisfies its deterministic hash bindings."""


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso(value: dt.datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat()


def canonical_json(value: Any) -> str:
    """Return deterministic UTF-8 JSON suitable for a hash binding."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise InvalidTopContentInput("value is not canonical JSON") from exc


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _required_text(value: Any, name: str, *, limit: int = 1_000) -> str:
    result = str(value or "").strip()
    if not result or len(result) > limit:
        raise InvalidTopContentInput(f"{name} is invalid")
    return result


def _run_id(value: Any) -> str:
    result = _required_text(value, "run_id", limit=200)
    if any(character.isspace() for character in result):
        raise InvalidTopContentInput("run_id cannot contain whitespace")
    return result


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise InvalidTopContentInput(f"{name} must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidTopContentInput(f"{name} must be a positive integer") from exc
    if result < 1:
        raise InvalidTopContentInput(f"{name} must be a positive integer")
    return result


def _nonnegative_int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise InvalidTopContentInput(f"{name} must be a nonnegative integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidTopContentInput(f"{name} must be a nonnegative integer") from exc
    if result < 0:
        raise InvalidTopContentInput(f"{name} must be a nonnegative integer")
    return result


def _json_copy(value: Any, name: str) -> Any:
    try:
        return json.loads(canonical_json(value))
    except json.JSONDecodeError as exc:  # pragma: no cover - json.dumps guards this
        raise InvalidTopContentInput(f"{name} is not valid JSON") from exc


def _mapping(value: Any, name: str) -> dict[str, Any]:
    result = _json_copy(value, name)
    if not isinstance(result, dict):
        raise InvalidTopContentInput(f"{name} must be a mapping")
    return result


def _ranking_order(value: Any) -> Any:
    result = _json_copy(value, "ranking_order")
    if result in (None, "", [], {}):
        raise InvalidTopContentInput("ranking_order cannot be empty")
    return result


def _reasons(value: Sequence[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        raise InvalidTopContentInput("reasons must be a sequence of strings")
    output: list[str] = []
    seen: set[str] = set()
    for raw in value:
        reason = _required_text(raw, "reason", limit=2_000)
        if reason not in seen:
            seen.add(reason)
            output.append(reason)
    return output


def normalize_source_url(value: Any) -> str:
    """Normalize the one supported TikTok One Top Content page URL."""

    raw = _required_text(value, "source_url", limit=2_000)
    try:
        parsed = urlsplit(raw)
        invalid_port = parsed.port not in (None, 443)
    except ValueError as exc:
        raise InvalidTopContentInput("source_url is invalid") from exc
    path = parsed.path.rstrip("/")
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() != "ads.tiktok.com"
        or parsed.username is not None
        or parsed.password is not None
        or invalid_port
        or path != _TOP_CONTENT_PATH
        or parsed.fragment
        or "#" in raw
    ):
        raise InvalidTopContentInput(
            "source_url must be the TikTok One Top Content HTTPS page"
        )
    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    if len(query_pairs) > 1:
        raise InvalidTopContentInput(
            "source_url may contain only one region query value"
        )
    region = "row"
    if query_pairs:
        key, value = query_pairs[0]
        if key.strip().casefold() != "region":
            raise InvalidTopContentInput(
                "source_url may contain only the region query key"
            )
        region = value.strip().casefold()
        if not _REGION_RE.fullmatch(region):
            raise InvalidTopContentInput("source_url region is invalid")
    return urlunsplit(
        (
            "https",
            "ads.tiktok.com",
            _TOP_CONTENT_PATH,
            urlencode({"region": region}),
            "",
        )
    )


def normalize_video_id(value: Any) -> str:
    result = _required_text(value, "video_id", limit=200)
    if _VIDEO_ID_RE.fullmatch(result) is None:
        raise InvalidTopContentInput("video_id must be a numeric TikTok video ID")
    return result


def normalize_creator(value: Any) -> str:
    result = _required_text(value, "creator", limit=65).lstrip("@").casefold()
    if _CREATOR_RE.fullmatch(result) is None:
        raise InvalidTopContentInput("creator is not a valid TikTok handle")
    return result


def normalize_video_url(value: Any, *, video_id: str, creator: str) -> str:
    """Validate and return a tracking-free canonical TikTok video URL."""

    raw = _required_text(value, "canonical_url", limit=2_000)
    try:
        parsed = urlsplit(raw)
        invalid_port = parsed.port not in (None, 443)
    except ValueError as exc:
        raise InvalidTopContentInput("canonical_url is invalid") from exc
    match = _VIDEO_PATH_RE.fullmatch(unquote(parsed.path))
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() not in {"tiktok.com", "www.tiktok.com"}
        or parsed.username is not None
        or parsed.password is not None
        or invalid_port
        or match is None
    ):
        raise InvalidTopContentInput("canonical_url must be a TikTok HTTPS video URL")
    path_creator = normalize_creator(match.group("creator"))
    path_video_id = normalize_video_id(match.group("video_id"))
    if path_creator != creator or path_video_id != video_id:
        raise InvalidTopContentInput(
            "canonical_url must match the supplied creator and video_id"
        )
    return f"https://www.tiktok.com/@{creator}/video/{video_id}"


def _normalized_master_path(value: Any) -> str:
    raw = _required_text(value, "master_database_path", limit=4_000)
    if raw == ":memory:":
        raise InvalidTopContentInput("master_database_path must identify a file")
    return str(Path(raw).expanduser().resolve())


def _immutable_scope_document(
    *,
    run_id: str,
    source_url: str,
    requested_count: int,
    collection_policy: str,
    scope: Mapping[str, Any],
    filter_snapshot: Mapping[str, Any],
    list_snapshot: Mapping[str, Any],
    account_binding: Mapping[str, Any],
    ranking_order: Any,
    master_database_identity: str,
    master_database_hash: str,
    master_database_path: str,
    known_excluded_count: int,
    known_excluded_provenance: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "platform": "tiktok_one_top_content",
        "run_id": run_id,
        "source_url": source_url,
        "requested_count": requested_count,
        "collection_policy": collection_policy,
        "scope": dict(scope),
        "filter_snapshot": dict(filter_snapshot),
        "list_snapshot": dict(list_snapshot),
        "account_binding": dict(account_binding),
        "ranking_order": ranking_order,
        "master_database": {
            "identity": master_database_identity,
            "sha256": master_database_hash,
            "path": master_database_path,
        },
        "known_excluded": {
            "count": known_excluded_count,
            "provenance": dict(known_excluded_provenance),
        },
    }


def _link_document(
    *,
    run_id: str,
    ordinal: int,
    video_id: str,
    canonical_url: str,
    creator: str,
    ranking_key: str,
    ranking_position: int,
) -> dict[str, Any]:
    return {
        "schema_version": "tiktok-one-top-content-link-v1",
        "run_id": run_id,
        "ordinal": ordinal,
        "video_id": video_id,
        "canonical_url": canonical_url,
        "creator": creator,
        "ranking_key": ranking_key,
        "ranking_position": ranking_position,
    }


def _failure_document(
    *,
    run_id: str,
    sequence: int,
    stage: str,
    reason: str,
    context: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": "tiktok-one-top-content-discovery-failure-v1",
        "run_id": run_id,
        "sequence": sequence,
        "stage": stage,
        "reason": reason,
        "context": dict(context),
    }


def _job_document(
    *,
    run_id: str,
    video_id: str,
    ordinal: int,
    canonical_url: str,
    project: str,
    status: str,
    child_run_id: str,
    child_database: str,
    reason: str,
) -> dict[str, Any]:
    return {
        "schema_version": "tiktok-one-top-content-listen-job-v1",
        "run_id": run_id,
        "video_id": video_id,
        "ordinal": ordinal,
        "canonical_url": canonical_url,
        "project": project,
        "status": status,
        "child_run_id": child_run_id,
        "child_database": child_database,
        "reason": reason,
    }


def _state_document(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": str(row["run_id"]),
        "status": str(row["status"]),
        "incomplete_reasons": json.loads(str(row["incomplete_reasons_json"])),
        "link_count": int(row["link_count"]),
        "links_manifest_hash": str(row["links_manifest_hash"]),
        "failure_count": int(row["failure_count"]),
        "failures_manifest_hash": str(row["failures_manifest_hash"]),
        "listen_jobs_initialized": bool(row["listen_jobs_initialized"]),
        "listen_project": str(row["listen_project"]),
        "listen_settings": json.loads(str(row["listen_settings_json"])),
        "listen_settings_hash": str(row["listen_settings_hash"]),
        "listen_jobs_manifest_hash": str(row["listen_jobs_manifest_hash"]),
    }


class TopContentLinkState:
    """SQLite-backed state for durable Top Content link discovery."""

    def __init__(self, database: str | Path) -> None:
        database_text = str(database)
        self._file_backed = database_text != ":memory:"
        if self._file_backed:
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
        if self._file_backed:
            self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._ensure_schema()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "TopContentLinkState":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield self._connection
        except Exception:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

    def _ensure_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS top_content_link_runs (
                run_id TEXT PRIMARY KEY,
                schema_version TEXT NOT NULL,
                platform TEXT NOT NULL,
                source_url TEXT NOT NULL,
                requested_count INTEGER NOT NULL,
                collection_policy TEXT NOT NULL,
                scope_json TEXT NOT NULL,
                scope_document_hash TEXT NOT NULL,
                filter_snapshot_json TEXT NOT NULL,
                filter_snapshot_hash TEXT NOT NULL,
                list_snapshot_json TEXT NOT NULL,
                list_snapshot_hash TEXT NOT NULL,
                account_binding_json TEXT NOT NULL,
                account_binding_hash TEXT NOT NULL,
                ranking_order_json TEXT NOT NULL,
                ranking_order_hash TEXT NOT NULL,
                master_database_identity TEXT NOT NULL DEFAULT '',
                master_database_hash TEXT NOT NULL DEFAULT '',
                master_database_path TEXT NOT NULL DEFAULT '',
                known_excluded_count INTEGER NOT NULL DEFAULT 0,
                known_excluded_provenance_json TEXT NOT NULL DEFAULT '{}',
                known_excluded_provenance_hash TEXT NOT NULL,
                immutable_scope_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                incomplete_reasons_json TEXT NOT NULL DEFAULT '[]',
                link_count INTEGER NOT NULL DEFAULT 0,
                links_manifest_hash TEXT NOT NULL,
                failure_count INTEGER NOT NULL DEFAULT 0,
                failures_manifest_hash TEXT NOT NULL,
                listen_jobs_initialized INTEGER NOT NULL DEFAULT 0,
                listen_project TEXT NOT NULL DEFAULT '',
                listen_settings_json TEXT NOT NULL DEFAULT '{}',
                listen_settings_hash TEXT NOT NULL DEFAULT
                    '44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a',
                listen_jobs_manifest_hash TEXT NOT NULL,
                state_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS top_content_links (
                run_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                video_id TEXT NOT NULL,
                canonical_url TEXT NOT NULL,
                creator TEXT NOT NULL,
                ranking_key TEXT NOT NULL,
                ranking_position INTEGER NOT NULL,
                link_hash TEXT NOT NULL,
                checkpointed_at TEXT NOT NULL,
                PRIMARY KEY (run_id, video_id),
                UNIQUE (run_id, ordinal),
                UNIQUE (run_id, ranking_key, ranking_position),
                FOREIGN KEY (run_id) REFERENCES top_content_link_runs(run_id)
            );

            CREATE TABLE IF NOT EXISTS top_content_discovery_failures (
                run_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                stage TEXT NOT NULL,
                reason TEXT NOT NULL,
                context_json TEXT NOT NULL,
                failure_hash TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                PRIMARY KEY (run_id, sequence),
                FOREIGN KEY (run_id) REFERENCES top_content_link_runs(run_id)
            );

            CREATE TABLE IF NOT EXISTS top_content_listen_jobs (
                run_id TEXT NOT NULL,
                video_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                canonical_url TEXT NOT NULL,
                project TEXT NOT NULL,
                status TEXT NOT NULL,
                child_run_id TEXT NOT NULL DEFAULT '',
                child_database TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '',
                job_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (run_id, video_id),
                UNIQUE (run_id, ordinal),
                FOREIGN KEY (run_id, video_id)
                    REFERENCES top_content_links(run_id, video_id)
            );

            CREATE INDEX IF NOT EXISTS top_content_links_order_idx
                ON top_content_links(run_id, ordinal);
            CREATE INDEX IF NOT EXISTS top_content_jobs_status_idx
                ON top_content_listen_jobs(run_id, status, ordinal);
            """
        )

    def create_run(
        self,
        run_id: str,
        *,
        source_url: str,
        requested_count: int,
        scope: Mapping[str, Any],
        filter_snapshot: Mapping[str, Any],
        list_snapshot: Mapping[str, Any],
        account_binding: Mapping[str, Any],
        ranking_order: Any,
        collection_policy: str = "all_links",
        master_database_identity: str = "",
        master_database_hash: str = "",
        master_database_path: str = "",
        known_excluded_count: int = 0,
        known_excluded_provenance: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_run_id = _run_id(run_id)
        normalized_source = normalize_source_url(source_url)
        normalized_count = _positive_int(requested_count, "requested_count")
        normalized_scope = _mapping(scope, "scope")
        normalized_filter = _mapping(filter_snapshot, "filter_snapshot")
        normalized_list = _mapping(list_snapshot, "list_snapshot")
        normalized_account = _mapping(account_binding, "account_binding")
        if not normalized_account:
            raise InvalidTopContentInput("account_binding cannot be empty")
        normalized_order = _ranking_order(ranking_order)
        normalized_policy = str(collection_policy or "").strip().casefold()
        if normalized_policy not in COLLECTION_POLICIES:
            raise InvalidTopContentInput("collection_policy is invalid")
        normalized_excluded = _nonnegative_int(
            known_excluded_count, "known_excluded_count"
        )
        normalized_exclusion_provenance = _mapping(
            known_excluded_provenance or {}, "known_excluded_provenance"
        )
        normalized_master_identity = str(master_database_identity or "").strip()
        normalized_master_hash = str(master_database_hash or "").strip().casefold()
        normalized_master_path = str(master_database_path or "").strip()
        if normalized_policy == "all_links":
            if (
                normalized_master_identity
                or normalized_master_hash
                or normalized_master_path
                or normalized_excluded
                or normalized_exclusion_provenance
            ):
                raise InvalidTopContentInput(
                    "all_links cannot claim a TikTok master exclusion"
                )
        else:
            normalized_master_identity = _required_text(
                normalized_master_identity, "master_database_identity", limit=500
            )
            if _HASH_RE.fullmatch(normalized_master_hash) is None:
                raise InvalidTopContentInput(
                    "master_database_hash must be a lowercase SHA-256"
                )
            normalized_master_path = _normalized_master_path(normalized_master_path)
            if normalized_excluded and not normalized_exclusion_provenance:
                raise InvalidTopContentInput(
                    "known exclusions require exclusion provenance"
                )

        scope_document = _immutable_scope_document(
            run_id=normalized_run_id,
            source_url=normalized_source,
            requested_count=normalized_count,
            collection_policy=normalized_policy,
            scope=normalized_scope,
            filter_snapshot=normalized_filter,
            list_snapshot=normalized_list,
            account_binding=normalized_account,
            ranking_order=normalized_order,
            master_database_identity=normalized_master_identity,
            master_database_hash=normalized_master_hash,
            master_database_path=normalized_master_path,
            known_excluded_count=normalized_excluded,
            known_excluded_provenance=normalized_exclusion_provenance,
        )
        immutable_scope_hash = canonical_sha256(scope_document)
        timestamp = iso(utc_now())
        empty_hash = canonical_sha256([])
        values = {
            "scope_json": canonical_json(normalized_scope),
            "scope_document_hash": canonical_sha256(normalized_scope),
            "filter_snapshot_json": canonical_json(normalized_filter),
            "filter_snapshot_hash": canonical_sha256(normalized_filter),
            "list_snapshot_json": canonical_json(normalized_list),
            "list_snapshot_hash": canonical_sha256(normalized_list),
            "account_binding_json": canonical_json(normalized_account),
            "account_binding_hash": canonical_sha256(normalized_account),
            "ranking_order_json": canonical_json(normalized_order),
            "ranking_order_hash": canonical_sha256(normalized_order),
            "known_excluded_provenance_json": canonical_json(
                normalized_exclusion_provenance
            ),
            "known_excluded_provenance_hash": canonical_sha256(
                normalized_exclusion_provenance
            ),
        }
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM top_content_link_runs WHERE run_id=?",
                (normalized_run_id,),
            ).fetchone()
            if existing is not None:
                self._verify_integrity(connection, normalized_run_id)
                if str(existing["immutable_scope_hash"]) != immutable_scope_hash:
                    raise TopContentStateConflict(
                        "run_id already exists with a different immutable scope"
                    )
                return self._status(connection, normalized_run_id, verify=False)

            connection.execute(
                """
                INSERT INTO top_content_link_runs (
                    run_id, schema_version, platform, source_url,
                    requested_count, collection_policy, scope_json,
                    scope_document_hash, filter_snapshot_json,
                    filter_snapshot_hash, list_snapshot_json,
                    list_snapshot_hash, account_binding_json,
                    account_binding_hash, ranking_order_json,
                    ranking_order_hash, master_database_identity,
                    master_database_hash, master_database_path,
                    known_excluded_count, known_excluded_provenance_json,
                    known_excluded_provenance_hash, immutable_scope_hash,
                    status, incomplete_reasons_json, links_manifest_hash,
                    failures_manifest_hash, listen_jobs_manifest_hash,
                    state_hash, created_at, updated_at
                ) VALUES (
                    ?, ?, 'tiktok_one_top_content', ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'collecting', '[]', ?, ?,
                    ?, '', ?, ?
                )
                """,
                (
                    normalized_run_id,
                    SCHEMA_VERSION,
                    normalized_source,
                    normalized_count,
                    normalized_policy,
                    values["scope_json"],
                    values["scope_document_hash"],
                    values["filter_snapshot_json"],
                    values["filter_snapshot_hash"],
                    values["list_snapshot_json"],
                    values["list_snapshot_hash"],
                    values["account_binding_json"],
                    values["account_binding_hash"],
                    values["ranking_order_json"],
                    values["ranking_order_hash"],
                    normalized_master_identity,
                    normalized_master_hash,
                    normalized_master_path,
                    normalized_excluded,
                    values["known_excluded_provenance_json"],
                    values["known_excluded_provenance_hash"],
                    immutable_scope_hash,
                    empty_hash,
                    empty_hash,
                    empty_hash,
                    timestamp,
                    timestamp,
                ),
            )
            self._refresh_state_hash(connection, normalized_run_id)
            return self._status(connection, normalized_run_id, verify=True)

    def _raw_run(self, connection: sqlite3.Connection, run_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM top_content_link_runs WHERE run_id=?",
            (_run_id(run_id),),
        ).fetchone()
        if row is None:
            raise TopContentRunNotFound("run_id was not found")
        return row

    @staticmethod
    def _decode_stored_json(row: sqlite3.Row, field: str, expected: type) -> Any:
        try:
            result = json.loads(str(row[field]))
        except (TypeError, json.JSONDecodeError) as exc:
            raise TopContentStateIntegrityError(f"{field} is invalid JSON") from exc
        if not isinstance(result, expected):
            raise TopContentStateIntegrityError(f"{field} has an invalid shape")
        return result

    def _verify_run_row(self, row: sqlite3.Row) -> dict[str, Any]:
        try:
            if (
                row["schema_version"] != SCHEMA_VERSION
                or row["platform"] != "tiktok_one_top_content"
            ):
                raise TopContentStateIntegrityError(
                    "run schema/platform binding is invalid"
                )
            if row["status"] not in RUN_STATUSES:
                raise TopContentStateIntegrityError("run status is invalid")
            if row["collection_policy"] not in COLLECTION_POLICIES:
                raise TopContentStateIntegrityError("collection policy is invalid")
            run_id = _run_id(row["run_id"])
            source_url = normalize_source_url(row["source_url"])
            if source_url != row["source_url"]:
                raise TopContentStateIntegrityError("source URL is not normalized")
            requested_count = _positive_int(row["requested_count"], "requested_count")
            scope = self._decode_stored_json(row, "scope_json", dict)
            filter_snapshot = self._decode_stored_json(
                row, "filter_snapshot_json", dict
            )
            list_snapshot = self._decode_stored_json(row, "list_snapshot_json", dict)
            account_binding = self._decode_stored_json(
                row, "account_binding_json", dict
            )
            if not account_binding:
                raise TopContentStateIntegrityError("account binding is empty")
            try:
                ranking_order = json.loads(str(row["ranking_order_json"]))
            except (TypeError, json.JSONDecodeError) as exc:
                raise TopContentStateIntegrityError(
                    "ranking_order_json is invalid JSON"
                ) from exc
            if ranking_order in (None, "", [], {}):
                raise TopContentStateIntegrityError("ranking order is empty")
            exclusion_provenance = self._decode_stored_json(
                row, "known_excluded_provenance_json", dict
            )
            listen_settings = self._decode_stored_json(
                row, "listen_settings_json", dict
            )
            if canonical_sha256(listen_settings) != row["listen_settings_hash"]:
                raise TopContentStateIntegrityError(
                    "LISTEN settings hash does not match stored JSON"
                )
            if not bool(row["listen_jobs_initialized"]) and listen_settings:
                raise TopContentStateIntegrityError(
                    "LISTEN settings exist without initialized jobs"
                )
            known_excluded_count = _nonnegative_int(
                row["known_excluded_count"], "known_excluded_count"
            )
            policy = str(row["collection_policy"])
            master_identity = str(row["master_database_identity"])
            master_hash = str(row["master_database_hash"])
            master_path = str(row["master_database_path"])
            if policy == "all_links":
                if (
                    master_identity
                    or master_hash
                    or master_path
                    or known_excluded_count
                    or exclusion_provenance
                ):
                    raise TopContentStateIntegrityError(
                        "all_links contains master-exclusion provenance"
                    )
            elif (
                not master_identity
                or _HASH_RE.fullmatch(master_hash) is None
                or not master_path
            ):
                raise TopContentStateIntegrityError(
                    "new-only master binding is invalid"
                )
            hash_pairs = (
                (scope, "scope_document_hash"),
                (filter_snapshot, "filter_snapshot_hash"),
                (list_snapshot, "list_snapshot_hash"),
                (account_binding, "account_binding_hash"),
                (ranking_order, "ranking_order_hash"),
                (exclusion_provenance, "known_excluded_provenance_hash"),
            )
            for value, hash_field in hash_pairs:
                if canonical_sha256(value) != row[hash_field]:
                    raise TopContentStateIntegrityError(
                        f"{hash_field} does not match stored JSON"
                    )
            immutable_scope = _immutable_scope_document(
                run_id=run_id,
                source_url=source_url,
                requested_count=requested_count,
                collection_policy=policy,
                scope=scope,
                filter_snapshot=filter_snapshot,
                list_snapshot=list_snapshot,
                account_binding=account_binding,
                ranking_order=ranking_order,
                master_database_identity=master_identity,
                master_database_hash=master_hash,
                master_database_path=master_path,
                known_excluded_count=known_excluded_count,
                known_excluded_provenance=exclusion_provenance,
            )
            if canonical_sha256(immutable_scope) != row["immutable_scope_hash"]:
                raise TopContentStateIntegrityError("immutable scope hash mismatch")
            reasons = self._decode_stored_json(row, "incomplete_reasons_json", list)
            if any(not isinstance(reason, str) or not reason for reason in reasons):
                raise TopContentStateIntegrityError(
                    "incomplete reasons have an invalid shape"
                )
            if canonical_sha256(_state_document(row)) != row["state_hash"]:
                raise TopContentStateIntegrityError("run state hash mismatch")
            return {
                "scope": scope,
                "filter_snapshot": filter_snapshot,
                "list_snapshot": list_snapshot,
                "account_binding": account_binding,
                "ranking_order": ranking_order,
                "known_excluded_provenance": exclusion_provenance,
                "incomplete_reasons": reasons,
                "listen_settings": listen_settings,
            }
        except InvalidTopContentInput as exc:
            raise TopContentStateIntegrityError(str(exc)) from exc

    def _verify_integrity(
        self, connection: sqlite3.Connection, run_id: str
    ) -> sqlite3.Row:
        row = self._raw_run(connection, run_id)
        decoded = self._verify_run_row(row)
        link_rows = connection.execute(
            "SELECT * FROM top_content_links WHERE run_id=? ORDER BY ordinal",
            (run_id,),
        ).fetchall()
        link_hashes: list[str] = []
        seen_video_ids: set[str] = set()
        for link in link_rows:
            try:
                ordinal = _positive_int(link["ordinal"], "ordinal")
                video_id = normalize_video_id(link["video_id"])
                creator = normalize_creator(link["creator"])
                canonical_url = normalize_video_url(
                    link["canonical_url"], video_id=video_id, creator=creator
                )
                ranking_key = _required_text(
                    link["ranking_key"], "ranking_key", limit=500
                )
                ranking_position = _positive_int(
                    link["ranking_position"], "ranking_position"
                )
            except InvalidTopContentInput as exc:
                raise TopContentStateIntegrityError(str(exc)) from exc
            if ordinal > int(row["requested_count"]):
                raise TopContentStateIntegrityError(
                    "link ordinal exceeds the requested count"
                )
            if video_id in seen_video_ids:
                raise TopContentStateIntegrityError("duplicate video ID stored")
            seen_video_ids.add(video_id)
            document = _link_document(
                run_id=str(row["run_id"]),
                ordinal=ordinal,
                video_id=video_id,
                canonical_url=canonical_url,
                creator=creator,
                ranking_key=ranking_key,
                ranking_position=ranking_position,
            )
            link_hash = canonical_sha256(document)
            if link_hash != link["link_hash"]:
                raise TopContentStateIntegrityError("link hash mismatch")
            link_hashes.append(link_hash)
        if len(link_rows) != int(row["link_count"]):
            raise TopContentStateIntegrityError("stored link count mismatch")
        if canonical_sha256(link_hashes) != row["links_manifest_hash"]:
            raise TopContentStateIntegrityError("links manifest hash mismatch")

        failure_rows = connection.execute(
            """
            SELECT * FROM top_content_discovery_failures
            WHERE run_id=? ORDER BY sequence
            """,
            (run_id,),
        ).fetchall()
        failure_hashes: list[str] = []
        for expected_sequence, failure in enumerate(failure_rows, start=1):
            if int(failure["sequence"]) != expected_sequence:
                raise TopContentStateIntegrityError(
                    "discovery failure sequence is not contiguous"
                )
            context = self._decode_stored_json(failure, "context_json", dict)
            document = _failure_document(
                run_id=str(row["run_id"]),
                sequence=expected_sequence,
                stage=str(failure["stage"]),
                reason=str(failure["reason"]),
                context=context,
            )
            failure_hash = canonical_sha256(document)
            if failure_hash != failure["failure_hash"]:
                raise TopContentStateIntegrityError("discovery failure hash mismatch")
            failure_hashes.append(failure_hash)
        if len(failure_rows) != int(row["failure_count"]):
            raise TopContentStateIntegrityError(
                "stored discovery failure count mismatch"
            )
        if canonical_sha256(failure_hashes) != row["failures_manifest_hash"]:
            raise TopContentStateIntegrityError(
                "discovery failures manifest hash mismatch"
            )

        job_rows = connection.execute(
            "SELECT * FROM top_content_listen_jobs WHERE run_id=? ORDER BY ordinal",
            (run_id,),
        ).fetchall()
        job_hashes: list[str] = []
        if bool(row["listen_jobs_initialized"]):
            if len(job_rows) != len(link_rows) or not row["listen_project"]:
                raise TopContentStateIntegrityError("LISTEN job set is incomplete")
        elif job_rows or row["listen_project"]:
            raise TopContentStateIntegrityError(
                "LISTEN jobs exist without initialization binding"
            )
        links_by_id = {str(link["video_id"]): link for link in link_rows}
        for job in job_rows:
            video_id = str(job["video_id"])
            link = links_by_id.get(video_id)
            if (
                link is None
                or int(job["ordinal"]) != int(link["ordinal"])
                or job["canonical_url"] != link["canonical_url"]
                or job["project"] != row["listen_project"]
                or job["status"] not in LISTEN_JOB_STATUSES
            ):
                raise TopContentStateIntegrityError("LISTEN job binding is invalid")
            document = _job_document(
                run_id=str(row["run_id"]),
                video_id=video_id,
                ordinal=int(job["ordinal"]),
                canonical_url=str(job["canonical_url"]),
                project=str(job["project"]),
                status=str(job["status"]),
                child_run_id=str(job["child_run_id"]),
                child_database=str(job["child_database"]),
                reason=str(job["reason"]),
            )
            job_hash = canonical_sha256(document)
            if job_hash != job["job_hash"]:
                raise TopContentStateIntegrityError("LISTEN job hash mismatch")
            self._verify_job_fields(document, integrity=True)
            job_hashes.append(job_hash)
        if canonical_sha256(job_hashes) != row["listen_jobs_manifest_hash"]:
            raise TopContentStateIntegrityError("LISTEN jobs manifest hash mismatch")

        status = str(row["status"])
        count = len(link_rows)
        requested = int(row["requested_count"])
        reasons = decoded["incomplete_reasons"]
        if status == "links_complete" and (
            count != requested
            or [int(link["ordinal"]) for link in link_rows]
            != list(range(1, requested + 1))
            or reasons
        ):
            raise TopContentStateIntegrityError(
                "complete status is inconsistent with stored links"
            )
        if status == "links_incomplete" and (count >= requested or not reasons):
            raise TopContentStateIntegrityError(
                "incomplete status is inconsistent with stored links"
            )
        if status == "collecting" and reasons:
            raise TopContentStateIntegrityError(
                "collecting status cannot retain incomplete reasons"
            )
        return row

    def _refresh_state_hash(self, connection: sqlite3.Connection, run_id: str) -> None:
        row = self._raw_run(connection, run_id)
        connection.execute(
            "UPDATE top_content_link_runs SET state_hash=? WHERE run_id=?",
            (canonical_sha256(_state_document(row)), run_id),
        )

    def _refresh_link_manifest(
        self, connection: sqlite3.Connection, run_id: str, timestamp: str
    ) -> None:
        rows = connection.execute(
            "SELECT link_hash FROM top_content_links WHERE run_id=? ORDER BY ordinal",
            (run_id,),
        ).fetchall()
        hashes = [str(row["link_hash"]) for row in rows]
        connection.execute(
            """
            UPDATE top_content_link_runs
            SET link_count=?, links_manifest_hash=?, updated_at=?
            WHERE run_id=?
            """,
            (len(hashes), canonical_sha256(hashes), timestamp, run_id),
        )
        self._refresh_state_hash(connection, run_id)

    def _refresh_failure_manifest(
        self, connection: sqlite3.Connection, run_id: str, timestamp: str
    ) -> None:
        rows = connection.execute(
            """
            SELECT failure_hash FROM top_content_discovery_failures
            WHERE run_id=? ORDER BY sequence
            """,
            (run_id,),
        ).fetchall()
        hashes = [str(row["failure_hash"]) for row in rows]
        connection.execute(
            """
            UPDATE top_content_link_runs
            SET failure_count=?, failures_manifest_hash=?, updated_at=?
            WHERE run_id=?
            """,
            (len(hashes), canonical_sha256(hashes), timestamp, run_id),
        )
        self._refresh_state_hash(connection, run_id)

    def _refresh_job_manifest(
        self, connection: sqlite3.Connection, run_id: str, timestamp: str
    ) -> None:
        rows = connection.execute(
            """
            SELECT job_hash FROM top_content_listen_jobs
            WHERE run_id=? ORDER BY ordinal
            """,
            (run_id,),
        ).fetchall()
        hashes = [str(row["job_hash"]) for row in rows]
        connection.execute(
            """
            UPDATE top_content_link_runs
            SET listen_jobs_manifest_hash=?, updated_at=? WHERE run_id=?
            """,
            (canonical_sha256(hashes), timestamp, run_id),
        )
        self._refresh_state_hash(connection, run_id)

    def get_run(self, run_id: str) -> dict[str, Any]:
        self._verify_integrity(self._connection, run_id)
        return self._status(self._connection, run_id, verify=False)

    def checkpoint_link(
        self,
        run_id: str,
        *,
        ordinal: int,
        video_id: str,
        canonical_url: str,
        creator: str,
        ranking_key: str,
        ranking_position: int,
    ) -> dict[str, Any]:
        normalized_run_id = _run_id(run_id)
        normalized_ordinal = _positive_int(ordinal, "ordinal")
        normalized_video_id = normalize_video_id(video_id)
        normalized_creator = normalize_creator(creator)
        normalized_url = normalize_video_url(
            canonical_url,
            video_id=normalized_video_id,
            creator=normalized_creator,
        )
        normalized_ranking_key = _required_text(ranking_key, "ranking_key", limit=500)
        normalized_ranking_position = _positive_int(
            ranking_position, "ranking_position"
        )
        document = _link_document(
            run_id=normalized_run_id,
            ordinal=normalized_ordinal,
            video_id=normalized_video_id,
            canonical_url=normalized_url,
            creator=normalized_creator,
            ranking_key=normalized_ranking_key,
            ranking_position=normalized_ranking_position,
        )
        link_hash = canonical_sha256(document)
        timestamp = iso(utc_now())
        with self._transaction() as connection:
            run = self._verify_integrity(connection, normalized_run_id)
            if run["status"] != "collecting":
                raise TopContentStateConflict(
                    "link checkpoints require a collecting run"
                )
            if normalized_ordinal > int(run["requested_count"]):
                raise InvalidTopContentInput(
                    "ordinal cannot exceed the requested count"
                )
            existing = connection.execute(
                """
                SELECT * FROM top_content_links
                WHERE run_id=? AND video_id=?
                """,
                (normalized_run_id, normalized_video_id),
            ).fetchone()
            if existing is not None:
                if existing["link_hash"] != link_hash:
                    raise TopContentStateConflict(
                        "video_id already has a different checkpoint"
                    )
                return self._link_result(existing)
            ordinal_owner = connection.execute(
                """
                SELECT video_id FROM top_content_links
                WHERE run_id=? AND ordinal=?
                """,
                (normalized_run_id, normalized_ordinal),
            ).fetchone()
            if ordinal_owner is not None:
                raise TopContentStateConflict("ordinal is already checkpointed")
            rank_owner = connection.execute(
                """
                SELECT video_id FROM top_content_links
                WHERE run_id=? AND ranking_key=? AND ranking_position=?
                """,
                (
                    normalized_run_id,
                    normalized_ranking_key,
                    normalized_ranking_position,
                ),
            ).fetchone()
            if rank_owner is not None:
                raise TopContentStateConflict(
                    "ranking_position is already checkpointed for this ranking key"
                )
            connection.execute(
                """
                INSERT INTO top_content_links (
                    run_id, ordinal, video_id, canonical_url, creator,
                    ranking_key, ranking_position, link_hash, checkpointed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_run_id,
                    normalized_ordinal,
                    normalized_video_id,
                    normalized_url,
                    normalized_creator,
                    normalized_ranking_key,
                    normalized_ranking_position,
                    link_hash,
                    timestamp,
                ),
            )
            self._refresh_link_manifest(connection, normalized_run_id, timestamp)
            inserted = connection.execute(
                """
                SELECT * FROM top_content_links
                WHERE run_id=? AND video_id=?
                """,
                (normalized_run_id, normalized_video_id),
            ).fetchone()
            return self._link_result(inserted)

    def record_discovery_failure(
        self,
        run_id: str,
        reason: str,
        *,
        stage: str = "discovery",
        context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_run_id = _run_id(run_id)
        normalized_reason = _required_text(reason, "reason", limit=2_000)
        normalized_stage = _required_text(stage, "stage", limit=200)
        normalized_context = _mapping(context or {}, "context")
        timestamp = iso(utc_now())
        with self._transaction() as connection:
            run = self._verify_integrity(connection, normalized_run_id)
            if run["status"] != "collecting":
                raise TopContentStateConflict(
                    "discovery failures require a collecting run"
                )
            sequence = int(run["failure_count"]) + 1
            document = _failure_document(
                run_id=normalized_run_id,
                sequence=sequence,
                stage=normalized_stage,
                reason=normalized_reason,
                context=normalized_context,
            )
            failure_hash = canonical_sha256(document)
            connection.execute(
                """
                INSERT INTO top_content_discovery_failures (
                    run_id, sequence, stage, reason, context_json,
                    failure_hash, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_run_id,
                    sequence,
                    normalized_stage,
                    normalized_reason,
                    canonical_json(normalized_context),
                    failure_hash,
                    timestamp,
                ),
            )
            self._refresh_failure_manifest(connection, normalized_run_id, timestamp)
            return {
                **document,
                "failure_hash": failure_hash,
                "recorded_at": timestamp,
            }

    def finalize(
        self, run_id: str, reasons: Sequence[str] | None = None
    ) -> dict[str, Any]:
        normalized_run_id = _run_id(run_id)
        normalized_reasons = _reasons(reasons)
        timestamp = iso(utc_now())
        with self._transaction() as connection:
            run = self._verify_integrity(connection, normalized_run_id)
            count = int(run["link_count"])
            requested = int(run["requested_count"])
            if count == requested:
                if normalized_reasons:
                    raise InvalidTopContentInput(
                        "a complete link set cannot have incomplete reasons"
                    )
                target_status = "links_complete"
                final_reasons: list[str] = []
            else:
                if not normalized_reasons:
                    rows = connection.execute(
                        """
                        SELECT reason FROM top_content_discovery_failures
                        WHERE run_id=? ORDER BY sequence
                        """,
                        (normalized_run_id,),
                    ).fetchall()
                    normalized_reasons = _reasons([str(row["reason"]) for row in rows])
                if not normalized_reasons:
                    raise InvalidTopContentInput(
                        "an incomplete link set requires at least one reason"
                    )
                target_status = "links_incomplete"
                final_reasons = normalized_reasons

            if run["status"] != "collecting":
                stored_reasons = json.loads(str(run["incomplete_reasons_json"]))
                if run["status"] == target_status and stored_reasons == final_reasons:
                    return self._status(connection, normalized_run_id, verify=False)
                raise TopContentStateConflict("terminal link state is immutable")
            if target_status == "links_complete":
                ordinals = [
                    int(row["ordinal"])
                    for row in connection.execute(
                        """
                        SELECT ordinal FROM top_content_links
                        WHERE run_id=? ORDER BY ordinal
                        """,
                        (normalized_run_id,),
                    ).fetchall()
                ]
                if ordinals != list(range(1, requested + 1)):
                    raise TopContentStateConflict(
                        "a complete link set requires contiguous ordinals"
                    )
            connection.execute(
                """
                UPDATE top_content_link_runs
                SET status=?, incomplete_reasons_json=?, updated_at=?
                WHERE run_id=?
                """,
                (
                    target_status,
                    canonical_json(final_reasons),
                    timestamp,
                    normalized_run_id,
                ),
            )
            self._refresh_state_hash(connection, normalized_run_id)
            return self._status(connection, normalized_run_id, verify=True)

    def resume(
        self,
        run_id: str,
        *,
        expected_scope_hash: str | None = None,
        source_url: str | None = None,
        requested_count: int | None = None,
        collection_policy: str | None = None,
        scope: Mapping[str, Any] | None = None,
        filter_snapshot: Mapping[str, Any] | None = None,
        list_snapshot: Mapping[str, Any] | None = None,
        account_binding: Mapping[str, Any] | None = None,
        ranking_order: Any | None = None,
    ) -> dict[str, Any]:
        normalized_run_id = _run_id(run_id)
        timestamp = iso(utc_now())
        with self._transaction() as connection:
            run = self._verify_integrity(connection, normalized_run_id)
            decoded = self._verify_run_row(run)
            if (
                expected_scope_hash is not None
                and str(expected_scope_hash) != run["immutable_scope_hash"]
            ):
                raise TopContentStateConflict("resume scope hash mismatch")
            comparisons: list[tuple[str, Any, Any]] = []
            if source_url is not None:
                comparisons.append(
                    ("source_url", normalize_source_url(source_url), run["source_url"])
                )
            if requested_count is not None:
                comparisons.append(
                    (
                        "requested_count",
                        _positive_int(requested_count, "requested_count"),
                        int(run["requested_count"]),
                    )
                )
            if collection_policy is not None:
                comparisons.append(
                    (
                        "collection_policy",
                        str(collection_policy).strip().casefold(),
                        run["collection_policy"],
                    )
                )
            if scope is not None:
                comparisons.append(
                    ("scope", _mapping(scope, "scope"), decoded["scope"])
                )
            if filter_snapshot is not None:
                comparisons.append(
                    (
                        "filter_snapshot",
                        _mapping(filter_snapshot, "filter_snapshot"),
                        decoded["filter_snapshot"],
                    )
                )
            if list_snapshot is not None:
                comparisons.append(
                    (
                        "list_snapshot",
                        _mapping(list_snapshot, "list_snapshot"),
                        decoded["list_snapshot"],
                    )
                )
            if account_binding is not None:
                comparisons.append(
                    (
                        "account_binding",
                        _mapping(account_binding, "account_binding"),
                        decoded["account_binding"],
                    )
                )
            if ranking_order is not None:
                comparisons.append(
                    (
                        "ranking_order",
                        _ranking_order(ranking_order),
                        decoded["ranking_order"],
                    )
                )
            for name, supplied, frozen in comparisons:
                if supplied != frozen:
                    raise TopContentStateConflict(
                        f"resume {name} differs from the frozen scope"
                    )
            if run["status"] == "links_complete":
                raise TopContentStateConflict("complete runs cannot be resumed")
            if run["status"] == "links_incomplete":
                connection.execute(
                    """
                    UPDATE top_content_link_runs
                    SET status='collecting', incomplete_reasons_json='[]',
                        updated_at=? WHERE run_id=?
                    """,
                    (timestamp, normalized_run_id),
                )
                self._refresh_state_hash(connection, normalized_run_id)
            return self._status(connection, normalized_run_id, verify=True)

    def status(self, run_id: str) -> dict[str, Any]:
        return self._status(self._connection, run_id, verify=True)

    def _status(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        *,
        verify: bool,
    ) -> dict[str, Any]:
        row = (
            self._verify_integrity(connection, run_id)
            if verify
            else self._raw_run(connection, run_id)
        )
        decoded = self._verify_run_row(row)
        job_counts = {status: 0 for status in sorted(LISTEN_JOB_STATUSES)}
        for job in connection.execute(
            """
            SELECT status, COUNT(*) AS count FROM top_content_listen_jobs
            WHERE run_id=? GROUP BY status
            """,
            (run_id,),
        ).fetchall():
            job_counts[str(job["status"])] = int(job["count"])
        link_count = int(row["link_count"])
        requested_count = int(row["requested_count"])
        return {
            "run_id": str(row["run_id"]),
            "schema_version": str(row["schema_version"]),
            "platform": str(row["platform"]),
            "source_url": str(row["source_url"]),
            "requested_count": requested_count,
            "collection_policy": str(row["collection_policy"]),
            "scope": decoded["scope"],
            "filter_snapshot": decoded["filter_snapshot"],
            "list_snapshot": decoded["list_snapshot"],
            "account_binding": decoded["account_binding"],
            "ranking_order": decoded["ranking_order"],
            "master_database": {
                "identity": str(row["master_database_identity"]),
                "sha256": str(row["master_database_hash"]),
                "path": str(row["master_database_path"]),
            },
            "known_excluded": {
                "count": int(row["known_excluded_count"]),
                "provenance": decoded["known_excluded_provenance"],
            },
            "immutable_scope_hash": str(row["immutable_scope_hash"]),
            "scope_hash": str(row["immutable_scope_hash"]),
            "filter_snapshot_hash": str(row["filter_snapshot_hash"]),
            "list_snapshot_hash": str(row["list_snapshot_hash"]),
            "account_binding_hash": str(row["account_binding_hash"]),
            "ranking_order_hash": str(row["ranking_order_hash"]),
            "status": str(row["status"]),
            "terminal": row["status"] != "collecting",
            "links_discovered": link_count,
            "link_count": link_count,
            "links_manifest_hash": str(row["links_manifest_hash"]),
            "remaining_count": requested_count - link_count,
            "incomplete_reasons": decoded["incomplete_reasons"],
            "status_summary": f"{row['status']}: {link_count}/{requested_count}",
            "failure_count": int(row["failure_count"]),
            "listen_jobs": {
                "initialized": bool(row["listen_jobs_initialized"]),
                "project": str(row["listen_project"]),
                "settings": decoded["listen_settings"],
                "settings_hash": str(row["listen_settings_hash"]),
                "manifest_hash": str(row["listen_jobs_manifest_hash"]),
                "counts": job_counts,
                "new_listen_evidence_complete": job_counts["complete"],
                "skipped_known": job_counts["skipped_known"],
            },
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    @staticmethod
    def _link_result(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "ordinal": int(row["ordinal"]),
            "video_id": str(row["video_id"]),
            "canonical_url": str(row["canonical_url"]),
            "creator": str(row["creator"]),
            "ranking_key": str(row["ranking_key"]),
            "ranking_position": int(row["ranking_position"]),
            "link_hash": str(row["link_hash"]),
            "checkpointed_at": str(row["checkpointed_at"]),
        }

    def links(self, run_id: str) -> list[dict[str, Any]]:
        self._verify_integrity(self._connection, run_id)
        rows = self._connection.execute(
            "SELECT * FROM top_content_links WHERE run_id=? ORDER BY ordinal",
            (_run_id(run_id),),
        ).fetchall()
        return [self._link_result(row) for row in rows]

    def export_links(self, run_id: str) -> dict[str, Any]:
        status = self.status(run_id)
        if status["status"] != "links_complete":
            raise TopContentStateConflict(
                "only an exact complete link set can be exported"
            )
        rows = self.links(run_id)
        return {
            "schema_version": EXPORT_SCHEMA_VERSION,
            "canonical_urls": [row["canonical_url"] for row in rows],
            "provenance": {
                "platform": "tiktok_one_top_content",
                "run_id": status["run_id"],
                "source_url": status["source_url"],
                "collection_policy": status["collection_policy"],
                "requested_count": status["requested_count"],
                "exported_count": len(rows),
                "immutable_scope_hash": status["immutable_scope_hash"],
                "filter_snapshot_hash": status["filter_snapshot_hash"],
                "list_snapshot_hash": status["list_snapshot_hash"],
                "account_binding_hash": status["account_binding_hash"],
                "ranking_order_hash": status["ranking_order_hash"],
                "links_manifest_hash": status["links_manifest_hash"],
                "known_excluded": status["known_excluded"],
                "master_database": status["master_database"],
            },
        }

    def initialize_listen_jobs(
        self,
        run_id: str,
        project: str,
        settings: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        normalized_run_id = _run_id(run_id)
        normalized_project = _required_text(project, "project", limit=500)
        normalized_settings = _mapping(settings or {}, "settings")
        normalized_settings_json = canonical_json(normalized_settings)
        normalized_settings_hash = canonical_sha256(normalized_settings)
        timestamp = iso(utc_now())
        with self._transaction() as connection:
            run = self._verify_integrity(connection, normalized_run_id)
            if run["status"] != "links_complete":
                raise TopContentStateConflict(
                    "LISTEN jobs require an exact complete link set"
                )
            if bool(run["listen_jobs_initialized"]):
                if (
                    run["listen_project"] != normalized_project
                    or run["listen_settings_hash"] != normalized_settings_hash
                    or run["listen_settings_json"] != normalized_settings_json
                ):
                    raise TopContentStateConflict(
                        "LISTEN project/settings binding is immutable"
                    )
                return self._job_rows(connection, normalized_run_id)
            links = connection.execute(
                "SELECT * FROM top_content_links WHERE run_id=? ORDER BY ordinal",
                (normalized_run_id,),
            ).fetchall()
            for link in links:
                document = _job_document(
                    run_id=normalized_run_id,
                    video_id=str(link["video_id"]),
                    ordinal=int(link["ordinal"]),
                    canonical_url=str(link["canonical_url"]),
                    project=normalized_project,
                    status="pending",
                    child_run_id="",
                    child_database="",
                    reason="",
                )
                connection.execute(
                    """
                    INSERT INTO top_content_listen_jobs (
                        run_id, video_id, ordinal, canonical_url, project,
                        status, child_run_id, child_database, reason, job_hash,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'pending', '', '', '', ?, ?, ?)
                    """,
                    (
                        normalized_run_id,
                        link["video_id"],
                        link["ordinal"],
                        link["canonical_url"],
                        normalized_project,
                        canonical_sha256(document),
                        timestamp,
                        timestamp,
                    ),
                )
            connection.execute(
                """
                UPDATE top_content_link_runs
                SET listen_jobs_initialized=1, listen_project=?,
                    listen_settings_json=?, listen_settings_hash=?, updated_at=?
                WHERE run_id=?
                """,
                (
                    normalized_project,
                    normalized_settings_json,
                    normalized_settings_hash,
                    timestamp,
                    normalized_run_id,
                ),
            )
            self._refresh_job_manifest(connection, normalized_run_id, timestamp)
            self._verify_integrity(connection, normalized_run_id)
            return self._job_rows(connection, normalized_run_id)

    @staticmethod
    def _job_result(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "ordinal": int(row["ordinal"]),
            "video_id": str(row["video_id"]),
            "canonical_url": str(row["canonical_url"]),
            "project": str(row["project"]),
            "status": str(row["status"]),
            "child_run_id": str(row["child_run_id"]),
            "child_database": str(row["child_database"]),
            "reason": str(row["reason"]),
            "job_hash": str(row["job_hash"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    def _job_rows(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        *,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        if status is None:
            rows = connection.execute(
                """
                SELECT * FROM top_content_listen_jobs
                WHERE run_id=? ORDER BY ordinal
                """,
                (run_id,),
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT * FROM top_content_listen_jobs
                WHERE run_id=? AND status=? ORDER BY ordinal
                """,
                (run_id, status),
            ).fetchall()
        return [self._job_result(row) for row in rows]

    def pending_listen_jobs(self, run_id: str) -> list[dict[str, Any]]:
        run = self._verify_integrity(self._connection, run_id)
        if not bool(run["listen_jobs_initialized"]):
            return []
        return self._job_rows(self._connection, _run_id(run_id), status="pending")

    def listen_jobs(
        self,
        run_id: str,
        statuses: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return the durable job set, optionally filtered for reconciliation."""

        normalized_run_id = _run_id(run_id)
        run = self._verify_integrity(self._connection, normalized_run_id)
        if not bool(run["listen_jobs_initialized"]):
            return []
        rows = self._job_rows(self._connection, normalized_run_id)
        if statuses is None:
            return rows
        if isinstance(statuses, (str, bytes)):
            raise InvalidTopContentInput("statuses must be a sequence")
        normalized_statuses = {
            str(status or "").strip().casefold() for status in statuses
        }
        if not normalized_statuses or not normalized_statuses.issubset(
            LISTEN_JOB_STATUSES
        ):
            raise InvalidTopContentInput("statuses contain an invalid LISTEN status")
        return [row for row in rows if row["status"] in normalized_statuses]

    @staticmethod
    def _verify_job_fields(
        document: Mapping[str, Any], *, integrity: bool = False
    ) -> None:
        error_type: type[TopContentStateError] = (
            TopContentStateIntegrityError if integrity else InvalidTopContentInput
        )
        status = str(document["status"])
        child_run_id = str(document["child_run_id"])
        child_database = str(document["child_database"])
        reason = str(document["reason"])
        if child_run_id and not child_database:
            raise error_type("child_run_id requires child_database")
        if status == "pending" and (child_run_id or child_database or reason):
            raise error_type("pending LISTEN jobs cannot have child state")
        if status == "running" and (not child_database or reason):
            raise error_type(
                "running LISTEN jobs require a fenced child database and no reason"
            )
        if status == "complete" and (not child_run_id or not child_database or reason):
            raise error_type(
                "complete LISTEN jobs require child identity and no reason"
            )
        if status == "incomplete" and (not child_database or not reason):
            raise error_type(
                "incomplete LISTEN jobs require a fenced database and a reason"
            )
        if status == "blocked" and not reason:
            raise error_type("blocked LISTEN jobs require a reason")
        if status == "skipped_known" and (child_run_id or child_database or not reason):
            raise error_type(
                "skipped-known LISTEN jobs require provenance and no child run"
            )

    def update_listen_job(
        self,
        run_id: str,
        video_id: str | None = None,
        *,
        ordinal: int | None = None,
        status: str,
        child_run_id: str = "",
        child_database: str = "",
        reason: str = "",
    ) -> dict[str, Any]:
        normalized_run_id = _run_id(run_id)
        if video_id is None and ordinal is None:
            raise InvalidTopContentInput("video_id or ordinal is required")
        if video_id is not None and ordinal is not None:
            raise InvalidTopContentInput("supply video_id or ordinal, not both")
        normalized_video_id = (
            normalize_video_id(video_id) if video_id is not None else None
        )
        normalized_ordinal = (
            _positive_int(ordinal, "ordinal") if ordinal is not None else None
        )
        normalized_status = str(status or "").strip().casefold()
        if normalized_status not in LISTEN_JOB_STATUSES:
            raise InvalidTopContentInput("LISTEN job status is invalid")
        normalized_child_run_id = str(child_run_id or "").strip()
        normalized_child_database = str(child_database or "").strip()
        normalized_reason = str(reason or "").strip()
        timestamp = iso(utc_now())
        with self._transaction() as connection:
            run = self._verify_integrity(connection, normalized_run_id)
            if not bool(run["listen_jobs_initialized"]):
                raise TopContentStateConflict("LISTEN jobs are not initialized")
            if normalized_video_id is not None:
                job = connection.execute(
                    """
                    SELECT * FROM top_content_listen_jobs
                    WHERE run_id=? AND video_id=?
                    """,
                    (normalized_run_id, normalized_video_id),
                ).fetchone()
            else:
                job = connection.execute(
                    """
                    SELECT * FROM top_content_listen_jobs
                    WHERE run_id=? AND ordinal=?
                    """,
                    (normalized_run_id, normalized_ordinal),
                ).fetchone()
            if job is None:
                raise InvalidTopContentInput("LISTEN job was not found")
            document = _job_document(
                run_id=normalized_run_id,
                video_id=str(job["video_id"]),
                ordinal=int(job["ordinal"]),
                canonical_url=str(job["canonical_url"]),
                project=str(job["project"]),
                status=normalized_status,
                child_run_id=normalized_child_run_id,
                child_database=normalized_child_database,
                reason=normalized_reason,
            )
            self._verify_job_fields(document)
            new_hash = canonical_sha256(document)
            if job["job_hash"] == new_hash:
                return self._job_result(job)
            current_status = str(job["status"])
            transitions = {
                "pending": {"running", "complete", "blocked", "skipped_known"},
                "running": {"running", "complete", "incomplete", "blocked"},
                "incomplete": {"running", "skipped_known"},
                "blocked": {"running", "skipped_known"},
                "complete": set(),
                "skipped_known": set(),
            }
            if normalized_status not in transitions[current_status]:
                raise TopContentStateConflict(
                    f"LISTEN job cannot transition from {current_status} "
                    f"to {normalized_status}"
                )
            if current_status == "running" and normalized_status == "running":
                if (
                    job["child_run_id"]
                    or not normalized_child_run_id
                    or job["child_database"] != normalized_child_database
                ):
                    raise TopContentStateConflict(
                        "running job may bind its generated child_run_id only once"
                    )
            if current_status in {
                "running",
                "incomplete",
                "blocked",
            } and normalized_status in {"running", "complete", "incomplete", "blocked"}:
                if job["child_database"] and (
                    job["child_database"] != normalized_child_database
                ):
                    raise TopContentStateConflict(
                        "LISTEN child database binding is immutable"
                    )
                if job["child_run_id"] and (
                    job["child_run_id"] != normalized_child_run_id
                ):
                    raise TopContentStateConflict(
                        "LISTEN child run binding is immutable"
                    )
            connection.execute(
                """
                UPDATE top_content_listen_jobs
                SET status=?, child_run_id=?, child_database=?, reason=?,
                    job_hash=?, updated_at=?
                WHERE run_id=? AND video_id=?
                """,
                (
                    normalized_status,
                    normalized_child_run_id,
                    normalized_child_database,
                    normalized_reason,
                    new_hash,
                    timestamp,
                    normalized_run_id,
                    job["video_id"],
                ),
            )
            self._refresh_job_manifest(connection, normalized_run_id, timestamp)
            updated = connection.execute(
                """
                SELECT * FROM top_content_listen_jobs
                WHERE run_id=? AND video_id=?
                """,
                (normalized_run_id, job["video_id"]),
            ).fetchone()
            self._verify_integrity(connection, normalized_run_id)
            return self._job_result(updated)


__all__ = [
    "COLLECTION_POLICIES",
    "EXPORT_SCHEMA_VERSION",
    "InvalidTopContentInput",
    "LISTEN_JOB_STATUSES",
    "RUN_STATUSES",
    "SCHEMA_VERSION",
    "TopContentLinkState",
    "TopContentRunNotFound",
    "TopContentStateConflict",
    "TopContentStateError",
    "TopContentStateIntegrityError",
    "canonical_json",
    "canonical_sha256",
    "normalize_creator",
    "normalize_source_url",
    "normalize_video_id",
    "normalize_video_url",
]
