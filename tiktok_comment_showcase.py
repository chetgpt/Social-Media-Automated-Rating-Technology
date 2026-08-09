"""Local state machine for TikTok comment-screenshot showcase posts.

This module deliberately does not open a browser, call an API, generate a
caption, or publish anything.  It stores and verifies the artifacts produced
by those separate stages and provides fail-closed state transitions.

A showcase can be enqueued only from a locally confirmed TikTok comment:

* ``publication_queue.status`` is ``published``;
* ``publication_receipts.status`` is ``published``;
* both rows and the receipt payload carry the same non-empty master attempt;
* the receipt payload contains an exact-comment element screenshot, its
  SHA-256/size, and capture proof bound to the source comment.

The screenshot payload accepted in ``publication_receipts.response_json`` is::

    {
      "target_url": "https://www.tiktok.com/@creator/video/123",
      "submitted_text": "...",
      "final_text_hash": "<sha256>",
      "observed_account": "publisher",
      "master_attempt_id": "...",
      "exact_comment_screenshot": {
        "path": "path/to/comment.png",
        "sha256": "<sha256>",
        "size_bytes": 12345,
        "proof": {
          "schema_version": "tiktok-exact-comment-screenshot-v1",
          "capture_kind": "exact_comment_element",
          "unique_match": true,
          "source_post_id": "123",
          "canonical_url": "https://www.tiktok.com/@creator/video/123",
          "remote_comment_id": "456",
          "comment_text_hash": "<sha256>",
          "observed_account": "publisher",
          "media_sha256": "<sha256>",
          "locator_strategy": "remote_comment_id",
          "captured_at": "..."
        }
      }
    }

Relative screenshot paths are resolved against the directory containing the
local SQLite database.  The file is hashed again at every later gate.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import ipaddress
import json
import re
import secrets
import sqlite3
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence
from urllib.parse import urlsplit

from tiktok_scraper.analysis_workflow import publication_ai_review_hash


SHOWCASE_SCHEMA_VERSION = "1"
SCREENSHOT_PROOF_SCHEMA = "tiktok-exact-comment-screenshot-v1"
CAPTION_INPUT_SCHEMA = "tiktok-comment-showcase-caption-input-v1"
CAPTION_POLICY_VERSION = "tiktok-comment-showcase-caption-v1"
PRESENTATION_SCHEMA = "tiktok-comment-showcase-presentation-v3"
TIKTOK_MUSIC_USAGE_CONSENT = (
    "By posting, you agree to TikTok's Music Usage Confirmation."
)
AUTHORIZATION_SCHEMA = "tiktok-comment-showcase-authorization-v3"
PUBLIC_PRIVACY_LEVEL = "PUBLIC_TO_EVERYONE"
MAX_CAPTION_UTF16_UNITS = 4000
MAX_PUBLISH_MEDIA_BYTES = 20 * 1024 * 1024
PUBLISH_MEDIA_WIDTH = 1080
PUBLISH_MEDIA_HEIGHT = 1920

BUILTIN_AI_ACTOR_PATTERN = re.compile(
    r"^(?:codex|antigravity)(?:[-_.:].+)?$",
    re.IGNORECASE,
)
AUTOMATION_IDENTITY_PATTERN = re.compile(
    r"(?:^|[^a-z0-9])(?:ai|codex|antigravity|system|automation|bot|agent)"
    r"(?:[^a-z0-9]|$)",
    re.IGNORECASE,
)
AI_DISCLOSURE_PATTERN = re.compile(
    r"(?:\bai[- ](?:assisted|generated|created|drafted)\b|"
    r"\banalisis ai\b|\bdibantu ai\b|\bautomated analysis\b)",
    re.IGNORECASE,
)
CANONICAL_TIKTOK_PATH = re.compile(r"^/@([A-Za-z0-9._-]+)/video/([0-9]+)$")
CANONICAL_TIKTOK_OUTPUT_PATH = re.compile(
    r"^/@([A-Za-z0-9._-]+)/(?:video|photo)/([0-9]+)$"
)
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

JOB_STATUSES = frozenset(
    {
        "captured",
        "publish_media_ready",
        "drafted",
        "review_rejected",
        "reviewed",
        "presented",
        "authorized",
        "reserved",
        "submit_intent",
        "retryable",
        "uncertain",
        "published",
        "blocked",
    }
)
BLOCKING_OUTPUT_STATUSES = frozenset(
    {"reserved", "submit_intent", "uncertain", "published"}
)
REQUIRED_REVIEW_CHECKS = (
    "grounded",
    "media_verified",
    "privacy_safe",
    "caption_exact",
    "link_exact",
    "ai_disclosure",
    "safe",
)


def showcase_post_settings() -> dict[str, Any]:
    """Return the exact TikTok photo settings shown and authorized."""
    return {
        "privacy_level": PUBLIC_PRIVACY_LEVEL,
        "allow_comments": True,
        "auto_add_music": False,
        "brand_content": False,
        "brand_organic": False,
    }


class ShowcaseError(RuntimeError):
    """Base error for a showcase workflow failure."""


class StageGateError(ShowcaseError):
    """Raised when a state or hash gate is not satisfied."""


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


def text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def stable_id(*parts: Any, length: int = 32) -> str:
    joined = "\x1f".join(str(part or "").strip() for part in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:length]


def utf16_units(value: str) -> int:
    try:
        return len(value.encode("utf-16-le")) // 2
    except UnicodeEncodeError as exc:
        raise StageGateError("caption contains an invalid Unicode surrogate") from exc


def require_builtin_ai_actor(value: Any, stage: str) -> str:
    actor = str(value or "").strip()
    if not BUILTIN_AI_ACTOR_PATTERN.fullmatch(actor):
        raise StageGateError(
            f"{stage} actor must identify interactive Codex/Antigravity"
        )
    return actor


def is_automation_identity(value: Any) -> bool:
    identity = str(value or "").strip().casefold()
    return (
        not identity
        or AUTOMATION_IDENTITY_PATTERN.search(identity) is not None
    )


def require_named_human(value: Any, stage: str) -> str:
    identity = str(value or "").strip()
    if is_automation_identity(identity):
        raise StageGateError(f"{stage} requires a named non-AI human")
    return identity


def normalize_account(value: Any) -> str:
    return str(value or "").strip().lstrip("@").casefold()


def canonical_tiktok_url(value: Any) -> tuple[str, str, str]:
    raw = str(value or "").strip()
    parsed = urlsplit(raw)
    if (
        parsed.scheme != "https"
        or parsed.netloc.casefold() != "www.tiktok.com"
        or parsed.query
        or parsed.fragment
    ):
        raise StageGateError("source URL is not a canonical TikTok post URL")
    path = parsed.path.rstrip("/")
    match = CANONICAL_TIKTOK_PATH.fullmatch(path)
    if not match:
        raise StageGateError("source URL is not a canonical TikTok post URL")
    canonical = f"https://www.tiktok.com/@{match.group(1)}/video/{match.group(2)}"
    if raw != canonical:
        raise StageGateError(
            "source URL must already equal the exact canonical TikTok URL"
        )
    return canonical, match.group(1), match.group(2)


def canonical_tiktok_output_url(value: Any) -> tuple[str, str, str]:
    """Validate a confirmed showcase URL (TikTok video or photo post)."""

    raw = str(value or "").strip()
    parsed = urlsplit(raw)
    if (
        parsed.scheme != "https"
        or parsed.netloc.casefold() != "www.tiktok.com"
        or parsed.query
        or parsed.fragment
    ):
        raise StageGateError("remote URL is not a canonical TikTok post URL")
    path = parsed.path.rstrip("/")
    match = CANONICAL_TIKTOK_OUTPUT_PATH.fullmatch(path)
    if not match:
        raise StageGateError("remote URL is not a canonical TikTok post URL")
    post_kind = "photo" if "/photo/" in path else "video"
    canonical = (
        f"https://www.tiktok.com/@{match.group(1)}/"
        f"{post_kind}/{match.group(2)}"
    )
    if raw != canonical:
        raise StageGateError(
            "remote URL must already equal the exact canonical TikTok URL"
        )
    return canonical, match.group(1), match.group(2)


def _json_object(value: Any, label: str) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError as exc:
        raise StageGateError(f"{label} is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise StageGateError(f"{label} must be a JSON object")
    return parsed


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1])
        for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    }


def _require_columns(
    conn: sqlite3.Connection,
    table: str,
    required: Sequence[str],
) -> None:
    columns = _table_columns(conn, table)
    missing = sorted(set(required) - columns)
    if missing:
        raise StageGateError(
            f"{table} is missing required columns: {', '.join(missing)}"
        )


def _row_dict(row: sqlite3.Row | Sequence[Any], columns: Sequence[str]) -> dict[str, Any]:
    if isinstance(row, sqlite3.Row):
        return {key: row[key] for key in row.keys()}
    return dict(zip(columns, row))


@contextlib.contextmanager
def _transaction(
    conn: sqlite3.Connection,
    *,
    require_durable_root: bool = False,
) -> Iterator[None]:
    """Use BEGIN IMMEDIATE, or a savepoint for a caller-owned transaction."""

    if require_durable_root and conn.in_transaction:
        raise StageGateError(
            "durable transition requires a connection with no open transaction"
        )
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
        return

    savepoint = "comment_showcase_" + secrets.token_hex(8)
    conn.execute(f'SAVEPOINT "{savepoint}"')
    try:
        yield
    except Exception:
        conn.execute(f'ROLLBACK TO "{savepoint}"')
        conn.execute(f'RELEASE "{savepoint}"')
        raise
    else:
        conn.execute(f'RELEASE "{savepoint}"')


def connect_database(path: str | Path) -> sqlite3.Connection:
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    ensure_schema(conn)
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the additive local showcase tables and indexes."""

    schema_script = """
        CREATE TABLE IF NOT EXISTS tiktok_comment_showcase_jobs (
            showcase_id TEXT PRIMARY KEY,
            source_publication_id TEXT NOT NULL UNIQUE,
            source_receipt_id TEXT NOT NULL UNIQUE,
            source_master_attempt_id TEXT NOT NULL UNIQUE,
            source_run_id TEXT NOT NULL DEFAULT '',
            source_post_id TEXT NOT NULL,
            canonical_url TEXT NOT NULL,
            source_creator TEXT NOT NULL DEFAULT '',
            source_comment_id TEXT NOT NULL,
            source_comment_text TEXT NOT NULL,
            source_comment_text_hash TEXT NOT NULL,
            source_analysis_input_hash TEXT NOT NULL DEFAULT '',
            posting_account TEXT NOT NULL,
            source_binding_hash TEXT NOT NULL,
            source_context_json TEXT NOT NULL DEFAULT '{}',
            source_context_hash TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            media_path TEXT NOT NULL,
            media_sha256 TEXT NOT NULL,
            media_size_bytes INTEGER NOT NULL,
            media_mime_type TEXT NOT NULL,
            capture_proof_json TEXT NOT NULL,
            capture_proof_hash TEXT NOT NULL,
            publish_media_path TEXT NOT NULL DEFAULT '',
            publish_media_sha256 TEXT NOT NULL DEFAULT '',
            publish_media_size_bytes INTEGER NOT NULL DEFAULT 0,
            publish_media_mime_type TEXT NOT NULL DEFAULT '',
            publish_media_width INTEGER NOT NULL DEFAULT 0,
            publish_media_height INTEGER NOT NULL DEFAULT 0,
            publish_media_public_url TEXT NOT NULL DEFAULT '',
            publish_media_binding_hash TEXT NOT NULL DEFAULT '',
            publish_media_bound_at TEXT NOT NULL DEFAULT '',
            caption_input_json TEXT NOT NULL DEFAULT '{}',
            caption_input_hash TEXT NOT NULL DEFAULT '',
            caption_text TEXT NOT NULL DEFAULT '',
            caption_hash TEXT NOT NULL DEFAULT '',
            caption_policy_version TEXT NOT NULL DEFAULT '',
            drafted_by TEXT NOT NULL DEFAULT '',
            draft_context_id TEXT NOT NULL DEFAULT '',
            drafted_at TEXT NOT NULL DEFAULT '',
            link_mode TEXT NOT NULL DEFAULT '',
            link_url TEXT NOT NULL DEFAULT '',
            review_status TEXT NOT NULL DEFAULT '',
            review_json TEXT NOT NULL DEFAULT '{}',
            review_hash TEXT NOT NULL DEFAULT '',
            reviewed_by TEXT NOT NULL DEFAULT '',
            review_context_id TEXT NOT NULL DEFAULT '',
            reviewed_at TEXT NOT NULL DEFAULT '',
            presentation_json TEXT NOT NULL DEFAULT '{}',
            presentation_hash TEXT NOT NULL DEFAULT '',
            presentation_token_hash TEXT NOT NULL DEFAULT '',
            presented_to TEXT NOT NULL DEFAULT '',
            presented_at TEXT NOT NULL DEFAULT '',
            authorization_by TEXT NOT NULL DEFAULT '',
            authorized_at TEXT NOT NULL DEFAULT '',
            authorization_token_hash TEXT NOT NULL DEFAULT '',
            authorization_presentation_hash TEXT NOT NULL DEFAULT '',
            authorization_media_sha256 TEXT NOT NULL DEFAULT '',
            authorization_caption_hash TEXT NOT NULL DEFAULT '',
            authorization_review_hash TEXT NOT NULL DEFAULT '',
            authorization_source_binding_hash TEXT NOT NULL DEFAULT '',
            authorization_account TEXT NOT NULL DEFAULT '',
            authorization_link_url TEXT NOT NULL DEFAULT '',
            authorization_json TEXT NOT NULL DEFAULT '{}',
            authorization_hash TEXT NOT NULL DEFAULT '',
            output_fingerprint TEXT NOT NULL DEFAULT '',
            active_attempt_id TEXT NOT NULL DEFAULT '',
            active_master_showcase_attempt_id TEXT NOT NULL DEFAULT '',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            remote_post_id TEXT NOT NULL DEFAULT '',
            remote_post_url TEXT NOT NULL DEFAULT '',
            published_at TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_showcase_jobs_status
            ON tiktok_comment_showcase_jobs(status, created_at);
        CREATE INDEX IF NOT EXISTS idx_showcase_jobs_output
            ON tiktok_comment_showcase_jobs(
                posting_account, output_fingerprint, status
            );

        CREATE TABLE IF NOT EXISTS tiktok_comment_showcase_attempts (
            attempt_id TEXT PRIMARY KEY,
            showcase_id TEXT NOT NULL,
            attempt_number INTEGER NOT NULL,
            posting_account TEXT NOT NULL,
            media_sha256 TEXT NOT NULL,
            caption_hash TEXT NOT NULL,
            authorization_hash TEXT NOT NULL,
            output_fingerprint TEXT NOT NULL,
            master_showcase_attempt_id TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL,
            reserved_at TEXT NOT NULL,
            submit_intent_at TEXT NOT NULL DEFAULT '',
            publish_id TEXT NOT NULL DEFAULT '',
            publish_id_checkpoint_at TEXT NOT NULL DEFAULT '',
            resolved_at TEXT NOT NULL DEFAULT '',
            remote_post_id TEXT NOT NULL DEFAULT '',
            remote_post_url TEXT NOT NULL DEFAULT '',
            visible INTEGER NOT NULL DEFAULT 0,
            persisted INTEGER NOT NULL DEFAULT 0,
            response_json TEXT NOT NULL DEFAULT '{}',
            error TEXT NOT NULL DEFAULT '',
            UNIQUE(showcase_id, attempt_number)
        );
        CREATE INDEX IF NOT EXISTS idx_showcase_attempts_job
            ON tiktok_comment_showcase_attempts(showcase_id, attempt_number);
        CREATE INDEX IF NOT EXISTS idx_showcase_attempts_output
            ON tiktok_comment_showcase_attempts(
                posting_account, output_fingerprint, state
            );
    """
    # ``executescript`` implicitly commits a caller-owned transaction.  Execute
    # these simple statements individually so a state transition remains
    # atomic even when it calls a schema-aware read helper.
    for statement in schema_script.split(";"):
        if statement.strip():
            conn.execute(statement)

    # Explicit additive migration for databases created by an earlier
    # showcase implementation.  Raw ``media_*`` columns remain the immutable
    # exact-comment capture; these columns bind the API-ready JPEG instead.
    job_columns = _table_columns(conn, "tiktok_comment_showcase_jobs")
    additions = {
        "source_context_json": "TEXT NOT NULL DEFAULT '{}'",
        "source_context_hash": "TEXT NOT NULL DEFAULT ''",
        "publish_media_path": "TEXT NOT NULL DEFAULT ''",
        "publish_media_sha256": "TEXT NOT NULL DEFAULT ''",
        "publish_media_size_bytes": "INTEGER NOT NULL DEFAULT 0",
        "publish_media_mime_type": "TEXT NOT NULL DEFAULT ''",
        "publish_media_width": "INTEGER NOT NULL DEFAULT 0",
        "publish_media_height": "INTEGER NOT NULL DEFAULT 0",
        "publish_media_public_url": "TEXT NOT NULL DEFAULT ''",
        "publish_media_binding_hash": "TEXT NOT NULL DEFAULT ''",
        "publish_media_bound_at": "TEXT NOT NULL DEFAULT ''",
        "active_master_showcase_attempt_id": "TEXT NOT NULL DEFAULT ''",
    }
    for name, definition in additions.items():
        if name not in job_columns:
            conn.execute(
                f'ALTER TABLE tiktok_comment_showcase_jobs '
                f'ADD COLUMN "{name}" {definition}'
            )
    attempt_columns = _table_columns(
        conn,
        "tiktok_comment_showcase_attempts",
    )
    attempt_additions = {
        "master_showcase_attempt_id": "TEXT NOT NULL DEFAULT ''",
        "publish_id": "TEXT NOT NULL DEFAULT ''",
        "publish_id_checkpoint_at": "TEXT NOT NULL DEFAULT ''",
    }
    for name, definition in attempt_additions.items():
        if name not in attempt_columns:
            conn.execute(
                f"ALTER TABLE tiktok_comment_showcase_attempts "
                f'ADD COLUMN "{name}" {definition}'
            )


