"""Isolated, retention-aware collection state for LinkedIn Page evidence.

LinkedIn data is deliberately kept out of the TikTok master registry and the
platform-neutral social-music export database.  Member-authored comment text is
short lived, and organization post evidence is purged after its configured
retention window.  Only LinkedIn resource URNs and deletion tombstones survive
the purge.

This module owns collection state only.  It has no OAuth, HTTP, AI, drafting,
approval, or publication behavior.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence
from urllib.parse import unquote, urlsplit


SCHEMA_VERSION = "linkedin-collection-state-v1"
WORKFLOWS = frozenset({"listen", "engage"})
SOURCE_MODES = frozenset({"organization", "post"})
RUN_STATUSES = frozenset(
    {"collecting", "collection_complete", "collection_incomplete"}
)
COMMENT_RETENTION_HOURS = 48
ORGANIZATION_POST_RETENTION_DAYS = 180

_HASH_RE = re.compile(r"[0-9a-f]{64}")
_ORGANIZATION_URN_RE = re.compile(r"urn:li:organization:[1-9][0-9]*")
_POST_URN_RE = re.compile(r"urn:li:(?:share|ugcPost):[1-9][0-9]*")
_COMMENT_URN_RE = re.compile(
    r"urn:li:comment:\(urn:li:activity:[1-9][0-9]*,[1-9][0-9]*\)"
)
_ACTOR_URN_RE = re.compile(
    r"urn:li:(?:person:[A-Za-z0-9_-]+|organization(?:Brand)?:[1-9][0-9]*)"
)


class LinkedInStateError(RuntimeError):
    """Base class for fail-closed LinkedIn state errors."""


class InvalidLinkedInStateInput(LinkedInStateError, ValueError):
    """A caller supplied an invalid run or evidence value."""


class LinkedInStateConflict(LinkedInStateError):
    """A mutation conflicts with frozen or terminal state."""


class LinkedInStateIntegrityError(LinkedInStateError):
    """Stored state no longer satisfies its hash bindings."""


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso(value: dt.datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(value: Any) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise InvalidLinkedInStateInput("timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc).replace(microsecond=0)


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
        raise InvalidLinkedInStateInput("value is not canonical JSON") from exc


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _required_text(value: Any, name: str, *, limit: int = 500) -> str:
    result = str(value or "").strip()
    if not result or len(result) > limit:
        raise InvalidLinkedInStateInput(f"{name} is invalid")
    return result


def _organization_urn(value: Any) -> str:
    result = _required_text(value, "organization_urn", limit=128)
    if _ORGANIZATION_URN_RE.fullmatch(result) is None:
        raise InvalidLinkedInStateInput(
            "organization_urn must be urn:li:organization:<numeric-id>"
        )
    return result


def _post_urn(value: Any) -> str:
    result = _required_text(value, "post_urn", limit=160)
    if _POST_URN_RE.fullmatch(result) is None:
        raise InvalidLinkedInStateInput(
            "post_urn must be a LinkedIn share or ugcPost URN"
        )
    return result


def _comment_urn(value: Any) -> str:
    result = _required_text(value, "comment_urn", limit=512)
    if _COMMENT_URN_RE.fullmatch(result) is None:
        raise InvalidLinkedInStateInput("comment_urn is invalid")
    return result


def _comment_rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise InvalidLinkedInStateInput("comments must be a list")
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, Mapping):
            raise InvalidLinkedInStateInput("comment must be a mapping")
        comment_urn = _comment_urn(raw.get("comment_urn"))
        if comment_urn in seen:
            raise InvalidLinkedInStateInput("comments contain a duplicate URN")
        seen.add(comment_urn)
        parent = str(raw.get("parent_comment_urn") or "").strip()
        if parent:
            parent = _comment_urn(parent)
        actor = str(raw.get("actor_urn") or "").strip()
        if actor and _ACTOR_URN_RE.fullmatch(actor) is None:
            raise InvalidLinkedInStateInput("comment actor_urn is invalid")
        message = str(raw.get("message") or "").strip()[:5_000]
        likes = raw.get("likes")
        if likes is not None:
            try:
                likes = max(0, int(likes))
            except (TypeError, ValueError) as exc:
                raise InvalidLinkedInStateInput("comment likes is invalid") from exc
        output.append(
            {
                "comment_urn": comment_urn,
                "parent_comment_urn": parent,
                "actor_urn": actor,
                "message": message,
                "likes": likes,
                "created_at": str(raw.get("created_at") or "")[:80],
            }
        )
    return output


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise InvalidLinkedInStateInput(f"{name} must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidLinkedInStateInput(
            f"{name} must be a positive integer"
        ) from exc
    if result < 1:
        raise InvalidLinkedInStateInput(f"{name} must be a positive integer")
    return result


def _hash(value: Any, name: str) -> str:
    result = str(value or "").strip().casefold()
    if _HASH_RE.fullmatch(result) is None:
        raise InvalidLinkedInStateInput(f"{name} must be a SHA-256 digest")
    return result


def _workflow(value: Any) -> str:
    result = str(value or "").strip().casefold()
    if result not in WORKFLOWS:
        raise InvalidLinkedInStateInput("workflow must be listen or engage")
    return result


def _source_mode(value: Any) -> str:
    result = str(value or "").strip().casefold()
    if result not in SOURCE_MODES:
        raise InvalidLinkedInStateInput("source_mode must be organization or post")
    return result


def _scope_document(
    *,
    run_id: str,
    project: str,
    workflow: str,
    source_mode: str,
    organization_urn: str,
    exact_post_urn: str,
    requested_count: int,
    api_version: str,
    comments_per_post: int,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "project": project,
        "platform": "linkedin",
        "workflow": workflow,
        "source_mode": source_mode,
        "organization_urn": organization_urn,
        "exact_post_urn": exact_post_urn,
        "requested_count": requested_count,
        "api_version": api_version,
        "comments_per_post": comments_per_post,
        "retention": {
            "comment_hours": COMMENT_RETENTION_HOURS,
            "organization_post_days": ORGANIZATION_POST_RETENTION_DAYS,
        },
        "publication_enabled": False,
        "external_ai_enabled": False,
    }


class LinkedInCollectionState:
    """SQLite-backed collection state with automatic LinkedIn TTL purging."""

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
        self._connection.execute("PRAGMA secure_delete=ON")
        if self._file_backed:
            self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._ensure_schema()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "LinkedInCollectionState":
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
            CREATE TABLE IF NOT EXISTS linkedin_runs (
                run_id TEXT PRIMARY KEY,
                schema_version TEXT NOT NULL,
                project TEXT NOT NULL,
                platform TEXT NOT NULL,
                workflow TEXT NOT NULL,
                source_mode TEXT NOT NULL,
                organization_urn TEXT NOT NULL,
                exact_post_urn TEXT NOT NULL DEFAULT '',
                requested_count INTEGER NOT NULL,
                api_version TEXT NOT NULL,
                comments_per_post INTEGER NOT NULL,
                scope_hash TEXT NOT NULL,
                inventory_json TEXT NOT NULL DEFAULT '[]',
                inventory_hash TEXT NOT NULL DEFAULT '',
                inventory_frozen INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                incomplete_reasons_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS linkedin_posts (
                run_id TEXT NOT NULL,
                post_urn TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                organization_urn TEXT NOT NULL,
                canonical_url TEXT NOT NULL,
                published_at TEXT NOT NULL DEFAULT '',
                commentary TEXT NOT NULL DEFAULT '',
                content_json TEXT NOT NULL DEFAULT '{}',
                metrics_json TEXT NOT NULL DEFAULT '{}',
                collection_json TEXT NOT NULL DEFAULT '{}',
                evidence_hash TEXT NOT NULL DEFAULT '',
                evidence_ready INTEGER NOT NULL DEFAULT 0,
                error_code TEXT NOT NULL DEFAULT '',
                observed_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                purged_at TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (run_id, post_urn),
                UNIQUE (run_id, ordinal),
                FOREIGN KEY (run_id) REFERENCES linkedin_runs(run_id)
            );

            CREATE TABLE IF NOT EXISTS linkedin_comments (
                run_id TEXT NOT NULL,
                post_urn TEXT NOT NULL,
                comment_urn TEXT NOT NULL,
                parent_comment_urn TEXT NOT NULL DEFAULT '',
                actor_urn TEXT NOT NULL DEFAULT '',
                message TEXT NOT NULL DEFAULT '',
                likes INTEGER,
                created_at TEXT NOT NULL DEFAULT '',
                observed_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                PRIMARY KEY (run_id, comment_urn),
                FOREIGN KEY (run_id, post_urn)
                    REFERENCES linkedin_posts(run_id, post_urn)
            );

            CREATE TABLE IF NOT EXISTS linkedin_comment_tombstones (
                run_id TEXT NOT NULL,
                post_urn TEXT NOT NULL,
                comment_urn TEXT NOT NULL,
                purged_at TEXT NOT NULL,
                PRIMARY KEY (run_id, comment_urn)
            );

            CREATE TABLE IF NOT EXISTS linkedin_known_posts (
                organization_urn TEXT NOT NULL,
                post_urn TEXT NOT NULL,
                first_run_id TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                PRIMARY KEY (organization_urn, post_urn)
            );

            CREATE INDEX IF NOT EXISTS linkedin_posts_run_ready_idx
                ON linkedin_posts(run_id, evidence_ready, ordinal);
            CREATE INDEX IF NOT EXISTS linkedin_comments_expiry_idx
                ON linkedin_comments(expires_at);
            CREATE INDEX IF NOT EXISTS linkedin_posts_expiry_idx
                ON linkedin_posts(expires_at, purged_at);
            """
        )

    def create_run(
        self,
        run_id: str,
        *,
        project: str,
        workflow: str,
        source_mode: str,
        organization_urn: str,
        requested_count: int,
        api_version: str,
        comments_per_post: int,
        exact_post_urn: str = "",
    ) -> dict[str, Any]:
        normalized_run_id = _required_text(run_id, "run_id", limit=200)
        if any(character.isspace() for character in normalized_run_id):
            raise InvalidLinkedInStateInput("run_id cannot contain whitespace")
        normalized_project = _required_text(project, "project", limit=200)
        normalized_workflow = _workflow(workflow)
        normalized_source = _source_mode(source_mode)
        normalized_organization = _organization_urn(organization_urn)
        normalized_count = _positive_int(requested_count, "requested_count")
        normalized_version = _required_text(api_version, "api_version", limit=12)
        if re.fullmatch(r"20[0-9]{4}", normalized_version) is None:
            raise InvalidLinkedInStateInput("api_version must use YYYYMM")
        normalized_comments = _positive_int(comments_per_post, "comments_per_post")
        normalized_exact = _post_urn(exact_post_urn) if exact_post_urn else ""
        if normalized_source == "post":
            if not normalized_exact or normalized_count != 1:
                raise InvalidLinkedInStateInput(
                    "post source requires exact_post_urn and requested_count=1"
                )
        elif normalized_exact:
            raise InvalidLinkedInStateInput(
                "exact_post_urn is valid only for post source"
            )
        scope = _scope_document(
            run_id=normalized_run_id,
            project=normalized_project,
            workflow=normalized_workflow,
            source_mode=normalized_source,
            organization_urn=normalized_organization,
            exact_post_urn=normalized_exact,
            requested_count=normalized_count,
            api_version=normalized_version,
            comments_per_post=normalized_comments,
        )
        scope_hash = canonical_sha256(scope)
        timestamp = iso(utc_now())
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM linkedin_runs WHERE run_id=?",
                (normalized_run_id,),
            ).fetchone()
            if existing is not None:
                self._verify_run(existing)
                if existing["scope_hash"] != scope_hash:
                    raise LinkedInStateConflict(
                        "run_id already exists with a different immutable scope"
                    )
                return self._status(connection, normalized_run_id)
            connection.execute(
                """
                INSERT INTO linkedin_runs (
                    run_id, schema_version, project, platform, workflow,
                    source_mode, organization_urn, exact_post_urn,
                    requested_count, api_version, comments_per_post,
                    scope_hash, status, created_at, updated_at
                ) VALUES (?, ?, ?, 'linkedin', ?, ?, ?, ?, ?, ?, ?, ?,
                          'collecting', ?, ?)
                """,
                (
                    normalized_run_id,
                    SCHEMA_VERSION,
                    normalized_project,
                    normalized_workflow,
                    normalized_source,
                    normalized_organization,
                    normalized_exact,
                    normalized_count,
                    normalized_version,
                    normalized_comments,
                    scope_hash,
                    timestamp,
                    timestamp,
                ),
            )
            return self._status(connection, normalized_run_id)

    def _verify_run(self, row: sqlite3.Row) -> None:
        if row["schema_version"] != SCHEMA_VERSION or row["platform"] != "linkedin":
            raise LinkedInStateIntegrityError("run schema/platform binding is invalid")
        if row["workflow"] not in WORKFLOWS or row["source_mode"] not in SOURCE_MODES:
            raise LinkedInStateIntegrityError("run workflow/source binding is invalid")
        if row["status"] not in RUN_STATUSES:
            raise LinkedInStateIntegrityError("run status is invalid")
        scope = _scope_document(
            run_id=str(row["run_id"]),
            project=str(row["project"]),
            workflow=str(row["workflow"]),
            source_mode=str(row["source_mode"]),
            organization_urn=str(row["organization_urn"]),
            exact_post_urn=str(row["exact_post_urn"]),
            requested_count=int(row["requested_count"]),
            api_version=str(row["api_version"]),
            comments_per_post=int(row["comments_per_post"]),
        )
        if canonical_sha256(scope) != row["scope_hash"]:
            raise LinkedInStateIntegrityError("run scope hash mismatch")
        if bool(row["inventory_frozen"]):
            try:
                inventory = json.loads(row["inventory_json"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise LinkedInStateIntegrityError("inventory JSON is invalid") from exc
            if not isinstance(inventory, list) or any(
                not isinstance(value, str) or _POST_URN_RE.fullmatch(value) is None
                for value in inventory
            ):
                raise LinkedInStateIntegrityError("inventory shape is invalid")
            if len(inventory) != len(set(inventory)):
                raise LinkedInStateIntegrityError("inventory contains duplicate URNs")
            if canonical_sha256(inventory) != row["inventory_hash"]:
                raise LinkedInStateIntegrityError("inventory hash mismatch")

    def _run_row(self, connection: sqlite3.Connection, run_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM linkedin_runs WHERE run_id=?",
            (_required_text(run_id, "run_id", limit=200),),
        ).fetchone()
        if row is None:
            raise InvalidLinkedInStateInput("run_id was not found")
        self._verify_run(row)
        return row

    def get_run(self, run_id: str) -> dict[str, Any]:
        self.purge_expired()
        row = self._run_row(self._connection, run_id)
        return dict(row)

    def freeze_inventory(self, run_id: str, post_urns: Sequence[str]) -> dict[str, Any]:
        normalized: list[str] = []
        seen: set[str] = set()
        for value in post_urns:
            urn = _post_urn(value)
            if urn in seen:
                raise InvalidLinkedInStateInput("inventory contains duplicate post URNs")
            seen.add(urn)
            normalized.append(urn)
        inventory_hash = canonical_sha256(normalized)
        timestamp = iso(utc_now())
        with self._transaction() as connection:
            run = self._run_row(connection, run_id)
            if run["status"] != "collecting":
                raise LinkedInStateConflict("terminal runs cannot freeze inventory")
            if bool(run["inventory_frozen"]):
                if (
                    json.loads(run["inventory_json"]) != normalized
                    or run["inventory_hash"] != inventory_hash
                ):
                    raise LinkedInStateConflict("run inventory is immutable")
                return self._status(connection, run_id)
            if run["source_mode"] == "post" and not normalized:
                raise LinkedInStateConflict("exact-post inventory cannot be empty")
            if run["source_mode"] == "post" and normalized != [run["exact_post_urn"]]:
                raise LinkedInStateConflict("exact-post inventory binding mismatch")
            connection.execute(
                """
                UPDATE linkedin_runs SET inventory_json=?, inventory_hash=?,
                    inventory_frozen=1, updated_at=? WHERE run_id=?
                """,
                (canonical_json(normalized), inventory_hash, timestamp, run_id),
            )
            return self._status(connection, run_id)

    def resume_incomplete(self, run_id: str) -> dict[str, Any]:
        """Reopen an incomplete immutable selection so failed rows can be repaired.

        The source, inventory, count, API version, and retention contract remain
        frozen.  A complete run is never reopened, and no inventory mutation is
        permitted by this transition.
        """

        timestamp = iso(utc_now())
        with self._transaction() as connection:
            run = self._run_row(connection, run_id)
            if run["status"] == "collection_complete":
                raise LinkedInStateConflict("complete runs cannot be resumed")
            if run["status"] == "collection_incomplete":
                connection.execute(
                    """
                    UPDATE linkedin_runs SET status='collecting',
                        incomplete_reasons_json='[]', updated_at=?
                    WHERE run_id=?
                    """,
                    (timestamp, run_id),
                )
            return self._status(connection, run_id)

    def inventory(self, run_id: str) -> list[str]:
        row = self._run_row(self._connection, run_id)
        if not bool(row["inventory_frozen"]):
            return []
        try:
            values = json.loads(row["inventory_json"])
        except json.JSONDecodeError as exc:
            raise LinkedInStateIntegrityError("inventory JSON is invalid") from exc
        if not isinstance(values, list) or any(
            not isinstance(value, str) or _POST_URN_RE.fullmatch(value) is None
            for value in values
        ):
            raise LinkedInStateIntegrityError("inventory shape is invalid")
        if canonical_sha256(values) != row["inventory_hash"]:
            raise LinkedInStateIntegrityError("inventory hash mismatch")
        return list(values)

    def known_post_urns(self, organization_urn: str) -> set[str]:
        rows = self._connection.execute(
            """
            SELECT post_urn FROM linkedin_known_posts
            WHERE organization_urn=? ORDER BY post_urn
            """,
            (_organization_urn(organization_urn),),
        ).fetchall()
        return {str(row["post_urn"]) for row in rows}

    def post_rows(self, run_id: str) -> list[dict[str, Any]]:
        self._run_row(self._connection, run_id)
        rows = self._connection.execute(
            "SELECT * FROM linkedin_posts WHERE run_id=? ORDER BY ordinal",
            (run_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def checkpoint_post(
        self,
        run_id: str,
        *,
        ordinal: int,
        observation: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(observation, Mapping):
            raise InvalidLinkedInStateInput("observation must be a mapping")
        normalized_ordinal = _positive_int(ordinal, "ordinal")
        post_urn = _post_urn(observation.get("post_urn"))
        organization_urn = _organization_urn(observation.get("organization_urn"))
        commentary = str(observation.get("commentary") or "").strip()[:10_000]
        canonical_url = str(observation.get("canonical_url") or "").strip()
        try:
            url = urlsplit(canonical_url)
            invalid_port = url.port not in (None, 443)
        except ValueError:
            invalid_port = True
            url = urlsplit("")
        if (
            url.scheme.casefold() != "https"
            or (url.hostname or "").casefold() != "www.linkedin.com"
            or url.username is not None
            or url.password is not None
            or invalid_port
            or url.query
            or url.fragment
            or unquote(url.path).rstrip("/") != f"/feed/update/{post_urn}"
        ):
            raise InvalidLinkedInStateInput("canonical_url must be a LinkedIn HTTPS URL")
        published_at = str(observation.get("published_at") or "").strip()[:80]
        raw_content = observation.get("content")
        raw_metrics = observation.get("metrics")
        raw_collection = observation.get("collection")
        content: dict[str, Any] = (
            dict(raw_content) if isinstance(raw_content, Mapping) else {}
        )
        metrics: dict[str, Any] = (
            dict(raw_metrics) if isinstance(raw_metrics, Mapping) else {}
        )
        collection: dict[str, Any] = (
            dict(raw_collection) if isinstance(raw_collection, Mapping) else {}
        )
        comments = _comment_rows(observation.get("comments"))
        observed = parse_iso(observation.get("observed_at") or iso(utc_now()))
        post_expires = observed + dt.timedelta(days=ORGANIZATION_POST_RETENTION_DAYS)
        comment_expires = observed + dt.timedelta(hours=COMMENT_RETENTION_HOURS)
        evidence = {
            "schema_version": "linkedin-organization-post-evidence-v1",
            "platform": "linkedin",
            "post_urn": post_urn,
            "organization_urn": organization_urn,
            "canonical_url": canonical_url,
            "published_at": published_at,
            "commentary": commentary,
            "content": content,
            "metrics": metrics,
            "collection": collection,
            "observed_at": iso(observed),
            "expires_at": iso(post_expires),
        }
        evidence_hash = canonical_sha256(evidence)
        timestamp = iso(utc_now())
        with self._transaction() as connection:
            run = self._run_row(connection, run_id)
            if run["status"] != "collecting" or not bool(run["inventory_frozen"]):
                raise LinkedInStateConflict(
                    "checkpoint requires a collecting run with frozen inventory"
                )
            if organization_urn != run["organization_urn"]:
                raise LinkedInStateConflict("post organization binding mismatch")
            if len(comments) > int(run["comments_per_post"]):
                raise InvalidLinkedInStateInput(
                    "comments exceed the frozen comments_per_post limit"
                )
            if (
                collection.get("authority") != "linkedin_official_api"
                or collection.get("api_version") != run["api_version"]
                or collection.get("post_status") != "available"
                or collection.get("social_metadata_status") != "available"
                or collection.get("comments_status") not in {"complete", "truncated"}
                or collection.get("terminal") is not True
            ):
                raise InvalidLinkedInStateInput(
                    "collection must contain terminal official-API outcomes"
                )
            try:
                frozen_comment_limit = int(str(collection.get("comments_limit")))
                retrieved_comments = int(
                    str(collection.get("total_comments_retrieved"))
                )
            except (TypeError, ValueError) as exc:
                raise InvalidLinkedInStateInput(
                    "collection comment bounds are invalid"
                ) from exc
            if (
                frozen_comment_limit != int(run["comments_per_post"])
                or retrieved_comments != len(comments)
            ):
                raise InvalidLinkedInStateInput(
                    "collection comment bounds do not match the frozen run"
                )
            inventory = json.loads(run["inventory_json"])
            if post_urn not in inventory or inventory.index(post_urn) + 1 != normalized_ordinal:
                raise LinkedInStateConflict("post ordinal/inventory binding mismatch")
            existing = connection.execute(
                "SELECT * FROM linkedin_posts WHERE run_id=? AND post_urn=?",
                (run_id, post_urn),
            ).fetchone()
            if existing is not None:
                if bool(existing["evidence_ready"]) and existing["evidence_hash"] == evidence_hash:
                    stored_comments = [
                        {
                            "comment_urn": str(row["comment_urn"]),
                            "parent_comment_urn": str(row["parent_comment_urn"]),
                            "actor_urn": str(row["actor_urn"]),
                            "message": str(row["message"]),
                            "likes": row["likes"],
                            "created_at": str(row["created_at"]),
                        }
                        for row in connection.execute(
                            """
                            SELECT * FROM linkedin_comments
                            WHERE run_id=? AND post_urn=? ORDER BY comment_urn
                            """,
                            (run_id, post_urn),
                        ).fetchall()
                    ]
                    supplied_comments = sorted(
                        comments, key=lambda item: item["comment_urn"]
                    )
                    if canonical_json(stored_comments) != canonical_json(supplied_comments):
                        raise LinkedInStateConflict(
                            "evidence-ready post comments are immutable"
                        )
                    return dict(existing)
                if bool(existing["evidence_ready"]):
                    raise LinkedInStateConflict("evidence-ready post is immutable")
                connection.execute(
                    "DELETE FROM linkedin_comments WHERE run_id=? AND post_urn=?",
                    (run_id, post_urn),
                )
            ready_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM linkedin_posts WHERE run_id=? AND evidence_ready=1",
                    (run_id,),
                ).fetchone()[0]
            )
            if ready_count >= int(run["requested_count"]):
                raise LinkedInStateConflict("requested exact cardinality is already satisfied")
            known = connection.execute(
                """
                SELECT first_run_id FROM linkedin_known_posts
                WHERE organization_urn=? AND post_urn=?
                """,
                (organization_urn, post_urn),
            ).fetchone()
            if known is not None and known["first_run_id"] != run_id:
                raise LinkedInStateConflict("post is already known")
            connection.execute(
                """
                INSERT INTO linkedin_posts (
                    run_id, post_urn, ordinal, organization_urn, canonical_url,
                    published_at, commentary, content_json, metrics_json,
                    collection_json, evidence_hash, evidence_ready, error_code,
                    observed_at, expires_at, purged_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, '', ?, ?, '')
                ON CONFLICT(run_id, post_urn) DO UPDATE SET
                    canonical_url=excluded.canonical_url,
                    published_at=excluded.published_at,
                    commentary=excluded.commentary,
                    content_json=excluded.content_json,
                    metrics_json=excluded.metrics_json,
                    collection_json=excluded.collection_json,
                    evidence_hash=excluded.evidence_hash,
                    evidence_ready=1,
                    error_code='',
                    observed_at=excluded.observed_at,
                    expires_at=excluded.expires_at,
                    purged_at=''
                """,
                (
                    run_id,
                    post_urn,
                    normalized_ordinal,
                    organization_urn,
                    canonical_url,
                    published_at,
                    commentary,
                    canonical_json(content),
                    canonical_json(metrics),
                    canonical_json(collection),
                    evidence_hash,
                    iso(observed),
                    iso(post_expires),
                ),
            )
            for raw in comments:
                connection.execute(
                    """
                    INSERT INTO linkedin_comments (
                        run_id, post_urn, comment_urn, parent_comment_urn,
                        actor_urn, message, likes, created_at, observed_at,
                        expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        post_urn,
                        raw["comment_urn"],
                        raw["parent_comment_urn"],
                        raw["actor_urn"],
                        raw["message"],
                        raw["likes"],
                        raw["created_at"],
                        iso(observed),
                        iso(comment_expires),
                    ),
                )
            connection.execute(
                """
                INSERT OR IGNORE INTO linkedin_known_posts (
                    organization_urn, post_urn, first_run_id, first_seen_at
                ) VALUES (?, ?, ?, ?)
                """,
                (organization_urn, post_urn, run_id, timestamp),
            )
            connection.execute(
                "UPDATE linkedin_runs SET updated_at=? WHERE run_id=?",
                (timestamp, run_id),
            )
            inserted = connection.execute(
                "SELECT * FROM linkedin_posts WHERE run_id=? AND post_urn=?",
                (run_id, post_urn),
            ).fetchone()
            if inserted is None:
                raise LinkedInStateIntegrityError("post checkpoint was not durable")
            return dict(inserted)

    def record_failure(
        self,
        run_id: str,
        *,
        ordinal: int,
        post_urn: str,
        error_code: str,
    ) -> None:
        normalized_post = _post_urn(post_urn)
        normalized_ordinal = _positive_int(ordinal, "ordinal")
        normalized_error = _required_text(error_code, "error_code", limit=120)
        timestamp = iso(utc_now())
        with self._transaction() as connection:
            run = self._run_row(connection, run_id)
            inventory = json.loads(run["inventory_json"])
            if (
                run["status"] != "collecting"
                or not bool(run["inventory_frozen"])
                or normalized_post not in inventory
                or inventory.index(normalized_post) + 1 != normalized_ordinal
            ):
                raise LinkedInStateConflict("failure does not match frozen inventory")
            existing = connection.execute(
                "SELECT evidence_ready FROM linkedin_posts WHERE run_id=? AND post_urn=?",
                (run_id, normalized_post),
            ).fetchone()
            if existing is not None and bool(existing["evidence_ready"]):
                raise LinkedInStateConflict(
                    "failure cannot downgrade an evidence-ready checkpoint"
                )
            connection.execute(
                """
                INSERT INTO linkedin_posts (
                    run_id, post_urn, ordinal, organization_urn, canonical_url,
                    collection_json, evidence_ready, error_code, observed_at,
                    expires_at
                ) VALUES (?, ?, ?, ?, '', '{}', 0, ?, ?, ?)
                ON CONFLICT(run_id, post_urn) DO UPDATE SET
                    evidence_ready=0, error_code=excluded.error_code,
                    observed_at=excluded.observed_at,
                    expires_at=excluded.expires_at
                """,
                (
                    run_id,
                    normalized_post,
                    normalized_ordinal,
                    run["organization_urn"],
                    normalized_error,
                    timestamp,
                    iso(utc_now() + dt.timedelta(days=ORGANIZATION_POST_RETENTION_DAYS)),
                ),
            )

    def finalize(
        self, run_id: str, *, reasons: Iterable[str] | None = None
    ) -> dict[str, Any]:
        normalized_reasons = list(
            dict.fromkeys(str(reason or "").strip()[:300] for reason in (reasons or []))
        )
        normalized_reasons = [reason for reason in normalized_reasons if reason]
        timestamp = iso(utc_now())
        with self._transaction() as connection:
            run = self._run_row(connection, run_id)
            if run["status"] in {"collection_complete", "collection_incomplete"}:
                return self._status(connection, run_id)
            if not bool(run["inventory_frozen"]):
                raise LinkedInStateConflict(
                    "collection cannot finalize before inventory freeze"
                )
            ready = int(
                connection.execute(
                    "SELECT COUNT(*) FROM linkedin_posts WHERE run_id=? AND evidence_ready=1",
                    (run_id,),
                ).fetchone()[0]
            )
            complete = ready == int(run["requested_count"])
            if complete:
                normalized_reasons = []
            elif not normalized_reasons:
                normalized_reasons = ["authorized_organization_inventory_exhausted"]
            connection.execute(
                """
                UPDATE linkedin_runs SET status=?, incomplete_reasons_json=?,
                    updated_at=? WHERE run_id=?
                """,
                (
                    "collection_complete" if complete else "collection_incomplete",
                    canonical_json(normalized_reasons),
                    timestamp,
                    run_id,
                ),
            )
            return self._status(connection, run_id)

    def _status(self, connection: sqlite3.Connection, run_id: str) -> dict[str, Any]:
        run = self._run_row(connection, run_id)
        counts = connection.execute(
            """
            SELECT COUNT(*) AS checkpointed,
                   COALESCE(SUM(evidence_ready), 0) AS ready,
                   COALESCE(SUM(CASE WHEN purged_at<>'' THEN 1 ELSE 0 END), 0) AS purged
            FROM linkedin_posts WHERE run_id=?
            """,
            (run_id,),
        ).fetchone()
        comments = int(
            connection.execute(
                "SELECT COUNT(*) FROM linkedin_comments WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]
        )
        ready = int(counts["ready"])
        purged = int(counts["purged"])
        return {
            "schema_version": SCHEMA_VERSION,
            "run_id": str(run["run_id"]),
            "project": str(run["project"]),
            "platform": "linkedin",
            "workflow": str(run["workflow"]),
            "source_mode": str(run["source_mode"]),
            "organization_urn": str(run["organization_urn"]),
            "exact_post_urn": str(run["exact_post_urn"]),
            "requested_count": int(run["requested_count"]),
            "api_version": str(run["api_version"]),
            "comments_per_post": int(run["comments_per_post"]),
            "scope_hash": str(run["scope_hash"]),
            "inventory_frozen": bool(run["inventory_frozen"]),
            "inventory_count": len(json.loads(run["inventory_json"])),
            "inventory_hash": str(run["inventory_hash"]),
            "status": str(run["status"]),
            "status_summary": f"{run['status']}: {ready}/{int(run['requested_count'])}",
            "checkpointed": int(counts["checkpointed"]),
            "evidence_ready": ready,
            "pending": max(0, int(run["requested_count"]) - ready),
            "available_comment_rows": comments,
            "purged_post_rows": purged,
            "data_expired": purged > 0,
            "reasons": json.loads(run["incomplete_reasons_json"]),
            "publication_enabled": False,
            "external_ai_enabled": False,
            "retention": {
                "comment_hours": COMMENT_RETENTION_HOURS,
                "organization_post_days": ORGANIZATION_POST_RETENTION_DAYS,
            },
            "created_at": str(run["created_at"]),
            "updated_at": str(run["updated_at"]),
        }

    def status(self, run_id: str) -> dict[str, Any]:
        self.purge_expired()
        return self._status(self._connection, run_id)

    def purge_expired(self, *, now: dt.datetime | None = None) -> dict[str, int]:
        cutoff = iso((now or utc_now()).astimezone(dt.timezone.utc))
        with self._transaction() as connection:
            comments = connection.execute(
                "SELECT run_id, post_urn, comment_urn FROM linkedin_comments WHERE expires_at<=?",
                (cutoff,),
            ).fetchall()
            for row in comments:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO linkedin_comment_tombstones (
                        run_id, post_urn, comment_urn, purged_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (row["run_id"], row["post_urn"], row["comment_urn"], cutoff),
                )
            if comments:
                connection.execute(
                    "DELETE FROM linkedin_comments WHERE expires_at<=?",
                    (cutoff,),
                )
            expiring_posts = connection.execute(
                """
                SELECT DISTINCT organization_urn, post_urn
                FROM linkedin_posts
                WHERE expires_at<=? AND purged_at=''
                """,
                (cutoff,),
            ).fetchall()
            post_count = connection.execute(
                """
                UPDATE linkedin_posts SET canonical_url='', published_at='',
                    commentary='', content_json='{}', metrics_json='{}',
                    collection_json='{}', evidence_hash='', evidence_ready=0,
                    error_code='', observed_at='', expires_at='', purged_at=?
                WHERE expires_at<=? AND purged_at=''
                """,
                (cutoff, cutoff),
            ).rowcount
            for row in expiring_posts:
                connection.execute(
                    """
                    UPDATE linkedin_known_posts SET first_run_id='', first_seen_at=''
                    WHERE organization_urn=? AND post_urn=?
                    """,
                    (row["organization_urn"], row["post_urn"]),
                )
            result = {"comments_purged": len(comments), "posts_purged": post_count}
        if self._file_backed and any(result.values()):
            checkpoint = self._connection.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)"
            ).fetchone()
            if checkpoint is not None and int(checkpoint[0]) != 0:
                raise LinkedInStateError(
                    "expired data was removed but the SQLite WAL could not be truncated"
                )
        return result


__all__ = [
    "COMMENT_RETENTION_HOURS",
    "InvalidLinkedInStateInput",
    "LinkedInCollectionState",
    "LinkedInStateConflict",
    "LinkedInStateError",
    "LinkedInStateIntegrityError",
    "ORGANIZATION_POST_RETENTION_DAYS",
    "SCHEMA_VERSION",
    "canonical_json",
    "canonical_sha256",
]
