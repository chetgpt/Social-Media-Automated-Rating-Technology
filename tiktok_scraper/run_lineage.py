"""Read-only validation for completed canonical TikTok source runs.

The audio archive workflow must select posts from the immutable observation
created by an already-completed LISTEN, AUDIT, or ENGAGE run.  This module
performs that binding without importing browser code or mutating either the
project database or the workspace master registry.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from contextlib import ExitStack, closing
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from tiktok_scraper.source_identity import SourceIdentityError, resolve_registered_source


LINEAGE_SCHEMA_VERSION = "tiktok-completed-run-lineage-v1"
SUPPORTED_WORKFLOWS = frozenset({"listen", "audit", "engage"})
TERMINAL_SOURCE_STATUS = {
    "listen": "collection_complete",
    "audit": "audit_complete",
    "engage": "stored",
}

_CANONICAL_POST_PATH = re.compile(
    r"^/@(?P<creator>[A-Za-z0-9._-]{1,64})/"
    r"(?P<content_type>video|photo)/(?P<post_id>[0-9]+)/?$",
    flags=re.IGNORECASE,
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REPARSE_POINT = 0x400


class RunLineageError(RuntimeError):
    """Raised when a source run cannot be proven complete and immutable."""


def canonical_json(value: Any) -> str:
    """Serialize using the workspace's deterministic JSON representation."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _stable_id(*parts: Any, length: int = 32) -> str:
    payload = "\x1f".join(str(part or "") for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]


def _text(value: Any) -> str:
    return str(value or "").strip()


def _resolve_database(value: str | Path, *, field: str) -> Path:
    raw = _text(value)
    if not raw:
        raise RunLineageError(f"{field} is required")
    supplied = Path(raw).expanduser()
    if not supplied.is_absolute():
        raise RunLineageError(f"{field} must be an absolute canonical path")
    absolute = Path(os.path.abspath(os.fspath(supplied)))
    for component in (absolute, *absolute.parents):
        try:
            details = component.stat(follow_symlinks=False)
        except OSError as exc:
            raise RunLineageError(f"{field} does not exist: {raw}") from exc
        if component.is_symlink() or int(
            getattr(details, "st_file_attributes", 0)
        ) & _REPARSE_POINT:
            raise RunLineageError(
                f"{field} must not use a symlink or reparse-point path"
            )
    try:
        resolved = absolute.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RunLineageError(f"{field} does not exist: {raw}") from exc
    if os.path.normcase(os.fspath(resolved)) != os.path.normcase(
        os.fspath(absolute)
    ):
        raise RunLineageError(f"{field} must be the canonical path")
    if not resolved.is_file():
        raise RunLineageError(f"{field} must be a SQLite file: {resolved}")
    return resolved


def _resolve_readable_database(value: str | Path, *, field: str) -> Path:
    """Resolve a local SQLite file without imposing historical path identity.

    AUDIO ARCHIVE uses this relaxed resolver so projects remain usable after a
    workspace move, restore, or directory reorganization.  The returned file
    is still opened read-only and query-only by the caller.
    """

    raw = _text(value)
    if not raw:
        raise RunLineageError(f"{field} is required")
    supplied = Path(raw).expanduser()
    if not supplied.is_absolute():
        supplied = Path.cwd() / supplied
    try:
        resolved = supplied.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RunLineageError(f"{field} does not exist: {raw}") from exc
    if not resolved.is_file():
        raise RunLineageError(f"{field} must be a SQLite file: {resolved}")
    return resolved


def _path_key(value: str | Path) -> str:
    return os.path.normcase(str(Path(value).expanduser().resolve()))