def _database_path(conn: sqlite3.Connection) -> Path:
    for _, name, path in conn.execute("PRAGMA database_list").fetchall():
        if str(name) == "main" and str(path or ""):
            return Path(str(path)).resolve()
    raise StageGateError(
        "a file-backed local database is required to resolve screenshot paths"
    )


def _media_metadata(path: Path) -> tuple[str, int, str]:
    if not path.is_file():
        raise StageGateError(f"comment screenshot is missing: {path}")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        first = handle.read(16)
        digest.update(first)
        size += len(first)
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    if first.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif first.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    else:
        raise StageGateError("comment screenshot must be a PNG or JPEG file")
    if size <= 0:
        raise StageGateError("comment screenshot is empty")
    return digest.hexdigest(), size, mime


def _jpeg_dimensions(path: Path) -> tuple[int, int]:
    """Read JPEG SOF dimensions without an imaging dependency."""

    data = path.read_bytes()
    if len(data) < 4 or not data.startswith(b"\xff\xd8"):
        raise StageGateError("publish media is not a valid JPEG")
    index = 2
    sof_markers = {
        0xC0,
        0xC1,
        0xC2,
        0xC3,
        0xC5,
        0xC6,
        0xC7,
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,
    }
    while index < len(data):
        while index < len(data) and data[index] != 0xFF:
            index += 1
        while index < len(data) and data[index] == 0xFF:
            index += 1
        if index >= len(data):
            break
        marker = data[index]
        index += 1
        if marker in {0xD8, 0xD9}:
            continue
        if marker == 0xDA:
            break
        if index + 2 > len(data):
            break
        segment_length = int.from_bytes(data[index : index + 2], "big")
        if segment_length < 2 or index + segment_length > len(data):
            raise StageGateError("publish JPEG has an invalid segment")
        if marker in sof_markers:
            if segment_length < 7:
                raise StageGateError("publish JPEG has an invalid frame header")
            height = int.from_bytes(data[index + 3 : index + 5], "big")
            width = int.from_bytes(data[index + 5 : index + 7], "big")
            if width <= 0 or height <= 0:
                raise StageGateError("publish JPEG dimensions are invalid")
            return width, height
        index += segment_length
    raise StageGateError("publish JPEG has no readable dimensions")


def _resolve_media_path(conn: sqlite3.Connection, raw_path: Any) -> Path:
    value = str(raw_path or "").strip()
    if not value:
        raise StageGateError("exact-comment screenshot path is required")
    path = Path(value)
    if not path.is_absolute():
        path = _database_path(conn).parent / path
    return path.resolve()


def verify_media(row: dict[str, Any] | sqlite3.Row) -> dict[str, Any]:
    item = (
        {key: row[key] for key in row.keys()}
        if isinstance(row, sqlite3.Row)
        else dict(row)
    )
    path = Path(str(item.get("media_path") or "")).resolve()
    actual_hash, actual_size, actual_mime = _media_metadata(path)
    if actual_hash != str(item.get("media_sha256") or "").casefold():
        raise StageGateError("comment screenshot hash changed")
    if actual_size != int(item.get("media_size_bytes") or 0):
        raise StageGateError("comment screenshot size changed")
    if actual_mime != str(item.get("media_mime_type") or ""):
        raise StageGateError("comment screenshot MIME type changed")
    return {
        "path": str(path),
        "sha256": actual_hash,
        "size_bytes": actual_size,
        "mime_type": actual_mime,
    }


def _stable_public_https_url(value: Any) -> str:
    raw = str(value or "").strip()
    parsed = urlsplit(raw)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not parsed.path
    ):
        raise StageGateError(
            "publish media requires a stable public HTTPS URL without "
            "credentials, query, or fragment"
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise StageGateError("publish media public URL has an invalid port") from exc
    if port not in (None, 443):
        raise StageGateError("publish media public URL must use standard HTTPS")
    hostname = str(parsed.hostname).casefold()
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise StageGateError(
            "publish media public URL must use a verified domain, not an IP"
        )
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise StageGateError(
            "publish media public URL must use a public verified domain"
        )
    return raw


def verify_publish_media(
    row: dict[str, Any] | sqlite3.Row,
) -> dict[str, Any]:
    item = (
        {key: row[key] for key in row.keys()}
        if isinstance(row, sqlite3.Row)
        else dict(row)
    )
    path = Path(str(item.get("publish_media_path") or "")).resolve()
    actual_hash, actual_size, actual_mime = _media_metadata(path)
    if actual_mime != "image/jpeg":
        raise StageGateError("publish media must be an API-ready JPEG")
    if actual_size > MAX_PUBLISH_MEDIA_BYTES:
        raise StageGateError("publish JPEG exceeds the 20 MB safety limit")
    actual_width, actual_height = _jpeg_dimensions(path)
    if (
        actual_width != PUBLISH_MEDIA_WIDTH
        or actual_height != PUBLISH_MEDIA_HEIGHT
    ):
        raise StageGateError(
            "publish JPEG must be normalized to exactly 1080x1920"
        )
    if actual_hash != str(item.get("publish_media_sha256") or "").casefold():
        raise StageGateError("publish JPEG hash changed")
    if actual_size != int(item.get("publish_media_size_bytes") or 0):
        raise StageGateError("publish JPEG size changed")
    if str(item.get("publish_media_mime_type") or "") != "image/jpeg":
        raise StageGateError("publish JPEG MIME binding changed")
    if (
        int(item.get("publish_media_width") or 0) != actual_width
        or int(item.get("publish_media_height") or 0) != actual_height
    ):
        raise StageGateError("publish JPEG dimension binding changed")
    public_url = _stable_public_https_url(
        item.get("publish_media_public_url")
    )
    binding = {
        "schema_version": "tiktok-comment-showcase-publish-media-v1",
        "showcase_id": item.get("showcase_id"),
        "source_binding_hash": item.get("source_binding_hash"),
        "source_context_hash": item.get("source_context_hash"),
        "raw_capture_sha256": item.get("media_sha256"),
        "raw_capture_proof_hash": item.get("capture_proof_hash"),
        "publish_media_sha256": actual_hash,
        "publish_media_size_bytes": actual_size,
        "publish_media_mime_type": actual_mime,
        "publish_media_width": actual_width,
        "publish_media_height": actual_height,
        "publish_media_public_url": public_url,
    }
    if json_hash(binding) != str(
        item.get("publish_media_binding_hash") or ""
    ):
        raise StageGateError("publish JPEG binding changed")
    return {
        "path": str(path),
        "sha256": actual_hash,
        "size_bytes": actual_size,
        "mime_type": actual_mime,
        "width": actual_width,
        "height": actual_height,
        "public_url": public_url,
        "binding_hash": json_hash(binding),
    }


def bind_publish_media(
    conn: sqlite3.Connection,
    *,
    showcase_id: str,
    media_path: str | Path,
    media_sha256: str,
    media_size_bytes: int,
    media_mime_type: str,
    public_media_url: str,
) -> dict[str, Any]:
    """Bind the API-ready JPEG and stable public URL before AI drafting."""

    ensure_schema(conn)
    with _transaction(conn):
        row = get_showcase(conn, showcase_id)
        if row["status"] not in {"captured", "publish_media_ready"}:
            raise StageGateError(
                "publish media can only be bound before caption drafting"
            )
        verify_media(row)
        verify_source_context(conn, row)
        path = _resolve_media_path(conn, media_path)
        actual_hash, actual_size, actual_mime = _media_metadata(path)
        if actual_size > MAX_PUBLISH_MEDIA_BYTES:
            raise StageGateError("publish JPEG exceeds the 20 MB safety limit")
        actual_width, actual_height = _jpeg_dimensions(path)
        declared_hash = str(media_sha256 or "").strip().casefold()
        try:
            declared_size = int(media_size_bytes)
        except (TypeError, ValueError) as exc:
            raise StageGateError("publish JPEG declared size is invalid") from exc
        if (
            actual_mime != "image/jpeg"
            or str(media_mime_type or "").strip().casefold() != "image/jpeg"
        ):
            raise StageGateError("publish media must be an API-ready JPEG")
        if (
            actual_width != PUBLISH_MEDIA_WIDTH
            or actual_height != PUBLISH_MEDIA_HEIGHT
        ):
            raise StageGateError(
                "publish JPEG must be normalized to exactly 1080x1920"
            )
        if (
            not SHA256_PATTERN.fullmatch(declared_hash)
            or declared_hash != actual_hash
            or declared_size != actual_size
        ):
            raise StageGateError("publish JPEG hash or size does not match")
        public_url = _stable_public_https_url(public_media_url)
        binding = {
            "schema_version": "tiktok-comment-showcase-publish-media-v1",
            "showcase_id": row["showcase_id"],
            "source_binding_hash": row["source_binding_hash"],
            "source_context_hash": row["source_context_hash"],
            "raw_capture_sha256": row["media_sha256"],
            "raw_capture_proof_hash": row["capture_proof_hash"],
            "publish_media_sha256": actual_hash,
            "publish_media_size_bytes": actual_size,
            "publish_media_mime_type": actual_mime,
            "publish_media_width": actual_width,
            "publish_media_height": actual_height,
            "publish_media_public_url": public_url,
        }
        binding_hash = json_hash(binding)
        if row["status"] == "publish_media_ready":
            expected = (
                str(path),
                actual_hash,
                actual_size,
                actual_mime,
                actual_width,
                actual_height,
                public_url,
                binding_hash,
            )
            stored = (
                row["publish_media_path"],
                row["publish_media_sha256"],
                int(row["publish_media_size_bytes"]),
                row["publish_media_mime_type"],
                int(row["publish_media_width"]),
                int(row["publish_media_height"]),
                row["publish_media_public_url"],
                row["publish_media_binding_hash"],
            )
            if stored != expected:
                raise StageGateError(
                    "publish media is already bound to a different artifact"
                )
            verify_publish_media(row)
            return row
        timestamp = now_iso()
        changed = conn.execute(
            """
            UPDATE tiktok_comment_showcase_jobs SET
                status='publish_media_ready',
                publish_media_path=?, publish_media_sha256=?,
                publish_media_size_bytes=?, publish_media_mime_type=?,
                publish_media_width=?, publish_media_height=?,
                publish_media_public_url=?, publish_media_binding_hash=?,
                publish_media_bound_at=?, error='', updated_at=?
            WHERE showcase_id=? AND status='captured'
            """,
            (
                str(path),
                actual_hash,
                actual_size,
                actual_mime,
                actual_width,
                actual_height,
                public_url,
                binding_hash,
                timestamp,
                timestamp,
                str(showcase_id),
            ),
        ).rowcount
        if changed != 1:
            raise StageGateError("showcase changed before publish media binding")
        result = get_showcase(conn, showcase_id)
        verify_publish_media(result)
        return result


def _proof_value(proof: dict[str, Any], *names: str) -> str:
    for name in names:
        value = str(proof.get(name) or "").strip()
        if value:
            return value
    return ""


def _load_confirmed_source(
    conn: sqlite3.Connection,
    publication_id: str,
    receipt_id: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    queue_required = (
        "publication_id",
        "status",
        "target_url",
        "content_key",
        "draft_text",
        "draft_hash",
        "master_attempt_id",
        "expected_account",
    )
    receipt_required = (
        "receipt_id",
        "publication_id",
        "status",
        "master_attempt_id",
        "remote_comment_id",
        "response_json",
    )
    _require_columns(conn, "publication_queue", queue_required)
    _require_columns(conn, "publication_receipts", receipt_required)
    queue_columns = _table_columns(conn, "publication_queue")
    optional_queue = [
        name
        for name in ("analysis_input_hash", "engage_run_id", "engage_post_id")
        if name in queue_columns
    ]
    queue_names = [*queue_required, *optional_queue]
    queue_row = conn.execute(
        f"""
        SELECT {", ".join(f'"{name}"' for name in queue_names)}
        FROM publication_queue
        WHERE publication_id=?
        """,
        (str(publication_id),),
    ).fetchone()
    if not queue_row:
        raise StageGateError("source comment publication was not found")
    queue = _row_dict(queue_row, queue_names)
    receipt_names = list(receipt_required)
    receipt_row = conn.execute(
        f"""
        SELECT {", ".join(f'"{name}"' for name in receipt_names)}
        FROM publication_receipts
        WHERE receipt_id=? AND publication_id=?
        """,
        (str(receipt_id), str(publication_id)),
    ).fetchone()
    if not receipt_row:
        raise StageGateError("matching source comment receipt was not found")
    receipt = _row_dict(receipt_row, receipt_names)
    if str(queue["status"] or "").casefold() != "published":
        raise StageGateError("source comment queue is not confirmed published")
    if str(receipt["status"] or "").casefold() != "published":
        raise StageGateError("source comment receipt is not confirmed published")
    master_attempt_id = str(queue["master_attempt_id"] or "").strip()
    if (
        not master_attempt_id
        or master_attempt_id != str(receipt["master_attempt_id"] or "").strip()
    ):
        raise StageGateError(
            "source queue and receipt master attempt IDs do not match"
        )
    response = _json_object(receipt["response_json"], "source receipt response")
    if str(response.get("master_attempt_id") or "").strip() != master_attempt_id:
        raise StageGateError(
            "source receipt payload master attempt ID does not match"
        )
    return queue, receipt, response


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type='table' AND name=?
            """,
            (str(table),),
        ).fetchone()
        is not None
    )


def _evidence_transcript(evidence: dict[str, Any]) -> dict[str, Any]:
    value = (
        evidence.get("transcript")
        or evidence.get("transcript_text")
        or evidence.get("speech_to_text")
        or ""
    )
    if isinstance(value, dict):
        return {
            "status": str(
                value.get("status")
                or evidence.get("transcript_status")
                or "available"
            ),
            "text": value.get("text") or value.get("transcript") or "",
            "segments": value.get("segments") or [],
        }
    if isinstance(value, list):
        return {
            "status": str(evidence.get("transcript_status") or "available"),
            "text": "",
            "segments": value,
        }
    return {
        "status": str(
            evidence.get("transcript_status")
            or ("available" if str(value or "").strip() else "unavailable")
        ),
        "text": str(value or ""),
        "segments": evidence.get("transcript_segments") or [],
    }


def _build_source_context(
    conn: sqlite3.Connection,
    queue: dict[str, Any],
    source: dict[str, Any],
) -> dict[str, Any]:
    run_id = str(queue.get("engage_run_id") or "").strip()
    engage_post_id = str(queue.get("engage_post_id") or "").strip()
    base = {
        "schema_version": "tiktok-comment-showcase-source-context-v1",
        "source_publication_id": str(queue.get("publication_id") or ""),
        "source_run_id": run_id,
        "source_post_id": source["post_id"],
        "canonical_url": source["canonical_url"],
        "reviewed_comment": {
            "text": source["comment_text"],
            "text_hash": source["comment_text_hash"],
        },
    }
    if not run_id or not engage_post_id:
        raise StageGateError(
            "stored ENGAGE evidence identifiers are required for a showcase"
        )
    if not _table_exists(conn, "engage_tiktok_posts"):
        raise StageGateError(
            "stored ENGAGE evidence table is required for a showcase"
        )

    required = {
        "run_id",
        "post_id",
        "publication_id",
        "url",
        "status",
        "evidence_ready",
        "evidence_json",
        "evidence_hash",
        "analysis_json",
        "analysis_hash",
        "analysis_actor",
        "analyzed_at",
        "review_json",
        "review_hash",
        "review_actor",
        "reviewed_at",
        "draft_text",
        "draft_hash",
        "draft_actor",
        "drafted_at",
    }
    columns = _table_columns(conn, "engage_tiktok_posts")
    missing = sorted(required - columns)
    if missing:
        raise StageGateError(
            "ENGAGE source context table is incomplete: " + ", ".join(missing)
        )
    names = sorted(required)
    record = conn.execute(
        f"""
        SELECT {", ".join(f'"{name}"' for name in names)}
        FROM engage_tiktok_posts
        WHERE run_id=? AND post_id=?
        """,
        (run_id, engage_post_id),
    ).fetchone()
    if not record:
        raise StageGateError("bound ENGAGE source context row is missing")
    item = _row_dict(record, names)
    if (
        str(item["publication_id"] or "") != str(queue["publication_id"])
        or str(item["post_id"] or "") != source["post_id"]
        or str(item["url"] or "") != source["canonical_url"]
        or str(item["draft_text"] or "") != source["comment_text"]
        or str(item["draft_hash"] or "") != source["comment_text_hash"]
    ):
        raise StageGateError("ENGAGE source context is bound to different content")
    if str(item["status"] or "").casefold() != "published":
        raise StageGateError(
            "ENGAGE source context is not a confirmed published post"
        )
    if int(item["evidence_ready"] or 0) != 1:
        raise StageGateError("ENGAGE source evidence is not evidence-ready")
    evidence = _json_object(item["evidence_json"], "ENGAGE source evidence")
    analysis = _json_object(item["analysis_json"], "ENGAGE source analysis")
    review = _json_object(item["review_json"], "ENGAGE source review")
    evidence_hash = str(item["evidence_hash"] or "")
    analysis_hash = str(item["analysis_hash"] or "")
    review_hash = str(item["review_hash"] or "")
    if not evidence_hash or json_hash(evidence) != evidence_hash:
        raise StageGateError("ENGAGE source evidence hash does not verify")
    if (
        str(queue.get("analysis_input_hash") or "") != evidence_hash
        or evidence.get("evidence_ready") is not True
        or evidence.get("readiness_issues") not in (None, [])
        or str(evidence.get("platform") or "").casefold() != "tiktok"
        or str(evidence.get("post_id") or "") != source["post_id"]
        or str(evidence.get("url") or "") != source["canonical_url"]
        or not str(evidence.get("observed_at") or "").strip()
        or not isinstance(evidence.get("metrics"), dict)
        or not isinstance(evidence.get("comments"), list)
    ):
        raise StageGateError(
            "ENGAGE source evidence is incomplete or bound to different content"
        )
    expected_analysis_hash = json_hash(
        {"evidence_hash": evidence_hash, "analysis": analysis}
    )
    if not analysis_hash or analysis_hash != expected_analysis_hash:
        raise StageGateError("ENGAGE source analysis hash does not verify")
    response_type = str(
        analysis.get("response_type")
        or analysis.get("classification")
        or analysis.get("type")
        or ""
    )
    if (
        response_type
        not in {
            "positive_support",
            "constructive_suggestion",
            "constructive_correction",
            "clarifying_question",
        }
        or analysis.get("comment_eligible") is not True
        or str(analysis.get("ai_execution_mode") or "")
        != "interactive_builtin_no_external_llm_api"
        or BUILTIN_AI_ACTOR_PATTERN.fullmatch(
            str(item["analysis_actor"] or "").strip()
        )
        is None
        or not str(item["analyzed_at"] or "").strip()
    ):
        raise StageGateError("ENGAGE source analysis is not a valid AI result")

    expected_account = str(queue.get("expected_account") or "").strip()
    draft_actor = str(item["draft_actor"] or "").strip()
    reviewer = str(item["review_actor"] or "").strip()
    reviewed_at = str(item["reviewed_at"] or "").strip()
    analysis_id = str(review.get("analysis_id") or "").strip()
    decision_hash = str(review.get("decision_hash") or "").strip().casefold()
    analysis_result_hash = str(
        review.get("analysis_result_hash") or ""
    ).strip().casefold()
    if (
        review.get("approved") is not True
        or review.get("independent_review") is not True
        or str(review.get("response_type") or "") != response_type
        or str(review.get("analysis_input_hash") or "") != evidence_hash
        or str(review.get("reviewed_text_hash") or "")
        != source["comment_text_hash"]
        or str(review.get("target_url") or "") != source["canonical_url"]
        or str(review.get("content_key") or "") != source["post_id"]
        or str(review.get("expected_account") or "") != expected_account
        or not analysis_id
        or not SHA256_PATTERN.fullmatch(decision_hash)
        or not SHA256_PATTERN.fullmatch(analysis_result_hash)
        or BUILTIN_AI_ACTOR_PATTERN.fullmatch(draft_actor) is None
        or BUILTIN_AI_ACTOR_PATTERN.fullmatch(reviewer) is None
        or draft_actor.casefold() == reviewer.casefold()
        or not str(item["drafted_at"] or "").strip()
        or not reviewed_at
    ):
        raise StageGateError("ENGAGE source AI review is invalid or unbound")
    expected_review_hash = publication_ai_review_hash(
        publication_id=str(item["publication_id"] or ""),
        analysis_id=analysis_id,
        analysis_input_hash=evidence_hash,
        draft_hash=source["comment_text_hash"],
        reviewer=reviewer,
        reviewed_at=reviewed_at,
        review_payload=review,
        target_url=source["canonical_url"],
        content_key=source["post_id"],
        expected_account=expected_account,
        decision_hash=decision_hash,
        analysis_result_hash=analysis_result_hash,
    )
    if not review_hash or review_hash != expected_review_hash:
        raise StageGateError("ENGAGE source AI review hash does not verify")
    rationale = (
        analysis.get("response_rationale")
        or analysis.get("rationale")
        or analysis.get("reasoning")
        or review.get("rationale")
        or review.get("reason")
        or ""
    )
    summary = (
        analysis.get("summary")
        or analysis.get("post_summary")
        or evidence.get("summary")
        or ""
    )
    caption = str(
        evidence.get("caption")
        or evidence.get("description")
        or evidence.get("desc")
        or ""
    )
    return {
        **base,
        "availability": "available",
        "reason": "",
        "evidence_hash": evidence_hash,
        "analysis_hash": analysis_hash,
        "post_caption": caption,
        "transcript": _evidence_transcript(evidence),
        "analysis": {
            "response_type": response_type,
            "rationale": rationale,
            "summary": summary,
        },
        "review_hash": review_hash,
    }


def verify_source_context(
    conn: sqlite3.Connection,
    row: dict[str, Any] | sqlite3.Row,
) -> dict[str, Any]:
    item = (
        {key: row[key] for key in row.keys()}
        if isinstance(row, sqlite3.Row)
        else dict(row)
    )
    stored = _json_object(
        item.get("source_context_json"),
        "stored showcase source context",
    )
    if json_hash(stored) != str(item.get("source_context_hash") or ""):
        raise StageGateError("stored showcase source context hash changed")
    queue = {
        "publication_id": item["source_publication_id"],
        "engage_run_id": item["source_run_id"],
        "engage_post_id": item["source_post_id"],
        "analysis_input_hash": item["source_analysis_input_hash"],
        "expected_account": item["posting_account"],
    }
    source = {
        "post_id": item["source_post_id"],
        "canonical_url": item["canonical_url"],
        "comment_text": item["source_comment_text"],
        "comment_text_hash": item["source_comment_text_hash"],
    }
    current = _build_source_context(conn, queue, source)
    if current != stored:
        raise StageGateError("stored ENGAGE source context drifted")
    return stored


def _validated_screenshot(
    conn: sqlite3.Connection,
    *,
    queue: dict[str, Any],
    receipt: dict[str, Any],
    response: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    screenshot = response.get("exact_comment_screenshot")
    alternate = response.get("comment_screenshot")
    if screenshot is None:
        screenshot = alternate
    elif alternate is not None and alternate != screenshot:
        raise StageGateError("receipt has conflicting comment screenshot records")
    if not isinstance(screenshot, dict):
        raise StageGateError(
            "confirmed receipt lacks an exact-comment screenshot record"
        )
    proof = screenshot.get("proof")
    if not isinstance(proof, dict):
        raise StageGateError("exact-comment screenshot proof is required")
    if str(proof.get("schema_version") or "") != SCREENSHOT_PROOF_SCHEMA:
        raise StageGateError("exact-comment screenshot proof version is invalid")
    if str(proof.get("capture_kind") or "") != "exact_comment_element":
        raise StageGateError("screenshot is not an exact comment-element capture")
    if proof.get("unique_match") is not True:
        raise StageGateError("screenshot comment match was not uniquely verified")

    canonical_url, _, url_post_id = canonical_tiktok_url(queue["target_url"])
    post_id = str(queue["content_key"] or "").strip()
    if not post_id or post_id != url_post_id:
        raise StageGateError("source post ID does not match its canonical URL")
    if str(response.get("target_url") or "") != canonical_url:
        raise StageGateError("source receipt target URL does not match")
    proof_post_id = _proof_value(proof, "source_post_id", "post_id")
    proof_url = _proof_value(
        proof,
        "canonical_url",
        "source_url",
        "target_url",
    )
    if proof_post_id != post_id or proof_url != canonical_url:
        raise StageGateError("screenshot proof is bound to a different TikTok post")

    comment_text = str(response.get("submitted_text") or "")
    final_text_hash = str(response.get("final_text_hash") or "").casefold()
    queue_text = str(queue["draft_text"] or "")
    queue_text_hash = str(queue["draft_hash"] or "").casefold()
    if (
        not comment_text
        or comment_text != queue_text
        or text_hash(comment_text) != final_text_hash
        or final_text_hash != queue_text_hash
    ):
        raise StageGateError("source receipt is not bound to the exact comment text")
    proof_text_hash = _proof_value(
        proof,
        "comment_text_hash",
        "text_hash",
    ).casefold()
    if proof_text_hash != final_text_hash:
        raise StageGateError("screenshot proof comment-text hash does not match")

    account = normalize_account(response.get("observed_account"))
    expected_account = normalize_account(queue.get("expected_account"))
    proof_account = normalize_account(
        proof.get("observed_account") or proof.get("account")
    )
    if not account or proof_account != account:
        raise StageGateError("screenshot proof has no matching observed account")
    if expected_account and account != expected_account:
        raise StageGateError("source receipt was produced by the wrong account")

    remote_comment_id = str(
        receipt.get("remote_comment_id")
        or response.get("remote_comment_id")
        or ""
    ).strip()
    proof_comment_id = _proof_value(
        proof,
        "remote_comment_id",
        "comment_id",
    )
    if not proof_comment_id:
        raise StageGateError("screenshot proof has no exact remote comment ID")
    if remote_comment_id and proof_comment_id != remote_comment_id:
        raise StageGateError("screenshot proof remote comment ID does not match")
    remote_comment_id = remote_comment_id or proof_comment_id
    if not _proof_value(proof, "locator_strategy"):
        raise StageGateError("screenshot proof has no locator strategy")
    if not _proof_value(proof, "captured_at"):
        raise StageGateError("screenshot proof has no capture timestamp")

    media_path = _resolve_media_path(conn, screenshot.get("path"))
    actual_hash, actual_size, actual_mime = _media_metadata(media_path)
    declared_hash = str(
        screenshot.get("sha256") or screenshot.get("media_sha256") or ""
    ).casefold()
    try:
        declared_size = int(screenshot.get("size_bytes"))
    except (TypeError, ValueError) as exc:
        raise StageGateError("screenshot declared size is invalid") from exc
    proof_media_hash = _proof_value(
        proof,
        "media_sha256",
        "screenshot_sha256",
    ).casefold()
    if (
        not SHA256_PATTERN.fullmatch(declared_hash)
        or actual_hash != declared_hash
        or proof_media_hash != actual_hash
    ):
        raise StageGateError("screenshot file hash or proof hash does not match")
    if actual_size != declared_size:
        raise StageGateError("screenshot file size does not match")

    return (
        {
            "path": str(media_path),
            "sha256": actual_hash,
            "size_bytes": actual_size,
            "mime_type": actual_mime,
            "proof": proof,
            "proof_hash": json_hash(proof),
        },
        {
            "canonical_url": canonical_url,
            "post_id": post_id,
            "comment_id": remote_comment_id,
            "comment_text": comment_text,
            "comment_text_hash": final_text_hash,
            "posting_account": account,
        },
    )


def enqueue_confirmed_comment(
    conn: sqlite3.Connection,
    *,
    publication_id: str,
    receipt_id: str,
) -> dict[str, Any]:
    """Idempotently enqueue a confirmed comment and its exact screenshot."""

    ensure_schema(conn)
    with _transaction(conn):
        queue, receipt, response = _load_confirmed_source(
            conn,
            publication_id,
            receipt_id,
        )
        media, source = _validated_screenshot(
            conn,
            queue=queue,
            receipt=receipt,
            response=response,
        )
        master_attempt_id = str(queue["master_attempt_id"]).strip()
        showcase_id = stable_id(
            "tiktok-comment-showcase-v1",
            master_attempt_id,
        )
        _, creator, _ = canonical_tiktok_url(source["canonical_url"])
        source_run_id = str(queue.get("engage_run_id") or "")
        analysis_input_hash = str(queue.get("analysis_input_hash") or "")
        source_context = _build_source_context(conn, queue, source)
        source_context_hash = json_hash(source_context)
        source_binding = {
            "schema_version": "tiktok-comment-showcase-source-v1",
            "source_publication_id": str(publication_id),
            "source_receipt_id": str(receipt_id),
            "source_master_attempt_id": master_attempt_id,
            "source_run_id": source_run_id,
            "source_post_id": source["post_id"],
            "canonical_url": source["canonical_url"],
            "source_creator": creator,
            "source_comment_id": source["comment_id"],
            "source_comment_text_hash": source["comment_text_hash"],
            "source_analysis_input_hash": analysis_input_hash,
            "posting_account": source["posting_account"],
            "media_sha256": media["sha256"],
            "media_size_bytes": media["size_bytes"],
            "capture_proof_hash": media["proof_hash"],
            "source_context_hash": source_context_hash,
        }
        binding_hash = json_hash(source_binding)
        existing = conn.execute(
            """
            SELECT * FROM tiktok_comment_showcase_jobs
            WHERE showcase_id=? OR source_master_attempt_id=?
               OR source_publication_id=? OR source_receipt_id=?
            """,
            (
                showcase_id,
                master_attempt_id,
                str(publication_id),
                str(receipt_id),
            ),
        ).fetchone()
        if existing:
            item = {key: existing[key] for key in existing.keys()}
            expected = {
                "showcase_id": showcase_id,
                "source_publication_id": str(publication_id),
                "source_receipt_id": str(receipt_id),
                "source_master_attempt_id": master_attempt_id,
                "source_post_id": source["post_id"],
                "canonical_url": source["canonical_url"],
                "source_comment_id": source["comment_id"],
                "source_comment_text_hash": source["comment_text_hash"],
                "posting_account": source["posting_account"],
                "source_binding_hash": binding_hash,
                "source_context_hash": source_context_hash,
                "media_sha256": media["sha256"],
                "media_size_bytes": media["size_bytes"],
                "capture_proof_hash": media["proof_hash"],
            }
            if any(item.get(key) != value for key, value in expected.items()):
                raise StageGateError(
                    "confirmed source collides with a different showcase binding"
                )
            verify_media(item)
            return {"created": False, **item}

        timestamp = now_iso()
        conn.execute(
            """
            INSERT INTO tiktok_comment_showcase_jobs (
                showcase_id, source_publication_id, source_receipt_id,
                source_master_attempt_id, source_run_id, source_post_id,
                canonical_url, source_creator, source_comment_id,
                source_comment_text, source_comment_text_hash,
                source_analysis_input_hash, posting_account,
                source_binding_hash, source_context_json,
                source_context_hash, status, media_path, media_sha256,
                media_size_bytes, media_mime_type, capture_proof_json,
                capture_proof_hash, created_at, updated_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                'captured', ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                showcase_id,
                str(publication_id),
                str(receipt_id),
                master_attempt_id,
                source_run_id,
                source["post_id"],
                source["canonical_url"],
                creator,
                source["comment_id"],
                source["comment_text"],
                source["comment_text_hash"],
                analysis_input_hash,
                source["posting_account"],
                binding_hash,
                canonical_json(source_context),
                source_context_hash,
                media["path"],
                media["sha256"],
                media["size_bytes"],
                media["mime_type"],
                canonical_json(media["proof"]),
                media["proof_hash"],
                timestamp,
                timestamp,
            ),
        )
        item = get_showcase(conn, showcase_id)
        return {"created": True, **item}


def stage_comment_screenshot(
    conn: sqlite3.Connection,
    *,
    publication_id: str,
    receipt_id: str,
) -> dict[str, Any]:
    """Canonical prepare entry point for the immutable raw comment capture."""

    return enqueue_confirmed_comment(
        conn,
        publication_id=publication_id,
        receipt_id=receipt_id,
    )


def get_showcase(conn: sqlite3.Connection, showcase_id: str) -> dict[str, Any]:
    ensure_schema(conn)
    row = conn.execute(
        "SELECT * FROM tiktok_comment_showcase_jobs WHERE showcase_id=?",
        (str(showcase_id),),
    ).fetchone()
    if not row:
        raise StageGateError(f"unknown comment showcase: {showcase_id}")
    if isinstance(row, sqlite3.Row):
        return {key: row[key] for key in row.keys()}
    columns = [
        str(item[1])
        for item in conn.execute(
            "PRAGMA table_info(tiktok_comment_showcase_jobs)"
        ).fetchall()
    ]
    return _row_dict(row, columns)


def list_showcases(
    conn: sqlite3.Connection,
    *,
    status: str = "",
) -> list[dict[str, Any]]:
    ensure_schema(conn)
    if status:
        rows = conn.execute(
            """
            SELECT * FROM tiktok_comment_showcase_jobs
            WHERE status=? ORDER BY created_at, showcase_id
            """,
            (str(status),),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT * FROM tiktok_comment_showcase_jobs
            ORDER BY created_at, showcase_id
            """
        ).fetchall()
    columns = [
        str(item[1])
        for item in conn.execute(
            "PRAGMA table_info(tiktok_comment_showcase_jobs)"
        ).fetchall()
    ]
    return [_row_dict(row, columns) for row in rows]


def _caption_input(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": CAPTION_INPUT_SCHEMA,
        "showcase_id": row["showcase_id"],
        "source_publication_id": row["source_publication_id"],
        "source_receipt_id": row["source_receipt_id"],
        "source_master_attempt_id": row["source_master_attempt_id"],
        "source_post_id": row["source_post_id"],
        "canonical_url": row["canonical_url"],
        "source_creator": row["source_creator"],
        "source_comment_id": row["source_comment_id"],
        "source_comment_text": row["source_comment_text"],
        "source_comment_text_hash": row["source_comment_text_hash"],
        "source_analysis_input_hash": row["source_analysis_input_hash"],
        "source_binding_hash": row["source_binding_hash"],
        "source_context": _json_object(
            row["source_context_json"],
            "stored showcase source context",
        ),
        "source_context_hash": row["source_context_hash"],
        "posting_account": row["posting_account"],
        "raw_capture_sha256": row["media_sha256"],
        "raw_capture_size_bytes": int(row["media_size_bytes"]),
        "raw_capture_mime_type": row["media_mime_type"],
        "capture_proof_hash": row["capture_proof_hash"],
        "publish_media_sha256": row["publish_media_sha256"],
        "publish_media_size_bytes": int(row["publish_media_size_bytes"]),
        "publish_media_mime_type": row["publish_media_mime_type"],
        "publish_media_public_url": row["publish_media_public_url"],
        "publish_media_binding_hash": row["publish_media_binding_hash"],
        "caption_policy_version": CAPTION_POLICY_VERSION,
        "requirements": {
            "canonical_url_exactly_once": row["canonical_url"],
            "creator_fallback_required": f"@{row['source_creator']}",
            "link_mode": "caption_text_unverified_clickability",
            "clickability_must_not_be_claimed": True,
            "ai_disclosure_required": True,
            "maximum_utf16_units": MAX_CAPTION_UTF16_UNITS,
            "unsupported_claims_prohibited": True,
            "creator_endorsement_must_not_be_implied": True,
        },
    }


def caption_input(
    conn: sqlite3.Connection,
    showcase_id: str,
) -> dict[str, Any]:
    row = get_showcase(conn, showcase_id)
    verify_source_context(conn, row)
    verify_media(row)
    verify_publish_media(row)
    payload = _caption_input(row)
    return {**payload, "caption_input_hash": json_hash(payload)}


def _validate_caption(
    caption: Any,
    canonical_url: str,
    source_creator: str,
) -> str:
    value = str(caption or "")
    if not value or value != value.strip():
        raise StageGateError(
            "caption must be non-empty and cannot rely on trimmed whitespace"
        )
    if "\x00" in value:
        raise StageGateError("caption contains a NUL character")
    units = utf16_units(value)
    if units > MAX_CAPTION_UTF16_UNITS:
        raise StageGateError(
            f"caption exceeds {MAX_CAPTION_UTF16_UNITS} UTF-16 units"
        )
    if value.count(canonical_url) != 1:
        raise StageGateError(
            "caption must contain the exact canonical TikTok URL exactly once"
        )
    creator_reference = f"@{str(source_creator or '').strip()}"
    if creator_reference == "@" or creator_reference.casefold() not in value.casefold():
        raise StageGateError(
            "caption must include the source creator handle as a fallback"
        )
    if AI_DISCLOSURE_PATTERN.search(value) is None:
        raise StageGateError("caption lacks an explicit AI disclosure")
    return value


def store_caption_draft(
    conn: sqlite3.Connection,
    *,
    showcase_id: str,
    caption: str,
    drafted_by: str,
    draft_context_id: str,
) -> dict[str, Any]:
    """Store an interactive built-in-AI caption bound to source and media."""

    actor = require_builtin_ai_actor(drafted_by, "caption drafting")
    context_id = str(draft_context_id or "").strip()
    if not context_id:
        raise StageGateError("caption drafting requires a distinct context ID")
    ensure_schema(conn)
    with _transaction(conn):
        row = get_showcase(conn, showcase_id)
        if row["status"] not in {
            "publish_media_ready",
            "review_rejected",
            "drafted",
        }:
            raise StageGateError(
                f"showcase cannot accept a caption from status {row['status']}"
            )
        verify_media(row)
        verify_publish_media(row)
        verify_source_context(conn, row)
        caption_text = _validate_caption(
            caption,
            row["canonical_url"],
            row["source_creator"],
        )
        input_payload = _caption_input(row)
        input_hash = json_hash(input_payload)
        caption_hash = text_hash(caption_text)
        if row["status"] == "drafted":
            if (
                row["caption_text"] == caption_text
                and row["caption_hash"] == caption_hash
                and row["caption_input_hash"] == input_hash
                and row["drafted_by"] == actor
                and row["draft_context_id"] == context_id
            ):
                return row
            raise StageGateError(
                "an existing caption must be rejected before it can be replaced"
            )
        timestamp = now_iso()
        changed = conn.execute(
            """
            UPDATE tiktok_comment_showcase_jobs SET
                status='drafted', caption_input_json=?,
                caption_input_hash=?, caption_text=?, caption_hash=?,
                caption_policy_version=?, drafted_by=?,
                draft_context_id=?, drafted_at=?,
                link_mode='caption_text_unverified_clickability', link_url=?,
                review_status='', review_json='{}', review_hash='',
                reviewed_by='', review_context_id='', reviewed_at='',
                presentation_json='{}', presentation_hash='',
                presentation_token_hash='', presented_to='', presented_at='',
                authorization_by='', authorized_at='',
                authorization_token_hash='',
                authorization_presentation_hash='',
                authorization_media_sha256='',
                authorization_caption_hash='',
                authorization_review_hash='',
                authorization_source_binding_hash='',
                authorization_account='', authorization_link_url='',
                authorization_json='{}', authorization_hash='',
                output_fingerprint='', active_attempt_id='',
                error='', updated_at=?
            WHERE showcase_id=?
              AND status IN ('publish_media_ready', 'review_rejected')
            """,
            (
                canonical_json(input_payload),
                input_hash,
                caption_text,
                caption_hash,
                CAPTION_POLICY_VERSION,
                actor,
                context_id,
                timestamp,
                row["canonical_url"],
                timestamp,
                str(showcase_id),
            ),
        ).rowcount
        if changed != 1:
            raise StageGateError("caption state changed before storage")
        return get_showcase(conn, showcase_id)


def _verify_caption_bindings(row: dict[str, Any]) -> None:
    expected_input = _caption_input(row)
    if (
        row["caption_input_json"] != canonical_json(expected_input)
        or row["caption_input_hash"] != json_hash(expected_input)
    ):
        raise StageGateError("caption input binding changed")
    if (
        text_hash(row["caption_text"]) != row["caption_hash"]
        or row["caption_policy_version"] != CAPTION_POLICY_VERSION
    ):
        raise StageGateError("caption text or policy binding changed")
    _validate_caption(
        row["caption_text"],
        row["canonical_url"],
        row["source_creator"],
    )


def review_caption(
    conn: sqlite3.Connection,
    *,
    showcase_id: str,
    reviewer: str,
    review_context_id: str,
    decision: dict[str, Any],
) -> dict[str, Any]:
    """Store a distinct built-in-AI critic decision for the exact draft."""

    actor = require_builtin_ai_actor(reviewer, "caption review")
    context_id = str(review_context_id or "").strip()
    if not context_id:
        raise StageGateError("caption review requires a context ID")
    if not isinstance(decision, dict) or not isinstance(
        decision.get("approved"), bool
    ):
        raise StageGateError("review decision requires a Boolean approved field")
    ensure_schema(conn)
    with _transaction(conn):
        row = get_showcase(conn, showcase_id)
        if row["status"] != "drafted":
            raise StageGateError(
                f"showcase cannot be reviewed from status {row['status']}"
            )
        if context_id.casefold() == str(row["draft_context_id"]).casefold():
            raise StageGateError(
                "caption reviewer must use a context independent from drafting"
            )
        if actor.casefold() == str(row["drafted_by"]).casefold():
            raise StageGateError(
                "caption reviewer identity must differ from the drafter"
            )
        verify_media(row)
        verify_publish_media(row)
        verify_source_context(conn, row)
        _verify_caption_bindings(row)
        approved = decision["approved"]
        if approved:
            failed_checks = [
                check
                for check in REQUIRED_REVIEW_CHECKS
                if decision.get(check) is not True
            ]
            if failed_checks:
                raise StageGateError(
                    "approved review lacks required checks: "
                    + ", ".join(failed_checks)
                )
        timestamp = now_iso()
        review_payload = {
            "schema_version": "tiktok-comment-showcase-review-v1",
            "showcase_id": row["showcase_id"],
            "caption_input_hash": row["caption_input_hash"],
            "caption_hash": row["caption_hash"],
            "raw_capture_sha256": row["media_sha256"],
            "capture_proof_hash": row["capture_proof_hash"],
            "publish_media_sha256": row["publish_media_sha256"],
            "publish_media_public_url": row["publish_media_public_url"],
            "publish_media_binding_hash": row["publish_media_binding_hash"],
            "source_binding_hash": row["source_binding_hash"],
            "source_context_hash": row["source_context_hash"],
            "canonical_url": row["canonical_url"],
            "posting_account": row["posting_account"],
            "reviewer": actor,
            "review_context_id": context_id,
            "reviewed_at": timestamp,
            "decision": decision,
        }
        review_hash = json_hash(review_payload)
        next_status = "reviewed" if approved else "review_rejected"
        conn.execute(
            """
            UPDATE tiktok_comment_showcase_jobs SET
                status=?, review_status=?, review_json=?, review_hash=?,
                reviewed_by=?, review_context_id=?, reviewed_at=?,
                presentation_json='{}', presentation_hash='',
                presentation_token_hash='', presented_to='', presented_at='',
                authorization_by='', authorized_at='',
                authorization_token_hash='',
                authorization_presentation_hash='',
                authorization_media_sha256='',
                authorization_caption_hash='',
                authorization_review_hash='',
                authorization_source_binding_hash='',
                authorization_account='', authorization_link_url='',
                authorization_json='{}', authorization_hash='',
                output_fingerprint='', error=?, updated_at=?
            WHERE showcase_id=? AND status='drafted'
            """,
            (
                next_status,
                "approved" if approved else "rejected",
                canonical_json(review_payload),
                review_hash,
                actor,
                context_id,
                timestamp,
                "" if approved else str(decision.get("reason") or "review rejected"),
                timestamp,
                str(showcase_id),
            ),
        )
        return get_showcase(conn, showcase_id)


def _verify_review_bindings(row: dict[str, Any]) -> None:
    if row["review_status"] != "approved" or not row["review_hash"]:
        raise StageGateError("showcase has no approved independent review")
    review = _json_object(row["review_json"], "stored showcase review")
    if json_hash(review) != row["review_hash"]:
        raise StageGateError("stored showcase review hash changed")
    if (
        review.get("caption_input_hash") != row["caption_input_hash"]
        or review.get("caption_hash") != row["caption_hash"]
        or review.get("raw_capture_sha256") != row["media_sha256"]
        or review.get("capture_proof_hash") != row["capture_proof_hash"]
        or review.get("publish_media_sha256")
        != row["publish_media_sha256"]
        or review.get("publish_media_public_url")
        != row["publish_media_public_url"]
        or review.get("publish_media_binding_hash")
        != row["publish_media_binding_hash"]
        or review.get("source_binding_hash") != row["source_binding_hash"]
        or review.get("source_context_hash") != row["source_context_hash"]
        or review.get("canonical_url") != row["canonical_url"]
        or review.get("posting_account") != row["posting_account"]
        or review.get("reviewer") != row["reviewed_by"]
        or review.get("review_context_id") != row["review_context_id"]
    ):
        raise StageGateError("independent review no longer matches the draft")
    decision = review.get("decision")
    if not isinstance(decision, dict) or decision.get("approved") is not True:
        raise StageGateError("independent review is not approved")
    if any(decision.get(check) is not True for check in REQUIRED_REVIEW_CHECKS):
        raise StageGateError("independent review checks are incomplete")
    if str(row["review_context_id"]).casefold() == str(
        row["draft_context_id"]
    ).casefold():
        raise StageGateError("review context is not independent from drafting")
    if str(row["reviewed_by"]).casefold() == str(row["drafted_by"]).casefold():
        raise StageGateError("reviewer identity is not independent from drafting")


def _presentation_payload(
    row: dict[str, Any],
    *,
    presented_to: str,
    presented_at: str,
) -> dict[str, Any]:
    return {
        "schema_version": PRESENTATION_SCHEMA,
        "showcase_id": row["showcase_id"],
        "source_publication_id": row["source_publication_id"],
        "source_receipt_id": row["source_receipt_id"],
        "source_master_attempt_id": row["source_master_attempt_id"],
        "source_post_id": row["source_post_id"],
        "canonical_url": row["canonical_url"],
        "source_creator": row["source_creator"],
        "source_comment_id": row["source_comment_id"],
        "source_comment_text": row["source_comment_text"],
        "source_comment_text_hash": row["source_comment_text_hash"],
        "source_binding_hash": row["source_binding_hash"],
        "source_context_hash": row["source_context_hash"],
        "posting_account": row["posting_account"],
        "raw_capture_path": row["media_path"],
        "raw_capture_sha256": row["media_sha256"],
        "raw_capture_size_bytes": int(row["media_size_bytes"]),
        "raw_capture_mime_type": row["media_mime_type"],
        "capture_proof_hash": row["capture_proof_hash"],
        "publish_media_path": row["publish_media_path"],
        "publish_media_sha256": row["publish_media_sha256"],
        "publish_media_size_bytes": int(row["publish_media_size_bytes"]),
        "publish_media_mime_type": row["publish_media_mime_type"],
        "publish_media_public_url": row["publish_media_public_url"],
        "publish_media_binding_hash": row["publish_media_binding_hash"],
        "caption_text": row["caption_text"],
        "caption_hash": row["caption_hash"],
        "caption_input_hash": row["caption_input_hash"],
        "link_mode": row["link_mode"],
        "link_url": row["link_url"],
        "post_settings": showcase_post_settings(),
        "consent_declaration": TIKTOK_MUSIC_USAGE_CONSENT,
        "review_hash": row["review_hash"],
        "presented_to": presented_to,
        "presented_at": presented_at,
    }


def present_showcase(
    conn: sqlite3.Connection,
    *,
    showcase_id: str,
    presented_to: str,
) -> dict[str, Any]:
    """Present the exact image/caption/link/account and issue a one-time token."""

    human = require_named_human(presented_to, "showcase presentation")
    ensure_schema(conn)
    with _transaction(conn):
        row = get_showcase(conn, showcase_id)
        if row["status"] not in {"reviewed", "presented"}:
            raise StageGateError(
                f"showcase cannot be presented from status {row['status']}"
            )
        verify_media(row)
        verify_publish_media(row)
        verify_source_context(conn, row)
        _verify_caption_bindings(row)
        _verify_review_bindings(row)
        timestamp = now_iso()
        payload = _presentation_payload(
            row,
            presented_to=human,
            presented_at=timestamp,
        )
        presentation_hash = json_hash(payload)
        token = secrets.token_urlsafe(32)
        token_hash = text_hash(f"{presentation_hash}\x1f{token}")
        changed = conn.execute(
            """
            UPDATE tiktok_comment_showcase_jobs SET
                status='presented', presentation_json=?,
                presentation_hash=?, presentation_token_hash=?,
                presented_to=?, presented_at=?,
                authorization_by='', authorized_at='',
                authorization_token_hash='',
                authorization_presentation_hash='',
                authorization_media_sha256='',
                authorization_caption_hash='',
                authorization_review_hash='',
                authorization_source_binding_hash='',
                authorization_account='', authorization_link_url='',
                authorization_json='{}', authorization_hash='',
                output_fingerprint='', error='', updated_at=?
            WHERE showcase_id=? AND status IN ('reviewed', 'presented')
            """,
            (
                canonical_json(payload),
                presentation_hash,
                token_hash,
                human,
                timestamp,
                timestamp,
                str(showcase_id),
            ),
        ).rowcount
        if changed != 1:
            raise StageGateError("showcase changed before presentation storage")
        return {
            **payload,
            "presentation_hash": presentation_hash,
            "approval_token": token,
        }


def authorize_showcase(
    conn: sqlite3.Connection,
    *,
    showcase_id: str,
    authorized_by: str,
    expected_presentation_hash: str,
    approval_token: str,
    expected_media_sha256: str,
    expected_caption_hash: str,
    expected_review_hash: str,
) -> dict[str, Any]:
    """Store exact, presentation-bound authorization from the same human."""

    human = require_named_human(authorized_by, "showcase authorization")
    ensure_schema(conn)
    with _transaction(conn):
        row = get_showcase(conn, showcase_id)
        if row["status"] != "presented":
            raise StageGateError(
                f"showcase cannot be authorized from status {row['status']}"
            )
        if human != row["presented_to"]:
            raise StageGateError(
                "showcase authorizer must match the named presentation recipient"
            )
        verify_media(row)
        verify_publish_media(row)
        verify_source_context(conn, row)
        _verify_caption_bindings(row)
        _verify_review_bindings(row)
        if (
            str(expected_presentation_hash) != row["presentation_hash"]
            or str(expected_media_sha256) != row["publish_media_sha256"]
            or str(expected_caption_hash) != row["caption_hash"]
            or str(expected_review_hash) != row["review_hash"]
        ):
            raise StageGateError("showcase authorization hashes do not match")
        presentation = _json_object(
            row["presentation_json"],
            "stored showcase presentation",
        )
        expected_presentation = _presentation_payload(
            row,
            presented_to=row["presented_to"],
            presented_at=row["presented_at"],
        )
        supplied_token_hash = text_hash(
            f"{row['presentation_hash']}\x1f{str(approval_token or '')}"
        )
        if (
            presentation != expected_presentation
            or json_hash(presentation) != row["presentation_hash"]
            or not secrets.compare_digest(
                str(row["presentation_token_hash"] or ""),
                supplied_token_hash,
            )
        ):
            raise StageGateError(
                "showcase presentation or one-time approval token is invalid"
            )
        timestamp = now_iso()
        authorization = {
            "schema_version": AUTHORIZATION_SCHEMA,
            "showcase_id": row["showcase_id"],
            "authorized_by": human,
            "authorized_at": timestamp,
            "presentation_hash": row["presentation_hash"],
            "media_sha256": row["publish_media_sha256"],
            "media_public_url": row["publish_media_public_url"],
            "caption_hash": row["caption_hash"],
            "review_hash": row["review_hash"],
            "source_binding_hash": row["source_binding_hash"],
            "source_context_hash": row["source_context_hash"],
            "posting_account": row["posting_account"],
            "canonical_url": row["canonical_url"],
            "link_mode": row["link_mode"],
            "link_url": row["link_url"],
            "post_settings": showcase_post_settings(),
            "consent_declaration": TIKTOK_MUSIC_USAGE_CONSENT,
        }
        authorization_hash = json_hash(authorization)
        output_fingerprint = stable_id(
            "tiktok-comment-showcase-output-v1",
            row["posting_account"],
            row["publish_media_sha256"],
            row["publish_media_public_url"],
            row["caption_hash"],
            json_hash(showcase_post_settings()),
            length=64,
        )
        changed = conn.execute(
            """
            UPDATE tiktok_comment_showcase_jobs SET
                status='authorized', authorization_by=?, authorized_at=?,
                authorization_token_hash=?,
                authorization_presentation_hash=?,
                authorization_media_sha256=?,
                authorization_caption_hash=?,
                authorization_review_hash=?,
                authorization_source_binding_hash=?,
                authorization_account=?, authorization_link_url=?,
                authorization_json=?, authorization_hash=?,
                output_fingerprint=?, presentation_token_hash='',
                error='', updated_at=?
            WHERE showcase_id=? AND status='presented'
              AND presentation_hash=? AND presentation_token_hash=?
            """,
            (
                human,
                timestamp,
                supplied_token_hash,
                row["presentation_hash"],
                row["publish_media_sha256"],
                row["caption_hash"],
                row["review_hash"],
                row["source_binding_hash"],
                row["posting_account"],
                row["link_url"],
                canonical_json(authorization),
                authorization_hash,
                output_fingerprint,
                timestamp,
                str(showcase_id),
                row["presentation_hash"],
                row["presentation_token_hash"],
            ),
        ).rowcount
        if changed != 1:
            raise StageGateError(
                "showcase presentation token was already consumed or changed"
            )
        return {
            **authorization,
            "authorization_hash": authorization_hash,
            "output_fingerprint": output_fingerprint,
        }


def _verify_authorization_bindings(row: dict[str, Any]) -> None:
    authorization = _json_object(
        row["authorization_json"],
        "stored showcase authorization",
    )
    if (
        not row["authorization_by"]
        or is_automation_identity(row["authorization_by"])
        or row["authorization_by"] != row["presented_to"]
        or row["authorization_presentation_hash"] != row["presentation_hash"]
        or row["authorization_media_sha256"]
        != row["publish_media_sha256"]
        or row["authorization_caption_hash"] != row["caption_hash"]
        or row["authorization_review_hash"] != row["review_hash"]
        or row["authorization_source_binding_hash"] != row["source_binding_hash"]
        or row["authorization_account"] != row["posting_account"]
        or row["authorization_link_url"] != row["canonical_url"]
        or row["link_url"] != row["canonical_url"]
        or row["link_mode"] != "caption_text_unverified_clickability"
        or json_hash(authorization) != row["authorization_hash"]
    ):
        raise StageGateError("showcase live authorization no longer matches")
    required = {
        "schema_version": AUTHORIZATION_SCHEMA,
        "showcase_id": row["showcase_id"],
        "authorized_by": row["authorization_by"],
        "authorized_at": row["authorized_at"],
        "presentation_hash": row["presentation_hash"],
        "media_sha256": row["publish_media_sha256"],
        "media_public_url": row["publish_media_public_url"],
        "caption_hash": row["caption_hash"],
        "review_hash": row["review_hash"],
        "source_binding_hash": row["source_binding_hash"],
        "source_context_hash": row["source_context_hash"],
        "posting_account": row["posting_account"],
        "canonical_url": row["canonical_url"],
        "link_mode": row["link_mode"],
        "link_url": row["link_url"],
        "post_settings": showcase_post_settings(),
        "consent_declaration": TIKTOK_MUSIC_USAGE_CONSENT,
    }
    if authorization != required:
        raise StageGateError("stored showcase authorization payload changed")


def claim_showcase(
    conn: sqlite3.Connection,
    *,
    showcase_id: str,
    expected_media_sha256: str,
    expected_caption_hash: str,
    expected_authorization_hash: str,
    reserve_hook: Callable[[sqlite3.Connection, dict[str, Any]], Any]
    | None = None,
) -> dict[str, Any]:
    """Atomically reserve one exact authorized showcase for browser submission."""

    ensure_schema(conn)
    with _transaction(conn, require_durable_root=True):
        row = get_showcase(conn, showcase_id)
        if row["status"] not in {"authorized", "retryable"}:
            raise StageGateError(
                f"showcase cannot be claimed from status {row['status']}"
            )
        verify_media(row)
        verify_publish_media(row)
        verify_source_context(conn, row)
        _verify_caption_bindings(row)
        _verify_review_bindings(row)
        _verify_authorization_bindings(row)
        if (
            str(expected_media_sha256) != row["publish_media_sha256"]
            or str(expected_caption_hash) != row["caption_hash"]
            or str(expected_authorization_hash) != row["authorization_hash"]
        ):
            raise StageGateError("claim hashes do not match the authorized showcase")
        duplicate = conn.execute(
            f"""
            SELECT showcase_id, status
            FROM tiktok_comment_showcase_jobs
            WHERE posting_account=? AND output_fingerprint=?
              AND showcase_id<>?
              AND status IN ({", ".join("?" for _ in BLOCKING_OUTPUT_STATUSES)})
            LIMIT 1
            """,
            (
                row["posting_account"],
                row["output_fingerprint"],
                row["showcase_id"],
                *sorted(BLOCKING_OUTPUT_STATUSES),
            ),
        ).fetchone()
        if duplicate:
            raise StageGateError(
                "the same account/media/caption output is already active or published"
            )
        attempt_number = int(row["attempt_count"] or 0) + 1
        attempt_id = stable_id(
            "tiktok-comment-showcase-attempt-v1",
            row["showcase_id"],
            attempt_number,
        )
        timestamp = now_iso()
        reserve_payload = {
            "operation": "reserve",
            "showcase_id": row["showcase_id"],
            "attempt_id": attempt_id,
            "attempt_number": attempt_number,
            "source_comment_attempt_id": row["source_master_attempt_id"],
            "source_publication_id": row["source_publication_id"],
            "source_post_id": row["source_post_id"],
            "canonical_url": row["canonical_url"],
            "posting_account": row["posting_account"],
            "media_sha256": row["publish_media_sha256"],
            "media_public_url": row["publish_media_public_url"],
            "caption_hash": row["caption_hash"],
            "authorization_hash": row["authorization_hash"],
            "output_fingerprint": row["output_fingerprint"],
            "reserved_at": timestamp,
        }
        master_showcase_attempt_id = ""
        if reserve_hook is not None:
            hook_result = reserve_hook(conn, dict(reserve_payload))
            if isinstance(hook_result, dict):
                master_showcase_attempt_id = str(
                    hook_result.get("master_showcase_attempt_id")
                    or hook_result.get("master_attempt_id")
                    or hook_result.get("attempt_id")
                    or ""
                ).strip()
            else:
                master_showcase_attempt_id = str(hook_result or "").strip()
            if not master_showcase_attempt_id:
                raise StageGateError(
                    "master reserve hook returned no showcase attempt ID"
                )
        conn.execute(
            """
            INSERT INTO tiktok_comment_showcase_attempts (
                attempt_id, showcase_id, attempt_number, posting_account,
                media_sha256, caption_hash, authorization_hash,
                output_fingerprint, master_showcase_attempt_id,
                state, reserved_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved', ?)
            """,
            (
                attempt_id,
                row["showcase_id"],
                attempt_number,
                row["posting_account"],
                row["publish_media_sha256"],
                row["caption_hash"],
                row["authorization_hash"],
                row["output_fingerprint"],
                master_showcase_attempt_id,
                timestamp,
            ),
        )
        changed = conn.execute(
            """
            UPDATE tiktok_comment_showcase_jobs SET
                status='reserved', active_attempt_id=?,
                active_master_showcase_attempt_id=?,
                attempt_count=?, error='', updated_at=?
            WHERE showcase_id=? AND status IN ('authorized', 'retryable')
              AND authorization_hash=?
            """,
            (
                attempt_id,
                master_showcase_attempt_id,
                attempt_number,
                timestamp,
                row["showcase_id"],
                row["authorization_hash"],
            ),
        ).rowcount
        if changed != 1:
            raise StageGateError("showcase authorization changed before claim")
        return {
            "showcase_id": row["showcase_id"],
            "attempt_id": attempt_id,
            "attempt_number": attempt_number,
            "master_showcase_attempt_id": master_showcase_attempt_id,
            "master_attempt_id": master_showcase_attempt_id,
            "posting_account": row["posting_account"],
            "media_path": row["publish_media_path"],
            "media_sha256": row["publish_media_sha256"],
            "media_public_url": row["publish_media_public_url"],
            "caption_text": row["caption_text"],
            "caption_hash": row["caption_hash"],
            "canonical_url": row["canonical_url"],
            "authorization_hash": row["authorization_hash"],
            "status": "reserved",
            "reserve_payload": reserve_payload,
        }


def mark_submit_intent(
    conn: sqlite3.Connection,
    *,
    showcase_id: str,
    attempt_id: str,
    submit_intent_hook: Callable[
        [sqlite3.Connection, dict[str, Any]],
        Any,
    ]
    | None = None,
) -> dict[str, Any]:
    """Durably fence a browser submission immediately before its click."""

    ensure_schema(conn)
    with _transaction(conn, require_durable_root=True):
        row = get_showcase(conn, showcase_id)
        if (
            row["status"] != "reserved"
            or row["active_attempt_id"] != str(attempt_id)
        ):
            raise StageGateError("showcase has no matching active reservation")
        attempt = conn.execute(
            """
            SELECT state, media_sha256, caption_hash, authorization_hash,
                   master_showcase_attempt_id
            FROM tiktok_comment_showcase_attempts
            WHERE attempt_id=? AND showcase_id=?
            """,
            (str(attempt_id), str(showcase_id)),
        ).fetchone()
        if not attempt or str(attempt[0]) != "reserved":
            raise StageGateError("showcase attempt is not reserved")
        verify_media(row)
        verify_publish_media(row)
        verify_source_context(conn, row)
        _verify_caption_bindings(row)
        _verify_review_bindings(row)
        _verify_authorization_bindings(row)
        if (
            attempt[1] != row["publish_media_sha256"]
            or attempt[2] != row["caption_hash"]
            or attempt[3] != row["authorization_hash"]
        ):
            raise StageGateError("reserved attempt bindings changed")
        timestamp = now_iso()
        intent_payload = {
            "operation": "submit_intent",
            "showcase_id": row["showcase_id"],
            "attempt_id": str(attempt_id),
            "master_showcase_attempt_id": str(attempt[4] or ""),
            "source_comment_attempt_id": row["source_master_attempt_id"],
            "source_post_id": row["source_post_id"],
            "posting_account": row["posting_account"],
            "media_sha256": row["publish_media_sha256"],
            "media_public_url": row["publish_media_public_url"],
            "caption_hash": row["caption_hash"],
            "authorization_hash": row["authorization_hash"],
            "submit_intent_at": timestamp,
        }
        if submit_intent_hook is not None:
            if not intent_payload["master_showcase_attempt_id"]:
                raise StageGateError(
                    "submit-intent hook requires a bound master showcase attempt"
                )
            submit_intent_hook(conn, dict(intent_payload))
        conn.execute(
            """
            UPDATE tiktok_comment_showcase_attempts
            SET state='submit_intent', submit_intent_at=?
            WHERE attempt_id=? AND state='reserved'
            """,
            (timestamp, str(attempt_id)),
        )
        conn.execute(
            """
            UPDATE tiktok_comment_showcase_jobs
            SET status='submit_intent', updated_at=?
            WHERE showcase_id=? AND status='reserved'
              AND active_attempt_id=?
            """,
            (timestamp, str(showcase_id), str(attempt_id)),
        )
        return {
            "showcase_id": str(showcase_id),
            "attempt_id": str(attempt_id),
            "master_showcase_attempt_id": str(attempt[4] or ""),
            "status": "submit_intent",
            "submit_intent_at": timestamp,
            "submit_intent_payload": intent_payload,
        }


def _validated_publish_id(value: Any) -> str:
    publish_id = str(value or "").strip()
    if (
        not publish_id
        or len(publish_id) > 512
        or any(ord(character) < 0x21 or ord(character) > 0x7E for character in publish_id)
    ):
        raise StageGateError(
            "TikTok publish ID must be a non-empty printable identifier"
        )
    return publish_id


def checkpoint_publish_id(
    conn: sqlite3.Connection,
    *,
    showcase_id: str,
    attempt_id: str,
    publish_id: str,
    checkpoint_hook: Callable[
        [sqlite3.Connection, dict[str, Any]],
        Any,
    ]
    | None = None,
) -> dict[str, Any]:
    """Atomically bind TikTok's API publish ID before the first status poll."""

    normalized_publish_id = _validated_publish_id(publish_id)
    ensure_schema(conn)
    with _transaction(conn, require_durable_root=True):
        row = get_showcase(conn, showcase_id)
        if (
            row["status"] != "submit_intent"
            or row["active_attempt_id"] != str(attempt_id)
        ):
            raise StageGateError(
                "showcase has no matching submit-intent attempt for publish ID"
            )
        attempt = conn.execute(
            """
            SELECT state, master_showcase_attempt_id, publish_id,
                   publish_id_checkpoint_at, media_sha256, caption_hash,
                   authorization_hash
            FROM tiktok_comment_showcase_attempts
            WHERE attempt_id=? AND showcase_id=?
            """,
            (str(attempt_id), str(showcase_id)),
        ).fetchone()
        if not attempt or str(attempt[0]) != "submit_intent":
            raise StageGateError("showcase attempt is not in submit intent")
        if (
            attempt[4] != row["publish_media_sha256"]
            or attempt[5] != row["caption_hash"]
            or attempt[6] != row["authorization_hash"]
        ):
            raise StageGateError("submit-intent attempt bindings changed")
        existing = str(attempt[2] or "")
        if existing and existing != normalized_publish_id:
            raise StageGateError("showcase attempt publish ID changed")
        timestamp = str(attempt[3] or now_iso())
        payload = {
            "operation": "publish_id_checkpoint",
            "showcase_id": row["showcase_id"],
            "attempt_id": str(attempt_id),
            "master_showcase_attempt_id": str(attempt[1] or ""),
            "source_comment_attempt_id": row["source_master_attempt_id"],
            "source_post_id": row["source_post_id"],
            "posting_account": row["posting_account"],
            "media_sha256": row["publish_media_sha256"],
            "caption_hash": row["caption_hash"],
            "authorization_hash": row["authorization_hash"],
            "publish_id": normalized_publish_id,
            "publish_id_checkpoint_at": timestamp,
        }
        if checkpoint_hook is not None:
            if not payload["master_showcase_attempt_id"]:
                raise StageGateError(
                    "publish-ID hook requires a bound master showcase attempt"
                )
            checkpoint_hook(conn, dict(payload))
        if not existing:
            changed = conn.execute(
                """
                UPDATE tiktok_comment_showcase_attempts
                SET publish_id=?, publish_id_checkpoint_at=?
                WHERE attempt_id=? AND showcase_id=?
                  AND state='submit_intent' AND publish_id=''
                """,
                (
                    normalized_publish_id,
                    timestamp,
                    str(attempt_id),
                    str(showcase_id),
                ),
            ).rowcount
            if changed != 1:
                raise StageGateError(
                    "showcase attempt changed before publish-ID checkpoint"
                )
        return {
            "showcase_id": str(showcase_id),
            "attempt_id": str(attempt_id),
            "master_showcase_attempt_id": str(attempt[1] or ""),
            "status": "submit_intent",
            "publish_id": normalized_publish_id,
            "publish_id_checkpoint_at": timestamp,
            "checkpoint_payload": payload,
        }


def record_outcome(
    conn: sqlite3.Connection,
    *,
    showcase_id: str,
    attempt_id: str,
    outcome: str,
    remote_post_id: str = "",
    remote_post_url: str = "",
    visible: bool = False,
    persisted: bool = False,
    response: dict[str, Any] | None = None,
    error: str = "",
    outcome_hook: Callable[[sqlite3.Connection, dict[str, Any]], Any]
    | None = None,
) -> dict[str, Any]:
    """Resolve one attempt under pre/post-submit fail-closed rules."""

    requested = str(outcome or "").strip().casefold()
    if requested not in {
        "published",
        "confirmed",
        "failed",
        "pre_submit_failed",
        "uncertain",
    }:
        raise ValueError(f"unsupported showcase outcome: {outcome}")
    ensure_schema(conn)
    with _transaction(conn, require_durable_root=True):
        row = get_showcase(conn, showcase_id)
        attempt = conn.execute(
            """
            SELECT * FROM tiktok_comment_showcase_attempts
            WHERE attempt_id=? AND showcase_id=?
            """,
            (str(attempt_id), str(showcase_id)),
        ).fetchone()
        if not attempt:
            raise StageGateError("showcase attempt was not found")
        attempt_item = {key: attempt[key] for key in attempt.keys()}
        prior = str(attempt_item["state"])
        response_payload = response or {}
        if not isinstance(response_payload, dict):
            raise StageGateError("showcase outcome response must be an object")
        reported_publish_id = str(
            response_payload.get("publish_id") or ""
        ).strip()
        checkpointed_publish_id = str(
            attempt_item.get("publish_id") or ""
        ).strip()
        if reported_publish_id:
            reported_publish_id = _validated_publish_id(reported_publish_id)
        if (
            checkpointed_publish_id
            and reported_publish_id
            and checkpointed_publish_id != reported_publish_id
        ):
            raise StageGateError(
                "showcase outcome publish ID differs from its checkpoint"
            )

        if prior == "confirmed":
            if requested not in {"published", "confirmed"}:
                raise StageGateError("a confirmed showcase cannot be downgraded")
            if (
                str(remote_post_id or attempt_item["remote_post_id"])
                != attempt_item["remote_post_id"]
                or str(remote_post_url or attempt_item["remote_post_url"])
                != attempt_item["remote_post_url"]
            ):
                raise StageGateError("confirmed showcase receipt changed")
            return {
                "showcase_id": str(showcase_id),
                "attempt_id": str(attempt_id),
                "status": "published",
                "attempt_state": "confirmed",
                "remote_post_id": attempt_item["remote_post_id"],
                "remote_post_url": attempt_item["remote_post_url"],
            }
        if prior == "retryable" and requested in {"failed", "pre_submit_failed"}:
            return {
                "showcase_id": str(showcase_id),
                "attempt_id": str(attempt_id),
                "status": "retryable",
                "attempt_state": "retryable",
            }
        if prior == "uncertain" and requested == "uncertain":
            return {
                "showcase_id": str(showcase_id),
                "attempt_id": str(attempt_id),
                "status": "uncertain",
                "attempt_state": "uncertain",
            }
        if prior not in {"reserved", "submit_intent", "uncertain"}:
            raise StageGateError(
                f"showcase attempt cannot be resolved from state {prior}"
            )
        if row["active_attempt_id"] != str(attempt_id):
            raise StageGateError("showcase active attempt binding changed")

        timestamp = now_iso()
        if requested in {"published", "confirmed"}:
            if prior not in {"submit_intent", "uncertain"}:
                raise StageGateError(
                    "showcase cannot be confirmed without durable submit intent"
                )
            remote_id = str(remote_post_id or "").strip()
            remote_url, remote_creator, url_remote_id = canonical_tiktok_output_url(
                remote_post_url
            )
            if (
                not remote_id
                or remote_id != url_remote_id
                or normalize_account(remote_creator) != row["posting_account"]
                or visible is not True
                or persisted is not True
            ):
                raise StageGateError(
                    "confirmed showcase requires a matching reachable remote "
                    "post on the authorized account"
                )
            verify_media(row)
            verify_publish_media(row)
            verify_source_context(conn, row)
            _verify_caption_bindings(row)
            _verify_review_bindings(row)
            _verify_authorization_bindings(row)
            attempt_state = "confirmed"
            job_status = "published"
            resolved_error = ""
        elif requested == "uncertain" or prior in {"submit_intent", "uncertain"}:
            remote_id = str(remote_post_id or "")
            remote_url = str(remote_post_url or "")
            attempt_state = "uncertain"
            job_status = "uncertain"
            resolved_error = str(
                error
                or "submission may have occurred; reconciliation is required"
            )
        else:
            remote_id = ""
            remote_url = ""
            attempt_state = "retryable"
            job_status = "retryable"
            resolved_error = str(
                error or "attempt failed before durable submit intent"
            )

        outcome_payload = {
            "operation": "outcome",
            "showcase_id": row["showcase_id"],
            "attempt_id": str(attempt_id),
            "master_showcase_attempt_id": str(
                attempt_item.get("master_showcase_attempt_id") or ""
            ),
            "source_comment_attempt_id": row["source_master_attempt_id"],
            "source_post_id": row["source_post_id"],
            "posting_account": row["posting_account"],
            "prior_state": prior,
            "attempt_state": attempt_state,
            "job_status": job_status,
            "remote_post_id": remote_id,
            "remote_post_url": remote_url,
            "visible": bool(visible),
            "persisted": bool(persisted),
            "media_sha256": row["publish_media_sha256"],
            "media_public_url": row["publish_media_public_url"],
            "caption_hash": row["caption_hash"],
            "authorization_hash": row["authorization_hash"],
            "response": response_payload,
            "error": resolved_error,
            "resolved_at": timestamp,
        }
        if outcome_hook is not None:
            if not outcome_payload["master_showcase_attempt_id"]:
                raise StageGateError(
                    "outcome hook requires a bound master showcase attempt"
                )
            outcome_hook(conn, dict(outcome_payload))

        conn.execute(
            """
            UPDATE tiktok_comment_showcase_attempts SET
                state=?, resolved_at=?, remote_post_id=?,
                remote_post_url=?, visible=?, persisted=?,
                response_json=?, error=?
            WHERE attempt_id=?
            """,
            (
                attempt_state,
                timestamp,
                remote_id,
                remote_url,
                1 if visible else 0,
                1 if persisted else 0,
                canonical_json(response_payload),
                resolved_error,
                str(attempt_id),
            ),
        )
        conn.execute(
            """
            UPDATE tiktok_comment_showcase_jobs SET
                status=?,
                active_attempt_id=CASE
                    WHEN ?='retryable' THEN '' ELSE active_attempt_id END,
                active_master_showcase_attempt_id=CASE
                    WHEN ?='retryable' THEN ''
                    ELSE active_master_showcase_attempt_id END,
                remote_post_id=CASE
                    WHEN ?='published' THEN ? ELSE remote_post_id END,
                remote_post_url=CASE
                    WHEN ?='published' THEN ? ELSE remote_post_url END,
                published_at=CASE
                    WHEN ?='published' THEN ? ELSE published_at END,
                error=?, updated_at=?
            WHERE showcase_id=? AND active_attempt_id=?
            """,
            (
                job_status,
                job_status,
                job_status,
                job_status,
                remote_id,
                job_status,
                remote_url,
                job_status,
                timestamp,
                resolved_error,
                timestamp,
                str(showcase_id),
                str(attempt_id),
            ),
        )
        return {
            "showcase_id": str(showcase_id),
            "attempt_id": str(attempt_id),
            "status": job_status,
            "attempt_state": attempt_state,
            "master_showcase_attempt_id": str(
                attempt_item.get("master_showcase_attempt_id") or ""
            ),
            "remote_post_id": remote_id,
            "remote_post_url": remote_url,
            "outcome_payload": outcome_payload,
        }


def _read_text_argument(value: str, path: str) -> str:
    if bool(value) == bool(path):
        raise StageGateError("supply exactly one inline value or file path")
    if path:
        return Path(path).read_text(encoding="utf-8")
    return value


def _public_job(item: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "showcase_id",
        "source_publication_id",
        "source_receipt_id",
        "source_master_attempt_id",
        "source_run_id",
        "source_post_id",
        "canonical_url",
        "source_creator",
        "source_comment_id",
        "posting_account",
        "status",
        "media_path",
        "media_sha256",
        "media_size_bytes",
        "media_mime_type",
        "source_context_hash",
        "publish_media_path",
        "publish_media_sha256",
        "publish_media_size_bytes",
        "publish_media_mime_type",
        "publish_media_width",
        "publish_media_height",
        "publish_media_public_url",
        "publish_media_binding_hash",
        "caption_text",
        "caption_hash",
        "link_mode",
        "link_url",
        "review_status",
        "review_hash",
        "presented_to",
        "presentation_hash",
        "authorization_by",
        "authorization_hash",
        "active_attempt_id",
        "active_master_showcase_attempt_id",
        "attempt_count",
        "remote_post_id",
        "remote_post_url",
        "published_at",
        "error",
        "created_at",
        "updated_at",
    )
    return {key: item.get(key) for key in keys}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Store and guard local TikTok comment-screenshot showcase state. "
            "This command never opens a browser or publishes."
        )
    )
    parser.add_argument("--database", required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init")

    enqueue = subparsers.add_parser("enqueue")
    enqueue.add_argument("--publication-id", required=True)
    enqueue.add_argument("--receipt-id", required=True)

    status = subparsers.add_parser("status")
    status.add_argument("--showcase-id", required=True)

    listing = subparsers.add_parser("list")
    listing.add_argument("--status", default="")

    export = subparsers.add_parser("caption-input")
    export.add_argument("--showcase-id", required=True)

    media = subparsers.add_parser("bind-publish-media")
    media.add_argument("--showcase-id", required=True)
    media.add_argument("--media-path", required=True)
    media.add_argument("--media-sha256", required=True)
    media.add_argument("--media-size-bytes", type=int, required=True)
    media.add_argument("--media-mime-type", default="image/jpeg")
    media.add_argument("--public-media-url", required=True)

    draft = subparsers.add_parser("store-caption")
    draft.add_argument("--showcase-id", required=True)
    draft.add_argument("--caption", default="")
    draft.add_argument("--caption-file", default="")
    draft.add_argument("--drafted-by", required=True)
    draft.add_argument("--draft-context-id", required=True)

    review = subparsers.add_parser("review")
    review.add_argument("--showcase-id", required=True)
    review.add_argument("--reviewer", required=True)
    review.add_argument("--review-context-id", required=True)
    review.add_argument("--decision-json", default="")
    review.add_argument("--decision-file", default="")

    show = subparsers.add_parser("show")
    show.add_argument("--showcase-id", required=True)
    show.add_argument("--presented-to", required=True)

    authorize = subparsers.add_parser("authorize")
    authorize.add_argument("--showcase-id", required=True)
    authorize.add_argument("--authorized-by", required=True)
    authorize.add_argument("--presentation-hash", required=True)
    authorize.add_argument("--approval-token", required=True)
    authorize.add_argument("--media-sha256", required=True)
    authorize.add_argument("--caption-hash", required=True)
    authorize.add_argument("--review-hash", required=True)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    conn = connect_database(args.database)
    try:
        if args.command == "init":
            result: Any = {
                "status": "ready",
                "schema_version": SHOWCASE_SCHEMA_VERSION,
                "database": str(Path(args.database).resolve()),
            }
        elif args.command == "enqueue":
            result = enqueue_confirmed_comment(
                conn,
                publication_id=args.publication_id,
                receipt_id=args.receipt_id,
            )
            result = _public_job(result)
        elif args.command == "status":
            result = _public_job(get_showcase(conn, args.showcase_id))
        elif args.command == "list":
            result = [
                _public_job(item)
                for item in list_showcases(conn, status=args.status)
            ]
        elif args.command == "caption-input":
            result = caption_input(conn, args.showcase_id)
        elif args.command == "bind-publish-media":
            result = _public_job(
                bind_publish_media(
                    conn,
                    showcase_id=args.showcase_id,
                    media_path=args.media_path,
                    media_sha256=args.media_sha256,
                    media_size_bytes=args.media_size_bytes,
                    media_mime_type=args.media_mime_type,
                    public_media_url=args.public_media_url,
                )
            )
        elif args.command == "store-caption":
            caption = _read_text_argument(args.caption, args.caption_file)
            result = _public_job(
                store_caption_draft(
                    conn,
                    showcase_id=args.showcase_id,
                    caption=caption,
                    drafted_by=args.drafted_by,
                    draft_context_id=args.draft_context_id,
                )
            )
        elif args.command == "review":
            decision_text = _read_text_argument(
                args.decision_json,
                args.decision_file,
            )
            decision = _json_object(decision_text, "review decision")
            result = _public_job(
                review_caption(
                    conn,
                    showcase_id=args.showcase_id,
                    reviewer=args.reviewer,
                    review_context_id=args.review_context_id,
                    decision=decision,
                )
            )
        elif args.command == "show":
            result = present_showcase(
                conn,
                showcase_id=args.showcase_id,
                presented_to=args.presented_to,
            )
        elif args.command == "authorize":
            result = authorize_showcase(
                conn,
                showcase_id=args.showcase_id,
                authorized_by=args.authorized_by,
                expected_presentation_hash=args.presentation_hash,
                approval_token=args.approval_token,
                expected_media_sha256=args.media_sha256,
                expected_caption_hash=args.caption_hash,
                expected_review_hash=args.review_hash,
            )
        else:  # pragma: no cover - argparse makes this unreachable.
            raise StageGateError(f"unsupported command: {args.command}")
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