def _open_read_only(path: Path) -> sqlite3.Connection:
    try:
        conn = sqlite3.connect(
            f"{path.as_uri()}?mode=ro",
            uri=True,
            timeout=5.0,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
            conn.close()
            raise RunLineageError(f"could not enable query-only mode for {path}")
        return conn
    except (OSError, sqlite3.Error) as exc:
        raise RunLineageError(f"could not open SQLite database read-only: {path}") from exc


def _table_columns(conn: sqlite3.Connection, table: str) -> frozenset[str]:
    try:
        rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    except sqlite3.Error as exc:
        raise RunLineageError(f"could not inspect SQLite table {table}") from exc
    return frozenset(str(row[1]) for row in rows)


def _require_columns(
    conn: sqlite3.Connection,
    table: str,
    required: set[str] | frozenset[str],
) -> None:
    columns = _table_columns(conn, table)
    if not columns:
        raise RunLineageError(f"required SQLite table is missing: {table}")
    missing = sorted(set(required).difference(columns))
    if missing:
        raise RunLineageError(
            f"SQLite table {table} is missing required columns: "
            + ", ".join(missing)
        )


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {str(key): row[key] for key in row.keys()}


def _required_int(row: Mapping[str, Any], field: str) -> int:
    try:
        value = int(row[field])
    except (KeyError, TypeError, ValueError) as exc:
        raise RunLineageError(f"source run has invalid {field}") from exc
    return value


def _required_sha256(value: Any, *, field: str) -> str:
    digest = _text(value)
    if _SHA256.fullmatch(digest) is None:
        raise RunLineageError(f"{field} must be a lowercase SHA-256 digest")
    return digest


def _parse_json_object(value: Any, *, field: str) -> dict[str, Any]:
    try:
        parsed = json.loads(_text(value) or "{}")
    except json.JSONDecodeError as exc:
        raise RunLineageError(f"{field} is invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise RunLineageError(f"{field} must be a JSON object")
    return parsed


def _canonical_post_identity(
    value: Any,
    *,
    expected_post_id: str,
    field: str,
) -> dict[str, str]:
    raw = _text(value)
    try:
        parsed = urlsplit(raw)
        parsed_port = parsed.port
    except ValueError as exc:
        raise RunLineageError(
            f"{field} is not a query-free canonical TikTok URL"
        ) from exc
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() not in {"tiktok.com", "www.tiktok.com"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed_port is not None
        or parsed.query
        or parsed.fragment
    ):
        raise RunLineageError(f"{field} is not a query-free canonical TikTok URL")
    match = _CANONICAL_POST_PATH.fullmatch(parsed.path)
    if match is None or match.group("post_id") != expected_post_id:
        raise RunLineageError(
            f"{field} must identify TikTok post {expected_post_id}"
        )
    creator = match.group("creator").casefold()
    content_type = match.group("content_type").casefold()
    return {
        "post_id": expected_post_id,
        "creator_handle": creator,
        "content_type": content_type,
        "canonical_url": (
            f"https://www.tiktok.com/@{creator}/{content_type}/{expected_post_id}"
        ),
    }


def _validate_local_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    source_database: Path,
    master_database: Path,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    _require_columns(
        conn,
        "engage_tiktok_runs",
        {
            "run_id",
            "project",
            "topic",
            "requested_count",
            "workflow",
            "collection_policy",
            "source_mode",
            "creator_handle",
            "direct_post_url",
            "cardinality_mode",
            "master_database",
            "expected_account",
            "observed_account",
            "status",
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
            "collection_attempt_id",
            "error",
            "created_at",
            "updated_at",
        },
    )
    _require_columns(
        conn,
        "engage_tiktok_posts",
        {
            "run_id",
            "post_id",
            "url",
            "evidence_ready",
            "evidence_json",
            "evidence_hash",
        },
    )
    row = conn.execute(
        "SELECT * FROM engage_tiktok_runs WHERE run_id=?",
        (run_id,),
    ).fetchone()
    if row is None:
        raise RunLineageError(f"source run not found: {run_id}")
    run = _row_dict(row)
    if _text(run.get("run_id")) != run_id:
        raise RunLineageError("source run ID binding failed")

    workflow = _text(run.get("workflow")).casefold()
    if workflow not in SUPPORTED_WORKFLOWS:
        raise RunLineageError(
            "audio source run workflow must be listen, audit, or engage"
        )
    status = _text(run.get("status")).casefold()
    required_status = TERMINAL_SOURCE_STATUS[workflow]
    if status != required_status:
        raise RunLineageError(
            f"{workflow.upper()} source run must have terminal status "
            f"{required_status}; observed {status or 'missing'}"
        )
    if _text(run.get("collection_attempt_id")):
        raise RunLineageError("source run still has an active collection attempt")
    if _text(run.get("error")):
        raise RunLineageError("source run retains a terminal error")

    requested_count = _required_int(run, "requested_count")
    requested = _required_int(run, "requested")
    evidence_ready = _required_int(run, "evidence_ready")
    if requested_count <= 0:
        raise RunLineageError("audio source run must contain at least one post")
    if requested != requested_count or evidence_ready != requested_count:
        raise RunLineageError(
            "source run exact-count gate failed: "
            f"evidence_ready/requested/requested_count="
            f"{evidence_ready}/{requested}/{requested_count}"
        )

    saved_master = _text(run.get("master_database"))
    if not saved_master or _path_key(saved_master) != _path_key(master_database):
        raise RunLineageError(
            "supplied master database does not match the source run's immutable path"
        )

    rows = conn.execute(
        """
        SELECT post_id, url, evidence_json, evidence_hash
        FROM engage_tiktok_posts
        WHERE run_id=? AND evidence_ready=1
        """,
        (run_id,),
    ).fetchall()
    if len(rows) != requested_count:
        raise RunLineageError(
            "local evidence-ready row count does not match requested_count: "
            f"{len(rows)}/{requested_count}"
        )

    local_posts: dict[str, dict[str, Any]] = {}
    for evidence_row in rows:
        post_id = _text(evidence_row["post_id"])
        if not post_id or not post_id.isdigit() or post_id in local_posts:
            raise RunLineageError("local evidence contains an invalid or duplicate post ID")
        evidence = _parse_json_object(
            evidence_row["evidence_json"],
            field=f"local evidence_json for post {post_id}",
        )
        evidence_hash = _required_sha256(
            evidence_row["evidence_hash"],
            field=f"local evidence_hash for post {post_id}",
        )
        if canonical_sha256(evidence) != evidence_hash:
            raise RunLineageError(f"local evidence hash mismatch for post {post_id}")
        if _text(evidence.get("post_id")) != post_id:
            raise RunLineageError(f"local evidence post ID mismatch for post {post_id}")
        if evidence.get("evidence_ready") is not True:
            raise RunLineageError(f"local evidence is not ready for post {post_id}")
        if _text(evidence.get("platform")).casefold() != "tiktok":
            raise RunLineageError(f"local evidence platform mismatch for post {post_id}")

        identity = _canonical_post_identity(
            evidence.get("url"),
            expected_post_id=post_id,
            field=f"local evidence URL for post {post_id}",
        )
        row_identity = _canonical_post_identity(
            evidence_row["url"],
            expected_post_id=post_id,
            field=f"local post URL for post {post_id}",
        )
        if identity != row_identity:
            raise RunLineageError(f"local URL binding mismatch for post {post_id}")
        creator = _text(evidence.get("creator")).lstrip("@").casefold()
        if creator != identity["creator_handle"]:
            raise RunLineageError(f"local creator binding mismatch for post {post_id}")
        content_type = _text(evidence.get("content_type")).casefold()
        if content_type != identity["content_type"]:
            raise RunLineageError(f"local content type mismatch for post {post_id}")
        local_posts[post_id] = {
            "evidence": evidence,
            "evidence_hash": evidence_hash,
            **identity,
        }

    return run, local_posts


_MASTER_STRING_FIELDS = (
    "project",
    "workflow",
    "topic",
    "source_mode",
    "direct_post_url",
    "creator_handle",
    "cardinality_mode",
    "profile_inventory_hash",
    "collection_policy",
    "mode",
    "expected_account",
    "observed_account",
    "status",
    "error",
    "created_at",
    "updated_at",
)
_MASTER_INTEGER_FIELDS = (
    "profile_inventory_count",
    "profile_inventory_terminal",
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
)
_CASEFOLD_MASTER_FIELDS = frozenset(
    {
        "workflow",
        "source_mode",
        "cardinality_mode",
        "collection_policy",
        "mode",
        "status",
    }
)
_HANDLE_MASTER_FIELDS = frozenset(
    {"creator_handle", "expected_account", "observed_account"}
)


def _normalized_run_field(field: str, value: Any) -> str:
    normalized = _text(value)
    if field in _HANDLE_MASTER_FIELDS:
        normalized = normalized.lstrip("@").casefold()
    elif field in _CASEFOLD_MASTER_FIELDS:
        normalized = normalized.casefold()
    return normalized


def _validate_master_run(
    conn: sqlite3.Connection,
    *,
    source_database: Path,
    run_id: str,
    local_run: Mapping[str, Any],
    local_posts: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    _require_columns(
        conn,
        "tiktok_master_sources",
        {"source_id", "database_path"},
    )
    _require_columns(
        conn,
        "tiktok_master_runs",
        {
            "master_run_id",
            "source_id",
            "local_run_id",
            *_MASTER_STRING_FIELDS,
            *_MASTER_INTEGER_FIELDS,
        },
    )
    _require_columns(
        conn,
        "tiktok_master_run_posts",
        {
            "master_run_id",
            "post_id",
            "snapshot_id",
            "collection_policy",
            "position",
        },
    )
    _require_columns(
        conn,
        "tiktok_master_snapshots",
        {
            "snapshot_id",
            "post_id",
            "source_id",
            "master_run_id",
            "local_run_id",
            "evidence_hash",
            "evidence_json",
        },
    )

    try:
        source = resolve_registered_source(conn, "main", source_database)
    except SourceIdentityError as exc:
        raise RunLineageError(str(exc)) from exc
    if source is None:
        raise RunLineageError(
            "master registry must contain exactly one source bound to the resolved "
            "project database; found 0"
        )
    source_id = _text(source["source_id"])

    master_rows = conn.execute(
        """
        SELECT *
        FROM tiktok_master_runs
        WHERE source_id=? AND local_run_id=?
        """,
        (source_id, run_id),
    ).fetchall()
    if len(master_rows) != 1:
        raise RunLineageError(
            "master registry must contain exactly one run for the source path and "
            f"local run ID; found {len(master_rows)}"
        )
    master_run = _row_dict(master_rows[0])
    master_run_id = _text(master_run.get("master_run_id"))
    if master_run_id != _stable_id(source_id, run_id):
        raise RunLineageError("master run ID binding failed")

    for field in _MASTER_STRING_FIELDS:
        if _normalized_run_field(field, local_run.get(field)) != _normalized_run_field(
            field, master_run.get(field)
        ):
            raise RunLineageError(f"master run field mismatch: {field}")
    for field in _MASTER_INTEGER_FIELDS:
        if _required_int(local_run, field) != _required_int(master_run, field):
            raise RunLineageError(f"master run counter mismatch: {field}")

    links = conn.execute(
        """
        SELECT
            rp.post_id AS linked_post_id,
            rp.snapshot_id AS linked_snapshot_id,
            rp.collection_policy AS linked_collection_policy,
            rp.position AS linked_position,
            s.snapshot_id AS snapshot_id,
            s.post_id AS snapshot_post_id,
            s.source_id AS snapshot_source_id,
            s.master_run_id AS snapshot_master_run_id,
            s.local_run_id AS snapshot_local_run_id,
            s.evidence_hash AS snapshot_evidence_hash,
            s.evidence_json AS snapshot_evidence_json
        FROM tiktok_master_run_posts AS rp
        LEFT JOIN tiktok_master_snapshots AS s
          ON s.snapshot_id = rp.snapshot_id
        WHERE rp.master_run_id=?
        ORDER BY rp.position, rp.post_id
        """,
        (master_run_id,),
    ).fetchall()
    requested_count = _required_int(local_run, "requested_count")
    if len(links) != requested_count:
        raise RunLineageError(
            "master run-post link count does not match requested_count: "
            f"{len(links)}/{requested_count}"
        )

    candidates: list[dict[str, Any]] = []
    seen_post_ids: set[str] = set()
    for expected_position, link in enumerate(links, start=1):
        try:
            position = int(link["linked_position"])
        except (TypeError, ValueError) as exc:
            raise RunLineageError("master run-post position is invalid") from exc
        if position != expected_position:
            raise RunLineageError(
                "master run-post positions must be contiguous and one-based"
            )
        post_id = _text(link["linked_post_id"])
        if post_id in seen_post_ids or post_id not in local_posts:
            raise RunLineageError(
                f"master run-post does not match local evidence: {post_id or 'missing'}"
            )
        seen_post_ids.add(post_id)
        if link["snapshot_id"] is None:
            raise RunLineageError(f"master snapshot is missing for post {post_id}")
        if _text(link["snapshot_id"]) != _text(link["linked_snapshot_id"]):
            raise RunLineageError(f"master snapshot link mismatch for post {post_id}")
        if _text(link["snapshot_post_id"]) != post_id:
            raise RunLineageError(f"master snapshot post ID mismatch for post {post_id}")
        if _text(link["snapshot_source_id"]) != source_id:
            raise RunLineageError(f"master snapshot source mismatch for post {post_id}")
        if _text(link["snapshot_master_run_id"]) != master_run_id:
            raise RunLineageError(f"master snapshot run mismatch for post {post_id}")
        if _text(link["snapshot_local_run_id"]) != run_id:
            raise RunLineageError(
                f"master snapshot local-run mismatch for post {post_id}"
            )
        local = local_posts[post_id]
        collection_policy = _text(local_run.get("collection_policy")).casefold()
        if _text(link["linked_collection_policy"]).casefold() != collection_policy:
            raise RunLineageError(
                f"master run-post collection policy mismatch for post {post_id}"
            )
        snapshot_hash = _required_sha256(
            link["snapshot_evidence_hash"],
            field=f"master snapshot evidence_hash for post {post_id}",
        )
        if snapshot_hash != local["evidence_hash"]:
            raise RunLineageError(f"master/local evidence hash mismatch for post {post_id}")
        snapshot_evidence = _parse_json_object(
            link["snapshot_evidence_json"],
            field=f"master snapshot evidence_json for post {post_id}",
        )
        if canonical_sha256(snapshot_evidence) != snapshot_hash:
            raise RunLineageError(f"master snapshot hash mismatch for post {post_id}")
        if canonical_json(snapshot_evidence) != canonical_json(local["evidence"]):
            raise RunLineageError(
                f"master snapshot does not equal local evidence for post {post_id}"
            )
        snapshot_identity = _canonical_post_identity(
            snapshot_evidence.get("url"),
            expected_post_id=post_id,
            field=f"master snapshot URL for post {post_id}",
        )
        if any(
            snapshot_identity[key] != local[key]
            for key in ("canonical_url", "creator_handle", "content_type")
        ):
            raise RunLineageError(
                f"master snapshot identity does not match local evidence for post {post_id}"
            )
        candidates.append(
            {
                "ordinal": position,
                "post_id": post_id,
                "canonical_url": local["canonical_url"],
                "creator_handle": local["creator_handle"],
                "content_type": local["content_type"],
                "snapshot_id": _text(link["snapshot_id"]),
                "evidence_hash": snapshot_hash,
            }
        )

    if seen_post_ids != set(local_posts):
        raise RunLineageError("master run-post set does not match local evidence set")
    return master_run, candidates


def load_completed_run_lineage(
    source_database: str | Path,
    source_run_id: str,
    master_database: str | Path,
) -> dict[str, Any]:
    """Return a hash-bound, ordered selection from one completed source run.

    Both databases are opened through SQLite URI ``mode=ro`` and set to
    ``query_only``.  The master relation's immutable ``snapshot_id`` is the
    evidence authority; the mutable ``latest_snapshot_id`` pointer is never
    consulted.
    """

    run_id = _text(source_run_id)
    if not run_id:
        raise RunLineageError("source_run_id is required")
    source_path = _resolve_database(source_database, field="source_database")
    master_path = _resolve_database(master_database, field="master_database")
    if _path_key(source_path) == _path_key(master_path):
        raise RunLineageError("source_database and master_database must be distinct")

    with ExitStack() as stack:
        source_conn = stack.enter_context(closing(_open_read_only(source_path)))
        master_conn = stack.enter_context(closing(_open_read_only(master_path)))
        local_run, local_posts = _validate_local_run(
            source_conn,
            run_id=run_id,
            source_database=source_path,
            master_database=master_path,
        )
        master_run, candidates = _validate_master_run(
            master_conn,
            source_database=source_path,
            run_id=run_id,
            local_run=local_run,
            local_posts=local_posts,
        )

    source_run = {
        "database_path": str(source_path),
        "run_id": run_id,
        "master_database_path": str(master_path),
        "source_id": _text(master_run.get("source_id")),
        "master_run_id": _text(master_run.get("master_run_id")),
        "project": _text(local_run.get("project")),
        "workflow": _text(local_run.get("workflow")).casefold(),
        "status": _text(local_run.get("status")).casefold(),
        "source_mode": _text(local_run.get("source_mode")).casefold(),
        "topic": _text(local_run.get("topic")),
        "creator_handle": _text(local_run.get("creator_handle"))
        .lstrip("@")
        .casefold(),
        "direct_post_url": _text(local_run.get("direct_post_url")),
        "collection_policy": _text(local_run.get("collection_policy")).casefold(),
        "cardinality_mode": _text(local_run.get("cardinality_mode")).casefold(),
        "requested_count": _required_int(local_run, "requested_count"),
        "requested": _required_int(local_run, "requested"),
        "evidence_ready": _required_int(local_run, "evidence_ready"),
        "expected_account": _text(local_run.get("expected_account"))
        .lstrip("@")
        .casefold(),
        "observed_account": _text(local_run.get("observed_account"))
        .lstrip("@")
        .casefold(),
        "created_at": _text(local_run.get("created_at")),
        "updated_at": _text(local_run.get("updated_at")),
    }
    result = {
        "schema_version": LINEAGE_SCHEMA_VERSION,
        "source_run": source_run,
        "candidates": candidates,
    }
    result["lineage_hash"] = canonical_sha256(result)
    return result


def load_archive_run_posts(
    source_database: str | Path,
    source_run_id: str,
) -> dict[str, Any]:
    """Load evidence-ready TikTok posts from any local workflow project.

    This is the intentionally simple AUDIO ARCHIVE source adapter.  Unlike the
    canonical lineage validator above, it does not require a terminal run,
    historical master-registry rows, or the database's original absolute path.
    That makes archived and reorganized projects usable without rewriting
    either database.  Invalid individual rows are reported in ``skipped`` and
    do not prevent other evidence-ready rows from being archived.
    """

    run_id = _text(source_run_id)
    if not run_id:
        raise RunLineageError("source_run_id is required")
    source_path = _resolve_readable_database(
        source_database,
        field="source_database",
    )

    with closing(_open_read_only(source_path)) as conn:
        run_columns = _table_columns(conn, "engage_tiktok_runs")
        post_columns = _table_columns(conn, "engage_tiktok_posts")
        if "run_id" not in run_columns:
            raise RunLineageError(
                "required SQLite table is missing: engage_tiktok_runs"
            )
        required_post_columns = {"run_id", "post_id", "url", "evidence_ready"}
        if not required_post_columns.issubset(post_columns):
            raise RunLineageError(
                "required SQLite table is missing or incompatible: "
                "engage_tiktok_posts"
            )

        run_rows = conn.execute(
            "SELECT * FROM engage_tiktok_runs WHERE run_id=?",
            (run_id,),
        ).fetchall()
        if len(run_rows) != 1:
            raise RunLineageError(
                f"source database must contain exactly one run {run_id}; "
                f"found {len(run_rows)}"
            )
        run = _row_dict(run_rows[0])

        selected_columns = ["rowid AS source_rowid", "run_id", "post_id", "url"]
        for optional in ("status", "evidence_json", "evidence_hash", "collection_error"):
            if optional in post_columns:
                selected_columns.append(optional)
        rows = conn.execute(
            "SELECT "
            + ", ".join(selected_columns)
            + " FROM engage_tiktok_posts "
            + "WHERE run_id=? AND evidence_ready=1 ORDER BY rowid",
            (run_id,),
        ).fetchall()

    candidates: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    seen: set[str] = set()
    for source_ordinal, row in enumerate(rows, start=1):
        item = _row_dict(row)
        post_id = _text(item.get("post_id"))
        if not post_id or post_id in seen:
            skipped.append(
                {
                    "post_id": post_id,
                    "reason": "missing_or_duplicate_post_id",
                }
            )
            continue

        packet: dict[str, Any] = {}
        raw_packet = _text(item.get("evidence_json"))
        raw_url = _text(item.get("url"))
        stored_hash = _text(item.get("evidence_hash")).casefold()
        if (not raw_url or _SHA256.fullmatch(stored_hash) is None) and raw_packet:
            try:
                parsed = json.loads(raw_packet)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                packet = parsed
        raw_url = raw_url or _text(packet.get("url"))
        try:
            identity = _canonical_post_identity(
                raw_url,
                expected_post_id=post_id,
                field="post URL",
            )
        except RunLineageError:
            skipped.append(
                {
                    "post_id": post_id,
                    "reason": "invalid_tiktok_post_url",
                }
            )
            continue

        evidence_hash = (
            stored_hash
            if _SHA256.fullmatch(stored_hash)
            else canonical_sha256(packet or {"post_id": post_id, "url": raw_url})
        )
        snapshot_id = _stable_id(
            "audio-archive-local-evidence",
            str(source_path),
            run_id,
            post_id,
            evidence_hash,
        )
        candidates.append(
            {
                "ordinal": source_ordinal,
                "post_id": post_id,
                "canonical_url": identity["canonical_url"],
                "creator_handle": identity["creator_handle"],
                "content_type": identity["content_type"],
                "snapshot_id": snapshot_id,
                "evidence_hash": evidence_hash,
            }
        )
        seen.add(post_id)

    def run_value(field: str, default: Any = "") -> Any:
        return run.get(field, default)

    source_run = {
        "database_path": str(source_path),
        "run_id": run_id,
        "master_database_path": _text(run_value("master_database")),
        "source_id": _stable_id("audio-archive-source", str(source_path)),
        "master_run_id": _stable_id(
            "audio-archive-run",
            str(source_path),
            run_id,
        ),
        "project": _text(run_value("project")) or source_path.parent.parent.name,
        "workflow": _text(run_value("workflow", "listen")).casefold() or "listen",
        "status": _text(run_value("status")).casefold(),
        "source_mode": _text(run_value("source_mode")).casefold(),
        "topic": _text(run_value("topic")),
        "creator_handle": _text(run_value("creator_handle")).lstrip("@").casefold(),
        "direct_post_url": _text(run_value("direct_post_url")),
        "collection_policy": _text(run_value("collection_policy")).casefold(),
        "cardinality_mode": _text(run_value("cardinality_mode")).casefold(),
        "requested_count": int(run_value("requested_count", 0) or 0),
        "requested": int(run_value("requested", 0) or 0),
        "evidence_ready": len(candidates),
        "expected_account": _text(run_value("expected_account")).lstrip("@").casefold(),
        "observed_account": _text(run_value("observed_account")).lstrip("@").casefold(),
        "created_at": _text(run_value("created_at")),
        "updated_at": _text(run_value("updated_at")),
    }
    result = {
        "schema_version": "tiktok-audio-archive-source-v2",
        "source_run": source_run,
        "candidates": candidates,
        "skipped": skipped,
    }
    result["lineage_hash"] = canonical_sha256(result)
    return result


__all__ = [
    "LINEAGE_SCHEMA_VERSION",
    "RunLineageError",
    "canonical_json",
    "canonical_sha256",
    "load_archive_run_posts",
    "load_completed_run_lineage",
]
